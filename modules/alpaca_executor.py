"""Canonical Alpaca paper-trading executor (notional orders, crypto formatting)."""

import time

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

import config
from modules import deployment_sizing


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
        self._equity_session_open = None  # None => check Alpaca clock per order
        self._account = None
        self._positions = None
        self._cofire_notionals = {}
        """Fetch account + positions once per pipeline cycle."""
        self._account = self.client.get_account()
        self._positions = list(self.client.get_all_positions())
        return self._account

    def begin_deployment_cycle(self):
        self._cofire_notionals = {}
        self._sizing_data = None

    def set_sizing_context(self, data=None):
        self._sizing_data = data

    def set_cofire_allocations(self, allocations):
        self._cofire_notionals = dict(allocations or {})

    def _invalidate_cache(self):
        self._account = None
        self._positions = None

    @property
    def equity_session_open(self):
        return self._equity_session_open

    @equity_session_open.setter
    def equity_session_open(self, value):
        self._equity_session_open = bool(value) if value is not None else None

    def _get_account(self):
        if self._account is None:
            self.refresh_cache()
        return self._account

    def _get_positions(self):
        if self._positions is None:
            self.refresh_cache()
        return self._positions

    @staticmethod
    def _order_filled_qty(order):
        if order is None:
            return 0.0
        return float(getattr(order, "filled_qty", None) or 0)

    def order_filled(self, order, max_wait=2.0):
        """True when Alpaca reports a non-zero fill (brief poll for market orders)."""
        if order is None:
            return False
        if self._order_filled_qty(order) > 0:
            return True
        if max_wait <= 0:
            return False
        oid = order.id
        deadline = time.time() + max_wait
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                order = self.client.get_order_by_id(oid)
            except Exception:
                return False
            if self._order_filled_qty(order) > 0:
                return True
            status = str(getattr(order, "status", "")).lower()
            if any(x in status for x in ("cancel", "reject", "expire", "fail")):
                return False
        try:
            return self._order_filled_qty(self.client.get_order_by_id(oid)) > 0
        except Exception:
            return False

    def _equity_trading_allowed(self, symbol):
        if config.is_crypto(symbol):
            return True
        if self._equity_session_open is True:
            return True
        if self._equity_session_open is False:
            return False
        from modules.market_hours import is_equity_market_open

        return is_equity_market_open(self.client)

    def _cancel_open_orders_for(self, symbol):
        target = config.normalize_symbol(symbol)
        request_params = GetOrdersRequest(status="open")
        for o in self.client.get_orders(filter=request_params):
            if config.normalize_symbol(o.symbol) == target:
                self.client.cancel_order_by_id(o.id)
                time.sleep(0.5)

    def cancel_open_equity_orders(self):
        """Cancel queued US equity orders (e.g. unfilled DAY orders after the close)."""
        canceled = 0
        request_params = GetOrdersRequest(status="open")
        for o in self.client.get_orders(filter=request_params):
            if config.is_crypto(o.symbol):
                continue
            self.client.cancel_order_by_id(o.id)
            canceled += 1
            time.sleep(0.2)
        if canceled:
            self._invalidate_cache()
        return canceled

    def get_order_params(self, symbol):
        is_crypto_sym = config.is_crypto(symbol)
        formatted_symbol = symbol.replace("-", "/") if is_crypto_sym else symbol
        tif = TimeInForce.GTC if is_crypto_sym else TimeInForce.DAY
        return formatted_symbol, tif, is_crypto_sym

    def open_position_count(self):
        try:
            return len(self._get_positions())
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
    def _is_metal_position(pos):
        return config.is_metal_symbol(pos.symbol)

    @staticmethod
    def _is_nyse_sleeve_position(pos):
        if AlpacaExecutor._is_crypto_position(pos):
            return False
        if AlpacaExecutor._is_spy_position(pos):
            return False
        if AlpacaExecutor._is_metal_position(pos):
            return False
        return True

    def _sleeve_exposure(self, predicate):
        total = 0.0
        try:
            for pos in self._get_positions():
                if predicate(pos):
                    total += self._position_market_value(pos)
        except Exception:
            pass
        return total

    def crypto_sleeve_value(self):
        return self._sleeve_exposure(self._is_crypto_position)

    def nyse_sleeve_value(self):
        return self._sleeve_exposure(self._is_nyse_sleeve_position)

    def metal_sleeve_value(self):
        return self._sleeve_exposure(self._is_metal_position)

    def spy_sleeve_value(self):
        return self._sleeve_exposure(self._is_spy_position)

    def _compute_capped_notional(self, sleeve_cap_pct, sleeve_value, sleeve_key=None):
        account = self._get_account()
        equity = float(account.equity)
        cash = float(account.cash)
        return deployment_sizing.resolve_sleeve_notional(
            equity,
            cash,
            sleeve_cap_pct,
            sleeve_value,
            sleeve_key or "",
            self._cofire_notionals,
        )

    def compute_notional(self):
        account = self._get_account()
        equity = float(account.equity)
        cash = float(account.cash)
        raw = round(equity * config.RISK_PER_TRADE, 2)
        capped = min(raw, config.MAX_NOTIONAL_PER_ORDER, round(cash * 0.95, 2))
        return max(config.MIN_NOTIONAL, capped)

    def compute_crypto_notional(self):
        return self._compute_capped_notional(
            config.effective_sleeve_cap(config.CRYPTO_SLEEVE_CAP_PCT),
            self.crypto_sleeve_value(),
            "crypto",
        )

    def compute_nyse_notional(self):
        return self._compute_capped_notional(
            config.effective_sleeve_cap(config.NYSE_SLEEVE_CAP_PCT),
            self.nyse_sleeve_value(),
            "nyse",
        )

    def spy_position_value(self):
        return self.spy_sleeve_value()

    def compute_spy_notional(self):
        base = self._compute_capped_notional(
            config.effective_sleeve_cap(config.SPY_SLEEVE_CAP_PCT),
            self.spy_sleeve_value(),
            "spy",
        )
        return deployment_sizing.apply_spy_ladder(
            base, getattr(self, "_sizing_data", None)
        )

    def sleeve_snapshot(self):
        account = self._get_account()
        equity = float(account.equity)
        spy_v = self.spy_sleeve_value()
        crypto_v = self.crypto_sleeve_value()
        nyse_v = self.nyse_sleeve_value()
        metal_v = self.metal_sleeve_value()
        snap = {
            "equity": equity,
            "spy_value": spy_v,
            "spy_cap": equity * config.effective_sleeve_cap(config.SPY_SLEEVE_CAP_PCT),
            "crypto_value": crypto_v,
            "crypto_cap": equity * config.effective_sleeve_cap(config.CRYPTO_SLEEVE_CAP_PCT),
            "nyse_value": nyse_v,
            "nyse_cap": equity * config.effective_sleeve_cap(config.NYSE_SLEEVE_CAP_PCT),
        }
        if config.metal_sleeve_enabled():
            snap["metal_value"] = metal_v
            snap["metal_cap"] = equity * config.METAL_SLEEVE_CAP_PCT
        return snap

    @staticmethod
    def _normalize_pos_symbol(pos):
        return config.normalize_symbol(pos.symbol)

    def _find_position(self, symbol):
        target = config.normalize_symbol(symbol)
        for pos in self._get_positions():
            if self._normalize_pos_symbol(pos) == target:
                return pos
        return None

    def execute_reduce_notional(self, symbol, sell_notional):
        """Sell up to sell_notional; crypto uses qty to avoid insufficient-balance errors."""
        if not self._equity_trading_allowed(symbol):
            return None
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

        self._cancel_open_orders_for(symbol)

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
        order = self.client.submit_order(order_data=order)
        self._invalidate_cache()
        return order

    def execute_full_exit(self, symbol):
        if not self._equity_trading_allowed(symbol):
            return None
        pos = self._find_position(symbol)
        if pos is None:
            return None
        formatted_symbol, tif, is_crypto_sym = self.get_order_params(symbol)
        self._cancel_open_orders_for(symbol)
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
        order = self.client.submit_order(order_data=order)
        self._invalidate_cache()
        return order

    def execute_order(self, symbol, side, notional=None, reduce_only=False):
        if not self._equity_trading_allowed(symbol):
            return None
        formatted_symbol, tif, is_crypto_sym = self.get_order_params(symbol)
        side_lower = side.lower()

        if not reduce_only:
            if side_lower == "sell":
                if self._find_position(symbol) is None:
                    return None

        self._cancel_open_orders_for(symbol)

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
        submitted = self.client.submit_order(order_data=order)
        self._invalidate_cache()
        return submitted


def get_trading_client(paper=None):
    """Return a TradingClient using config credentials (for utility scripts)."""
    api_key, secret_key = config.get_alpaca_credentials()
    use_paper = config.PAPER_TRADING if paper is None else paper
    if not use_paper and not config.ALLOW_LIVE_TRADING:
        raise RuntimeError(
            "Live trading is disabled. Set ALLOW_LIVE_TRADING=yes to acknowledge live risk."
        )
    return TradingClient(api_key, secret_key, paper=use_paper)
