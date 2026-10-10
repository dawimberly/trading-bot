"""Quantify buy/sell cancellation (self-conflict) across RHYME / sleeves.

Measure-only. Prefers journal ``event=fill`` rows; falls back to
``logs/bot_actions.jsonl`` fill + unmatched order_submitted.

Usage (from stock-bot/):
  python scripts/analysis/rhyme_conflict_audit.py
  python scripts/analysis/rhyme_conflict_audit.py --since 2026-07-29
  python scripts/analysis/rhyme_conflict_audit.py --days 10 --book live
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
import sys

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

from modules.cost_basis import sleeve_for_symbol  # noqa: E402
from modules.paper_journal import (  # noqa: E402
    normalize_journal_df,
    prefer_fill_rows,
    row_is_entry,
    row_is_exit,
)
from trade_reconciliation import read_journal_csv  # noqa: E402

OUT_MD = Path(__file__).with_name("rhyme_conflict_audit_last.md")
OUT_JSON = Path(__file__).with_name("rhyme_conflict_audit_last.json")
FREEZE_START = date(2026, 7, 29)
ET = "America/New_York"
RHYME_RE = re.compile(r"RHYME_([A-E])", re.I)
CORE_SYMS = frozenset({"VTI", "VOO", "ITOT"})
BETA_CLUSTER = frozenset({"SPY", "VTI", "VOO", "QQQ", "IWM", "IVV"})
PAPER_BOOK = ROOT / "data" / "portal" / "users" / "dawimberly" / "books" / "alpaca_paper"
PAPER_V2_BOOK = PAPER_BOOK.parent / "alpaca_paper_v2"
LIVE_BOOK = PAPER_BOOK.parent / "alpaca_live"
_BOOK_DIRS = {
    "paper": PAPER_BOOK,
    "paper_v2": PAPER_V2_BOOK,
    "live": LIVE_BOOK,
}


def _journal_candidates(book: str) -> list[Path]:
    book_dir = _BOOK_DIRS.get(book, PAPER_BOOK)
    env_j = (os.getenv("PAPER_JOURNAL_CSV") or "").strip()
    paths = [
        book_dir / "paper_journal.csv",
        ROOT / "paper_chase_journal.csv",
        Path(env_j) if env_j else None,
        ROOT / Path(env_j) if env_j else None,
        ROOT / "paper_journal.csv",
    ]
    out: list[Path] = []
    seen: set[Path] = set()
    for p in paths:
        if p is None or not str(p):
            continue
        rp = p if p.is_absolute() else ROOT / p
        try:
            key = rp.resolve()
        except OSError:
            key = rp
        if key in seen or not rp.is_file():
            continue
        seen.add(key)
        out.append(rp)
    return out


def _load_csv(path: Path) -> pd.DataFrame:
    """Keep extra-column fill rows (same rule as recon). Do not skip bad lines."""
    try:
        df, _warnings = read_journal_csv(path)
    except Exception:
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    return normalize_journal_df(df)


def _regime_letter(raw: Any) -> str:
    s = str(raw or "").strip()
    if not s or s.lower() in ("nan", "none", ""):
        return ""
    m = RHYME_RE.search(s)
    return m.group(1).upper() if m else ""


def _side_norm(side: Any, event: Any = "") -> str:
    s = str(side or "").strip().lower().split(".")[-1]
    if s in ("buy", "long", "b"):
        return "buy"
    if s in ("sell", "short", "s", "sell_short"):
        return "sell"
    ev = str(event or "").strip().lower()
    if row_is_exit(ev, s):
        return "sell"
    if row_is_entry(ev, s):
        return "buy"
    return ""


def _notional_row(r: pd.Series) -> float:
    n = pd.to_numeric(r.get("notional"), errors="coerce")
    if pd.notna(n) and abs(float(n)) > 0:
        return abs(float(n))
    qty = pd.to_numeric(r.get("qty"), errors="coerce")
    px = pd.to_numeric(r.get("price"), errors="coerce")
    if pd.notna(qty) and pd.notna(px):
        return abs(float(qty) * float(px))
    return 0.0


def _sleeve_norm(raw: Any, symbol: str) -> str:
    s = str(raw or "").strip().lower()
    if s and s not in ("nan", "none", "null"):
        if s in ("vti", "vti_core", "core"):
            return "core"
        return s
    try:
        return str(sleeve_for_symbol(symbol) or "unknown")
    except Exception:
        return "unknown"


def _to_et_date(ts: pd.Timestamp) -> date | None:
    if ts is None or pd.isna(ts):
        return None
    t = pd.Timestamp(ts)
    try:
        if t.tzinfo is None:
            # Journal timestamps are local wall-clock; session date is NYSE/ET.
            t = t.tz_localize(ET, ambiguous="NaT", nonexistent="shift_forward")
            if pd.isna(t):
                return pd.Timestamp(ts).date()
            return t.date()
        return t.tz_convert(ET).date()
    except Exception:
        return pd.Timestamp(ts).date()


def load_journal_fills(book: str) -> tuple[pd.DataFrame, list[str]]:
    notes: list[str] = []
    fallback: pd.DataFrame | None = None
    for path in _journal_candidates(book):
        df = _load_csv(path)
        if df.empty:
            notes.append(f"journal empty: {path}")
            continue
        n = len(df)
        fills = prefer_fill_rows(df)
        n_fill = 0 if fills is None or fills.empty else len(fills)
        notes.append(f"journal {path.name}: rows={n} preferred_trade_rows={n_fill}")
        if fills is None or fills.empty:
            continue
        part = fills.copy()
        part["source"] = f"journal:{path.name}"
        part["path"] = str(path)
        ev = part["event"].astype(str).str.strip().str.lower()
        if "event" in part.columns and (ev == "fill").any():
            part["timestamp"] = pd.to_datetime(part["timestamp"], errors="coerce", utc=True)
            notes.append(f"SoT journal: {path}")
            return part, notes
        if fallback is None:
            fallback = part
    if fallback is None:
        return pd.DataFrame(), notes
    fallback["timestamp"] = pd.to_datetime(fallback["timestamp"], errors="coerce", utc=True)
    return fallback, notes


def load_jsonl_actions(path: Path | None = None) -> pd.DataFrame:
    path = path or (ROOT / "logs" / "bot_actions.jsonl")
    if not path.is_file():
        return pd.DataFrame()
    rows: list[dict] = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev = str(rec.get("event") or "").strip().lower()
            if ev not in ("fill", "order_submitted"):
                continue
            rows.append(rec)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df.get("ts"), errors="coerce", utc=True)
    df["event"] = df["event"].astype(str).str.lower()
    return df


def _actions_to_fills(actions: pd.DataFrame, book: str) -> pd.DataFrame:
    if actions.empty:
        return pd.DataFrame()
    mode = actions.get("mode", pd.Series("", index=actions.index)).astype(str).str.lower()
    want = "paper" if book == "paper" else "live"
    df = actions.loc[mode.isin([want, ""])].copy()
    if df.empty:
        return df
    df["order_id"] = df.get("order_id", pd.Series("", index=df.index)).astype(str)
    fills = df.loc[df["event"] == "fill"]
    submitted = df.loc[df["event"] == "order_submitted"]
    filled_oids = set(fills["order_id"].dropna().astype(str)) - {"", "nan", "None"}
    if not submitted.empty:
        keep_sub = ~submitted["order_id"].isin(filled_oids)
        submitted = submitted.loc[keep_sub]
    parts = [fills]
    if not submitted.empty:
        parts.append(submitted)
    out = pd.concat(parts, ignore_index=True)
    out["source"] = out["event"].map(
        lambda e: "jsonl:fill" if e == "fill" else "jsonl:order_submitted"
    )
    return out


def _normalize_fills(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    rows = []
    for _, r in raw.iterrows():
        sym = str(r.get("ticker") or r.get("symbol") or "").strip().upper()
        if not sym or sym in ("NAN", "NONE"):
            continue
        side = _side_norm(r.get("side"), r.get("event"))
        if side not in ("buy", "sell"):
            continue
        ts = pd.to_datetime(r.get("timestamp"), errors="coerce")
        if pd.isna(ts):
            continue
        if ts.tzinfo is None:
            ts = ts.tz_localize(ET, ambiguous="NaT", nonexistent="shift_forward")
        else:
            ts = ts.tz_convert(ET)
        if pd.isna(ts):
            continue
        d = ts.date()
        notional = _notional_row(r)
        sleeve = _sleeve_norm(r.get("sleeve"), sym)
        rows.append(
            {
                "timestamp": pd.Timestamp(ts),
                "date": d,
                "symbol": sym,
                "side": side,
                "notional": notional,
                "dollar_ok": notional > 0,
                "signed": (notional if side == "buy" else -notional) if notional > 0 else 0.0,
                "sleeve": sleeve,
                "regime": str(r.get("regime") or ""),
                "regime_letter": _regime_letter(r.get("regime")),
                "source": str(r.get("source") or ""),
                "order_id": str(r.get("order_id") or ""),
                "event": str(r.get("event") or ""),
            }
        )
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    out = out.sort_values("timestamp")
    if "order_id" in out.columns:
        has_oid = out["order_id"].astype(str).str.len().gt(8)
        dedup = out.loc[has_oid].drop_duplicates(subset=["order_id"], keep="last")
        out = pd.concat([dedup, out.loc[~has_oid]], ignore_index=True).sort_values(
            "timestamp"
        )
    out = out.drop_duplicates(
        subset=["timestamp", "symbol", "side", "notional"], keep="first"
    )
    return out.reset_index(drop=True)


def load_regime_asof() -> pd.DataFrame:
    """Cycle marks with RHYME labels for as-of join."""
    frames = []
    for path in [
        PAPER_BOOK / "paper_journal.csv",
        ROOT / "paper_chase_journal.csv",
        ROOT / "paper_journal.csv",
    ]:
        if not path.is_file():
            continue
        df = _load_csv(path)
        if df.empty or "regime" not in df.columns:
            continue
        ev = df["event"].astype(str).str.lower()
        cyc = df.loc[ev.eq("cycle") & df["regime"].astype(str).str.contains("RHYME", case=False, na=False)].copy()
        if cyc.empty:
            continue
        cyc["timestamp"] = pd.to_datetime(cyc["timestamp"], errors="coerce", utc=True)
        cyc = cyc.dropna(subset=["timestamp"])
        cyc["regime_letter"] = cyc["regime"].map(_regime_letter)
        frames.append(cyc[["timestamp", "regime", "regime_letter"]])
    if not frames:
        return pd.DataFrame(columns=["timestamp", "regime", "regime_letter"])
    out = pd.concat(frames, ignore_index=True).sort_values("timestamp")
    return out.drop_duplicates(subset=["timestamp"], keep="last")


def attach_regime(fills: pd.DataFrame, marks: pd.DataFrame) -> pd.DataFrame:
    if fills.empty:
        return fills
    out = fills.copy()
    missing = out["regime_letter"].eq("") | out["regime_letter"].isna()
    if marks.empty or not missing.any():
        return out
    left = out.loc[missing, ["timestamp"]].copy()
    left["timestamp"] = pd.to_datetime(left["timestamp"], utc=True)
    right = marks.copy()
    right["timestamp"] = pd.to_datetime(right["timestamp"], utc=True)
    left = left.sort_values("timestamp")
    right = right.sort_values("timestamp")
    joined = pd.merge_asof(
        left.reset_index(),
        right,
        on="timestamp",
        direction="backward",
        suffixes=("", "_mark"),
    )
    for idx, row in joined.iterrows():
        orig_idx = row["index"]
        letter = str(row.get("regime_letter") or "")
        if letter:
            out.at[orig_idx, "regime_letter"] = letter
            out.at[orig_idx, "regime"] = row.get("regime") or letter
    return out


def _weekday_sessions(start: date, end: date) -> list[date]:
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _session_gap(a: date, b: date, sessions: list[date]) -> int:
    """Trading-session distance; 0 = same session."""
    if a == b:
        return 0
    try:
        ia = min(range(len(sessions)), key=lambda i: abs((sessions[i] - a).days)) if a not in sessions else sessions.index(a)
        ib = min(range(len(sessions)), key=lambda i: abs((sessions[i] - b).days)) if b not in sessions else sessions.index(b)
        if a in sessions:
            ia = sessions.index(a)
        if b in sessions:
            ib = sessions.index(b)
        return abs(ib - ia)
    except ValueError:
        return abs((b - a).days)


def _cancel_ratio(gross: float, net: float) -> float | None:
    if gross <= 0:
        return None
    return round(1.0 - (abs(net) / gross), 4)


def pair_round_trips(fills: pd.DataFrame, sessions: list[date], max_gap: int = 2) -> pd.DataFrame:
    if fills.empty:
        return pd.DataFrame()
    pairs = []
    for sym, g in fills.groupby("symbol"):
        buys = g.loc[g["side"] == "buy"].sort_values("timestamp")
        sells = g.loc[g["side"] == "sell"].sort_values("timestamp")
        used: set[int] = set()
        for _, b in buys.iterrows():
            for si, s in sells.iterrows():
                if si in used:
                    continue
                if s["timestamp"] < b["timestamp"]:
                    continue
                bn, sn = float(b["notional"]), float(s["notional"])
                if bn < 5 or sn < 5:
                    continue
                overlap = min(bn, sn) / max(bn, sn)
                if overlap < 0.25 and min(bn, sn) < 25:
                    continue
                gap = _session_gap(b["date"], s["date"], sessions)
                if gap > max_gap:
                    continue
                used.add(si)
                pairs.append(
                    {
                        "symbol": sym,
                        "buy_date": b["date"],
                        "sell_date": s["date"],
                        "session_gap": gap,
                        "buy_notional": b["notional"],
                        "sell_notional": s["notional"],
                        "buy_regime": b.get("regime_letter") or "",
                        "sell_regime": s.get("regime_letter") or "",
                        "regime_mismatch": bool(
                            b.get("regime_letter")
                            and s.get("regime_letter")
                            and b.get("regime_letter") != s.get("regime_letter")
                        ),
                        "buy_sleeve": b.get("sleeve"),
                        "sell_sleeve": s.get("sleeve"),
                        "cross_sleeve": bool(b.get("sleeve") and s.get("sleeve") and b.get("sleeve") != s.get("sleeve")),
                    }
                )
                break
    return pd.DataFrame(pairs)


def daily_table(fills: pd.DataFrame, sessions: list[date], pairs: pd.DataFrame) -> pd.DataFrame:
    rows = []
    prev_letter = ""
    for d in sessions:
        day = fills.loc[fills["date"] == d]
        if day.empty:
            continue
        dollar = day.loc[day["dollar_ok"]] if "dollar_ok" in day.columns else day
        buys = day.loc[day["side"] == "buy"]
        sells = day.loc[day["side"] == "sell"]
        gross = float(dollar["notional"].sum()) if not dollar.empty else 0.0
        net = float(dollar["signed"].sum()) if not dollar.empty else 0.0
        n_buy, n_sell = int(len(buys)), int(len(sells))
        count_cancel = _cancel_ratio(float(n_buy + n_sell), float(n_buy - n_sell))
        letters = [x for x in day["regime_letter"].astype(str) if x and x != "nan"]
        regime = max(set(letters), key=letters.count) if letters else ""
        flipped = bool(prev_letter and regime and prev_letter != regime)
        both_sides = (not buys.empty) and (not sells.empty)
        rt_syms = set()
        if not pairs.empty:
            rt_syms = set(
                pairs.loc[(pairs["buy_date"] == d) | (pairs["sell_date"] == d), "symbol"]
            )
        same_day_both = set(buys["symbol"]) & set(sells["symbol"])
        # Cross-sleeve same symbol
        cross = 0
        for sym in same_day_both:
            sg = day.loc[day["symbol"] == sym]
            bsl = set(sg.loc[sg["side"] == "buy", "sleeve"])
            ssl = set(sg.loc[sg["side"] == "sell", "sleeve"])
            if bsl and ssl and bsl != ssl and not (bsl & ssl == bsl == ssl):
                if bsl != ssl:
                    cross += 1
        beta_buy = set(buys["symbol"]) & BETA_CLUSTER
        beta_sell = set(sells["symbol"]) & BETA_CLUSTER
        beta_opp = bool(beta_buy and beta_sell and not (set(buys["symbol"]) & set(sells["symbol"]) & BETA_CLUSTER) and beta_buy != beta_sell)
        # same-name beta both sides already in same_day_both
        notes = []
        if flipped and both_sides:
            notes.append(f"regime_flip {prev_letter}->{regime} with both sides")
        elif flipped:
            notes.append(f"regime_flip {prev_letter}->{regime}")
        if both_sides:
            notes.append("buy+sell same day")
        if beta_opp or (beta_buy and beta_sell and beta_buy != beta_sell):
            notes.append(f"beta cluster opp {sorted(beta_buy)} vs {sorted(beta_sell)}")
        if cross:
            notes.append(f"cross_sleeve_syms={cross}")
        tactical = dollar.loc[~dollar["symbol"].isin(CORE_SYMS)] if not dollar.empty else dollar
        t_gross = float(tactical["notional"].sum()) if not tactical.empty else 0.0
        t_net = float(tactical["signed"].sum()) if not tactical.empty else 0.0
        rows.append(
            {
                "date": d.isoformat(),
                "regime": regime,
                "n_buy": n_buy,
                "n_sell": n_sell,
                "gross": round(gross, 2),
                "net": round(net, 2),
                "cancel_ratio": _cancel_ratio(gross, net),
                "count_cancel": count_cancel,
                "cancel_ex_core": _cancel_ratio(t_gross, t_net),
                "same_sym_rt": len(same_day_both),
                "paired_rt_syms": len(rt_syms),
                "regime_flip": flipped,
                "flip_both_sides": flipped and both_sides,
                "notes": "; ".join(notes),
            }
        )
        if regime:
            prev_letter = regime
    return pd.DataFrame(rows)


def regime_rollups(fills: pd.DataFrame) -> pd.DataFrame:
    if fills.empty:
        return pd.DataFrame()
    rows = []
    gcol = fills["regime_letter"].replace("", "unknown").fillna("unknown")
    for letter, g in fills.groupby(gcol):
        gross = float(g["notional"].sum())
        net = float(g["signed"].sum())
        rows.append(
            {
                "regime": letter,
                "n_buy": int((g["side"] == "buy").sum()),
                "n_sell": int((g["side"] == "sell").sum()),
                "gross": round(gross, 2),
                "net": round(net, 2),
                "cancel_ratio": _cancel_ratio(gross, net),
                "n_days": int(g["date"].nunique()),
            }
        )
    return pd.DataFrame(rows).sort_values("gross", ascending=False)


def _fmt_pct(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{x:.2f}"


def render_md(summary: dict, daily: pd.DataFrame, by_reg: pd.DataFrame, top: pd.DataFrame) -> str:
    lines = [
        "# RHYME / sleeve conflict audit",
        "",
        f"Generated: {summary['generated']}",
        f"Book: `{summary['book']}` | Window: {summary['start']} → {summary['end']} (ET session dates)",
        f"Sources: {', '.join(summary['sources'])}",
        "",
        "## How to read",
        "",
        "- Cancel ratio = `1 - |net| / gross`. Persistently **> 0.60–0.70** = strong self-conflict.",
        "- `count_cancel` uses fill counts (catches sell submits with null notional).",
        "- Dollar cancel ignores those $0 sells; **verdict uses event=fill from 2026-08-12** when enough rows exist.",
        "- Same-symbol RT = buy and sell of that ticker on the same session (daily) or paired within 2 sessions (top-10).",
        "- Freeze: measure only — no `.env` / live / paper retune from this file.",
        "",
        "## Window totals",
        "",
        f"- Fills: **{summary['n_fills']}** (buy {summary['n_buy']} / sell {summary['n_sell']})",
        f"- Gross turnover: **${summary['gross']:,.0f}** | Net signed: **${summary['net']:,.0f}**",
        f"- Cancel ratio (window): **{_fmt_pct(summary['cancel_ratio'])}** | "
        f"median daily: **{_fmt_pct(summary.get('median_daily_cancel'))}** | "
        f"ex-core: **{_fmt_pct(summary['cancel_ex_core'])}**",
        f"- Paired round-trips (≤2 sessions): **{summary['n_pairs']}** | regime-mismatch pairs: **{summary['n_regime_mismatch_pairs']}**",
        f"- Days with regime flip **and** both buy+sell: **{summary['n_flip_both']}** / {summary['n_days_with_fills']}",
        f"- Verdict: **{summary['verdict']}**",
        "",
        "## Daily",
        "",
        "| date | regime | #buy | #sell | gross $ | net $ | cancel $ | count_cancel | ex-core | same_sym_rt | notes |",
        "|------|--------|-----:|------:|--------:|------:|---------:|-------------:|--------:|------------:|-------|",
    ]
    for _, r in daily.iterrows():
        lines.append(
            f"| {r['date']} | {r['regime'] or '-'} | {r['n_buy']} | {r['n_sell']} | "
            f"{r['gross']:,.0f} | {r['net']:,.0f} | {_fmt_pct(r['cancel_ratio'])} | "
            f"{_fmt_pct(r.get('count_cancel'))} | {_fmt_pct(r['cancel_ex_core'])} | "
            f"{r['same_sym_rt']} | {r['notes'] or ''} |"
        )
    lines += ["", "## By RHYME letter (fill as-of)", ""]
    if by_reg.empty:
        lines.append("_No regime labels on fills._")
    else:
        lines += [
            "| regime | #buy | #sell | gross $ | net $ | cancel | days |",
            "|--------|-----:|------:|--------:|------:|-------:|-----:|",
        ]
        for _, r in by_reg.iterrows():
            lines.append(
                f"| {r['regime']} | {r['n_buy']} | {r['n_sell']} | {r['gross']:,.0f} | "
                f"{r['net']:,.0f} | {_fmt_pct(r['cancel_ratio'])} | {r['n_days']} |"
            )
    lines += ["", "## Top 10 symbols by round-trip pairs (≤2 sessions)", ""]
    if top.empty:
        lines.append("_None._")
    else:
        lines += [
            "| symbol | pairs | regime-mismatch | cross-sleeve | buy $ | sell $ |",
            "|--------|------:|----------------:|-------------:|------:|-------:|",
        ]
        for _, r in top.iterrows():
            lines.append(
                f"| {r['symbol']} | {int(r['pairs'])} | {int(r['regime_mismatch'])} | "
                f"{int(r['cross_sleeve'])} | {r['buy_notional']:,.0f} | {r['sell_notional']:,.0f} |"
            )
    lines += ["", "## Notes", ""]
    for n in summary.get("load_notes", []):
        lines.append(f"- {n}")
    lines.append("")
    return "\n".join(lines)


def _verdict(
    cancel: float | None,
    cancel_ex: float | None,
    mismatch_frac: float,
    flip_both: int,
    days: int,
    median_daily: float | None = None,
) -> str:
    # Persistently high daily cancel is the user's threshold, not a one-way de-risk day.
    c = median_daily if median_daily is not None else (
        cancel_ex if cancel_ex is not None else cancel
    )
    if c is None:
        return "insufficient fills"
    if c >= 0.70 and mismatch_frac >= 0.25:
        return "RHYME looks net subtractive on this tape (high cancel + regime-mismatch round-trips)"
    if c >= 0.70:
        return "strong internal disagreement (churn); regime-mismatch share is modest - more sleeve/stop recycling than letter wars"
    if c >= 0.60:
        return "elevated self-conflict - keep RHYME but do not add more regime overlays until freeze ends"
    if flip_both >= max(2, days // 4):
        return "regime flips often coincide with two-way flow - candidate for tighter hysteresis, not a new sleeve"
    return "cancellation not persistently in the 0.6-0.7 danger zone; RHYME not clearly subtractive on this window"


def run(start: date, end: date, book: str) -> dict[str, Any]:
    jfills, notes = load_journal_fills(book)
    actions = load_jsonl_actions()
    a_fills = _actions_to_fills(actions, book)
    parts = []
    if not jfills.empty:
        ev = jfills["event"].astype(str).str.lower()
        true_fills = jfills.loc[ev == "fill"]
        if not true_fills.empty:
            parts.append(true_fills)
            notes.append(f"using {len(true_fills)} journal event=fill rows")
        else:
            notes.append("journal has no event=fill in preferred set; using jsonl")
    if not parts and not a_fills.empty:
        parts.append(a_fills)
        notes.append(f"jsonl action rows after fill-over-submit: {len(a_fills)}")
    elif not a_fills.empty:
        notes.append(f"jsonl present ({len(a_fills)}) but ignored; journal event=fill is SoT")
    if not parts:
        raw = pd.DataFrame()
    else:
        raw = pd.concat(parts, ignore_index=True)
    fills = _normalize_fills(raw)
    marks = load_regime_asof()
    fills = attach_regime(fills, marks)
    if not fills.empty:
        fills = fills.loc[(fills["date"] >= start) & (fills["date"] <= end)].copy()
    sessions = _weekday_sessions(start, end)
    fill_tape = fills
    if not fills.empty:
        src = fills["source"].astype(str)
        ev = fills["event"].astype(str).str.lower()
        ft = fills.loc[src.str.contains("fill", case=False) | ev.eq("fill")]
        if len(ft) >= 20:
            fill_tape = ft
            notes.append(
                f"verdict tape: {len(ft)} event=fill rows (submit $ is biased; many sells have null notional)"
            )
        else:
            notes.append("few event=fill rows; dollar cancel uses rows with known notional")
    pairs = pair_round_trips(fill_tape, sessions, max_gap=2)
    daily_all = daily_table(fills, sessions, pairs)
    daily = daily_table(fill_tape, sessions, pairs)
    by_reg = regime_rollups(fill_tape)
    if pairs.empty:
        top = pd.DataFrame()
        n_mismatch = 0
    else:
        top = (
            pairs.groupby("symbol")
            .agg(
                pairs=("symbol", "size"),
                regime_mismatch=("regime_mismatch", "sum"),
                cross_sleeve=("cross_sleeve", "sum"),
                buy_notional=("buy_notional", "sum"),
                sell_notional=("sell_notional", "sum"),
            )
            .sort_values("pairs", ascending=False)
            .head(10)
            .reset_index()
        )
        n_mismatch = int(pairs["regime_mismatch"].sum())
    n_pairs = 0 if pairs.empty else len(pairs)
    dollar = fill_tape.loc[fill_tape["dollar_ok"]] if not fill_tape.empty and "dollar_ok" in fill_tape.columns else fill_tape
    gross = float(dollar["notional"].sum()) if not dollar.empty else 0.0
    net = float(dollar["signed"].sum()) if not dollar.empty else 0.0
    tactical = dollar.loc[~dollar["symbol"].isin(CORE_SYMS)] if not dollar.empty else dollar
    t_gross = float(tactical["notional"].sum()) if not tactical.empty else 0.0
    t_net = float(tactical["signed"].sum()) if not tactical.empty else 0.0
    cancel = _cancel_ratio(gross, net)
    cancel_ex = _cancel_ratio(t_gross, t_net)
    n_flip = int(daily["flip_both_sides"].sum()) if not daily.empty else 0
    mismatch_frac = (n_mismatch / n_pairs) if n_pairs else 0.0
    med = None
    if not daily.empty and "cancel_ratio" in daily.columns:
        med_s = pd.to_numeric(daily["cancel_ratio"], errors="coerce").median()
        med = None if pd.isna(med_s) else round(float(med_s), 4)
    sources = sorted({str(s) for s in fills["source"].unique()}) if not fills.empty else []
    two_way = (
        int((daily_all["n_buy"].gt(0) & daily_all["n_sell"].gt(0)).sum())
        if not daily_all.empty
        else 0
    )
    notes.append(f"submit+fill days with both sides: {two_way}/{len(daily_all)}")
    summary = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "book": book,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "sources": sources,
        "n_fills": int(len(fill_tape)),
        "n_buy": int((fill_tape["side"] == "buy").sum()) if not fill_tape.empty else 0,
        "n_sell": int((fill_tape["side"] == "sell").sum()) if not fill_tape.empty else 0,
        "gross": round(gross, 2),
        "net": round(net, 2),
        "cancel_ratio": cancel,
        "cancel_ex_core": cancel_ex,
        "median_daily_cancel": med,
        "n_pairs": n_pairs,
        "n_regime_mismatch_pairs": n_mismatch,
        "mismatch_frac": round(mismatch_frac, 4),
        "n_flip_both": n_flip,
        "n_days_with_fills": int(len(daily)),
        "verdict": _verdict(
            cancel, cancel_ex, mismatch_frac, n_flip, int(len(daily)), med
        ),
        "load_notes": notes,
    }
    return {
        "summary": summary,
        "daily": daily,
        "by_regime": by_reg,
        "top": top,
        "pairs": pairs,
        "fills": fills,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="RHYME buy/sell conflict audit")
    p.add_argument("--since", default=FREEZE_START.isoformat(), help="YYYY-MM-DD (default freeze start)")
    p.add_argument("--until", default="", help="YYYY-MM-DD (default today ET)")
    p.add_argument("--days", type=int, default=0, help="If set, last N weekdays ending until")
    p.add_argument("--book", choices=["paper", "paper_v2", "live"], default="paper_v2")
    args = p.parse_args()
    until = date.fromisoformat(args.until) if args.until else _to_et_date(pd.Timestamp.now(tz="UTC"))
    if until is None:
        until = date.today()
    if args.days and args.days > 0:
        sessions = _weekday_sessions(until - timedelta(days=args.days * 2), until)
        start = sessions[-args.days] if len(sessions) >= args.days else sessions[0]
    else:
        start = date.fromisoformat(args.since)
    stem = f"rhyme_conflict_audit_{args.book}_last"
    out_md = Path(__file__).with_name(f"{stem}.md")
    out_json = Path(__file__).with_name(f"{stem}.json")
    result = run(start, until, args.book)
    md = render_md(result["summary"], result["daily"], result["by_regime"], result["top"])
    out_md.write_text(md, encoding="utf-8")
    payload = {
        "summary": result["summary"],
        "daily": result["daily"].to_dict(orient="records") if not result["daily"].empty else [],
        "by_regime": result["by_regime"].to_dict(orient="records") if not result["by_regime"].empty else [],
        "top_symbols": result["top"].to_dict(orient="records") if not result["top"].empty else [],
    }
    out_json.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    if args.book == "paper":
        OUT_MD.write_text(md, encoding="utf-8")
        OUT_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {out_md}")
    print(f"Wrote {out_json}")
    s = result["summary"]
    print(
        f"{s['book']} {s['start']}..{s['end']} fills={s['n_fills']} "
        f"cancel={s['cancel_ratio']} ex_core={s['cancel_ex_core']} "
        f"pairs={s['n_pairs']} mismatch={s['n_regime_mismatch_pairs']}"
    )
    print(s["verdict"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
