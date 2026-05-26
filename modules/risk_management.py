"""Drawdown monitoring and position-size helpers."""

import datetime

import config


class RiskManager:
    def __init__(self, max_drawdown_pct=None, log_file=None):
        self.max_drawdown = max_drawdown_pct or config.MAX_DRAWDOWN_PCT
        self.peak_equity = None
        self.log_file = log_file or config.RISK_EVENTS_LOG
        self._halt_logged = False

    def check_drawdown(self, current_equity):
        if self.peak_equity is None:
            self.peak_equity = current_equity
        if current_equity > self.peak_equity:
            self.peak_equity = current_equity

        drawdown = (self.peak_equity - current_equity) / self.peak_equity
        if drawdown >= self.max_drawdown:
            if not self._halt_logged:
                self._log_event(
                    f"CRITICAL: Drawdown {drawdown:.2%} reached. System Halted."
                )
                self._halt_logged = True
            return False
        self._halt_logged = False
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
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(self.log_file, "a") as f:
            f.write(f"{ts} | {message}\n")