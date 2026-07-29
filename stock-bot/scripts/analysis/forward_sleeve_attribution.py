"""Forward-paper sleeve attribution vs STRICT baseline expectations.

Measure-only: reads portal paper journal (+ heartbeat MTM). Does not change
.env, live Profile A, or paper defaults.

Usage (from stock-bot/):
  python scripts/analysis/forward_sleeve_attribution.py
  python scripts/analysis/forward_sleeve_attribution.py --days 14
  python scripts/analysis/forward_sleeve_attribution.py --open

Writes:
  scripts/analysis/forward_sleeve_attr_last.md
  scripts/analysis/forward_sleeve_attr_last.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

OUT_MD = Path(__file__).with_name("forward_sleeve_attr_last.md")
OUT_JSON = Path(__file__).with_name("forward_sleeve_attr_last.json")
PAPER_BOOK = ROOT / "data" / "portal" / "users" / "dawimberly" / "books" / "alpaca_paper"

# STRICT paper expectations (spy_off promoted; exit_h45 rejected). Annualized-ish
# reference from exit_spy_ab_365_last.md — not a live target.
STRICT_BASELINE_365 = {
    "label": "STRICT paper spy_off (365d)",
    "return_pct": 27.67,
    "sharpe": 1.47,
    "max_dd_pct": -7.05,
    "source": "scripts/analysis/exit_spy_ab_365_last.md",
}
STRICT_BASELINE_90 = {
    "label": "STRICT paper windows (~90d)",
    "return_pct": 15.6,
    "sharpe": 2.44,
    "source": "scripts/analysis/eval_strict_windows_last.md",
}

EXIT_EVENTS = frozenset({"exit", "sell", "close"})
BUY_EVENTS = frozenset({"buy", "entry", "open", "fill", "signal"})


@dataclass
class SleeveRow:
    name: str
    fills: int = 0
    exits: int = 0
    realized_pnl: float = 0.0
    realized_wins: int = 0
    notional_bought: float = 0.0
    unrealized_pnl: float = 0.0
    open_positions: int = 0

    @property
    def win_rate(self) -> float | None:
        if self.exits <= 0:
            return None
        return self.realized_wins / self.exits


@dataclass
class Report:
    generated_at: str
    days: int
    window_start: str
    journal_path: str
    freeze_note: str
    equity_start: float | None = None
    equity_end: float | None = None
    period_return_pct: float | None = None
    closed_trades: int = 0
    sleeves: dict[str, dict[str, Any]] = field(default_factory=dict)
    vs_strict: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    ok: bool = True


def _journal_candidates() -> list[Path]:
    return [
        PAPER_BOOK / "paper_journal.csv",
        ROOT / "paper_chase_journal.csv",
        ROOT / "paper_journal.csv",
    ]


def _load_journal() -> tuple[pd.DataFrame, str]:
    from modules.paper_journal import normalize_journal_df

    for path in _journal_candidates():
        if not path.is_file():
            continue
        try:
            raw = pd.read_csv(path, low_memory=False)
        except Exception:
            continue
        df = normalize_journal_df(raw)
        if df.empty:
            continue
        try:
            from modules.wisdom_evaluator import filter_journal

            seg, _ = filter_journal(df, book_type="paper")
            if seg is not None and not seg.empty:
                return seg, str(path)
        except Exception:
            pass
        return df, str(path)
    return pd.DataFrame(), ""


def _load_heartbeat() -> dict[str, Any] | None:
    for path in (
        PAPER_BOOK / "heartbeat.json",
        ROOT / "heartbeat.json",
        ROOT / "data" / "heartbeat.json",
    ):
        if not path.is_file():
            continue
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
    return None


def _extract_pnl(text: str) -> float | None:
    import re

    if not text:
        return None
    m = re.search(r"(?:pnl|p&l|profit)[:=\s]*([+-]?\d+(?:\.\d+)?)", text, re.I)
    if m:
        return float(m.group(1))
    m = re.search(r"([+-]\d+(?:\.\d+)?)\s*(?:usd|\$)?\s*$", text.strip(), re.I)
    if m:
        return float(m.group(1))
    return None


def _build_sleeves(df: pd.DataFrame, cutoff: datetime, hb: dict | None) -> dict[str, SleeveRow]:
    sleeves: dict[str, SleeveRow] = {}
    if df.empty:
        return sleeves
    window = df[df["timestamp"] >= cutoff].copy()
    if window.empty:
        return sleeves

    for _, row in window.iterrows():
        sleeve = str(row.get("sleeve") or "").strip().lower() or "unknown"
        if sleeve in ("", "nan", "none"):
            sleeve = "unknown"
        st = sleeves.setdefault(sleeve, SleeveRow(name=sleeve))
        ev = str(row.get("event") or "").strip().lower()
        notional = float(row.get("notional") or 0.0) if pd.notna(row.get("notional")) else 0.0
        if ev in BUY_EVENTS:
            st.fills += 1
            st.notional_bought += abs(notional)
        if ev in EXIT_EVENTS:
            st.exits += 1
            pnl = _extract_pnl(str(row.get("notes") or row.get("exit_reason") or ""))
            if pnl is not None:
                st.realized_pnl += pnl
                if pnl > 0:
                    st.realized_wins += 1

    if hb:
        for name, block in (hb.get("sleeve_pnl") or {}).items():
            if not isinstance(block, dict):
                continue
            key = str(name).lower()
            st = sleeves.setdefault(key, SleeveRow(name=key))
            st.unrealized_pnl = float(block.get("unrealized_pnl") or 0.0)
            st.open_positions = int(block.get("positions") or 0)
    return sleeves


def _period_equity(df: pd.DataFrame, cutoff: datetime) -> tuple[float | None, float | None, float | None]:
    if df.empty or "equity" not in df.columns:
        return None, None, None
    eq = df.dropna(subset=["timestamp", "equity"]).sort_values("timestamp")
    if eq.empty:
        return None, None, None
    recent = eq[eq["timestamp"] >= cutoff]
    if recent.empty:
        return None, None, None
    start = float(recent["equity"].iloc[0])
    end = float(recent["equity"].iloc[-1])
    if start <= 0:
        return start, end, None
    return start, end, (end / start - 1.0) * 100.0


def _vs_strict(days: int, period_return_pct: float | None) -> dict[str, Any]:
    if days <= 120:
        ref = STRICT_BASELINE_90
        expected = float(ref["return_pct"]) * (days / 90.0)
        scale_note = f"90d STRICT window scaled by {days}/90 (envelope only)"
    else:
        ref = STRICT_BASELINE_365
        expected = float(STRICT_BASELINE_365["return_pct"]) * (days / 365.0)
        scale_note = f"365d STRICT spy_off scaled by {days}/365 (envelope only)"
    delta = None if period_return_pct is None else period_return_pct - expected
    return {
        "reference": ref,
        "expected_return_pct_envelope": round(expected, 2),
        "period_return_pct": period_return_pct,
        "delta_pp": None if delta is None else round(delta, 2),
        "scale_note": scale_note,
        "caveat": (
            "Short live-paper samples are noisy; do not retune from this delta. "
            "Dashboard Sharpe over days/weeks is not comparable to STRICT backtest Sharpe."
        ),
    }


def build_report(days: int) -> Report:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    df, jpath = _load_journal()
    hb = _load_heartbeat()
    notes: list[str] = [
        "Forward-paper freeze: measure only - no .env / live / sleeve changes from this report.",
        "SPY fills on paper should be ~0 (satellite OFF). Non-zero SPY -> check restart after lock.",
    ]
    if not jpath:
        notes.append("No paper journal found - attribution empty.")
        return Report(
            generated_at=now.strftime("%Y-%m-%d %H:%M UTC"),
            days=days,
            window_start=cutoff.strftime("%Y-%m-%d"),
            journal_path="",
            freeze_note="See FORWARD_PAPER_FREEZE.md",
            notes=notes,
            ok=False,
        )

    if "timestamp" in df.columns:
        df = df.copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)

    sleeves = _build_sleeves(df, cutoff, hb)
    eq0, eq1, ret = _period_equity(df, cutoff)
    closed = sum(s.exits for s in sleeves.values())
    spy = sleeves.get("spy")
    if spy and spy.fills > 0:
        notes.append(
            f"WARNING: {spy.fills} SPY sleeve fills in window - paper SPY should be OFF; restart paper bot."
        )
    if closed < 3:
        notes.append("Sparse exits - treat sleeve ranks as provisional.")

    return Report(
        generated_at=now.strftime("%Y-%m-%d %H:%M UTC"),
        days=days,
        window_start=cutoff.strftime("%Y-%m-%d"),
        journal_path=jpath,
        freeze_note="See FORWARD_PAPER_FREEZE.md (2-4 weeks, no new features)",
        equity_start=eq0,
        equity_end=eq1,
        period_return_pct=None if ret is None else round(ret, 2),
        closed_trades=closed,
        sleeves={
            k: {
                **asdict(v),
                "win_rate": None if v.win_rate is None else round(v.win_rate, 3),
            }
            for k, v in sorted(sleeves.items(), key=lambda kv: -(kv[1].realized_pnl + kv[1].unrealized_pnl))
        },
        vs_strict=_vs_strict(days, None if ret is None else round(ret, 2)),
        notes=notes,
        ok=True,
    )


def _write_md(rep: Report) -> str:
    lines = [
        f"# Forward paper sleeve attribution ({rep.days}d)",
        "",
        f"Generated: {rep.generated_at}",
        f"Window start: {rep.window_start}",
        f"Journal: `{rep.journal_path or 'n/a'}`",
        f"Freeze: {rep.freeze_note}",
        "",
        f"Period equity: {_fmt_money(rep.equity_start)} -> {_fmt_money(rep.equity_end)} "
        f"({_fmt_pct(rep.period_return_pct)}) | closed exits: {rep.closed_trades}",
        "",
        "## Sleeve table",
        "",
        "| Sleeve | Fills | Exits | Realized PnL | Win rate | Unrealized | Open |",
        "|--------|------:|------:|-------------:|---------:|-----------:|-----:|",
    ]
    for name, s in rep.sleeves.items():
        wr = s.get("win_rate")
        wr_s = "n/a" if wr is None else f"{wr:.0%}"
        lines.append(
            f"| {name} | {s.get('fills', 0)} | {s.get('exits', 0)} | "
            f"{_fmt_money(s.get('realized_pnl'))} | {wr_s} | "
            f"{_fmt_money(s.get('unrealized_pnl'))} | {s.get('open_positions', 0)} |"
        )
    vs = rep.vs_strict or {}
    ref = vs.get("reference") or {}
    lines.extend(
        [
            "",
            "## vs STRICT envelope (honesty check)",
            "",
            f"- Reference: {ref.get('label', 'n/a')} ({ref.get('source', '')})",
            f"- Envelope expected return: {_fmt_pct(vs.get('expected_return_pct_envelope'))}",
            f"- Observed period return: {_fmt_pct(vs.get('period_return_pct'))}",
            f"- Delta: {_fmt_pp(vs.get('delta_pp'))}",
            f"- Note: {vs.get('scale_note', '')}",
            f"- Caveat: {vs.get('caveat', '')}",
            "",
            "## Notes",
            "",
        ]
    )
    for n in rep.notes:
        lines.append(f"- {n}")
    lines.append("")
    return "\n".join(lines)


def _fmt_pct(v: float | None) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    return f"{v:+.2f}%"


def _fmt_pp(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.2f}pp"


def _fmt_money(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"${v:,.2f}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Forward-paper sleeve attribution (measure only)")
    ap.add_argument("--days", type=int, default=int(os.getenv("FORWARD_ATTR_DAYS", "14")))
    ap.add_argument("--open", action="store_true", help="Open the markdown report")
    args = ap.parse_args()
    days = max(1, int(args.days))

    rep = build_report(days)
    md = _write_md(rep)
    OUT_MD.write_text(md, encoding="utf-8")
    OUT_JSON.write_text(json.dumps(asdict(rep), indent=2, default=str), encoding="utf-8")
    print(md.encode("ascii", errors="replace").decode("ascii"))
    print(f"\nWrote {OUT_MD.name} / {OUT_JSON.name}")
    if args.open and OUT_MD.is_file():
        try:
            os.startfile(str(OUT_MD))  # type: ignore[attr-defined]
        except Exception:
            subprocess.run(["cmd", "/c", "start", "", str(OUT_MD)], check=False)
    return 0 if rep.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
