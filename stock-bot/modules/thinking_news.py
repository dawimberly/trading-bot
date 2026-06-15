"""Scheduled market headlines for paper Thinking Engine (8 AM / 6 PM ET)."""

from __future__ import annotations

import datetime
import json
import os
import re
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import config

ROOT = Path(__file__).resolve().parents[1]
SCHEDULE_STATE_FILE = ROOT / "thinking_news_schedule.json"
NEWS_CACHE_FILE = ROOT / "thinking_news_cache.json"
MANUAL_NEWS_FILE = ROOT / "thinking_news_manual.txt"
ET = ZoneInfo("America/New_York")

# Lightweight Google News RSS (no API key)
RSS_FEEDS = (
    "https://news.google.com/rss/search?q=stock+market+S%26P+500+Federal+Reserve&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=Trump+tariff+oil+markets&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=geopolitical+Middle+East+markets&hl=en-US&gl=US&ceid=US:en",
)

SLOT_LABELS = {
    "premarket": "8:00 AM ET pre-market",
    "postmarket": "6:00 PM ET post-close",
}
SLOT_HOUR_ET = {"premarket": 8, "postmarket": 18}

_run_lock = threading.Lock()
_bg_running = False


def news_enabled() -> bool:
    if os.getenv("THINKING_NEWS_ENABLED", "true").lower() in ("0", "false", "no"):
        return False
    return bool(config.PAPER_TRADING or config.paper_aggressive_context())


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def normalize_news_headlines(news_headlines: str | list | None) -> str:
    if news_headlines is None:
        return ""
    if isinstance(news_headlines, str):
        lines = [ln.strip() for ln in news_headlines.splitlines() if ln.strip()]
    else:
        lines = [str(x).strip() for x in news_headlines if str(x).strip()]
    return "\n".join(lines[:12])


def _manual_headlines() -> list[str]:
    env = os.getenv("THINKING_NEWS_MANUAL", "").strip()
    if env:
        return [ln.strip() for ln in env.split("|") if ln.strip()]
    if MANUAL_NEWS_FILE.is_file():
        try:
            text = MANUAL_NEWS_FILE.read_text(encoding="utf-8")
            return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        except OSError:
            pass
    return []


def _parse_rss(xml_text: str, source: str, *, max_items: int) -> list[dict]:
    out: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        desc = re.sub(r"<[^>]+>", " ", (item.findtext("description") or "")).strip()
        out.append(
            {
                "title": title[:280],
                "snippet": desc[:200] if desc else "",
                "source_feed": source,
            }
        )
        if len(out) >= max_items:
            break
    return out


def fetch_market_headlines(*, max_items: int = 8, timeout: float = 8.0) -> list[dict]:
    """Fetch headlines from RSS; returns cached copy if fresh (<30 min)."""
    cached = _read_json(NEWS_CACHE_FILE)
    ts = cached.get("fetched_at")
    if ts:
        try:
            age = (
                datetime.datetime.now(datetime.timezone.utc)
                - datetime.datetime.fromisoformat(str(ts))
            ).total_seconds()
            if age < 1800 and cached.get("headlines"):
                return list(cached["headlines"])
        except (TypeError, ValueError):
            pass

    headlines: list[dict] = []
    try:
        import requests

        headers = {"User-Agent": "PythonTradingBot/1.0"}
        for url in RSS_FEEDS:
            if len(headlines) >= max_items:
                break
            try:
                resp = requests.get(url, timeout=timeout, headers=headers)
                resp.raise_for_status()
                headlines.extend(
                    _parse_rss(resp.text, url, max_items=max_items - len(headlines))
                )
            except (requests.RequestException, OSError):
                continue
    except ImportError:
        pass

    _write_json(
        NEWS_CACHE_FILE,
        {
            "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "headlines": headlines[:max_items],
        },
    )
    return headlines[:max_items]


