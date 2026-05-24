"""Verify Alpaca API credentials and connection.

Run: python scripts/account/verify.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modules.alpaca_executor import get_trading_client

try:
    client = get_trading_client()
    account = client.get_account()
    print("--- CONNECTION SUCCESSFUL ---")
    print(f"Account: {account.account_number}")
    print(f"Buying Power: ${account.buying_power}")
    print(f"Status: {account.status}")
except Exception as e:
    print(f"Error connecting to Alpaca: {e}")
