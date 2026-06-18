"""Top 1% asymmetric loss cutting + scaled profit takes (research/paper backtest)."""

from __future__ import annotations

import logging
import os
from typing import Any

import config

logger = logging.getLogger(__name__)

NORMAL_STOP_LOSS_PCT = float(os.getenv("TOP1_NORMAL_STOP_LOSS_PCT", "-0.07"))
SPECULATIVE_STOP_LOSS_PCT = float(os.getenv("TOP1_SPECULATIVE_STOP_LOSS_PCT", "-0.04"))
TIGHTENED_NORMAL_STOP_PCT = float(os.getenv("TOP1_TIGHTENED_NORMAL_STOP_PCT", "-0.05"))
TIGHTENED_SPECULATIVE_STOP_PCT = float(os.getenv("TOP1_TIGHTENED_SPECULATIVE_STOP_PCT", "-0.03"))

TAKE_LEVELS = tuple(
    float(x)
    for x in os.getenv("TOP1_TAKE_LEVELS", "0.06,0.12,0.20").split(",")
    if x.strip()
)
TAKE_FRACTIONS = tuple(
    float(x)
    for x in os.getenv("TOP1_TAKE_FRACTIONS", "0.30,0.30,0.40").split(",")
    if x.strip()
)
HIGH_CONVICTION_THRESHOLD = float(os.getenv("TOP1_LOSS_CUT_CONVICTION", "0.70"))
THINKING_NEGATIVE_THRESHOLD = float(os.getenv("TOP1_THINKING_NEGATIVE", "0.55"))
TRAIL_ARM_GAIN_PCT = float(os.getenv("TOP1_TRAIL_ARM_GAIN", "0.06"))
TRAIL_STOP_PCT = float(os.getenv("TOP1_TRAIL_STOP_PCT", "0.10"))

CONSERVATIVE_SPEC_STOP_PCT = float(os.getenv("TOP1_CONSERVATIVE_SPEC_STOP_PCT", "-0.05"))
CONSERVATIVE_ATR_STOP_MULT = float(os.getenv("TOP1_CONSERVATIVE_ATR_STOP_MULT", "1.5"))
CONSERVATIVE_TRAIL_ARM_GAIN_PCT = float(os.getenv("TOP1_CONSERVATIVE_TRAIL_ARM_GAIN", "0.08"))
CONSERVATIVE_TRAIL_STOP_PCT = float(os.getenv("TOP1_CONSERVATIVE_TRAIL_STOP_PCT", "0.12"))
CONSERVATIVE_CONVICTION_THRESHOLD = float(os.getenv("TOP1_CONSERVATIVE_CONVICTION", "0.65"))

_loss_cutting_conservative_ctx = False


def set_loss_cutting_conservative(enabled: bool) -> None:
    global _loss_cutting_conservative_ctx
    _loss_cutting_conservative_ctx = bool(enabled)


def loss_cutting_conservative_mode() -> bool:
    if _loss_cutting_conservative_ctx:
        return True
    try:
        return bool(config.effective_top1_loss_conservative())
    except AttributeError:
        pass
    return os.getenv("TOP1_LOSS_CUT_CONSERVATIVE", "").lower() in ("1", "true", "yes")


def loss_cutting_enabled() -> bool:
    try:
        return bool(config.effective_loss_cutting_enabled())
    except AttributeError:
        return False


def _book(executor):
    return getattr(executor, "portfolio", executor)


def _is_live_alpaca_executor(executor) -> bool:
    return hasattr(executor, "_get_positions") and not hasattr(executor, "portfolio")


def _long_position_items(executor) -> list:
    """Backtest portfolio positions or live Alpaca Position objects."""
    if _is_live_alpaca_executor(executor):
        try:
            return [p for p in executor._get_positions() if float(p.qty) > 0]
        except Exception as exc:
            logger.warning("loss_cutting: failed to get positions: %s", exc)
            return []
    book = getattr(executor, "portfolio", None)
    if book is not None and hasattr(book, "positions"):
        items = []
        for sym, qty in book.positions.items():
            if float(qty) <= 0:
                continue
            pos = executor._find_position(sym) if hasattr(executor, "_find_position") else None
            if pos is not None:
                items.append(pos)
        return items
    return []


