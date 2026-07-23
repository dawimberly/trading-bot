"""Drawdown monitoring and position-size helpers."""



import datetime
import logging
from pathlib import Path

import config

logger = logging.getLogger(__name__)





class RiskManager:

    def __init__(

        self,

        max_drawdown_pct=None,

        resume_drawdown_pct=None,

        log_file=None,

        *,

        halt_min_bars: int | None = None,

    ):

        self.max_drawdown = max_drawdown_pct or config.MAX_DRAWDOWN_PCT

        self.resume_drawdown = (

            resume_drawdown_pct

            if resume_drawdown_pct is not None

            else config.effective_halt_resume_drawdown_pct()

        )

        self.peak_equity = None

        self.log_file = log_file or config.RISK_EVENTS_LOG

        self.halted = False

        self.recovery_mode = False

        self._halted_at: datetime.datetime | None = None

        self._halted_at_bar: int | None = None

        self._current_bar: int = 0

        self.halt_min_bars = (

            int(halt_min_bars)

            if halt_min_bars is not None

            else int(getattr(config, "HALT_MIN_BARS", 0) or 0)

        )

        self._breach_liquidated = False

        self.halt_events = 0

        self.resume_events = 0

        self._equity_history: list[float] = []



    def record_equity(self, current_equity: float, *, max_len: int = 60) -> None:
        """Append equity for portfolio vol ceiling (tail-risk overlay)."""
        eq = float(current_equity)
        if eq <= 0:
            return
        self._equity_history.append(eq)
        if len(self._equity_history) > max_len:
            self._equity_history = self._equity_history[-max_len:]

    def recent_equity_history(self) -> list[float]:
        return list(self._equity_history)

    def set_current_bar(self, bar_index: int) -> None:
        self._current_bar = int(bar_index)

    def current_drawdown(self, current_equity):

        if self.peak_equity is None or self.peak_equity <= 0:

            return 0.0

        return (self.peak_equity - current_equity) / self.peak_equity



    def update_peak(self, current_equity):

        if self.peak_equity is None:

            self.peak_equity = current_equity

        elif current_equity > self.peak_equity:

            self.peak_equity = current_equity



    def _halt_cooldown_elapsed(self) -> bool:

        if self.halt_min_bars > 0 and self._halted_at_bar is not None:

            return (self._current_bar - self._halted_at_bar) >= self.halt_min_bars

        min_halt = config.HALT_MIN_SECONDS

        if self._halted_at is None or min_halt <= 0:

            return True

        elapsed = (datetime.datetime.now() - self._halted_at).total_seconds()

        return elapsed >= min_halt



    def _clear_recovery_if_healed(self, drawdown: float) -> None:

        if (

            self.recovery_mode

            and drawdown < config.PAPER_HALT_RECOVERY_CLEAR_PCT

            and not self.halted

        ):

            self.recovery_mode = False



    def check_drawdown(self, current_equity):

        self.update_peak(current_equity)

        drawdown = self.current_drawdown(current_equity)

        self._clear_recovery_if_healed(drawdown)



        if not self.halted:

            if drawdown >= self.max_drawdown:

                self.halted = True

                self._halted_at = datetime.datetime.now()

                self._halted_at_bar = self._current_bar

                self.halt_events += 1

                self._log_event(

                    f"CRITICAL: Drawdown {drawdown:.2%} reached. System Halted."

                )

                return False

            return True



        if drawdown < self.resume_drawdown and self._halt_cooldown_elapsed():

            self.halted = False

            self._halted_at = None

            self._halted_at_bar = None

            self._breach_liquidated = False

            self.recovery_mode = True

            self.resume_events += 1

            self._log_event(

                f"RESUME: Drawdown {drawdown:.2%} below "

                f"{self.resume_drawdown:.0%}. Trading resumed (recovery sizing)."

            )

            return True

        return False



    def should_liquidate_on_breach(self):

        """One-shot per halt episode when HALT_LIQUIDATE_ON_BREACH is enabled."""

        if not config.HALT_LIQUIDATE_ON_BREACH or self._breach_liquidated:

            return False

        self._breach_liquidated = True

        return True



    def get_position_size(self, current_equity, risk_per_trade=0.02):

        return round(current_equity * risk_per_trade, 2)



    def can_trade(self, symbol, portfolio):

        """

        Gatekeeper logic: Prevents over-exposure.

        For now, we limit to 5 concurrent positions.

        """

        if len(portfolio.positions) >= 5:

            return False

        return True



    def _log_event(self, message):

        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        log_path = Path(self.log_file)

        log_path.parent.mkdir(parents=True, exist_ok=True)

        with open(log_path, "a", encoding="utf-8") as f:

            f.write(f"{ts} | {message}\n")





def _is_long_sleeve_symbol(symbol):

    sym = config.normalize_symbol(symbol)

    if config.is_metal_symbol(sym):

        return False

    return True





