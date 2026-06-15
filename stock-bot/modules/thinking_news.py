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


_THEME_PATTERNS: dict[str, tuple[str, ...]] = {
    "policy": (
        "trump",
        "tariff",
        "fed",
        "federal reserve",
        "fomc",
        "fiscal",
        "policy",
        "white house",
        "treasury",
        "rate cut",
        "rate hike",
        "inflation",
    ),
    "liquidity": (
        "flood the market",
        "flood market",
        "liquidity",
        "stimulus",
        "injection",
        "ease",
        "easing",
        "dovish",
        "strategic petroleum",
        "spr release",
        "reserve release",
        "qe",
    ),
    "geopolitics": (
        "iran",
        "israel",
        "middle east",
        "hormuz",
        "war",
        "sanctions",
        "geopolitical",
        "missile",
        "ukraine",
        "taiwan",
    ),
    "sector_energy": (
        "oil",
        "energy",
        "opec",
        "gasoline",
        "crude",
        "hormuz",
        "xle",
    ),
    "sector_tech": (
        "ai",
        "nvidia",
        "semiconductor",
        "tech",
        "software",
        "datacenter",
        "cloud",
        "small-cap",
        "qqq",
        "magnificent",
    ),
    "sector_financials": (
        "bank",
        "financial",
        "credit",
        "yields",
        "bond",
        "treasury yield",
    ),
}


def _headline_lines(news_headlines: str | list | None) -> list[str]:
    text = normalize_news_headlines(news_headlines)
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def analyze_news_headlines(
    news_headlines: str | list | None,
    *,
    ai_cycle_phase: str | None = None,
) -> dict[str, Any]:
    """Extract themes + news_impact_score (0-1) from headline text."""
    lines = _headline_lines(news_headlines)
    combined = " ".join(lines).lower()
    themes: dict[str, dict[str, Any]] = {}
    for key, patterns in _THEME_PATTERNS.items():
        matched = [p for p in patterns if p in combined]
        themes[key] = {
            "active": bool(matched),
            "signals": matched[:4],
            "weight": min(1.0, len(matched) * 0.25),
        }

    impact = 0.0
    if lines:
        impact += min(0.25, 0.08 * len(lines))
    for key in ("geopolitics", "policy", "liquidity"):
        if themes[key]["active"]:
            impact += 0.18 * float(themes[key]["weight"])
    for key in ("sector_energy", "sector_tech", "sector_financials"):
        if themes[key]["active"]:
            impact += 0.10 * float(themes[key]["weight"])
    if themes["geopolitics"]["active"] and themes["liquidity"]["active"]:
        impact += 0.12
    if themes["policy"]["active"] and themes["sector_tech"]["active"]:
        impact += 0.08
    impact = round(min(1.0, impact), 2)

    theme_labels = {
        "policy": "Policy / rates / tariffs",
        "liquidity": "Liquidity / stimulus rhetoric",
        "geopolitics": "Geopolitics / supply shock",
        "sector_energy": "Energy / oil sector",
        "sector_tech": "AI / tech / semis",
        "sector_financials": "Financials / rates channel",
    }
    active_bits = [
        f"{theme_labels[k]} ({', '.join(themes[k]['signals'][:2])})"
        for k in theme_labels
        if themes[k]["active"]
    ]
    theme_summary = " | ".join(active_bits) if active_bits else "No dominant headline theme"

    phase = str(ai_cycle_phase or "unknown")
    ai_tech_context = _ai_tech_boom_context(phase, themes, combined)

    digest_lines = [
        f"news_impact_score: {impact:.2f} (0=ignore, 1=strong tilt evidence)",
        f"Themes: {theme_summary}",
        f"AI/tech boom lens: {ai_tech_context}",
        "Headlines:",
    ]
    digest_lines.extend(f"- {ln}" for ln in lines[:8])
    digest_text = "\n".join(digest_lines)

    return {
        "headlines": lines,
        "themes": themes,
        "theme_summary": theme_summary,
        "news_impact_score": impact,
        "ai_tech_context": ai_tech_context,
        "digest_text": digest_text,
    }


