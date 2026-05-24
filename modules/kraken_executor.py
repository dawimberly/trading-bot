from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

def execute_crypto_trade_via_alpaca(trading_client, symbol):
    """Places a BUY order for a fixed dollar amount (e.g., $15.00)."""
    try:
        print(f"Placing new BUY order for {symbol}...")
        # 'notional' specifies the exact dollar amount to buy.
        # This is mutually exclusive with 'qty' and satisfies the $10 minimum.
        order_data = MarketOrderRequest(
            symbol=symbol,
            notional=15.00,  
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC
        )
        trading_client.submit_order(order_data)
        print(f"SUCCESS: Bought {symbol} on Alpaca.")
    except Exception as e:
        print(f"FAILED: Could not buy {symbol}. Error: {e}")

def sell_crypto(trading_client, symbol, qty):
    """Closes an existing position by selling the specified quantity."""
    try:
        print(f"Placing SELL order for {symbol}...")
        order_data = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC
        )
        trading_client.submit_order(order_data)
        print(f"SUCCESS: Sold {symbol} (Closed Position).")
    except Exception as e:
        print(f"FAILED: Could not sell {symbol}. Error: {e}")