def get_news_for_thinking(*, max_items: int = 8) -> str:
    """RSS headlines + optional manual lines for the thinking prompt."""
    manual = _manual_headlines()
    fetched = fetch_market_headlines(max_items=max_items)
    lines: list[str] = []
    seen: set[str] = set()
    for item in manual + fetched:
        if isinstance(item, dict):
            title = str(item.get("title") or "").strip()
        else:
            title = str(item).strip()
        key = title.lower()
        if not title or key in seen:
            continue
        seen.add(key)
        lines.append(title)
        if len(lines) >= max_items:
            break
    return normalize_news_headlines(lines)


def format_news_summary(news_text: str, *, slot: str | None = None) -> str:
    label = SLOT_LABELS.get(slot or "", slot or "manual")
    body = news_text.strip() or "(no headlines fetched — add thinking_news_manual.txt or THINKING_NEWS_MANUAL)"
    return f"[{label}]\n{body}"


def _schedule_state() -> dict:
    return _read_json(SCHEDULE_STATE_FILE)


def _save_schedule_state(state: dict) -> None:
    _write_json(SCHEDULE_STATE_FILE, state)


def due_news_slot(now_et: datetime.datetime | None = None) -> str | None:
    """Return premarket/postmarket slot due today (max 2/day), or None."""
    now_et = now_et or datetime.datetime.now(ET)
    today = now_et.date().isoformat()
    state = _schedule_state()
    if state.get("date") != today:
        state = {"date": today, "runs": [], "in_progress": None}
    runs = set(state.get("runs") or [])
    if state.get("in_progress"):
        return None
    if len(runs) >= 2:
        return None
    hour = now_et.hour
    if hour >= SLOT_HOUR_ET["premarket"] and "premarket" not in runs:
        return "premarket"
    if hour >= SLOT_HOUR_ET["postmarket"] and "postmarket" not in runs:
        return "postmarket"
    return None


def mark_slot_started(slot: str) -> None:
    state = _schedule_state()
    today = datetime.datetime.now(ET).date().isoformat()
    if state.get("date") != today:
        state = {"date": today, "runs": [], "in_progress": None}
    state["in_progress"] = slot
    _save_schedule_state(state)


def mark_slot_completed(slot: str) -> None:
    state = _schedule_state()
    today = datetime.datetime.now(ET).date().isoformat()
    if state.get("date") != today:
        state = {"date": today, "runs": [], "in_progress": None}
    runs = list(state.get("runs") or [])
    if slot not in runs:
        runs.append(slot)
    state["runs"] = runs[:2]
    state["in_progress"] = None
    _save_schedule_state(state)


def mark_slot_failed(slot: str) -> None:
    state = _schedule_state()
    if state.get("in_progress") == slot:
        state["in_progress"] = None
        _save_schedule_state(state)


def maybe_run_scheduled_news_thinking(
    data,
    regime: str,
    vol: str,
    wisdom: dict | None = None,
) -> str | None:
    """Paper-only: at most 2 scheduled news+LLM runs per day (non-blocking)."""
    global _bg_running
    if not news_enabled():
        return None
    if not config.effective_thinking_engine_enabled():
        return None
    if not config.PAPER_TRADING and not config.paper_aggressive_context():
        return None

    slot = due_news_slot()
    if not slot:
        return None

    with _run_lock:
        if _bg_running:
            return None
        _bg_running = True
        mark_slot_started(slot)

    def _worker() -> None:
        global _bg_running
        try:
            from modules.thinking_engine import run_thinking_with_news

            news_text = get_news_for_thinking()
            run_thinking_with_news(
                data,
                regime,
                vol,
                wisdom=wisdom,
                news_headlines=news_text,
                slot=slot,
                background=False,
            )
            mark_slot_completed(slot)
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "Scheduled news thinking failed (slot=%s)", slot
            )
            mark_slot_failed(slot)
        finally:
            with _run_lock:
                _bg_running = False

    threading.Thread(target=_worker, name=f"thinking-news-{slot}", daemon=True).start()
    return slot
