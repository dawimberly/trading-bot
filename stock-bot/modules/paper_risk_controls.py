"""Paper-aggressive risk controls: regime/DD sizing, exits, sleeve caps."""

from __future__ import annotations

from typing import TYPE_CHECKING

import config

if TYPE_CHECKING:
    from backtester import BacktestExecutor, BacktestPortfolio


def paper_risk_controls_active() -> bool:
    if config.effective_tail_risk_controls():
        if config.paper_aggressive_context() or config.backtest_paper_sleeves_context():
            return True
        if config.PAPER_TRADING and config.PAPER_AGGRESSIVE_ENABLED:
            return True
    return bool(config.paper_aggressive_context() and config.PAPER_REGIME_DD_RISK_ENABLED)


def regime_dd_risk_multiplier(
    regime: str,
    drawdown: float,
    *,
    recovery_mode: bool = False,
) -> float:
    """Scale position sizing by RHYME regime and current drawdown from peak."""
    if not paper_risk_controls_active():
        return 1.0
    mult = 1.0
    reg = str(regime or "")
    if not config.effective_regime_dynamic_sizing():
        if "RHYME_B" in reg:
            mult *= config.PAPER_REGIME_B_RISK_MULT
        elif "RHYME_D" in reg:
            mult *= config.PAPER_REGIME_D_RISK_MULT
    dd = max(0.0, float(drawdown))
    if dd >= config.PAPER_DD_RISK_SEVERE_PCT:
        mult *= config.PAPER_DD_RISK_MULT_8
    elif dd >= config.PAPER_DD_RISK_WARN_PCT:
        mult *= config.PAPER_DD_RISK_MULT_5
    if recovery_mode:
        mult *= config.PAPER_HALT_RECOVERY_RISK_MULT
    return round(max(0.05, min(1.0, mult)), 4)


def vol_ceiling_risk_multiplier(vol_score: float | None = None) -> float:
    """Scale risk down when cross-asset vol (annualized) exceeds VOL_CEILING_PCT."""
    if not paper_risk_controls_active() or not config.VOL_CEILING_ENABLED:
        return 1.0
    ceiling = float(config.effective_vol_ceiling_pct())
    if ceiling <= 0:
        return 1.0
    vs = float(
        vol_score
        if vol_score is not None
        else config._dynamic_risk_ctx.get("vol_score", 0.02)
    )
    ann_vol = vs * (252**0.5)
    if ann_vol <= ceiling:
        return 1.0
    return round(max(0.25, ceiling / ann_vol), 4)


def effective_sleeve_cap_pct(
    sleeve_key: str,
    sleeve_cap_pct: float,
    *,
    regime: str | None = None,
    cash_pct: float | None = None,
    equity: float | None = None,
    cash: float | None = None,
) -> float:
    """Apply hard per-sleeve exposure ceiling (paper aggressive) + weak-regime cap."""
    if sleeve_key == "nyse" and (
        config.paper_aggressive_context() or config.is_realistic_research_active()
    ):
        return config.effective_nyse_sleeve_cap_pct(
            cash_pct,
            equity=equity,
            cash=cash,
            regime=regime,
            base_pct=sleeve_cap_pct,
        )

    cap = float(sleeve_cap_pct)
    hard = config.paper_sleeve_hard_cap_pct(sleeve_key)
    if hard is not None:
        cap = min(cap, float(hard))
    if regime and config.effective_tail_risk_controls() and "RHYME_B" in str(regime):
        cap = round(cap * (1.0 - float(config.PAPER_REGIME_B_CASH_BUFFER_BOOST)), 6)
    if regime:
        from modules.regime_sizing import regime_sleeve_exposure_ceiling

        weak_ceil = regime_sleeve_exposure_ceiling(regime)
        if weak_ceil is not None:
            cap = min(cap, weak_ceil)
    if config.paper_aggressive_context() and sleeve_key == "spy":
        boost = config.effective_excess_cash_sleeve_mult(
            cash_pct, equity=equity, cash=cash
        )
        if boost > 1.0:
            cap = round(cap * boost, 6)
            headroom = round(
                1.0
                - config.effective_vti_core_pct()
                - (config.METAL_SLEEVE_CAP_PCT if config.metal_sleeve_enabled() else 0.0),
                6,
            )
            sleeve_ceil = headroom * 0.55
            cap = min(cap, round(sleeve_ceil, 6))
    return cap


def _sleeve_predicate(executor, sleeve_key: str):
    if sleeve_key == "spy":
        return executor._is_spy_position
    if sleeve_key == "nyse":
        return executor._is_nyse_sleeve_position
    if sleeve_key == "stat_arb":
        from modules.stat_arb_sleeve import stat_arb_pair_symbols

        pair_syms = stat_arb_pair_symbols(executor)

        def _is_stat_arb(sym: str) -> bool:
            return config.normalize_symbol(sym) in pair_syms

        return _is_stat_arb
    if sleeve_key == "crypto":
        return executor._is_crypto_position
    return lambda _s: False