def trim_long_sleeves_to_cash_target(

    portfolio,

    prices,

    target_pct,

    tx_cost=0.001,

    *,

    protect_symbols: frozenset[str] | None = None,

):

    """Sell long-sleeve holdings until cash >= target_pct of equity."""

    eq = portfolio.equity(prices)

    if eq <= 0:

        return 0

    target_cash = eq * target_pct

    if portfolio.cash >= target_cash:

        return 0

    need = target_cash - portfolio.cash

    sells = 0

    protected = protect_symbols or frozenset()

    for symbol in list(portfolio.positions.keys()):

        if need <= 0:

            break

        sym = config.normalize_symbol(symbol)

        if sym in protected:

            continue

        if not _is_long_sleeve_symbol(symbol):

            continue

        qty = portfolio.positions.get(symbol, 0)

        price = prices.get(symbol)

        if qty <= 0 or price is None or price <= 0:

            continue

        pos_val = qty * price

        sell_val = min(pos_val, need / (1 - tx_cost))

        sell_qty = sell_val / price

        proceeds = sell_val * (1 - tx_cost)

        portfolio.cash += proceeds

        portfolio.positions[symbol] = qty - sell_qty

        if portfolio.positions[symbol] < 1e-9:

            del portfolio.positions[symbol]

        need -= proceeds

        sells += 1

    return sells


def portfolio_realized_vol_annualized(
    equity_values,
    *,
    window: int = 20,
) -> float | None:
    """Annualized realized vol from recent equity curve (20d default)."""
    import math

    if not equity_values:
        return None
    vals = [float(x) for x in equity_values if x is not None and float(x) > 0]
    if len(vals) < max(6, window + 1):
        return None
    import pandas as pd

    rets = pd.Series(vals).pct_change().dropna().tail(window)
    if len(rets) < 5:
        return None
    daily_vol = float(rets.std())
    if not math.isfinite(daily_vol) or daily_vol <= 0:
        return None
    return daily_vol * (252**0.5)


def portfolio_vol_risk_multiplier(
    equity_values,
    *,
    ceiling: float = 0.18,
    window: int = 20,
    min_mult: float = 0.35,
) -> float:
    """Scale sizing down when portfolio vol exceeds ceiling (e.g. 18% ann.)."""
    ann = portfolio_realized_vol_annualized(equity_values, window=window)
    if ann is None or ann <= ceiling:
        return 1.0
    return round(max(min_mult, ceiling / ann), 4)


def calculate_atr(data, symbol: str, period: int | None = None) -> float | None:
    """Average true range proxy from daily close series (|Δclose| rolling mean)."""
    import numpy as np

    sym = config.normalize_symbol(symbol)
    if data is None or not hasattr(data, "columns") or sym not in data.columns:
        return None
    period = max(2, int(period or getattr(config, "ATR_PERIOD", 14)))
    prices = data[sym].dropna()
    if len(prices) < period + 1:
        return None
    tr = prices.diff().abs()
    atr = tr.rolling(window=period).mean().iloc[-1]
    if not np.isfinite(atr) or float(atr) <= 0:
        return None
    return round(float(atr), 4)


def _symbol_price(data, symbol: str) -> float | None:
    sym = config.normalize_symbol(symbol)
    if data is not None and hasattr(data, "columns") and sym in data.columns:
        prices = data[sym].dropna()
        if not prices.empty:
            px = float(prices.iloc[-1])
            if px > 0:
                return px
    return None


def get_atr_risk_size(
    equity: float,
    symbol: str,
    atr_or_data,
    *,
    risk_pct: float | None = None,
    atr_multiple: float | None = None,
    price: float | None = None,
) -> dict[str, float | str]:
    """ATR-based notional: risk $ = equity × risk_pct; size = risk$ / (ATR × multiple) × price."""
    equity = float(equity)
    sym = config.normalize_symbol(symbol)
    risk_pct = float(
        risk_pct if risk_pct is not None else config.effective_risk_per_trade(equity)
    )
    atr_multiple = float(
        atr_multiple if atr_multiple is not None else getattr(config, "ATR_RISK_MULTIPLE", 2.0)
    )
    max_pct = float(getattr(config, "ATR_MAX_SIZE_PCT", 0.04))
    fixed = round(equity * risk_pct, 2)
    base = {
        "symbol": sym,
        "equity": equity,
        "risk_pct": risk_pct,
        "atr_multiple": atr_multiple,
        "max_size_pct": max_pct,
    }

    data = atr_or_data if hasattr(atr_or_data, "columns") else None
    atr = None
    if data is not None:
        atr = calculate_atr(data, sym)
    elif atr_or_data is not None:
        try:
            atr = float(atr_or_data)
        except (TypeError, ValueError):
            atr = None

    px = price or (_symbol_price(data, sym) if data is not None else None)
    if atr is None or px is None or px <= 0 or atr_multiple <= 0:
        capped = min(fixed, round(equity * max_pct, 2))
        max_order = config.effective_max_notional_per_order(equity)
        return {
            **base,
            "notional": min(capped, max_order),
            "atr": 0.0,
            "price": float(px or 0.0),
            "stop_distance": 0.0,
            "method": "fixed_pct",
        }

    stop_distance = atr * atr_multiple
    risk_dollars = equity * risk_pct
    notional = round(risk_dollars * px / stop_distance, 2)
    notional = min(notional, round(equity * max_pct, 2))
    notional = min(notional, config.effective_max_notional_per_order(equity))
    min_n = config.effective_min_notional(equity)
    if notional < min_n:
        notional = min(fixed, round(equity * max_pct, 2))
        return {
            **base,
            "notional": notional,
            "atr": atr,
            "price": px,
            "stop_distance": round(stop_distance, 4),
            "method": "fixed_pct",
        }
    return {
        **base,
        "notional": notional,
        "atr": atr,
        "price": px,
        "stop_distance": round(stop_distance, 4),
        "method": "atr",
    }


