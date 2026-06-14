"""Central configuration for paths, model hyperparameters, and feature settings."""

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Standalone layout: .env at project root or ufc_betting_bot/.env
ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env", override=False)
load_dotenv(ROOT_DIR / "ufc_betting_bot" / ".env", override=False)

# --- Paths ---
DATA_DIR = ROOT_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
CACHE_DIR = DATA_DIR / "cache"
MODELS_DIR = ROOT_DIR / "models"

RAW_FIGHTS_CSV = RAW_DIR / "fights.csv"
PROCESSED_FEATURES_CSV = PROCESSED_DIR / "fight_features.csv"
DEFAULT_MODEL_PATH = MODELS_DIR / "ensemble_winner.joblib"
LEGACY_MODEL_PATH = MODELS_DIR / "lgbm_winner.joblib"
METRICS_PATH = MODELS_DIR / "training_metrics.json"
FEATURE_IMPORTANCE_PATH = MODELS_DIR / "feature_importance.json"
BACKTEST_DIR = MODELS_DIR / "backtest"
BACKTEST_SUMMARY_CSV = BACKTEST_DIR / "backtest_summary.csv"
BACKTEST_PREDICTIONS_CSV = BACKTEST_DIR / "walk_forward_predictions.csv"
BACKTEST_THRESHOLD_CSV = BACKTEST_DIR / "threshold_roi.csv"
BACKTEST_IMPORTANCE_CSV = BACKTEST_DIR / "importance_timeline.csv"
BACKTEST_METRICS_BY_YEAR_CSV = BACKTEST_DIR / "metrics_by_year.csv"
BACKTEST_CALIBRATION_PNG = BACKTEST_DIR / "calibration_plot.png"
BACKTEST_ROI_PNG = BACKTEST_DIR / "roi_threshold_plot.png"
PLOTS_DIR = DATA_DIR / "plots"
BACKTEST_2025_CSV = DATA_DIR / "backtest_2025_results.csv"
BACKTEST_2025_YEAR = int(os.getenv("BACKTEST_2025_YEAR", "2025"))

# --- Data ---
UFC_STATS_BASE_URL = os.getenv(
    "UFC_STATS_BASE_URL", "http://ufcstats.com/statistics/events/completed?page=all"
)
UFC_STATS_UPCOMING_URL = os.getenv(
    "UFC_STATS_UPCOMING_URL", "http://ufcstats.com/statistics/events/upcoming"
)
UFC_EVENTS_URL = os.getenv("UFC_EVENTS_URL", "https://www.ufc.com/events")
ESPN_UFC_SCOREBOARD_URL = os.getenv(
    "ESPN_UFC_SCOREBOARD_URL",
    "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard",
)
HISTORICAL_DATA_URL = os.getenv(
    "HISTORICAL_DATA_URL",
    "https://raw.githubusercontent.com/jansen88/ufc-data/main/data/complete_ufc_data.csv",
)
HF_UFC_DATASET = os.getenv("HF_UFC_DATASET", "JesterLabs/UFC_FIGHT_DATA")
HF_UFC_SPLIT = os.getenv("HF_UFC_SPLIT", "train")
HF_UFC_PAGE_SIZE = int(os.getenv("HF_UFC_PAGE_SIZE", "100"))

REQUEST_TIMEOUT_SEC = int(os.getenv("REQUEST_TIMEOUT_SEC", "30"))
REQUEST_DELAY_SEC = float(os.getenv("REQUEST_DELAY_SEC", "1.0"))
CACHE_TTL_HOURS = int(os.getenv("CACHE_TTL_HOURS", "24"))

# Canonical fights.csv columns (user-facing)
FIGHTS_COLUMNS = [
    "fight_id",
    "event",
    "date",
    "location",
    "fighter1",
    "fighter2",
    "winner",
    "weight_class",
    "method",
    "round",
    "time",
    "is_title_fight",
    "is_main_event",
    "sig_strikes_landed_f1",
    "sig_strikes_attempted_f1",
    "sig_strikes_landed_f2",
    "sig_strikes_attempted_f2",
    "takedowns_landed_f1",
    "takedowns_attempted_f1",
    "takedowns_landed_f2",
    "takedowns_attempted_f2",
    "finish",
    "f1_odds",
    "f2_odds",
    "source",
]