def _held_symbols(executor) -> set[str]:
    held: set[str] = set()
    if _is_live_alpaca_executor(executor):
        try:
            for pos in executor._get_positions():
                if float(pos.qty) > 0:
                    held.add(config.normalize_symbol(pos.symbol))
        except Exception:
            return set()
        return held
    book = getattr(executor, "portfolio", None)
    if book is not None and hasattr(book, "positions"):
        for sym, qty in book.positions.items():
            if float(qty) > 0:
                held.add(config.normalize_symbol(sym))
    return held


def _state_map(executor) -> dict[str, dict]:
    book = _book(executor)
    if not hasattr(book, "_loss_cutting_state"):
        book._loss_cutting_state = {}
    return book._loss_cutting_state


def _stats(executor) -> dict[str, Any]:
    book = _book(executor)
    if not hasattr(book, "loss_cutting_stats"):
        book.loss_cutting_stats = {
            "hard_stops": 0,
            "partial_takes": 0,
            "trail_exits": 0,
            "thinking_tightened": 0,
            "sector_tightened": 0,
            "by_symbol": {},
        }
    return book.loss_cutting_stats


def _sym_stats(stats: dict, symbol: str) -> dict:
    sym = config.normalize_symbol(symbol)
    return stats.setdefault("by_symbol", {}).setdefault(
        sym,
        {
            "hard_stops": 0,
            "partial_takes": 0,
            "trail_exits": 0,
            "speculative": 0,
        },
    )


def _position_pnl_pct(entry: float, current: float) -> float:
    if entry <= 0 or current <= 0:
        return 0.0
    return (current - entry) / entry


def _thinking_context(executor) -> dict:
    return getattr(executor, "_top1_sizing_ctx", {}) or {}


def _thinking_confidence(executor) -> float:
    return float(_thinking_context(executor).get("thinking_confidence") or 0.72)


def _sector_headwind(executor, symbol: str) -> bool:
    summary = _thinking_context(executor).get("market_summary") or {}
    try:
        from modules.sector_rotation import ticker_sector

        sym_sector = ticker_sector(symbol)
        laggards = {
            str(r.get("sector", ""))
            for r in (summary.get("sector_laggards") or [])
        }
        return sym_sector in laggards and bool(laggards)
    except Exception:
        return False


def _thinking_negative(executor) -> bool:
    return _thinking_confidence(executor) < THINKING_NEGATIVE_THRESHOLD


def is_eligible_symbol(
    symbol: str,
    *,
    data=None,
    bar_idx: int | None = None,
) -> bool:
    sym = config.normalize_symbol(symbol)
    if sym == config.VTI_CORE_SYMBOL:
        return False
    if config.is_crypto(sym) or config.is_metal_symbol(sym):
        return False
    if sym != config.SPY_BOT_SYMBOL and not config._nyse_eligible_symbol(sym):
        return False
    return True


def _effective_stop(
    executor,
    symbol: str,
    *,
    data=None,
    bar_idx: int | None = None,
    full_data=None,
    prices=None,
) -> tuple[float, bool, bool, str]:
    from modules.vol_position_sizing import is_speculative_name

    speculative, spec_reason = is_speculative_name(
        symbol,
        data=data,
        bar_idx=bar_idx,
        full_data=full_data,
        prices=prices,
    )
    base = SPECULATIVE_STOP_LOSS_PCT if speculative else NORMAL_STOP_LOSS_PCT
    tightened = False
    reasons: list[str] = []

    if _thinking_negative(executor):
        tightened = True
        reasons.append("thinking negative")
    if _sector_headwind(executor, symbol):
        tightened = True
        reasons.append("sector headwind")

    if tightened:
        stop = (
            TIGHTENED_SPECULATIVE_STOP_PCT
            if speculative
            else TIGHTENED_NORMAL_STOP_PCT
        )
    else:
        stop = base

    return stop, speculative, tightened, spec_reason


def _sell_fraction(
    executor,
    pos,
    fraction: float,
    *,
    reason: str,
    symbol: str,
) -> bool:
    price = float(pos.current_price or 0)
    if price <= 0 or float(pos.qty) <= 0:
        return False
    notional = float(pos.qty) * price * fraction
    if notional < 1:
        return False
    order = executor.execute_reduce_notional(
        pos.symbol,
        notional,
        reason=reason,
        sleeve="loss_cutting",
    )
    if order is None:
        return False
    if hasattr(executor, "order_filled") and not executor.order_filled(order):
        return False
    stats = _stats(executor)
    stats["partial_takes"] = stats.get("partial_takes", 0) + 1
    _sym_stats(stats, symbol)["partial_takes"] += 1
    return True


