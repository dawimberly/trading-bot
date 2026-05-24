from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
import os
from dotenv import load_dotenv

load_dotenv()

# Initialize Client
client = TradingClient(os.getenv('APCA_API_KEY_ID'), os.getenv('APCA_API_SECRET_KEY'), paper=True)

print("1. Cancelling all pending orders...")
client.cancel_orders()

print("2. Closing all active positions...")
client.close_all_positions(cancel_orders=True)

# Verification
remaining = client.get_orders(filter=GetOrdersRequest(status="open"))
if not remaining:
    print("SUCCESS: Your account is now completely clean.")
else:
    print(f"WARNING: {len(remaining)} orders are still stuck.")