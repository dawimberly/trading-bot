"""
Phase 2b: Out-of-sample (OOS) validation of the RHYME D-gate decision.

Splits the 180d equity trade set chronologically -- NOT randomly, since
these are time-ordered trades and a random shuffle would leak future
information into the "train" half. First N% of the date range is the
train/decision window; the remainder is the untouched holdout.

The point: the 180d full-sample gate comparison (D vs C vs E) already told
us D looks best on THAT sample. This script checks whether a gate decision
made using only the train half still holds up on the holdout half it never
saw. If it does, that's real evidence. If it falls apart, the 180d result
was likely overfit to that specific window's C/D/E mix.

Research-only. Freeze-safe. Reads existing trade CSV, writes a comparison,
does not touch live/paper or promote anything.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
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
    days = sorted(trades["session_date"].unique().tolist())
    rhyme_map = label_rhyme_for_universe(days)
    return trades.merge(rhyme_map, left_on="session_date", right_on="day", how="left")


def chronological_split(
    trades: pd.DataFrame, train_frac: float = 0.5
) -> tuple[pd.DataFrame, pd.DataFrame, date]:
    """
    Split by DATE, not by row count -- avoids splitting a session's trades
    across train/holdout, and keeps the split meaningful in calendar terms.
    """
    all_days = sorted(trades["session_date"].unique())
    if len(all_days) < 4:
        raise ValueError(f"Need more unique session days to split (got {len(all_days)})")

    split_idx = max(1, min(len(all_days) - 1, int(len(all_days) * train_frac)))
    split_date = all_days[split_idx]

    train = trades[trades["session_date"] < split_date].copy()
    holdout = trades[trades["session_date"] >= split_date].copy()
    return train, holdout, split_date


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


def rhyme_breakdown_simple(trades: pd.DataFrame, prefix: str) -> list[dict]:
    if trades.empty:
        return []
    rows = []
    for label, group in trades.groupby("rhyme"):
        rows.append(stats(group, f"{prefix}_rhyme_{label}"))
    return rows


def decide_gate_from_train(train: pd.DataFrame) -> str:
    """
    Decision rule locked on TRAIN only (what we'd have written down mid-sample):
    prefer blocking C if train C avg R is worse than train non-C avg R.
    Returns gate name for logging — applied gate is always d_allow_c_block
    when that decision fires (matches Phase 2 full-sample preference).
    """
    if train.empty or "rhyme" not in train.columns:
        return "insufficient"
    c = train[train["rhyme"] == "C"]
    non_c = train[train["rhyme"] != "C"]
    if c.empty or non_c.empty:
        return "insufficient"
    c_r = float(c["r_multiple"].mean())
    non_c_r = float(non_c["r_multiple"].mean())
    if c_r < non_c_r:
        return "d_allow_c_block"
    return "no_gate"


def run_oos_check(trades_csv: Path, train_frac: float = 0.5) -> pd.DataFrame:
    trades = load_trades(trades_csv)
    trades = attach_rhyme(trades)

    train, holdout, split_date = chronological_split(trades, train_frac)
    train_regimes = sorted(train["rhyme"].dropna().unique().tolist())
    holdout_regimes = sorted(holdout["rhyme"].dropna().unique().tolist())
    gate_decision = decide_gate_from_train(train)

    print(f"Split date: {split_date}")
    print(f"  train={len(train)} trades  days={train['session_date'].nunique()}  rhymes={train_regimes}")
    print(f"  holdout={len(holdout)} trades  days={holdout['session_date'].nunique()}  rhymes={holdout_regimes}")
    print(f"  train gate decision (blind): {gate_decision}\n")

    results: list[dict] = []

    results.append(stats(train, "train_baseline"))
    results.append(stats(holdout, "holdout_baseline"))
    results.extend(rhyme_breakdown_simple(train, "train"))
    results.extend(rhyme_breakdown_simple(holdout, "holdout"))

    # Apply the SAME gate decided on train (D-allow, C-block) to holdout blind
    train_gated = train[train["rhyme"] != "C"]
    holdout_gated = holdout[holdout["rhyme"] != "C"]

    results.append(stats(train_gated, "train_d_allow_c_block"))
    results.append(stats(holdout_gated, "holdout_d_allow_c_block"))

    holdout_d_only = holdout[holdout["rhyme"] == "D"]
    results.append(stats(holdout_d_only, "holdout_d_only"))

    # Explicit OOS verdict row for the CSV
    hb = stats(holdout, "holdout_baseline")
    hg = stats(holdout_gated, "holdout_d_allow_c_block")
    if hb.get("n_trades") and hg.get("n_trades"):
        results.append(
            {
                "variant": "oos_verdict",
                "n_trades": hg["n_trades"],
                "win_rate": hg["win_rate"],
                "avg_r_multiple": hg["avg_r_multiple"],
                "avg_pnl_bps": hg["avg_pnl_bps"],
                "total_pnl_bps": hg["total_pnl_bps"],
                "note": (
                    f"gated_avgR={hg['avg_r_multiple']} vs baseline_avgR={hb['avg_r_multiple']}; "
                    f"gated_total={hg['total_pnl_bps']} vs baseline_total={hb['total_pnl_bps']}; "
                    f"train_decision={gate_decision}"
                ),
            }
        )

    return pd.DataFrame(results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 2b chronological OOS RHYME gate check")
    parser.add_argument(
        "--trades-csv",
        type=Path,
        default=HERE / "output" / "sneaky_pivot_equity_trades_180d.csv",
    )
    parser.add_argument(
        "--train-frac",
        type=float,
        default=0.5,
        help="Fraction of the date range used as train/decision window",
    )
    args = parser.parse_args(argv)

    csv_path = args.trades_csv
    if not csv_path.is_file():
        fallback = HERE / "output" / "sneaky_pivot_equity_trades.csv"
        if fallback.is_file():
            print(f"Missing {csv_path.name}; falling back to {fallback.name}")
            csv_path = fallback
        else:
            print(f"Missing trades CSV: {args.trades_csv}")
            return 1

    result = run_oos_check(csv_path, args.train_frac)
    pd.set_option("display.width", 160)
    pd.set_option("display.max_colwidth", 100)
    print(result.to_string(index=False))

    out_path = csv_path.parent / "phase2b_oos_comparison.csv"
    result.to_csv(out_path, index=False)
    print(f"\nWrote comparison to {out_path}")
    print("\nRead this as: does 'holdout_d_allow_c_block' beat 'holdout_baseline'")
    print("on data the gate decision never saw? That's the actual answer, not the")
    print("full-sample 180d numbers from before.")
    print("Research only -- freeze unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
