import os
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# Setup connection
load_dotenv()
client = TradingClient(os.getenv('APCA_API_KEY_ID'), os.getenv('APCA_API_SECRET_KEY'), paper=True)

# Define the test order
order_data = MarketOrderRequest(
    symbol="VTI",
    qty=1,
    side=OrderSide.BUY,
    time_in_force=TimeInForce.DAY
)

# Submit the order
print("Submitting test order for 1 share of VTI...")
try:
    order = client.submit_order(order_data)
    print(f"Order successful! Order ID: {order.id}")
except Exception as e:
    print(f"Order failed: {e}")