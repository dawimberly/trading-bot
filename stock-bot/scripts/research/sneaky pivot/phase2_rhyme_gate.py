"""
Phase 2: RHYME gate test.

Takes the trades already produced by Phase 0+1 (loaded from CSV, no need to
re-run the backtest) and answers: does gating or side-flipping on RHYME turn
the blended result into something better?

Compares, on the SAME trade set:
    1. baseline          -- take every trade as generated
    2. d_only            -- only RHYME_D days
    3. d_allow_c_block   -- drop C entirely (keep D + any other)
    4. d_allow_c_flip    -- D as-is; on C, naive mirror of R/pnl (HINT ONLY)

Flip is a what-if, not a re-simulated short-side backtest.

Research-only. Freeze-safe.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness import label_rhyme_for_universe  # noqa: E402


def load_trades(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path, parse_dates=["entry_ts", "exit_ts"])
    df["session_date"] = pd.to_datetime(df["session_date"]).dt.date
    return df


def attach_rhyme(trades: pd.DataFrame) -> pd.DataFrame:
    """RHYME is market-wide — join on day only."""
    days = sorted(trades["session_date"].unique().tolist())
    rhyme_map = label_rhyme_for_universe(days)
    return trades.merge(rhyme_map, left_on="session_date", right_on="day", how="left")


def stats(trades: pd.DataFrame, label: str) -> dict:
    if trades.empty:
        return {"variant": label, "n_trades": 0}
    wins = trades["pnl_bps"] > 0
    return {
        "variant": label,
        "n_trades": int(len(trades)),
        "win_rate": round(float(wins.mean()), 3),
        "avg_r_multiple": round(float(trades["r_multiple"].mean()), 3),
        "avg_pnl_bps": round(float(trades["pnl_bps"].mean()), 2),
        "total_pnl_bps": round(float(trades["pnl_bps"].sum()), 1),
    }


def approximate_flip(trades: pd.DataFrame) -> pd.DataFrame:
    """
    Naive mirror of R / pnl_bps for C-day flip what-if.
    Flagged — not a real opposite-side re-simulation.
    """
    flipped = trades.copy()
    flipped["needs_resim"] = True
    flipped["approx_r_multiple"] = -trades["r_multiple"]
    flipped["approx_pnl_bps"] = -trades["pnl_bps"]
    return flipped


def run_gate_test(trades_csv: Path) -> pd.DataFrame:
    trades = load_trades(trades_csv)
    trades = attach_rhyme(trades)

    results: list[dict] = []

    results.append(stats(trades, "baseline"))

    d_only = trades[trades["rhyme"] == "D"]
    results.append(stats(d_only, "d_only"))

    d_allow_c_block = trades[trades["rhyme"] != "C"]
    results.append(stats(d_allow_c_block, "d_allow_c_block"))

    non_c = trades[trades["rhyme"] != "C"].copy()
    c_trades = trades[trades["rhyme"] == "C"].copy()
    flip_piece = c_trades.copy()
    flip_piece["r_multiple"] = -c_trades["r_multiple"].astype(float)
    flip_piece["pnl_bps"] = -c_trades["pnl_bps"].astype(float)
    flip_piece["needs_resim"] = True
    # Align columns for concat (drop helper cols that aren't on non_c).
    flip_piece = flip_piece[non_c.columns]
    combined = pd.concat([non_c, flip_piece], ignore_index=True, sort=False)
    flip_stats = stats(combined, "d_allow_c_flip_APPROX")
    flip_stats["warning"] = (
        "C-flip uses naive mirrored R, not a real short-side re-sim -- hint only"
    )
    results.append(flip_stats)

    return pd.DataFrame(results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 2 RHYME gate comparison")
    parser.add_argument(
        "--trades-csv",
        type=Path,
        default=HERE / "output" / "sneaky_pivot_equity_trades.csv",
    )
    args = parser.parse_args(argv)

    if not args.trades_csv.is_file():
        print(f"Missing trades CSV: {args.trades_csv}")
        return 1

    print(f"Loading {args.trades_csv} ...")
    trades_preview = load_trades(args.trades_csv)
    trades_preview = attach_rhyme(trades_preview)
    regime_counts = (
        trades_preview["rhyme"].value_counts(dropna=False).sort_index().to_dict()
    )
    print(f"RHYME letters in sample: {regime_counts}")
    missing = [x for x in ("A", "B", "C", "D", "E") if x not in regime_counts]
    if missing:
        print(
            f"NOTE: no trades on RHYME {missing} — "
            f"'D is best' here only means D vs regimes present, not vs A/B/E."
        )

    result = run_gate_test(args.trades_csv)
    pd.set_option("display.width", 140)
    pd.set_option("display.max_colwidth", 80)
    print(result.to_string(index=False))

    out_path = args.trades_csv.parent / "phase2_gate_comparison.csv"
    result.to_csv(out_path, index=False)
    print(f"\nWrote comparison to {out_path}")
    print("Research only -- freeze unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