def _ai_tech_boom_context(phase: str, themes: dict, combined: str) -> str:
    tech_news = themes.get("sector_tech", {}).get("active")
    geo = themes.get("geopolitics", {}).get("active")
    policy = themes.get("policy", {}).get("active")
    if "mid-cycle" in phase or "ai" in phase.lower():
        if tech_news and policy:
            return (
                "AI/datacenter cycle still leading index gains, but policy/tariff headlines "
                "can whipsaw crowded semis — favor winners, don't chase laggards"
            )
        if tech_news and geo:
            return (
                "AI boom intact in price action, but geopolitical/oil shock threatens "
                "multiple expansion — trim beta, keep VTI core"
            )
        if tech_news:
            return "AI/tech leadership phase — headline flow supports selective SPY/semi tilt vs passive VTI"
    if geo and "small-cap" in combined:
        return "Risk-off geopolitics + small-cap beta warnings — de-risk crowded AI laggards"
    if policy and "tariff" in combined:
        return "Tariff/policy overhang on global supply chains — balance VTI anchor vs active sleeves"
    return f"Cycle phase {phase} — weigh headlines against VTI benchmark, avoid crowded chase"


_CAP_KEYS = ("vti_core", "spy", "crypto", "nyse", "metal", "cash_buffer")
_THINKING_MAX_ACTIVE_SLEEVES = 3
_THINKING_MAX_TOTAL_DELTA = 0.12


def _theme_active(themes: dict, key: str) -> bool:
    block = themes.get(key) if isinstance(themes.get(key), dict) else {}
    return bool(block.get("active"))


def _news_priority_sleeves(summary: dict) -> tuple[str, str, str]:
    """Top-3 custodian sleeves for theme-driven consolidation."""
    themes = summary.get("news_themes") or {}
    geo = _theme_active(themes, "geopolitics")
    energy = _theme_active(themes, "sector_energy")
    liq = _theme_active(themes, "liquidity")
    policy = _theme_active(themes, "policy")
    tech = _theme_active(themes, "sector_tech")
    vix = summary.get("vix")
    vix_f = float(vix) if vix not in (None, "n/a") else 18.0
    oil = float(summary.get("oil_change") or 0.0)

    if geo and (liq or policy) or (oil >= 3.5 and (geo or energy)):
        return ("vti_core", "cash_buffer", "nyse")
    if geo or energy or oil >= 3.0:
        return ("nyse", "vti_core", "cash_buffer")
    if liq and policy and tech:
        return ("spy", "vti_core", "cash_buffer")
    if liq and policy:
        return ("spy", "vti_core", "cash_buffer")
    if vix_f >= 22 or "below MA" in str(summary.get("spy_trend", "")):
        return ("vti_core", "cash_buffer", "nyse")
    return ("vti_core", "spy", "cash_buffer")


def _redirect_cap_to_priority(key: str, top3: tuple[str, ...], summary: dict) -> str:
    if key in top3:
        return key
    themes = summary.get("news_themes") or {}
    geo = _theme_active(themes, "geopolitics")
    liq = _theme_active(themes, "liquidity")
    if key == "metal":
        return "cash_buffer" if "cash_buffer" in top3 else top3[0]
    if key == "crypto":
        if geo and "cash_buffer" in top3:
            return "cash_buffer"
        if "spy" in top3:
            return "spy"
        return top3[0]
    if key == "nyse":
        return "nyse" if "nyse" in top3 else top3[0]
    if key == "spy":
        if liq and "spy" in top3:
            return "spy"
        return "vti_core" if "vti_core" in top3 else top3[0]
    return top3[0]


