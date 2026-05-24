"""Mock order executor for backtesting without live API calls."""


class MockExecutor:
    """Records simulated orders in memory for backtest scripts."""

    def __init__(self):
        self.orders = []

    def execute_order(self, symbol, side, qty=1, notional=None):
        order = {"symbol": symbol, "side": side, "qty": qty, "notional": notional}
        self.orders.append(order)
        return order
