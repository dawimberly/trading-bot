"""Betting-bot configuration (separate from crypto trading bot)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

ROOT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT_DIR.parent


def _resolve_predictor_dir() -> Path:
    """Find ufc-predictor root: standalone copy, monorepo sibling, or explicit env."""
    explicit = os.getenv("UFC_PREDICTOR_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    canonical = os.getenv("UFC_CANONICAL_ROOT", "").strip()
    if canonical:
        path = Path(canonical).expanduser()
        if (path / "config.py").is_file():
            return path
    # Standalone: ufc_betting_bot/ inside UFC-Predictor project root
    if (REPO_ROOT / "config.py").is_file() and (REPO_ROOT / "src").is_dir():
        return REPO_ROOT
    sibling = REPO_ROOT / "ufc-predictor"
    if (sibling / "config.py").is_file():
        return sibling
    return sibling


PREDICTOR_DIR = _resolve_predictor_dir()

DATA_DIR = ROOT_DIR / "data"
REPORTS_DIR = DATA_DIR / "reports"
PLOTS_DIR = DATA_DIR / "plots"
CACHE_DIR = DATA_DIR / "cache"
ODDS_RAW_DIR = DATA_DIR / "odds_raw"

PREDICTOR_FIGHTS_CSV = PREDICTOR_DIR / "data" / "raw" / "fights.csv"
PREDICTOR_FEATURES_CSV = PREDICTOR_DIR / "data" / "processed" / "fight_features.csv"
PREDICTOR_MODEL_PATH = PREDICTOR_DIR / "models" / "ensemble_winner.joblib"
PREDICTOR_ODDS_CACHE = PREDICTOR_DIR / "data" / "cache" / "ufc_odds_api.csv"

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

KAGGLE_UFC_BETTING_ODDS_SLUG = os.getenv(
    "KAGGLE_UFC_BETTING_ODDS_SLUG",
    "jerzyszocik/ufc-betting-odds-daily-dataset",
)
KAGGLE_ODDS_DIR = PREDICTOR_DIR / "data" / "raw" / "kaggle" / "ufc-betting-odds-daily-dataset"

LOCAL_ODDS_CANDIDATES = [
    KAGGLE_ODDS_DIR / "ufc-master.csv",
    KAGGLE_ODDS_DIR / "data.csv",
    KAGGLE_ODDS_DIR / "ufc_betting_odds.csv",
    PREDICTOR_DIR / "data" / "raw" / "ufc_betting_odds_daily.csv",
    ODDS_RAW_DIR / "ufc-master.csv",
    ODDS_RAW_DIR / "cleaned_odds.csv",
    ODDS_RAW_DIR / "complete_ufc_data.csv",
    PREDICTOR_DIR / "data" / "raw" / "ufc-master.csv",
]

HISTORICAL_ODDS_CACHE = CACHE_DIR / "historical_odds_unified.csv"
BACKTEST_2025_CSV = REPORTS_DIR / "backtest_2025_results.csv"
BANKROLL_STATE_PATH = DATA_DIR / "bankroll_state.json"
LIVE_SIGNALS_CSV = DATA_DIR / "live_signals.csv"

REQUEST_TIMEOUT_SEC = int(os.getenv("UFC_BOT_REQUEST_TIMEOUT", "30"))

ODDS_API_KEY = os.getenv("THE_ODDS_API_KEY") or os.getenv("ODDS_API_KEY", "")

TARGET_COLUMN = "f1_win"
DATE_COLUMN = "event_date"
FIGHT_ID_COLUMN = "fight_id"
BACKTEST_YEAR = int(os.getenv("UFC_BOT_BACKTEST_YEAR", "2025"))

EDGE_THRESHOLDS = [
    float(x)
    for x in os.getenv("UFC_BOT_EDGE_THRESHOLDS", "0.05,0.08,0.10,0.12").split(",")
    if x.strip()
]


@dataclass
class BankrollSettings:
    initial_bankroll: float = float(os.getenv("UFC_BOT_INITIAL_BANKROLL", "1000"))
    kelly_fraction: float = float(os.getenv("UFC_BOT_KELLY_FRACTION", "0.25"))
    max_bet_fraction: float = float(os.getenv("UFC_BOT_MAX_BET_FRACTION", "0.02"))
    min_bet_fraction: float = float(os.getenv("UFC_BOT_MIN_BET_FRACTION", "0.005"))
    max_card_risk_fraction: float = float(os.getenv("UFC_BOT_MAX_CARD_RISK", "0.08"))
    daily_loss_limit_fraction: float = float(
        os.getenv("UFC_BOT_DAILY_LOSS_LIMIT", "0.05")
    )
    min_edge: float = float(os.getenv("UFC_BOT_MIN_EDGE", "0.05"))
    flat_stake: float = float(os.getenv("UFC_BOT_FLAT_STAKE", "10"))


@dataclass
class Settings:
    predictor_dir: Path = PREDICTOR_DIR
    bankroll: BankrollSettings = field(default_factory=BankrollSettings)
    backtest_year: int = BACKTEST_YEAR
    edge_thresholds: list[float] = field(default_factory=lambda: list(EDGE_THRESHOLDS))


def get_settings() -> Settings:
    return Settings()


def ensure_dirs() -> None:
    for path in (DATA_DIR, REPORTS_DIR, PLOTS_DIR, CACHE_DIR, ODDS_RAW_DIR):
        path.mkdir(parents=True, exist_ok=True)
