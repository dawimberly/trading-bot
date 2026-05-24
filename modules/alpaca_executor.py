import os
from dotenv import load_dotenv, find_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# Load variables from .env
load_dotenv(find_dotenv())

class AlpacaExecutor:
    def __init__(self):
        # Match these names exactly to your .env file
        self.api_key = os.getenv("APCA_API_KEY_ID")
        self.secret_key = os.getenv("APCA_API_SECRET_KEY")
        
        if not self.api_key or not self.secret_key:
            raise ValueError("Alpaca credentials missing in .env file.")
        
        # Initialize client (paper=True is default for paper keys)
        self.client = TradingClient(self.api_key, self.secret_key, paper=True)

    def execute_order(self, symbol, side, qty=1):
        # Map 'buy'/'sell' strings to Alpaca Enums
        order_side = OrderSide.BUY if side.lower() == 'buy' else OrderSide.SELL
        
        order = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=order_side,
            time_in_force=TimeInForce.DAY
        )
        return self.client.submit_order(order_data=order)