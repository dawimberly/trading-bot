"""Optional fight-news sentiment scores for upcoming matchups."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

import config
from src.data_loader import ensure_data_dirs

logger = logging.getLogger(__name__)

_POSITIVE = {
    "dominant", "sharp", "improved", "ready", "confident", "momentum", "winner",
    "elite", "dangerous", "explosive", "accurate", "aggressive", "focused",
    "camp", "prepared", "peak", "form", "favorite", "impressive",
}
_NEGATIVE = {
    "injury", "injured", "doubt", "concern", "struggle", "loss", "suspended",
    "weight", "missed", "cut", "layoff", "rusty", "underdog", "trouble",
    "damaged", "surgery", "illness", "withdraw", "replacement",
}

_CACHE_PATH = config.CACHE_DIR / "sentiment_cache.json"


def _load_cache() -> dict[str, Any]:
    if not _CACHE_PATH.is_file():
        return {"fighters": {}, "updated_at": None}
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"fighters": {}, "updated_at": None}


def _save_cache(cache: dict[str, Any]) -> None:
    ensure_data_dirs()
    cache["updated_at"] = datetime.now(timezone.utc).isoformat()
    _CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def _lexicon_score(text: str) -> float:
    """Rule-based sentiment in [-1, 1] from headline/commentary text."""
    if not text or not str(text).strip():
        return 0.0
    tokens = set(re.findall(r"[a-z']+", str(text).lower()))
    pos = len(tokens.intersection(_POSITIVE))
    neg = len(tokens.intersection(_NEGATIVE))
    if pos == 0 and neg == 0:
        return 0.0
    raw = (pos - neg) / max(pos + neg, 1)
    return float(np.clip(raw, -1.0, 1.0))


def _fetch_newsapi_headlines(fighter_name: str, *, max_articles: int = 8) -> list[str]:
    """Pull recent headlines from NewsAPI when configured."""
    if not config.NEWS_API_KEY:
        return []
    query = f'"{fighter_name}" AND (UFC OR MMA OR fight)'
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": query,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": max_articles,
        "apiKey": config.NEWS_API_KEY,
    }
    try:
        resp = requests.get(url, params=params, timeout=config.REQUEST_TIMEOUT_SEC)
        resp.raise_for_status()
        articles = resp.json().get("articles", [])
        texts = []
        for art in articles:
            title = str(art.get("title", "")).strip()
            desc = str(art.get("description", "")).strip()
            if title:
                texts.append(f"{title}. {desc}".strip())
        return texts
    except requests.RequestException as exc:
        logger.debug("NewsAPI request failed for %s: %s", fighter_name, exc)
        return []


def score_fighter_sentiment(
    fighter_name: str,
    *,
    force_refresh: bool = False,
    cache_ttl_hours: int | None = None,
) -> float:
    """
    Recent commentary sentiment for a fighter in [-1, 1].

    Uses NewsAPI when ``NEWS_API_KEY`` is set; otherwise returns 0 (neutral).
    Results are cached on disk.
    """
    name = str(fighter_name).strip()
    if not name:
        return 0.0

    ttl = cache_ttl_hours if cache_ttl_hours is not None else config.SENTIMENT_CACHE_TTL_HOURS
    cache = _load_cache()
    key = name.lower()
    cached = cache.get("fighters", {}).get(key)
    if cached and not force_refresh:
        try:
            age_h = (
                datetime.now(timezone.utc)
                - datetime.fromisoformat(cached["fetched_at"].replace("Z", "+00:00"))
            ).total_seconds() / 3600
            if age_h < ttl:
                return float(cached["score"])
        except (TypeError, ValueError):
            pass

    headlines = _fetch_newsapi_headlines(name)
    if headlines:
        scores = [_lexicon_score(t) for t in headlines]
        score = float(np.mean(scores))
        source = "newsapi"
    else:
        score = 0.0
        source = "neutral"

    cache.setdefault("fighters", {})[key] = {
        "score": score,
        "source": source,
        "headline_count": len(headlines),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_cache(cache)
    logger.debug("Sentiment %s: %.3f (%s, %s headlines)", name, score, source, len(headlines))
    time.sleep(0.1)
    return score


def attach_sentiment_features(
    features: pd.DataFrame,
    *,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Add ``sentiment_f1``, ``sentiment_f2``, ``sentiment_diff`` columns."""
    out = features.copy()
    f1_col = "fighter_1" if "fighter_1" in out.columns else "fighter1"
    f2_col = "fighter_2" if "fighter_2" in out.columns else "fighter2"

    if f1_col not in out.columns or f2_col not in out.columns:
        out["sentiment_f1"] = 0.0
        out["sentiment_f2"] = 0.0
        out["sentiment_diff"] = 0.0
        return out

    s1, s2 = [], []
    for _, row in out.iterrows():
        s1.append(score_fighter_sentiment(str(row[f1_col]), force_refresh=force_refresh))
        s2.append(score_fighter_sentiment(str(row[f2_col]), force_refresh=force_refresh))
    out["sentiment_f1"] = s1
    out["sentiment_f2"] = s2
    out["sentiment_diff"] = out["sentiment_f1"] - out["sentiment_f2"]
    return out
