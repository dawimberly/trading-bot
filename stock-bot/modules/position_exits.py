"""Stop-loss and max-hold exits for open Alpaca positions."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")


def _position_symbol(raw_symbol):
    return config.normalize_symbol(raw_symbol)


def _position_entry_hour_et(pos) -> str:
    """ET hour bucket (HH:00) when the position was opened — for exit journal analytics."""
    created = getattr(pos, "created_at", None) or getattr(pos, "createdAt", None)
    if created is None:
        return ""
    if isinstance(created, str):
        try:
            created = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            return ""
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    try:
        et = created.astimezone(_ET)
    except Exception:
        return ""
    return f"{et.hour:02d}:00"


def _trailing_stop_hit(
    entry: float,
    peak: float,
    current: float,
    *,
    symbol: str = "",
    executor=None,
) -> bool:
    if entry <= 0 or peak <= 0 or current <= 0:
        return False
    if config.effective_exit_optimization_enabled() and executor is not None:
        from modules.exit_management import (
            resolve_symbol_atr_and_conviction,
            trailing_stop_triggered,
        )

        atr, conviction = resolve_symbol_atr_and_conviction(
            executor,
            symbol,
            regime=getattr(executor, "_last_regime", None),
        )
        return trailing_stop_triggered(
            entry,
            peak,
            current,
            symbol=symbol,
            atr=atr,
            regime=getattr(executor, "_last_regime", None),
            conviction=conviction,
            side="long",
        )
    gain = (peak - entry) / entry
    if gain < config.PAPER_TRAILING_STOP_ARM_PCT:
        return False
    trail = config.PAPER_TRAILING_STOP_TRAIL_PCT
    return current <= peak * (1.0 - trail)


def _position_age_bars(pos) -> int | None:
    created = getattr(pos, "created_at", None) or getattr(pos, "createdAt", None)
    if created is None:
        return None
    if isinstance(created, str):
        try:
            created = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - created).total_seconds() / 86400.0
    return max(0, int(round(age_days)))


def _max_hold_bars() -> int:
    if config.effective_exit_optimization_enabled():
        return int(getattr(config, "EXIT_OPTIMIZATION_MAX_HOLD_BARS", 35))
    return int(config.PAPER_POSITION_MAX_HOLD_BARS)


def run_position_exits(
    executor, risk_manager, journal=None, equity_session_open=True, journal_path=None
):
    """Close positions on stop, partial @1R, trailing stop, or max hold."""
    exits = 0
    try:
        positions = executor._get_positions()
    except Exception as e:
        if journal:
            journal.log_event("exit_error", notes=str(e), journal_path=journal_path)
        else:
            logger.warning("position_exits: failed to get positions: %s", e)
        return 0

    account = executor._get_account()
    equity = float(account.equity)
    paper_controls = config.paper_aggressive_context() and config.PAPER_REGIME_DD_RISK_ENABLED
    exit_opt = config.effective_exit_optimization_enabled()
    controls = paper_controls or exit_opt

    peak_cache: dict[str, float] = getattr(executor, "_exit_peak_prices", {}) or {}
    meta_cache: dict[str, dict] = getattr(executor, "_exit_opt_meta", {}) or {}

    for pos in positions:
        symbol = _position_symbol(pos.symbol)
        if config.vti_core_enabled() and symbol == config.VTI_CORE_SYMBOL:
            continue
        if not equity_session_open and not config.is_crypto(symbol):
            continue

        qty = float(pos.qty)
        if qty == 0:
            continue

        entry = float(pos.avg_entry_price or 0)
        current = float(pos.current_price or 0)
        if entry <= 0 or current <= 0:
            continue

        pnl_pct = (current - entry) / entry
        if qty < 0:
            pnl_pct = -pnl_pct

        peak = max(float(peak_cache.get(symbol, entry)), current)
        peak_cache[symbol] = peak
        age_bars = _position_age_bars(pos)
        meta = dict(meta_cache.get(symbol) or {})

        atr_stop_hit = False
        stop_hit = pnl_pct <= -config.STOP_LOSS_PCT
        plpc = getattr(pos, "unrealized_plpc", None)
        if plpc is not None and float(plpc) <= -config.STOP_LOSS_PCT:
            stop_hit = True

        smart_on = config.effective_smart_stops_enabled()
        if smart_on and qty > 0 and not config.is_crypto(symbol):
            from modules.exit_management import resolve_symbol_atr_and_conviction
            from modules.smart_atr_stops import evaluate_smart_stop

            atr, _conv = resolve_symbol_atr_and_conviction(
                executor, symbol, regime=getattr(executor, "_last_regime", None)
            )
            decision = evaluate_smart_stop(
                symbol=symbol,
                entry=entry,
                current=current,
                atr=float(atr),
                meta=meta,
                qty=qty,
                data=getattr(executor, "_sizing_data", None),
                side="long" if qty > 0 else "short",
            )
            meta = decision.get("meta") or meta
            meta_cache[symbol] = meta
            action = decision.get("action")
            if action == "reduce":
                try:
                    reduce_frac = float(decision.get("reduce_frac") or 0.5)
                    reduce_n = round(abs(qty) * current * reduce_frac, 2)
                    order = executor.execute_reduce_notional(
                        symbol,
                        reduce_n,
                        reason=decision.get("exit_code") or "smart_size_reduce",
                        sleeve="NYSE",
                    )
                    if order and executor.order_filled(order):
                        exits += 1
                        risk_manager._log_event(
                            f"SMART STOP REDUCE: {symbol} pnl={pnl_pct:.2%} ({reduce_n:.0f})"
                        )
                        if journal:
                            journal.log_exit(
                                symbol,
                                "sell",
                                decision.get("reason") or "smart_size_reduce",
                                equity,
                                journal_path=journal_path,
                            )
                except Exception as e:
                    if journal:
                        journal.log_event(
                            "exit_error", symbol=symbol, notes=str(e), journal_path=journal_path
                        )
                continue
            if action == "exit":
                atr_stop_hit = True
                stop_hit = True
                meta["smart_exit_code"] = decision.get("exit_code") or "smart_atr_stop"
                meta["smart_exit_reason"] = decision.get("reason") or ""
                meta_cache[symbol] = meta
            else:
                # Smart stops replace fixed −STOP_LOSS_PCT hard exit
                stop_hit = False

        elif exit_opt and qty > 0 and not config.is_crypto(symbol):
            from modules.exit_management import (
                compute_trailing_stop_plan,
                partial_exit_fraction,
                record_exit_event,
                resolve_symbol_atr_and_conviction,
                should_partial_exit,
            )

            atr, conviction = resolve_symbol_atr_and_conviction(
                executor, symbol, regime=getattr(executor, "_last_regime", None)
            )
            plan = compute_trailing_stop_plan(
                symbol, entry, atr, getattr(executor, "_last_regime", None), conviction
            )
            if current <= plan["stop_price"]:
                atr_stop_hit = True
                stop_hit = True
            profit_target = entry + abs(entry - plan["stop_price"]) * float(
                config.PARTIAL_EXIT_RR
            )
            if should_partial_exit(
                {
                    "entry_price": entry,
                    "stop_price": plan["stop_price"],
                    "partial_taken": meta.get("partial_taken"),
                    "qty": qty,
                },
                current,
                age_bars or 0,
                profit_target,
            ):
                try:
                    mv = abs(qty) * current
                    reduce_n = round(mv * partial_exit_fraction(), 2)
                    order = executor.execute_reduce_notional(
                        symbol,
                        reduce_n,
                        reason="partial_1r",
                        sleeve="NYSE",
                    )
                    if order and executor.order_filled(order):
                        meta["partial_taken"] = True
                        meta_cache[symbol] = meta
                        exits += 1
                        record_exit_event(
                            "partial",
                            symbol,
                            sleeve="NYSE",
                            partial=True,
                            notional=reduce_n,
                        )
                        risk_manager._log_event(
                            f"PARTIAL EXIT: {symbol} pnl={pnl_pct:.2%} ({reduce_n:.0f})"
                        )
                        if journal:
                            journal.log_exit(
                                symbol,
                                "sell",
                                f"partial_1r {pnl_pct:.2%}",
                                equity,
                                journal_path=journal_path,
                            )
                except Exception as e:
                    if journal:
                        journal.log_event(
                            "exit_error", symbol=symbol, notes=str(e), journal_path=journal_path
                        )
                continue

        trail_hit = controls and qty > 0 and _trailing_stop_hit(
            entry, peak, current, symbol=symbol, executor=executor
        )

        hold_hit = False
        if controls and qty > 0 and not config.is_crypto(symbol) and age_bars is not None:
            if exit_opt:
                from modules.exit_management import get_time_based_exit

                hold_hit = get_time_based_exit(age_bars, max_hold=_max_hold_bars())
            elif age_bars >= config.PAPER_POSITION_MAX_HOLD_BARS:
                hold_hit = True

        if not (stop_hit or trail_hit or hold_hit):
            continue

        side = "sell" if qty > 0 else "buy"
        try:
            if atr_stop_hit:
                exit_code = meta.get("smart_exit_code") or "atr_stop"
                reason = meta.get("smart_exit_reason") or f"atr_stop {pnl_pct:.2%}"
                event_reason = "stop"
            elif stop_hit:
                exit_code = "stop_loss"
                reason = f"stop_loss {pnl_pct:.2%}"
                event_reason = "stop"
            elif trail_hit:
                exit_code = "take_profit"
                reason = f"trailing_stop {pnl_pct:.2%}"
                event_reason = "trail"
            else:
                exit_code = "max_hold"
                reason = f"max_hold {pnl_pct:.2%}"
                event_reason = "time"

            quality = False
            try:
                quality = bool(config.effective_paper_momentum_quality_fixes())
            except Exception:
                quality = False

            if quality:
                try:
                    order = executor.execute_full_exit(
                        symbol, reason=exit_code, sleeve="NYSE"
                    )
                except TypeError:
                    order = executor.execute_full_exit(symbol)
            else:
                order = executor.execute_full_exit(symbol)
            if not executor.order_filled(order):
                continue
            exits += 1
            peak_cache.pop(symbol, None)
            meta_cache.pop(symbol, None)
            try:
                from modules.pipeline_strategies import mark_nyse_sold_today

                mark_nyse_sold_today(symbol)
            except Exception:
                pass

            if exit_opt:
                from modules.exit_management import record_exit_event

                record_exit_event(
                    event_reason,
                    symbol,
                    sleeve="NYSE",
                    notional=abs(qty) * current,
                )

            risk_manager._log_event(f"EXIT: {symbol} pnl={pnl_pct:.2%} qty={qty} ({reason})")
            if journal:
                entry_hour = _position_entry_hour_et(pos) if quality else ""
                if quality:
                    journal.log_exit(
                        symbol,
                        side,
                        reason,
                        equity,
                        journal_path=journal_path,
                        exit_reason=exit_code,
                        entry_hour=entry_hour,
                    )
                else:
                    journal.log_exit(
                        symbol, side, reason, equity, journal_path=journal_path
                    )
        except Exception as e:
            if journal:
                journal.log_event(
                    "exit_error", symbol=symbol, notes=str(e), journal_path=journal_path
                )

    executor._exit_peak_prices = peak_cache
    executor._exit_opt_meta = meta_cache
    return exits