def atr_adjust_notional(
    notional: float | None,
    equity: float,
    symbol: str,
    data,
    *,
    sleeve_key: str | None = None,
) -> float | None:
    """Tighten *notional* with ATR sizing when enabled; fallback to input."""
    del sleeve_key
    if notional is None or not config.effective_atr_sizing_enabled():
        return notional
    sym = config.normalize_symbol(symbol)
    if not sym or config.is_crypto(sym):
        return notional
    sizing = get_atr_risk_size(equity, sym, data)
    if sizing.get("method") != "atr":
        return notional
    atr_n = float(sizing["notional"])
    return round(min(float(notional), atr_n), 2)


def atr_stop_price(data, symbol: str, *, side: str = "long") -> float | None:
    """Implied stop level at ATR_STOP_MULTIPLIER (fallback ATR_RISK_MULTIPLE) from last price."""
    sym = config.normalize_symbol(symbol)
    atr = calculate_atr(data, sym)
    px = _symbol_price(data, sym)
    if atr is None or px is None:
        return None
    mult = float(
        getattr(
            config,
            "ATR_STOP_MULTIPLIER",
            getattr(config, "ATR_RISK_MULTIPLE", 2.0),
        )
    )
    dist = atr * mult
    if str(side).lower() == "short":
        return round(px + dist, 2)
    return round(max(0.01, px - dist), 2)


def format_atr_sizing_banner() -> str | None:
    if not config.effective_atr_sizing_enabled():
        return ">>> ATR Sizing: OFF"
    mult = float(getattr(config, "ATR_RISK_MULTIPLE", 2.0))
    return f">>> ATR Sizing: ON ({mult:.1f}x)"


def format_weekly_atr_sizing_note() -> str:
    if not config.effective_atr_sizing_enabled():
        return ""
    return (
        f"ATR sizing: ON ({config.ATR_RISK_MULTIPLE:.1f}x stop, "
        f"max {config.ATR_MAX_SIZE_PCT:.0%}/trade, {config.ATR_PERIOD}d)"
    )


# --- Conviction-based position sizing (paper) ---------------------------------

# v1.5.1 tune: raise RVOL/ORB/Catalyst/Insider weights (0.75 -> 0.84 of long score),
# trimming regime/mtf so high-signal scanner names size up more decisively.
_CONVICTION_WEIGHTS_LONG = {
    "rvol": 0.24,
    "orb": 0.20,
    "catalyst": 0.24,
    "insider": 0.16,
    "regime": 0.08,
    "mtf": 0.08,
}
# Lift scanner/insider ("signals") weight in stat-arb conviction 0.30 -> 0.38.
_CONVICTION_WEIGHTS_STAT_ARB = {
    "z_score": 0.38,
    "signals": 0.38,
    "regime": 0.24,
}
_CONVICTION_WEIGHTS_SHORT = {
    "bubble": 0.35,
    "insider_short": 0.30,
    "regime": 0.35,
}


def conviction_scale(
    conviction_score: float,
    *,
    scale_band: tuple[float, float] | None = None,
) -> float:
    """Map conviction 0–1 to position scale between min and max.

    Pass ``scale_band`` to override the global (min, max) — e.g. stat-arb pairs
    use a tighter 0.6x–1.4x band than the global 0.4x–2.0x.
    """
    score = max(0.0, min(1.0, float(conviction_score)))
    if scale_band is not None:
        lo, hi = float(scale_band[0]), float(scale_band[1])
    else:
        lo = float(getattr(config, "CONVICTION_MIN_SCALE", 0.4))
        hi = float(getattr(config, "CONVICTION_MAX_SCALE", 1.8))
    return round(lo + score * (hi - lo), 4)


def _weighted_average(components: dict[str, float], weights: dict[str, float]) -> float:
    total_w = 0.0
    total = 0.0
    for key, weight in weights.items():
        if key not in components:
            continue
        total_w += weight
        total += weight * max(0.0, min(1.0, float(components[key])))
    if total_w <= 0:
        return 0.5
    return round(total / total_w, 4)


