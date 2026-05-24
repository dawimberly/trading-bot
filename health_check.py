import os
from dotenv import load_dotenv
from kraken.spot import SpotClient

# 1. Load the .env file
# This tells your computer to look for the .env file in the same folder
load_dotenv()

# 2. Get the keys from the .env file
api_key = os.getenv('KRAKEN_API_KEY')
secret_key = os.getenv('KRAKEN_SECRET_KEY')

# 3. Check if keys exist in memory
if not api_key or not secret_key:
    print("ERROR: Credentials missing. Check your .env file.")
    exit()

# 4. Connect to Kraken and get balance
try:
    client = SpotClient(key=api_key, secret=secret_key)
    # This calls your actual Kraken account
    balance = client.request("POST", "/0/private/Balance")
    print("SUCCESS: Connected to Kraken!")
    print("Your Account Balance:", balance)
except Exception as e:
    print(f"FAILED: Could not connect to Kraken. Error: {e}")