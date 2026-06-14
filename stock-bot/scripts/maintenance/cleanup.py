"""Cancel all open Alpaca orders via alpaca-py.

Run: python scripts/maintenance/cleanup.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from alpaca.trading.requests import GetOrdersRequest

from modules.alpaca_executor import get_trading_client

client = get_trading_client()
orders = client.get_orders(filter=GetOrdersRequest(status="open"))
if not orders:
    print("No open orders found.")
else:
    for order in orders:
        client.cancel_order_by_id(order.id)
        print(f"Cancelled order: {order.id}")