def _per_name_cap_exempt(sym: str) -> bool:
    """Core trend sleeves are capped by sleeve limits, not per-name %."""
    return sym in (config.VTI_CORE_SYMBOL, config.SPY_BOT_SYMBOL)


def cap_per_name_buy_notional(
    *,
    symbol: str,
    side: str,
    notional: float | None,
    equity: float,
    prices: dict,
    positions: dict,
) -> float | None:
    """Cap buys so a single name cannot exceed PAPER_MAX_POSITION_PCT of equity."""
    if not paper_risk_controls_active():
        return notional
    if side.lower() != "buy" or notional is None or equity <= 0:
        return notional
    sym = config.normalize_symbol(symbol)
    if _per_name_cap_exempt(sym):
        return notional
    cap_val = equity * config.effective_per_name_max_pct()
    px = prices.get(sym)
    if px is None:
        px = prices.get(symbol)
    current_val = 0.0
    if px is not None and float(px) > 0:
        for key, qty in positions.items():
            if config.normalize_symbol(key) == sym:
                current_val += float(qty) * float(px)
    else:
        # New name with no quote — cap order notional to the per-name ceiling.
        current_val = 0.0
        px = None
    room = max(0.0, cap_val - current_val)
    capped = min(float(notional), room)
    min_n = config.effective_min_notional(equity)
    if capped < min_n:
        return None
    return round(capped, 2)


def trim_per_name_overexposure(executor, prices) -> int:
    """Sell down tactical names above PAPER_MAX_POSITION_PCT."""
    if not paper_risk_controls_active():
        return 0
    portfolio = executor.portfolio
    eq = portfolio.equity(prices)
    if eq <= 0:
        return 0
    cap_val = eq * config.PAPER_MAX_POSITION_PCT
    min_n = config.effective_min_notional(eq)
    trims = 0
    for sym in sorted(portfolio.positions.keys()):
        sym_n = config.normalize_symbol(sym)
        if _per_name_cap_exempt(sym_n):
            continue
        qty = float(portfolio.positions.get(sym, 0))
        if qty <= 0:
            continue
        price = prices.get(sym)
        if price is None or float(price) <= 0:
            continue
        pos_val = qty * float(price)
        excess = pos_val - cap_val
        if excess < min_n:
            continue
        sell_val = min(pos_val, excess)
        if executor.execute_order(
            sym, "sell", notional=round(sell_val, 2), reason="per_name_cap_trim"
        ):
            trims += 1
    return trims


def trim_sleeve_overexposure(executor, prices) -> int:
    """Sell long holdings when a sleeve exceeds its hard cap."""
    if not paper_risk_controls_active():
        return 0
    portfolio = executor.portfolio
    eq = portfolio.equity(prices)
    if eq <= 0:
        return 0
    trims = 0
    for sleeve_key in ("spy", "nyse", "crypto"):
        regime = getattr(executor, "_current_regime", None)
        base_pct = {
            "spy": config.SPY_SLEEVE_CAP_PCT,
            "nyse": config.NYSE_SLEEVE_CAP_PCT,
            "crypto": config.CRYPTO_SLEEVE_CAP_PCT,
        }.get(sleeve_key, 1.0)
        cap_pct = effective_sleeve_cap_pct(sleeve_key, base_pct, regime=regime)
        pred = _sleeve_predicate(executor, sleeve_key)
        sleeve_val = sum(
            float(portfolio.positions.get(sym, 0)) * float(prices.get(sym, 0))
            for sym in portfolio.positions
            if pred(sym) and float(portfolio.positions.get(sym, 0)) > 0
        )
        cap_val = eq * cap_pct
        excess = sleeve_val - cap_val
        if excess < config.effective_min_notional(eq):
            continue
        for sym in sorted(portfolio.positions.keys()):
            if excess <= 0:
                break
            qty = float(portfolio.positions.get(sym, 0))
            if qty <= 0 or not pred(sym):
                continue
            price = prices.get(sym)
            if price is None or float(price) <= 0:
                continue
            pos_val = qty * float(price)
            sell_val = min(pos_val, excess)
            if executor.execute_order(sym, "sell", notional=round(sell_val, 2), reason="sleeve_cap_trim"):
                trims += 1
                excess -= sell_val
    return trims


