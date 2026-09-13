"""
RHYME volatility classifier audit (research sidecar).

Answers: why A/B never fire on 365d/900d walks — threshold design vs
genuinely calm data. Market-wide implication: every sleeve that gates on
RHYME A/B inherits this.

Does NOT change config or live/paper. Writes a report under
scripts/research/sneaky pivot/output/.
"""

from __future__ import annotations

import datetime as dt
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import modules.market_context as mc  # noqa: E402
from modules.data_loader import load_close_matrix  # noqa: E402
from modules.market_context import (  # noqa: E402
    cross_asset_vol_score,
    get_market_regime,
    get_price_sentiment,
    get_volatility,
    normalize_regime_sentiment,
    regime_vol_threshold,
    reset_regime_hysteresis,
    set_regime_bar_index,
)

OUTPUT_DIR = HERE / "output"
OUTPUT_DIR.mkdir(exist_ok=True)


def _letter(full: str) -> str:
    if full.startswith("RHYME_"):
        return full.split(":")[0].replace("RHYME_", "")
    return full


def walk_matrix(days: int = 900) -> pd.DataFrame:
    matrix = load_close_matrix(interval="1d", days=days)
    if matrix is None or matrix.empty or len(matrix) < 30:
        raise RuntimeError("load_close_matrix returned insufficient daily data")

    matrix = matrix.copy()
    matrix.index = pd.to_datetime(matrix.index)
    thresh = float(regime_vol_threshold("1d"))
    warmup = 20

    reset_regime_hysteresis()
    announce = mc.announce_regime_change
    mc.announce_regime_change = lambda r: r

    rows = []
    try:
        for i in range(warmup, len(matrix)):
            set_regime_bar_index(i)
            window = matrix.iloc[: i + 1]
            # Also compute short rolling vol (20d) for comparison — NOT used by live classifier
            recent = matrix.iloc[max(0, i - 19) : i + 1]
            score_full = cross_asset_vol_score(window)
            score_roll20 = cross_asset_vol_score(recent)
            vol_label = "High" if score_full > thresh else "Low"
            # Counterfactual: High if rolling-20 exceeded same fixed thresh
            vol_roll_label = "High" if score_roll20 > thresh else "Low"
            sent_raw = get_price_sentiment(window)
            sent_norm = normalize_regime_sentiment(sent_raw)
            full = get_market_regime(
                sent_raw, get_volatility(window, interval="1d"), apply_hysteresis=True
            )
            day = pd.Timestamp(matrix.index[i]).date()
            rows.append(
                {
                    "day": day,
                    "n_bars_in_window": i + 1,
                    "vol_score_expanding": score_full,
                    "vol_score_roll20": score_roll20,
                    "vol_threshold_daily": thresh,
                    "vol_label_expanding": vol_label,
                    "vol_label_if_roll20": vol_roll_label,
                    "above_thresh_expanding": score_full > thresh,
                    "above_thresh_roll20": score_roll20 > thresh,
                    "sentiment_raw": sent_raw,
                    "sentiment_norm": sent_norm,
                    "rhyme": _letter(full),
                    "rhyme_full": full,
                }
            )
    finally:
        mc.announce_regime_change = announce
        reset_regime_hysteresis()

    return pd.DataFrame(rows)


