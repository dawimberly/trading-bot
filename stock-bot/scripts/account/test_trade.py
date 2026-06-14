"""Place a 1-share VTI test market order on Alpaca paper.

Run: python scripts/account/test_trade.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from modules.alpaca_executor import get_trading_client

client = get_trading_client()
order_data = MarketOrderRequest(
    symbol="VTI",
    qty=1,
    side=OrderSide.BUY,
    time_in_force=TimeInForce.DAY,
)
print("Submitting test order for 1 share of VTI...")
try:
    order = client.submit_order(order_data)
    print(f"Order successful! Order ID: {order.id}")
except Exception as e:
    print(f"Order failed: {e}")
