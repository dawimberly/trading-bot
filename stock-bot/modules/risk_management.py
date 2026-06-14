"""Drawdown monitoring and position-size helpers."""

import datetime

import config


class RiskManager:
    def __init__(self, max_drawdown_pct=None, resume_drawdown_pct=None, log_file=None):
        self.max_drawdown = max_drawdown_pct or config.MAX_DRAWDOWN_PCT
        self.resume_drawdown = (
            resume_drawdown_pct
            if resume_drawdown_pct is not None
            else config.HALT_RESUME_DRAWDOWN_PCT
        )
        self.peak_equity = None
        self.log_file = log_file or config.RISK_EVENTS_LOG
        self.halted = False
        self._breach_liquidated = False
        self.halt_events = 0
        self.resume_events = 0

    def current_drawdown(self, current_equity):
        if self.peak_equity is None or self.peak_equity <= 0:
            return 0.0
        return (self.peak_equity - current_equity) / self.peak_equity

    def update_peak(self, current_equity):
        if self.peak_equity is None:
            self.peak_equity = current_equity
        elif current_equity > self.peak_equity:
            self.peak_equity = current_equity

    def check_drawdown(self, current_equity):
        self.update_peak(current_equity)
        drawdown = self.current_drawdown(current_equity)

        if not self.halted:
            if drawdown >= self.max_drawdown:
                self.halted = True
                self.halt_events += 1
                self._log_event(
                    f"CRITICAL: Drawdown {drawdown:.2%} reached. System Halted."
                )
                return False
            return True

        if drawdown < self.resume_drawdown:
            self.halted = False
            self._breach_liquidated = False
            self.resume_events += 1
            self._log_event(
                f"RESUME: Drawdown {drawdown:.2%} below "
                f"{self.resume_drawdown:.0%}. Trading resumed."
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
        with open(self.log_file, "a") as f:
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
