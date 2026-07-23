"""Sector ETF shorts — weak sectors only (paper/research)."""

from __future__ import annotations

import logging
from typing import Callable

import config

logger = logging.getLogger(__name__)


def weak_sector_candidates(data, *, limit: int | None = None) -> list[dict]:
    """Sector ETFs with very negative momentum + relative strength vs SPY."""
    from modules.sector_screener import compute_sector_strengths

    if data is None or getattr(data, "empty", True):
        return []
    max_score = float(config.SECTOR_SHORT_MAX_SCORE)
    min_rs = float(config.SECTOR_SHORT_MIN_RS_VS_SPY)
    cap = limit or int(config.SECTOR_SHORT_MAX_POSITIONS)
    rows: list[dict] = []
    for row in compute_sector_strengths(data):
        score = float(row.get("score") or 0.0)
        rs = float(row.get("rs_vs_spy") or 0.0)
        if score > max_score or rs > min_rs:
            continue
        rows.append(row)
        if len(rows) >= cap:
            break
    return rows


def run_sector_shorts(
    data,
    executor,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_bars=None,
    log_fn: Callable | None = None,
    vol_score: float | None = None,
    bubble_score: float = 0.0,
    vix_reason: str = "",
    trigger_reason: str = "",
) -> int:
    """Short weak sector ETFs when protective short triggers are active."""
    if not config.effective_sector_short_enabled():
        return 0
    from modules.opportunistic_short_sleeve import _open_short, short_regime_active

    if not short_regime_active(regime):
        return 0

    min_bub = config.effective_sector_short_min_bubble()
    bub = float(bubble_score)
    bub_norm = bub if bub <= 1.0 else bub / 100.0
    if bub_norm < min_bub:
        logger.debug(
            "Sector shorts skipped: bubble %.2f < %.2f",
            bub_norm,
            min_bub,
        )
        return 0

    from modules.pipeline_strategies import _spy_bear_streak

    streak_ok, streak = _spy_bear_streak(data, config.SPY_BOT_SYMBOL)
    if not streak_ok:
        logger.debug(
            "Sector shorts skipped: SPY bear streak %s < %s bars",
            streak,
            config.SHORT_RHYME_E_BEAR_STREAK_BARS,
        )
        return 0

    candidates = weak_sector_candidates(data)
    if not candidates:
        return 0

    trades = 0
    ma_window = config.effective_spy_ma_window()
    prices = data.iloc[-1] if hasattr(data, "iloc") else data
    sector_cap = float(config.SECTOR_SHORT_MAX_PCT)

    for row in candidates:
        sym = str(row.get("etf") or "")
        if not sym or sym not in data.columns:
            continue
        pair_key = f"{sym}/SECTOR_SHORT/MA{ma_window}"
        reason = f"{trigger_reason}|sector={sym}|score={float(row.get('score', 0)):.3f}"
        trades += _open_short(
            executor,
            data,
            prices,
            regime,
            now,
            pair_cooldown,
            sym,
            pair_key=pair_key,
            cooldown_bars=cooldown_bars,
            log_fn=log_fn,
            vol_score=vol_score,
            bubble_score=bubble_score,
            vix_reason=vix_reason,
            trigger_reason=reason,
            leg_max_pct=sector_cap,
        )
        logger.info(
            "SECTOR SHORT candidate %s score=%.3f rs=%.3f",
            sym,
            float(row.get("score") or 0),
            float(row.get("rs_vs_spy") or 0),
        )
    return trades