def update_position_meta_on_fill(
    portfolio,
    symbol: str,
    side: str,
    price: float,
    *,
    bar_index: int | None,
    order: dict | None = None,
) -> None:
    if not paper_risk_controls_active():
        return
    sym = config.normalize_symbol(symbol)
    if sym == config.VTI_CORE_SYMBOL:
        return
    meta = getattr(portfolio, "position_meta", None)
    if meta is None:
        meta = {}
        portfolio.position_meta = meta
    qty = float(portfolio.positions.get(sym, 0))
    px = float(price)
    if side == "buy" and qty > 0:
        add_qty = float((order or {}).get("qty") or 0)
        row = meta.get(sym, {})
        if row.get("entry_price") and add_qty > 0 and qty > add_qty:
            old_qty = qty - add_qty
            avg = (old_qty * float(row["entry_price"]) + add_qty * px) / qty
            entry_bar = row.get("entry_bar", bar_index)
        else:
            avg = px
            entry_bar = bar_index
        meta[sym] = {
            "entry_price": avg,
            "entry_bar": int(entry_bar if entry_bar is not None else 0),
            "peak_price": max(float(row.get("peak_price", px)), px),
            "qty": qty,
        }
    elif side == "sell" and qty < 1e-9:
        meta.pop(sym, None)
    elif qty > 0:
        row = meta.get(sym, {})
        if row:
            row["qty"] = qty
            row["peak_price"] = max(float(row.get("peak_price", px)), px)


def _trailing_stop_hit(entry: float, peak: float, current: float) -> bool:
    if entry <= 0 or peak <= 0 or current <= 0:
        return False
    gain = (peak - entry) / entry
    if gain < config.PAPER_TRAILING_STOP_ARM_PCT:
        return False
    trail = config.PAPER_TRAILING_STOP_TRAIL_PCT
    return current <= peak * (1.0 - trail)


def _is_tactical_long(sym: str) -> bool:
    """Max-hold / trailing apply to NYSE picks, not core SPY/VTI trend holds."""
    sym = config.normalize_symbol(sym)
    if sym in (config.VTI_CORE_SYMBOL, config.SPY_BOT_SYMBOL):
        return False
    if config.is_crypto(sym):
        return False
    return True


def run_paper_position_exits(
    portfolio,
    prices,
    bar_index: int,
    executor,
) -> int:
    """Stop-loss, trailing stop, and max-hold exits for paper long positions."""
    if not paper_risk_controls_active():
        return 0
    meta = getattr(portfolio, "position_meta", {}) or {}
    exits = 0
    smart_on = config.effective_smart_stops_enabled()
    sizing_data = getattr(executor, "_sizing_data", None)
    for sym in list(portfolio.positions.keys()):
        qty = float(portfolio.positions.get(sym, 0))
        if qty <= 0:
            continue
        if sym == config.VTI_CORE_SYMBOL:
            continue
        tactical = _is_tactical_long(sym)
        current = prices.get(sym)
        if current is None or float(current) <= 0:
            continue
        current = float(current)
        row = meta.get(sym, {})
        entry = float(row.get("entry_price") or current)
        peak = max(float(row.get("peak_price") or entry), current)
        row["peak_price"] = peak
        meta[sym] = row
        pnl_pct = (current - entry) / entry if entry > 0 else 0.0
        entry_bar = row.get("entry_bar")
        held = int(bar_index) - int(entry_bar) if entry_bar is not None else 0
        reason = None

        if smart_on and tactical:
            atr = None
            try:
                from modules.risk_management import calculate_atr

                atr = calculate_atr(sizing_data, sym) if sizing_data is not None else None
            except Exception:
                atr = None
            if atr is None or atr <= 0:
                atr = entry * 0.02
            from modules.smart_atr_stops import evaluate_smart_stop

            decision = evaluate_smart_stop(
                symbol=sym,
                entry=entry,
                current=current,
                atr=float(atr),
                meta=row,
                qty=qty,
                data=sizing_data,
                bar_index=bar_index,
                side="long",
            )
            meta[sym] = decision.get("meta") or row
            action = decision.get("action")
            if action == "reduce":
                reduce_frac = float(decision.get("reduce_frac") or 0.5)
                sell_n = round(qty * current * reduce_frac, 2)
                if sell_n > 0 and executor.execute_order(
                    sym, "sell", notional=sell_n, reason=decision.get("exit_code") or "smart_size_reduce"
                ):
                    exits += 1
                    # Refresh qty after partial; keep meta
                    new_qty = float(portfolio.positions.get(sym, 0))
                    if new_qty < 1e-9:
                        meta.pop(sym, None)
                    else:
                        meta[sym]["qty"] = new_qty
                    continue
            elif action == "exit":
                reason = decision.get("exit_code") or "smart_atr_stop"
        elif pnl_pct <= -config.STOP_LOSS_PCT:
            reason = "stop_loss"

        if reason is None and tactical and _trailing_stop_hit(entry, peak, current):
            reason = "trailing_stop"
        elif reason is None and tactical and held >= config.PAPER_POSITION_MAX_HOLD_BARS:
            reason = "max_hold"
        if not reason:
            continue
        if executor.execute_order(sym, "sell", notional=round(qty * current, 2), reason=reason):
            exits += 1
            meta.pop(sym, None)
    portfolio.position_meta = meta
    return exits
