"""US equity session helpers via Alpaca clock."""

from modules.alpaca_client import call_with_retry


def is_equity_market_open(trading_client):
    """True during regular US equity hours (Alpaca clock)."""
    try:
        clock = call_with_retry(trading_client.get_clock, op_name="get_clock")
        return bool(clock.is_open)
    except Exception as e:
        print(f"Market clock unavailable ({e}); treating equity session as closed")
        return False
