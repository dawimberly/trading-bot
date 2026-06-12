"""Live web mood: daily cached headline sentiment (free, no Tavily)."""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

import requests

import config
from modules.sentiment_keywords import score_text_sentiment

ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = ROOT / config.WEB_SENTIMENT_CACHE_FILE
USER_AGENT = "PythonTradingBot/1.0"
FETCH_URLS = (
    "https://finance.yahoo.com/",
    "https://www.cnn.com/business",
)


def _strip_html(html: str) -> str:
    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.I | re.S)
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.I | re.S)
    html = re.sub(r"<[^>]+>", " ", html)
    return " ".join(html.split())


def _fetch_page_sentiment(url: str) -> tuple[float, int, str]:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    text = _strip_html(resp.text)
    return score_text_sentiment(text), len(text), text[:2500]


def _load_cache() -> dict | None:
    if not CACHE_PATH.exists():
        return None
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(payload: dict) -> None:
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _cache_fresh(cached: dict) -> bool:
    ts = cached.get("fetched_at")
    if not ts:
        return False
    fetched = datetime.datetime.fromisoformat(ts)
    age = datetime.datetime.now() - fetched
    return age.total_seconds() < config.WEB_SENTIMENT_CACHE_HOURS * 3600


def get_live_web_sentiment(force_refresh: bool = False) -> float | None:
    """
    Return [-1, 1] web sentiment from finance headlines.
    Cached for WEB_SENTIMENT_CACHE_HOURS (default 24h).
    """
    if not force_refresh:
        cached = _load_cache()
        if cached and _cache_fresh(cached):
            return float(cached["sentiment"])

    scores: list[float] = []
    sources: list[dict] = []
    for url in FETCH_URLS:
        try:
            score, chars, snippet = _fetch_page_sentiment(url)
            scores.append(score)
            sources.append(
                {
                    "url": url,
                    "sentiment": score,
                    "text_chars": chars,
                    "headline_text": snippet,
                }
            )
        except requests.RequestException as exc:
            sources.append({"url": url, "error": str(exc)})

    if not scores:
        cached = _load_cache()
        if cached and "sentiment" in cached:
            return float(cached["sentiment"])
        return None

    sentiment = round(sum(scores) / len(scores), 4)
    headline_text = " ".join(
        str(s.get("headline_text") or "") for s in sources if isinstance(s, dict)
    )[:4000]
    _save_cache(
        {
            "fetched_at": datetime.datetime.now().isoformat(),
            "sentiment": sentiment,
            "headline_text": headline_text,
            "sources": sources,
        }
    )
    return sentiment
