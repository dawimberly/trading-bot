"""Lightweight chart pattern detection (research-only, no ML).

Rule-based price-action heuristics with optional TA-Lib CDL pattern hints.
Used for screener boosts, NYSE momentum ranking, and thinking narratives.
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)

try:
    import talib as _talib  # type: ignore

    _HAS_TALIB = True
except ImportError:
    _talib = None
    _HAS_TALIB = False

LOOKBACK = int(os.getenv("PATTERN_LOOKBACK_BARS", "120"))
MIN_BARS = int(os.getenv("PATTERN_MIN_BARS", "40"))
PEAK_ORDER = int(os.getenv("PATTERN_PEAK_ORDER", "3"))

PATTERN_LABELS: dict[str, tuple[str, str]] = {
    "cup_handle": ("Cup & Handle", "bullish"),
    "head_shoulders": ("Head & Shoulders", "bearish"),
    "inv_head_shoulders": ("Inverse H&S", "bullish"),
    "double_top": ("Double Top", "bearish"),
    "double_bottom": ("Double Bottom", "bullish"),
    "asc_triangle": ("Ascending Triangle", "bullish"),
    "desc_triangle": ("Descending Triangle", "bearish"),
    "flag": ("Flag", "bullish"),
    "pennant": ("Pennant", "neutral"),
}


@dataclass
class PatternHit:
    key: str
    label: str
    direction: str
    confidence: float
    bars_ago: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "direction": self.direction,
            "confidence": round(float(self.confidence), 4),
            "bars_ago": int(self.bars_ago),
        }


@dataclass
class PatternState:
    hits: list[PatternHit] = field(default_factory=list)
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": [h.to_dict() for h in self.hits],
            "score": round(float(self.score), 4),
        }


def _pattern_min_confidence(key: str) -> float:
    if key == "cup_handle":
        return config.PATTERN_CUP_HANDLE_MIN_CONF
    if key == "flag":
        return config.PATTERN_FLAG_MIN_CONF
    return config.PATTERN_MIN_CONFIDENCE


def _meets_confidence_threshold(hit: PatternHit) -> bool:
    return hit.confidence >= _pattern_min_confidence(hit.key)


def _eligible_for_patterns(
    prices: np.ndarray,
    *,
    avg_volume: float | None = None,
    volume: pd.Series | np.ndarray | None = None,
) -> bool:
    if len(prices) < MIN_BARS:
        return False
    price = float(prices[-1])
    if price < config.PATTERN_MIN_PRICE:
        return False
    if avg_volume is not None:
        return float(avg_volume) >= config.PATTERN_MIN_AVG_VOLUME
    if volume is not None:
        vol_arr = _to_prices(volume) if not isinstance(volume, np.ndarray) else volume
        if len(vol_arr) >= 20:
            return float(np.mean(vol_arr[-20:])) >= config.PATTERN_MIN_AVG_VOLUME
    return True


def _log_rank_adjustment(symbol: str, hits: list[PatternHit], score: float) -> None:
    if not hits:
        return
    sym = config.normalize_symbol(symbol)
    bearish_only = config.effective_pattern_bearish_only()
    for hit in hits:
        if not _meets_confidence_threshold(hit):
            continue
        if bearish_only and hit.direction == "bullish":
            continue
        if hit.direction == "bullish" and score > 0.05:
            logger.info(
                "Detected Bullish %s on %s - boosting (conf=%.2f)",
                hit.label,
                sym,
                hit.confidence,
            )
        elif hit.direction == "bearish" and score < -0.05:
            logger.info(
                "Detected Bearish %s on %s - trimming (conf=%.2f)",
                hit.label,
                sym,
                hit.confidence,
            )


def _book(executor) -> Any:
    return getattr(executor, "portfolio", executor)


def _stats(executor) -> dict[str, Any]:
    book = _book(executor) if executor is not None else None
    if book is None:
        return {}
    if not hasattr(book, "pattern_awareness_stats"):
        book.pattern_awareness_stats = {
            "detections": 0,
            "bullish": 0,
            "bearish": 0,
            "by_pattern": Counter(),
            "by_symbol": {},
            "scans": 0,
        }
    return book.pattern_awareness_stats


def _record_detection(executor, symbol: str, hits: list[PatternHit]) -> None:
    if not hits or executor is None:
        return
    stats = _stats(executor)
    if not stats:
        return
    sym = config.normalize_symbol(symbol)
    stats["scans"] = stats.get("scans", 0) + 1
    sym_row = stats.setdefault("by_symbol", {}).setdefault(
        sym,
        {"detections": 0, "bullish": 0, "bearish": 0, "patterns": Counter()},
    )
    for hit in hits:
        stats["detections"] = stats.get("detections", 0) + 1
        sym_row["detections"] += 1
        stats["by_pattern"][hit.key] = stats["by_pattern"].get(hit.key, 0) + 1
        sym_row["patterns"][hit.key] = sym_row["patterns"].get(hit.key, 0) + 1
        if hit.direction == "bullish":
            stats["bullish"] = stats.get("bullish", 0) + 1
            sym_row["bullish"] += 1
        elif hit.direction == "bearish":
            stats["bearish"] = stats.get("bearish", 0) + 1
            sym_row["bearish"] += 1


def _to_prices(series: pd.Series | np.ndarray | list) -> np.ndarray:
    if isinstance(series, pd.Series):
        arr = series.dropna().astype(float).values
    else:
        arr = np.asarray(series, dtype=float)
        arr = arr[~np.isnan(arr)]
    if len(arr) > LOOKBACK:
        arr = arr[-LOOKBACK:]
    return arr


def _local_extrema(prices: np.ndarray, order: int = PEAK_ORDER) -> tuple[list[int], list[int]]:
    peaks: list[int] = []
    troughs: list[int] = []
    n = len(prices)
    if n < order * 2 + 3:
        return peaks, troughs
    for i in range(order, n - order):
        seg = prices[i - order : i + order + 1]
        if prices[i] >= np.max(seg) * 0.999:
            peaks.append(i)
        if prices[i] <= np.min(seg) * 1.001:
            troughs.append(i)
    return peaks, troughs


def _pct_diff(a: float, b: float) -> float:
    base = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / base


def _detect_double_top(prices: np.ndarray) -> PatternHit | None:
    peaks, _ = _local_extrema(prices)
    if len(peaks) < 2:
        return None
    p1, p2 = peaks[-2], peaks[-1]
    if p2 - p1 < 10 or p2 - p1 > 70:
        return None
    v1, v2 = prices[p1], prices[p2]
    if _pct_diff(v1, v2) > 0.03:
        return None
    mid = prices[p1:p2]
    if len(mid) < 3:
        return None
    valley = float(np.min(mid))
    if valley > min(v1, v2) * 0.97:
        return None
    conf = 0.55 + 0.35 * (1.0 - _pct_diff(v1, v2) / 0.03)
    return PatternHit(
        "double_top",
        *PATTERN_LABELS["double_top"],
        confidence=min(0.95, conf),
        bars_ago=len(prices) - 1 - p2,
    )


def _detect_double_bottom(prices: np.ndarray) -> PatternHit | None:
    _, troughs = _local_extrema(prices)
    if len(troughs) < 2:
        return None
    t1, t2 = troughs[-2], troughs[-1]
    if t2 - t1 < 10 or t2 - t1 > 70:
        return None
    v1, v2 = prices[t1], prices[t2]
    if _pct_diff(v1, v2) > 0.03:
        return None
    mid = prices[t1:t2]
    if len(mid) < 3:
        return None
    peak = float(np.max(mid))
    if peak < max(v1, v2) * 1.03:
        return None
    conf = 0.55 + 0.35 * (1.0 - _pct_diff(v1, v2) / 0.03)
    return PatternHit(
        "double_bottom",
        *PATTERN_LABELS["double_bottom"],
        confidence=min(0.95, conf),
        bars_ago=len(prices) - 1 - t2,
    )


def _detect_head_shoulders(prices: np.ndarray) -> PatternHit | None:
    peaks, _ = _local_extrema(prices)
    if len(peaks) < 3:
        return None
    l, h, r = peaks[-3], peaks[-2], peaks[-1]
    lv, hv, rv = prices[l], prices[h], prices[r]
    if hv <= lv * 1.02 or hv <= rv * 1.02:
        return None
    if _pct_diff(lv, rv) > 0.05:
        return None
    conf = 0.5 + 0.4 * min(1.0, (hv / max(lv, rv) - 1.0) / 0.08)
    return PatternHit(
        "head_shoulders",
        *PATTERN_LABELS["head_shoulders"],
        confidence=min(0.92, conf),
        bars_ago=len(prices) - 1 - r,
    )


def _detect_inv_head_shoulders(prices: np.ndarray) -> PatternHit | None:
    _, troughs = _local_extrema(prices)
    if len(troughs) < 3:
        return None
    l, h, r = troughs[-3], troughs[-2], troughs[-1]
    lv, hv, rv = prices[l], prices[h], prices[r]
    if hv >= lv * 0.98 or hv >= rv * 0.98:
        return None
    if _pct_diff(lv, rv) > 0.05:
        return None
    conf = 0.5 + 0.4 * min(1.0, (min(lv, rv) / hv - 1.0) / 0.08)
    return PatternHit(
        "inv_head_shoulders",
        *PATTERN_LABELS["inv_head_shoulders"],
        confidence=min(0.92, conf),
        bars_ago=len(prices) - 1 - r,
    )


def _detect_cup_handle(prices: np.ndarray) -> PatternHit | None:
    n = len(prices)
    if n < 55:
        return None
    left = prices[: n // 3]
    mid = prices[n // 3 : 2 * n // 3]
    right = prices[2 * n // 3 :]
    if len(left) < 12 or len(mid) < 12 or len(right) < 10:
        return None
    left_high = float(np.max(left))
    mid_low = float(np.min(mid))
    handle_len = max(4, len(right) // 4)
    right_rim = right[:-handle_len]
    handle = right[-handle_len:]
    if len(right_rim) < 4 or len(handle) < 3:
        return None
    right_high = float(np.max(right_rim))
    handle_pullback = (right_high - float(np.min(handle))) / max(right_high, 1e-9)
    cup_depth = (left_high - mid_low) / max(left_high, 1e-9)
    recovery = right_high / max(mid_low, 1e-9) - 1.0
    rim_match = _pct_diff(left_high, right_high)
    last_price = float(prices[-1])
    near_breakout = last_price >= right_high * 0.97
    if cup_depth < 0.12 or cup_depth > 0.28:
        return None
    if recovery < 0.08:
        return None
    if rim_match > 0.04:
        return None
    if handle_pullback < 0.03 or handle_pullback > 0.10:
        return None
    if not near_breakout:
        return None
    conf = 0.62 + 0.28 * min(1.0, recovery / 0.18) + 0.05 * (1.0 - rim_match / 0.04)
    return PatternHit(
        "cup_handle",
        *PATTERN_LABELS["cup_handle"],
        confidence=min(0.92, conf),
        bars_ago=max(0, len(handle) - 1),
    )


def _detect_triangle(prices: np.ndarray, *, ascending: bool) -> PatternHit | None:
    n = len(prices)
    if n < 30:
        return None
    seg = prices[-40:] if n >= 40 else prices
    x = np.arange(len(seg), dtype=float)
    slope = float(np.polyfit(x, seg, 1)[0])
    norm_slope = slope / max(float(np.mean(seg)), 1e-9)
    highs = pd.Series(seg).rolling(5, min_periods=3).max().dropna().values
    lows = pd.Series(seg).rolling(5, min_periods=3).min().dropna().values
    if len(highs) < 5 or len(lows) < 5:
        return None
    high_range = (float(np.max(highs)) - float(np.min(highs))) / max(float(np.mean(seg)), 1e-9)
    low_range = (float(np.max(lows)) - float(np.min(lows))) / max(float(np.mean(seg)), 1e-9)
    if ascending:
        if norm_slope <= 0 or high_range > 0.06 or low_range < 0.03:
            return None
        if low_range >= high_range:
            return None
        key = "asc_triangle"
    else:
        if norm_slope >= 0 or low_range > 0.06 or high_range < 0.03:
            return None
        if high_range >= low_range:
            return None
        key = "desc_triangle"
    conf = 0.48 + 0.35 * min(1.0, abs(norm_slope) * 200)
    return PatternHit(
        key,
        *PATTERN_LABELS[key],
        confidence=min(0.88, conf),
        bars_ago=0,
    )


def _detect_flag_pennant(prices: np.ndarray) -> list[PatternHit]:
    hits: list[PatternHit] = []
    n = len(prices)
    if n < 28:
        return hits
    pole = prices[-28:-12]
    cons = prices[-12:]
    if len(pole) < 10 or len(cons) < 6:
        return hits
    pole_ret = pole[-1] / max(pole[0], 1e-9) - 1.0
    cons_range = (float(np.max(cons)) - float(np.min(cons))) / max(float(np.mean(cons)), 1e-9)
    if abs(pole_ret) < 0.10:
        return hits
    if cons_range > 0.035:
        return hits
    direction = "bullish" if pole_ret > 0 else "bearish"
    conf = 0.58 + 0.32 * min(1.0, abs(pole_ret) / 0.18)
    if cons_range < 0.022:
        hits.append(
            PatternHit(
                "pennant",
                *PATTERN_LABELS["pennant"],
                confidence=min(0.88, conf),
                bars_ago=0,
            )
        )
    hits.append(
        PatternHit(
            "flag",
            PATTERN_LABELS["flag"][0],
            direction,
            min(0.90, conf),
            bars_ago=0,
        )
    )
    return hits


def _talib_hints(prices: np.ndarray) -> list[PatternHit]:
    if not _HAS_TALIB or len(prices) < MIN_BARS:
        return []
    o = h = l = c = prices.astype(float)
    hints: list[PatternHit] = []
    checks = [
        ("inv_head_shoulders", _talib.CDLINVERTEDHAMMER),
        ("double_bottom", _talib.CDLHAMMER),
        ("head_shoulders", _talib.CDLSHOOTINGSTAR),
        ("double_top", _talib.CDLHANGINGMAN),
    ]
    for key, fn in checks:
        try:
            out = fn(o, h, l, c)
            if out is None or len(out) == 0:
                continue
            val = int(out[-1])
            if val == 0:
                continue
            label, direction = PATTERN_LABELS.get(key, (key, "neutral"))
            hints.append(
                PatternHit(
                    key,
                    label,
                    direction,
                    confidence=0.42,
                    bars_ago=0,
                )
            )
        except Exception:
            continue
    return hints


def detect_patterns(
    prices: pd.Series | np.ndarray | list,
    *,
    symbol: str = "",
    min_confidence: float | None = None,
    volume: pd.Series | np.ndarray | list | None = None,
    avg_volume: float | None = None,
) -> list[PatternHit]:
    """Detect chart patterns on a close-price series (most recent bar = end)."""
    arr = _to_prices(prices)
    if not _eligible_for_patterns(arr, avg_volume=avg_volume, volume=volume):
        return []

    floor = float(min_confidence if min_confidence is not None else config.PATTERN_MIN_CONFIDENCE)
    hits: list[PatternHit] = []
    detectors = [
        _detect_cup_handle,
        _detect_head_shoulders,
        _detect_inv_head_shoulders,
        _detect_double_top,
        _detect_double_bottom,
        lambda p: _detect_triangle(p, ascending=True),
        lambda p: _detect_triangle(p, ascending=False),
    ]
    for fn in detectors:
        try:
            hit = fn(arr)
            if hit is not None and _meets_confidence_threshold(hit):
                hits.append(hit)
        except Exception:
            logger.debug("pattern detector failed for %s", symbol, exc_info=True)

    try:
        for hit in _detect_flag_pennant(arr):
            if _meets_confidence_threshold(hit):
                hits.append(hit)
    except Exception:
        pass

    if _HAS_TALIB:
        for hit in _talib_hints(arr):
            if hit.confidence >= floor and _meets_confidence_threshold(hit):
                hits.append(hit)

    best: dict[str, PatternHit] = {}
    for hit in hits:
        prev = best.get(hit.key)
        if prev is None or hit.confidence > prev.confidence:
            best[hit.key] = hit
    return sorted(best.values(), key=lambda h: -h.confidence)


def pattern_score(
    hits: list[PatternHit] | PatternState | None,
    *,
    bearish_only: bool | None = None,
) -> float:
    """Net pattern bias in [-1, 1] (bullish positive, bearish negative)."""
    if hits is None:
        return 0.0
    if isinstance(hits, PatternState):
        return float(hits.score)
    if not hits:
        return 0.0
    if bearish_only is None:
        bearish_only = config.effective_pattern_bearish_only()
    bull = sum(h.confidence for h in hits if h.direction == "bullish")
    bear = sum(h.confidence for h in hits if h.direction == "bearish")
    if bearish_only:
        raw = -bear
        return float(max(-1.0, min(0.0, raw / 1.5)))
    raw = bull - bear
    return float(max(-1.0, min(1.0, raw / 1.5)))


def pattern_composite_multiplier(score: float) -> float:
    """Screener / ranking multiplier from pattern_score."""
    boost = config.PATTERN_SCORE_BOOST
    trim = config.PATTERN_SCORE_TRIM
    s = float(score)
    if config.effective_pattern_bearish_only():
        if s < 0:
            return max(0.80, 1.0 + trim * s)
        return 1.0
    if s > 0:
        return 1.0 + boost * s
    if s < 0:
        return max(0.80, 1.0 + trim * s)
    return 1.0


def _price_series(data, symbol: str, bar_idx: int | None, full_data) -> pd.Series | None:
    sym = config.normalize_symbol(symbol)
    frame = data
    if bar_idx is not None and full_data is not None:
        frame = full_data.iloc[: bar_idx + 1]
    if sym not in frame.columns:
        return None
    series = frame[sym].dropna()
    if len(series) < MIN_BARS:
        return None
    return series


def apply_patterns_to_ranked(
    ranked: list[str],
    data,
    *,
    bar_idx: int | None = None,
    full_data=None,
    executor=None,
) -> list[str]:
    """Reorder momentum picks: bullish patterns first, bearish deferred."""
    if not ranked or not config.effective_pattern_awareness_enabled():
        return ranked

    scores: dict[str, float] = {}
    for sym in ranked:
        series = _price_series(data, sym, bar_idx, full_data)
        if series is None:
            scores[sym] = 0.0
            continue
        if not _eligible_for_patterns(series.values):
            scores[sym] = 0.0
            continue
        hits = detect_patterns(series, symbol=sym)
        scores[sym] = pattern_score(hits)
        _record_detection(executor, sym, hits)
        _log_rank_adjustment(sym, hits, scores[sym])

    def sort_key(sym: str) -> tuple[float, int]:
        try:
            idx = ranked.index(sym)
        except ValueError:
            idx = len(ranked)
        return (-scores.get(sym, 0.0), idx)

    ordered = sorted(ranked, key=sort_key)
    if config.effective_pattern_bearish_only():
        # Drop high-confidence bearish setups instead of only deferring them.
        filtered = [sym for sym in ordered if scores.get(sym, 0.0) > -0.12]
        if filtered:
            return filtered
    return ordered


def _scan_universe_patterns(data, symbols: list[str], limit: int = 12) -> list[tuple[str, list[PatternHit]]]:
    found: list[tuple[str, list[PatternHit]]] = []
    for sym in symbols:
        if sym not in getattr(data, "columns", []):
            continue
        series = data[sym].dropna()
        if len(series) < MIN_BARS:
            continue
        if not _eligible_for_patterns(series.values):
            continue
        hits = detect_patterns(series, symbol=sym)
        if hits:
            found.append((sym, hits))
    found.sort(key=lambda x: -abs(pattern_score(x[1])))
    return found[:limit]


def build_pattern_narrative(
    summary_or_hits: dict | list[PatternHit] | list[tuple[str, list[PatternHit]]] | None,
) -> str:
    """Human-readable pattern narrative for thinking / status."""
    if summary_or_hits is None:
        return ""
    if isinstance(summary_or_hits, dict):
        leaders = summary_or_hits.get("pattern_leaders") or []
        if not leaders:
            return "Chart patterns: no high-confidence setups in scan window."
        parts = []
        for row in leaders[:4]:
            sym = row.get("symbol", "?")
            labels = ", ".join(row.get("patterns", [])[:2])
            score = row.get("score", 0.0)
            bias = "bullish" if score > 0.1 else "bearish" if score < -0.1 else "mixed"
            parts.append(f"{sym} ({labels}, {bias})")
        return "Chart patterns: " + "; ".join(parts) + "."

    if isinstance(summary_or_hits, list) and summary_or_hits:
        if isinstance(summary_or_hits[0], tuple):
            parts = []
            for sym, hits in summary_or_hits[:4]:
                labels = ", ".join(h.label for h in hits[:2])
                sc = pattern_score(hits)
                bias = "bullish" if sc > 0.1 else "bearish" if sc < -0.1 else "mixed"
                parts.append(f"{sym} ({labels}, {bias})")
            return "Chart patterns: " + "; ".join(parts) + "."
        labels = ", ".join(h.label for h in summary_or_hits[:3])
        sc = pattern_score(summary_or_hits)
        bias = "bullish tilt" if sc > 0.15 else "bearish tilt" if sc < -0.15 else "neutral"
        return f"Chart patterns: {labels} — {bias}."

    return "Chart patterns: no high-confidence setups."


def enrich_summary_with_patterns(summary: dict, data=None) -> None:
    """Attach pattern scan + narrative to market summary (in-place)."""
    if not config.effective_pattern_awareness_enabled():
        return
    symbols: list[str] = []
    if data is not None and hasattr(data, "columns"):
        try:
            from modules.pipeline_strategies import _nyse_equity_columns

            symbols = list(_nyse_equity_columns(data))[:40]
        except Exception:
            symbols = [c for c in data.columns if config._nyse_eligible_symbol(str(c))][:40]
    if not symbols:
        summary["pattern_awareness_narrative"] = "Chart patterns: scan skipped (no price data)."
        return

    found = _scan_universe_patterns(data, symbols)
    leaders = []
    bull = bear = 0
    for sym, hits in found:
        sc = pattern_score(hits)
        leaders.append(
            {
                "symbol": sym,
                "score": round(sc, 4),
                "patterns": [h.label for h in hits],
                "hits": [h.to_dict() for h in hits],
            }
        )
        for h in hits:
            if h.direction == "bullish":
                bull += 1
            elif h.direction == "bearish":
                bear += 1

    summary["pattern_leaders"] = leaders
    summary["pattern_bullish_count"] = bull
    summary["pattern_bearish_count"] = bear
    summary["pattern_awareness_narrative"] = build_pattern_narrative(summary)


def format_pattern_stats_report(stats: dict | None) -> str:
    if not stats:
        return "No pattern detections recorded."
    by_pat = stats.get("by_pattern") or {}
    if isinstance(by_pat, Counter):
        by_pat = dict(by_pat)
    top = sorted(by_pat.items(), key=lambda x: -x[1])[:6]
    top_str = ", ".join(f"{PATTERN_LABELS.get(k, (k, ''))[0]}:{v}" for k, v in top) or "none"
    return (
        f"detections {stats.get('detections', 0)} | "
        f"bullish {stats.get('bullish', 0)} | "
        f"bearish {stats.get('bearish', 0)} | "
        f"scans {stats.get('scans', 0)} | "
        f"top: {top_str}"
    )