HISTORICAL_META_PATH = CACHE_DIR / "historical_meta.json"
UPCOMING_CARD_CACHE = CACHE_DIR / "upcoming_card.csv"
LOCAL_KAGGLE_CANDIDATES = [
    RAW_DIR / "ufc-master.csv",
    RAW_DIR / "complete_ufc_data.csv",
    RAW_DIR / "raw_total_fight_data.csv",
    RAW_DIR / "data.csv",
]
KAGGLE_UFC_BETTING_ODDS_SLUG = os.getenv(
    "KAGGLE_UFC_BETTING_ODDS_SLUG",
    "jerzyszocik/ufc-betting-odds-daily-dataset",
)
KAGGLE_ODDS_DIR = RAW_DIR / "kaggle" / "ufc-betting-odds-daily-dataset"
LOCAL_ODDS_CANDIDATES = [
    KAGGLE_ODDS_DIR / "ufc-master.csv",
    KAGGLE_ODDS_DIR / "data.csv",
    KAGGLE_ODDS_DIR / "ufc_betting_odds.csv",
    RAW_DIR / "ufc_betting_odds_daily.csv",
    RAW_DIR / "ufc-master.csv",
    RAW_DIR / "cleaned_odds.csv",
    RAW_DIR / "complete_ufc_data.csv",
    RAW_DIR / "ufc_odds.csv",
]
ULTIMATE_UFC_DATASET_URL = os.getenv(
    "ULTIMATE_UFC_DATASET_URL",
    "https://raw.githubusercontent.com/shortlikeafox/ultimate_ufc_dataset/main/ufc-master.csv",
)
JANSEN_CLEANED_ODDS_URL = os.getenv(
    "JANSEN_CLEANED_ODDS_URL",
    "https://raw.githubusercontent.com/jansen88/ufc-data/master/data/cleaned_odds.csv",
)
JANSEN_COMPLETE_URL = os.getenv(
    "JANSEN_COMPLETE_URL",
    "https://raw.githubusercontent.com/jansen88/ufc-data/main/data/complete_ufc_data.csv",
)
HISTORICAL_ODDS_CACHE = CACHE_DIR / "historical_odds_unified.csv"
ODDS_API_CACHE_PATH = CACHE_DIR / "ufc_odds_api.csv"

# --- UFCstats / Greco1899 enrichment (fighter profiles + career stats) ---
GRECO_UFCSTATS_BASE_URL = os.getenv(
    "GRECO_UFCSTATS_BASE_URL",
    "https://raw.githubusercontent.com/Greco1899/scrape_ufc_stats/main/",
)
UFCSTATS_GRECO_CACHE_DIR = CACHE_DIR / "ufcstats_greco"
UFCSTATS_ENRICH_META_PATH = CACHE_DIR / "ufcstats_enrich_meta.json"
UFCSTATS_ENRICH_TTL_HOURS = int(os.getenv("UFCSTATS_ENRICH_TTL_HOURS", "12"))

FIGHTS_ENRICHMENT_COLUMNS = [
    "fighter1_height",
    "fighter2_height",
    "fighter1_reach",
    "fighter2_reach",
    "fighter1_dob",
    "fighter2_dob",
    "fighter1_stance",
    "fighter2_stance",
    "fighter1_sig_strikes_landed_pm",
    "fighter2_sig_strikes_landed_pm",
    "fighter1_sig_strikes_accuracy",
    "fighter2_sig_strikes_accuracy",
    "fighter1_takedown_accuracy",
    "fighter2_takedown_accuracy",
    "fighter1_takedown_defence",
    "fighter2_takedown_defence",
    "fighter1_submission_avg_attempted_per15m",
    "fighter2_submission_avg_attempted_per15m",
]
FIGHTS_SAVE_COLUMNS = list(
    dict.fromkeys(FIGHTS_COLUMNS + FIGHTS_ENRICHMENT_COLUMNS)
)

# --- Features ---
ROLLING_FIGHTS = int(os.getenv("ROLLING_FIGHTS", "5"))
MIN_FIGHTS_PER_FIGHTER = int(os.getenv("MIN_FIGHTS_PER_FIGHTER", "3"))

