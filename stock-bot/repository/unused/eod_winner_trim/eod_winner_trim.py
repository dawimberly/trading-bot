"""EOD partial trim on faded winners (research/backtest only)."""

from __future__ import annotations

import logging
import os

import config

logger = logging.getLogger(__name__)

EOD_TRIM_MIN_DAY_GAIN_PCT = float(
    os.getenv("PAPER_EOD_WINNER_TRIM_MIN_GAIN", "0.05")
)
EOD_TRIM_FRACTION = float(os.getenv("PAPER_EOD_WINNER_TRIM_FRACTION", "0.50"))
EOD_TRIM_CLOSE_RANGE_MAX = float(
    os.getenv("PAPER_EOD_WINNER_TRIM_CLOSE_RANGE", "0.35")
)
EOD_TRIM_RSI_MIN = float(os.getenv("PAPER_EOD_WINNER_TRIM_RSI", "70"))
EOD_TRIM_STRONG_RANGE_MIN = float(
    os.getenv("PAPER_EOD_WINNER_TRIM_STRONG_RANGE", "0.50")
)
EOD_TRIM_STRONG_RSI_MAX = float(
    os.getenv("PAPER_EOD_WINNER_TRIM_STRONG_RSI", "65")
)
EOD_TRIM_RANGE_LOOKBACK = int(os.getenv("PAPER_EOD_WINNER_TRIM_LOOKBACK", "5"))


def _stats(executor) -> dict:
    if not hasattr(executor, "eod_winner_trim_stats"):
        executor.eod_winner_trim_stats = {
            "trims": 0,
            "skipped_strong": 0,
            "candidates": 0,
        }
    return executor.eod_winner_trim_stats


def eod_winner_trim_enabled() -> bool:
    try:
        return bool(
            config.backtest_paper_sleeves_context()
            and config.PAPER_EOD_WINNER_TRIM_ENABLED
        )
    except AttributeError:
        return False


def is_eligible_symbol(symbol: str) -> bool:
    sym = config.normalize_symbol(symbol)
    if sym == config.VTI_CORE_SYMBOL:
        return False
    if config.is_crypto(sym) or config.is_metal_symbol(sym):
        return False
    if sym != config.SPY_BOT_SYMBOL and not config._nyse_eligible_symbol(sym):
        return False
    return True


def _close_range_position(closes, lookback: int = EOD_TRIM_RANGE_LOOKBACK) -> float | None:
    if closes is None or len(closes) < 2:
        return None
    window = closes.iloc[-lookback:] if len(closes) >= lookback else closes
    hi = float(window.max())
    lo = float(window.min())
    last = float(window.iloc[-1])
    if hi <= lo:
        return 0.5
    return (last - lo) / (hi - lo)


def _position_gain_pct(entry: float, current: float) -> float:
    if entry <= 0 or current <= 0:
        return 0.0
    return (current - entry) / entry


