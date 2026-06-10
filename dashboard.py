"""Streamlit dashboard for PythonTrading bot status, positions, journal, wisdom, and price charts."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st

import config
from modules.alpaca_executor import get_trading_client
from nerdminer import config as nm_config
from nerdminer.monitor import assess_health, load_history, load_state

REFRESH_SECONDS = 60
CHART_DAYS = 30
CHART_HEIGHT_COMPACT = 260
CHART_HEIGHT_FULL = 420
CRYPTO_VOL_HEARTBEAT_FILE = "crypto_vol_heartbeat.json"
PROJECT_ROOT = Path(__file__).resolve().parent

LIVE_WARNING_HTML = """
<div class="live-trading-banner">
  ⚠️ LIVE TRADING — REAL MONEY ACCOUNT ⚠️
  <div class="live-trading-sub">Connected to your live Alpaca account. Orders use real funds.</div>
</div>
"""


def _resolve_path(relative: str) -> Path:
    return PROJECT_ROOT / relative


def _inject_dashboard_css() -> None:
    st.markdown(
        """
        <style>
        .live-trading-banner {
            background: linear-gradient(135deg, #7f0000 0%, #b91c1c 55%, #991b1b 100%);
            color: #fff;
            padding: 1.1rem 1rem;
            border-radius: 10px;
            text-align: center;
            font-size: 1.45rem;
            font-weight: 800;
            letter-spacing: 0.03em;
            margin-bottom: 0.75rem;
            border: 2px solid #fecaca;
            box-shadow: 0 4px 14px rgba(127, 0, 0, 0.35);
        }
        .live-trading-sub {
            font-size: 0.95rem;
            font-weight: 500;
            margin-top: 0.35rem;
            opacity: 0.95;
        }
        .metric-calm [data-testid="stMetricValue"] {
            font-size: 1.35rem;
        }
        @media (max-width: 768px) {
            .block-container { padding-top: 1rem; padding-left: 0.75rem; padding-right: 0.75rem; }
            h1 { font-size: 1.45rem !important; }
            .live-trading-banner { font-size: 1.1rem; padding: 0.85rem 0.65rem; }
            .metric-calm [data-testid="stMetricValue"] { font-size: 1.1rem; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _display_equity(
    heartbeat: dict | None, acct_eq: float | None, acct_cash: float | None
) -> tuple[float, float]:
    hb_eq = float((heartbeat or {}).get("equity") or 0)
    hb_cash = float((heartbeat or {}).get("cash") or 0)
    equity = acct_eq if acct_eq is not None else hb_eq
    cash = acct_cash if acct_cash is not None else hb_cash
    return equity, cash


def _market_open_countdown(heartbeat: dict | None) -> str:
    scan = (heartbeat or {}).get("scan_schedule") or {}
    if scan.get("market_open"):
        return "Market open"
    session_open = scan.get("session_open") or scan.get("orders_start")
    if not session_open:
        return "—"
    try:
        open_dt = pd.Timestamp(session_open)
        now = pd.Timestamp.now(tz=open_dt.tz)
        delta = open_dt - now
        secs = delta.total_seconds()
        if secs <= 0:
            return "Opening soon"
        hours = int(secs // 3600)
        mins = int((secs % 3600) // 60)
        if hours >= 24:
            days = hours // 24
            hours = hours % 24
            return f"Opens in {days}d {hours}h"
        return f"Opens in {hours}h {mins}m"
    except Exception:
        return str(session_open)


def _total_unrealized_pnl(positions_df: pd.DataFrame | None) -> float:
    if positions_df is None or positions_df.empty:
        return 0.0
    return float(positions_df["Unrealized P&L ($)"].sum())


def _active_exposure_pct(heartbeat: dict | None, equity: float) -> float:
    if equity <= 0:
        return 0.0
    exposure = (heartbeat or {}).get("sleeve_exposure") or {}
    if exposure:
        invested = sum(
            float(exposure.get(f"{key}_value") or 0)
            for key in ("vti_core", "spy", "crypto", "nyse", "metal")
        )
        if invested > 0:
            return invested / equity * 100
    cash = float((heartbeat or {}).get("cash") or 0)
    return max(0.0, (equity - cash) / equity * 100)


def _expected_next_actions(heartbeat: dict | None) -> list[str]:
    if heartbeat is None:
        return ["Start `run_all.py` to begin trading cycles."]

    actions: list[str] = []
    if heartbeat.get("halted"):
        actions.append("Risk halt active — no new entries until drawdown recovers.")

    wisdom = heartbeat.get("wisdom") or {}
    if wisdom.get("paused"):
        actions.append("Wisdom paused — new entries blocked in this regime.")

    gp_state = heartbeat.get("game_plan_state") or {}
    signals = gp_state.get("signals") or {}
    if signals.get("yield_gate"):
        actions.append("Yield gate active — hostile-rate SPY entries blocked.")
    elif (heartbeat.get("game_plan") or {}).get("yield_gate_only"):
        actions.append("Yield gate armed (yield-gate-only game plan).")

    exposure = heartbeat.get("sleeve_exposure") or {}
    vti_val = float(exposure.get("vti_core_value") or 0)
    vti_cap = float(exposure.get("vti_core_cap") or 0)
    vti_target = float((heartbeat.get("sleeve_caps") or {}).get("vti_core") or 0)
    scan = heartbeat.get("scan_schedule") or {}

    if vti_target > 0 and vti_cap > 0:
        util = vti_val / vti_cap if vti_cap else 0.0
        if util < 0.95:
            target_label = f"{vti_target:.0%}"
            if scan.get("market_open"):
                actions.append(
                    f"VTI under target ({util:.0%} of cap) — rebalance toward {target_label} likely."
                )
            else:
                actions.append(
                    f"Will buy VTI toward {target_label} core on next market open."
                )
        else:
            actions.append(f"VTI core on target (~{vti_target:.0%} of equity).")

    if scan.get("market_open"):
        actions.append("Equity session open — SPY, NYSE, and VTI sleeves active.")
    else:
        phase = scan.get("phase", "closed")
        countdown = _market_open_countdown(heartbeat)
        if phase == "overnight":
            actions.append(f"Overnight phase — crypto scans only. Next equity session: {countdown}.")
        else:
            actions.append(f"Equity session closed — {countdown}.")

    macro = heartbeat.get("macro_event") or {}
    if macro.get("active") and macro.get("event"):
        ev = macro["event"]
        actions.append(
            f"Macro event guard: {ev.get('name')} — sizing x{macro.get('sizing_scale', 1):.2f}."
        )

    if heartbeat.get("crypto_vol_only"):
        ipo = heartbeat.get("spacex_ipo") or {}
        if not ipo.get("crypto_allowed", True):
            actions.append("Crypto entries gated — volatility regime too low.")

    return actions or ["Monitoring — no immediate actions flagged."]


def _render_key_metrics_row(
    heartbeat: dict | None,
    positions_df: pd.DataFrame | None,
    equity: float,
    cash: float,
) -> None:
    st.markdown('<div class="metric-calm">', unsafe_allow_html=True)
    cash_pct = (cash / equity * 100) if equity > 0 else 0.0
    exposure_pct = _active_exposure_pct(heartbeat, equity)
    upl = _total_unrealized_pnl(positions_df)
    market_label = _market_open_countdown(heartbeat)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Equity", f"${equity:,.2f}")
    c2.metric("Cash", f"{cash_pct:.1f}%")
    c3.metric("Invested", f"{exposure_pct:.1f}%")
    c4.metric("Unrealized P&L", f"${upl:+,.2f}")
    c5.metric("Next market", market_label)
    st.markdown("</div>", unsafe_allow_html=True)


def _render_expected_actions(heartbeat: dict | None) -> None:
    st.subheader("Expected next actions")
    for line in _expected_next_actions(heartbeat):
        st.info(line)


def _render_vti_core_section(heartbeat: dict) -> None:
    exposure = heartbeat.get("sleeve_exposure") or {}
    caps = heartbeat.get("sleeve_caps") or {}
    equity = float(exposure.get("equity") or heartbeat.get("equity") or 0)
    vti_val = float(exposure.get("vti_core_value") or 0)
    vti_cap = float(exposure.get("vti_core_cap") or 0)
    vti_target_pct = float(caps.get("vti_core") or 0) * 100
    vti_eq_pct = (vti_val / equity * 100) if equity > 0 else 0.0
    util = (vti_val / vti_cap * 100) if vti_cap > 0 else 0.0

    st.subheader(f"VTI core ({config.VTI_CORE_SYMBOL})")
    m1, m2, m3 = st.columns(3)
    m1.metric("Target", f"{vti_target_pct:.0f}%")
    m2.metric("Current", f"{vti_eq_pct:.1f}% of equity")
    m3.metric("Fill", f"{util:.0f}% of cap")

    target_frac = min(max(vti_eq_pct / vti_target_pct, 0.0), 1.0) if vti_target_pct > 0 else 0.0
    st.progress(
        target_frac,
        text=f"${vti_val:,.2f} / ${vti_cap:,.2f} target",
    )


def _render_performance_summary_prominent(scorecard: dict, *, compact: bool) -> None:
    st.subheader("Performance snapshot")
    live = scorecard.get("live") or {}
    sharpe = float(live.get("sharpe") or 0)
    live_ret = float(live.get("return_pct") or 0)
    vs_sim = scorecard.get("live_vs_active_sim_return_pp")
    if vs_sim is None:
        vs_sim = scorecard.get("live_vs_best_sim_return_pp")
    window = scorecard.get("window_days", "—")
    evaluated = scorecard.get("evaluated_at", "—")

    if compact:
        c1, c2, c3 = st.columns(3)
        c1.metric("Live Sharpe", f"{sharpe:.2f}")
        c2.metric(f"Live return ({window}d)", f"{live_ret:+.2f}%")
        c3.metric("vs Active sim", f"{float(vs_sim or 0):+.2f} pp")
        st.caption(f"Scorecard evaluated {evaluated}")
    else:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Live Sharpe", f"{sharpe:.2f}")
        c2.metric("Live return", f"{live_ret:+.2f}%")
        c3.metric("vs Active sim", f"{float(vs_sim or 0):+.2f} pp")
        c4.metric("Max drawdown", f"{float(live.get('max_drawdown_pct') or 0):+.2f}%")
        st.caption(f"Window: {window} days | Evaluated: {evaluated}")

    rec = scorecard.get("recommendation")
    if rec:
        st.caption(f"Recommendation: {rec}")


def _load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _infer_sleeve(symbol: str) -> str:
    sym = config.normalize_symbol(symbol or "")
    if sym == config.SPY_BOT_SYMBOL:
        return "SPY"
    if config.is_crypto(sym):
        return "Crypto"
    if config.is_metal_symbol(sym):
        return "Metal"
    if sym:
        return "NYSE"
    return ""


def _active_sleeves(heartbeat: dict) -> list[str]:
    exposure = heartbeat.get("sleeve_exposure") or {}
    active: list[str] = []
    checks = [
        ("VTI", "vti_core_value"),
        ("SPY", "spy_value"),
        ("Crypto", "crypto_value"),
        ("NYSE", "nyse_value"),
        ("Metal", "metal_value"),
    ]
    for label, key in checks:
        if float(exposure.get(key) or 0) > 0:
            active.append(label)
    return active


def _sleeve_exposure_rows(heartbeat: dict) -> list[dict]:
    exposure = heartbeat.get("sleeve_exposure") or {}
    caps = heartbeat.get("sleeve_caps") or {}
    equity = float(exposure.get("equity") or heartbeat.get("equity") or 0)
    rows: list[dict] = []
    vti_cap_pct = float(caps.get("vti_core") or 0)
    vti_cap = float(exposure.get("vti_core_cap") or 0)
    vti_val = float(exposure.get("vti_core_value") or 0)
    if vti_cap_pct > 0 or vti_cap > 0:
        util = (vti_val / vti_cap * 100) if vti_cap > 0 else 0.0
        eq_pct = (vti_val / equity * 100) if equity > 0 else 0.0
        rows.append(
            {
                "Sleeve": "VTI Core",
                "Value ($)": vti_val,
                "Cap ($)": vti_cap,
                "Cap Target (%)": vti_cap_pct * 100,
                "Utilization (%)": util,
                "Equity (%)": eq_pct,
            }
        )
    for key, label in (
        ("spy", "SPY"),
        ("crypto", "Crypto"),
        ("nyse", "NYSE"),
        ("metal", "Metal"),
    ):
        value = float(exposure.get(f"{key}_value") or 0)
        cap = float(exposure.get(f"{key}_cap") or 0)
        cap_pct = float(caps.get(key) or 0)
        util = (value / cap * 100) if cap > 0 else 0.0
        eq_pct = (value / equity * 100) if equity > 0 else 0.0
        rows.append(
            {
                "Sleeve": label,
                "Value ($)": value,
                "Cap ($)": cap,
                "Cap Target (%)": cap_pct * 100,
                "Utilization (%)": util,
                "Equity (%)": eq_pct,
            }
        )
    cash = float(heartbeat.get("cash") or 0)
    cash_pct = float(caps.get("cash_buffer") or 0) * 100
    cash_eq = (cash / equity * 100) if equity > 0 else 0.0
    rows.append(
        {
            "Sleeve": "Cash",
            "Value ($)": cash,
            "Cap ($)": equity * float(caps.get("cash_buffer") or 0),
            "Cap Target (%)": cash_pct,
            "Utilization (%)": (cash_eq / cash_pct * 100) if cash_pct > 0 else 0.0,
            "Equity (%)": cash_eq,
        }
    )
    return rows


def _fetch_account_summary() -> tuple[float | None, float | None, str | None]:
    try:
        client = get_trading_client()
        acct = client.get_account()
        return float(acct.equity), float(acct.cash), None
    except ValueError as exc:
        return None, None, f"Alpaca credentials not configured: {exc}"
    except Exception as exc:  # noqa: BLE001
        return None, None, f"Could not fetch Alpaca account: {exc}"


def _fetch_positions() -> tuple[pd.DataFrame | None, str | None]:
    try:
        client = get_trading_client()
        positions = client.get_all_positions()
    except ValueError as exc:
        return None, f"Alpaca credentials not configured: {exc}"
    except Exception as exc:  # noqa: BLE001 — surface API errors in UI
        return None, f"Could not fetch Alpaca positions: {exc}"

    if not positions:
        return pd.DataFrame(
            columns=[
                "Ticker",
                "Qty",
                "Entry",
                "Current",
                "Unrealized P&L ($)",
                "Unrealized P&L (%)",
            ]
        ), None

    rows = []
    for pos in positions:
        entry = float(getattr(pos, "avg_entry_price", 0) or 0)
        current = float(getattr(pos, "current_price", 0) or 0)
        upl = float(getattr(pos, "unrealized_pl", 0) or 0)
        upl_pct = float(getattr(pos, "unrealized_plpc", 0) or 0) * 100
        rows.append(
            {
                "Ticker": config.normalize_symbol(pos.symbol),
                "Qty": float(pos.qty),
                "Entry": entry,
                "Current": current,
                "Unrealized P&L ($)": upl,
                "Unrealized P&L (%)": upl_pct,
            }
        )
    return pd.DataFrame(rows), None


def _style_pnl_df(df: pd.DataFrame):
    def _color_pnl(val):
        if pd.isna(val):
            return ""
        if val > 0:
            return "color: #198754; font-weight: 600"
        if val < 0:
            return "color: #dc3545; font-weight: 600"
        return ""

    styler = df.style
    for col in ("Unrealized P&L ($)", "Unrealized P&L (%)"):
        if col in df.columns:
            styler = styler.map(_color_pnl, subset=[col])
    fmt = {
        "Qty": "{:.4f}",
        "Entry": "${:,.2f}",
        "Current": "${:,.2f}",
        "Unrealized P&L ($)": "${:+,.2f}",
        "Unrealized P&L (%)": "{:+.2f}%",
    }
    for col, spec in fmt.items():
        if col in df.columns:
            styler = styler.format({col: spec})
    return styler


def _load_journal(limit: int = 20) -> pd.DataFrame | None:
    path = _resolve_path(config.PAPER_JOURNAL_CSV)
    if not path.is_file():
        return None
    try:
        df = pd.read_csv(path)
    except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
        return None
    if df.empty:
        return df
    df["sleeve"] = df["symbol"].fillna("").astype(str).map(_infer_sleeve)
    return df.tail(limit).iloc[::-1].reset_index(drop=True)


def _daily_table_name(symbol: str) -> str:
    return f"{config.normalize_symbol(symbol)}_daily"


def _load_daily_ohlcv(symbol: str, days: int = CHART_DAYS) -> pd.DataFrame | None:
    """Load last N daily bars from market_data.db ({ticker}_daily tables).

    Tables store Date + Close only; OHLC is derived for candlestick display.
    """
    table = _daily_table_name(symbol)
    db_path = _resolve_path(config.DB_PATH)
    if not db_path.is_file():
        return None

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        if cur.fetchone() is None:
            return None
        df = pd.read_sql(
            f'SELECT Date, Close FROM "{table}" ORDER BY Date DESC LIMIT ?',
            conn,
            params=(days,),
        )
    finally:
        conn.close()

    if df.empty:
        return None

    df = df.sort_values("Date").reset_index(drop=True)
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    close = pd.to_numeric(df["Close"], errors="coerce")
    open_ = close.shift(1)
    open_.iloc[0] = close.iloc[0]
    df["Open"] = open_
    df["High"] = pd.concat([open_, close], axis=1).max(axis=1)
    df["Low"] = pd.concat([open_, close], axis=1).min(axis=1)
    df["Close"] = close
    df["MA50"] = close.rolling(50, min_periods=1).mean()
    df["MA200"] = close.rolling(200, min_periods=1).mean()
    return df.dropna(subset=["Date", "Close"])


def _build_candlestick_figure(
    symbol: str, df: pd.DataFrame, *, height: int = CHART_HEIGHT_FULL
) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(
        go.Candlestick(
            x=df["Date"],
            open=df["Open"],
            high=df["High"],
            low=df["Low"],
            close=df["Close"],
            name=symbol,
        )
    )
    fig.add_trace(
        go.Scatter(
            x=df["Date"],
            y=df["MA50"],
            mode="lines",
            name="MA50",
            line=dict(color="#fd7e14", width=1.5),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=df["Date"],
            y=df["MA200"],
            mode="lines",
            name="MA200",
            line=dict(color="#0d6efd", width=1.5),
        )
    )
    fig.update_layout(
        title=f"{symbol} — last {len(df)} daily bars ({config.DB_PATH})",
        xaxis_title="Date",
        yaxis_title="Price",
        height=height,
        margin=dict(t=48, b=32),
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def render_crypto_vol_panel() -> None:
    """Small panel for the isolated crypto vol paper sleeve."""
    hb = _load_json(_resolve_path(CRYPTO_VOL_HEARTBEAT_FILE))
    with st.expander("Crypto vol sleeve (isolated paper)", expanded=bool(hb and hb.get("active_positions"))):
        if hb is None:
            st.caption(f"No heartbeat at `{CRYPTO_VOL_HEARTBEAT_FILE}` — enable via CRYPTO_VOL_SLEEVE_ENABLED.")
            return
        if hb.get("blocked"):
            st.warning(f"Paper-only guard: {hb['blocked']}")
        elif hb.get("error"):
            st.warning(hb["error"])

        positions = hb.get("active_positions") or []
        cooldown = hb.get("cooldown_coins") or []
        last_sig = hb.get("last_signal_time")
        today_pnl = float(hb.get("today_pnl") or 0)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Active positions", len(positions))
        c2.metric("Last signal", str(last_sig or "—")[-19:] if last_sig else "—")
        c3.metric("Cooldown coins", len(cooldown))
        c4.metric("Today PnL", f"${today_pnl:+,.2f}")

        if positions:
            st.dataframe(pd.DataFrame(positions), use_container_width=True, hide_index=True)
        if cooldown:
            st.caption(f"Cooldown: {', '.join(cooldown)}")
        filters = hb.get("filters") or {}
        if filters.get("spy_gate"):
            st.caption(f"SPY gate active — entries blocked ({filters.get('spy_reason', '')})")
        elif filters.get("hour_ok") is False:
            st.caption("Outside UTC entry hours (13–16, 18–22)")


def render_bot_status(
    heartbeat: dict | None,
    *,
    compact: bool = False,
    chart_height: int = CHART_HEIGHT_FULL,
) -> None:
    st.subheader("Bot status")
    if heartbeat is None:
        st.info(f"No heartbeat file found at `{config.HEARTBEAT_FILE}`. Is `run_all.py` running?")
        return

    regime = heartbeat.get("regime", "—")
    halted = bool(heartbeat.get("halted"))
    last_cycle = heartbeat.get("timestamp", "—")
    active = _active_sleeves(heartbeat)
    paper = heartbeat.get("paper", config.PAPER_TRADING)

    if compact:
        c1, c2, c3 = st.columns(3)
        c1.metric("Regime", regime.split(":")[-1].strip() if ":" in regime else regime)
        c2.metric("Status", "HALTED" if halted else "Running")
        c3.metric("Last cycle", str(last_cycle)[-19:] if last_cycle != "—" else "—")
        st.caption(
            f"{'Paper' if paper else 'Live'} | "
            f"Sleeves: {', '.join(active) if active else 'cash only'} | "
            f"Session: {'open' if heartbeat.get('equity_session_open') else 'closed'}"
        )
    else:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Regime", regime)
        c2.metric("Active Sleeves", ", ".join(active) if active else "None")
        c3.metric("Halt Status", "HALTED" if halted else "Running")
        c4.metric("Last Cycle", last_cycle)
        st.caption(
            f"Account: {'Paper' if paper else 'Live'} | "
            f"Equity: ${float(heartbeat.get('equity') or 0):,.2f} | "
            f"Cash: ${float(heartbeat.get('cash') or 0):,.2f} | "
            f"Session open: {heartbeat.get('equity_session_open', '—')}"
        )

    exposure_rows = _sleeve_exposure_rows(heartbeat)
    exp_df = pd.DataFrame(exposure_rows)

    with st.expander("Sleeve breakdown", expanded=not compact):
        for row in exposure_rows:
            if row["Sleeve"] == "Cash":
                continue
            pct = min(max(float(row["Equity (%)"]) / 100.0, 0.0), 1.0)
            st.progress(
                pct,
                text=f"{row['Sleeve']}: {row['Equity (%)']:.1f}% of equity "
                f"({row['Utilization (%)']:.0f}% of cap)",
            )

        show_df = exp_df.copy()
        for col in ("Value ($)", "Cap ($)"):
            show_df[col] = show_df[col].map(lambda v: f"${v:,.2f}")
        for col in ("Cap Target (%)", "Utilization (%)", "Equity (%)"):
            show_df[col] = show_df[col].map(lambda v: f"{v:.1f}%")
        st.dataframe(show_df, use_container_width=True, hide_index=True)

        chart_df = exp_df[exp_df["Sleeve"] != "Cash"].copy()
        if not chart_df.empty:
            bar_h = min(chart_height, 280) if compact else 320
            fig = px.bar(
                chart_df,
                x="Sleeve",
                y="Equity (%)",
                color="Sleeve",
                title="Allocation (% of equity)",
                text=chart_df["Equity (%)"].map(lambda v: f"{v:.1f}%"),
            )
            fig.update_layout(showlegend=False, height=bar_h, margin=dict(t=40, b=20))
            fig.update_traces(textposition="outside")
            st.plotly_chart(fig, use_container_width=True)

    wisdom = heartbeat.get("wisdom")
    if wisdom:
        label = "Wisdom" if compact else "Wisdom (live heartbeat)"
        with st.expander(label, expanded=False):
            w1, w2, w3, w4 = st.columns(4)
            w1.metric("Mode", wisdom.get("mode", "—"))
            w2.metric("Web sentiment", f"{float(wisdom.get('web_sentiment') or 0):+.2f}")
            w3.metric("Gap", f"{float(wisdom.get('gap') or 0):+.2f}")
            w4.metric("Paused", "Yes" if wisdom.get("paused") else "No")
            mult = wisdom.get("sizing_multiplier")
            if mult is not None:
                st.caption(f"Sizing multiplier: {float(mult):.2f}")

    macro = heartbeat.get("macro_event")
    if macro and (macro.get("active") or macro.get("next")):
        with st.expander("Macro calendar", expanded=bool(macro.get("active"))):
            if macro.get("active") and macro.get("event"):
                ev = macro["event"]
                st.warning(
                    f"Event guard active: **{ev.get('name')}** ({ev.get('date')}) — "
                    f"sizing x{macro.get('sizing_scale', 1):.2f}"
                )
            elif macro.get("next"):
                nxt = macro["next"]
                st.caption(
                    f"Next release: **{nxt.get('name')}** on {nxt.get('date')} "
                    f"({nxt.get('hours_until', 0):+.0f}h)"
                )

    sleeve_pnl = heartbeat.get("sleeve_pnl")
    if sleeve_pnl and not compact:
        pnl_rows = []
        for key, label in (("spy", "SPY"), ("crypto", "Crypto"), ("nyse", "NYSE")):
            row = sleeve_pnl.get(key) or {}
            if row.get("positions", 0) <= 0:
                continue
            pnl_rows.append(
                {
                    "Sleeve": label,
                    "Unrealized ($)": row.get("unrealized_pnl", 0),
                    "Unrealized (%)": row.get("unrealized_pnl_pct", 0) * 100,
                    "Underwater": "Yes" if row.get("underwater") else "No",
                }
            )
        if pnl_rows:
            st.markdown("**Sleeve cost basis (heartbeat)**")
            st.dataframe(pd.DataFrame(pnl_rows), use_container_width=True, hide_index=True)


def render_positions(df: pd.DataFrame | None, err: str | None) -> None:
    st.subheader("Live Positions & P&L")
    if err:
        st.warning(err)
        return
    if df is None:
        st.warning("Unable to load positions.")
        return
    if df.empty:
        st.info("No open Alpaca positions.")
        return
    st.dataframe(_style_pnl_df(df), use_container_width=True, hide_index=True)
    total_upl = df["Unrealized P&L ($)"].sum()
    st.metric("Total unrealized P&L", f"${total_upl:+,.2f}")


def render_recent_trades() -> None:
    st.subheader("Recent Trades")
    sleeve_filter = st.selectbox(
        "Filter by sleeve",
        options=["All", "SPY", "NYSE", "Crypto"],
        key="journal_sleeve_filter",
    )
    df = _load_journal(limit=500)
    if df is None:
        st.info(f"No journal found at `{config.PAPER_JOURNAL_CSV}`.")
        return
    if df.empty:
        st.info("Journal file is empty.")
        return

    if sleeve_filter != "All":
        df = df[df["sleeve"] == sleeve_filter]
    display = df.head(20).copy()
    if display.empty:
        st.info(f"No journal rows for sleeve **{sleeve_filter}**.")
        return

    cols = [c for c in display.columns if c != "sleeve"]
    show = display[cols + (["sleeve"] if "sleeve" in display.columns else [])]
    st.dataframe(show, use_container_width=True, hide_index=True)


def render_wisdom_scorecard(scorecard: dict | None, *, compact: bool = False) -> None:
    if scorecard is None:
        st.info(f"No scorecard found at `{config.WISDOM_SCORECARD_FILE}`.")
        return

    evaluated = scorecard.get("evaluated_at", "—")
    window = scorecard.get("window_days", "—")
    st.caption(f"Evaluated: {evaluated} | Window: {window} days")

    live = scorecard.get("live") or {}
    if live and not compact:
        st.markdown("**Live performance (daily self-evaluation)**")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Return", f"{float(live.get('return_pct') or 0):+.2f}%")
        m2.metric("Sharpe", f"{float(live.get('sharpe') or 0):.2f}")
        m3.metric("Max drawdown", f"{float(live.get('max_drawdown_pct') or 0):+.2f}%")
        m4.metric("Mode", live.get("mode", "—"))

        m5, m6, m7, m8 = st.columns(4)
        m5.metric("Start equity", f"${float(live.get('start_equity') or 0):,.2f}")
        m6.metric("End equity", f"${float(live.get('end_equity') or 0):,.2f}")
        m7.metric("Pause cycles", f"{int(live.get('pause_cycles') or 0):,}")
        m8.metric("Total cycles", f"{int(live.get('cycles') or 0):,}")

        if live.get("from_date") and live.get("to_date"):
            st.caption(f"Period: {live['from_date']} → {live['to_date']}")

    best = scorecard.get("best_sim_mode")
    live_vs_best = scorecard.get("live_vs_best_sim_return_pp")
    if best is not None:
        st.caption(
            f"Best simulated mode: **{best}** | "
            f"Live vs best sim: {float(live_vs_best or 0):+.2f} pp"
        )

    sim_modes = scorecard.get("simulated_modes") or {}
    if sim_modes:
        sim_rows = []
        for mode, stats in sim_modes.items():
            sim_rows.append(
                {
                    "Mode": mode,
                    "Return (%)": float(stats.get("return_pct") or 0),
                    "Sharpe": float(stats.get("sharpe") or 0),
                    "Max DD (%)": float(stats.get("max_drawdown_pct") or 0),
                    "Orders": int(stats.get("orders") or 0),
                    "Paused days": int(stats.get("paused_days") or 0),
                }
            )
        with st.expander("Simulated modes comparison", expanded=not compact):
            st.dataframe(pd.DataFrame(sim_rows), use_container_width=True, hide_index=True)


def render_nerdminer() -> None:
    state = load_state(nm_config.STATE_FILE)

    with st.expander("Setup tips (WiFi, power, pool)"):
        st.markdown(
            """
            - **WiFi:** Use 2.4 GHz only; aim for RSSI better than **-70 dBm** (move closer to the router).
            - **USB:** Prefer a **direct rear USB port** or powered hub — avoid flaky front-panel ports.
            - **Pool:** Default NerdMiner solo pool (`solobtc.nmminer.com`) is fine; lower latency = fewer stale shares.
            - **Firmware:** You're near the ESP32 hash ceiling on v1.8.x; gains come from stability, not overclocking.
            - **Monitor:** Keep `python -m nerdminer` running while the dashboard is open.
            """
        )

    if state is None:
        st.info(
            f"No miner data at `{nm_config.STATE_FILE}`. "
            "Run `python -m nerdminer --once` or start the background monitor."
        )
        return

    status, warnings = assess_health(state, stale_seconds=nm_config.STALE_SECONDS)
    status_labels = {"ok": "OK", "warning": "Warning", "offline": "Offline"}
    st.caption(
        f"Port `{state.get('port', '—')}` | "
        f"Firmware v{state.get('firmware', '—')} | "
        f"Pool `{state.get('pool', '—')}` | "
        f"Updated `{state.get('updated_at', '—')}`"
    )

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Status", status_labels.get(status, status))
    hr = state.get("hash_rate_mhs")
    m2.metric("Hash rate", f"{float(hr):.4f} MH/s" if hr is not None else "—")
    accepted = int(state.get("shares_accepted") or 0)
    rejected = int(state.get("shares_rejected") or 0)
    m3.metric("Shares (R/A)", f"{rejected} / {accepted}")
    reject_pct = state.get("reject_pct")
    m4.metric("Reject %", f"{float(reject_pct):.2f}%" if reject_pct is not None else "—")

    m5, m6, m7, m8 = st.columns(4)
    m5.metric("Best diff", state.get("best_diff", "—"))
    m6.metric("RSSI", f"{state.get('rssi_dbm')} dBm" if state.get("rssi_dbm") is not None else "—")
    share_ms = state.get("last_share_ms")
    m7.metric("Last share", f"{share_ms} ms" if share_ms is not None else "—")
    m8.metric("Block hits", str(state.get("hits", "—")))

    if warnings:
        st.warning(" · ".join(warnings))

    history = load_history(limit=300, path=nm_config.HISTORY_FILE)
    if history:
        hist_df = pd.DataFrame(history)
        if "ts" in hist_df.columns and "hash_rate_mhs" in hist_df.columns:
            hist_df["ts"] = pd.to_datetime(hist_df["ts"], errors="coerce", utc=True)
            hist_df = hist_df.dropna(subset=["ts", "hash_rate_mhs"])
            if not hist_df.empty:
                fig = px.line(
                    hist_df,
                    x="ts",
                    y="hash_rate_mhs",
                    title="Hash rate (monitor history)",
                    labels={"ts": "Time (UTC)", "hash_rate_mhs": "MH/s"},
                )
                fig.update_layout(margin=dict(l=0, r=0, t=40, b=0), height=280)
                st.plotly_chart(fig, use_container_width=True)


def render_price_chart(
    positions_df: pd.DataFrame | None, *, chart_height: int = CHART_HEIGHT_FULL
) -> None:
    if positions_df is None or positions_df.empty:
        st.info("Open an Alpaca position to chart a ticker from `market_data.db`.")
        return

    tickers = sorted(positions_df["Ticker"].dropna().unique().tolist())
    selected = st.selectbox("Ticker (open positions)", options=tickers, key="chart_ticker")
    if not selected:
        return

    bars = _load_daily_ohlcv(selected, days=CHART_DAYS)
    if bars is None or bars.empty:
        st.warning(
            f"No daily data for `{_daily_table_name(selected)}` in `{config.DB_PATH}`. "
            "Run `python fetch_data.py --daily --days 365`."
        )
        return

    st.caption(
        f"DB table `{_daily_table_name(selected)}` — {len(bars)} rows "
        f"(Close-only stored; OHLC derived for candlesticks)"
    )
    st.plotly_chart(
        _build_candlestick_figure(selected, bars, height=chart_height),
        use_container_width=True,
    )


def _auto_refresh() -> None:
    """Re-run the app every REFRESH_SECONDS via sleep + st.rerun (no st.fragment)."""
    now = time.time()
    if "last_refresh" not in st.session_state:
        st.session_state.last_refresh = now

    elapsed = now - float(st.session_state.last_refresh)
    remaining = max(0.0, REFRESH_SECONDS - elapsed)

    if elapsed >= REFRESH_SECONDS:
        st.session_state.last_refresh = time.time()
        st.rerun()

    st.sidebar.caption(f"Next auto-refresh in ~{int(remaining)}s")
    time.sleep(remaining)
    st.session_state.last_refresh = time.time()
    st.rerun()


def main() -> None:
    st.set_page_config(
        page_title="PythonTrading Dashboard",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    _inject_dashboard_css()

    heartbeat = _load_json(_resolve_path(config.HEARTBEAT_FILE))
    scorecard = _load_json(_resolve_path(config.WISDOM_SCORECARD_FILE))
    acct_eq, acct_cash, acct_err = _fetch_account_summary()
    positions_df, pos_err = _fetch_positions()

    equity, cash = _display_equity(heartbeat, acct_eq, acct_cash)
    if equity > 0:
        config.configure_account_profile(equity)
    small_account = equity > 0 and config.is_small_account(equity)
    chart_height = CHART_HEIGHT_COMPACT if small_account else CHART_HEIGHT_FULL

    title = "PythonTrading Dashboard"
    if small_account:
        title = "PythonTrading · $100 Live"
    st.title(title)

    if small_account:
        st.error(
            "SMALL ACCOUNT MODE ACTIVE — 1% risk, 90% VTI core, $10 max order"
        )

    if not config.PAPER_TRADING:
        st.markdown(LIVE_WARNING_HTML, unsafe_allow_html=True)

    st.sidebar.header("Controls")
    if st.sidebar.button("Refresh now"):
        st.session_state.last_refresh = 0

    mode_label = "Paper" if config.PAPER_TRADING else "Live"
    st.sidebar.metric("Trading mode", mode_label)
    if acct_err:
        st.sidebar.warning(acct_err)
    elif acct_eq is not None:
        st.sidebar.metric("Alpaca equity", f"${acct_eq:,.2f}")
        st.sidebar.metric("Alpaca cash", f"${acct_cash:,.2f}")
    if small_account:
        st.sidebar.caption("Small account safety profile active")
    st.sidebar.caption(f"Heartbeat: `{config.HEARTBEAT_FILE}`")
    st.sidebar.caption(f"Journal: `{config.PAPER_JOURNAL_CSV}`")
    st.sidebar.caption(f"Scorecard: `{config.WISDOM_SCORECARD_FILE}`")
    st.sidebar.caption(f"Database: `{config.DB_PATH}`")

    _render_key_metrics_row(heartbeat, positions_df, equity, cash)

    if scorecard:
        _render_performance_summary_prominent(scorecard, compact=small_account)

    if heartbeat:
        _render_expected_actions(heartbeat)
        if small_account and float((heartbeat.get("sleeve_caps") or {}).get("vti_core") or 0) > 0:
            _render_vti_core_section(heartbeat)

    st.divider()
    render_bot_status(heartbeat, compact=small_account, chart_height=chart_height)
    render_crypto_vol_panel()
    st.divider()
    render_positions(positions_df, pos_err)
    st.divider()
    render_recent_trades()

    with st.expander("Wisdom scorecard detail", expanded=not small_account):
        render_wisdom_scorecard(scorecard, compact=small_account)

    with st.expander("Price chart", expanded=bool(positions_df is not None and not positions_df.empty)):
        render_price_chart(positions_df, chart_height=chart_height)

    with st.expander("NerdMiner v2", expanded=False):
        render_nerdminer()

    updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st.caption(f"Last updated: {updated} (auto-refresh every {REFRESH_SECONDS}s)")

    _auto_refresh()


if __name__ == "__main__":
    main()
