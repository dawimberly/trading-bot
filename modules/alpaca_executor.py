"""Canonical Alpaca paper-trading executor (notional orders, crypto formatting)."""

import time

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

import config


class AlpacaExecutor:
    """Submit market orders via alpaca-py with shared credential loading."""

    def __init__(self, paper=None):
        api_key, secret_key = config.get_alpaca_credentials()
        use_paper = config.PAPER_TRADING if paper is None else paper
        if not use_paper and not config.ALLOW_LIVE_TRADING:
            raise RuntimeError(
                "Live trading is disabled. Use Alpaca paper keys with PAPER_TRADING=true, "
                "or set ALLOW_LIVE_TRADING=yes to acknowledge live risk."
            )
        self.paper = use_paper
        self.client = TradingClient(api_key, secret_key, paper=use_paper)

    def get_order_params(self, symbol):
        is_crypto_sym = config.is_crypto(symbol)
        formatted_symbol = symbol.replace("-", "/") if is_crypto_sym else symbol
        tif = TimeInForce.GTC if is_crypto_sym else TimeInForce.DAY
        return formatted_symbol, tif, is_crypto_sym

    def execute_order(self, symbol, side):
        formatted_symbol, tif, _ = self.get_order_params(symbol)
        request_params = GetOrdersRequest(status="open")
        orders = self.client.get_orders(filter=request_params)
        for o in orders:
            if o.symbol == formatted_symbol:
                self.client.cancel_order_by_id(o.id)
                time.sleep(0.5)
        account = self.client.get_account()
        available_cash = float(account.cash)
        target_notional = round(min(available_cash * 0.10, 10000.0), 2)
        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        order = MarketOrderRequest(
            symbol=formatted_symbol,
            notional=target_notional,
            side=order_side,
            time_in_force=tif,
        )
        return self.client.submit_order(order_data=order)


def get_trading_client(paper=None):
    """Return a TradingClient using config credentials (for utility scripts)."""
    api_key, secret_key = config.get_alpaca_credentials()
    use_paper = config.PAPER_TRADING if paper is None else paper
    if not use_paper and not config.ALLOW_LIVE_TRADING:
        raise RuntimeError(
            "Live trading is disabled. Set ALLOW_LIVE_TRADING=yes to acknowledge live risk."
        )
    return TradingClient(api_key, secret_key, paper=use_paper)