def _fade_signals(
    symbol: str,
    data,
    bar_idx: int | None,
    *,
    entry: float | None = None,
    current: float | None = None,
) -> tuple[bool, bool, dict]:
    """Return (should_trim, hold_strong, debug).

    Daily-bar proxy: trim extended winners showing exhaustion (high RSI + off highs),
    not literal intraday fade (needs OHLC for that).
    """
    sym = config.normalize_symbol(symbol)
    if data is None or not hasattr(data, "columns") or sym not in data.columns:
        return False, False, {}
    if bar_idx is not None:
        prices = data[sym].iloc[: bar_idx + 1].dropna()
    else:
        prices = data[sym].dropna()
    if len(prices) < 2:
        return False, False, {}

    last = float(prices.iloc[-1])
    day_gain = float(last / float(prices.iloc[-2]) - 1.0)
    range_pos = _close_range_position(prices)
    pos_gain = None
    if entry and current and entry > 0 and current > 0:
        pos_gain = _position_gain_pct(entry, current)

    rsi = None
    try:
        from modules.pipeline_strategies import _nyse_symbol_rsi

        rsi = _nyse_symbol_rsi(sym, prices.to_frame(name=sym))
    except Exception:
        pass

    dbg = {
        "day_gain": round(day_gain, 4),
        "pos_gain": round(pos_gain, 4) if pos_gain is not None else None,
        "range_pos": round(range_pos, 4) if range_pos is not None else None,
        "rsi": round(rsi, 2) if rsi is not None else None,
    }

    strong = (
        range_pos is not None
        and range_pos >= EOD_TRIM_STRONG_RANGE_MIN
        and rsi is not None
        and rsi < EOD_TRIM_STRONG_RSI_MAX
        and day_gain >= 0
    )
    if strong:
        return False, True, dbg

    hot_day = day_gain >= EOD_TRIM_MIN_DAY_GAIN_PCT
    hot_position = pos_gain is not None and pos_gain >= EOD_TRIM_MIN_DAY_GAIN_PCT
    if not hot_day and not hot_position:
        return False, False, dbg

    weak_close = range_pos is not None and range_pos <= EOD_TRIM_CLOSE_RANGE_MAX
    high_rsi = rsi is not None and rsi >= EOD_TRIM_RSI_MIN
    off_high = range_pos is not None and range_pos <= 0.55
    exhausted = high_rsi and (weak_close or off_high or hot_day)
    return bool(exhausted), False, dbg


def run_eod_winner_trims(
    executor,
    *,
    full_data=None,
    bar_idx: int | None = None,
    now=None,
) -> int:
    """Partial trim (~50%) on NYSE longs up on the day with fade signals."""
    if not eod_winner_trim_enabled():
        return 0

    trims = 0
    stats = _stats(executor)
    items = []
    if hasattr(executor, "portfolio"):
        for sym, qty in executor.portfolio.positions.items():
            if float(qty) <= 0:
                continue
            pos = executor._find_position(sym)
            if pos is not None:
                items.append(pos)
    else:
        try:
            items = [p for p in executor._get_positions() if float(p.qty) > 0]
        except Exception as exc:
            logger.warning("eod_winner_trim: positions failed: %s", exc)
            return 0

    for pos in items:
        symbol = config.normalize_symbol(pos.symbol)
        if not is_eligible_symbol(symbol):
            continue

        entry = float(pos.avg_entry_price or 0)
        current = float(pos.current_price or 0)
        if hasattr(executor, "prices") and executor.prices.get(pos.symbol) is not None:
            current = float(executor.prices.get(pos.symbol))
        if entry <= 0 or current <= 0:
            continue

        should_trim, hold_strong, dbg = _fade_signals(
            symbol, full_data, bar_idx, entry=entry, current=current
        )
        if hold_strong:
            stats["skipped_strong"] = stats.get("skipped_strong", 0) + 1
            continue
        if not should_trim:
            continue

        stats["candidates"] = stats.get("candidates", 0) + 1
        mv = float(pos.qty) * current
        reduce_n = round(mv * EOD_TRIM_FRACTION, 2)
        if reduce_n < config.effective_min_notional(mv):
            continue

        try:
            order = executor.execute_reduce_notional(
                symbol,
                reduce_n,
                reason="eod_winner_trim",
                sleeve="NYSE",
            )
        except TypeError:
            order = executor.execute_reduce_notional(symbol, reduce_n)
        if order is None:
            continue
        if hasattr(executor, "order_filled") and not executor.order_filled(order):
            continue
        if isinstance(order, dict) and not order:
            continue

        trims += 1
        stats["trims"] = stats.get("trims", 0) + 1
        try:
            from modules.pipeline_strategies import mark_nyse_sold_today

            mark_nyse_sold_today(symbol, now=now, data=full_data)
        except Exception:
            pass
        pnl = (current - entry) / entry
        logger.info(
            "eod_winner_trim %s pnl=%.2f%% day=%.2f%% range=%s rsi=%s",
            symbol,
            pnl * 100,
            dbg.get("day_gain", 0) * 100,
            dbg.get("range_pos"),
            dbg.get("rsi"),
        )

    return trims
