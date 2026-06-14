"""Cancel all orders and close all Alpaca positions.

Run: python scripts/account/reset_account.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from alpaca.trading.requests import GetOrdersRequest

from modules.alpaca_executor import get_trading_client

client = get_trading_client()
print("1. Cancelling all pending orders...")
client.cancel_orders()
print("2. Closing all active positions...")
client.close_all_positions(cancel_orders=True)
remaining = client.get_orders(filter=GetOrdersRequest(status="open"))
if not remaining:
    print("SUCCESS: Your account is now completely clean.")
else:
    print(f"WARNING: {len(remaining)} orders are still stuck.")
