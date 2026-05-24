import os
from dotenv import load_dotenv
from kraken.spot import SpotClient

# Load the keys from your .env file
load_dotenv()
api_key = os.getenv("KRAKEN_API_KEY")
secret_key = os.getenv("KRAKEN_SECRET_KEY")

# Connect to Kraken
client = SpotClient(key=api_key, secret=secret_key)

# Fetch your balance
try:
    response = client.request("POST", "/0/private/Balance")
    print("Connection Successful! Your Balance:")
    print(response)
except Exception as e:
    print(f"Connection Failed: {e}")