def _conviction_rvol_component(symbol: str, data) -> float | None:
    if not config.effective_rvol_scanner_enabled():
        return None
    try:
        from modules.volume_analysis import calculate_rvol

        rvol = calculate_rvol(data, symbol)
        if rvol is None:
            return None
        floor = float(config.RVOL_MIN_THRESHOLD)
        boost = float(config.RVOL_MOMENTUM_BOOST_THRESHOLD)
        strong = float(config.RVOL_STRONG_THRESHOLD)
        if rvol < floor:
            return 0.15
        if rvol < boost:
            span = max(0.01, boost - floor)
            return round(0.35 + 0.25 * (rvol - floor) / span, 4)
        if rvol >= strong:
            return 1.0
        span = max(0.01, strong - boost)
        return round(0.65 + 0.35 * (rvol - boost) / span, 4)
    except Exception:
        return None


def _conviction_orb_component(symbol: str, data) -> float | None:
    if not config.effective_orb_enabled():
        return None
    try:
        from modules.orb_strategy import calculate_opening_range
        from modules.volume_analysis import calculate_rvol

        or_info = calculate_opening_range(
            data, symbol, minutes=int(getattr(config, "ORB_BREAKOUT_MINUTES", 30))
        )
        if not or_info:
            return 0.2
        if or_info.get("breakout_up"):
            rvol = calculate_rvol(data, symbol)
            if rvol is not None and rvol >= float(config.ORB_RVOL_MIN):
                return 1.0
            return 0.65
        if or_info.get("breakout_down"):
            return 0.25
        return 0.35
    except Exception:
        return None


def _conviction_catalyst_component(symbol: str, data) -> float | None:
    if not config.effective_catalyst_scoring_enabled():
        return None
    try:
        from modules.catalyst_scoring import score_catalysts

        row = score_catalysts(data, symbol)
        return round(max(0.0, min(1.0, int(row.get("score") or 0) / 100.0)), 4)
    except Exception:
        return None


def _conviction_insider_component(symbol: str) -> float | None:
    if not config.effective_insider_signal_boost_enabled():
        return None
    try:
        from modules.insider_signal_handler import momentum_rank_boost

        boost = float(momentum_rank_boost(symbol))
        if boost <= 0:
            return 0.2
        cap = float(getattr(config, "INSIDER_CLUSTER_BOOST_MAX", 0.18))
        return round(min(1.0, 0.45 + boost / max(0.01, cap) * 0.55), 4)
    except Exception:
        return None


def _conviction_mtf_component(symbol: str, data) -> float | None:
    if not config.effective_multi_timeframe_enabled():
        return None
    try:
        from modules.multi_timeframe import check_multi_timeframe_alignment

        return check_multi_timeframe_alignment(symbol, data)
    except Exception:
        return None


def _conviction_regime_component(regime: str | None) -> float | None:
    if not regime:
        return None
    try:
        from modules.regime_sizing import effective_regime_sizing_multiplier

        mult = float(effective_regime_sizing_multiplier(regime))
        lo, hi = 0.30, 1.60
        base = round(max(0.0, min(1.0, (mult - lo) / (hi - lo))), 4)
    except Exception:
        base = 0.5
    # Blend with HMM next-regime conviction when available (soft signal).
    try:
        from modules.markov_regime import hmm_conviction_component

        hmm_c = hmm_conviction_component()
        if hmm_c is not None:
            base = round(0.65 * base + 0.35 * float(hmm_c), 4)
    except Exception:
        pass
    # Soft-trim conviction when GARCH forecast vol is elevated (does not re-apply
    # the full risk_per_trade multiplier — that path already sizes dollars).
    try:
        if config.effective_garch_vol_enabled():
            from modules.garch_vol import garch_vol_size_multiplier

            g = float(garch_vol_size_multiplier())
            blend = float(getattr(config, "GARCH_VOL_CONVICTION_BLEND", 0.35) or 0.35)
            blend = max(0.0, min(1.0, blend))
            if blend > 0 and g < 0.999:
                # Map size mult [min,1] → conviction tilt toward lower score.
                tilted = base * (1.0 + blend * (g - 1.0))
                base = round(max(0.0, min(1.0, tilted)), 4)
    except Exception:
        pass
    # Optional ARIMA / hybrid: soft-lift conviction when size mult ≠ 1
    # (hybrid already vol-scales mean; GARCH conviction trim above stays separate).
    try:
        if config.effective_arima_enabled():
            from modules.arima_forecast import arima_size_multiplier

            a = float(arima_size_multiplier())
            blend = float(getattr(config, "ARIMA_CONVICTION_BLEND", 0.25) or 0.25)
            blend = max(0.0, min(1.0, blend))
            if blend > 0 and abs(a - 1.0) > 1e-6:
                tilted = base * (1.0 + blend * (a - 1.0))
                base = round(max(0.0, min(1.0, tilted)), 4)
    except Exception:
        pass
    return base


