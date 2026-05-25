"""US equity session helpers via Alpaca clock."""


def is_equity_market_open(trading_client):
    """True during regular US equity hours (Alpaca clock)."""
    try:
        return bool(trading_client.get_clock().is_open)
    except Exception as e:
        print(f"Market clock unavailable ({e}); treating equity session as closed")
        return False
