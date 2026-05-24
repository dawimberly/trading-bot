import os
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient

# 1. Load the variables from the .env file
# We explicitly tell it the file path to avoid any confusion
env_path = r'C:\Users\Owner\PythonTrading\.env'
load_dotenv(dotenv_path=env_path)

# 2. Get the keys
api_key = os.getenv('APCA_API_KEY_ID')
secret_key = os.getenv('APCA_API_SECRET_KEY')

# 3. Connection and verification
if not api_key or not secret_key:
    print("ERROR: Could not find keys. Check that your .env file is formatted correctly.")
else:
    try:
        client = TradingClient(api_key, secret_key, paper=True)
        account = client.get_account()
        
        print("--- CONNECTION SUCCESSFUL ---")
        print(f"Account: {account.account_number}")
        print(f"Buying Power: ${account.buying_power}")
        print(f"Status: {account.status}")
        
    except Exception as e:
        print(f"Error connecting to Alpaca: {e}")