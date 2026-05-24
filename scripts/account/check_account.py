"""Print Alpaca paper account status via alpaca-py.

Run: python scripts/account/check_account.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modules.alpaca_executor import get_trading_client

client = get_trading_client()
account = client.get_account()
print(f"Status: {account.status}")
print(f"Cash Available: ${account.cash}")
print(f"Buying Power: ${account.buying_power}")
print(f"Equity: ${account.equity}")