FEATURE_COLUMNS = [
    # Differential (primary signals — fighter1 minus fighter2)
    "elo_diff",
    "win_rate_diff",
    "last5_winrate_diff",
    "momentum_diff",
    "striking_acc_diff",
    "takedown_acc_diff",
    "sub_avg_diff",
    "ko_rate_diff",
    "sig_strikes_per_min_diff",
    "td_defense_diff",
    "control_time_diff",
    "age_diff",
    "height_diff",
    "reach_diff",
    "stance_matchup",
    "southpaw_advantage",
    "striker_score_diff",
    "grappler_score_diff",
    "striker_vs_grappler",
    "style_clash",
    "days_since_last_fight_diff",
    "experience_diff",
    # Optional news sentiment (0 when API disabled)
    "sentiment_diff",
    # Context
    "is_title_fight",
    "is_main_event",
    "scheduled_rounds",
]

TARGET_COLUMN = "f1_win"
DATE_COLUMN = "event_date"
FIGHT_ID_COLUMN = "fight_id"
# Expected mean(TARGET) after canonical fighter slots (alphabetical f1/f2)
TARGET_MEAN_MIN = float(os.getenv("TARGET_MEAN_MIN", "0.48"))
TARGET_MEAN_MAX = float(os.getenv("TARGET_MEAN_MAX", "0.62"))

# --- Model (ensemble: LightGBM + XGBoost) ---
RANDOM_STATE = int(os.getenv("RANDOM_STATE", "42"))
TEST_SIZE = float(os.getenv("TEST_SIZE", "0.2"))
CALIBRATION_SIZE = float(os.getenv("CALIBRATION_SIZE", "0.15"))
CALIBRATION_METHOD = os.getenv("CALIBRATION_METHOD", "isotonic")  # isotonic | sigmoid
USE_ENSEMBLE = os.getenv("USE_ENSEMBLE", "true").lower() in ("1", "true", "yes")
TUNE_ON_TRAIN = os.getenv("TUNE_ON_TRAIN", "false").lower() in ("1", "true", "yes")
OPTUNA_TRIALS = int(os.getenv("OPTUNA_TRIALS", "50"))
CONFORMAL_ALPHA = float(os.getenv("CONFORMAL_ALPHA", "0.10"))
UNCERTAINTY_HIGH_WIDTH = float(os.getenv("UNCERTAINTY_HIGH_WIDTH", "0.22"))
WF_MIN_TRAIN_RATIO = float(os.getenv("WF_MIN_TRAIN_RATIO", "0.60"))
WF_IMPORTANCE_INTERVAL = int(os.getenv("WF_IMPORTANCE_INTERVAL", "400"))
STYLE_BONUS_MAX = float(os.getenv("STYLE_BONUS_MAX", "0.05"))
EDGE_RANK_MIN = float(os.getenv("EDGE_RANK_MIN", "0.0"))
RUN_BACKTEST_ON_TRAIN = os.getenv("RUN_BACKTEST_ON_TRAIN", "true").lower() in (
    "1",
    "true",
    "yes",
)
LGBM_PARAMS = {
    "objective": "binary",
    "metric": "binary_logloss",
    "boosting_type": "gbdt",
    "num_leaves": 31,
    "learning_rate": 0.05,
    "feature_fraction": 0.9,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "verbose": -1,
    "random_state": RANDOM_STATE,
    "n_estimators": int(os.getenv("LGBM_N_ESTIMATORS", "300")),
}
XGB_PARAMS = {
    "objective": "binary:logistic",
    "eval_metric": "logloss",
    "max_depth": 6,
    "learning_rate": 0.05,
    "subsample": 0.8,
    "colsample_bytree": 0.85,
    "reg_lambda": 1.0,
    "random_state": RANDOM_STATE,
    "n_estimators": int(os.getenv("XGB_N_ESTIMATORS", "300")),
    "verbosity": 0,
}
DEFAULT_ENSEMBLE_WEIGHTS = [
    float(x)
    for x in os.getenv("ENSEMBLE_WEIGHTS", "0.55,0.45").split(",")
    if x.strip()
]

# --- Sentiment / news (optional) ---
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")
SENTIMENT_CACHE_TTL_HOURS = int(os.getenv("SENTIMENT_CACHE_TTL_HOURS", "12"))
ATTACH_SENTIMENT_ON_INFERENCE = os.getenv(
    "ATTACH_SENTIMENT_ON_INFERENCE", "true"
).lower() in ("1", "true", "yes")

