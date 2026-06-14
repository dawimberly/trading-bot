"""Print Alpaca paper account buying power and cash.

Run: python scripts/account/check_balance.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modules.alpaca_executor import get_trading_client

client = get_trading_client()
account = client.get_account()
print("--- ACCOUNT STATUS ---")
print(f"Buying Power: ${account.buying_power}")
print(f"Cash: ${account.cash}")
print(f"Status: {account.status}")
