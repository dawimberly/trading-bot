import config
import alpaca_trade_api as tradeapi

# Initialize API
api = tradeapi.REST(config.API_KEY, config.SECRET_KEY, config.BASE_URL, api_version='v2')

# Cancel all open orders
orders = api.list_orders(status='open')
if not orders:
    print("No open orders found.")
else:
    for order in orders:
        api.cancel_order(order.id)
        print(f"Cancelled order: {order.id}")

# Refresh account to show new status
account = api.get_account()
print(f"\n--- Post-Cleanup Status ---")
print(f"Buying Power: ${account.buying_power}")