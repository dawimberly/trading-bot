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

    def open_position_count(self):
        try:
            return len(self.client.get_all_positions())
        except Exception:
            return 0

    def compute_notional(self):
        account = self.client.get_account()
        equity = float(account.equity)
        cash = float(account.cash)
        raw = round(equity * config.RISK_PER_TRADE, 2)
        capped = min(raw, config.MAX_NOTIONAL_PER_ORDER, round(cash * 0.95, 2))
        return max(config.MIN_NOTIONAL, capped)

    def compute_spy_notional(self):
        account = self.client.get_account()
        equity = float(account.equity)
        cash = float(account.cash)
        raw = round(equity * config.SPY_RISK_PER_TRADE, 2)
        capped = min(raw, config.MAX_NOTIONAL_PER_ORDER, round(cash * 0.95, 2))
        return max(config.MIN_NOTIONAL, capped)

    def execute_full_exit(self, symbol):
        formatted_symbol, _, _ = self.get_order_params(symbol)
        for pos in self.client.get_all_positions():
            if pos.symbol != formatted_symbol:
                continue
            qty = float(pos.qty)
            price = float(pos.current_price or 0)
            if qty <= 0 or price <= 0:
                return None
            return self.execute_order(
                symbol, "sell", notional=round(qty * price, 2), reduce_only=True
            )
        return None

    def execute_order(self, symbol, side, notional=None, reduce_only=False):
        formatted_symbol, tif, is_crypto_sym = self.get_order_params(symbol)
        side_lower = side.lower()

        if not reduce_only:
            if side_lower == "buy" and self.open_position_count() >= config.MAX_OPEN_POSITIONS:
                return None
            if side_lower == "sell":
                held = any(
                    p.symbol == formatted_symbol
                    for p in self.client.get_all_positions()
                )
                if not held:
                    return None

        request_params = GetOrdersRequest(status="open")
        orders = self.client.get_orders(filter=request_params)
        for o in orders:
            if o.symbol == formatted_symbol:
                self.client.cancel_order_by_id(o.id)
                time.sleep(0.5)

        target_notional = notional if notional is not None else self.compute_notional()
        if target_notional < config.MIN_NOTIONAL:
            return None

        order_side = OrderSide.BUY if side_lower == "buy" else OrderSide.SELL
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