# --- Backtest ---
INITIAL_BANKROLL = float(os.getenv("INITIAL_BANKROLL", "1000"))
FLAT_STAKE = float(os.getenv("FLAT_STAKE", "10"))
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.03"))  # model prob minus implied prob
EDGE_THRESHOLDS = [
    float(x)
    for x in os.getenv("EDGE_THRESHOLDS", "0,0.02,0.03,0.05,0.08,0.10").split(",")
    if x.strip()
]

# --- Monte Carlo risk analysis ---
MC_SIMULATIONS = int(os.getenv("MC_SIMULATIONS", "10000"))
MC_CARD_SIMULATIONS = int(os.getenv("MC_CARD_SIMULATIONS", "5000"))
MC_CONFIDENCE_LEVEL = float(os.getenv("MC_CONFIDENCE_LEVEL", "0.95"))
MC_RUIN_THRESHOLD_FRACTION = float(os.getenv("MC_RUIN_THRESHOLD_FRACTION", "0.5"))
MC_MAX_CARD_RISK_FRACTION = float(os.getenv("MC_MAX_CARD_RISK_FRACTION", "0.08"))
MC_MIN_CARD_RISK_FRACTION = float(os.getenv("MC_MIN_CARD_RISK_FRACTION", "0.02"))
MC_MAX_BET_FRACTION = float(os.getenv("MC_MAX_BET_FRACTION", "0.02"))
MC_MIN_BET_FRACTION = float(os.getenv("MC_MIN_BET_FRACTION", "0.005"))
MC_HIGH_DRAWDOWN_WARN_PCT = float(os.getenv("MC_HIGH_DRAWDOWN_WARN_PCT", "25"))
MC_HIGH_RUIN_WARN_PROB = float(os.getenv("MC_HIGH_RUIN_WARN_PROB", "0.05"))
MC_ROLLING_CARD_WINDOW = int(os.getenv("MC_ROLLING_CARD_WINDOW", "3"))

# --- Inference ---
CONFIDENCE_HIGH = float(os.getenv("CONFIDENCE_HIGH", "0.65"))
CONFIDENCE_MEDIUM = float(os.getenv("CONFIDENCE_MEDIUM", "0.58"))

# --- The Odds API (https://the-odds-api.com) ---
# Set THE_ODDS_API_KEY or ODDS_API_KEY in .env — never commit the real key.
ODDS_API_KEY = os.getenv("THE_ODDS_API_KEY") or os.getenv("ODDS_API_KEY", "")
ODDS_API_BASE_URL = os.getenv("ODDS_API_BASE_URL", "https://api.the-odds-api.com/v4")
ODDS_API_SPORT = os.getenv("ODDS_API_SPORT", "mma_mixed_martial_arts")
ODDS_API_REGIONS = os.getenv("ODDS_API_REGIONS", "us")
ODDS_API_MARKETS = os.getenv("ODDS_API_MARKETS", "h2h")
ODDS_API_PROP_MARKETS = os.getenv("ODDS_API_PROP_MARKETS", "totals")
BETNOW_PROPS_URL = os.getenv(
    "BETNOW_PROPS_URL",
    "https://www.betnow.eu/sportsbook-info/fighting/ufc/",
)
BETNOW_COOKIE = os.getenv("BETNOW_COOKIE", "")
MYBOOKIE_ENABLED = os.getenv("MYBOOKIE_ENABLED", "true").lower() in ("1", "true", "yes")
MYBOOKIE_UFC_URL = os.getenv("MYBOOKIE_UFC_URL", "https://www.mybookie.ag/sportsbook/ufc/")
MYBOOKIE_PROPS_URL = os.getenv("MYBOOKIE_PROPS_URL", "https://www.mybookie.ag/sportsbook/ufc/props/")
MYBOOKIE_COOKIE = os.getenv("MYBOOKIE_COOKIE", "")
ODDS_API_ODDS_FORMAT = os.getenv("ODDS_API_ODDS_FORMAT", "decimal")  # decimal | american
ODDS_CACHE_PATH = CACHE_DIR / "ufc_odds_api.csv"
ODDS_CACHE_TTL_HOURS = int(os.getenv("ODDS_CACHE_TTL_HOURS", "1"))