def summarize(df: pd.DataFrame) -> str:
    thresh = float(df["vol_threshold_daily"].iloc[0])
    s = df["vol_score_expanding"]
    r = df["vol_score_roll20"]
    lines = [
        "# RHYME volatility classifier audit",
        "",
        f"Generated: {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "**Research only — freeze unchanged. Market-wide finding, not Sneaky-Pivot-only.**",
        "",
        "## How High/Low is defined (current code)",
        "",
        "```",
        "cross_asset_vol_score(data) = mean over assets of stdev(pct_change of FULL window)",
        "get_volatility -> 'High' if score > REGIME_VOL_THRESHOLD_DAILY else 'Low'",
        "```",
        "",
        f"- `REGIME_VOL_THRESHOLD_DAILY` = **{config.REGIME_VOL_THRESHOLD_DAILY}** "
        f"(env overrideable; default 0.02)",
        f"- `REGIME_VOL_THRESHOLD_5M` = {config.REGIME_VOL_THRESHOLD_5M} (live 5m path)",
        f"- Threshold type: **fixed absolute cutoff** — not a rolling percentile, "
        f"not adapting to recent vol regime",
        f"- Score type: **expanding-window full-history stdev** — stress spikes are "
        f"diluted as `n_bars` grows; hysteresis does NOT change the High/Low cut "
        f"(it only sticky-holds the final A–E letter after classify)",
        "",
        "## Sentiment side (for A/B need High vol + extreme sentiment)",
        "",
        f"- `REGIME_SENTIMENT_THRESHOLD` = {config.REGIME_SENTIMENT_THRESHOLD}",
        f"- `REGIME_RAW_SENTIMENT_MAX` (normalize band) = {config.REGIME_RAW_SENTIMENT_MAX}",
        f"- Hysteresis enabled = {config.REGIME_HYSTERESIS_ENABLED}, "
        f"dwell bars = {config.REGIME_MIN_DWELL_BARS}",
        "",
        "## Expanding-window vol score distribution (classifier input)",
        "",
        f"| Stat | Value |",
        f"|------|------:|",
        f"| n days | {len(df)} |",
        f"| min | {s.min():.5f} |",
        f"| p10 | {s.quantile(0.10):.5f} |",
        f"| p25 | {s.quantile(0.25):.5f} |",
        f"| p50 | {s.quantile(0.50):.5f} |",
        f"| p75 | {s.quantile(0.75):.5f} |",
        f"| p90 | {s.quantile(0.90):.5f} |",
        f"| p95 | {s.quantile(0.95):.5f} |",
        f"| p99 | {s.quantile(0.99):.5f} |",
        f"| max | {s.max():.5f} |",
        f"| threshold | {thresh:.5f} |",
        f"| days above thresh | {int(df['above_thresh_expanding'].sum())} "
        f"({100*df['above_thresh_expanding'].mean():.2f}%) |",
        f"| max / threshold | {s.max()/thresh:.3f}x |",
        "",
        "## Counterfactual: same fixed thresh on 20d rolling score",
        "",
        f"| Stat | Value |",
        f"|------|------:|",
        f"| roll20 max | {r.max():.5f} |",
        f"| roll20 p95 | {r.quantile(0.95):.5f} |",
        f"| days roll20 > thresh | {int(df['above_thresh_roll20'].sum())} "
        f"({100*df['above_thresh_roll20'].mean():.2f}%) |",
        "",
        "If rolling-20 clears the thresh often but expanding never does, the issue is "
        "**dilution / expanding-window design**, not 'markets were calm.'",
        "",
        "## RHYME letter counts (actual classifier walk)",
        "",
    ]
    counts = Counter(df["rhyme"])
    lines.append("| Letter | Days |")
    lines.append("|--------|-----:|")
    for k in "ABCDE":
        lines.append(f"| {k} | {counts.get(k, 0)} |")
    lines.append("")

    # Top expanding spikes (closest to / farthest under thresh)
    top = df.nlargest(15, "vol_score_expanding")[
        ["day", "n_bars_in_window", "vol_score_expanding", "vol_score_roll20", "rhyme"]
    ]
    lines += [
        "## Highest expanding vol-score days (closest approaches to High)",
        "",
        top.to_string(index=False),
        "",
    ]
    top_r = df.nlargest(15, "vol_score_roll20")[
        ["day", "vol_score_expanding", "vol_score_roll20", "vol_label_if_roll20", "rhyme"]
    ]
    lines += [
        "## Highest 20d-rolling vol-score days (stress the expanding score missed)",
        "",
        top_r.to_string(index=False),
        "",
    ]

    # Known stress windows if present in index
    candidates = {
        "Aug 2024 yen-carry (approx)": (dt.date(2024, 8, 1), dt.date(2024, 8, 10)),
        "Apr 2025 tariff selloff (approx)": (dt.date(2025, 4, 1), dt.date(2025, 4, 15)),
    }
    lines.append("## Known stress windows in this walk (if covered)")
    lines.append("")
    for name, (a, b) in candidates.items():
        chunk = df[(df["day"] >= a) & (df["day"] <= b)]
        if chunk.empty:
            lines.append(f"- **{name}**: not in loaded matrix")
            continue
        lines.append(
            f"- **{name}**: n={len(chunk)}, "
            f"expanding max={chunk['vol_score_expanding'].max():.5f}, "
            f"roll20 max={chunk['vol_score_roll20'].max():.5f}, "
            f"expanding High days={int(chunk['above_thresh_expanding'].sum())}, "
            f"roll20 High days={int(chunk['above_thresh_roll20'].sum())}, "
            f"letters={dict(Counter(chunk['rhyme']))}"
        )
    lines += [
        "",
        "## Verdict",
        "",
    ]
    n_high = int(df["above_thresh_expanding"].sum())
    n_roll_high = int(df["above_thresh_roll20"].sum())
    if n_high == 0 and n_roll_high > 0:
        lines.append(
            f"**Threshold/design problem (not 'no stress occurred').** "
            f"Expanding-window score never cleared {thresh} ({s.max():.5f} max = "
            f"{s.max()/thresh:.0%} of thresh), but a 20d rolling score would have "
            f"labeled High on **{n_roll_high}** days under the same absolute cut. "
            f"A/B are structurally rare/impossible on long expanding walks."
        )
    elif n_high == 0 and n_roll_high == 0:
        lines.append(
            f"**Possibly calm under this metric + thresh.** Neither expanding nor "
            f"roll20 cleared {thresh}. Still check whether 0.02 is too high for "
            f"cross-asset mean daily stdev (max roll20={r.max():.5f})."
        )
    else:
        lines.append(
            f"Expanding High fired on {n_high} days. Investigate sentiment side "
            f"if A/B still absent (need High + extreme sentiment)."
        )
    lines += [
        "",
        "## Market-wide implication",
        "",
        "This is not limited to Sneaky Pivot research. Live/`run_all`, "
        "`compute_regime_breakdown`, sleeve gates on RHYME_A/B, and any "
        "paper profile that assumes panic/euphoria regimes can fire — all share "
        "`get_volatility` + fixed `REGIME_VOL_THRESHOLD_DAILY`. Do **not** patch "
        "only the sneaky-pivot harness; any fix belongs in `market_context` / "
        "config after an explicit design decision.",
        "",
        "Freeze unchanged. No config edits in this audit.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    print("Walking ~900d daily close matrix through RHYME vol path...")
    df = walk_matrix(900)
    csv_path = OUTPUT_DIR / "rhyme_vol_audit_900d.csv"
    df.to_csv(csv_path, index=False)
    report = summarize(df)
    md_path = OUTPUT_DIR / "rhyme_vol_audit_900d.md"
    md_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"\nWrote {md_path}")
    print(f"Wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
