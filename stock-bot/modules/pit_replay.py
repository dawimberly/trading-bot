"""Point-in-time (PIT) replay for news, social, and thinking inputs during backtests.

Eliminates look-ahead from live web sentiment, unsliced macro series, and
same-bar premarket news that peeks at the closing print.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

import config
from modules.wayback_sentiment import load_monthly_web_sentiment, web_sentiment_for_date

logger = logging.getLogger(__name__)

DEFAULT_PIT_EQUITY_SLIPPAGE_BPS = float(
    __import__("os").getenv("STRICT_PIT_EQUITY_SLIPPAGE_BPS", "8")
)
DEFAULT_PIT_EQUITY_COMMISSION_BPS = float(
    __import__("os").getenv("STRICT_PIT_EQUITY_COMMISSION_BPS", "1")
)
DEFAULT_PIT_CRYPTO_SLIPPAGE_BPS = float(
    __import__("os").getenv("STRICT_PIT_CRYPTO_SLIPPAGE_BPS", "12")
)


@dataclass
class PitBarContext:
    bar_ts: pd.Timestamp
    bar_index: int
    slot: str = "close"


_current: PitBarContext | None = None
_monthly_web: pd.Series | None = None


def pit_enabled() -> bool:
    return config.effective_strict_pit_backtest()


def set_pit_bar_context(bar_ts: pd.Timestamp, bar_index: int, slot: str = "close") -> None:
    global _current
    _current = PitBarContext(
        bar_ts=pd.Timestamp(bar_ts),
        bar_index=int(bar_index),
        slot=str(slot or "close"),
    )


def get_pit_bar_context() -> PitBarContext | None:
    return _current


def clear_pit_bar_context() -> None:
    global _current
    _current = None


def _normalize_ts(ts: pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is not None:
        ts = ts.tz_convert(None)
    return ts.normalize()


def effective_as_of_ts(
    bar_ts: pd.Timestamp,
    *,
    slot: str = "close",
    strict: bool | None = None,
) -> pd.Timestamp:
    """Decision cutoff: premarket uses prior session close; close uses bar date."""
    bar_ts = _normalize_ts(bar_ts)
    if strict is None:
        strict = pit_enabled()
    if not strict:
        return bar_ts
    if slot == "premarket":
        return bar_ts - pd.Timedelta(days=1)
    return bar_ts


def slice_series_as_of(series: pd.Series, as_of: pd.Timestamp) -> pd.Series:
    if series is None or series.empty:
        return series
    as_of = _normalize_ts(as_of)
    idx = series.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert(None)
        work = series.copy()
        work.index = idx
    else:
        work = series
    mask = work.index.normalize() <= as_of
    return work.loc[mask]


def pit_thinking_window(
    data: pd.DataFrame,
    *,
    bar_index: int,
    slot: str = "close",
    strict: bool | None = None,
) -> pd.DataFrame:
    """Price window visible to thinking/news at the simulated decision time."""
    if strict is None:
        strict = pit_enabled()
    if not strict:
        return data
    if slot == "premarket" and bar_index > 0:
        return data.iloc[:bar_index]
    return data.iloc[: bar_index + 1]


def _monthly_web_series() -> pd.Series:
    global _monthly_web
    if _monthly_web is None:
        try:
            _monthly_web = load_monthly_web_sentiment()
        except Exception:
            _monthly_web = pd.Series(dtype=float)
    return _monthly_web


def pit_web_sentiment(as_of: pd.Timestamp) -> float | None:
    monthly = _monthly_web_series()
    if monthly.empty:
        return None
    val = web_sentiment_for_date(monthly, as_of)
    if val is None or not np.isfinite(val):
        return None
    return float(val)


def pit_wisdom_context(as_of: pd.Timestamp, price_sentiment: float | None = None) -> dict[str, Any]:
    web = pit_web_sentiment(as_of)
    ctx: dict[str, Any] = {"as_of": str(as_of.date())}
    if web is not None:
        ctx["web_sentiment"] = web
    if price_sentiment is not None:
        ctx["price_sentiment"] = float(price_sentiment)
    return ctx


def synthesize_pit_news(
    data: pd.DataFrame,
    regime: str,
    vol: str,
    *,
    bar_ts: pd.Timestamp,
    bar_index: int,
    slot: str = "premarket",
) -> dict[str, Any]:
    """PIT-safe headline digest — macro/news inputs sliced to decision cutoff."""
    from modules.thinking_news import build_news_digest

    as_of = effective_as_of_ts(bar_ts, slot=slot, strict=True)
    window = pit_thinking_window(data, bar_index=bar_index, slot=slot, strict=True)
    if window.empty:
        return build_news_digest(
            [f"Macro tape: regime {regime}, vol {vol} — insufficient history"],
            slot=slot,
        )
    from modules.thinking_engine import build_market_summary

    summary = build_market_summary(
        window,
        regime,
        vol,
        as_of=as_of,
        pit_slot=slot,
    )
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
    if str(regime).startswith("RHYME") and vix_f <= 16 and tech_leading and oil >= 2.5:
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

    digest = build_news_digest(
        headlines[:6],
        slot=slot,
        ai_cycle_phase=str(summary.get("ai_cycle_phase") or ""),
    )
    digest["pit_as_of"] = str(as_of.date())
    digest["pit_slot"] = slot
    return digest


def apply_strict_pit_execution_costs(run_options) -> None:
    """Bump slippage/commission for small-account realism under strict PIT."""
    if not getattr(run_options, "strict_pit", False):
        return
    if not getattr(run_options, "realistic_costs", True):
        return
    run_options.equity_slippage_bps = max(
        float(run_options.equity_slippage_bps),
        DEFAULT_PIT_EQUITY_SLIPPAGE_BPS,
    )
    run_options.crypto_slippage_bps = max(
        float(run_options.crypto_slippage_bps),
        DEFAULT_PIT_CRYPTO_SLIPPAGE_BPS,
    )
    run_options.equity_commission_bps = max(
        float(run_options.equity_commission_bps),
        DEFAULT_PIT_EQUITY_COMMISSION_BPS,
    )


def reset_pit_caches() -> None:
    global _monthly_web
    _monthly_web = None
    clear_pit_bar_context()
