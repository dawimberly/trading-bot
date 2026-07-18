"""Catalyst scoring — paper/research; combines news, insider, RVOL, ORB, thinking context."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import config
from modules.scanner_common import bump_boost_for_insider_cluster, nyse_scan_universe

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
_SCORE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SEC = 600
_SCAN_UNIVERSE_CAP = 50
_BOOST_SCORE_THRESHOLD = 70

_EVENT_PATTERNS: dict[str, tuple[str, ...]] = {
    "earnings_beat": (
        "beats earnings",
        "earnings beat",
        "topped estimates",
        "surpassed expectations",
        "raised guidance",
        "upbeat guidance",
        "strong quarter",
        "record revenue",
    ),
    "fda": (
        "fda approval",
        "fda clears",
        "fda approves",
        "pdufa",
        "phase 3 success",
        "breakthrough therapy",
    ),
    "contract": (
        "contract award",
        "wins contract",
        "partnership",
        "strategic alliance",
        "supply agreement",
        "licensing deal",
    ),
    "mna": (
        "to acquire",
        "acquisition of",
        "merger agreement",
        "takeover bid",
    ),
}


def _cache_get(key: str) -> dict[str, Any] | None:
    hit = _SCORE_CACHE.get(key)
    if not hit:
        return None
    ts, payload = hit
    if time.time() - ts > _CACHE_TTL_SEC:
        _SCORE_CACHE.pop(key, None)
        return None
    return payload


def _cache_put(key: str, payload: dict[str, Any]) -> None:
    _SCORE_CACHE[key] = (time.time(), payload)


def _load_headline_corpus() -> str:
    if config.effective_historical_news_enabled():
        try:
            from modules.historical_news import get_backtest_headline_corpus

            corpus = get_backtest_headline_corpus()
            if corpus.strip():
                return corpus
        except Exception as exc:
            logger.debug("historical headline corpus unavailable: %s", exc)
    chunks: list[str] = []
    try:
        from modules.thinking_news import get_news_digest_for_thinking

        digest = get_news_digest_for_thinking(max_items=10)
        for line in digest.get("headlines") or []:
            chunks.append(str(line))
        if digest.get("digest_text"):
            chunks.append(str(digest["digest_text"]))
    except Exception as exc:
        logger.debug("live news digest unavailable for catalyst corpus: %s", exc)
    try:
        cache_path = ROOT / config.WEB_SENTIMENT_CACHE_FILE
        if cache_path.is_file():
            with open(cache_path, encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("headline_text"):
                chunks.append(str(cached["headline_text"]))
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    try:
        out_path = ROOT / config.THINKING_ENGINE_OUTPUT_FILE
        if out_path.is_file():
            with open(out_path, encoding="utf-8") as f:
                last = json.load(f)
            for key in ("narrative", "reasoning", "regime_narrative"):
                if last.get(key):
                    chunks.append(str(last[key])[:1200])
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return "\n".join(chunks).lower()


def _symbol_in_text(symbol: str, text: str) -> bool:
    sym = config.normalize_symbol(symbol)
    if not sym or not text:
        return False
    return bool(re.search(rf"\b{re.escape(sym)}\b", text, re.I))


def _event_score(symbol: str, corpus: str) -> tuple[float, list[str]]:
    if not corpus or not _symbol_in_text(symbol, corpus):
        return 0.0, []
    hits: list[str] = []
    score = 0.0
    for event, patterns in _EVENT_PATTERNS.items():
        for pat in patterns:
            if pat in corpus and _symbol_in_text(symbol, corpus):
                if event == "earnings_beat":
                    score = max(score, 28.0)
                    hits.append("earnings/guidance")
                elif event == "fda":
                    score = max(score, 30.0)
                    hits.append("FDA/approval")
                elif event == "contract":
                    score = max(score, 24.0)
                    hits.append("contract/partnership")
                elif event == "mna":
                    score = max(score, 22.0)
                    hits.append("M&A")
                break
    if score > 0 and not hits:
        hits.append("headline mention")
    return min(score, 30.0), hits


def _insider_score(symbol: str) -> tuple[float, str | None]:
    if not config.effective_insider_monitor_enabled():
        return 0.0, None
    try:
        from modules.insider_monitor import get_cluster_buy_signals, _sig_type

        sym = config.normalize_symbol(symbol)
        best = 0.0
        note = None
        for sig in get_cluster_buy_signals(min_insiders=2, days=7):
            if config.normalize_symbol(str(sig.get("ticker") or "")) != sym:
                continue
            insiders = int(sig.get("insiders_count") or 0)
            quality = int(sig.get("score") or 0)
            pts = 12.0 + min(13.0, insiders * 2.0 + quality * 0.05)
            if pts > best:
                best = pts
                note = f"cluster buy ({insiders} insiders, s{quality})"
        return min(best, 25.0), note
    except Exception as exc:
        logger.debug("insider cluster catalyst score unavailable for %s: %s", symbol, exc)
        return 0.0, None


def _rvol_score(data, symbol: str) -> tuple[float, float | None]:
    try:
        from modules.volume_analysis import calculate_rvol

        rvol = calculate_rvol(data, symbol)
    except Exception as exc:
        logger.debug("RVOL catalyst factor unavailable for %s: %s", symbol, exc)
        return 0.0, None
    if rvol is None:
        return 0.0, None
    if rvol >= float(config.RVOL_STRONG_THRESHOLD):
        return 20.0, rvol
    if rvol >= float(config.ORB_RVOL_MIN):
        return 12.0, rvol
    if rvol >= float(config.RVOL_MIN_THRESHOLD):
        return 6.0, rvol
    return 0.0, rvol


def _orb_score(data, symbol: str) -> tuple[float, str | None]:
    if not config.effective_orb_enabled():
        return 0.0, None
    try:
        from modules.orb_strategy import calculate_opening_range
        from modules.volume_analysis import calculate_rvol

        or_info = calculate_opening_range(
            data, symbol, minutes=int(config.ORB_BREAKOUT_MINUTES)
        )
        if not or_info or not or_info.get("breakout_up"):
            return 0.0, None
        rvol = calculate_rvol(data, symbol)
        if rvol is None or rvol < float(config.ORB_RVOL_MIN):
            return 4.0, "OR-high (low RVOL)"
        return 10.0, "OR-high + RVOL"
    except Exception as exc:
        logger.debug("ORB catalyst factor unavailable for %s: %s", symbol, exc)
        return 0.0, None


def _sentiment_score(symbol: str, corpus: str) -> tuple[float, str | None]:
    if not corpus or not _symbol_in_text(symbol, corpus):
        return 0.0, None
    try:
        from modules.sentiment_keywords import score_text_sentiment

        sym = config.normalize_symbol(symbol)
        lines = [ln for ln in corpus.splitlines() if _symbol_in_text(sym, ln)]
        if not lines:
            lines = [corpus] if _symbol_in_text(sym, corpus) else []
        if not lines:
            return 0.0, None
        mood = score_text_sentiment(" ".join(lines[:6]))
        if mood >= 0.35:
            return 15.0, f"positive news mood {mood:+.2f}"
        if mood >= 0.12:
            return 8.0, f"neutral-positive mood {mood:+.2f}"
        if mood <= -0.2:
            return 0.0, f"negative mood {mood:+.2f}"
        return 4.0, f"mixed mood {mood:+.2f}"
    except Exception as exc:
        logger.debug("sentiment catalyst factor unavailable for %s: %s", symbol, exc)
        return 0.0, None


def score_catalysts(data, symbol: str, *, corpus: str | None = None) -> dict[str, Any]:
    """Return catalyst score 0–100 with factor breakdown."""
    sym = config.normalize_symbol(symbol)
    if not sym:
        return {"symbol": symbol, "score": 0, "factors": []}

    cache_key = f"cat:{sym}"
    if corpus is None:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    if corpus is None:
        corpus = _load_headline_corpus()

    factors: list[str] = []
    total = 0.0

    evt_pts, evt_hits = _event_score(sym, corpus)
    if evt_pts:
        total += evt_pts
        factors.extend(evt_hits)

    ins_pts, ins_note = _insider_score(sym)
    if ins_pts:
        total += ins_pts
        if ins_note:
            factors.append(ins_note)

    rvol_pts, rvol = _rvol_score(data, sym)
    if rvol_pts:
        total += rvol_pts
        factors.append(f"RVOL {rvol:.1f}x")

    orb_pts, orb_note = _orb_score(data, sym)
    if orb_pts:
        total += orb_pts
        if orb_note:
            factors.append(orb_note)

    sent_pts, sent_note = _sentiment_score(sym, corpus)
    if sent_pts:
        total += sent_pts
        if sent_note:
            factors.append(sent_note)

    align = sum(1 for x in (evt_pts, ins_pts, rvol_pts, orb_pts) if x > 0)
    if align >= 3:
        total += 8.0
        factors.append("multi-factor conviction")
    elif align >= 2:
        total += 4.0
        factors.append("dual-factor")

    score = int(round(min(100.0, max(0.0, total))))
    payload = {
        "symbol": sym,
        "score": score,
        "factors": factors[:6],
        "rvol": rvol,
        "high_conviction": score >= _BOOST_SCORE_THRESHOLD,
    }
    if corpus is None:
        _cache_put(cache_key, payload)
    return payload


def get_top_catalyst_stocks(
    data,
    min_score: float | None = None,
    *,
    limit: int = 15,
) -> list[dict[str, Any]]:
    """Rank universe by catalyst score (desc)."""
    if not config.effective_catalyst_scoring_enabled():
        return []
    threshold = float(
        min_score if min_score is not None else config.CATALYST_MIN_SCORE
    )
    limit = max(1, int(limit))
    corpus = _load_headline_corpus()
    scored: list[tuple[int, dict[str, Any]]] = []
    for sym in nyse_scan_universe(data, limit_scan=_SCAN_UNIVERSE_CAP):
        row = score_catalysts(data, sym, corpus=corpus)
        if int(row.get("score") or 0) >= threshold:
            scored.append((int(row["score"]), row))
    scored.sort(reverse=True)
    return [row for _, row in scored[:limit]]


def catalyst_momentum_rank_boost(symbol: str, data=None) -> float:
    if not config.effective_catalyst_scoring_enabled():
        return 0.0
    row = score_catalysts(data, symbol)
    if int(row.get("score") or 0) < _BOOST_SCORE_THRESHOLD:
        return 0.0
    boost = float(config.CATALYST_BOOST_FACTOR)
    boost = bump_boost_for_insider_cluster(boost, symbol, cap_mult=1.25)
    try:
        from modules.volume_analysis import rvol_momentum_rank_boost
        from modules.orb_strategy import orb_momentum_rank_boost

        if rvol_momentum_rank_boost(symbol, data) > 0 or orb_momentum_rank_boost(symbol, data) > 0:
            boost = round(min(boost + 0.04, boost * 1.2), 4)
    except Exception as exc:
        logger.debug("RVOL/ORB catalyst rank bump skipped for %s: %s", symbol, exc)
    return round(boost, 4)


def catalyst_stat_arb_long_mult(symbol: str, data=None) -> float:
    if not config.effective_catalyst_scoring_enabled():
        return 1.0
    row = score_catalysts(data, symbol)
    if int(row.get("score") or 0) < _BOOST_SCORE_THRESHOLD:
        return 1.0
    return round(1.0 + float(config.CATALYST_BOOST_FACTOR), 4)


def catalyst_insider_cluster_extra(symbol: str, data=None) -> float:
    row = score_catalysts(data, symbol)
    if int(row.get("score") or 0) < _BOOST_SCORE_THRESHOLD:
        return 0.0
    return 0.05


def get_catalyst_context_for_thinking(data=None) -> str:
    """Top catalyst names for Kimi / thinking engine daily context."""
    if not config.effective_catalyst_scoring_enabled():
        return "catalyst scanner off"
    if data is None:
        try:
            from modules.pipeline_strategies import load_pipeline_data

            data = load_pipeline_data()
        except Exception as exc:
            logger.debug("pipeline data unavailable for catalyst thinking context: %s", exc)
            return "catalyst data unavailable"
    top = get_top_catalyst_stocks(
        data, min_score=float(config.CATALYST_MIN_SCORE), limit=8
    )
    if not top:
        return f"no catalysts ≥ {config.CATALYST_MIN_SCORE:.0f} today"
    parts = []
    for row in top[:6]:
        fac = ", ".join((row.get("factors") or [])[:2]) or "multi-signal"
        parts.append(f"{row['symbol']} {row['score']} ({fac})")
    return "; ".join(parts)


def format_catalyst_scanner_banner() -> str | None:
    if not config.effective_catalyst_scoring_enabled():
        return ">>> Catalyst Scanner: OFF"
    return f">>> Catalyst Scanner: ON (min {int(config.CATALYST_MIN_SCORE)})"


def format_telegram_weekly_catalyst_block(data=None) -> str:
    if not config.effective_catalyst_scoring_enabled():
        return ""
    if data is None:
        try:
            from modules.pipeline_strategies import load_pipeline_data

            data = load_pipeline_data()
        except Exception as exc:
            logger.debug("pipeline data unavailable for catalyst telegram block: %s", exc)
            return ""
    top = get_top_catalyst_stocks(data, min_score=float(config.CATALYST_MIN_SCORE), limit=6)
    if not top:
        return f"\n\nCatalysts: none ≥ {config.CATALYST_MIN_SCORE:.0f} this week."
    lines = [f"\n\nTop catalysts (score ≥ {config.CATALYST_MIN_SCORE:.0f}):"]
    for row in top:
        fac = ", ".join((row.get("factors") or [])[:2]) or "multi-signal"
        lines.append(f"  {row['symbol']} {row['score']} — {fac}")
    return "\n".join(lines)


def extend_combined_scanner_rows(
    rows: list[dict[str, str]],
    seen: set[str],
    data,
    *,
    limit: int,
) -> list[dict[str, str]]:
    """Prepend high-score catalyst rows to RVOL/ORB dashboard table."""
    for row in get_top_catalyst_stocks(
        data, min_score=float(config.CATALYST_MIN_SCORE), limit=limit
    ):
        sym = str(row["symbol"])
        if sym in seen:
            continue
        seen.add(sym)
        rvol = row.get("rvol")
        rvol_s = f"{float(rvol):.2f}x" if rvol is not None else "—"
        rows.insert(
            0,
            {
                "Symbol": sym,
                "RVOL": rvol_s,
                "ORB": "—",
                "Signal": f"Catalyst {row['score']}",
                "_tag": "catalyst_high",
            },
        )
        if len(rows) >= limit:
            break
    return rows[:limit]