def _conservative_spec_stop(
    symbol: str,
    *,
    full_data=None,
    bar_idx: int | None = None,
) -> float:
    """Wider of fixed -5% or ATR-based stop (min = more negative)."""
    stop = CONSERVATIVE_SPEC_STOP_PCT
    frame = full_data
    sym = config.normalize_symbol(symbol)
    if frame is not None and sym in getattr(frame, "columns", []):
        try:
            from modules.vol_position_sizing import _atr_pct_from_close

            series = frame[sym]
            if bar_idx is not None:
                series = series.iloc[: bar_idx + 1]
            atr_pct = _atr_pct_from_close(series.dropna())
            atr_stop = -CONSERVATIVE_ATR_STOP_MULT * atr_pct
            stop = min(stop, atr_stop)
        except Exception:
            pass
    return stop


def _conservative_mild_trail(
    executor,
    pos,
    sym: str,
    *,
    entry: float,
    current: float,
    pnl: float,
    row: dict,
) -> bool:
    """Mild trailing exit after +8% when Thinking confidence >= 0.65."""
    conf = _thinking_confidence(executor)
    if conf < CONSERVATIVE_CONVICTION_THRESHOLD:
        return False
    if pnl >= CONSERVATIVE_TRAIL_ARM_GAIN_PCT:
        row["trail_armed"] = True
    if not row.get("trail_armed"):
        return False
    row["hwm"] = max(float(row.get("hwm") or current), current)
    floor = float(row["hwm"]) * (1.0 - CONSERVATIVE_TRAIL_STOP_PCT)
    if current > floor or pnl <= 0:
        return False
    logger.info(
        "Conservative mild trail on %s (conf=%.2f, pnl=%.1f%%, trail %.0f%% from HWM)",
        sym,
        conf,
        pnl * 100,
        CONSERVATIVE_TRAIL_STOP_PCT * 100,
    )
    return _full_exit(
        executor,
        pos,
        reason="conservative_trail",
        symbol=sym,
        stat_key="trail_exits",
    )


def _full_exit(executor, pos, *, reason: str, symbol: str, stat_key: str) -> bool:
    try:
        order = executor.execute_full_exit(
            pos.symbol, reason=reason, sleeve="loss_cutting"
        )
    except TypeError:
        order = executor.execute_full_exit(pos.symbol)
    if order is None:
        return False
    if hasattr(executor, "order_filled") and not executor.order_filled(order):
        return False
    stats = _stats(executor)
    stats[stat_key] = stats.get(stat_key, 0) + 1
    _sym_stats(stats, symbol)[stat_key] += 1
    _state_map(executor).pop(config.normalize_symbol(symbol), None)
    try:
        from modules.vol_position_sizing import release_top1_risk_on_sell

        release_top1_risk_on_sell(
            _book(executor), pos.symbol, float(pos.qty), float(pos.qty)
        )
    except ImportError:
        pass
    return True


