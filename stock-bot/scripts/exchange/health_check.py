"""Verify Kraken API credentials and fetch balance.

Run: python scripts/exchange/health_check.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config

api_key, secret_key = config.get_kraken_credentials()
if not api_key or not secret_key:
    print("ERROR: Credentials missing. Check your .env file.")
    sys.exit(1)

try:
    from kraken.spot import SpotClient

    client = SpotClient(key=api_key, secret=secret_key)
    balance = client.request("POST", "/0/private/Balance")
    print("SUCCESS: Connected to Kraken!")
    print("Your Account Balance:", balance)
except Exception as e:
    print(f"FAILED: Could not connect to Kraken. Error: {e}")
