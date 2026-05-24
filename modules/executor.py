from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import TimeInForce

class AlpacaExecutor:
    def __init__(self, api_key, api_secret, paper=True):
        from alpaca.trading.client import TradingClient
        self.client = TradingClient(api_key, api_secret, paper=paper)

    def execute_order(self, symbol, side, notional, time_in_force=TimeInForce.DAY):
        # Using notional is safer for Alpaca margin management
        order_data = MarketOrderRequest(
            symbol=symbol,
            notional=float(notional),
            side=side,
            time_in_force=time_in_force
        )
        return self.client.submit_order(order_data)