def _sleeve_rank_score(
    key: str,
    delta: float,
    summary: dict,
    *,
    impact: float,
    top3: tuple[str, ...],
) -> float:
    """Rank sleeves by |delta| scaled by news_impact and theme priority."""
    themes = summary.get("news_themes") or {}
    boost = 1.0
    if key in top3:
        boost += 0.35 * (3 - top3.index(key))
    if key == "nyse" and (_theme_active(themes, "geopolitics") or _theme_active(themes, "sector_energy")):
        boost += 0.45
    if key == "spy" and (_theme_active(themes, "liquidity") or _theme_active(themes, "policy")):
        boost += 0.40
    if key in ("vti_core", "cash_buffer") and (
        _theme_active(themes, "geopolitics") or float(summary.get("vix") or 0) >= 20
    ):
        boost += 0.35
    return abs(float(delta)) * boost * (1.0 + 0.5 * impact)


def _clamp_cap_deltas(
    deltas: dict[str, float],
    *,
    max_per_sleeve: float,
    max_total: float = _THINKING_MAX_TOTAL_DELTA,
) -> dict[str, float]:
    clamped = {
        k: round(max(-max_per_sleeve, min(max_per_sleeve, float(deltas.get(k, 0.0)))), 6)
        for k in _CAP_KEYS
    }
    total = sum(abs(v) for v in clamped.values())
    if total > max_total and total > 0:
        scale = max_total / total
        clamped = {k: round(v * scale, 6) for k, v in clamped.items()}
    return clamped


def consolidate_news_deltas(
    deltas: dict[str, float],
    market_summary: dict | None,
    *,
    max_per_sleeve: float,
    max_sleeves: int = _THINKING_MAX_ACTIVE_SLEEVES,
) -> dict[str, float]:
    """Merge multi-sleeve news tilts into <=3 custodians before live safety guard."""
    if not market_summary:
        return _clamp_cap_deltas(deltas, max_per_sleeve=max_per_sleeve)

    impact = float(market_summary.get("news_impact_score") or 0.0)
    has_news = bool(
        market_summary.get("news_headlines")
        or market_summary.get("news_digest")
        or market_summary.get("news_theme_summary")
    )
    raw = {k: float(deltas.get(k, 0.0)) for k in _CAP_KEYS}
    material = {k: v for k, v in raw.items() if abs(v) >= 0.005}

    if len(material) <= max_sleeves and not (has_news and impact >= 0.25):
        return _clamp_cap_deltas(raw, max_per_sleeve=max_per_sleeve)

    top3 = _news_priority_sleeves(market_summary)
    keep_list = list(top3)
    consolidated = {k: 0.0 for k in _CAP_KEYS}

    for key, val in raw.items():
        if abs(val) < 1e-9:
            continue
        target = key if key in keep_list else _redirect_cap_to_priority(key, tuple(keep_list), market_summary)
        consolidated[target] += val

    for key in _CAP_KEYS:
        if key not in keep_list:
            consolidated[key] = 0.0

    return _clamp_cap_deltas(consolidated, max_per_sleeve=max_per_sleeve)


def _consolidate_news_deltas(
    deltas: dict[str, float],
    market_summary: dict | None,
    *,
    max_per_sleeve: float,
) -> dict[str, float]:
    """Alias for thinking_engine integration."""
    return consolidate_news_deltas(deltas, market_summary, max_per_sleeve=max_per_sleeve)


def build_news_digest(
    news_headlines: str | list | None,
    *,
    slot: str | None = None,
    ai_cycle_phase: str | None = None,
) -> dict[str, Any]:
    analysis = analyze_news_headlines(news_headlines, ai_cycle_phase=ai_cycle_phase)
    label = SLOT_LABELS.get(slot or "", slot or "manual")
    analysis["slot"] = slot
    analysis["formatted"] = f"[{label}]\n{analysis['digest_text']}"
    return analysis


