"""Paper-only international equity sleeve via liquid US-listed ADRs.

Alpaca does not support true forex — ADRs only (no currency pairs).
Allocation: 0–10% of equity when macro or thinking triggers fire.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

import config

logger = logging.getLogger(__name__)

ADR_RISK_NOTE = (
    "ADR/currency risk: home-market gaps and FX moves are unhedged on Alpaca "
    "(no forex sleeve) — international sleeve uses USD ADRs only."
)

# Top liquid ADRs commonly available on Alpaca US equities
INTERNATIONAL_ADR_SYMBOLS: tuple[str, ...] = (
    "ASML",
    "TSM",
    "BABA",
    "SONY",
    "SAP",
    "TM",
    "NVO",
    "UL",
    "HSBC",
    "BP",
    "RIO",
    "BHP",
    "NMR",
    "SAN",
    "INFY",
    "BIDU",
    "JD",
    "PDD",
    "MELI",
    "STM",
)

_INTERNATIONAL_ADR_SET = frozenset(INTERNATIONAL_ADR_SYMBOLS)
_adr_risk_logged = False


def is_international_adr(symbol: str) -> bool:
    return config.normalize_symbol(symbol) in _INTERNATIONAL_ADR_SET


def international_universe(data_columns) -> list[str]:
    """ADR candidates present in price data (curated list; no forex)."""
    cols = {config.normalize_symbol(c) for c in data_columns}
    return [s for s in INTERNATIONAL_ADR_SYMBOLS if s in cols]


def _macro_summary_from_window(data, *, bar_idx: int | None = None) -> dict[str, Any]:
    """Lightweight macro hints for backtest triggers (no Ollama)."""
    summary: dict[str, Any] = {}
    if data is None or len(data) < 2:
        return summary
    idx = bar_idx if bar_idx is not None else len(data) - 1
    idx = max(1, min(idx, len(data) - 1))
    row = data.iloc[idx]
    prev = data.iloc[idx - 1]

    def _pct(sym: str) -> float | None:
        if sym not in data.columns:
            return None
        try:
            c0 = float(prev[sym])
            c1 = float(row[sym])
            if c0 <= 0:
                return None
            return (c1 / c0 - 1.0) * 100.0
        except (TypeError, ValueError):
            return None

    gold = _pct("GLD")
    if gold is not None:
        summary["gold_change"] = gold
    oil = _pct("XOM") or _pct("CVX")
    if oil is not None:
        summary["oil_change"] = oil
    if "SPY" in data.columns:
        spy = float(row["SPY"])
        ma50 = data["SPY"].iloc[max(0, idx - 49) : idx + 1].mean()
        summary["spy_trend"] = "above MA" if spy >= ma50 else "below MA"
    tech_leaders = []
    for sym in ("NVDA", "ASML", "TSM"):
        ch = _pct(sym)
        if ch is not None and ch > 0:
            tech_leaders.append({"sector": "Semis/AI", "symbol": sym, "change": ch})
    if tech_leaders:
        summary["sector_leaders"] = tech_leaders
    return summary


def international_trigger_context(
    *,
    market_summary: dict | None = None,
    thinking_scales: dict | None = None,
    regime: str = "",
    data=None,
    bar_idx: int | None = None,
) -> tuple[bool, float, str]:
    """
    Return (active, cap_pct 0..INTERNATIONAL_SLEEVE_CAP_PCT, reason).

    Triggers: global rotation / USD weakness / AI-tech cycle / thinking tilt.
    """
    summary = dict(market_summary or {})
    if not summary and data is not None:
        summary = _macro_summary_from_window(data, bar_idx=bar_idx)

    cap_max = config.INTERNATIONAL_SLEEVE_CAP_PCT
    reasons: list[str] = []
    score = 0.0

    gold = float(summary.get("gold_change") or 0.0)
    if gold >= 2.0:
        score += 0.35
        reasons.append(f"USD-weakness proxy (GLD {gold:+.1f}%)")

    oil = float(summary.get("oil_change") or 0.0)
    if oil >= 3.0:
        score += 0.20
        reasons.append(f"commodity/global cycle (oil {oil:+.1f}%)")

    theme = str(summary.get("news_theme_summary") or summary.get("news_digest") or "").lower()
    if any(k in theme for k in ("geopolit", "global", "international", "europe", "asia")):
        score += 0.25
        reasons.append("global rotation headline theme")
    if any(k in theme for k in ("tech", "ai", "semi")):
        score += 0.20
        reasons.append("AI/tech cycle theme")

    leaders = summary.get("sector_leaders") or []
    if any("Semi" in str(r.get("sector", "")) or "Tech" in str(r.get("sector", "")) for r in leaders[:2]):
        score += 0.15
        reasons.append("semis/AI leadership")

    if "above MA" in str(summary.get("spy_trend", "")) and score > 0:
        score += 0.10

    if thinking_scales and (
        config.effective_thinking_engine_enabled()
        or any(abs(float(v) - 1.0) > 0.02 for v in thinking_scales.values() if v is not None)
    ):
        intl_scale = float(thinking_scales.get("international") or thinking_scales.get("intl") or 0.0)
        nyse_scale = float(thinking_scales.get("nyse") or thinking_scales.get("nyse_scale") or 0.0)
        if intl_scale > 0.05:
            score += 0.40
            reasons.append("thinking engine international tilt")
        elif nyse_scale > 0.08 and score >= 0.35:
            score += 0.15
            reasons.append("thinking global risk-on with macro confirm")

    impact = float(summary.get("news_impact_score") or 0.0)
    if impact >= 0.35:
        score += 0.15
        reasons.append(f"high news impact ({impact:.2f})")

    if "BULL" in str(regime).upper() and score >= 0.35:
        score += 0.05

    if score < 0.45:
        return False, 0.0, ""

    cap = round(min(cap_max, max(0.05, cap_max * min(1.0, score))), 4)
    reason = "; ".join(reasons[:3]) if reasons else "macro/thinking international trigger"
    return True, cap, reason


def log_adr_risk_once() -> None:
    global _adr_risk_logged
    if _adr_risk_logged:
        return
    _adr_risk_logged = True
    logger.info(ADR_RISK_NOTE)


def _portfolio_stats(executor) -> dict:
    portfolio = getattr(executor, "portfolio", None)
    if portfolio is not None:
        stats = getattr(portfolio, "international_stats", None)
        if stats is None:
            stats = {"trades": 0, "symbols": Counter(), "active_bars": 0}
            portfolio.international_stats = stats
        return stats
    stats = getattr(executor, "international_stats", None)
    if stats is None:
        stats = {"trades": 0, "symbols": Counter(), "active_bars": 0}
        executor.international_stats = stats
    return stats


def record_international_trade(executor, symbol: str) -> None:
    stats = _portfolio_stats(executor)
    stats["trades"] += 1
    stats["symbols"][config.normalize_symbol(symbol)] += 1


def note_international_active_bar(executor) -> None:
    stats = _portfolio_stats(executor)
    stats["active_bars"] = int(stats.get("active_bars", 0) or 0) + 1


def international_stats_summary(executor) -> dict[str, Any]:
    stats = _portfolio_stats(executor)
    symbols = stats.get("symbols") or Counter()
    top = symbols.most_common(8)
    return {
        "trades": int(stats.get("trades") or 0),
        "top_symbols": top,
        "active_bars": int(stats.get("active_bars") or 0),
    }
