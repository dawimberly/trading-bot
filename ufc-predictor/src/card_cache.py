"""Per-event feature + prediction cache for fast repeat analysis."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

import config

logger = logging.getLogger(__name__)

EVENT_CACHE_DIR = config.CACHE_DIR / "event_analysis"
HISTORY_META_PATH = config.CACHE_DIR / "history_features_meta.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _file_fingerprint(path: Path) -> str:
    if not path.is_file():
        return "missing"
    stat = path.stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def fights_source_fingerprint() -> str:
    return _file_fingerprint(config.RAW_FIGHTS_CSV)


def _slug(text: str) -> str:
    clean = "".join(c if c.isalnum() else "_" for c in str(text).strip().lower())
    return clean[:60] or "event"


def event_cache_dir(event_name: str, fight_ids: list[str]) -> Path:
    key = hashlib.sha1("|".join(sorted(fight_ids)).encode()).hexdigest()[:12]
    return EVENT_CACHE_DIR / f"{_slug(event_name)}_{key}"


def _series_values(card: pd.DataFrame, column: str, default: list[Any] | None = None) -> list[Any]:
    if column not in card.columns:
        return list(default or [])
    vals = card[column]
    if hasattr(vals, "tolist"):
        return vals.tolist()
    if isinstance(vals, list):
        return vals
    return list(vals)


def _card_fight_ids(card: pd.DataFrame) -> list[str]:
    """Stable fight ids for cache keys; synthesize from fighters when fight_id is absent."""
    id_col = config.FIGHT_ID_COLUMN if config.FIGHT_ID_COLUMN in card.columns else "fight_id"
    ids = [str(x) for x in _series_values(card, id_col) if str(x).strip()]
    if ids:
        return sorted(ids)
    f1_col = "fighter_1" if "fighter_1" in card.columns else ("fighter1" if "fighter1" in card.columns else None)
    f2_col = "fighter_2" if "fighter_2" in card.columns else ("fighter2" if "fighter2" in card.columns else None)
    if not f1_col or not f2_col:
        return []
    date_col = config.DATE_COLUMN if config.DATE_COLUMN in card.columns else ("date" if "date" in card.columns else None)
    dates = _series_values(card, date_col, [""] * len(card)) if date_col else [""] * len(card)
    return sorted(f"{a}|{b}|{d}" for a, b, d in zip(card[f1_col], card[f2_col], dates))


def card_fingerprint(card: pd.DataFrame) -> dict[str, Any]:
    ids = _card_fight_ids(card)
    dates = []
    if config.DATE_COLUMN in card.columns:
        dates = sorted(str(x) for x in _series_values(card, config.DATE_COLUMN))
    return {
        "fight_ids": ids,
        "dates": dates,
        "n_fights": len(ids),
        "fights_fp": fights_source_fingerprint(),
        "event_name": str(card.get("event_name", card.get("event", "")).iloc[0] if len(card) else ""),
    }


def _meta_path(cache_dir: Path) -> Path:
    return cache_dir / "meta.json"


def _features_path(cache_dir: Path) -> Path:
    return cache_dir / "features.parquet"


def _predictions_path(cache_dir: Path) -> Path:
    return cache_dir / "predictions.parquet"


def load_event_cache(
    event_name: str,
    card: pd.DataFrame,
) -> dict[str, Any] | None:
    """Return cached bundle if fingerprint matches current card + fights source."""
    fp = card_fingerprint(card)
    if not fp["fight_ids"]:
        return None
    cache_dir = event_cache_dir(event_name, fp["fight_ids"])
    meta_path = _meta_path(cache_dir)
    pred_path = _predictions_path(cache_dir)
    if not meta_path.is_file() or not pred_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if meta.get("fingerprint") != fp:
        return None
    try:
        preds = pd.read_parquet(pred_path)
        features = pd.DataFrame()
        feat_path = _features_path(cache_dir)
        if feat_path.is_file():
            features = pd.read_parquet(feat_path)
    except Exception as exc:
        logger.debug("Event cache read failed: %s", exc)
        return None
    logger.info("Event cache hit: %s (%s fights)", event_name, len(preds))
    return {
        "meta": meta,
        "predictions": preds,
        "features": features,
        "cache_dir": cache_dir,
        "from_cache": True,
    }


def save_event_cache(
    event_name: str,
    card: pd.DataFrame,
    *,
    predictions: pd.DataFrame,
    features: pd.DataFrame | None = None,
    explain: bool = False,
) -> Path:
    fp = card_fingerprint(card)
    cache_dir = event_cache_dir(event_name, fp["fight_ids"])
    cache_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "event_name": event_name,
        "fingerprint": fp,
        "explain": explain,
        "saved_at": _utc_now(),
        "n_predictions": len(predictions),
    }
    _meta_path(cache_dir).write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    predictions.to_parquet(_predictions_path(cache_dir), index=False)
    if features is not None and not features.empty:
        features.to_parquet(_features_path(cache_dir), index=False)
    logger.info("Event cache saved: %s", cache_dir)
    return cache_dir


def split_new_fights(
    card: pd.DataFrame,
    cached_fight_ids: set[str],
) -> pd.DataFrame:
    """Return card rows whose fight_id is not already cached."""
    if config.FIGHT_ID_COLUMN not in card.columns:
        return card
    mask = ~card[config.FIGHT_ID_COLUMN].astype(str).isin(cached_fight_ids)
    return card.loc[mask].copy()


def merge_cached_predictions(
    cached: pd.DataFrame,
    new_preds: pd.DataFrame,
) -> pd.DataFrame:
    """Combine cached predictions with newly featurized fights."""
    if cached.empty:
        return new_preds
    if new_preds.empty:
        return cached
    key = config.FIGHT_ID_COLUMN
    if key not in cached.columns or key not in new_preds.columns:
        return pd.concat([cached, new_preds], ignore_index=True)
    old_ids = set(cached[key].astype(str))
    fresh = new_preds[~new_preds[key].astype(str).isin(old_ids)]
    return pd.concat([cached, fresh], ignore_index=True)


def invalidate_event_caches() -> int:
    """Clear all event caches (e.g. after fights.csv refresh)."""
    if not EVENT_CACHE_DIR.is_dir():
        return 0
    count = 0
    for path in EVENT_CACHE_DIR.iterdir():
        if path.is_dir():
            for f in path.iterdir():
                f.unlink(missing_ok=True)
            path.rmdir()
            count += 1
    return count


def history_cache_valid() -> bool:
    if not HISTORY_META_PATH.is_file():
        return False
    try:
        meta = json.loads(HISTORY_META_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return meta.get("fights_fp") == fights_source_fingerprint()


def touch_history_cache() -> None:
    HISTORY_META_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_META_PATH.write_text(
        json.dumps(
            {"fights_fp": fights_source_fingerprint(), "updated_at": _utc_now()},
            indent=2,
        ),
        encoding="utf-8",
    )


def predict_card_cached(
    card: pd.DataFrame,
    fights: pd.DataFrame,
    event_name: str,
    *,
    explain: bool = False,
    use_cache: bool = True,
    progress: Callable[[str, float | None], None] | None = None,
    step_pct: float = 0.0,
    step_span: float = 0.0,
) -> pd.DataFrame:
    """Score a card using per-event cache; only featurize new fights when card grows."""
    from src.predictor import FightPredictor, build_card_features

    def _log(msg: str, pct: float | None = None) -> None:
        if progress:
            progress(msg, pct)

    cached_bundle = load_event_cache(event_name, card) if use_cache else None
    from src.model_cache import get_shared_predictor

    predictor = get_shared_predictor()

    if cached_bundle and cached_bundle["meta"].get("explain") == explain:
        _log(f"Cache hit: {event_name}", step_pct + step_span)
        preds = cached_bundle["predictions"].copy()
        preds["event_name"] = event_name
        return preds

    partial_preds = pd.DataFrame()
    cached_ids: set[str] = set()
    if cached_bundle:
        partial_preds = cached_bundle["predictions"]
        if config.FIGHT_ID_COLUMN in partial_preds.columns:
            cached_ids = set(partial_preds[config.FIGHT_ID_COLUMN].astype(str))
        _log(f"Incremental: {len(cached_ids)} cached, featurizing new fights…", step_pct)

    new_card = split_new_fights(card, cached_ids) if cached_ids else card
    if new_card.empty and not partial_preds.empty:
        return partial_preds

    _log(f"Feature engineering: {event_name}…", step_pct + step_span * 0.2)
    features = build_card_features(card if not cached_ids else new_card, historical_fights=fights)
    logger.info("build_card_features: %d rows for %r", len(features), event_name)
    if features.empty:
        if not partial_preds.empty:
            return partial_preds
        raise ValueError(f"No features for {event_name}")

    _log(f"Model inference: {event_name}…", step_pct + step_span * 0.7)
    scored = predictor.predict_batch(features, explain=explain)
    if config.FIGHT_ID_COLUMN in card.columns:
        meta_cols = [
            c for c in (config.FIGHT_ID_COLUMN, "event_name", config.DATE_COLUMN) if c in card.columns
        ]
        scored = scored.merge(
            card[meta_cols].drop_duplicates(config.FIGHT_ID_COLUMN),
            on=config.FIGHT_ID_COLUMN,
            how="left",
        )

    preds = merge_cached_predictions(partial_preds, scored)
    preds["event_name"] = event_name

    if use_cache:
        save_event_cache(event_name, card, predictions=preds, features=features, explain=explain)

    _log(f"Done: {event_name}", step_pct + step_span)
    return preds