def _conviction_z_component(z_score: float | None) -> float | None:
    if z_score is None:
        return None
    z = abs(float(z_score))
    entry = float(config.effective_stat_arb_z_entry())
    if z < entry:
        return round(max(0.2, z / max(0.01, entry) * 0.5), 4)
    span = max(0.5, entry * 1.5)
    return round(min(1.0, 0.55 + (z - entry) / span * 0.45), 4)


def _conviction_short_insider_component(symbol: str, bubble_score: float) -> float | None:
    if not config.effective_insider_signal_boost_enabled():
        return None
    try:
        from modules.insider_signal_handler import short_candidate_boost

        boost = float(short_candidate_boost(symbol, bubble_score))
        if boost <= 0:
            return 0.15
        cap = float(getattr(config, "INSIDER_SELL_SHORT_BOOST_MAX", 0.30))
        return round(min(1.0, 0.4 + boost / max(0.01, cap) * 0.6), 4)
    except Exception:
        return None


def compute_conviction_score(
    symbol: str | None = None,
    data=None,
    regime: str | None = None,
    *,
    sleeve: str | None = None,
    z_score: float | None = None,
    bubble_score: float | None = None,
) -> float:
    """Weighted average of available signal strengths (0.0–1.0)."""
    sleeve_key = str(sleeve or "nyse").lower()
    sym = config.normalize_symbol(symbol) if symbol else ""

    if sleeve_key in ("short", "protective_short", "sector_short"):
        components: dict[str, float] = {}
        if bubble_score is not None:
            components["bubble"] = max(0.0, min(1.0, float(bubble_score)))
        if sym:
            ins = _conviction_short_insider_component(sym, float(bubble_score or 0))
            if ins is not None:
                components["insider_short"] = ins
        reg = _conviction_regime_component(regime)
        if reg is not None:
            components["regime"] = reg
        return _weighted_average(components, _CONVICTION_WEIGHTS_SHORT)

    if sleeve_key in ("stat_arb", "stat_arb_equity", "stat_arb_crypto"):
        components = {}
        zc = _conviction_z_component(z_score)
        if zc is not None:
            components["z_score"] = zc
        signal_parts: list[float] = []
        if sym:
            for fn in (_conviction_rvol_component, _conviction_catalyst_component):
                val = fn(sym, data)
                if val is not None:
                    signal_parts.append(val)
            ins = _conviction_insider_component(sym)
            if ins is not None:
                signal_parts.append(ins)
            mtf = _conviction_mtf_component(sym, data)
            if mtf is not None:
                signal_parts.append(mtf)
            try:
                from modules.insider_signal_handler import stat_arb_long_boost

                boost = float(stat_arb_long_boost(sym))
                if boost > 1.0:
                    signal_parts.append(min(1.0, 0.5 + (boost - 1.0) * 2))
            except Exception as exc:
                logger.debug("conviction insider stat-arb component skipped for %s: %s", sym, exc)
        if signal_parts:
            components["signals"] = sum(signal_parts) / len(signal_parts)
        reg = _conviction_regime_component(regime)
        if reg is not None:
            components["regime"] = reg
        return _weighted_average(components, _CONVICTION_WEIGHTS_STAT_ARB)

    components = {}
    if sym:
        rvol = _conviction_rvol_component(sym, data)
        if rvol is not None:
            components["rvol"] = rvol
        orb = _conviction_orb_component(sym, data)
        if orb is not None:
            components["orb"] = orb
        cat = _conviction_catalyst_component(sym, data)
        if cat is not None:
            components["catalyst"] = cat
        ins = _conviction_insider_component(sym)
        if ins is not None:
            components["insider"] = ins
        mtf = _conviction_mtf_component(sym, data)
        if mtf is not None:
            components["mtf"] = mtf
    reg = _conviction_regime_component(regime)
    if reg is not None:
        components["regime"] = reg
    return _weighted_average(components, _CONVICTION_WEIGHTS_LONG)


