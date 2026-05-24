import os
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient

load_dotenv()
API_KEY = os.getenv("ALPACA_API_KEY")
API_SECRET = os.getenv("ALPACA_SECRET_KEY")

# Initialize client to get account info
client = TradingClient(API_KEY, API_SECRET, paper=True)
account = client.get_account()

print(f"--- ACCOUNT STATUS ---")
print(f"Buying Power: ${account.buying_power}")
print(f"Cash: ${account.cash}")
print(f"Status: {account.status}")
