import config
import alpaca_trade_api as tradeapi

api = tradeapi.REST(config.API_KEY, config.SECRET_KEY, config.BASE_URL, api_version='v2')

account = api.get_account()
print(f"Status: {account.status}")
print(f"Cash Available: ${account.cash}")
print(f"Buying Power: ${account.buying_power}")
print(f"Equity: ${account.equity}")