def get_conviction_based_notional(
    equity: float,
    base_risk_pct: float,
    conviction_score: float,
    symbol: str | None = None,
    *,
    data=None,
    regime: str | None = None,
    strategy_id: str | None = None,
    sleeve: str | None = None,
) -> float:
    """Scale equity × risk_pct by conviction; combine with ATR cap when enabled."""
    equity = float(equity)
    base_risk_pct = float(base_risk_pct)
    base_notional = round(equity * base_risk_pct, 2)
    if not config.effective_conviction_sizing_enabled():
        if symbol:
            try:
                from modules.dynamic_vti_allocator import spy_like_size_boost

                base_notional = round(
                    base_notional * float(spy_like_size_boost(symbol, data)), 2
                )
            except Exception:
                pass
        try:
            from modules.strategy_rating import apply_strategy_rating_to_notional

            rated = apply_strategy_rating_to_notional(
                base_notional, strategy_id=strategy_id, sleeve=sleeve
            )
            if rated is not None:
                return rated
        except Exception:
            pass
        return base_notional

    scale = conviction_scale(conviction_score)
    notional = round(base_notional * scale, 2)
    if symbol:
        try:
            from modules.dynamic_vti_allocator import spy_like_size_boost

            notional = round(notional * float(spy_like_size_boost(symbol, data)), 2)
        except Exception:
            pass
    max_pct = float(getattr(config, "ATR_MAX_SIZE_PCT", 0.04))
    notional = min(notional, round(equity * max_pct, 2))
    notional = min(notional, config.effective_max_notional_per_order(equity))
    min_n = config.effective_min_notional(equity)
    if notional < min_n:
        notional = min(base_notional, round(equity * max_pct, 2))
    if symbol and data is not None:
        adjusted = atr_adjust_notional(notional, equity, symbol, data)
        if adjusted is not None:
            notional = adjusted
    try:
        from modules.strategy_rating import apply_strategy_rating_to_notional

        rated = apply_strategy_rating_to_notional(
            notional, strategy_id=strategy_id, sleeve=sleeve
        )
        if rated is not None:
            notional = rated
    except Exception:
        pass
    record_conviction_sample(float(conviction_score))
    return round(notional, 2)


def scale_notional_by_conviction(
    notional: float | None,
    equity: float,
    conviction_score: float,
    *,
    symbol: str | None = None,
    data=None,
    scale_band: tuple[float, float] | None = None,
    strategy_id: str | None = None,
    sleeve: str | None = None,
) -> float | None:
    """Apply conviction scale to an existing sleeve notional (NYSE / stat arb / shorts).

    ``scale_band`` overrides the global conviction scale range (used by stat-arb
    pairs for a tighter 0.6x–1.4x band).
    """
    if notional is None:
        return notional
    if not config.effective_conviction_sizing_enabled():
        scaled = float(notional)
        if symbol:
            try:
                from modules.dynamic_vti_allocator import spy_like_size_boost

                scaled = round(scaled * float(spy_like_size_boost(symbol, data)), 2)
            except Exception:
                pass
        try:
            from modules.strategy_rating import apply_strategy_rating_to_notional

            rated = apply_strategy_rating_to_notional(
                scaled, strategy_id=strategy_id, sleeve=sleeve
            )
            if rated is not None:
                return rated
        except Exception:
            pass
        return round(scaled, 2)
    equity = float(equity)
    scale = conviction_scale(conviction_score, scale_band=scale_band)
    scaled = round(float(notional) * scale, 2)
    if symbol:
        try:
            from modules.dynamic_vti_allocator import spy_like_size_boost

            scaled = round(scaled * float(spy_like_size_boost(symbol, data)), 2)
        except Exception:
            pass
    min_n = config.effective_min_notional(equity)
    max_pct = float(getattr(config, "ATR_MAX_SIZE_PCT", 0.04))
    scaled = min(scaled, round(equity * max_pct, 2))
    scaled = min(scaled, config.effective_max_notional_per_order(equity))
    if scaled < min_n:
        return None
    if symbol and data is not None:
        adjusted = atr_adjust_notional(scaled, equity, symbol, data)
        if adjusted is not None:
            scaled = adjusted
    try:
        from modules.strategy_rating import apply_strategy_rating_to_notional

        rated = apply_strategy_rating_to_notional(
            scaled, strategy_id=strategy_id, sleeve=sleeve
        )
        if rated is not None:
            scaled = rated
    except Exception:
        pass
    record_conviction_sample(float(conviction_score))
    return round(scaled, 2)


def _conviction_metrics_path() -> "Path":
    from pathlib import Path

    return Path(getattr(config, "CONVICTION_METRICS_FILE", "data/conviction_metrics.json"))


def record_conviction_sample(score: float) -> None:
    """Append conviction sample for rolling dashboard / weekly averages."""
    if not config.effective_conviction_sizing_enabled():
        return
    try:
        from datetime import datetime

        from modules.safe_io import update_json_atomic

        path = _conviction_metrics_path()
        ts = datetime.now().isoformat(timespec="seconds")
        sample = {"ts": ts, "score": round(float(score), 4)}

        def _mutate(payload: dict) -> dict:
            samples = list(payload.get("samples") or [])
            samples.append(sample)
            payload["samples"] = samples[-500:]
            payload["last_score"] = sample["score"]
            payload["updated_at"] = ts
            return payload

        update_json_atomic(path, _mutate, default={"samples": []})
    except Exception as exc:
        logger.warning("failed to persist conviction metrics: %s", exc)