# --- Live alerts (Discord / Telegram) ---
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ALERT_MIN_EDGE = float(os.getenv("ALERT_MIN_EDGE", "0.04"))
ALERT_MIN_PARLAY_EV = float(os.getenv("ALERT_MIN_PARLAY_EV", "0.08"))
ALERT_PARLAY_MIN_EDGE = float(os.getenv("ALERT_PARLAY_MIN_EDGE", "0.03"))
ALERT_PARLAY_MIN_COMBINED_PROB = float(os.getenv("ALERT_PARLAY_MIN_COMBINED_PROB", "0.25"))
ALERT_PARLAY_MAX_LEGS = int(os.getenv("ALERT_PARLAY_MAX_LEGS", "3"))
ALERT_MAX_PARLAYS = int(os.getenv("ALERT_MAX_PARLAYS", "5"))
ALERT_COOLDOWN_MINUTES = int(os.getenv("ALERT_COOLDOWN_MINUTES", "60"))
ALERT_POLL_MINUTES = int(os.getenv("ALERT_POLL_MINUTES", "45"))
ALERT_DRY_RUN = os.getenv("ALERT_DRY_RUN", "false").lower() in ("1", "true", "yes")
ALERT_BOT_NAME = os.getenv("ALERT_BOT_NAME", "UFC Predictor")
ALERT_REQUEST_TIMEOUT_SEC = int(os.getenv("ALERT_REQUEST_TIMEOUT_SEC", "15"))
ALERT_STATE_PATH = CACHE_DIR / "alert_state.json"

# --- Ops / production (ported from trading bot) ---
LOG_DIR = DATA_DIR / "logs"
BET_JOURNAL_CSV = DATA_DIR / "bet_journal.csv"
HEARTBEAT_PATH = CACHE_DIR / "heartbeat.json"
CIRCUIT_BREAKER_STATE_PATH = CACHE_DIR / "circuit_breaker_state.json"
DRAWDOWN_STATE_PATH = CACHE_DIR / "drawdown_state.json"
RISK_EVENTS_LOG = LOG_DIR / "risk_events.log"

# --- Profile: live (conservative) vs research (default) ---
UFC_PROFILE = os.getenv("UFC_PROFILE", "research").strip().lower()

_PROFILE_LIVE = {
    "max_card_risk_fraction": float(os.getenv("LIVE_MAX_CARD_RISK", "0.05")),
    "max_bet_fraction": float(os.getenv("LIVE_MAX_BET_FRACTION", "0.015")),
    "daily_loss_limit_fraction": float(os.getenv("LIVE_DAILY_LOSS_LIMIT", "0.02")),
    "max_drawdown_fraction": float(os.getenv("LIVE_MAX_DRAWDOWN", "0.10")),
    "resume_drawdown_fraction": float(os.getenv("LIVE_RESUME_DRAWDOWN", "0.08")),
    "alert_min_edge": float(os.getenv("LIVE_ALERT_MIN_EDGE", "0.08")),
    "parlay_min_edge": float(os.getenv("LIVE_PARLAY_MIN_EDGE", "0.07")),
    "parlay_min_combined_prob": float(os.getenv("LIVE_PARLAY_MIN_COMBINED_PROB", "0.35")),
    "parlay_min_ev": float(os.getenv("LIVE_PARLAY_MIN_EV", "0.15")),
    "kelly_fraction": float(os.getenv("LIVE_KELLY_FRACTION", "0.20")),
}

_PROFILE_RESEARCH = {
    "max_card_risk_fraction": MC_MAX_CARD_RISK_FRACTION,
    "max_bet_fraction": MC_MAX_BET_FRACTION,
    "daily_loss_limit_fraction": float(os.getenv("RESEARCH_DAILY_LOSS_LIMIT", "0.04")),
    "max_drawdown_fraction": float(os.getenv("RESEARCH_MAX_DRAWDOWN", "0.15")),
    "resume_drawdown_fraction": float(os.getenv("RESEARCH_RESUME_DRAWDOWN", "0.12")),
    "alert_min_edge": float(os.getenv("RESEARCH_ALERT_MIN_EDGE", "0.04")),
    "parlay_min_edge": float(os.getenv("RESEARCH_PARLAY_MIN_EDGE", "0.03")),
    "parlay_min_combined_prob": float(os.getenv("RESEARCH_PARLAY_MIN_COMBINED_PROB", "0.25")),
    "parlay_min_ev": float(os.getenv("RESEARCH_PARLAY_MIN_EV", "0.08")),
    "kelly_fraction": float(os.getenv("RESEARCH_KELLY_FRACTION", "0.25")),
}

