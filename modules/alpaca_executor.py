"""Canonical Alpaca paper-trading executor (notional orders, crypto formatting)."""

import time

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

import config


class AlpacaExecutor:
    """Submit market orders via alpaca-py with shared credential loading."""

    def __init__(self, paper=None, credentials_fn=None):
        cred_fn = credentials_fn or config.get_alpaca_credentials
        api_key, secret_key = cred_fn()
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

    @staticmethod
    def _position_market_value(pos):
        mv = getattr(pos, "market_value", None)
        if mv is not None:
            return abs(float(mv))
        qty = float(pos.qty)
        price = float(pos.current_price or 0)
        return abs(qty * price)

    @staticmethod
    def _is_crypto_position(pos):
        sym = pos.symbol.replace("/", "-")
        return config.is_crypto(sym)

    @staticmethod
    def _is_spy_position(pos):
        return pos.symbol.replace("/", "-") == config.SPY_BOT_SYMBOL

    @staticmethod
    def _is_nyse_sleeve_position(pos):
        if AlpacaExecutor._is_crypto_position(pos):
            return False
        if AlpacaExecutor._is_spy_position(pos):
            return False
        return True

    def _sleeve_exposure(self, predicate):
        total = 0.0
        try:
            for pos in self.client.get_all_positions():
                if predicate(pos):
                    total += self._position_market_value(pos)
        except Exception:
            pass
        return total

    def crypto_sleeve_value(self):
        return self._sleeve_exposure(self._is_crypto_position)

    def nyse_sleeve_value(self):
        return self._sleeve_exposure(self._is_nyse_sleeve_position)

    def spy_sleeve_value(self):
        return self._sleeve_exposure(self._is_spy_position)

    def _compute_capped_notional(self, sleeve_cap_pct, sleeve_value):
        account = self.client.get_account()
        equity = float(account.equity)
        cash = float(account.cash)
        cap = round(equity * sleeve_cap_pct, 2)
        room = round(cap - sleeve_value, 2)
        if room < config.MIN_NOTIONAL:
            return None
        per_trade = round(equity * config.RISK_PER_TRADE, 2)
        raw = min(room, per_trade, config.MAX_NOTIONAL_PER_ORDER, round(cash * 0.95, 2))
        if raw < config.MIN_NOTIONAL:
            return None
        return raw

    def compute_notional(self):
        account = self.client.get_account()
        equity = float(account.equity)
        cash = float(account.cash)
        raw = round(equity * config.RISK_PER_TRADE, 2)
        capped = min(raw, config.MAX_NOTIONAL_PER_ORDER, round(cash * 0.95, 2))
        return max(config.MIN_NOTIONAL, capped)

    def compute_crypto_notional(self):
        return self._compute_capped_notional(
            config.CRYPTO_SLEEVE_CAP_PCT, self.crypto_sleeve_value()
        )

    def compute_nyse_notional(self):
        return self._compute_capped_notional(
            config.NYSE_SLEEVE_CAP_PCT, self.nyse_sleeve_value()
        )

    def spy_position_value(self):
        return self.spy_sleeve_value()

    def compute_spy_notional(self):
        return self._compute_capped_notional(
            config.SPY_SLEEVE_CAP_PCT, self.spy_sleeve_value()
        )

    def sleeve_snapshot(self):
        account = self.client.get_account()
        equity = float(account.equity)
        spy_v = self.spy_sleeve_value()
        crypto_v = self.crypto_sleeve_value()
        nyse_v = self.nyse_sleeve_value()
        return {
            "equity": equity,
            "spy_value": spy_v,
            "spy_cap": equity * config.SPY_SLEEVE_CAP_PCT,
            "crypto_value": crypto_v,
            "crypto_cap": equity * config.CRYPTO_SLEEVE_CAP_PCT,
            "nyse_value": nyse_v,
            "nyse_cap": equity * config.NYSE_SLEEVE_CAP_PCT,
        }

    @staticmethod
    def _normalize_pos_symbol(pos):
        return config.normalize_symbol(pos.symbol)

    def _find_position(self, symbol):
        target = config.normalize_symbol(symbol)
        for pos in self.client.get_all_positions():
            if self._normalize_pos_symbol(pos) == target:
                return pos
        return None

    def execute_reduce_notional(self, symbol, sell_notional):
        """Sell up to sell_notional; crypto uses qty to avoid insufficient-balance errors."""
        formatted_symbol, tif, is_crypto_sym = self.get_order_params(symbol)
        pos = self._find_position(symbol)
        if pos is None:
            return None

        qty = float(pos.qty)
        price = float(pos.current_price or pos.avg_entry_price or 0)
        if qty <= 0 or price <= 0:
            return None

        mv = qty * price
        sell_notional = min(float(sell_notional), mv)
        if sell_notional < config.MIN_NOTIONAL:
            return None

        request_params = GetOrdersRequest(status="open")
        for o in self.client.get_orders(filter=request_params):
            if config.normalize_symbol(o.symbol) == config.normalize_symbol(symbol):
                self.client.cancel_order_by_id(o.id)
                time.sleep(0.5)

        if is_crypto_sym:
            sell_qty = min(qty, sell_notional / price)
            sell_qty = round(sell_qty, 8)
            if sell_qty <= 0:
                return None
            order = MarketOrderRequest(
                symbol=formatted_symbol,
                qty=sell_qty,
                side=OrderSide.SELL,
                time_in_force=tif,
            )
        else:
            order = MarketOrderRequest(
                symbol=formatted_symbol,
                notional=round(sell_notional, 2),
                side=OrderSide.SELL,
                time_in_force=tif,
            )
        return self.client.submit_order(order_data=order)

    def execute_full_exit(self, symbol):
        pos = self._find_position(symbol)
        if pos is None:
            return None
        formatted_symbol, tif, is_crypto_sym = self.get_order_params(symbol)
        qty = float(pos.qty)
        price = float(pos.current_price or 0)
        if qty <= 0 or price <= 0:
            return None
        if is_crypto_sym:
            order = MarketOrderRequest(
                symbol=formatted_symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=tif,
            )
        else:
            order = MarketOrderRequest(
                symbol=formatted_symbol,
                notional=round(qty * price, 2),
                side=OrderSide.SELL,
                time_in_force=tif,
            )
        return self.client.submit_order(order_data=order)

    def execute_order(self, symbol, side, notional=None, reduce_only=False):
        formatted_symbol, tif, is_crypto_sym = self.get_order_params(symbol)
        side_lower = side.lower()

        if not reduce_only:
            if side_lower == "buy" and self.open_position_count() >= config.MAX_OPEN_POSITIONS:
                return None
            if side_lower == "sell":
                if self._find_position(symbol) is None:
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