def get_average_conviction(*, days: int = 7) -> float | None:
    """Rolling mean conviction over the last *days* (from recorded samples)."""
    try:
        from datetime import datetime, timedelta

        from modules.safe_io import read_json_file

        payload = read_json_file(_conviction_metrics_path()) or {}
        samples = list(payload.get("samples") or [])
        if not samples:
            last = payload.get("last_score")
            return float(last) if last is not None else None
        cutoff = datetime.now() - timedelta(days=max(1, int(days)))
        scores: list[float] = []
        for row in samples:
            try:
                ts = datetime.fromisoformat(str(row.get("ts", "")).replace("Z", "+00:00"))
                if ts.tzinfo is not None:
                    ts = ts.replace(tzinfo=None)
                if ts >= cutoff:
                    scores.append(float(row["score"]))
            except (KeyError, TypeError, ValueError):
                continue
        if not scores:
            return float(payload.get("last_score")) if payload.get("last_score") is not None else None
        return round(sum(scores) / len(scores), 3)
    except Exception:
        return None


def conviction_level_label(avg: float | None) -> str:
    if avg is None:
        return "—"
    if avg >= 0.75:
        return "High"
    if avg >= 0.55:
        return "Moderate"
    if avg >= 0.40:
        return "Low"
    return "Weak"


def format_conviction_sizing_banner() -> str | None:
    if not config.effective_conviction_sizing_enabled():
        return ">>> Conviction Sizing: OFF"
    lo = float(getattr(config, "CONVICTION_MIN_SCALE", 0.4))
    hi = float(getattr(config, "CONVICTION_MAX_SCALE", 2.0))
    avg = get_average_conviction(days=7)
    avg_part = (
        f" | 7d avg {avg:.2f} ({conviction_level_label(avg)})" if isinstance(avg, float) else ""
    )
    return f">>> Conviction Sizing: ON ({lo:.1f}x-{hi:.1f}x by signal strength){avg_part} <<<"


def format_weekly_conviction_note() -> str:
    if not config.effective_conviction_sizing_enabled():
        return ""
    avg = get_average_conviction(days=7)
    if avg is None:
        return "Conviction sizing: ON (no samples yet)"
    return (
        f"Conviction sizing: ON | 7d avg {avg:.2f} ({conviction_level_label(avg)}) | "
        f"scale {config.CONVICTION_MIN_SCALE:.1f}x–{config.CONVICTION_MAX_SCALE:.1f}x"
    )


def format_telegram_weekly_conviction_block() -> str:
    note = format_weekly_conviction_note()
    if not note:
        return ""
    return f"\n\n{note}"


def conviction_dashboard_snapshot() -> dict[str, str | float]:
    avg = get_average_conviction(days=7)
    avg30 = get_average_conviction(days=30)
    last_path = _conviction_metrics_path()
    from modules.safe_io import read_json_file

    payload = read_json_file(last_path) or {}
    last = payload.get("last_score")
    return {
        "avg_7d": avg if avg is not None else "—",
        "avg_30d": avg30 if avg30 is not None else "—",
        "last": last if last is not None else "—",
        "level": conviction_level_label(avg if isinstance(avg, float) else None),
        "enabled": config.effective_conviction_sizing_enabled(),
    }


# --- Portfolio correlation guard (paper) ---------------------------------------


def _normalize_position_symbols(positions) -> list[str]:
    syms: list[str] = []
    if positions is None:
        return syms
    if isinstance(positions, dict):
        iterable = positions.keys()
    else:
        iterable = positions
    for item in iterable:
        if isinstance(item, str):
            sym = config.normalize_symbol(item)
        elif isinstance(item, dict):
            sym = config.normalize_symbol(item.get("symbol") or "")
        else:
            sym = config.normalize_symbol(getattr(item, "symbol", str(item)))
        if sym:
            syms.append(sym)
    return list(dict.fromkeys(syms))


def calculate_portfolio_correlation(
    positions,
    data,
    *,
    lookback: int = 30,
    extra_symbol: str | None = None,
) -> float | None:
    """Average pairwise correlation of daily returns for current holdings."""
    import numpy as np

    syms = _normalize_position_symbols(positions)
    if extra_symbol:
        extra = config.normalize_symbol(extra_symbol)
        if extra and extra not in syms:
            syms.append(extra)
    if data is None or not hasattr(data, "columns"):
        return None
    syms = [s for s in syms if s in data.columns]
    if len(syms) < 2:
        return None
    lookback = max(10, int(lookback))
    rets = data[syms].pct_change().dropna().tail(lookback)
    if len(rets) < 5:
        return None
    corr = rets.corr().values
    n = corr.shape[0]
    upper: list[float] = []
    for i in range(n):
        for j in range(i + 1, n):
            val = corr[i, j]
            if np.isfinite(val):
                upper.append(float(val))
    if not upper:
        return None
    return round(float(np.mean(upper)), 4)


