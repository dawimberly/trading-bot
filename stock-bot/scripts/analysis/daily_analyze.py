"""Daily ANALYZE note (paper+live snapshot). Measure only.

Writes:
  docs/DAILY_ANALYZE_YYYY-MM-DD.md
  docs/DAILY_ANALYZE_LAST.md

Usage (from stock-bot/):
  python scripts/analysis/daily_analyze.py
  python scripts/analysis/daily_analyze.py --open
  python scripts/analysis/daily_analyze.py --day 2026-08-26

No Telegram. No .env / strategy / orders.

Task Scheduler (do not register unless owner says):
  Weekdays 15:15 America/Chicago → scripts\\analysis\\open_daily_analyze.bat
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

from trade_reconciliation import read_journal_csv  # noqa: E402

CT = ZoneInfo("America/Chicago")
DOCS = ROOT / "docs"
PAPER_JOURNAL = (
    ROOT / "data" / "portal" / "users" / "dawimberly" / "books" / "alpaca_paper" / "paper_journal.csv"
)
LIVE_JOURNAL = (
    ROOT / "data" / "portal" / "users" / "dawimberly" / "books" / "alpaca_live" / "paper_journal.csv"
)
PAPER_HB = PAPER_JOURNAL.parent / "bot_heartbeat.json"
LIVE_HB = LIVE_JOURNAL.parent / "bot_heartbeat.json"
ACTIONS = ROOT / "logs" / "bot_actions.jsonl"
NO_STRATEGY = "no strategy change from this file"


def _num(val: Any, default: float = 0.0) -> float:
    try:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return default
        x = float(val)
        if x != x:
            return default
        return x
    except (TypeError, ValueError):
        return default


def _s(val: Any) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    t = str(val).strip()
    if t.lower() in ("nan", "none", "null"):
        return ""
    return t


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _ts_ct(val: Any) -> datetime | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, datetime):
        ts = val
    else:
        try:
            ts = pd.to_datetime(val, errors="coerce")
            if pd.isna(ts):
                return None
            ts = ts.to_pydatetime()
        except Exception:
            return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=CT)
    return ts.astimezone(CT)


def _day_mask(series: pd.Series, day: date) -> pd.Series:
    out = []
    for v in series:
        ts = _ts_ct(v)
        out.append(ts is not None and ts.date() == day)
    return pd.Series(out, index=series.index)


def _load_fills(path: Path, day: date) -> pd.DataFrame:
    if not path.is_file():
        return pd.DataFrame()
    df, _warn = read_journal_csv(path)
    if df is None or df.empty:
        return pd.DataFrame()
    ev = df["event"].astype(str).str.lower() if "event" in df.columns else pd.Series("", index=df.index)
    df = df.loc[ev == "fill"].copy()
    if df.empty or "timestamp" not in df.columns:
        return df
    return df.loc[_day_mask(df["timestamp"], day)].copy()


def _hb_block(hb: dict[str, Any], label: str) -> list[str]:
    caps = hb.get("sleeve_caps") if isinstance(hb.get("sleeve_caps"), dict) else {}
    exp = hb.get("sleeve_exposure") if isinstance(hb.get("sleeve_exposure"), dict) else {}
    skip = hb.get("entry_skip_daily") if isinstance(hb.get("entry_skip_daily"), dict) else {}
    vti_cap = _num(caps.get("vti_core"), 0.0)
    flag = " **FLAG expected 0**" if vti_cap > 1e-6 else ""
    eq = hb.get("equity")
    cash = hb.get("cash")
    cash_pct = hb.get("cash_pct")
    nyse_mv = exp.get("nyse_value")
    nyse_cap = caps.get("nyse")
    lines = [
        f"### {label}",
        f"- Equity: {eq} | cash: {cash} | cash %: {cash_pct}",
        f"- NYSE MV: {nyse_mv} | NYSE cap: {nyse_cap}",
        f"- vti_core cap: {vti_cap}{flag}",
        f"- Regime: {hb.get('regime') or 'n/a'} | session open: {hb.get('equity_session_open')}",
        f"- Last skip: `{hb.get('entry_skip_reason') or 'n/a'}`",
    ]
    tokens = skip.get("by_token") if isinstance(skip.get("by_token"), dict) else {}
    if tokens:
        top = ", ".join(f"{k}={v}" for k, v in list(tokens.items())[:8])
        lines.append(f"- Skip tokens: {top}")
    else:
        lines.append("- Skip tokens: n/a")
    return lines


def _classify_vti(row: pd.Series) -> str:
    pk = _s(row.get("pair_key")).lower()
    notes = _s(row.get("notes")).lower()
    reason = _s(row.get("exit_reason")).lower()
    blob = f"{pk} {notes} {reason}"
    if "ma50" in blob:
        return "MA50"
    if _s(row.get("pair_key")) == "" and _s(row.get("notes")) == "" and _s(row.get("exit_reason")) == "":
        return "empty-reason core-style"
    if "manual" in blob:
        return "manual"
    return "other"


def _fill_section(fills: pd.DataFrame, title: str) -> list[str]:
    lines = [f"### {title}"]
    if fills is None or fills.empty:
        lines.append("- Fills: 0")
        lines.append("- Realized sum: 0")
        return lines
    side = fills["side"].astype(str).str.lower() if "side" in fills.columns else pd.Series("", index=fills.index)
    n_buy = int((side == "buy").sum())
    n_sell = int((side == "sell").sum())
    pnl = fills["realized_pnl"].map(_num) if "realized_pnl" in fills.columns else pd.Series(0.0, index=fills.index)
    lines.append(f"- Fills: {len(fills)} (buys {n_buy} / sells {n_sell})")
    lines.append(f"- Realized sum: {round(float(pnl.sum()), 2)}")
    return lines


def _vti_section(fills: pd.DataFrame) -> list[str]:
    lines = ["## VTI rows"]
    if fills is None or fills.empty:
        lines.append("- none")
        return lines
    sym = fills["symbol"].astype(str).str.upper() if "symbol" in fills.columns else pd.Series("", index=fills.index)
    vti = fills.loc[sym == "VTI"].copy()
    if vti.empty:
        lines.append("- none")
        return lines
    flagged = False
    for _, r in vti.iterrows():
        kind = _classify_vti(r)
        ts = _s(r.get("timestamp"))[:19]
        side = _s(r.get("side"))
        qty = _s(r.get("qty"))
        notional = _s(r.get("notional"))
        flag = ""
        if kind == "empty-reason core-style" and side.lower() == "buy":
            flag = " **FLAG empty-reason buy**"
            flagged = True
        lines.append(
            f"- {ts} {side} qty={qty} notional={notional} kind={kind}{flag}"
        )
    if not flagged:
        lines.append("- No empty-reason VTI buys today.")
    return lines


def _atr_section(fills: pd.DataFrame) -> list[str]:
    lines = ["## ATR stops"]
    if fills is None or fills.empty:
        lines.append("- none")
        return lines
    blob_cols = []
    for c in ("exit_reason", "pair_key", "notes"):
        if c in fills.columns:
            blob_cols.append(fills[c].astype(str))
    if not blob_cols:
        lines.append("- none")
        return lines
    blob = blob_cols[0]
    for extra in blob_cols[1:]:
        blob = blob + " " + extra
    atr = fills.loc[blob.str.lower().str.contains("smart_atr_stop|atr_stop", regex=True)].copy()
    if atr.empty:
        lines.append("- none")
        return lines
    side = fills["side"].astype(str).str.lower() if "side" in fills.columns else pd.Series("", index=fills.index)
    buys = fills.loc[side == "buy"]
    buy_syms = set(buys["symbol"].astype(str).str.upper()) if "symbol" in buys.columns else set()
    for _, r in atr.iterrows():
        sym = _s(r.get("symbol")).upper()
        pnl = _num(r.get("realized_pnl"))
        ts_stop = _ts_ct(r.get("timestamp"))
        rebuy = "N"
        if ts_stop is not None and "timestamp" in buys.columns and "symbol" in buys.columns:
            later = buys.loc[buys["symbol"].astype(str).str.upper() == sym]
            for _, b in later.iterrows():
                tb = _ts_ct(b.get("timestamp"))
                if tb is not None and tb > ts_stop:
                    rebuy = "Y"
                    break
        elif sym in buy_syms:
            rebuy = "?"
        lines.append(
            f"- {sym} pnl={round(pnl, 2)} rebuy={rebuy} ts={_s(r.get('timestamp'))[:19]}"
        )
    return lines


def _rhyme_section(day: date) -> list[str]:
    lines = ["## Cancel / pairs (RHYME helper)"]
    try:
        from rhyme_conflict_audit import run as rhyme_run

        pack = rhyme_run(day, day, "paper")
        summ = pack.get("summary") or {}
        n_pairs = summ.get("n_pairs")
        cancel = summ.get("cancel_ratio")
        lines.append(
            f"- paper {day.isoformat()}: pairs={n_pairs} cancel_ratio={cancel} "
            f"buys={summ.get('n_buy')} sells={summ.get('n_sell')}"
        )
    except Exception as exc:
        lines.append(f"- skipped ({type(exc).__name__}: {exc})")
    return lines


def _jsonl_vti(day: date) -> list[str]:
    lines = ["## jsonl VTI (optional)"]
    if not ACTIONS.is_file():
        lines.append("- no logs/bot_actions.jsonl")
        return lines
    hits = 0
    empty_buys = 0
    try:
        with ACTIONS.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"VTI"' not in line and '"vti"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                ts = _ts_ct(o.get("ts") or o.get("timestamp"))
                if ts is None or ts.date() != day:
                    continue
                if str(o.get("symbol") or "").upper() != "VTI":
                    continue
                ev = str(o.get("event") or "")
                if ev not in ("order_submitted", "fill"):
                    continue
                hits += 1
                reason = str(o.get("reason") or "").strip()
                if ev == "order_submitted" and str(o.get("side") or "").lower() == "buy" and not reason:
                    empty_buys += 1
    except Exception as exc:
        lines.append(f"- read failed: {exc}")
        return lines
    lines.append(f"- VTI order/fill rows: {hits}; empty-reason paper/live buys: {empty_buys}")
    if empty_buys:
        lines.append("- **FLAG** jsonl empty-reason VTI buy(s)")
    return lines


def find_pycharm_exe() -> Path | None:
    for key in ("PYCHARM_EXE", "WEEKLY_REVIEW_PYCHARM", "FREEZE_OPS_PYCHARM"):
        raw = (os.getenv(key) or "").strip().strip('"')
        if raw:
            p = Path(raw)
            if p.is_file():
                return p
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    for rel in (
        r"Programs\PyCharm Community Edition\bin\pycharm64.exe",
        r"Programs\PyCharm\bin\pycharm64.exe",
    ):
        p = local / rel
        if p.is_file():
            return p
    jet = Path(r"C:\Program Files\JetBrains")
    if jet.is_dir():
        found = sorted(jet.glob("PyCharm */bin/pycharm64.exe"), reverse=True)
        if found:
            return found[0]
    return None


def open_last_md(path: Path) -> None:
    if not path.is_file():
        print(f"[daily_analyze] missing {path}", flush=True)
        return
    exe = find_pycharm_exe()
    try:
        if exe is not None:
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
            subprocess.Popen(
                [str(exe), str(path.resolve())],
                cwd=str(path.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                close_fds=True,
                creationflags=flags,
            )
            print(f"[daily_analyze] Opened PyCharm: {exe}", flush=True)
            return
        if sys.platform == "win32":
            os.startfile(str(path.resolve()))  # type: ignore[attr-defined]
            print("[daily_analyze] Opened via startfile (no pycharm64.exe)", flush=True)
    except Exception as exc:
        print(f"[daily_analyze] IDE open skipped: {exc}", flush=True)


def build_markdown(day: date) -> str:
    paper_hb = _load_json(PAPER_HB)
    live_hb = _load_json(LIVE_HB)
    paper_fills = _load_fills(PAPER_JOURNAL, day)
    live_fills = _load_fills(LIVE_JOURNAL, day)
    now = datetime.now(CT).strftime("%Y-%m-%d %H:%M %Z")
    lines = [
        f"# Daily ANALYZE — {day.isoformat()}",
        "",
        f"Generated: {now}",
        f"SoT journal: `{PAPER_JOURNAL}`",
        "",
        "## Snapshot",
        "",
    ]
    lines.extend(_hb_block(paper_hb, "Paper"))
    lines.append("")
    lines.extend(_hb_block(live_hb, "Live"))
    lines.extend(["", "## Fills today", ""])
    lines.extend(_fill_section(paper_fills, "Paper"))
    lines.append("")
    lines.extend(_fill_section(live_fills, "Live"))
    lines.append("")
    lines.extend(_vti_section(pd.concat([paper_fills, live_fills], ignore_index=True) if not paper_fills.empty or not live_fills.empty else paper_fills))
    lines.append("")
    lines.extend(_atr_section(paper_fills))
    lines.append("")
    lines.extend(_rhyme_section(day))
    lines.append("")
    lines.extend(_jsonl_vti(day))
    lines.extend(
        [
            "",
            "## Intent check",
            "",
            "- Stack: paper+live NYSE 100%, VTI core OFF.",
            f"- **{NO_STRATEGY}**",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Daily ANALYZE note (no Telegram, no strategy).")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--day", default="", help="YYYY-MM-DD (default today America/Chicago)")
    args = ap.parse_args(argv)

    if args.day.strip():
        day = date.fromisoformat(args.day.strip())
    else:
        day = datetime.now(CT).date()

    DOCS.mkdir(parents=True, exist_ok=True)
    md = build_markdown(day)
    dated = DOCS / f"DAILY_ANALYZE_{day.isoformat()}.md"
    last = DOCS / "DAILY_ANALYZE_LAST.md"
    dated.write_text(md, encoding="utf-8")
    last.write_text(md, encoding="utf-8")
    print(md.encode("ascii", errors="replace").decode("ascii"))
    print(f"\nWrote {dated}")
    print(f"Wrote {last}")
    if args.open:
        open_last_md(last)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
