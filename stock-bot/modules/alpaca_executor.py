"""Canonical Alpaca paper-trading executor (notional orders, crypto formatting)."""

import logging
import time
from datetime import datetime
from typing import Callable, TypeVar

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

import config
from modules import deployment_sizing
from modules.alpaca_client import (
    AlpacaValidationError,
    call_with_retry,
    get_trading_client,
)
from modules.cost_basis import underwater_sizing_scale
from modules.logging_utils import log_event

logger = logging.getLogger(__name__)

T = TypeVar("T")


class AlpacaExecutor:
    """Submit market orders via alpaca-py with shared credential loading."""

    def __init__(self, paper=None, credentials_fn=None):
        cred_fn = credentials_fn or config.get_alpaca_credentials
        self._credentials_fn = cred_fn
        use_paper = config.PAPER_TRADING if paper is None else paper
        if not use_paper and not config.ALLOW_LIVE_TRADING:
            raise RuntimeError(
                "Live trading is disabled. Use Alpaca paper keys with PAPER_TRADING=true, "
                "or set ALLOW_LIVE_TRADING=yes to acknowledge live risk."
            )
        self.paper = use_paper
        self.client = get_trading_client(paper=use_paper, credentials_fn=cred_fn)
        self._equity_session_open = None  # None => check Alpaca clock per order
        self._account = None
        self._positions = None
        self._cofire_notionals = {}
        self._account = self._api("get_account", self.client.get_account)
        self._positions = list(self._api("get_all_positions", self.client.get_all_positions))
        self._order_notify_ctx: dict[str, dict] = {}
        self._notified_order_ids: set[str] = set()
        logger.info(
            "AlpacaExecutor initialized",
            extra={"paper": self.paper, "base_url": config.get_alpaca_base_url(paper=self.paper)},
        )

    def _api(self, op_name: str, func: Callable[..., T], /, *args, **kwargs) -> T:
        """Alpaca SDK call with retry/backoff (does not change order logic)."""
        return call_with_retry(func, *args, op_name=op_name, **kwargs)

    def begin_deployment_cycle(self):
        self._cofire_notionals = {}
        self._sizing_data = None
        self._sleeve_pnl = None
        self._paper_feature_flags = config.get_paper_feature_flags()

    def set_sizing_context(self, data=None):
        self._sizing_data = data

    def set_sleeve_pnl(self, sleeve_pnl: dict | None) -> None:
        self._sleeve_pnl = sleeve_pnl

    def set_pod_risk_scales(self, scales: dict[str, float] | None) -> None:
        self._pod_risk_scales = dict(scales) if scales else {}

    def pod_risk_scale(self, pod: str) -> float:
        return float(getattr(self, "_pod_risk_scales", {}).get(pod, 1.0))

    def set_wisdom_sizing_multiplier(self, multiplier: float = 1.0) -> None:
        self._wisdom_sizing_multiplier = float(multiplier)

    def set_dynamic_sleeve_caps(self, caps: dict[str, float] | None) -> None:
        """Per-cycle cap overrides from get_dynamic_sleeve_caps (vol scaling)."""
        self._dynamic_sleeve_caps = dict(caps) if caps else None

    def set_dynamic_risk_context(
        self,
        *,
        vol_score: float,
        regime: str,
        macro_stress: bool,
    ) -> None:
        """Paper aggressive only: feed vol/regime/stress into dynamic risk per trade."""
        if not (config.paper_aggressive_context() and config.PAPER_DYNAMIC_RISK_ENABLED):
            return
        config.set_dynamic_risk_context(
            vol_score=vol_score,
            regime=regime,
            macro_stress=macro_stress,
        )

    def _risk_per_trade(self, equity: float) -> float:
        return config.effective_risk_per_trade(equity)

    def _sleeve_cap_pct(self, key: str, base_pct: float) -> float:
        caps = getattr(self, "_dynamic_sleeve_caps", None)
        if caps and key in caps:
            pct = caps[key]
        elif key == "vti_core":
            return config.vti_core_allocation_pct()
        else:
            pct = config.effective_sleeve_cap(base_pct)
        pod_key = key if key in ("spy", "crypto", "nyse") else None
        if pod_key:
            pct *= self.pod_risk_scale(pod_key)
        return pct

    def _account_equity(self) -> float:
        return float(self._get_account().equity)

    def _min_notional(self) -> float:
        return config.effective_min_notional(self._account_equity())

    def _coerce_notional(self, notional: float | None) -> float | None:
        if notional is None:
            return None
        return round(float(notional), 2)

    def _skip_if_notional_invalid(
        self,
        notional: float | None,
        *,
        symbol: str,
        op: str,
    ) -> float | None:
        """Return rounded notional when valid; log and return None when below min."""
        n = self._coerce_notional(notional)
        min_n = self._min_notional()
        if n is None or n <= 0:
            logger.info(
                "Skip %s: notional $%.2f <= 0 (min $%.2f, symbol=%s)",
                op,
                float(notional or 0),
                min_n,
                symbol,
            )
            return None
        if n < min_n:
            logger.info(
                "Skip %s: notional $%.2f < min $%.2f (symbol=%s)",
                op,
                n,
                min_n,
                symbol,
            )
            return None
        return n

    def _submit_order(self, order, *, symbol: str, op: str):
        """Submit with pre-flight notional guard; validation errors return None."""
        req_notional = getattr(order, "notional", None)
        if req_notional is not None:
            valid = self._skip_if_notional_invalid(req_notional, symbol=symbol, op=op)
            if valid is None:
                return None
            if valid != req_notional:
                order = MarketOrderRequest(
                    symbol=order.symbol,
                    notional=valid,
                    side=order.side,
                    time_in_force=order.time_in_force,
                )
        try:
            return self._api("submit_order", self.client.submit_order, order_data=order)
        except AlpacaValidationError:
            return None

    def _max_notional(self) -> float:
        return config.effective_max_notional_per_order(self._account_equity())

    def _apply_sizing_multiplier(
        self, notional: float | None, *, sleeve_key: str | None = None
    ) -> float | None:
        if notional is None:
            return None
        mult = getattr(self, "_wisdom_sizing_multiplier", 1.0)
        if sleeve_key:
            mult *= underwater_sizing_scale(sleeve_key, getattr(self, "_sleeve_pnl", None))
        if mult >= 0.999:
            return notional
        scaled = round(notional * mult, 2)
        if scaled < self._min_notional():
            return None
        return scaled

    def set_cofire_allocations(self, allocations):
        if not config.effective_cofire_budget_enabled():
            self._cofire_notionals = {}
            return
        self._cofire_notionals = dict(allocations or {})

    def paper_feature_flags(self) -> dict[str, bool]:
        return dict(getattr(self, "_paper_feature_flags", None) or config.get_paper_feature_flags())

    def _invalidate_cache(self):
        self._account = None
        self._positions = None

    def refresh_cache(self):
        self._account = self._api("get_account", self.client.get_account)
        self._positions = list(self._api("get_all_positions", self.client.get_all_positions))
        logger.debug(
            "AlpacaExecutor cache refreshed",
            extra={"equity": float(self._account.equity) if self._account else None},
        )

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

    @staticmethod
    def _order_status(order) -> str:
        return str(getattr(order, "status", "")).lower()

    @staticmethod
    def _order_side_label(order, fallback: str = "") -> str:
        side = getattr(order, "side", None)
        if side is not None:
            raw = str(side).replace("OrderSide.", "").replace("orderside.", "")
            if raw:
                return raw.capitalize()
        return fallback.capitalize() if fallback else "?"

    def _account_type_label(self) -> str:
        return "Paper" if self.paper else "Live"

    def _infer_sleeve(self, symbol: str) -> str:
        norm = config.normalize_symbol(symbol)
        if config.is_crypto(symbol):
            return "Crypto"
        if norm == config.SPY_BOT_SYMBOL:
            return "SPY"
        if config.is_metal_symbol(symbol):
            return "Metal"
        if norm == config.VTI_CORE_SYMBOL:
            return "VTI"
        return "NYSE"

    def _track_order(
        self,
        order,
        *,
        symbol: str,
        side: str,
        reason: str = "",
        sleeve: str | None = None,
    ) -> None:
        oid = str(getattr(order, "id", "") or "")
        if not oid:
            return
        self._order_notify_ctx[oid] = {
            "symbol": config.normalize_symbol(symbol),
            "side": side.capitalize() if side else self._order_side_label(order),
            "reason": reason,
            "sleeve": sleeve or self._infer_sleeve(symbol),
        }

    def _emit_fill_notification(self, order, details: dict) -> None:
        if not details or not details.get("filled"):
            return
        oid = str(getattr(order, "id", "") or "")
        if not oid or oid in self._notified_order_ids:
            return

        ctx = self._order_notify_ctx.pop(oid, {})
        symbol = ctx.get("symbol") or config.normalize_symbol(getattr(order, "symbol", ""))
        side = ctx.get("side") or self._order_side_label(order)
        qty = float(details.get("qty") or self._order_filled_qty(order) or 0)
        avg = float(getattr(order, "filled_avg_price", None) or 0)
        notional = details.get("notional")
        if (not notional or notional <= 0) and qty > 0 and avg > 0:
            notional = round(qty * avg, 2)

        from modules.trade_notifier import send_trade_notification

        send_trade_notification(
            {
                "symbol": symbol,
                "side": side,
                "quantity": qty if qty > 0 else None,
                "price": avg if avg > 0 else None,
                "notional": notional if notional and notional > 0 else None,
                "sleeve": ctx.get("sleeve") or self._infer_sleeve(symbol),
                "reason": ctx.get("reason", ""),
                "account_type": self._account_type_label(),
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        self._notified_order_ids.add(oid)

    def order_filled(self, order, max_wait=5.0, *, require_complete: bool = True):
        """True when Alpaca confirms fill (poll market orders; optional partial OK)."""
        details = self.order_fill_details(
            order, max_wait=max_wait, require_complete=require_complete
        )
        return details is not None and details.get("filled")

    def order_fill_details(
        self, order, max_wait=5.0, *, require_complete: bool = True
    ) -> dict | None:
        """Return fill metadata: filled, qty, notional, status, partial."""
        if order is None:
            return None

        def _details(o) -> dict | None:
            qty = self._order_filled_qty(o)
            status = self._order_status(o)
            avg = float(getattr(o, "filled_avg_price", None) or 0)
            notional = round(qty * avg, 2) if qty > 0 and avg > 0 else 0.0
            if qty <= 0:
                return None
            complete = status == "filled"
            partial = status == "partially_filled" or (
                status not in ("filled",) and qty > 0
            )
            if require_complete and not complete:
                return {
                    "filled": False,
                    "partial": partial,
                    "qty": qty,
                    "notional": notional,
                    "status": status,
                }
            return {
                "filled": True,
                "partial": partial and not complete,
                "qty": qty,
                "notional": notional,
                "status": status,
            }

        first = _details(order)
        if first and first["filled"]:
            self._emit_fill_notification(order, first)
            return first
        if max_wait <= 0:
            return first

        oid = order.id
        deadline = time.time() + max_wait
        latest = first
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                order = self._api("get_order_by_id", self.client.get_order_by_id, oid)
            except Exception:
                return latest
            latest = _details(order)
            if latest and latest["filled"]:
                self._emit_fill_notification(order, latest)
                return latest
            status = self._order_status(order)
            if any(x in status for x in ("cancel", "reject", "expire", "fail")):
                return latest
        try:
            final = _details(
                self._api("get_order_by_id", self.client.get_order_by_id, oid)
            ) or latest
            if final and final.get("filled"):
                self._emit_fill_notification(order, final)
            return final
        except Exception:
            return latest

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
        for o in self._api("get_orders", self.client.get_orders, filter=request_params):
            if config.normalize_symbol(o.symbol) == target:
                self._api("cancel_order_by_id", self.client.cancel_order_by_id, o.id)
                time.sleep(0.5)
                logger.info(
                    "cancelled open order",
                    extra={"symbol": target, "order_id": o.id},
                )

    def cancel_open_equity_orders(self):
        """Cancel queued US equity orders (e.g. unfilled DAY orders after the close)."""
        canceled = 0
        request_params = GetOrdersRequest(status="open")
        for o in self._api("get_orders", self.client.get_orders, filter=request_params):
            if config.is_crypto(o.symbol):
                continue
            self._api("cancel_order_by_id", self.client.cancel_order_by_id, o.id)
            canceled += 1
            time.sleep(0.2)
        if canceled:
            self._invalidate_cache()
            logger.info(
                "cancel_open_equity_orders: cancelled",
                extra={"count": canceled},
            )
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
    def _is_vti_core_position(pos):
        return config.normalize_symbol(pos.symbol) == config.VTI_CORE_SYMBOL

    @staticmethod
    def _is_nyse_sleeve_position(pos):
        if AlpacaExecutor._is_crypto_position(pos):
            return False
        if AlpacaExecutor._is_spy_position(pos):
            return False
        if AlpacaExecutor._is_metal_position(pos):
            return False
        if AlpacaExecutor._is_vti_core_position(pos):
            return False
        if config.is_international_adr(pos.symbol):
            return False
        if config.is_bond_symbol(pos.symbol):
            return False
        return True

    @staticmethod
    def _is_bond_sleeve_position(pos):
        return config.is_bond_symbol(pos.symbol)

    @staticmethod
    def _is_international_sleeve_position(pos):
        return config.is_international_adr(pos.symbol)

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

    def international_sleeve_value(self):
        return self._sleeve_exposure(self._is_international_sleeve_position)

    def bond_sleeve_value(self):
        return self._sleeve_exposure(self._is_bond_sleeve_position)

    def metal_sleeve_value(self):
        return self._sleeve_exposure(self._is_metal_position)

    def spy_sleeve_value(self):
        return self._sleeve_exposure(self._is_spy_position)

    def _compute_capped_notional_raw(self, sleeve_cap_pct, sleeve_value, sleeve_key=None):
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

    def _compute_capped_notional(self, sleeve_cap_pct, sleeve_value, sleeve_key=None):
        return self._apply_sizing_multiplier(
            self._compute_capped_notional_raw(sleeve_cap_pct, sleeve_value, sleeve_key),
            sleeve_key=sleeve_key,
        )

    def compute_notional(self):
        account = self._get_account()
        equity = float(account.equity)
        cash = float(account.cash)
        raw = round(equity * self._risk_per_trade(equity), 2)
        capped = min(raw, self._max_notional(), round(cash * 0.95, 2))
        return self._apply_sizing_multiplier(max(self._min_notional(), capped))

    def compute_crypto_notional(self):
        raw = self._compute_capped_notional(
            self._sleeve_cap_pct("crypto", config.CRYPTO_SLEEVE_CAP_PCT),
            self.crypto_sleeve_value(),
            "crypto",
        )
        return deployment_sizing.apply_alpaca_crypto_fee_reserve(
            raw, equity=self._account_equity()
        )

    def compute_nyse_notional(self):
        return self._compute_capped_notional(
            self._sleeve_cap_pct("nyse", config.NYSE_SLEEVE_CAP_PCT),
            self.nyse_sleeve_value(),
            "nyse",
        )

    def compute_international_notional(self):
        cap_pct = float(getattr(self, "international_cap_pct", 0.0) or 0.0)
        if cap_pct <= 0:
            return None
        return self._compute_capped_notional(
            cap_pct,
            self.international_sleeve_value(),
            "international",
        )

    def compute_bond_notional(self):
        cap_pct = float(getattr(self, "bond_cap_pct", 0.0) or 0.0)
        if cap_pct <= 0:
            return None
        return self._compute_capped_notional(
            cap_pct,
            self.bond_sleeve_value(),
            "bond",
        )

    def spy_position_value(self):
        return self.spy_sleeve_value()

    def compute_spy_notional(self):
        base = self._compute_capped_notional(
            self._sleeve_cap_pct("spy", config.SPY_SLEEVE_CAP_PCT),
            self.spy_sleeve_value(),
            "spy",
        )
        return deployment_sizing.apply_spy_ladder(
            base,
            getattr(self, "_sizing_data", None),
            equity=self._account_equity(),
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
            "spy_cap": equity * self._sleeve_cap_pct("spy", config.SPY_SLEEVE_CAP_PCT),
            "crypto_value": crypto_v,
            "crypto_cap": equity * self._sleeve_cap_pct("crypto", config.CRYPTO_SLEEVE_CAP_PCT),
            "nyse_value": nyse_v,
            "nyse_cap": equity * self._sleeve_cap_pct("nyse", config.NYSE_SLEEVE_CAP_PCT),
        }
        if config.vti_core_enabled():
            from modules.vti_core import vti_core_value

            snap["vti_core_value"] = vti_core_value(self)
            snap["vti_core_cap"] = equity * self._sleeve_cap_pct("vti_core", 0.0)
        if config.metal_sleeve_enabled():
            snap["metal_value"] = metal_v
            snap["metal_cap"] = equity * config.METAL_SLEEVE_CAP_PCT
        sleeve_pnl = getattr(self, "_sleeve_pnl", None)
        if sleeve_pnl:
            snap["sleeve_pnl"] = sleeve_pnl
        return snap

    @staticmethod
    def _normalize_pos_symbol(pos):
        return config.normalize_symbol(pos.symbol)

    def register_pair_symbols(self, long_sym: str, short_sym: str) -> None:
        """Track pair-book symbols for backtest P&L attribution."""
        symbols = getattr(self, "_pair_symbols", None)
        if symbols is None:
            symbols = set()
            self._pair_symbols = symbols
        symbols.add(long_sym)
        symbols.add(short_sym)

    def _find_position(self, symbol):
        target = config.normalize_symbol(symbol)
        for pos in self._get_positions():
            if self._normalize_pos_symbol(pos) == target:
                return pos
        return None

    def execute_reduce_notional(self, symbol, reduce_notional, *, reason="reduce", sleeve=None):
        """Reduce long (sell) or short (buy cover) up to reduce_notional."""
        if not self._equity_trading_allowed(symbol):
            return None
        formatted_symbol, tif, is_crypto_sym = self.get_order_params(symbol)
        pos = self._find_position(symbol)
        if pos is None:
            return None

        qty = float(pos.qty)
        price = float(pos.current_price or pos.avg_entry_price or 0)
        if qty == 0 or price <= 0:
            return None

        self._cancel_open_orders_for(symbol)
        reduce_notional = float(reduce_notional)

        if qty > 0:
            mv = qty * price
            sell_notional = min(reduce_notional, mv)
            sell_notional = self._skip_if_notional_invalid(
                sell_notional, symbol=symbol, op="execute_reduce_notional"
            )
            if sell_notional is None:
                return None
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
                    notional=sell_notional,
                    side=OrderSide.SELL,
                    time_in_force=tif,
                )
        else:
            abs_qty = abs(qty)
            mv = abs_qty * price
            cover_notional = min(reduce_notional, mv)
            cover_notional = self._skip_if_notional_invalid(
                cover_notional, symbol=symbol, op="execute_reduce_notional"
            )
            if cover_notional is None:
                return None
            if is_crypto_sym:
                buy_qty = min(abs_qty, cover_notional / price)
                buy_qty = round(buy_qty, 8)
                if buy_qty <= 0:
                    return None
                order = MarketOrderRequest(
                    symbol=formatted_symbol,
                    qty=buy_qty,
                    side=OrderSide.BUY,
                    time_in_force=tif,
                )
            else:
                order = MarketOrderRequest(
                    symbol=formatted_symbol,
                    notional=cover_notional,
                    side=OrderSide.BUY,
                    time_in_force=tif,
                )
        submitted = self._submit_order(
            order, symbol=symbol, op="execute_reduce_notional"
        )
        if submitted is None:
            return None
        self._invalidate_cache()
        side_label = "Sell" if qty > 0 else "Buy"
        self._track_order(
            submitted,
            symbol=symbol,
            side=side_label,
            reason=reason,
            sleeve=sleeve,
        )
        logger.info(
            "execute_reduce_notional submitted",
            extra={"symbol": symbol, "order_id": getattr(submitted, "id", None)},
        )
        return submitted

    def execute_full_exit(self, symbol, *, reason="exit", sleeve=None):
        if not self._equity_trading_allowed(symbol):
            return None
        pos = self._find_position(symbol)
        if pos is None:
            return None
        formatted_symbol, tif, is_crypto_sym = self.get_order_params(symbol)
        self._cancel_open_orders_for(symbol)
        qty = float(pos.qty)
        price = float(pos.current_price or pos.avg_entry_price or 0)
        if qty == 0 or price <= 0:
            return None
        if qty > 0:
            if is_crypto_sym:
                order = MarketOrderRequest(
                    symbol=formatted_symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=tif,
                )
            else:
                exit_notional = self._skip_if_notional_invalid(
                    qty * price, symbol=symbol, op="execute_full_exit"
                )
                if exit_notional is None:
                    return None
                order = MarketOrderRequest(
                    symbol=formatted_symbol,
                    notional=exit_notional,
                    side=OrderSide.SELL,
                    time_in_force=tif,
                )
        else:
            abs_qty = abs(qty)
            if is_crypto_sym:
                order = MarketOrderRequest(
                    symbol=formatted_symbol,
                    qty=abs_qty,
                    side=OrderSide.BUY,
                    time_in_force=tif,
                )
            else:
                exit_notional = self._skip_if_notional_invalid(
                    abs_qty * price, symbol=symbol, op="execute_full_exit"
                )
                if exit_notional is None:
                    return None
                order = MarketOrderRequest(
                    symbol=formatted_symbol,
                    notional=exit_notional,
                    side=OrderSide.BUY,
                    time_in_force=tif,
                )
        submitted = self._submit_order(order, symbol=symbol, op="execute_full_exit")
        if submitted is None:
            return None
        self._invalidate_cache()
        side_label = "Sell" if qty > 0 else "Buy"
        self._track_order(
            submitted,
            symbol=symbol,
            side=side_label,
            reason=reason,
            sleeve=sleeve,
        )
        logger.info(
            "execute_full_exit submitted",
            extra={"symbol": symbol, "order_id": getattr(submitted, "id", None)},
        )
        return submitted

    def execute_order(
        self,
        symbol,
        side,
        notional=None,
        reduce_only=False,
        *,
        reason="",
        sleeve=None,
    ):
        if not self._equity_trading_allowed(symbol):
            return None
        formatted_symbol, tif, is_crypto_sym = self.get_order_params(symbol)
        side_lower = side.lower()

        if not reduce_only and side_lower == "sell":
            if self._find_position(symbol) is None:
                short_open_ok = (
                    not is_crypto_sym
                    and (
                        config.effective_equity_pairs_enabled()
                        or config.effective_stat_arb_enabled()
                    )
                )
                if not short_open_ok:
                    return None

        self._cancel_open_orders_for(symbol)

        target_notional = notional if notional is not None else self.compute_notional()
        if is_crypto_sym and side_lower == "buy":
            target_notional = deployment_sizing.apply_alpaca_crypto_fee_reserve(
                target_notional, equity=self._account_equity()
            )
        target_notional = self._skip_if_notional_invalid(
            target_notional, symbol=symbol, op="execute_order"
        )
        if target_notional is None:
            return None

        order_side = OrderSide.BUY if side_lower == "buy" else OrderSide.SELL
        order = MarketOrderRequest(
            symbol=formatted_symbol,
            notional=target_notional,
            side=order_side,
            time_in_force=tif,
        )
        submitted = self._submit_order(order, symbol=symbol, op="execute_order")
        if submitted is None:
            return None
        self._invalidate_cache()
        order_id = getattr(submitted, "id", None)
        self._track_order(
            submitted,
            symbol=symbol,
            side=side,
            reason=reason,
            sleeve=sleeve,
        )
        logger.info(
            "order submitted",
            extra={
                "symbol": symbol,
                "side": side.lower(),
                "notional": target_notional,
                "order_id": order_id,
            },
        )
        log_event(
            "order_submitted",
            symbol=symbol,
            side=side.lower(),
            notional=target_notional,
            order_id=order_id,
        )
        return submitted

    def profit_rebuy_blocked(self, symbol, now, *, cooldown_bars=None) -> bool:
        from modules.profit_target import profit_rebuy_blocked as _blocked

        return _blocked(self, symbol, now, cooldown_bars=cooldown_bars)

    def run_profit_target_exits(self, **kwargs) -> int:
        from modules.profit_target import run_profit_target_exits

        return run_profit_target_exits(self, **kwargs)


def get_trading_client(paper=None, credentials_fn=None):
    """Return a cached TradingClient (utility scripts)."""
    from modules.alpaca_client import get_trading_client as _get_client

    return _get_client(paper=paper, credentials_fn=credentials_fn)
