"""Read-only Core vs NYSE performance gates (freeze-safe).

Measure-only: Alpaca paper snapshot + data/gate_ab_log.csv (preferred) or
overnight_pack stubs. Does NOT change .env, trim VTI, enable
DYNAMIC_SLEEVE_CAPS, or place orders.

Usage (from stock-bot/):
  python scripts/analysis/core_nyse_performance_gates.py
  python scripts/analysis/core_nyse_performance_gates.py --days 15

Daily logger: python scripts/ops/log_gate_ab.py
See FORWARD_PAPER_FREEZE.md § Core vs NYSE performance gates (post-freeze).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values  # noqa: E402

PAPER_BOOK = ROOT / "data" / "portal" / "users" / "dawimberly" / "books" / "alpaca_paper"
GATE_LOG = ROOT / "data" / "gate_ab_log.csv"
CORE = {"VTI", "VOO", "ITOT"}
SPY = {"SPY"}
METAL = {"GLD", "SLV", "CPER", "IAU", "GDX"}
SPCX = "SPCX"


def _sleeve(sym: str) -> str:
    s = sym.upper()
    if s in CORE:
        return "core"
    if s in SPY:
        return "spy"
    if s in METAL:
        return "metal"
    return "nyse"


def _load_paper_env() -> None:
    vals = dotenv_values(PAPER_BOOK / ".env")
    for k in list(os.environ):
        if "ALPACA" in k or "APCA" in k:
            os.environ.pop(k, None)
    os.environ.update({k: v for k, v in vals.items() if v is not None})


def _alpaca_snapshot() -> dict[str, Any]:
    from alpaca.trading.client import TradingClient

    _load_paper_env()
    key = (
        os.getenv("ALPACA_API_KEY")
        or os.getenv("APCA_API_KEY_ID")
        or os.getenv("PAPER_API_KEY")
    )
    sec = (
        os.getenv("ALPACA_SECRET_KEY")
        or os.getenv("APCA_API_SECRET_KEY")
        or os.getenv("PAPER_SECRET_KEY")
    )
    if not key or not sec:
        raise RuntimeError("Missing Alpaca paper API keys in book .env")

    client = TradingClient(key, sec, paper=True)
    acct = client.get_account()
    equity = float(acct.equity)
    cash = float(acct.cash)

    rows: list[dict[str, Any]] = []
    for p in client.get_all_positions():
        sym = str(p.symbol).upper()
        cost = float(p.avg_entry_price) * abs(float(p.qty))
        mv = float(p.market_value)
        up = float(p.unrealized_pl)
        pct = (up / cost) if cost else 0.0
        rows.append(
            {
                "sym": sym,
                "sleeve": _sleeve(sym),
                "cost": cost,
                "mv": mv,
                "up": up,
                "pct": pct,
            }
        )

    def agg(sleeve: str, exclude: set[str] | None = None) -> dict[str, float]:
        xs = [
            r
            for r in rows
            if r["sleeve"] == sleeve and (not exclude or r["sym"] not in exclude)
        ]
        cost = sum(r["cost"] for r in xs)
        mv = sum(r["mv"] for r in xs)
        up = sum(r["up"] for r in xs)
        return {
            "n": float(len(xs)),
            "cost": cost,
            "mv": mv,
            "up": up,
            "open_pct": (up / cost) if cost else 0.0,
            "book_pct": (mv / equity) if equity else 0.0,
        }

    return {
        "equity": equity,
        "cash": cash,
        "cash_pct": (cash / equity) if equity else 0.0,
        "core": agg("core"),
        "nyse": agg("nyse"),
        "nyse_ex_spcx": agg("nyse", {SPCX}),
        "metal": agg("metal"),
        "spy": agg("spy"),
        "positions": rows,
    }


def _pack_open_ret_series(days: int) -> list[dict[str, Any]]:
    """Approximate sleeve open % from overnight packs (UPnL / implied MV)."""
    packs = sorted(ROOT.joinpath("data").glob("overnight_pack_*.txt"))
    out: list[dict[str, Any]] = []
    for p in packs[-max(days * 2, days) :]:
        text = p.read_text(encoding="utf-8", errors="replace")
        m_eq = re.search(r"Paper:\s*\$([0-9,]+\.\d+)", text)
        if not m_eq:
            continue
        eq = float(m_eq.group(1).replace(",", ""))
        date = p.stem.replace("overnight_pack_", "")
        row: dict[str, Any] = {"date": date, "equity": eq}
        for m in re.finditer(
            r"(core|nyse|spy|metal)\s+(\d+)\s+([+-]?[0-9,]+\.\d+)\s+([0-9.]+)%",
            text,
        ):
            sleeve = m.group(1)
            upnl = float(m.group(3).replace(",", ""))
            pct_book = float(m.group(4))
            mv = eq * pct_book / 100.0
            row[f"{sleeve}_open_pct"] = (upnl / mv) if mv else None
            row[f"{sleeve}_upnl"] = upnl
            row[f"{sleeve}_pct_book"] = pct_book
        out.append(row)
    return out[-days:] if days > 0 else out


def _read_gate_log(days: int) -> list[dict[str, Any]] | None:
    if not GATE_LOG.is_file():
        return None
    with GATE_LOG.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return None
    out: list[dict[str, Any]] = []
    for r in rows[-days:]:
        try:
            vti = float(r.get("vti_open_pct") or "nan")
            nyse = float(r.get("nyse_open_pct") or "nan")
        except ValueError:
            continue
        ex_raw = (r.get("nyse_ex_spcx_pct") or "").strip()
        ex = float(ex_raw) if ex_raw else None
        out.append(
            {
                "date": r.get("date"),
                "core_open_pct": vti / 100.0,
                "nyse_open_pct": nyse / 100.0,
                "nyse_ex_spcx_pct": (ex / 100.0) if ex is not None else None,
                "source": r.get("source") or "",
                "nyse_beats_vti": str(r.get("nyse_beats_vti")).lower() == "true",
                "nyse_ex_spcx_beats_vti": (
                    str(r.get("nyse_ex_spcx_beats_vti")).lower() == "true"
                    if (r.get("nyse_ex_spcx_beats_vti") or "").strip()
                    else None
                ),
            }
        )
    return out or None


def _gate_ab(
    snap: dict[str, Any], series: list[dict[str, Any]], need_days: int, source: str
) -> dict[str, Any]:
    nyse_pct = float(snap["nyse"]["open_pct"])
    core_pct = float(snap["core"]["open_pct"])
    nyse_ex = float(snap["nyse_ex_spcx"]["open_pct"])

    today_a = nyse_pct > core_pct
    today_b = nyse_ex > core_pct

    if source == "gate_ab_log":
        a_flags = [r.get("nyse_beats_vti") for r in series if r.get("nyse_beats_vti") is not None]
        b_flags = [
            r.get("nyse_ex_spcx_beats_vti")
            for r in series
            if r.get("nyse_ex_spcx_beats_vti") is not None
        ]
        wins_a = sum(1 for v in a_flags if v)
        wins_b = sum(1 for v in b_flags if v)
        n_a, n_b = len(a_flags), len(b_flags)
        hist_note = (
            f"gate_ab_log: A {wins_a}/{n_a} True; B(ex-SPCX) {wins_b}/{n_b} True "
            f"(need ~{need_days}; pack_approx rows have blank B)."
        )
        if n_a < need_days:
            hist_a = "INCOMPLETE"
            hist_note = f"THIN HISTORY — {hist_note}"
        elif wins_a >= need_days or (n_a >= need_days and wins_a / n_a >= 0.8):
            hist_a = "PASS" if wins_a >= need_days else "WEAK"
        else:
            hist_a = "FAIL"
        n_hist, wins = n_a, wins_a
    else:
        comparable = [
            r
            for r in series
            if r.get("nyse_open_pct") is not None and r.get("core_open_pct") is not None
        ]
        wins = sum(1 for r in comparable if r["nyse_open_pct"] > r["core_open_pct"])
        n_hist = len(comparable)
        hist_note = (
            f"{wins}/{n_hist} pack days NYSE open% > core open% "
            f"(need ~{need_days}; pack approx = UPnL/sleeve MV; no ex-SPCX)."
        )
        if n_hist < need_days:
            hist_a = "INCOMPLETE"
            hist_note = f"THIN HISTORY — {hist_note}"
        elif wins >= need_days:
            hist_a = "PASS"
        elif n_hist and (wins / n_hist) >= 0.8:
            hist_a = "WEAK"
        else:
            hist_a = "FAIL"

    return {
        "A_today": "PASS" if today_a else "FAIL",
        "B_today": "PASS" if today_b else "FAIL",
        "A_history_stub": hist_a,
        "history_note": hist_note,
        "nyse_open_pct": nyse_pct,
        "nyse_ex_spcx_open_pct": nyse_ex,
        "core_open_pct": core_pct,
        "comparable_pack_days": n_hist,
        "nyse_beats_core_pack_days": wins,
        "history_source": source,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--days",
        type=int,
        default=15,
        help="Trading-day window for pack history stub (default 15)",
    )
    args = ap.parse_args()

    print("=== Core vs NYSE performance gates (READ-ONLY) ===")
    print(f"as_of: {datetime.now().isoformat(timespec='seconds')}")
    print("freeze: measure only — no .env / trim / rebalance")
    print()

    try:
        snap = _alpaca_snapshot()
    except Exception as exc:
        print(f"ERROR Alpaca snapshot: {exc}")
        # Fall back to heartbeat equity only
        hb_path = PAPER_BOOK / "bot_heartbeat.json"
        if not hb_path.exists():
            return 1
        hb = json.loads(hb_path.read_text(encoding="utf-8"))
        print(f"(heartbeat equity only) equity={hb.get('equity')}")
        return 1

    eq = snap["equity"]
    print(f"Book equity: ${eq:,.2f}")
    print(f"  core  {snap['core']['book_pct']:.1%} of book  open%={snap['core']['open_pct']:+.2%}")
    print(f"  nyse  {snap['nyse']['book_pct']:.1%} of book  open%={snap['nyse']['open_pct']:+.2%}")
    print(
        f"  nyse ex-SPCX open%={snap['nyse_ex_spcx']['open_pct']:+.2%}  "
        f"(n={int(snap['nyse_ex_spcx']['n'])})"
    )
    print(f"  metal {snap['metal']['book_pct']:.1%} of book  open%={snap['metal']['open_pct']:+.2%}")
    print(f"  cash  {snap['cash_pct']:.1%} of book  (${snap['cash']:,.2f})")
    print()

    log_series = _read_gate_log(args.days)
    if log_series is not None:
        series = log_series
        hist_src = "gate_ab_log"
    else:
        series = _pack_open_ret_series(args.days)
        hist_src = "overnight_packs"
    gates = _gate_ab(snap, series, args.days, hist_src)
    print(f"Gates A-B (snapshot + {hist_src}):")
    print(
        f"  A today (NYSE open% > VTI/core): {gates['A_today']}  "
        f"({gates['nyse_open_pct']:+.2%} vs {gates['core_open_pct']:+.2%})"
    )
    print(
        f"  B today (same ex-SPCX):          {gates['B_today']}  "
        f"({gates['nyse_ex_spcx_open_pct']:+.2%} vs {gates['core_open_pct']:+.2%})"
    )
    print(f"  A history ({args.days}d via {hist_src}): {gates['A_history_stub']}")
    print(f"  note: {gates['history_note']}")
    print()
    print("Gates C-E: not auto-scored here (capacity / MC / regime — manual).")
    print("Reminder: open % != promote. Do not change trading knobs while freeze is ON.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
