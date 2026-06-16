"""
Persistent per-fighter rolling stats cache for fast card inference.

Stores long-format history (rolling, Greco, similar-opponent) and Elo state so
upcoming-card feature engineering only recomputes fighters on the card.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

import config
from src.card_cache import fights_source_fingerprint
from src.data_loader import ensure_data_dirs
from src.feature_engineering import (
    _assemble_wide_feature_matrix,
    _build_history_long_pipeline,
    _canonicalize_fighter_slots,
    _compute_elo_state,
    _fighter_history,
    _recompute_long_stats,
    elo_lookup_for_fights,
)
from src.safe_io import read_json_file, write_json_atomic

logger = logging.getLogger(__name__)

FIGHTER_HISTORY_PATH = config.CACHE_DIR / "fighter_history_long.parquet"
FIGHTER_ELO_PATH = config.CACHE_DIR / "fighter_elo.parquet"
FIGHTER_CACHE_META_PATH = config.CACHE_DIR / "fighter_cache_meta.json"
CACHE_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def labeled_fights(fights: pd.DataFrame) -> pd.DataFrame:
    """Rows with a recorded winner (completed bouts only)."""
    work = _canonicalize_fighter_slots(fights)
    if "winner" not in work.columns:
        return work.iloc[0:0].copy()
    winners = work["winner"].fillna("").astype(str).str.strip()
    return work[winners != ""].copy()


def _read_meta() -> dict[str, Any]:
    if not FIGHTER_CACHE_META_PATH.is_file():
        return {}
    return read_json_file(FIGHTER_CACHE_META_PATH)


def is_cache_valid() -> bool:
    """True when on-disk cache matches the current fights.csv fingerprint."""
    meta = _read_meta()
    if not meta:
        return False
    if meta.get("version") != CACHE_VERSION:
        return False
    if meta.get("fights_fp") != fights_source_fingerprint():
        return False
    return FIGHTER_HISTORY_PATH.is_file() and FIGHTER_ELO_PATH.is_file()


def load_fighter_cache() -> tuple[pd.DataFrame, pd.DataFrame, dict[str, float]]:
    """Load cached long history, historical Elo table, and post-history ratings."""
    meta = _read_meta()
    history = pd.read_parquet(FIGHTER_HISTORY_PATH)
    elo = pd.read_parquet(FIGHTER_ELO_PATH)
    ratings = {str(k): float(v) for k, v in (meta.get("elo_ratings") or {}).items()}
    if config.DATE_COLUMN in history.columns:
        history[config.DATE_COLUMN] = pd.to_datetime(
            history[config.DATE_COLUMN], errors="coerce"
        )
    return history, elo, ratings


def _save_fighter_cache(
    history: pd.DataFrame,
    elo: pd.DataFrame,
    ratings: dict[str, float],
    *,
    n_fights: int,
) -> None:
    ensure_data_dirs()
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    history.to_parquet(FIGHTER_HISTORY_PATH, index=False)
    elo.to_parquet(FIGHTER_ELO_PATH, index=False)
    write_json_atomic(
        FIGHTER_CACHE_META_PATH,
        {
            "version": CACHE_VERSION,
            "fights_fp": fights_source_fingerprint(),
            "updated_at": _utc_now(),
            "n_fights": n_fights,
            "n_rows": len(history),
            "elo_ratings": ratings,
        },
    )
    logger.info(
        "Fighter cache saved (%s fights, %s long rows)",
        n_fights,
        len(history),
    )


def build_full_cache(fights: pd.DataFrame) -> None:
    """Full rebuild of per-fighter rolling stats from labeled fights."""
    labeled = labeled_fights(fights)
    if labeled.empty:
        logger.warning("Fighter cache build skipped — no labeled fights")
        return

    logger.info("Building fighter cache from %s labeled fights…", len(labeled))
    history = _build_history_long_pipeline(labeled)
    elo, ratings = _compute_elo_state(history)
    _save_fighter_cache(history, elo, ratings, n_fights=len(labeled))


def ensure_fighter_cache(fights: pd.DataFrame | None = None) -> None:
    """Build cache when missing or stale."""
    if is_cache_valid():
        return
    if fights is None:
        from src.data_loader import load_fights

        fights = load_fights()
    build_full_cache(fights)


def warm_fighter_cache() -> None:
    """Pre-warm fighter cache (background runner / dashboard startup)."""
    from src.data_loader import load_fights

    ensure_fighter_cache(load_fights())


def _card_fighters(fights: pd.DataFrame) -> set[str]:
    fighters: set[str] = set()
    for col in ("fighter_1", "fighter_2"):
        if col in fights.columns:
            fighters.update(fights[col].dropna().astype(str).tolist())
    return fighters


def extend_history_for_card(
    cached_long: pd.DataFrame,
    upcoming_fights: pd.DataFrame,
) -> pd.DataFrame:
    """
    Append upcoming card rows and recompute rolling stats only for card fighters.
    """
    upcoming = _canonicalize_fighter_slots(upcoming_fights)
    new_long = _fighter_history(upcoming)
    if new_long.empty:
        return cached_long

    card_ids = set(upcoming[config.FIGHT_ID_COLUMN].astype(str).tolist())
    affected = _card_fighters(upcoming)

    base = cached_long[
        ~cached_long[config.FIGHT_ID_COLUMN].astype(str).isin(card_ids)
    ].copy()
    affected_hist = base[base["fighter"].isin(affected)].copy()
    unaffected = base[~base["fighter"].isin(affected)].copy()

    combined = pd.concat([affected_hist, new_long], ignore_index=True)
    combined = combined.drop_duplicates(
        subset=[config.FIGHT_ID_COLUMN, "fighter", "side"],
        keep="last",
    )
    combined = combined.sort_values(
        ["fighter", config.DATE_COLUMN, "side"]
    ).reset_index(drop=True)

    recomputed = _recompute_long_stats(combined)
    return pd.concat([unaffected, recomputed], ignore_index=True)


def build_features_with_cache(
    fights: pd.DataFrame,
    *,
    target_fight_ids: set[str],
    keep_unlabeled: bool = True,
) -> pd.DataFrame:
    """
    Feature matrix for upcoming card fights using persisted fighter history.
    """
    fights = _canonicalize_fighter_slots(fights)
    labeled = labeled_fights(fights)
    ensure_fighter_cache(labeled if not labeled.empty else fights)

    cached_long, _elo_hist, ratings = load_fighter_cache()

    upcoming = fights[
        fights[config.FIGHT_ID_COLUMN].astype(str).isin(
            {str(x) for x in target_fight_ids}
        )
    ].copy()
    if upcoming.empty:
        logger.warning("Fighter cache path: no upcoming rows for target fight ids")
        return pd.DataFrame()

    extended = extend_history_for_card(cached_long, upcoming)
    elo = elo_lookup_for_fights(upcoming, ratings)

    logger.info(
        "Fighter cache inference: %s card fights, %s fighters recomputed",
        len(upcoming),
        len(_card_fighters(upcoming)),
    )

    return _assemble_wide_feature_matrix(
        extended,
        elo,
        keep_unlabeled=keep_unlabeled,
        target_fight_ids={str(x) for x in target_fight_ids},
    )
