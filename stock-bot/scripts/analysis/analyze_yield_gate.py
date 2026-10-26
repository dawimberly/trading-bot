"""Analyze Yield Gate: theoretical vs effective (paper override) behavior.

Writes scripts/analysis/yield_gate_analysis.txt with pause frequency,
implied SPY-block rate, and tuning suggestions.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import config
from modules.macro_signals import bond_stress, load_daily_matrix, yield_gate_blocks
from modules.market_context import get_market_regime, get_price_sentiment, get_volatility


def _rolling_gate_series(daily: pd.DataFrame) -> pd.DataFrame:
    rows = []
    # Need 50 bars for MA + 6 for 5d rise check
    for i in range(55, len(daily)):
        window = daily.iloc[: i + 1]
        raw = bool(yield_gate_blocks(window))
        bond = bool(bond_stress(window))
        try:
            sentiment = get_price_sentiment(window)
            vol = get_volatility(window)
            regime = get_market_regime(sentiment, vol, apply_hysteresis=False)
        except Exception:
            regime = ""
        hard = config._yield_gate_hard_regime(regime)
        # Mirror effective_yield_gate with paper override on
        if not raw:
            effective_paper = False
        elif hard:
            effective_paper = True
        else:
            effective_paper = False  # PAPER_YIELD_GATE_OVERRIDE softens mild stress
        effective_live = raw  # live has no soft override
        rows.append(
            {
                "date": daily.index[i],
                "raw": raw,
                "bond_stress": bond,
                "regime": regime,
                "hard_regime": hard,
                "effective_paper": effective_paper,
                "effective_live": effective_live,
            }
        )
    return pd.DataFrame(rows)


def _pct(n: int, d: int) -> float:
    return (100.0 * n / d) if d else 0.0


def main() -> int:
    config.enforce_realistic_research_profile()
    daily = load_daily_matrix(days=420, refresh=False)
    if daily.empty or len(daily) < 80:
        print("Insufficient macro daily data for yield-gate analysis")
        return 1

    # Focus last ~90 and ~365 trading days of gate evaluations
    full = _rolling_gate_series(daily)
    windows = {
        "90d": full.tail(90),
        "365d": full.tail(252),
        "full": full,
    }

    lines: list[str] = []
    lines.append("=== Yield Gate Analysis ===")
    lines.append("")
    lines.append("Theory (modules/macro_signals.yield_gate_blocks):")
    lines.append("  - Primary: TNX > MA50 AND TNX > TNX[t-5]  (10Y rising above trend)")
    lines.append("  - Fallback: TLT below MA50 (bond_stress) when TNX unavailable")
    lines.append("  - Live: raw gate blocks SPY / NYSE / sleeves when True")
    lines.append(
        "  - Paper (PAPER_YIELD_GATE_OVERRIDE=true): raw gate only blocks in "
        "RHYME_B/E (panic/bear); mild rate stress is softened"
    )
    lines.append("")
    lines.append(
        f"Config: YIELD_GATE_ENABLED={config.YIELD_GATE_ENABLED} | "
        f"PAPER_YIELD_GATE_OVERRIDE={config.PAPER_YIELD_GATE_OVERRIDE} | "
        f"GAME_PLAN_YIELD_GATE_ONLY={config.GAME_PLAN_YIELD_GATE_ONLY}"
    )
    lines.append(f"Sample bars evaluated: {len(full)} | data end={daily.index[-1].date()}")
    lines.append("")

    for label, df in windows.items():
        n = len(df)
        raw_n = int(df["raw"].sum())
        paper_n = int(df["effective_paper"].sum())
        live_n = int(df["effective_live"].sum())
        bond_n = int(df["bond_stress"].sum())
        hard_n = int(df["hard_regime"].sum())
        soft_only = int((df["raw"] & ~df["effective_paper"]).sum())
        lines.append(f"--- Window {label} (n={n}) ---")
        lines.append(
            f"  Raw gate ON:          {raw_n:4d}  ({_pct(raw_n, n):5.1f}% of days)"
        )
        lines.append(
            f"  Bond stress ON:       {bond_n:4d}  ({_pct(bond_n, n):5.1f}%)"
        )
        lines.append(
            f"  Hard regime days:     {hard_n:4d}  ({_pct(hard_n, n):5.1f}%)"
        )
        lines.append(
            f"  Effective LIVE block: {live_n:4d}  ({_pct(live_n, n):5.1f}%)  "
            f"[≈ SPY new-buy block rate]"
        )
        lines.append(
            f"  Effective PAPER block:{paper_n:4d}  ({_pct(paper_n, n):5.1f}%)  "
            f"[override softens {_pct(soft_only, n):.1f}% of days]"
        )
        lines.append("")

    # Recent regime when raw gate was on
    recent = windows["90d"]
    if recent["raw"].any():
        lines.append("90d raw-gate days by regime:")
        counts = recent.loc[recent["raw"], "regime"].value_counts()
        for reg, cnt in counts.items():
            lines.append(f"  {cnt:3d}  {reg}")
        lines.append("")

    # Tuning suggestion
    paper_90 = _pct(int(windows["90d"]["effective_paper"].sum()), len(windows["90d"]))
    live_90 = _pct(int(windows["90d"]["effective_live"].sum()), len(windows["90d"]))
    raw_90 = _pct(int(windows["90d"]["raw"].sum()), len(windows["90d"]))
    lines.append("=== Assessment ===")
    if raw_90 >= 40:
        lines.append(
            f"Raw gate is ON {_pct(int(windows['90d']['raw'].sum()), len(windows['90d'])):.0f}% "
            "of recent days — fairly active / potentially over-conservative for live."
        )
    elif raw_90 >= 20:
        lines.append(
            f"Raw gate fires on ~{raw_90:.0f}% of recent days — moderate. "
            "Paper override keeps most of those open."
        )
    else:
        lines.append(
            f"Raw gate is relatively quiet recently (~{raw_90:.0f}% of days)."
        )
    lines.append(
        f"Paper effective block ~{paper_90:.1f}% vs live ~{live_90:.1f}% "
        f"(override removes ~{live_90 - paper_90:.1f}pp of blocks)."
    )
    lines.append("")
    lines.append("Tuning suggestions (if over-conservative):")
    lines.append(
        "  1. Keep PAPER_YIELD_GATE_OVERRIDE=true (already softens mild stress) — DONE for paper."
    )
    lines.append(
        "  2. Require larger TNX rise: e.g. TNX > MA50 * 1.02 OR rise over 10 bars "
        "instead of 5 (reduces whipsaw)."
    )
    lines.append(
        "  3. Live-only: add LIVE_YIELD_GATE_SOFT=true to mirror paper soft-override "
        "outside RHYME_B/E (optional; currently live stays hard)."
    )
    lines.append(
        "  4. Do NOT disable YIELD_GATE_ENABLED entirely — historical yield_gate_only "
        "A/B improved max DD vs baseline (see README game-plan table)."
    )
    lines.append("")

    # Scan recent logs for yield_gate chatter
    log_hits = 0
    log_dirs = [
        ROOT / "logs",
        ROOT / "scripts" / "analysis",
    ]
    for d in log_dirs:
        if not d.is_dir():
            continue
        for path in list(d.glob("*.log"))[-20:] + list(d.glob("*365*.txt"))[-10:]:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            log_hits += text.lower().count("yield_gate")
    lines.append(f"Log scan: {log_hits} 'yield_gate' mentions in recent analysis/logs files.")
    alert = ROOT / "alert_state.json"
    if alert.is_file():
        try:
            import json

            st = json.loads(alert.read_text(encoding="utf-8"))
            lines.append(
                f"Live alert_state yield_gate_active={st.get('yield_gate_active')}"
            )
        except Exception:
            pass

    out = ROOT / "scripts" / "analysis" / "yield_gate_analysis.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
