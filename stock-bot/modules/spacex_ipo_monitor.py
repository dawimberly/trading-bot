"""SpaceX IPO ↔ crypto headline monitor (free RSS; no Tavily).

SpaceX S-1 disclosed ~18,712 BTC on the balance sheet — crypto markets often
front-run mega IPOs with corporate BTC treasuries. This module tracks that
narrative separately from macro web sentiment.
"""

from __future__ import annotations

import datetime
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

import config
from modules.sentiment_keywords import (
    SPACEX_CRYPTO_LINK,
    SPACEX_IPO_TOPICS,
    SPCX_PERP_TOPICS,
    spacex_crypto_relevance,
)

ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = ROOT / config.SPACEX_IPO_CACHE_FILE
HISTORY_PATH = ROOT / config.SPACEX_IPO_HISTORY_FILE
USER_AGENT = "PythonTradingBot/1.0"

# Google News RSS — SpaceX IPO + Bitcoin / treasury angle
RSS_FEEDS = (
    "https://news.google.com/rss/search?q=SpaceX+IPO+Bitcoin&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=SpaceX+S-1+Bitcoin+treasury&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=SpaceX+IPO+crypto&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=SpaceX+SPCX+Hyperliquid+whale&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=SPCX+token+pre-IPO+perp&hl=en-US&gl=US&ceid=US:en",
)


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


def _append_history(payload: dict) -> None:
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")


def _cache_fresh(cached: dict) -> bool:
    ts = cached.get("fetched_at")
    if not ts:
        return False
    fetched = datetime.datetime.fromisoformat(ts)
    age = datetime.datetime.now() - fetched
    return age.total_seconds() < config.SPACEX_IPO_CACHE_HOURS * 3600


def _parse_rss(xml_text: str, source: str) -> list[dict]:
    headlines: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return headlines

    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        desc = (item.findtext("description") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", desc)
        pub = (item.findtext("pubDate") or "").strip()
        link = (item.findtext("link") or "").strip()
        body = f"{title}. {desc}"
        rel = spacex_crypto_relevance(body)
        if rel["spacex_hits"] == 0 and not rel["spcx_perp"]:
            continue
        headlines.append(
            {
                "title": title,
                "published": pub,
                "link": link,
                "source_feed": source,
                "spacex_hits": rel["spacex_hits"],
                "crypto_hits": rel["crypto_hits"],
                "spcx_perp_hits": rel["spcx_perp_hits"],
                "btc_linked": rel["linked"],
                "spcx_perp": rel["spcx_perp"],
                "sentiment": rel["sentiment"],
            }
        )
    return headlines


def _fetch_feed(url: str) -> tuple[list[dict], str | None]:
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        resp.raise_for_status()
        return _parse_rss(resp.text, url), None
    except requests.RequestException as exc:
        return [], str(exc)


def _summarize(headlines: list[dict]) -> dict:
    if not headlines:
        return {
            "headline_count": 0,
            "btc_linked_count": 0,
            "spcx_perp_count": 0,
            "avg_sentiment": 0.0,
            "narrative": "quiet",
            "top_headlines": [],
            "top_spcx_perp": [],
        }

    linked = [h for h in headlines if h["btc_linked"]]
    perp = [h for h in headlines if h["spcx_perp"]]
    sentiments = [h["sentiment"] for h in headlines]
    avg = round(sum(sentiments) / len(sentiments), 4)
    btc_linked_count = len(linked)
    spcx_perp_count = len(perp)

    if btc_linked_count >= config.SPACEX_IPO_ALERT_HEADLINES:
        narrative = "hot_btc_narrative"
    elif spcx_perp_count >= config.SPACEX_CRYPTO_OVERRIDE_MIN_SPCX_PERP:
        narrative = "spcx_perp_active"
    elif btc_linked_count >= 1:
        narrative = "btc_tied"
    elif len(headlines) >= config.SPACEX_IPO_ALERT_HEADLINES:
        narrative = "ipo_active"
    else:
        narrative = "watching"

    ranked = sorted(headlines, key=lambda h: (h["btc_linked"], h["crypto_hits"]), reverse=True)
    perp_ranked = sorted(perp, key=lambda h: h["spcx_perp_hits"], reverse=True)
    return {
        "headline_count": len(headlines),
        "btc_linked_count": btc_linked_count,
        "spcx_perp_count": spcx_perp_count,
        "avg_sentiment": avg,
        "narrative": narrative,
        "top_headlines": ranked[:5],
        "top_spcx_perp": perp_ranked[:3],
    }


def get_spacex_ipo_monitor(force_refresh: bool = False) -> dict | None:
    """
    Return SpaceX IPO ↔ crypto monitor snapshot.
    Cached for SPACEX_IPO_CACHE_HOURS (default 6h).
    """
    if not config.SPACEX_IPO_MONITOR_ENABLED:
        return None

    if not force_refresh:
        cached = _load_cache()
        if cached and _cache_fresh(cached):
            return cached

    all_headlines: list[dict] = []
    feeds: list[dict] = []
    seen_titles: set[str] = set()

    for url in RSS_FEEDS:
        items, err = _fetch_feed(url)
        feeds.append({"url": url, "count": len(items), "error": err})
        for h in items:
            key = h["title"].lower()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            all_headlines.append(h)

    summary = _summarize(all_headlines)
    alert = summary["btc_linked_count"] >= config.SPACEX_IPO_ALERT_HEADLINES

    payload = {
        "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "enabled": True,
        "context": {
            "spacex_btc_disclosed": 18712,
            "ticker": "SPCX",
            "spcx_perp_note": (
                "SPCX-USDC on Hyperliquid is a synthetic pre-IPO perp — "
                "not SpaceX equity; Alpaca trades BTC pairs as narrative proxy"
            ),
            "note": "S-1 BTC treasury + SPCX perp/whale headlines vs BTC sleeve",
        },
        "summary": summary,
        "alert": alert,
        "feeds": feeds,
        "keywords": {
            "spacex": list(SPACEX_IPO_TOPICS),
            "crypto_link": list(SPACEX_CRYPTO_LINK),
            "spcx_perp": list(SPCX_PERP_TOPICS),
        },
    }

    if all_headlines or not _load_cache():
        _save_cache(payload)
        _append_history(
            {
                "at": payload["fetched_at"],
                "headline_count": summary["headline_count"],
                "btc_linked_count": summary["btc_linked_count"],
                "spcx_perp_count": summary["spcx_perp_count"],
                "avg_sentiment": summary["avg_sentiment"],
                "narrative": summary["narrative"],
                "alert": alert,
            }
        )
        return payload

    cached = _load_cache()
    return cached


def format_monitor_line(snapshot: dict | None) -> str:
    if not snapshot:
        return "SpaceX IPO monitor: off"
    s = snapshot.get("summary", {})
    return (
        f"SpaceX IPO: {s.get('narrative', 'n/a')} | "
        f"{s.get('headline_count', 0)} headlines | "
        f"{s.get('btc_linked_count', 0)} BTC-linked | "
        f"{s.get('spcx_perp_count', 0)} SPCX-perp | "
        f"sent {s.get('avg_sentiment', 0):+.2f}"
    )
