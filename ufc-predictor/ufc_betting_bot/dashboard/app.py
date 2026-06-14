"""Streamlit dashboard — backtest results, bankroll rules, live signals."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from ufc_betting_bot.config.settings import (
    BANKROLL_STATE_PATH,
    LIVE_SIGNALS_CSV,
    PLOTS_DIR,
    REPORTS_DIR,
    get_settings,
)
from ufc_betting_bot.live_runner.runner import run_live_dry_run

st.set_page_config(page_title="UFC Betting Bot", layout="wide")
settings = get_settings()

st.title("UFC Betting Bot")
st.caption("Separate from crypto trading bot · uses ufc-predictor model")

with st.sidebar:
    st.header("Bankroll rules")
    b = settings.bankroll
    st.metric("Bankroll", f"${b.initial_bankroll:,.0f}")
    st.write(f"Kelly fraction: **{b.kelly_fraction:.0%}** (fractional)")
    st.write(f"Max bet: **{b.max_bet_fraction:.1%}** of bankroll")
    st.write(f"Daily loss limit: **{b.daily_loss_limit_fraction:.1%}**")
    st.write(f"Min edge: **{b.min_edge:.1%}**")

tab_bt, tab_live, tab_state = st.tabs(["Backtest 2025", "Live dry-run", "Bankroll state"])

with tab_bt:
    summary_path = REPORTS_DIR / f"backtest_{settings.backtest_year}_summary.json"
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Accuracy", f"{summary.get('overall_accuracy', 0):.1%}")
        c2.metric("Fights", int(summary.get("overall_n_fights", 0)))
        c3.metric("w/ Odds", int(summary.get("fights_with_odds", 0)))
        c4.metric("Kelly ROI", f"{summary.get('bankroll_roi_pct', 0):.1f}%")

        sweep_path = REPORTS_DIR / f"backtest_{settings.backtest_year}_bankroll_sweep.csv"
        if sweep_path.is_file():
            sweep = pd.read_csv(sweep_path)
            st.subheader("Fractional Kelly ROI sweep")
            st.dataframe(sweep, use_container_width=True)
            st.line_chart(sweep.set_index("min_edge")["roi_pct"])

        cal_png = PLOTS_DIR / f"calibration_{settings.backtest_year}.png"
        roi_png = PLOTS_DIR / f"bankroll_roi_{settings.backtest_year}.png"
        if cal_png.is_file():
            st.image(str(cal_png), caption="Calibration")
        if roi_png.is_file():
            st.image(str(roi_png), caption="ROI by edge threshold")
    else:
        st.info("No backtest yet. Run: `python main.py --backtest-2025`")

    if st.button("Run 2025 backtest"):
        with st.spinner("Running walk-forward backtest…"):
            from ufc_betting_bot.backtester import backtest_2025, print_backtest_summary

            result = backtest_2025()
            print_backtest_summary(result)
        st.success("Backtest complete. Refresh page.")
        st.rerun()

with tab_live:
    if LIVE_SIGNALS_CSV.is_file():
        st.dataframe(pd.read_csv(LIVE_SIGNALS_CSV), use_container_width=True)
    if st.button("Generate live signals (dry-run)"):
        with st.spinner("Fetching card + odds…"):
            df = run_live_dry_run()
        st.dataframe(df, use_container_width=True)

with tab_state:
    if BANKROLL_STATE_PATH.is_file():
        st.json(json.loads(BANKROLL_STATE_PATH.read_text(encoding="utf-8")))
    else:
        st.write("No persisted bankroll state yet.")