def run_loss_cutting_exits(
    executor,
    *,
    bar_idx: int | None = None,
    full_data=None,
    equity_session_open: bool = True,
) -> int:
    """Hard stops, partial profit scale-outs, and conviction trailing exits."""
    if not loss_cutting_enabled() or not equity_session_open:
        return 0

    actions = 0
    try:
        _prune_state(executor)
    except Exception as exc:
        logger.warning("loss_cutting: prune state skipped: %s", exc)

    items = _long_position_items(executor)
    if not items and not _is_live_alpaca_executor(executor) and not equity_session_open:
        return 0

    for pos in items:
        sym = config.normalize_symbol(pos.symbol)
        if not is_eligible_symbol(sym, data=full_data, bar_idx=bar_idx):
            continue

        entry = float(pos.avg_entry_price or 0)
        current = float(pos.current_price or 0)
        if hasattr(executor, "prices") and executor.prices.get(pos.symbol) is not None:
            current = float(executor.prices.get(pos.symbol))
        if entry <= 0 or current <= 0:
            continue

        pnl = _position_pnl_pct(entry, current)
        stop, speculative, tightened, spec_reason = _effective_stop(
            executor,
            sym,
            data=full_data,
            bar_idx=bar_idx,
            full_data=full_data,
            prices=getattr(executor, "prices", None),
        )
        row = _state_map(executor).setdefault(
            sym,
            {"levels_taken": set(), "trail_armed": False, "hwm": 0.0},
        )
        if speculative:
            _sym_stats(_stats(executor), sym)["speculative"] = 1

        if loss_cutting_conservative_mode():
            if not speculative:
                continue
            stop = SPECULATIVE_STOP_LOSS_PCT
            if pnl <= stop:
                extra = f" ({spec_reason})" if spec_reason else ""
                logger.info(
                    "Conservative hard stop %.1f%% on speculative %s%s (limit %.0f%%)",
                    pnl * 100,
                    sym,
                    extra,
                    stop * 100,
                )
                if _full_exit(
                    executor,
                    pos,
                    reason="hard_stop_speculative",
                    symbol=sym,
                    stat_key="hard_stops",
                ):
                    actions += 1
            continue

        if pnl <= stop:
            label = "speculative" if speculative else "normal"
            extra = f" ({spec_reason})" if speculative and spec_reason else ""
            if tightened:
                if _thinking_negative(executor):
                    _stats(executor)["thinking_tightened"] = (
                        _stats(executor).get("thinking_tightened", 0) + 1
                    )
                if _sector_headwind(executor, sym):
                    _stats(executor)["sector_tightened"] = (
                        _stats(executor).get("sector_tightened", 0) + 1
                    )
            logger.info(
                "Hard stop hit %.1f%% on %s %s name%s (limit %.0f%%)",
                pnl * 100,
                sym,
                label,
                extra,
                stop * 100,
            )
            if _full_exit(
                executor,
                pos,
                reason=f"hard_stop_{label}",
                symbol=sym,
                stat_key="hard_stops",
            ):
                actions += 1
            continue

        conf = _thinking_confidence(executor)
        high_conviction = conf >= HIGH_CONVICTION_THRESHOLD

        if high_conviction:
            if pnl >= TRAIL_ARM_GAIN_PCT:
                row["trail_armed"] = True
            if row.get("trail_armed"):
                row["hwm"] = max(float(row.get("hwm") or current), current)
                floor = float(row["hwm"]) * (1.0 - TRAIL_STOP_PCT)
                if current <= floor and pnl > 0:
                    logger.info(
                        "Trailing stop hit on high-conviction %s "
                        "(conf=%.2f, pnl=%.1f%%, trail %.0f%% from HWM)",
                        sym,
                        conf,
                        pnl * 100,
                        TRAIL_STOP_PCT * 100,
                    )
                    if _full_exit(
                        executor,
                        pos,
                        reason="conviction_trail",
                        symbol=sym,
                        stat_key="trail_exits",
                    ):
                        actions += 1
                    continue
        else:
            levels = row.setdefault("levels_taken", set())
            for level, frac in zip(TAKE_LEVELS, TAKE_FRACTIONS):
                if level in levels or pnl < level:
                    continue
                logger.info(
                    "Partial take +%.0f%% on %s: sell %.0f%% at pnl %.1f%%",
                    level * 100,
                    sym,
                    frac * 100,
                    pnl * 100,
                )
                if _sell_fraction(
                    executor,
                    pos,
                    frac,
                    reason=f"take_{level:.0%}",
                    symbol=sym,
                ):
                    levels.add(level)
                    actions += 1
                    pos = executor._find_position(sym)
                    if pos is None or float(pos.qty) <= 0:
                        break

        _state_map(executor)[sym] = row

    return actions


def _prune_state(executor) -> None:
    held = _held_symbols(executor)
    stale = [s for s in _state_map(executor) if s not in held]
    for s in stale:
        _state_map(executor).pop(s, None)


def format_loss_cutting_report(stats: dict | None) -> str:
    if not stats:
        return "no loss-cutting activity"
    by = stats.get("by_symbol") or {}
    spcx = by.get("SPCX", {})
    spcx_bit = ""
    if spcx:
        spcx_bit = (
            f" | SPCX: stops {spcx.get('hard_stops', 0)} "
            f"partials {spcx.get('partial_takes', 0)} "
            f"trails {spcx.get('trail_exits', 0)}"
        )
    return (
        f"hard stops {stats.get('hard_stops', 0)} | "
        f"partial takes {stats.get('partial_takes', 0)} | "
        f"trail exits {stats.get('trail_exits', 0)} | "
        f"thinking tightened {stats.get('thinking_tightened', 0)} | "
        f"sector tightened {stats.get('sector_tightened', 0)}"
        f"{spcx_bit}"
    )
