"""Test Kraken REST balance endpoint.

Run: python scripts/exchange/test_kraken.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config

api_key, secret_key = config.get_kraken_credentials()
try:
    from kraken.spot import SpotClient

    client = SpotClient(key=api_key, secret=secret_key)
    response = client.request("POST", "/0/private/Balance")
    print("Connection Successful! Your Balance:")
    print(response)
except Exception as e:
    print(f"Connection Failed: {e}")
