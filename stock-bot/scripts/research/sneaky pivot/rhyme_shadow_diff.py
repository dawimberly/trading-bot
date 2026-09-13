"""
Shadow comparison: recompute RHYME labels for the full historical walk
using the FIXED cross_asset_vol_score, diff against the currently-live
(buggy) labels day by day.

Answers:
    - How many days flip label at all, and to what?
    - Of those, how many would have flipped INTO or OUT OF A/B?
    - Next: cross-reference flipped_into_AB days against rhyme_gate_audit.md
      GATE hits / run logs (manual — gates are code locations, not dated events).

Does not modify any live/paper code. Monkeypatches market_context only
inside this process for the FIXED pass, then restores.

Run from stock-bot/:
    python "scripts/research/sneaky pivot/rhyme_shadow_diff.py"
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

import modules.market_context as market_context  # noqa: E402
from cross_asset_vol_score_shadow import (  # noqa: E402
    cross_asset_vol_score_BUGGY,
    cross_asset_vol_score_FIXED,
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


def _letter(full: str) -> str:
    if not full:
        return ""
    if full.startswith("RHYME_"):
        return full.split(":")[0].replace("RHYME_", "")
    return str(full)[:1]


def compute_labels_for_walk(
    start: dt.date, end: dt.date, *, use_fixed: bool
) -> pd.DataFrame:
    """
    Expanding-window walk matching harness / backtester usage:
      sentiment = get_price_sentiment(window)
      vol = get_volatility(window)  # uses patched cross_asset_vol_score
      regime = get_market_regime(sentiment, vol, apply_hysteresis=True)

    Returns DataFrame: day, rhyme_label, vol_score, sentiment_norm, vol_high
    """
    # Load enough history; filter to [start, end] after walk for reporting.
    lookback = max(120, (end - start).days + 40)
    matrix = load_close_matrix(interval="1d", days=lookback)
    if matrix is None or matrix.empty or len(matrix) < 30:
        raise RuntimeError("load_close_matrix returned insufficient daily data")

    matrix = matrix.copy()
    matrix.index = pd.to_datetime(matrix.index)

    original = market_context.cross_asset_vol_score
    score_fn = cross_asset_vol_score_FIXED if use_fixed else cross_asset_vol_score_BUGGY
    market_context.cross_asset_vol_score = score_fn

    announce = market_context.announce_regime_change
    market_context.announce_regime_change = lambda r: r
    reset_regime_hysteresis()

    thresh = float(regime_vol_threshold("1d"))
    warmup = 20
    rows: list[dict] = []

    try:
        for i in range(warmup, len(matrix)):
            set_regime_bar_index(i)
            window = matrix.iloc[: i + 1]
            day = pd.Timestamp(matrix.index[i]).date()
            if day < start or day > end:
                # Still advance hysteresis clock so state matches a continuous walk,
                # but only emit rows in the requested window.
                sent_raw = get_price_sentiment(window)
                vol_label = get_volatility(window, interval="1d")
                get_market_regime(sent_raw, vol_label, apply_hysteresis=True)
                continue

            sent_raw = get_price_sentiment(window)
            sent_norm = normalize_regime_sentiment(sent_raw)
            vol_score = float(score_fn(window))
            vol_label = get_volatility(window, interval="1d")
            full = get_market_regime(sent_raw, vol_label, apply_hysteresis=True)
            rows.append(
                {
                    "day": day,
                    "rhyme_label": _letter(full),
                    "rhyme_full": full,
                    "vol_score": vol_score,
                    "vol_label": vol_label,
                    "vol_high": vol_score > thresh,
                    "sentiment_raw": float(sent_raw),
                    "sentiment_norm": float(sent_norm),
                    "score_impl": "FIXED" if use_fixed else "BUGGY",
                }
            )
    finally:
        market_context.cross_asset_vol_score = original
        market_context.announce_regime_change = announce
        reset_regime_hysteresis()

    return pd.DataFrame(rows)


def diff_labels(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    merged = old.merge(new, on="day", suffixes=("_old", "_new"))
    merged["flipped"] = merged["rhyme_label_old"] != merged["rhyme_label_new"]
    merged["flipped_into_AB"] = (
        merged["flipped"]
        & merged["rhyme_label_new"].isin(["A", "B"])
        & ~merged["rhyme_label_old"].isin(["A", "B"])
    )
    merged["flipped_out_of_AB"] = (
        merged["flipped"]
        & merged["rhyme_label_old"].isin(["A", "B"])
        & ~merged["rhyme_label_new"].isin(["A", "B"])
    )
    return merged


def confusion_matrix(diff: pd.DataFrame) -> pd.DataFrame:
    return pd.crosstab(
        diff["rhyme_label_old"],
        diff["rhyme_label_new"],
        margins=True,
        margins_name="TOTAL",
    )


def write_gate_crossref_note(out_dir: Path, into_ab: pd.DataFrame) -> Path:
    """
    Gates are code locations, not dated events. Write a checklist pointing
    at rhyme_gate_audit.md GATE modules for manual log cross-check.
    """
    path = out_dir / "rhyme_shadow_gate_crossref_NOTE.md"
    audit = out_dir / "rhyme_gate_audit.md"
    lines = [
        "# Shadow A/B days × GATE cross-reference (manual)",
        "",
        "Gate hits from `rhyme_gate_audit.py` are **code locations**, not calendar",
        "events. This note lists days that would flip INTO A/B under FIXED vol",
        "score — check live/paper logs / journals on these dates for whether",
        "`crypto_vol_gate`, `paper_risk_controls`, protective shorts, etc. ran.",
        "",
        f"Audit report: `{audit.name if audit.is_file() else 'rhyme_gate_audit.md (run audit first)'}`",
        "",
        "## Days flipped INTO A/B (FIXED vs BUGGY)",
        "",
    ]
    if into_ab.empty:
        lines.append("(none)")
    else:
        lines.append("| day | old | new | vol_score_new | sentiment_norm_new |")
        lines.append("|-----|-----|-----|--------------:|-------------------:|")
        for _, r in into_ab.iterrows():
            lines.append(
                f"| {r['day']} | {r['rhyme_label_old']} | {r['rhyme_label_new']} | "
                f"{r.get('vol_score_new', float('nan')):.5f} | "
                f"{r.get('sentiment_norm_new', float('nan')):.4f} |"
            )
    lines += [
        "",
        "## Priority GATE modules to check against those days",
        "",
        "- `modules/crypto_vol_gate.py` — blocks crypto on RHYME_B",
        "- `modules/paper_risk_controls.py` — B risk mult / sleeve cap trim",
        "- `modules/pipeline_strategies.py` — PAUSED_REGIMES, protective shorts B-path",
        "- `config.py` — yield override / cash buffer / PAPER_REGIME_B_* sizing",
        "- `modules/insider_signal_handler.py` — B bullish/short mults",
        "- `modules/opportunistic_short_sleeve.py` — B-conditioned shorts",
        "",
        "Freeze unchanged. No production wiring.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    end = dt.date.today()
    start = end - dt.timedelta(days=900)

    print(f"Window: {start} -> {end}")
    print("Computing labels with BUGGY score (current production behavior)...")
    old_labels = compute_labels_for_walk(start, end, use_fixed=False)
    print(f"  {len(old_labels)} days")

    print("Computing labels with FIXED score (shadow)...")
    new_labels = compute_labels_for_walk(start, end, use_fixed=True)
    print(f"  {len(new_labels)} days")

    diff = diff_labels(old_labels, new_labels)

    out_dir = HERE / "output"
    out_dir.mkdir(exist_ok=True)
    diff_path = out_dir / "rhyme_shadow_diff_900d.csv"
    diff.to_csv(diff_path, index=False)

    cm = confusion_matrix(diff)
    print("\nConfusion matrix (rows=old/BUGGY label, cols=new/FIXED label):")
    print(cm.to_string())
    cm.to_csv(out_dir / "rhyme_shadow_confusion_900d.csv")

    n = len(diff)
    n_flipped = int(diff["flipped"].sum()) if n else 0
    n_into_ab = int(diff["flipped_into_AB"].sum()) if n else 0
    n_out_ab = int(diff["flipped_out_of_AB"].sum()) if n else 0

    print(f"\nTotal days: {n}")
    print(f"Flipped label: {n_flipped} ({(n_flipped / n * 100) if n else 0:.1f}%)")
    print(f"Flipped INTO A/B: {n_into_ab}")
    print(f"Flipped OUT OF A/B: {n_out_ab}")

    into_ab = diff[diff["flipped_into_AB"]] if n else diff
    if n_into_ab > 0:
        print("\nDays that would now be A/B:")
        cols = [
            "day",
            "rhyme_label_old",
            "rhyme_label_new",
            "vol_score_old",
            "vol_score_new",
            "sentiment_norm_new",
        ]
        print(into_ab[cols].to_string(index=False))

    # Letter counts both sides
    print("\nBUGGY letter counts:", old_labels["rhyme_label"].value_counts().to_dict())
    print("FIXED letter counts:", new_labels["rhyme_label"].value_counts().to_dict())

    note = write_gate_crossref_note(out_dir, into_ab)
    print(f"\nFull diff written to: {diff_path}")
    print(f"Gate crossref checklist: {note}")
    print("\nNEXT: cross-reference flipped_into_AB days against rhyme_gate_audit.md's")
    print("GATE-classified hits and any live/paper run logs.")
    print("Research only -- freeze unchanged. market_context restored after shadow.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