CIRCUIT_BREAKER_ENABLED = os.getenv("CIRCUIT_BREAKER_ENABLED", "true").lower() in ("1", "true", "yes")
DRAWDOWN_HALT_ENABLED = os.getenv("DRAWDOWN_HALT_ENABLED", "true").lower() in ("1", "true", "yes")
DYNAMIC_THRESHOLDS_ENABLED = os.getenv("UFC_DYNAMIC_THRESHOLDS", "true").lower() in (
    "1",
    "true",
    "yes",
)

# --- Dashboard / watch intervals (minutes) ---
WATCH_CARD_CHECK_MINUTES = int(os.getenv("UFC_WATCH_CARD_CHECK_MINUTES", "45"))
WATCH_AUTO_ODDS_MINUTES = int(os.getenv("UFC_WATCH_AUTO_ODDS_MINUTES", "12"))
DASHBOARD_AUTO_ODDS_MINUTES = int(os.getenv("UFC_DASHBOARD_AUTO_ODDS_MINUTES", "12"))
DASHBOARD_CARD_CHECK_MINUTES = int(os.getenv("UFC_DASHBOARD_CARD_CHECK_MINUTES", "45"))

# --- Prop betting (method, rounds, decision) ---
ENABLE_PROPS = os.getenv("ENABLE_PROPS", "false").lower() in ("1", "true", "yes")
PROP_MIN_EDGE = float(os.getenv("PROP_MIN_EDGE", "0.05"))
PROP_MIN_MODEL_PROB = float(os.getenv("PROP_MIN_MODEL_PROB", "0.30"))
PROP_SYNTHETIC_VIG = float(os.getenv("PROP_SYNTHETIC_VIG", "0.08"))
PROP_PARLAY_MIN_EV = float(os.getenv("PROP_PARLAY_MIN_EV", "0.08"))
PROP_PARLAY_MIN_COMBINED_PROB = float(os.getenv("PROP_PARLAY_MIN_COMBINED_PROB", "0.20"))
PROP_PARLAY_MAX_LEGS_DK = int(os.getenv("PROP_PARLAY_MAX_LEGS_DK", "3"))
PROP_CORRELATION_DISCOUNT = float(os.getenv("PROP_CORRELATION_DISCOUNT", "0.12"))
PROP_MARKETS = [
    x.strip()
    for x in os.getenv(
        "PROP_MARKETS",
        "goes_to_decision,finish,ko_tko,submission,round_1_finish,over_1_5_rounds,fighter_ko,fighter_sub",
    ).split(",")
    if x.strip()
]
BOOK_PROP_RULES: dict[str, dict[str, Any]] = {
    "BetNow.eu": {
        "allow_prop_parlays": False,
        "allow_mixed_parlays": False,
        "max_prop_parlay_legs": 1,
    },
    "DraftKings": {
        "allow_prop_parlays": True,
        "allow_mixed_parlays": True,
        "max_prop_parlay_legs": PROP_PARLAY_MAX_LEGS_DK,
    },
    "MyBookie": {
        "allow_prop_parlays": True,
        "allow_mixed_parlays": True,
        "max_prop_parlay_legs": PROP_PARLAY_MAX_LEGS_DK,
    },
}


def is_live_profile() -> bool:
    return UFC_PROFILE == "live"


def profile_settings() -> dict[str, float]:
    return dict(_PROFILE_LIVE if is_live_profile() else _PROFILE_RESEARCH)


def profile_value(key: str) -> float:
    return profile_settings()[key]


def apply_profile_overrides() -> None:
    """Apply profile caps to module-level defaults (call at startup)."""
    global ALERT_MIN_EDGE, MC_MAX_CARD_RISK_FRACTION, MC_MAX_BET_FRACTION
    global ALERT_MIN_PARLAY_EV, ALERT_PARLAY_MIN_EDGE, ALERT_PARLAY_MIN_COMBINED_PROB
    ps = profile_settings()
    ALERT_MIN_EDGE = ps["alert_min_edge"]
    ALERT_PARLAY_MIN_EDGE = ps["parlay_min_edge"]
    ALERT_PARLAY_MIN_COMBINED_PROB = ps["parlay_min_combined_prob"]
    ALERT_MIN_PARLAY_EV = ps["parlay_min_ev"]
    MC_MAX_CARD_RISK_FRACTION = ps["max_card_risk_fraction"]
    MC_MAX_BET_FRACTION = ps["max_bet_fraction"]

