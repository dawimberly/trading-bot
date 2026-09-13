"""Historical 1R hit rate on paper NYSE buys (measure-only).

Uses the same stop/target math as live paper (2.0× daily ATR, 1% floor, RR=1).
Hit = a later daily close reached the 1R target before the matching sell (or now).

Usage (from stock-bot/):
  python scripts/analysis/one_r_hit_test.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

import config  # noqa: E402
from modules.data_loader import load_close_matrix  # noqa: E402
from modules.one_r_hit_test import plan_from_entry  # noqa: E402
from modules.paper_journal import prefer_fill_rows  # noqa: E402
from modules.risk_management import calculate_atr  # noqa: E402
from trade_reconciliation import read_journal_csv  # noqa: E402

JOURNAL = (
    ROOT
    / "data"
    / "portal"
    / "users"
    / "dawimberly"
    / "books"
    / "alpaca_paper_v2"
    / "paper_journal.csv"
)
OUT_MD = Path(__file__).with_name("one_r_hit_test_last.md")
SKIP_SYM = frozenset({"VTI", "VOO", "SPY", "QQQ"})


def _num(val: Any) -> float | None:
    try:
        x = float(val)
    except (TypeError, ValueError):
        return None
    if x != x or x <= 0:
        return None
    return x


def _load_fills() -> pd.DataFrame:
    df, _warn = read_journal_csv(JOURNAL)
    fills = prefer_fill_rows(df)
    if fills is None or fills.empty:
        return pd.DataFrame()
    fills = fills.copy()
    fills["timestamp"] = pd.to_datetime(fills["timestamp"], errors="coerce")
    fills = fills.dropna(subset=["timestamp"]).sort_values("timestamp")
    fills["symbol"] = fills["symbol"].astype(str).str.strip().str.upper()
    fills["side"] = fills["side"].astype(str).str.strip().str.lower()
    fills["sleeve"] = fills.get("sleeve", pd.Series("", index=fills.index)).astype(str)
    return fills


def _pair_rounds(fills: pd.DataFrame) -> list[dict[str, Any]]:
    open_lots: dict[str, list[dict[str, Any]]] = {}
    rounds: list[dict[str, Any]] = []
    for row in fills.itertuples(index=False):
        sym = str(getattr(row, "symbol", "") or "")
        if not sym or sym in SKIP_SYM:
            continue
        sleeve = str(getattr(row, "sleeve", "") or "").lower()
        if sleeve in ("vti", "spy", "crypto", "core"):
            continue
        side = str(getattr(row, "side", "") or "")
        ts = getattr(row, "timestamp", None)
        px = _num(getattr(row, "price", None))
        if side == "buy" and px:
            open_lots.setdefault(sym, []).append({"ts": ts, "entry": px, "symbol": sym})
        elif side == "sell" and open_lots.get(sym):
            lot = open_lots[sym].pop(0)
            lot["exit_ts"] = ts
            lot["exit"] = px
            rounds.append(lot)
    now = pd.Timestamp(datetime.now())
    for lots in open_lots.values():
        for lot in lots:
            lot["exit_ts"] = now
            lot["exit"] = None
            lot["open"] = True
            rounds.append(lot)
    return rounds


def _score(rounds: list[dict[str, Any]], data: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for lot in rounds:
        sym = lot["symbol"]
        if sym not in data.columns:
            continue
        entry_ts = pd.Timestamp(lot["ts"])
        exit_ts = pd.Timestamp(lot["exit_ts"])
        held_days = int((exit_ts.normalize() - entry_ts.normalize()).days)
        series = data[sym].dropna()
        prior = series[series.index < entry_ts.normalize()]
        if len(prior) < 20:
            continue
        window = prior.to_frame(name=sym)
        atr = calculate_atr(window, sym)
        if atr is None or atr <= 0:
            continue
        plan = plan_from_entry(lot["entry"], atr, rr=1.0)
        after = series[(series.index >= entry_ts.normalize()) & (series.index <= exit_ts)]
        if after.empty:
            continue
        mfe_px = float(after.max())
        mae_px = float(after.min())
        hit = mfe_px + 1e-9 >= plan["target"]
        rows.append(
            {
                "symbol": sym,
                "entry_ts": entry_ts,
                "exit_ts": exit_ts,
                "open": bool(lot.get("open")),
                "entry": round(lot["entry"], 2),
                "target": plan["target"],
                "target_pct": round(plan["target_pct"] * 100.0, 2),
                "stop": plan["stop"],
                "atr": plan["atr"],
                "mfe_pct": round(100.0 * (mfe_px - lot["entry"]) / lot["entry"], 2),
                "mae_pct": round(100.0 * (mae_px - lot["entry"]) / lot["entry"], 2),
                "held_days": held_days,
                "hit": hit,
                "exit_pct": (
                    None
                    if lot.get("exit") is None
                    else round(100.0 * (lot["exit"] - lot["entry"]) / lot["entry"], 2)
                ),
            }
        )
    return pd.DataFrame(rows)


def main() -> int:
    fills = _load_fills()
    if fills.empty:
        print(f"No fills in {JOURNAL}")
        return 1
    rounds = _pair_rounds(fills)
    symbols = sorted({r["symbol"] for r in rounds})
    print(f"Journal fills: {len(fills)}  NYSE rounds: {len(rounds)}  symbols: {len(symbols)}")
    data = load_close_matrix(interval="1d", days=400)
    keep = [c for c in symbols if c in data.columns]
    data = data[keep] if keep else data
    scored = _score(rounds, data)
    if scored.empty:
        print("No rounds with enough daily history to score.")
        return 1
    closed = scored.loc[~scored["open"]]
    n = len(closed) if not closed.empty else 0
    hits = int(closed["hit"].sum()) if n else 0
    rate = (100.0 * hits / n) if n else 0.0
    overnight = closed.loc[closed["held_days"] >= 1] if n else closed
    on = len(overnight)
    on_hits = int(overnight["hit"].sum()) if on else 0
    on_rate = (100.0 * on_hits / on) if on else 0.0
    still_open = scored.loc[scored["open"]]
    open_hits = int(still_open["hit"].sum()) if not still_open.empty else 0
    lines = [
        "# Paper NYSE 1R hit test (historical)",
        "",
        f"Journal: `{JOURNAL}`",
        "Rule: 1R target = entry + max(2.0× daily ATR, 1% of entry). Hit if a later daily **close** reached it before the sell.",
        "",
        f"- Closed rounds scored: **{n}** — hit 1R: **{hits} ({rate:.0f}%)**",
        f"- Held to a later day: **{on}** — hit 1R: **{on_hits} ({on_rate:.0f}%)** (same-day exits never get a second close)",
        f"- Still open: {len(still_open)} (already tagged 1R: {open_hits})",
        f"- Median target: {scored['target_pct'].median():.2f}%  |  median MFE: {scored['mfe_pct'].median():.2f}%",
        "",
        "## Closed rounds",
        "",
        "| symbol | days | entry | target % | MFE % | MAE % | exit % | hit |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    show = closed.sort_values("entry_ts", ascending=False).head(40)
    for r in show.itertuples(index=False):
        lines.append(
            f"| {r.symbol} | {r.held_days} | {r.entry:.2f} | {r.target_pct:.2f} | {r.mfe_pct:.2f} | "
            f"{r.mae_pct:.2f} | {r.exit_pct if r.exit_pct is not None else ''} | "
            f"{'yes' if r.hit else 'no'} |"
        )
    text = "\n".join(lines) + "\n"
    OUT_MD.write_text(text, encoding="utf-8")
    print(text)
    print(f"Wrote {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
