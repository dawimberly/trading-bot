"""UFC Predictor — Streamlit trading-terminal dashboard."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import streamlit as st

import config
from src.backtester import (
    backtest_2025,
    load_backtest_summary,
    run_holdout_backtest,
)
from src.data_loader import (
    ensure_data_dirs,
    get_upcoming_card,
    list_upcoming_events,
    load_fights,
    load_historical_data,
    load_processed_features,
)
from src.explainability import parse_explanation_json, shap_available
from src.feature_engineering import build_feature_matrix, feature_coverage_summary, save_features
from src.model_trainer import load_trained_model, train_model
from src.predictor import (
    FightPredictor,
    OddsAPIError,
    get_fight_explanation,
    merge_predictions_with_odds,
    predict_upcoming_card,
    rank_predictions_by_edge,
)
from src.risk_manager import assess_upcoming_card_risk, run_monte_carlo
from src.alerts import (
    dispatch_alerts,
    format_alert_text,
    generate_alerts,
    send_discord_alert,
    send_telegram_alert,
)

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="UFC Predictor Terminal",
    page_icon="🥊",
    layout="wide",
    initial_sidebar_state="expanded",
)

ensure_data_dirs()

# ── Trading-terminal theme ───────────────────────────────────────────────────
TERMINAL_CSS = """
<style>
    @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap');

    .stApp {
        background: linear-gradient(165deg, #0b0f14 0%, #111820 45%, #0d1218 100%);
    }
    [data-testid="stSidebar"] {
        background: #0a0e13;
        border-right: 1px solid #1e2a38;
    }
    [data-testid="stSidebar"] .stMarkdown h1,
    [data-testid="stSidebar"] .stMarkdown h2,
    [data-testid="stSidebar"] .stMarkdown h3 {
        color: #8fd3ff;
        font-family: 'IBM Plex Mono', monospace;
        letter-spacing: 0.04em;
    }
    h1, h2, h3, p, label, span, div {
        font-family: 'IBM Plex Sans', sans-serif;
    }
    .terminal-header {
        font-family: 'IBM Plex Mono', monospace;
        background: linear-gradient(90deg, #0f1923, #152232);
        border: 1px solid #243447;
        border-radius: 8px;
        padding: 1rem 1.25rem;
        margin-bottom: 1rem;
    }
    .terminal-header h1 {
        color: #e8f4ff;
        font-size: 1.55rem;
        margin: 0;
        letter-spacing: 0.06em;
    }
    .terminal-header p {
        color: #6b8aa8;
        margin: 0.25rem 0 0;
        font-size: 0.85rem;
    }
    .status-pill {
        display: inline-block;
        padding: 0.15rem 0.55rem;
        border-radius: 4px;
        font-family: 'IBM Plex Mono', monospace;
        font-size: 0.72rem;
        font-weight: 600;
        letter-spacing: 0.05em;
    }
    .pill-live { background: #0d3320; color: #3dff9a; border: 1px solid #1f6b42; }
    .pill-warn { background: #3a2a08; color: #ffc857; border: 1px solid #6b4f12; }
    .pill-off  { background: #2a1518; color: #ff7b8a; border: 1px solid #6b2a32; }
    div[data-testid="stMetric"] {
        background: #121a24;
        border: 1px solid #1e2d3d;
        border-radius: 8px;
        padding: 0.65rem 0.85rem;
    }
    div[data-testid="stMetric"] label { color: #6b8aa8 !important; font-size: 0.75rem !important; }
    div[data-testid="stMetric"] [data-testid="stMetricValue"] {
        color: #d7ecff !important;
        font-family: 'IBM Plex Mono', monospace !important;
    }
    .stDataFrame { border: 1px solid #1e2d3d; border-radius: 8px; }
    .stButton > button {
        background: linear-gradient(180deg, #1a3a52, #122a3c);
        color: #c8e8ff;
        border: 1px solid #2a5575;
        font-family: 'IBM Plex Mono', monospace;
        font-weight: 600;
        letter-spacing: 0.04em;
    }
    .stButton > button:hover {
        border-color: #3d8ec4;
        color: #ffffff;
    }
    hr { border-color: #1e2a38; }
</style>
"""
st.markdown(TERMINAL_CSS, unsafe_allow_html=True)


# ── Session defaults ─────────────────────────────────────────────────────────
def _init_state() -> None:
    defaults = {
        "predictions": None,
        "backtest": None,
        "backtest_2025": None,
        "last_refresh": None,
        "data_status": "unknown",
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


_init_state()


# ── Helpers ──────────────────────────────────────────────────────────────────
def _model_ready() -> bool:
    return config.DEFAULT_MODEL_PATH.is_file() or config.LEGACY_MODEL_PATH.is_file()


def _features_ready() -> bool:
    return config.PROCESSED_FEATURES_CSV.is_file()


def _load_importance() -> dict[str, float]:
    if not config.FEATURE_IMPORTANCE_PATH.is_file():
        artifact = load_trained_model() if _model_ready() else {}
        return artifact.get("feature_importance", {})
    payload = json.loads(config.FEATURE_IMPORTANCE_PATH.read_text(encoding="utf-8"))
    return payload.get("importance", {})


def _implied_prob(f1_odds: float | None, f2_odds: float | None, pick_f1: bool) -> float | None:
    if f1_odds is None or f2_odds is None or f1_odds <= 1 or f2_odds <= 1:
        return None
    p1 = 1 / f1_odds
    p2 = 1 / f2_odds
    total = p1 + p2
    if total <= 0:
        return None
    return (p1 / total) if pick_f1 else (p2 / total)


def _edge_stars(edge_pct: float | None) -> str:
    if edge_pct is None or (isinstance(edge_pct, float) and pd.isna(edge_pct)):
        return "—"
    e = float(edge_pct)
    if e >= 8:
        return "★★★"
    if e >= 5:
        return "★★"
    if e >= 3:
        return "★"
    return "·"


def _edge_color_class(edge_pct: float | None) -> str:
    if edge_pct is None or (isinstance(edge_pct, float) and pd.isna(edge_pct)):
        return "neutral"
    e = float(edge_pct)
    if e >= 5:
        return "positive"
    if e <= -2:
        return "negative"
    return "neutral"


def _format_predictions_table(
    preds: pd.DataFrame,
    *,
    odds_lookup: dict[str, tuple[float | None, float | None]] | None = None,
) -> pd.DataFrame:
    """Build display table: Fight, Predicted Winner, Model Prob %, Implied Odds, Edge %."""
    rows: list[dict] = []
    for _, r in preds.iterrows():
        f1 = str(r.get("fighter_1", r.get("fighter1", "")))
        f2 = str(r.get("fighter_2", r.get("fighter2", "")))
        fight = f"{f1} vs {f2}"
        winner = str(r.get("predicted_winner", ""))
        if pd.notna(r.get("predicted_prob")):
            model_prob = float(r["predicted_prob"]) * 100
        elif winner == f1:
            model_prob = float(r.get("prob_f1_win", 0.5)) * 100
        else:
            model_prob = float(r.get("prob_f2_win", 1.0 - float(r.get("prob_f1_win", 0.5)))) * 100
        pick_f1 = winner == f1

        f1_odds = f2_odds = None
        fid = str(r.get(config.FIGHT_ID_COLUMN, fight))
        if odds_lookup and fid in odds_lookup:
            f1_odds, f2_odds = odds_lookup[fid]
        elif "f1_odds" in r and pd.notna(r.get("f1_odds")):
            f1_odds, f2_odds = float(r["f1_odds"]), float(r.get("f2_odds", 0) or 0)

        implied = _implied_prob(f1_odds, f2_odds, pick_f1)
        if "implied_prob_f1" in r and pd.notna(r.get("implied_prob_f1")):
            imp = float(r["implied_prob_f1"] if pick_f1 else r.get("implied_prob_f2", 0))
            implied_str = f"{imp * 100:.1f}%"
            edge_str = f"{float(r.get('edge_pct', (model_prob / 100 - imp) * 100)):+.1f}%"
        elif implied is not None:
            implied_str = f"{implied * 100:.1f}%"
            edge = (model_prob / 100 - implied) * 100
            edge_str = f"{edge:+.1f}%"
        else:
            implied_str = "TBD"
            edge_str = "—"

        ci_low = r.get("predicted_ci_low")
        ci_high = r.get("predicted_ci_high")
        if pd.notna(ci_low) and pd.notna(ci_high):
            ci_str = f"{float(ci_low)*100:.0f}–{float(ci_high)*100:.0f}%"
        else:
            ci_str = "—"

        edge_val = None
        if pd.notna(r.get("edge_pct")):
            edge_val = float(r["edge_pct"])
        elif edge_str not in ("—", "TBD") and edge_str.endswith("%"):
            try:
                edge_val = float(edge_str.replace("%", "").replace("+", ""))
            except ValueError:
                edge_val = None

        rows.append(
            {
                "Fight": fight,
                "Weight Class": r.get("weight_class", "—"),
                "Predicted Winner": winner,
                "Model Prob %": f"{model_prob:.1f}%",
                "90% CI": ci_str,
                "Edge %": edge_str,
                "Edge ★": _edge_stars(edge_val),
                "Signal": _edge_color_class(edge_val),
                "Uncertainty": str(r.get("uncertainty_label", "—")).upper(),
                "Confidence": str(r.get("confidence_label", "—")).upper(),
                "Implied Odds": implied_str,
                "_edge_val": edge_val,
            }
        )
    return pd.DataFrame(rows)


def _fight_label(row: pd.Series) -> str:
    f1 = str(row.get("fighter_1", row.get("fighter1", "")))
    f2 = str(row.get("fighter_2", row.get("fighter2", "")))
    pick = str(row.get("predicted_winner", ""))
    prob = row.get("predicted_prob", row.get("prob_f1_win"))
    prob_txt = f" — {float(prob):.0%}" if pd.notna(prob) else ""
    return f"{f1} vs {f2} → {pick}{prob_txt}"


def _resolve_explanation(row: pd.Series) -> dict:
    """Use cached SHAP JSON on the row or compute on demand."""
    if pd.notna(row.get("shap_explanation")):
        exp = parse_explanation_json(row.get("shap_explanation"))
        if exp.get("available"):
            return exp
    if pd.notna(row.get("reasoning")) and not shap_available():
        return {"available": False, "reasoning": str(row["reasoning"])}
    try:
        return get_fight_explanation(row)
    except Exception as exc:
        return {"available": False, "reasoning": f"Could not explain: {exc}"}


def _render_shap_bar_chart(explanation: dict) -> None:
    toward = explanation.get("toward_pick") or explanation.get("top_features") or []
    if not toward:
        st.caption("No SHAP contributors to chart.")
        return
    labels = [r["label"] for r in toward[:8]]
    impacts = [float(r["shap"]) for r in toward[:8]]
    colors = ["#3dd68c" if v >= 0 else "#f87171" for v in impacts]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    y_pos = range(len(labels))
    ax.barh(list(y_pos), impacts, color=colors)
    ax.set_yticks(list(y_pos))
    ax.set_yticklabels(labels)
    ax.axvline(0, color="#666", linewidth=0.8)
    ax.set_xlabel("SHAP impact on P(Fighter 1 wins) — log-odds scale")
    ax.set_title("Top feature drivers toward model pick")
    ax.invert_yaxis()
    fig.tight_layout()
    st.pyplot(fig)
    plt.close(fig)


def _render_drawdown_histogram(mc_result, staking: str = "quarter_kelly") -> None:
    dist = getattr(mc_result, "max_drawdown_distribution", {}).get(staking, [])
    if not dist:
        st.caption("No drawdown distribution for this staking mode.")
        return
    fig, ax = plt.subplots(figsize=(8, 3.5))
    ax.hist(dist, bins=40, color="#60a5fa", edgecolor="#1e293b", alpha=0.85)
    ax.axvline(float(np.mean(dist)), color="#fbbf24", linestyle="--", label="Expected")
    var_level = config.MC_CONFIDENCE_LEVEL
    var_dd = float(np.percentile(dist, var_level * 100))
    ax.axvline(var_dd, color="#f87171", linestyle=":", label=f"VaR {var_level:.0%}")
    ax.set_xlabel("Max drawdown (%)")
    ax.set_ylabel("Simulations")
    ax.set_title(f"Max drawdown distribution — {staking.replace('_', ' ')}")
    ax.legend()
    fig.tight_layout()
    st.pyplot(fig)
    plt.close(fig)


def _render_equity_samples(mc_result, staking: str = "quarter_kelly") -> None:
    curves = getattr(mc_result, "sample_equity_curves", {}).get(staking, [])
    if not curves:
        return
    fig, ax = plt.subplots(figsize=(8, 3.5))
    for curve in curves[:15]:
        ax.plot(curve, alpha=0.35, color="#34d399")
    ax.set_xlabel("Bet step")
    ax.set_ylabel("Equity ($)")
    ax.set_title(f"Sample equity paths — {staking.replace('_', ' ')}")
    fig.tight_layout()
    st.pyplot(fig)
    plt.close(fig)


def _style_predictions_df(df: pd.DataFrame) -> pd.DataFrame:
    """Color-code edge column for Streamlit display."""
    if df.empty or "Edge %" not in df.columns:
        return df

    def _highlight(row):
        signal = row.get("Signal", "neutral")
        styles = [""] * len(row)
        if signal == "positive":
            styles = ["background-color: #0d3320; color: #3dff9a"] * len(row)
        elif signal == "negative":
            styles = ["background-color: #2a1518; color: #ff7b8a"] * len(row)
        return styles

    display = df.drop(columns=[c for c in ("Signal", "_edge_val") if c in df.columns])
    try:
        return display.style.apply(_highlight, axis=1)
    except Exception:
        return display


def _refresh_pipeline() -> tuple[str, int, int]:
    """Download data, rebuild features. Returns status message, fights, feature rows."""
    fights = load_historical_data(force_refresh=True)
    features = build_feature_matrix(fights)
    save_features(features)
    get_upcoming_card(force_refresh=True)
    st.session_state.last_refresh = datetime.now(timezone.utc).isoformat()
    st.session_state.data_status = "live"
    return f"Refreshed {len(fights):,} fights → {len(features):,} feature rows", len(fights), len(features)


def _retrain_pipeline(tune: str) -> str:
    if not _features_ready():
        fights = load_fights()
        save_features(build_feature_matrix(fights))
    features = load_processed_features()
    result = train_model(features, tune=tune, calibration_method=config.CALIBRATION_METHOD)
    bt = run_holdout_backtest(features, save_report=True, run_walk_forward=True)
    st.session_state.backtest = bt
    return (
        f"Model saved · AUC {result.metrics.get('roc_auc', 0):.3f} · "
        f"Acc {result.metrics.get('accuracy', 0):.1%} · "
        f"Backtest report → {bt.report_dir or config.BACKTEST_DIR}"
    )


@st.cache_data(ttl=1800, show_spinner=False)
def _cached_backtest():
    if not _features_ready() or not _model_ready():
        return None
    return run_holdout_backtest(
        load_processed_features(),
        save_report=True,
        run_walk_forward=True,
    )


# ── Header ─────────────────────────────────────────────────────────────────
now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
model_status = "LIVE" if _model_ready() else "NO MODEL"
pill_class = "pill-live" if _model_ready() else "pill-off"

st.markdown(
    f"""
    <div class="terminal-header">
        <h1>◈ UFC PREDICTOR TERMINAL</h1>
        <p>
            <span class="status-pill {pill_class}">{model_status}</span>
            &nbsp;·&nbsp; {now}
            &nbsp;·&nbsp; LightGBM + calibrated probabilities
        </p>
    </div>
    """,
    unsafe_allow_html=True,
)

# ── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### ⚙ CONTROLS")

    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("↻ REFRESH DATA", use_container_width=True):
            try:
                with st.spinner("Syncing UFC data…"):
                    msg, _, _ = _refresh_pipeline()
                st.success(msg)
            except Exception as exc:
                st.error(str(exc))
    with col_b:
        if st.button("▶ RETRAIN", use_container_width=True):
            if not _model_ready() and not _features_ready():
                st.warning("Refresh data first.")
            else:
                try:
                    with st.spinner("Training model…"):
                        tune_mode = st.session_state.get("tune_mode", "none")
                        msg = _retrain_pipeline(tune_mode)
                    st.success(msg)
                    st.cache_data.clear()
                except Exception as exc:
                    st.error(str(exc))

    if st.button("📈 RUN BACKTEST", use_container_width=True):
        if not _model_ready():
            st.warning("Train a model first.")
        elif not _features_ready():
            st.warning("Build features first (↻ REFRESH DATA).")
        else:
            try:
                with st.spinner("Walk-forward backtest…"):
                    bt = run_holdout_backtest(
                        load_processed_features(),
                        save_report=True,
                        run_walk_forward=True,
                    )
                    st.session_state.backtest = bt
                st.success(
                    f"Report saved → {bt.report_dir or config.BACKTEST_DIR} "
                    f"({int(bt.classification.get('n_fights', 0))} hold-out fights)"
                )
                st.cache_data.clear()
            except Exception as exc:
                st.error(str(exc))

    st.divider()
    st.markdown("### 🎯 MATCHUP SOURCE")
    source_mode = st.radio(
        "Input mode",
        ["Upcoming Event", "Manual Matchup"],
        label_visibility="collapsed",
    )

    selected_card: pd.DataFrame | None = None
    manual_odds: tuple[float | None, float | None] = (None, None)

    if source_mode == "Upcoming Event":
        events = list_upcoming_events()
        if events:
            labels = [
                f"{e.get('event_name', 'Event')} ({e.get('event_date', '')})"
                for e in events
            ]
            pick = st.selectbox("Select event", range(len(labels)), format_func=lambda i: labels[i])
            if st.session_state.get("event_index") != pick:
                st.session_state.event_index = pick
                try:
                    with st.spinner("Fetching fight card…"):
                        st.session_state.selected_card = get_upcoming_card(
                            event_index=pick, force_refresh=False
                        )
                except Exception as exc:
                    st.error(str(exc))
            selected_card = st.session_state.get("selected_card")
        else:
            st.caption("Could not reach UFC.com — using cached card.")
            cache_path = config.CACHE_DIR / f"upcoming_card_{st.session_state.get('event_index', 0)}.csv"
            if cache_path.is_file():
                selected_card = pd.read_csv(cache_path, parse_dates=["date"])
    else:
        st.markdown("**Manual matchup**")
        fighter1 = st.text_input("Fighter 1 (red corner)", placeholder="Ilia Topuria")
        fighter2 = st.text_input("Fighter 2 (blue corner)", placeholder="Justin Gaethje")
        weight_class = st.text_input("Weight class", value="Lightweight")
        st.caption("Optional odds (decimal) for edge calc")
        c1, c2 = st.columns(2)
        with c1:
            f1_odds_in = st.number_input("F1 odds", min_value=1.01, value=1.85, step=0.05)
        with c2:
            f2_odds_in = st.number_input("F2 odds", min_value=1.01, value=2.05, step=0.05)
        manual_odds = (f1_odds_in, f2_odds_in)
        if fighter1 and fighter2:
            selected_card = pd.DataFrame(
                [{
                    "event": "Manual Matchup",
                    "date": pd.Timestamp.utcnow().normalize(),
                    "fighter1": fighter1,
                    "fighter2": fighter2,
                    "weight_class": weight_class,
                    "f1_odds": f1_odds_in,
                    "f2_odds": f2_odds_in,
                }]
            )

    st.divider()
    st.markdown("### 🔧 TRAIN OPTIONS")
    st.session_state.tune_mode = st.selectbox(
        "Tuning",
        ["none", "optuna", "grid"],
        index=0,
        help="Used when you click RETRAIN",
    )
    use_odds_api = st.toggle(
        "Use The Odds API",
        value=bool(config.ODDS_API_KEY),
        help="Set THE_ODDS_API_KEY in .env",
    )
    use_sentiment = st.toggle(
        "Use News Sentiment",
        value=bool(config.NEWS_API_KEY),
        help="Set NEWS_API_KEY in .env (newsapi.org)",
    )
    st.session_state.use_sentiment = use_sentiment
    use_explain = st.toggle(
        "SHAP Explanations",
        value=True,
        help="Compute per-fight 'Why This Pick?' drivers (requires shap package).",
    )
    st.session_state.use_explain = use_explain

    st.divider()
    st.caption("DATA STATUS")
    if config.RAW_FIGHTS_CSV.is_file():
        n = len(pd.read_csv(config.RAW_FIGHTS_CSV))
        st.markdown(f"**{n:,}** historical fights")
    if st.session_state.last_refresh:
        st.caption(f"Last sync: {st.session_state.last_refresh[:19]}")

# ── Run predictions (on demand) ─────────────────────────────────────────────
predictions_table = st.session_state.get("predictions", pd.DataFrame())
raw_preds: pd.DataFrame | None = st.session_state.get("raw_preds")

run_preds = st.button("▶ RUN PREDICTIONS", use_container_width=True)
if run_preds:
    if not _model_ready():
        st.warning("Train a model first with **▶ RETRAIN**.")
    elif selected_card is None or selected_card.empty:
        st.warning("Select an event or enter a manual matchup.")
    else:
        try:
            with st.spinner("Running inference…"):
                raw_preds = predict_upcoming_card(
                    selected_card,
                    historical_fights=load_fights(),
                    attach_odds=use_odds_api and bool(config.ODDS_API_KEY),
                    attach_sentiment=st.session_state.get("use_sentiment", False)
                    and bool(config.NEWS_API_KEY),
                    explain=st.session_state.get("use_explain", False),
                )
                if use_odds_api and config.ODDS_API_KEY and "odds_matched" not in raw_preds.columns:
                    raw_preds = merge_predictions_with_odds(raw_preds)
                odds_lookup: dict[str, tuple[float | None, float | None]] = {}
                if source_mode == "Manual Matchup" and manual_odds[0]:
                    fid = str(raw_preds[config.FIGHT_ID_COLUMN].iloc[0])
                    odds_lookup[fid] = manual_odds
                for _, row in selected_card.iterrows():
                    f1 = str(row.get("fighter1", row.get("fighter_1", "")))
                    f2 = str(row.get("fighter2", row.get("fighter_2", "")))
                    fid_col = row.get("fight_id") or row.get(config.FIGHT_ID_COLUMN)
                    if pd.notna(row.get("f1_odds")):
                        key = str(fid_col) if pd.notna(fid_col) else f"{f1}|{f2}"
                        odds_lookup[key] = (float(row["f1_odds"]), float(row.get("f2_odds", 0)))
                predictions_table = _format_predictions_table(raw_preds, odds_lookup=odds_lookup)
                st.session_state.predictions = predictions_table
                st.session_state.raw_preds = raw_preds
        except Exception as exc:
            st.warning(f"Could not score card: {exc}")
elif not _model_ready():
    st.info("Train a model with **▶ RETRAIN** (after **↻ REFRESH DATA**) to see predictions.")

# ── Top metrics ──────────────────────────────────────────────────────────────
m1, m2, m3, m4, m5 = st.columns(5)
fights_n = len(pd.read_csv(config.RAW_FIGHTS_CSV)) if config.RAW_FIGHTS_CSV.is_file() else 0
feat_n = len(load_processed_features()) if _features_ready() else 0
m1.metric("FIGHTS", f"{fights_n:,}")
m2.metric("FEATURE ROWS", f"{feat_n:,}")
m3.metric("BOUTS SCORED", len(predictions_table) if not predictions_table.empty else 0)

if _model_ready():
    metrics = load_trained_model().get("metrics", {})
    m4.metric("MODEL AUC", f"{metrics.get('roc_auc', 0):.3f}")
    m5.metric("LOG LOSS", f"{metrics.get('log_loss', 0):.3f}")
else:
    m4.metric("MODEL AUC", "—")
    m5.metric("LOG LOSS", "—")

st.divider()

tab_preds, tab_explorer, tab_coverage, tab_strategies, tab_risk, tab_alerts, tab_backtest = st.tabs(
    [
        "📋 Predictions",
        "🔍 Why This Pick?",
        "📊 Feature Coverage",
        "💰 Betting Strategies",
        "⚠️ Risk Analysis",
        "🔔 Live Alerts",
        "📈 Backtest Report",
    ]
)

with tab_preds:
    st.markdown("### TOP EDGE BETS (current card)")
    if raw_preds is not None and not raw_preds.empty:
        ranked = rank_predictions_by_edge(raw_preds)
        top5 = ranked.head(5)
        if not top5.empty and top5["best_edge"].notna().any():
            top_rows = []
            for _, r in top5.iterrows():
                f1 = str(r.get("fighter_1", ""))
                f2 = str(r.get("fighter_2", ""))
                pick = str(r.get("predicted_winner", ""))
                edge = r.get("best_edge")
                edge_pct = float(edge) * 100 if pd.notna(edge) else None
                top_rows.append(
                    {
                        "Fight": f"{f1} vs {f2}",
                        "Pick": pick,
                        "Edge %": f"{edge_pct:+.1f}%" if edge_pct is not None else "—",
                        "Edge ★": _edge_stars(edge_pct),
                        "Model %": f"{float(r.get('predicted_prob', 0.5))*100:.1f}%",
                    }
                )
            st.dataframe(pd.DataFrame(top_rows), use_container_width=True, hide_index=True)
        else:
            st.caption("No odds/edge data — enable Odds API or enter manual odds.")
    else:
        st.caption("Run **▶ RUN PREDICTIONS** to see top edge opportunities.")

    st.markdown("### FULL CARD")
    if not predictions_table.empty:
        styled = _style_predictions_df(predictions_table)
        st.dataframe(styled, use_container_width=True, hide_index=True)
        if raw_preds is not None and selected_card is not None and len(raw_preds) < len(selected_card):
            st.caption(
                f"Showing {len(raw_preds)} of {len(selected_card)} bouts — "
                "fighters need ≥3 UFC fights for features."
            )
    else:
        st.dataframe(
            pd.DataFrame(
                columns=["Fight", "Predicted Winner", "Model Prob %", "Edge %", "Edge ★", "Implied Odds"]
            ),
            use_container_width=True,
            hide_index=True,
        )
        st.caption("Select an upcoming event or enter a manual matchup in the sidebar.")

with tab_explorer:
    st.markdown("### WHY THIS PICK? — Fight Explorer")
    st.caption(
        "SHAP drivers from the LightGBM base model (log-odds impact on P(Fighter 1 wins)). "
        "Positive bars favor Fighter 1; negative bars favor Fighter 2."
    )
    if not shap_available():
        st.warning("Install `shap` for explanations: `pip install shap`")
    if raw_preds is None or raw_preds.empty:
        st.info("Run **▶ RUN PREDICTIONS** to explore model reasoning.")
    else:
        fight_labels = [_fight_label(raw_preds.iloc[i]) for i in range(len(raw_preds))]
        pick_idx = st.selectbox(
            "Select fight",
            range(len(fight_labels)),
            format_func=lambda i: fight_labels[i],
        )
        row = raw_preds.iloc[pick_idx]
        explanation = _resolve_explanation(row)

        c1, c2, c3 = st.columns(3)
        c1.metric("Predicted winner", str(row.get("predicted_winner", "—")))
        prob = row.get("predicted_prob")
        if pd.isna(prob) and pd.notna(row.get("prob_f1_win")):
            prob = (
                float(row["prob_f1_win"])
                if row.get("predicted_winner") == row.get("fighter_1")
                else float(row.get("prob_f2_win", 1 - float(row["prob_f1_win"])))
            )
        c2.metric("Model probability", f"{float(prob):.1%}" if pd.notna(prob) else "—")
        c3.metric("Confidence", str(row.get("confidence_label", "—")).upper())

        st.markdown("#### Natural language summary")
        st.info(explanation.get("reasoning", "No explanation available."))

        toward = explanation.get("toward_pick") or explanation.get("top_features") or []
        if toward:
            st.markdown("#### Top 8 SHAP drivers")
            driver_rows = []
            pick = str(row.get("predicted_winner", ""))
            f1 = str(row.get("fighter_1", ""))
            for feat in toward[:8]:
                impact = float(feat.get("shap", 0))
                if pick == f1:
                    direction = "supports pick" if impact > 0 else "opposes pick"
                else:
                    direction = "supports pick" if impact < 0 else "opposes pick"
                driver_rows.append(
                    {
                        "Feature": feat.get("label", feat.get("feature")),
                        "F1−F2 value": feat.get("value_display", feat.get("value")),
                        "SHAP impact": f"{impact:+.4f}",
                        "Direction": direction,
                    }
                )
            st.dataframe(pd.DataFrame(driver_rows), use_container_width=True, hide_index=True)
            _render_shap_bar_chart(explanation)
        elif not explanation.get("available"):
            st.caption(explanation.get("error", "SHAP explanation unavailable for this model."))

with tab_coverage:
    st.markdown("### FEATURE COVERAGE %")
    st.caption("Share of fights with non-zero key differential features (2025 holdout).")
    if _features_ready():
        feats = load_processed_features()
        cov = feature_coverage_summary(feats, year=config.BACKTEST_2025_YEAR)
        if not cov.empty:
            cov["nonzero_pct"] = (cov["nonzero_pct"] * 100).round(1)
            cov = cov.rename(columns={"feature": "Feature", "nonzero_pct": "Non-zero %", "n_fights": "Fights"})
            st.dataframe(cov, use_container_width=True, hide_index=True)
            avg_cov = cov["Non-zero %"].mean()
            st.metric("Mean coverage", f"{avg_cov:.1f}%")
            sparse = cov[cov["Non-zero %"] < 70]
            if not sparse.empty:
                st.warning(
                    "Sparse features (<70%): "
                    + ", ".join(sparse["Feature"].tolist())
                    + " — run ↻ REFRESH DATA with ufcstats enrichment."
                )
        else:
            st.info("No 2025 rows in feature matrix.")
    else:
        st.info("Build features via ↻ REFRESH DATA first.")

with tab_strategies:
    st.markdown("### BETTING STRATEGY LAB")
    kelly_frac = st.slider("Kelly fraction", 0.1, 1.0, 0.25, 0.05, help="Quarter-Kelly (0.25) recommended")
    max_bet_pct = st.slider("Max bet % bankroll", 0.5, 5.0, 2.0, 0.5) / 100.0
    max_card_pct = st.slider("Max card risk %", 3.0, 15.0, 8.0, 1.0) / 100.0
    min_edge_pct = st.slider("Min single-bet edge %", 3.0, 15.0, 5.0, 0.5) / 100.0
    st.markdown("#### Parlay filters (same card)")
    parlay_min_edge = st.slider("Parlay min leg edge %", 5.0, 15.0, 7.0, 0.5) / 100.0
    parlay_min_prob = st.slider("Min combined probability %", 20.0, 60.0, 35.0, 1.0) / 100.0
    parlay_max_legs = st.selectbox("Max parlay legs", [2, 3], index=1)

    preds = st.session_state.get("predictions")
    if preds is not None and not preds.empty:
        try:
            import config
            from src.strategy import StrategyConfig, build_parlay_candidates

            cfg = StrategyConfig(
                kelly_fraction=kelly_frac,
                max_bet_fraction=max_bet_pct,
                max_card_risk_fraction=max_card_pct,
                min_edge=min_edge_pct,
                parlay_min_edge=parlay_min_edge,
                parlay_min_combined_prob=parlay_min_prob,
                parlay_max_legs=parlay_max_legs,
            )
            st.caption(
                f"Kelly {kelly_frac:.0%} · max bet {max_bet_pct:.1%} · "
                f"card cap {max_card_pct:.0%} · min edge {min_edge_pct:.0%}"
            )
            if "event_name" in preds.columns:
                for event, grp in preds.groupby("event_name"):
                    parlays = build_parlay_candidates(grp, config=cfg)
                    if not parlays:
                        continue
                    st.markdown(f"**{event}** — top parlays by EV")
                    rows = []
                    for p in parlays[:5]:
                        legs = " + ".join(
                            f"{c.bet_side.upper()}@{c.edge:.0%}" for c in p.legs
                        )
                        rows.append(
                            {
                                "Legs": len(p.legs),
                                "Combined prob": f"{p.combined_prob:.0%}",
                                "Combined odds": f"{p.combined_odds:.2f}",
                                "EV": f"{p.expected_value:+.3f}",
                                "Picks": legs,
                            }
                        )
                    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            else:
                st.caption("Run predictions on an upcoming card to explore parlays.")
        except Exception as exc:
            st.error(f"Strategy module error: {exc}")
    else:
        st.info("Run **▶ RUN PREDICTIONS** first to preview parlay candidates.")

with tab_risk:
    st.markdown("### RISK ANALYSIS — Monte Carlo")
    st.caption(
        f"Bootstrap/parametric simulations ({config.MC_SIMULATIONS:,} historical · "
        f"{config.MC_CARD_SIMULATIONS:,} card) at {config.MC_CONFIDENCE_LEVEL:.0%} confidence."
    )

    mc_hist = st.session_state.get("monte_carlo_hist")
    bt2025_risk = st.session_state.get("backtest_2025")

    col_run, col_bank = st.columns([2, 1])
    with col_run:
        run_mc = st.button("▶ RUN HISTORICAL MC", use_container_width=True)
    with col_bank:
        mc_bankroll = st.number_input(
            "Bankroll ($)",
            min_value=100.0,
            value=float(config.INITIAL_BANKROLL),
            step=100.0,
        )

    if run_mc:
        preds_source = None
        if bt2025_risk is not None and getattr(bt2025_risk, "predictions", None) is not None:
            preds_source = bt2025_risk.predictions
        elif config.BACKTEST_2025_CSV.is_file():
            preds_source = pd.read_csv(config.BACKTEST_2025_CSV)
        if preds_source is None or preds_source.empty:
            st.warning("Run **2025 backtest** first or ensure backtest CSV exists.")
        else:
            try:
                with st.spinner("Simulating equity paths…"):
                    mc_hist = run_monte_carlo(
                        preds_source,
                        initial_bankroll=mc_bankroll,
                        random_seed=42,
                    )
                    st.session_state.monte_carlo_hist = mc_hist
            except Exception as exc:
                st.error(str(exc))

    mc_hist = st.session_state.get("monte_carlo_hist")
    if mc_hist is not None and getattr(mc_hist, "staking_summaries", None):
        st.markdown("#### Historical drawdown & tail risk")
        mode_pick = st.selectbox(
            "Staking mode",
            list(mc_hist.staking_summaries.keys()),
            format_func=lambda x: x.replace("_", " ").title(),
        )
        stats = mc_hist.staking_summaries.get(mode_pick, {})
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("Expected max DD", f"{stats.get('expected_max_drawdown_pct', 0):.1f}%")
        r2.metric(f"VaR max DD ({config.MC_CONFIDENCE_LEVEL:.0%})", f"{stats.get('var_max_drawdown_pct', 0):.1f}%")
        r3.metric("CVaR max DD", f"{stats.get('cvar_max_drawdown_pct', 0):.1f}%")
        r4.metric("Ruin probability", f"{stats.get('ruin_probability', 0):.1%}")

        r5, r6, r7 = st.columns(3)
        r5.metric("VaR return", f"{stats.get('var_return_pct', 0):+.1f}%")
        r6.metric("CVaR return", f"{stats.get('cvar_return_pct', 0):+.1f}%")
        r7.metric("Prob. positive", f"{stats.get('prob_positive_return', 0):.1%}")

        for w in getattr(mc_hist, "warnings", []) or []:
            st.warning(w)

        c1, c2 = st.columns(2)
        with c1:
            _render_drawdown_histogram(mc_hist, mode_pick)
        with c2:
            _render_equity_samples(mc_hist, mode_pick)

        if not mc_hist.per_card_pnl.empty:
            st.markdown("#### Per-card PnL distribution")
            st.dataframe(mc_hist.per_card_pnl, use_container_width=True, hide_index=True)
        if not mc_hist.rolling_card_risk.empty:
            st.markdown("#### Rolling multi-card risk")
            st.dataframe(mc_hist.rolling_card_risk, use_container_width=True, hide_index=True)
    else:
        st.info("Click **▶ RUN HISTORICAL MC** after a backtest to see drawdown distributions.")

    st.divider()
    st.markdown("#### Card risk summary (upcoming)")
    raw_for_risk = st.session_state.get("raw_preds")
    if raw_for_risk is not None and not raw_for_risk.empty:
        try:
            card_risk = assess_upcoming_card_risk(
                raw_for_risk,
                bankroll=mc_bankroll,
                simulations=config.MC_CARD_SIMULATIONS,
            )
            if card_risk.get("available"):
                cr = card_risk["card_pnl"]
                s1, s2, s3, s4 = st.columns(4)
                s1.metric("Value bets on card", int(card_risk["n_bets"]))
                s2.metric("Mean PnL", f"${cr['mean_pnl']:+,.0f}")
                s3.metric("5th %ile PnL", f"${cr['p5_pnl']:+,.0f}")
                s4.metric("Prob. losing card", f"{cr['prob_loss']:.1%}")
                st.metric(
                    "Suggested max card risk",
                    f"{card_risk['suggested_max_risk_pct']:.1f}%",
                    delta=f"base {card_risk['base_max_risk_pct']:.1f}%",
                )
                qk = card_risk.get("staking_modes", {}).get("quarter_kelly", {})
                st.caption(
                    f"Quarter-Kelly MC: exp DD {qk.get('expected_max_drawdown_pct', 0):.1f}% · "
                    f"ruin {qk.get('ruin_probability', 0):.1%}"
                )
                for w in card_risk.get("warnings", []):
                    st.warning(w)
            else:
                st.caption(card_risk.get("reason", "No card risk data."))
        except Exception as exc:
            st.error(f"Card risk simulation failed: {exc}")
    else:
        st.caption("Run **▶ RUN PREDICTIONS** with odds to assess upcoming card risk.")

with tab_alerts:
    st.markdown("### LIVE ALERTS")
    st.caption(
        "Discord / Telegram value-bet alerts with SHAP reasoning and MC card risk. "
        f"Cooldown {config.ALERT_COOLDOWN_MINUTES}m per event · min edge {config.ALERT_MIN_EDGE:.0%}."
    )

    c1, c2 = st.columns(2)
    with c1:
        discord_url = st.text_input(
            "Discord webhook",
            value=st.session_state.get("discord_webhook", config.DISCORD_WEBHOOK),
            type="password",
            help="Or set DISCORD_WEBHOOK in .env",
        )
        st.session_state.discord_webhook = discord_url
    with c2:
        tg_token = st.text_input(
            "Telegram bot token",
            value=st.session_state.get("telegram_token", config.TELEGRAM_BOT_TOKEN),
            type="password",
        )
        tg_chat = st.text_input(
            "Telegram chat ID",
            value=st.session_state.get("telegram_chat", config.TELEGRAM_CHAT_ID),
        )
        st.session_state.telegram_token = tg_token
        st.session_state.telegram_chat = tg_chat

    alert_edge = st.slider(
        "Alert min edge %",
        min_value=3.0,
        max_value=15.0,
        value=float(config.ALERT_MIN_EDGE * 100),
        step=0.5,
    ) / 100.0
    alert_dry = st.checkbox("Dry-run (no POST)", value=config.ALERT_DRY_RUN)

    raw_alert_src = st.session_state.get("raw_preds")
    if st.button("▶ PREVIEW ALERTS", use_container_width=True):
        if raw_alert_src is None or raw_alert_src.empty:
            st.warning("Run **▶ RUN PREDICTIONS** first (with odds for edge).")
        else:
            try:
                alert_preview = generate_alerts(
                    raw_alert_src,
                    min_edge=alert_edge,
                    event_name=None,
                )
                st.session_state.alert_preview = alert_preview
            except Exception as exc:
                st.error(str(exc))

    preview = st.session_state.get("alert_preview")
    if preview:
        st.text(format_alert_text(preview))
        if preview.get("warnings"):
            for w in preview["warnings"]:
                st.warning(w)

    t1, t2 = st.columns(2)
    with t1:
        if st.button("📤 TEST DISCORD", use_container_width=True):
            if not discord_url:
                st.warning("Enter Discord webhook URL.")
            else:
                sample = preview or {
                    "available": True,
                    "event_name": "TEST EVENT",
                    "generated_at": "test",
                    "risk_summary": "Dry-run test alert.",
                    "bankroll": config.INITIAL_BANKROLL,
                    "singles_count": 1,
                    "parlays_count": 0,
                    "singles": [
                        {
                            "fight": "Fighter A vs Fighter B",
                            "pick": "Fighter A",
                            "prob": 0.62,
                            "edge_pct": 8.5,
                            "suggested_stake": 15.0,
                            "reasoning": "Test: striking accuracy edge.",
                        }
                    ],
                    "parlays": [],
                    "warnings": [],
                }
                ok = send_discord_alert(sample, discord_url, dry_run=alert_dry)
                st.success("Sent!" if ok and not alert_dry else "Dry-run logged / sent.")
    with t2:
        if st.button("📤 TEST TELEGRAM", use_container_width=True):
            if not tg_token or not tg_chat:
                st.warning("Enter Telegram token and chat ID.")
            else:
                sample = preview or {
                    "available": True,
                    "event_name": "TEST EVENT",
                    "generated_at": "test",
                    "risk_summary": "Dry-run test alert.",
                    "bankroll": config.INITIAL_BANKROLL,
                    "singles": [
                        {
                            "fight": "Fighter A vs Fighter B",
                            "pick": "Fighter A",
                            "prob": 0.62,
                            "edge_pct": 8.5,
                            "suggested_stake": 15.0,
                            "reasoning": "Test alert.",
                        }
                    ],
                    "parlays": [],
                    "warnings": [],
                }
                ok = send_telegram_alert(sample, tg_token, tg_chat, dry_run=alert_dry)
                st.success("Sent!" if ok and not alert_dry else "Dry-run logged / sent.")

    if preview and (discord_url or (tg_token and tg_chat)):
        if st.button("🚀 SEND LIVE ALERT", use_container_width=True):
            status = dispatch_alerts(
                preview,
                discord=bool(discord_url),
                telegram=bool(tg_token and tg_chat),
                dry_run=alert_dry,
                respect_cooldown=not alert_dry,
                discord_webhook=discord_url or None,
                telegram_token=tg_token or None,
                telegram_chat_id=tg_chat or None,
            )
            if status.get("skipped"):
                st.info(f"Skipped: {status.get('skip_reason')}")
            elif status.get("sent") or alert_dry:
                st.success("Alert dispatched.")
            else:
                st.error("Dispatch failed — check logs.")

    st.divider()
    st.markdown("#### Watch mode (CLI)")
    st.code(
        "python main.py --watch --odds --explain --alerts --discord --telegram",
        language="bash",
    )
    st.caption(
        f"Polls every {config.ALERT_POLL_MINUTES}m; only alerts when bet fingerprint changes. "
        "Add `--dry-run` to test without sending."
    )

with tab_backtest:
    st.markdown("### 2025 EVENT BACKTEST")
    st.caption(
        "Walk-forward by event: imputer fit on all fights before each card, "
        "frozen model scores 2025 bouts only."
    )
    if st.button("▶ RUN 2025 BACKTEST", use_container_width=True):
        if not _model_ready():
            st.warning("Train a model first.")
        elif not _features_ready():
            st.warning("Build features first (↻ REFRESH DATA).")
        else:
            try:
                with st.spinner("Running 2025 walk-forward backtest…"):
                    bt2025 = backtest_2025(load_processed_features())
                    st.session_state.backtest_2025 = bt2025
                st.success(f"Saved → {bt2025.report_csv}")
            except ValueError as exc:
                st.warning(str(exc))
            except Exception as exc:
                st.error(str(exc))

    bt2025 = st.session_state.get("backtest_2025")
    if bt2025 is not None:
        om = bt2025.overall_metrics
        c1, c2, c3, c4 = st.columns(4)
        c1.metric(f"{bt2025.target_year} Accuracy", f"{om.get('accuracy', 0):.1%}")
        c2.metric("Log loss", f"{om.get('log_loss', 0):.3f}")
        c3.metric("Brier", f"{om.get('brier_score', 0):.3f}")
        c4.metric("Events", len(bt2025.per_event))

        staking = getattr(bt2025, "staking_modes", pd.DataFrame())
        if isinstance(staking, pd.DataFrame) and not staking.empty:
            st.markdown("#### Staking modes")
            show = staking.copy()
            if "roi_pct" in show.columns:
                show["roi_pct"] = show["roi_pct"].map(lambda x: f"{x:.1f}%")
            if "hit_rate" in show.columns:
                show["hit_rate"] = show["hit_rate"].map(lambda x: f"{x:.1%}")
            if "max_drawdown_pct" in show.columns:
                show["max_drawdown_pct"] = show["max_drawdown_pct"].map(lambda x: f"{x:.1f}%")
            mc_cols = [c for c in show.columns if c.startswith("mc_")]
            for c in mc_cols:
                if "pct" in c or "drawdown" in c:
                    show[c] = show[c].map(lambda x: f"{float(x):.1f}%" if pd.notna(x) else "—")
                elif "ruin" in c or "prob" in c:
                    show[c] = show[c].map(lambda x: f"{float(x):.1%}" if pd.notna(x) else "—")
            st.dataframe(show, use_container_width=True, hide_index=True)

        mc_bt = getattr(bt2025, "monte_carlo", None)
        if mc_bt is not None:
            st.session_state.monte_carlo_hist = mc_bt
            st.markdown("#### Monte Carlo summary")
            mc_rows = []
            for mode, stats in mc_bt.staking_summaries.items():
                mc_rows.append(
                    {
                        "Mode": mode,
                        "Exp max DD": f"{stats.get('expected_max_drawdown_pct', 0):.1f}%",
                        "VaR DD": f"{stats.get('var_max_drawdown_pct', 0):.1f}%",
                        "Ruin": f"{stats.get('ruin_probability', 0):.1%}",
                    }
                )
            st.dataframe(pd.DataFrame(mc_rows), use_container_width=True, hide_index=True)
            for w in getattr(mc_bt, "warnings", []) or []:
                st.warning(w)

        seg = bt2025.segment_metrics
        st.markdown(
            f"**Main events** {seg.get('main_events', {}).get('accuracy', 0):.1%} · "
            f"**Title fights** {seg.get('title_fights', {}).get('accuracy', 0):.1%} · "
            f"**Undercard** {seg.get('undercard', {}).get('accuracy', 0):.1%}"
        )
        if not bt2025.per_event.empty:
            st.dataframe(bt2025.per_event, use_container_width=True, hide_index=True)
        cal_png = config.PLOTS_DIR / f"calibration_{bt2025.target_year}.png"
        roi_png = config.PLOTS_DIR / f"roi_flat_{bt2025.target_year}.png"
        p1, p2 = st.columns(2)
        with p1:
            if cal_png.is_file():
                st.image(str(cal_png), caption="2025 calibration")
        with p2:
            if roi_png.is_file():
                st.image(str(roi_png), caption="2025 flat-stake ROI")
        if not bt2025.threshold_sweep_flat.empty:
            st.line_chart(
                bt2025.threshold_sweep_flat.set_index("min_edge")[["roi_pct"]].rename(
                    columns={"roi_pct": "Flat ROI %"}
                )
            )
    elif config.BACKTEST_2025_CSV.is_file():
        st.caption(f"Last run on disk: `{config.BACKTEST_2025_CSV}`")
    else:
        st.caption("No 2025 backtest yet. Requires 2025 fights in historical data.")

    st.divider()
    st.markdown("### FULL BACKTEST REPORT")
    bt_data = st.session_state.backtest
    if bt_data is None:
        cached = _cached_backtest()
        if cached:
            bt_data = cached

    saved_summary = load_backtest_summary()

    if bt_data is not None:
        if hasattr(bt_data, "classification"):
            cls = bt_data.classification
            summ = bt_data.summary
            sweep = bt_data.threshold_sweep
            wf_metrics = bt_data.walk_forward_metrics
        else:
            cls = bt_data["classification"]
            summ = bt_data["summary"]
            sweep = pd.DataFrame(bt_data.get("threshold_sweep", []))
            wf_metrics = {}

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Hold-out Acc", f"{cls.get('accuracy', 0):.1%}")
        c2.metric("Precision", f"{cls.get('precision', 0):.1%}")
        c3.metric("Recall", f"{cls.get('recall', 0):.1%}")
        c4.metric("Bet ROI", f"{summ.get('roi_pct', 0):.1f}%")

        st.markdown(
            f"**Log loss** {cls.get('log_loss', 0):.3f} · "
            f"**Brier** {cls.get('brier_score', 0):.3f} · "
            f"**AUC** {cls.get('roc_auc', float('nan')):.3f} · "
            f"**Trades** {int(summ.get('trades', 0))} · "
            f"**Hit rate** {summ.get('hit_rate', 0):.1%}"
        )
        if wf_metrics:
            st.caption(
                f"Walk-forward ({int(wf_metrics.get('n_fights', 0))} fights): "
                f"acc {wf_metrics.get('accuracy', 0):.1%}, "
                f"log loss {wf_metrics.get('log_loss', 0):.3f}"
            )

        plot_l, plot_r = st.columns(2)
        with plot_l:
            if config.BACKTEST_CALIBRATION_PNG.is_file():
                st.image(str(config.BACKTEST_CALIBRATION_PNG), caption="Calibration (reliability)")
            elif hasattr(bt_data, "calibration_bins") and not bt_data.calibration_bins.empty:
                st.line_chart(
                    bt_data.calibration_bins.set_index("mean_predicted")["fraction_positive"]
                )
        with plot_r:
            if config.BACKTEST_ROI_PNG.is_file():
                st.image(str(config.BACKTEST_ROI_PNG), caption="ROI by edge threshold")
            elif isinstance(sweep, pd.DataFrame) and not sweep.empty:
                st.line_chart(sweep.set_index("min_edge")["roi_pct"])

        if isinstance(sweep, pd.DataFrame) and not sweep.empty:
            st.markdown("#### Threshold sweep")
            st.dataframe(
                sweep.rename(
                    columns={
                        "min_edge": "Min edge",
                        "roi_pct": "ROI %",
                        "hit_rate": "Hit rate",
                        "trades": "Trades",
                        "avg_yield_pct": "Avg yield %",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )

        if hasattr(bt_data, "metrics_by_year") and not bt_data.metrics_by_year.empty:
            st.markdown("#### Metrics by year")
            st.dataframe(bt_data.metrics_by_year, use_container_width=True, hide_index=True)

        if hasattr(bt_data, "importance_timeline") and not bt_data.importance_timeline.empty:
            st.markdown("#### Feature importance over time")
            st.dataframe(bt_data.importance_timeline, use_container_width=True, hide_index=True)

        if bt_data.report_dir or config.BACKTEST_DIR.is_dir():
            st.caption(f"Report files: `{bt_data.report_dir or config.BACKTEST_DIR}`")
    elif saved_summary:
        c1, c2, c3 = st.columns(3)
        c1.metric("Accuracy", f"{saved_summary.get('accuracy', 0):.1%}")
        c2.metric("Log loss", f"{saved_summary.get('log_loss', 0):.3f}")
        c3.metric("ROI", f"{saved_summary.get('roi_pct', 0):.1f}%")
        if config.BACKTEST_CALIBRATION_PNG.is_file():
            st.image(str(config.BACKTEST_CALIBRATION_PNG))
        st.caption(f"Loaded saved report from `{config.BACKTEST_DIR}`")
    else:
        st.caption("Run **▶ RETRAIN** to generate walk-forward backtest report.")

    st.markdown("### FEATURE IMPORTANCE (global)")
    importance = _load_importance()
    if importance:
        imp_df = (
            pd.DataFrame(list(importance.items()), columns=["feature", "importance"])
            .query("importance > 0")
            .sort_values("importance", ascending=True)
            .tail(12)
        )
        if not imp_df.empty:
            st.bar_chart(imp_df.set_index("feature")["importance"])

# ── Footer ───────────────────────────────────────────────────────────────────
st.divider()
st.caption(
    "UFC Predictor · data → features → LightGBM → calibration → edge backtest · "
    + (
        "Odds API: connected"
        if config.ODDS_API_KEY
        else "Odds API: set THE_ODDS_API_KEY in .env for live implied probs"
    )
)