def get_correlation_guard_multiplier(
    current_corr: float | None,
    max_allowed: float = 0.65,
) -> float:
    """Return sizing multiplier 1.0 (ok) down to CORR_GUARD_MIN_SCALE at ceiling."""
    if not config.effective_correlation_guard_enabled():
        return 1.0
    if current_corr is None:
        return 1.0
    corr = float(current_corr)
    max_allowed = float(
        max_allowed if max_allowed is not None else getattr(config, "MAX_PORTFOLIO_CORR", 0.65)
    )
    min_scale = float(getattr(config, "CORR_GUARD_MIN_SCALE", 0.60))
    ceiling = float(getattr(config, "CORR_GUARD_CEILING", 0.85))
    if corr <= max_allowed:
        return 1.0
    if corr >= ceiling:
        return min_scale
    span = max(0.01, ceiling - max_allowed)
    excess = (corr - max_allowed) / span
    return round(1.0 - excess * (1.0 - min_scale), 4)


def _correlation_metrics_path():
    from pathlib import Path

    return Path(getattr(config, "CORRELATION_METRICS_FILE", "data/correlation_guard.json"))


def _save_correlation_snapshot(corr: float | None, mult: float) -> None:
    try:
        from datetime import datetime

        from modules.safe_io import update_json_atomic

        path = _correlation_metrics_path()
        ts = datetime.now().isoformat(timespec="seconds")

        def _mutate(payload: dict) -> dict:
            payload["last_corr"] = corr
            payload["last_multiplier"] = mult
            payload["updated_at"] = ts
            return payload

        update_json_atomic(path, _mutate, default={})
    except Exception as exc:
        logger.warning("failed to persist correlation snapshot: %s", exc)


def portfolio_correlation_from_executor(
    executor,
    data,
    *,
    extra_symbol: str | None = None,
) -> float | None:
    positions = []
    try:
        positions = list(executor._get_positions())
    except Exception as exc:
        logger.debug("could not read positions for correlation calc: %s", exc)
    return calculate_portfolio_correlation(
        positions, data, extra_symbol=extra_symbol
    )


def apply_correlation_guard_notional(
    notional: float | None,
    equity: float,
    executor,
    data,
    *,
    symbol: str | None = None,
) -> float | None:
    """Scale *notional* down when portfolio holdings are too correlated."""
    if notional is None or not config.effective_correlation_guard_enabled():
        return notional
    equity = float(equity)
    corr = portfolio_correlation_from_executor(executor, data, extra_symbol=symbol)
    mult = get_correlation_guard_multiplier(corr)
    _save_correlation_snapshot(corr, mult)
    if mult >= 0.999:
        return round(float(notional), 2)
    scaled = round(float(notional) * mult, 2)
    min_n = config.effective_min_notional(equity)
    if scaled < min_n:
        logger.info(
            "Correlation guard blocked entry %s: corr=%s mult=%.2f below min notional",
            symbol or "?",
            f"{corr:.2f}" if corr is not None else "n/a",
            mult,
        )
        return None
    logger.info(
        "Correlation guard active: portfolio corr %s > %.2f — sizing x%.2f (%s)",
        f"{corr:.2f}" if corr is not None else "n/a",
        float(config.MAX_PORTFOLIO_CORR),
        mult,
        symbol or "entry",
    )
    return scaled


def get_last_portfolio_correlation() -> float | None:
    try:
        from modules.safe_io import read_json_file

        payload = read_json_file(_correlation_metrics_path()) or {}
        val = payload.get("last_corr")
        return float(val) if val is not None else None
    except Exception:
        return None


def format_correlation_guard_banner() -> str | None:
    if not config.effective_correlation_guard_enabled():
        return ">>> Correlation Guard: OFF"
    return f">>> Correlation Guard: ON (max {config.MAX_PORTFOLIO_CORR:.2f}) <<<"


def format_weekly_correlation_note() -> str:
    if not config.effective_correlation_guard_enabled():
        return ""
    corr = get_last_portfolio_correlation()
    if corr is None:
        return (
            f"Correlation guard: ON (max {config.MAX_PORTFOLIO_CORR:.2f}) | "
            "portfolio corr: n/a"
        )
    mult = get_correlation_guard_multiplier(corr)
    status = "reducing size" if mult < 1.0 else "ok"
    return (
        f"Correlation guard: ON | portfolio corr {corr:.2f} ({status}, "
        f"scale x{mult:.2f})"
    )


def format_telegram_weekly_correlation_block() -> str:
    note = format_weekly_correlation_note()
    if not note:
        return ""
    return f"\n\n{note}"


def correlation_dashboard_status() -> str:
    if not config.effective_correlation_guard_enabled():
        return ""
    corr = get_last_portfolio_correlation()
    if corr is None:
        return f"Corr guard max {config.MAX_PORTFOLIO_CORR:.2f} | portfolio n/a"
    mult = get_correlation_guard_multiplier(corr)
    flag = "ACTIVE" if mult < 1.0 else "ok"
    return f"Portfolio corr {corr:.2f} ({flag} x{mult:.2f})"


def run_profit_target_exits(executor, **kwargs):
    """Delegate to modules.profit_target (paper optional trailing stops)."""
    from modules.profit_target import run_profit_target_exits as _run

    return _run(executor, **kwargs)