def get_news_digest_for_thinking(
    *,
    max_items: int = 8,
    ai_cycle_phase: str | None = None,
    slot: str | None = None,
) -> dict[str, Any]:
    """RSS + manual headlines with theme analysis for Thinking Engine."""
    manual = _manual_headlines()
    fetched = fetch_market_headlines(max_items=max_items)
    lines: list[str] = []
    seen: set[str] = set()
    for item in manual + fetched:
        title = str(item.get("title") if isinstance(item, dict) else item).strip()
        key = title.lower()
        if not title or key in seen:
            continue
        seen.add(key)
        lines.append(title)
        if len(lines) >= max_items:
            break
    return build_news_digest(lines, slot=slot, ai_cycle_phase=ai_cycle_phase)


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
    """RSS headlines + optional manual lines (plain text)."""
    digest = get_news_digest_for_thinking(max_items=max_items)
    return normalize_news_headlines(digest.get("headlines"))


def format_news_summary(news_text: str, *, slot: str | None = None) -> str:
    label = SLOT_LABELS.get(slot or "", slot or "manual")
    body = news_text.strip() or "(no headlines fetched — add thinking_news_manual.txt or THINKING_NEWS_MANUAL)"
    return f"[{label}]\n{body}"


def format_news_digest(digest: dict[str, Any]) -> str:
    return str(digest.get("formatted") or format_news_summary("", slot=digest.get("slot")))


def synthesize_backtest_news(
    data,
    regime: str,
    vol: str,
    *,
    slot: str = "premarket",
) -> dict[str, Any]:
    """Historical headline proxy for backtest (simulates 8 AM / 6 PM digest from macro tape)."""
    from modules.thinking_engine import build_market_summary

    summary = build_market_summary(data, regime, vol)
    oil = float(summary.get("oil_change") or 0.0)
    gold = float(summary.get("gold_change") or 0.0)
    vix = summary.get("vix")
    vix_f = float(vix) if vix not in (None, "n/a") else 18.0
    spy_trend = str(summary.get("spy_trend", ""))
    headlines: list[str] = []

    if oil >= 3.0:
        headlines.append(
            f"Oil jumps {oil:.1f}% on Middle East / Hormuz shipping risk; gold "
            f"{'firm' if gold >= 0 else 'soft'}"
        )
    if oil >= 3.5 and vix_f >= 18:
        headlines.append(
            "Trump admin may flood the market with strategic oil reserves amid geopolitical tensions"
        )
    if gold >= 2.0 and vix_f >= 18:
        headlines.append("Safe-haven bid lifts gold as equity vol rises")
    if "below MA" in spy_trend and vix_f >= 20:
        headlines.append("Equity trend breaks MA200 — risk-off rotation into VTI and cash")
    leaders = summary.get("sector_leaders") or []
    tech_leading = any(
        any(k in str(r.get("sector", "")) for k in ("Tech", "Semis", "AI"))
        for r in leaders[:2]
    )
    if tech_leading:
        headlines.append(
            "Analysts warn tariff headlines may whipsaw small-cap beta before Fed speak; "
            "AI/datacenter demand still supports semis"
        )
    if str(regime).startswith("RHYME") and vix_f <= 16 and tech_leading:
        headlines.append("Mid-cycle AI leadership persists — selective SPY tilt vs passive VTI")
    crowded = str(summary.get("crowded_trade_warning") or "")
    if crowded.startswith("CROWDED"):
        headlines.append(f"Crowded trade alert: {crowded.replace('CROWDED: ', '')[:120]}")
    if not headlines:
        top = str(summary.get("top_headline") or "").strip()
        if top and top != "n/a":
            headlines.append(top[:240])
        else:
            headlines.append(
                f"Macro tape: regime {regime}, vol {vol}, VIX {vix_f:.0f} — no dominant headline"
            )

    return build_news_digest(
        headlines[:6],
        slot=slot,
        ai_cycle_phase=str(summary.get("ai_cycle_phase") or ""),
    )


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

            digest = get_news_digest_for_thinking(slot=slot)
            run_thinking_with_news(
                data,
                regime,
                vol,
                wisdom=wisdom,
                news_headlines=digest.get("headlines") or [],
                news_digest=digest,
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
