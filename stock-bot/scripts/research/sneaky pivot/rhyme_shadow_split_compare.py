"""
Shadow: BUGGY vs FIXED-blended vs EQUITY-ONLY vol for RHYME labels.

Also reports crypto_vol_score High frequency (proposed crypto_vol_gate input)
without feeding it into RHYME.

Does not modify market_context / live / paper.

Run from stock-bot/:
    python "scripts/research/sneaky pivot/rhyme_shadow_split_compare.py"
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import modules.market_context as market_context  # noqa: E402
from cross_asset_vol_score_shadow import (  # noqa: E402
    composition_snapshot,
    cross_asset_vol_score_BUGGY,
    cross_asset_vol_score_FIXED,
    crypto_vol_score,
    equity_vol_score,
)
from modules.data_loader import load_close_matrix  # noqa: E402
from modules.market_context import (  # noqa: E402
    get_market_regime,
    get_price_sentiment,
    get_volatility,
    normalize_regime_sentiment,
    regime_vol_threshold,
    reset_regime_hysteresis,
    set_regime_bar_index,
)

OUT = HERE / "output"
OUT.mkdir(exist_ok=True)


def _letter(full: str) -> str:
    if full.startswith("RHYME_"):
        return full.split(":")[0].replace("RHYME_", "")
    return str(full)[:1]


def walk_labels(matrix: pd.DataFrame, start: dt.date, end: dt.date, score_fn) -> pd.DataFrame:
    original = market_context.cross_asset_vol_score
    announce = market_context.announce_regime_change
    market_context.cross_asset_vol_score = score_fn
    market_context.announce_regime_change = lambda r: r
    reset_regime_hysteresis()
    thresh = float(regime_vol_threshold("1d"))
    rows = []
    try:
        for i in range(20, len(matrix)):
            set_regime_bar_index(i)
            window = matrix.iloc[: i + 1]
            day = pd.Timestamp(matrix.index[i]).date()
            sent_raw = get_price_sentiment(window)
            vol_label = get_volatility(window, interval="1d")
            full = get_market_regime(sent_raw, vol_label, apply_hysteresis=True)
            if day < start or day > end:
                continue
            score = float(score_fn(window))
            rows.append(
                {
                    "day": day,
                    "rhyme_label": _letter(full),
                    "vol_score": score,
                    "vol_high": score > thresh,
                    "sentiment_norm": float(normalize_regime_sentiment(sent_raw)),
                }
            )
    finally:
        market_context.cross_asset_vol_score = original
        market_context.announce_regime_change = announce
        reset_regime_hysteresis()
    return pd.DataFrame(rows)


def crypto_high_walk(matrix: pd.DataFrame, start: dt.date, end: dt.date) -> pd.DataFrame:
    """Crypto panel High days vs same daily threshold (shadow for crypto_vol_gate)."""
    thresh = float(regime_vol_threshold("1d"))
    rows = []
    for i in range(20, len(matrix)):
        window = matrix.iloc[: i + 1]
        day = pd.Timestamp(matrix.index[i]).date()
        if day < start or day > end:
            continue
        score = crypto_vol_score(window)
        rows.append({"day": day, "crypto_vol_score": score, "crypto_high": score > thresh})
    return pd.DataFrame(rows)


def freq_table(df: pd.DataFrame, label: str) -> dict:
    if df.empty:
        return {"variant": label, "n": 0}
    vc = df["rhyme_label"].value_counts()
    n = len(df)
    out = {"variant": label, "n": n}
    for L in "ABCDE":
        c = int(vc.get(L, 0))
        out[f"{L}"] = c
        out[f"{L}_pct"] = round(100.0 * c / n, 1) if n else 0.0
    out["AB_pct"] = round(100.0 * (vc.get("A", 0) + vc.get("B", 0)) / n, 1) if n else 0.0
    out["vol_high_pct"] = round(100.0 * df["vol_high"].mean(), 1) if n else 0.0
    return out


def main() -> int:
    end = dt.date.today()
    start = end - dt.timedelta(days=900)
    print(f"Window: {start} -> {end}")
    print("Loading close matrix...")
    matrix = load_close_matrix(interval="1d", days=940)
    matrix = matrix.copy()
    matrix.index = pd.to_datetime(matrix.index)

    snap = composition_snapshot(matrix)
    print("\n=== Composition (full loaded matrix) ===")
    print(
        f"  equity cols={snap['n_equity']}  crypto cols={snap['n_crypto']}\n"
        f"  equity_std={snap['equity_std']:.4f} ({snap['equity_std_vs_thresh']:.2f}x thresh)\n"
        f"  crypto_std={snap['crypto_std']:.4f} ({snap['crypto_std_vs_thresh']:.2f}x thresh)\n"
        f"  blended_FIXED={snap['blended_fixed']:.4f} ({snap['blended_fixed_vs_thresh']:.2f}x)\n"
        f"  blended_BUGGY={snap['blended_buggy']:.4f} ({snap['blended_buggy_vs_thresh']:.2f}x)\n"
        f"  threshold={snap['threshold']}"
    )

    variants = [
        ("BUGGY_blended", cross_asset_vol_score_BUGGY),
        ("FIXED_blended", cross_asset_vol_score_FIXED),
        ("EQUITY_ONLY", equity_vol_score),
    ]
    frames = {}
    summary_rows = []
    for name, fn in variants:
        print(f"\nWalking RHYME labels with {name}...")
        df = walk_labels(matrix, start, end, fn)
        frames[name] = df
        summary_rows.append(freq_table(df, name))
        print(f"  AB%={summary_rows[-1]['AB_pct']}  letters={df['rhyme_label'].value_counts().to_dict()}")

    print("\nWalking crypto_vol_score High days (not fed into RHYME)...")
    cwalk = crypto_high_walk(matrix, start, end)
    crypto_high_pct = 100.0 * cwalk["crypto_high"].mean() if len(cwalk) else 0.0
    print(f"  crypto High days: {int(cwalk['crypto_high'].sum())}/{len(cwalk)} ({crypto_high_pct:.1f}%)")

    summary = pd.DataFrame(summary_rows)
    summary["crypto_high_pct_separate"] = crypto_high_pct
    print("\n=== A/B frequency comparison ===")
    cols = ["variant", "n", "AB_pct", "A_pct", "B_pct", "C_pct", "D_pct", "E_pct", "vol_high_pct"]
    print(summary[cols].to_string(index=False))

    # Diff FIXED_blended vs EQUITY_ONLY into AB
    if "FIXED_blended" in frames and "EQUITY_ONLY" in frames:
        m = frames["BUGGY_blended"].merge(
            frames["EQUITY_ONLY"], on="day", suffixes=("_buggy", "_eq")
        )
        m = m.merge(
            frames["FIXED_blended"][["day", "rhyme_label", "vol_score"]].rename(
                columns={"rhyme_label": "rhyme_fixed", "vol_score": "vol_fixed"}
            ),
            on="day",
        )
        m["into_AB_fixed"] = m["rhyme_fixed"].isin(["A", "B"]) & ~m["rhyme_label_buggy"].isin(
            ["A", "B"]
        )
        m["into_AB_equity"] = m["rhyme_label_eq"].isin(["A", "B"]) & ~m[
            "rhyme_label_buggy"
        ].isin(["A", "B"])
        print(
            f"\nInto A/B vs BUGGY: FIXED_blended={int(m['into_AB_fixed'].sum())}  "
            f"EQUITY_ONLY={int(m['into_AB_equity'].sum())}"
        )
        m.to_csv(OUT / "rhyme_shadow_split_diff_900d.csv", index=False)

    summary.to_csv(OUT / "rhyme_shadow_split_summary_900d.csv", index=False)
    cwalk.to_csv(OUT / "rhyme_shadow_crypto_vol_high_900d.csv", index=False)

    print(f"\nWrote {OUT / 'rhyme_shadow_split_summary_900d.csv'}")
    print("Research only -- freeze unchanged. market_context not patched.")
    print(
        "\nGreen-light check: EQUITY_ONLY AB% should be sparse vs FIXED_blended; "
        "crypto High% is separate for crypto_vol_gate design."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
