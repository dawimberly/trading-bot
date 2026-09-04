"""Canonical Alpaca paper-trading executor (notional orders, crypto formatting)."""

import logging
import math
import time
import os
from types import SimpleNamespace
from datetime import datetime
from typing import Callable, TypeVar

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

import config
from modules import deployment_sizing
from modules.alpaca_client import (
    AlpacaAuthError,
    AlpacaValidationError,
    call_with_retry,
    get_trading_client,
    is_not_fractionable_error,
    is_unknown_asset_error,
)
from modules.cost_basis import underwater_sizing_scale
from modules.logging_utils import log_event

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Session-level skip list for symbols Alpaca rejects as unknown/untradable (e.g. SKY-USD).
_UNKNOWN_ASSETS: set[str] = set()
_TRADABLE_ASSETS: set[str] = set()
# Whole-share only — Alpaca notional/fractional orders 403 with 40310000.
_NON_FRACTIONABLE_ASSETS: set[str] = set()


class AlpacaExecutor:
    """Submit market orders via alpaca-py with shared credential loading.

    EXTENSION POINT — adding a new broker (multi-broker support):
      The trading loop only relies on a small duck-typed surface, so a new broker
      needs a class exposing the same methods used by run_all.py / the sleeves:
        - execute_order(symbol, side, *, notional=..., reason=..., sleeve=..., ...)
        - _get_account()  -> object with `.equity`
        - _get_positions() / get_all_positions() -> iterable of positions
        - prices (dict-like)  and  portfolio (with .equity(prices))
      Keep the same PAPER_TRADING / ALLOW_LIVE_TRADING guard so a misconfigured
      live key can never trade by accident. MockExecutor (modules/mock_executor.py)
      is the minimal reference implementation for backtests.
    """

    def __init__(self, paper=None, credentials_fn=None, *, allow_live=None):
        cred_fn = credentials_fn or config.get_alpaca_credentials
        self._credentials_fn = cred_fn
        use_paper = config.PAPER_TRADING if paper is None else paper
        use_allow_live = config.ALLOW_LIVE_TRADING if allow_live is None else allow_live
        if not use_paper and not use_allow_live:
            raise RuntimeError(
                "Live trading is disabled. Use Alpaca paper keys with PAPER_TRADING=true, "
                "or set ALLOW_LIVE_TRADING=yes to acknowledge live risk."
            )
        self.paper = use_paper
        # DRY_RUN mode: set via environment DRY_RUN=1|true|yes to prevent any real submission.
        self.dry_run = str(os.getenv("DRY_RUN", "")).strip().lower() in ("1", "true", "yes", "on")
        if self.dry_run:
            logger.warning("AlpacaExecutor started in DRY_RUN mode — orders will not be submitted")
        self.client = get_trading_client(
            paper=use_paper, credentials_fn=cred_fn, allow_live=use_allow_live
        )
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

    def _dry_submit_mock(self, order):
        """Return a lightweight fake order object for DRY_RUN mode and log the full order request.

        The mock mimics the minimal attributes used elsewhere (id, filled_qty, status, symbol, filled_avg_price).
        """
        try:
            oid = f"DRY-{int(time.time() * 1000)}"
            logger.info(
                "DRY_RUN order (not submitted)",
                extra={
                    "order_repr": repr(order),
                    "dry_order_id": oid,
                    "symbol": getattr(order, "symbol", None),
                    "qty": getattr(order, "qty", None),
                    "notional": getattr(order, "notional", None),
                },
            )
            return SimpleNamespace(
                id=oid,
                filled_qty=0,
                status="new",
                symbol=getattr(order, "symbol", None),
                filled_avg_price=None,
            )
        except Exception as exc:
            logger.exception("Failed to build dry-run order mock: %s", exc)
            return None

    def _validate_order_symbol(self, symbol: str) -> bool:
        norm = config.normalize_symbol(symbol)
        if not norm:
            logger.warning("Order skipped: empty or invalid symbol", extra={"symbol": symbol})
            return False
        if str(symbol).strip() != str(symbol):
            logger.warning(
                "Order skipped: symbol contains surrounding whitespace",
                extra={"symbol": symbol},
            )
            return False
        if self._is_unknown_asset(symbol):
            logger.info("Order skipped unknown asset %s", symbol)
            return False
        return True

    def _sanitize_qty(self, qty, *, precision: int = 8, max_qty=None):
        try:
            qty_value = float(qty)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(qty_value):
            return None
        if max_qty is not None:
            try:
                max_value = float(max_qty)
            except (TypeError, ValueError):
                return None
            qty_value = min(qty_value, max_value)
        qty_value = max(0.0, qty_value)
        if qty_value <= 0:
            return None
        factor = 10**precision
        return math.floor(qty_value * factor) / factor

    def _format_qty_for_alpaca(self, qty, *, is_crypto: bool, max_qty=None):
        safe_qty = self._sanitize_qty(
            qty,
            precision=8 if is_crypto else 6,
            max_qty=max_qty,
        )
        if safe_qty is None:
            return None
        if is_crypto:
            return safe_qty
        return format(safe_qty, "f").rstrip("0").rstrip(".") or "0"

    def _sanitize_notional(self, notional, *, min_notional: float | None = None):
        try:
            notional_value = float(notional)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(notional_value):
            return None
        rounded = round(notional_value, 2)
        if min_notional is not None and rounded < float(min_notional):
            return None
        return rounded

    def _submit_order(
        self,
        order,
        *,
        symbol: str,
        side: str,
        reason: str = "",
        sleeve: str | None = None,
        notional=None,
        qty=None,
        op: str | None = None,
    ):
        """Submit order; ``side`` is required by all execute_* callers.

        Optional ``op`` is only used for notional-guard log labels (defaults to side).
        """
        if not self._validate_order_symbol(symbol):
            return None

        op_label = (op or side or "submit").strip() or "submit"

        # Pre-flight notional on the request object (qty-only orders skip this).
        req_notional = notional if notional is not None else getattr(order, "notional", None)
        if req_notional is not None:
            valid = self._skip_if_notional_invalid(
                req_notional, symbol=symbol, op=op_label
            )
            if valid is None:
                return None
            if valid != self._coerce_notional(req_notional):
                order = MarketOrderRequest(
                    symbol=order.symbol,
                    notional=valid,
                    side=order.side,
                    time_in_force=order.time_in_force,
                )
                notional = valid

        safe_notional = None
        if notional is not None:
            safe_notional = self._sanitize_notional(
                notional,
                min_notional=self._min_notional(),
            )
            if safe_notional is None:
                logger.info(
                    "Order skipped: notional below min threshold",
                    extra={
                        "symbol": symbol,
                        "side": side,
                        "reason": reason,
                        "sleeve": sleeve,
                        "requested_notional": notional,
                    },
                )
                return None
            try:
                order.notional = safe_notional
            except Exception:
                pass

        if qty is not None:
            safe_qty = self._format_qty_for_alpaca(
                qty,
                is_crypto=config.is_crypto(symbol),
                max_qty=qty,
            )
            if safe_qty is None:
                logger.warning(
                    "Order skipped: invalid quantity",
                    extra={
                        "symbol": symbol,
                        "side": side,
                        "reason": reason,
                        "sleeve": sleeve,
                        "requested_qty": qty,
                    },
                )
                return None
            try:
                order.qty = safe_qty
            except Exception:
                pass

        if getattr(self, "dry_run", False):
            logger.info(
                "Dry-run preflight passed",
                extra={
                    "symbol": symbol,
                    "side": side,
                    "reason": reason,
                    "sleeve": sleeve,
                    "qty": getattr(order, "qty", None),
                    "notional": getattr(order, "notional", None),
                },
            )
            submitted = self._dry_submit_mock(order)
        else:
            try:
                submitted = self._api(
                    "submit_order", self.client.submit_order, order_data=order
                )
            except Exception as exc:
                try:
                    from modules import error_watcher

                    error_watcher.log_failed_order(
                        symbol=symbol,
                        side=side,
                        reason=reason or "",
                        error=str(exc),
                        notional=getattr(order, "notional", None),
                    )
                except Exception:
                    pass
                raise
        # Capture avg entry before cache drop so sell fills can compute realized PnL
        # even if the position is gone after a full exit.
        avg_entry = None
        if str(side or "").lower() in ("sell", "sell_short"):
            try:
                pos = self._find_position(symbol)
                raw = getattr(pos, "avg_entry_price", None) if pos is not None else None
                avg_entry = float(raw) if raw not in (None, "") else None
                if avg_entry is not None and avg_entry <= 0:
                    avg_entry = None
            except Exception:
                avg_entry = None
        self._invalidate_cache()
        self._track_order(
            submitted,
            symbol=symbol,
            side=side,
            reason=reason,
            sleeve=sleeve,
            avg_entry=avg_entry,
        )
        logger.info(
            "order submitted",
            extra={
                "symbol": symbol,
                "side": side.lower(),
                "qty": getattr(order, "qty", None),
                "notional": getattr(order, "notional", None),
                "order_id": getattr(submitted, "id", None),
            },
        )
        try:
            from modules import error_watcher

            error_watcher.log_action(
                "order_submitted",
                symbol=symbol,
                side=side,
                reason=reason,
                sleeve=sleeve,
                notional=getattr(order, "notional", None),
                order_id=str(getattr(submitted, "id", "") or ""),
            )
        except Exception:
            pass
        try:
            from modules.pipeline_strategies import mark_nyse_atr_stop_from_exit

            mark_nyse_atr_stop_from_exit(symbol, reason=reason or "", sleeve=sleeve)
        except Exception:
            pass
        # Poll fill so Telegram buy/sell alerts fire even when callers don't wait.
        # Deduped by order id inside _emit_fill_notification.
        if submitted is not None and not getattr(self, "dry_run", False):
            try:
                paper = bool(getattr(config, "PAPER_TRADING", False))
                wait = 5.0 if paper else 3.0
                details = self.order_fill_details(submitted, max_wait=wait)
                # Paper VTI/notional often fills after 5s. Same notify path; no second journal.
                if paper and (not details or not details.get("filled")):
                    details = self.order_fill_details(submitted, max_wait=25.0)
                if paper and (not details or not details.get("filled")):
                    logger.warning(
                        "fill notify missed after extra wait order_id=%s status=%s",
                        getattr(submitted, "id", ""),
                        self._order_status(submitted) if submitted is not None else "",
                    )
            except Exception as exc:
                logger.debug("post-submit fill poll failed: %s", exc)
        return submitted

    @staticmethod
    def _mark_unknown_asset(symbol: str, exc: BaseException | None = None) -> None:
        sym = config.normalize_symbol(symbol)
        if not sym or sym in _UNKNOWN_ASSETS:
            return
        _UNKNOWN_ASSETS.add(sym)
        logger.warning(
            "Skipping unknown/untradable Alpaca asset for rest of session: %s (%s)",
            sym,
            exc or "blacklisted",
        )

    def _is_unknown_asset(self, symbol: str) -> bool:
        return config.normalize_symbol(symbol) in _UNKNOWN_ASSETS

    @staticmethod
    def _mark_not_fractionable(symbol: str) -> None:
        sym = config.normalize_symbol(symbol)
        if not sym or sym in _NON_FRACTIONABLE_ASSETS:
            return
        _NON_FRACTIONABLE_ASSETS.add(sym)
        logger.info("Alpaca asset %s is not fractionable — whole-share orders only", sym)

    def _is_not_fractionable(self, symbol: str) -> bool:
        return config.normalize_symbol(symbol) in _NON_FRACTIONABLE_ASSETS

    def _last_equity_price(self, symbol: str) -> float | None:
        try:
            from modules.real_time_data import get_latest_price

            px = get_latest_price(symbol)
            if px is not None and float(px) > 0:
                return float(px)
        except Exception:
            pass
        data = getattr(self, "_sizing_data", None)
        if data is None:
            return None
        try:
            cols = getattr(data, "columns", [])
            if symbol not in cols:
                return None
            series = data[symbol].dropna()
            if len(series) == 0:
                return None
            px = float(series.iloc[-1])
            return px if px > 0 else None
        except Exception:
            return None

    def _whole_share_qty_for_notional(self, symbol: str, notional) -> str | None:
        px = self._last_equity_price(symbol)
        if px is None or px <= 0:
            return None
        shares = int(math.floor(float(notional) / px))
        if shares < 1:
            return None
        return str(shares)

    def _asset_tradable(self, symbol: str) -> bool:
        """Best-effort tradability check; False on 401/403/404/not-found. Never raises."""
        sym = config.normalize_symbol(symbol)
        if not sym or sym in _UNKNOWN_ASSETS:
            return False
        if sym in _TRADABLE_ASSETS:
            return True
        try:
            formatted, _, _ = self.get_order_params(symbol)
            asset = self._api("get_asset", self.client.get_asset, formatted)
            tradable = bool(getattr(asset, "tradable", True))
            if not tradable:
                self._mark_unknown_asset(sym, "not tradable")
                return False
            if not bool(getattr(asset, "fractionable", True)):
                self._mark_not_fractionable(sym)
            _TRADABLE_ASSETS.add(sym)
            return True
        except AlpacaAuthError as exc:
            # Account-level auth — do not blacklist the symbol.
            logger.warning("get_asset auth failed for %s (not blacklisting): %s", sym, exc)
            return True
        except Exception as exc:
            if is_unknown_asset_error(exc) or "not found" in str(exc).lower():
                self._mark_unknown_asset(sym, exc)
                return False
            if isinstance(exc, AlpacaValidationError) and "not found" in str(exc).lower():
                self._mark_unknown_asset(sym, exc)
                return False
            status = getattr(exc, "status_code", None) or getattr(
                getattr(exc, "__cause__", None), "status_code", None
            )
            if status in (404,):
                self._mark_unknown_asset(sym, exc)
                return False
            if status in (401, 403):
                # Per-asset forbidden vs account auth: skip symbol without crashing cycle.
                self._mark_unknown_asset(sym, exc)
                return False
            logger.debug("get_asset check skipped for %s: %s", sym, exc)
            return True

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

    def set_current_regime(self, regime: str) -> None:
        self._current_regime = str(regime or "")

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
        account = self._get_account()
        equity = float(account.equity)
        cash = float(account.cash)
        broker_pct = round(cash / equity, 6) if equity > 0 else None
        regime = getattr(self, "_current_regime", None) or None

        if key == "vti_core":
            return config.vti_core_allocation_pct()

        if key == "nyse" and (
            config.paper_aggressive_context() or config.is_realistic_research_active()
        ):
            # Prefer expanded paper NYSE target; floor dynamic constructor caps when cash high.
            target = config.effective_nyse_sleeve_cap_pct(
                broker_pct,
                equity=equity,
                cash=cash,
                regime=regime,
                base_pct=base_pct,
            )
            if caps and key in caps:
                dyn = float(caps[key])
                if config.paper_deploy_aggressive(broker_pct, equity=equity, cash=cash):
                    pct = max(dyn, target)
                else:
                    pct = max(dyn, min(target, float(config.PAPER_NYSE_SLEEVE_CAP_PCT)))
            else:
                pct = target
        elif caps and key in caps:
            pct = caps[key]
        else:
            pct = config.effective_sleeve_cap(base_pct)

        pod_key = key if key in ("spy", "crypto", "nyse", "stat_arb") else None
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

    def _max_notional(self) -> float:
        return config.effective_max_notional_per_order(self._account_equity())

    def _apply_sizing_multiplier(
        self, notional: float | None, *, sleeve_key: str | None = None
    ) -> float | None:
        if notional is None:
            return None
        mult = getattr(self, "_wisdom_sizing_multiplier", 1.0)
        if sleeve_key and str(sleeve_key).upper() != "SHORT":
            hedge = getattr(self, "_short_long_hedge_mult", 1.0)
            if hedge < 0.999:
                mult *= hedge
        if sleeve_key:
            mult *= underwater_sizing_scale(sleeve_key, getattr(self, "_sleeve_pnl", None))
        if mult >= 0.999:
            return notional
        scaled = round(notional * mult, 2)
        min_n = self._min_notional()
        if scaled < min_n:
            # Live leftover NYSE clips are already near the broker floor
            # (~$5). Wisdom 0.5× must not zero an order that already cleared min.
            if (
                not config.PAPER_TRADING
                and str(sleeve_key or "").strip().lower() == "nyse"
                and float(notional) >= min_n
            ):
                return round(min_n, 2)
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
        status = getattr(order, "status", None)
        if status is None:
            return ""
        # Alpaca SDK enums stringify as "OrderStatus.FILLED" — normalize to "filled".
        raw = getattr(status, "value", None) or getattr(status, "name", None) or str(status)
        return (
            str(raw)
            .replace("OrderStatus.", "")
            .replace("orderstatus.", "")
            .strip()
            .lower()
        )

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
            return "NYSE" if config.metal_counts_as_nyse() else "Metal"
        if config.nyse_allow_vanguard() and config.is_vanguard_or_index_etf(norm):
            return "NYSE"
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
        avg_entry=None,
    ) -> None:
        oid = str(getattr(order, "id", "") or "")
        if not oid:
            return
        self._order_notify_ctx[oid] = {
            "symbol": config.normalize_symbol(symbol),
            "side": side.capitalize() if side else self._order_side_label(order),
            "reason": reason,
            "sleeve": sleeve or self._infer_sleeve(symbol),
            "avg_entry": avg_entry,
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

        equity_after = None
        try:
            equity_after = float(self._account_equity() or 0) or None
        except Exception:
            equity_after = None

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
                "equity_after": equity_after,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        self._notified_order_ids.add(oid)
        try:
            from modules import error_watcher

            error_watcher.log_action(
                "fill",
                symbol=symbol,
                side=side,
                notional=notional,
                qty=qty,
                sleeve=ctx.get("sleeve"),
            )
        except Exception:
            pass
        self._journal_fill(
            order,
            details,
            ctx=ctx,
            symbol=symbol,
            side=side,
            qty=qty,
            avg=avg,
            notional=notional,
            equity_after=equity_after,
            oid=oid,
        )

    def _journal_fill(
        self,
        order,
        details: dict,
        *,
        ctx: dict,
        symbol: str,
        side: str,
        qty: float,
        avg: float,
        notional,
        equity_after,
        oid: str,
    ) -> None:
        """Observational blotter write. Never raises into the order path."""
        try:
            from modules import trade_journal

            side_l = str(side or "").lower()
            is_sell = side_l in ("sell", "sell_short")
            avg_entry = ctx.get("avg_entry")
            realized = trade_journal.compute_realized_pnl(
                qty, avg, avg_entry, is_sell=is_sell
            )
            realized_pct = ""
            if realized is not None and avg_entry not in (None, "") and qty:
                try:
                    cost = abs(float(qty) * float(avg_entry))
                    if cost > 0:
                        realized_pct = round(100.0 * float(realized) / cost, 4)
                except (TypeError, ValueError):
                    realized_pct = ""
            cash_after = ""
            try:
                cash_after = round(float(getattr(self._get_account(), "cash", 0) or 0), 2)
            except Exception:
                cash_after = ""
            regime = str(getattr(self, "_last_regime", "") or "")
            book = "paper" if self.paper else "live"
            reason = ctx.get("reason") or ""
            sleeve = ctx.get("sleeve") or self._infer_sleeve(symbol)
            is_partial = bool(details.get("partial"))
            trade_journal.log_fill(
                symbol,
                side_l,
                qty=round(qty, 8) if qty else "",
                price=round(avg, 6) if avg else "",
                notional=round(float(notional), 2) if notional else "",
                sleeve=sleeve,
                reason=reason,
                pair_key=reason,
                order_id=oid,
                equity=round(float(equity_after), 2) if equity_after not in (None, "") else "",
                cash=cash_after,
                regime=regime,
                book=book,
                exit_reason=reason if is_sell else "",
                realized_pnl="" if realized is None else realized,
                realized_pnl_pct=realized_pct,
                is_partial="1" if is_partial else "0",
                notes=reason,
            )
        except Exception:
            logger.debug("journal fill write skipped", exc_info=True)

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
        raw = str(symbol)
        # Slash USD pairs (e.g. WIF/USD from crypto vol sleeve) are crypto even
        # when outside the static CRYPTO_TICKERS / main UNIVERSE set.
        is_crypto_sym = config.is_crypto(symbol) or (
            "/" in raw and raw.upper().endswith("USD")
        )
        formatted_symbol = raw.replace("-", "/") if is_crypto_sym else raw
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
        if AlpacaExecutor._is_metal_position(pos) and not config.metal_counts_as_nyse():
            return False
        if config.nyse_allow_vanguard() and config.is_vanguard_or_index_etf(pos.symbol):
            return True
        if AlpacaExecutor._is_vti_core_position(pos):
            return False
        # Sector SPDRs belong to the sector-rotation sleeve, not NYSE momentum.
        try:
            from modules.sector_screener import sector_etf_symbols

            if config.normalize_symbol(pos.symbol) in set(sector_etf_symbols()):
                return False
        except Exception:
            pass
        return True

    def _sleeve_exposure(self, predicate):
        total = 0.0
        try:
            for pos in self._get_positions():
                if predicate(pos):
                    total += self._position_market_value(pos)
        except Exception as exc:
            logger.debug("sleeve exposure scan failed: %s", exc)
        return total

    def stat_arb_sleeve_value(self):
        from modules.stat_arb_sleeve import stat_arb_sleeve_gross_value

        return stat_arb_sleeve_gross_value(self)

    def nyse_momentum_sleeve_value(self):
        total = self.nyse_sleeve_value()
        if not config.effective_stat_arb_sleeve_cap_enabled():
            return total
        from modules.stat_arb_sleeve import stat_arb_pair_symbols

        pair_syms = stat_arb_pair_symbols(self)
        if not pair_syms:
            return total
        excluded = 0.0
        try:
            for pos in self._get_positions():
                sym = config.normalize_symbol(pos.symbol)
                if sym not in pair_syms or float(pos.qty) <= 0:
                    continue
                excluded += self._position_market_value(pos)
        except Exception:
            return total
        return max(0.0, total - excluded)

    def crypto_sleeve_value(self):
        return self._sleeve_exposure(self._is_crypto_position)

    def nyse_sleeve_value(self):
        return self._sleeve_exposure(self._is_nyse_sleeve_position)

    def metal_sleeve_value(self):
        return self._sleeve_exposure(self._is_metal_position)

    def spy_sleeve_value(self):
        return self._sleeve_exposure(self._is_spy_position)

    def _log_deploy_skip(self, sleeve_key, reason, *, symbol=None, extra=None):
        if not config.PAPER_DEPLOY_DEBUG or not reason:
            return
        sym = f" {symbol}" if symbol else ""
        detail = f" {extra}" if extra else ""
        logger.info(
            "deploy_skip sleeve=%s%s reason=%s%s",
            sleeve_key or "?",
            sym,
            reason,
            detail,
        )

    def _compute_capped_notional_raw(self, sleeve_cap_pct, sleeve_value, sleeve_key=None):
        account = self._get_account()
        equity = float(account.equity)
        cash = float(account.cash)
        broker_pct = round(cash / equity, 6) if equity > 0 else None
        if config.paper_deploy_aggressive(broker_pct, equity=equity, cash=cash):
            logger.info(
                "aggressive deploy mode activated equity=%.0f cash=%.0f cash_pct=%.1f%%",
                equity,
                cash,
                (broker_pct or 0.0) * 100.0,
            )
        notional, skip_reason = deployment_sizing.resolve_sleeve_notional_detail(
            equity,
            cash,
            sleeve_cap_pct,
            sleeve_value,
            sleeve_key or "",
            self._cofire_notionals,
            regime=getattr(self, "_current_regime", None) or None,
        )
        if (
            notional is None
            and skip_reason
            and str(skip_reason).startswith("no_room")
            and sleeve_key == "nyse"
            and config.paper_deploy_aggressive(broker_pct, equity=equity, cash=cash)
        ):
            # High-cash paper: allow a small top-up even when sleeve room is dust.
            top_up = deployment_sizing.high_cash_nyse_top_up_notional(
                equity,
                cash,
                sleeve_cap_pct,
                sleeve_value,
                cash_pct=broker_pct,
            )
            if top_up is not None:
                logger.info(
                    "NYSE high-cash top-up allowed notional=%.2f (was %s) equity=%.0f cash=%.0f",
                    top_up,
                    skip_reason,
                    equity,
                    cash,
                )
                return top_up
        if notional is None and skip_reason:
            self._log_deploy_skip(
                sleeve_key,
                skip_reason,
                extra=f"equity={equity:.0f} cash={cash:.0f}",
            )
        return notional

    def _compute_capped_notional(
        self, sleeve_cap_pct, sleeve_value, sleeve_key=None, symbol=None
    ):
        raw = self._compute_capped_notional_raw(
            sleeve_cap_pct, sleeve_value, sleeve_key
        )
        if raw is None:
            return None
        scaled = self._apply_sizing_multiplier(raw, sleeve_key=sleeve_key)
        if scaled is None:
            self._log_deploy_skip(
                sleeve_key,
                "sizing_multiplier",
                symbol=symbol,
            )
            return None
        if scaled < config.effective_min_notional(self._account_equity()):
            self._log_deploy_skip(
                sleeve_key,
                "min_notional",
                symbol=symbol,
                extra=f"raw={scaled:.2f}",
            )
            return None
        if not symbol:
            return scaled
        adjusted = self._atr_adjust_notional(symbol, scaled)
        if adjusted is None:
            self._log_deploy_skip(
                sleeve_key,
                "atr_adjust",
                symbol=symbol,
            )
        return adjusted

    def compute_notional(self, symbol=None):
        del symbol
        account = self._get_account()
        equity = float(account.equity)
        cash = float(account.cash)
        broker_pct = round(cash / equity, 6) if equity > 0 else None
        cash_use = (
            config.PAPER_AGGRESSIVE_CASH_USE_PCT
            if config.paper_deploy_aggressive(broker_pct, equity=equity, cash=cash)
            else 0.95
        )
        raw = round(equity * self._risk_per_trade(equity), 2)
        capped = min(raw, self._max_notional(), round(cash * cash_use, 2))
        order_min = (
            config.ALPACA_MIN_NOTIONAL
            if config.paper_deploy_aggressive(broker_pct, equity=equity, cash=cash)
            else self._min_notional()
        )
        return self._apply_sizing_multiplier(max(order_min, capped))

    def _atr_adjust_notional(self, symbol, notional):
        if notional is None:
            return None
        from modules.risk_management import atr_adjust_notional

        return atr_adjust_notional(
            notional,
            self._account_equity(),
            symbol,
            getattr(self, "_sizing_data", None),
        )

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
        sleeve_val = (
            self.nyse_momentum_sleeve_value()
            if config.effective_stat_arb_sleeve_cap_enabled()
            else self.nyse_sleeve_value()
        )
        return self._compute_capped_notional(
            self._sleeve_cap_pct("nyse", config.NYSE_SLEEVE_CAP_PCT),
            sleeve_val,
            "nyse",
        )

    def compute_stat_arb_notional(self):
        equity = self._account_equity()
        min_n = config.effective_min_notional(equity)
        leg_min = min_n * (
            config.PAPER_STAT_ARB_LEG_MIN_MULT
            if config.paper_aggressive_context()
            else 2.0
        )
        if config.effective_stat_arb_sleeve_cap_enabled():
            raw = self._compute_capped_notional(
                self._sleeve_cap_pct("stat_arb", config.STAT_ARB_SLEEVE_CAP_PCT),
                self.stat_arb_sleeve_value(),
                "stat_arb",
            )
        else:
            raw = self.compute_nyse_notional()
        if raw is None:
            return None
        if float(raw) < leg_min:
            self._log_deploy_skip(
                "stat_arb",
                "leg_min_notional",
                extra=f"raw={float(raw):.2f} min={leg_min:.2f}",
            )
            return None
        return raw

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
            "crypto_cap": (
                equity * self._sleeve_cap_pct("crypto", config.CRYPTO_SLEEVE_CAP_PCT)
                if config.effective_crypto_enabled()
                else 0.0
            ),
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

    @staticmethod
    def _position_signed_qty(pos) -> float:
        try:
            return float(getattr(pos, "qty", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _position_available_qty(pos) -> float:
        """Qty that can actually be closed now (respects Alpaca qty_available).

        Never returns a magnitude larger than ``abs(qty)``. Prefer
        ``qty_available`` when present so we do not over-request (403
        insufficient qty).
        """
        qty = AlpacaExecutor._position_signed_qty(pos)
        if qty == 0:
            return 0.0
        raw = getattr(pos, "qty_available", None)
        if raw is None:
            return qty
        try:
            avail = float(raw)
        except (TypeError, ValueError):
            return qty
        if qty > 0:
            free = abs(avail) if avail >= 0 else 0.0
            return max(0.0, min(qty, free))
        free = abs(avail)
        return -max(0.0, min(abs(qty), free))

    def _fresh_position(self, symbol):
        """Refresh broker positions and return the named position (or None)."""
        try:
            self.refresh_cache()
        except Exception as exc:
            logger.warning("position refresh failed for %s: %s", symbol, exc)
            self._invalidate_cache()
            try:
                self.refresh_cache()
            except Exception:
                pass
        return self._find_position(symbol)

    def _maybe_dust_exit(self, symbol, pos, *, reason: str, sleeve=None):
        """If the open position itself is dust, close via auto-dust path.

        Does not treat a locked/partial ``qty_available`` remnant of a larger
        holding as dust (that would recurse or refuse). Callers sell
        broker-available qty instead.
        """
        if not config.effective_auto_dust_cleaner_enabled():
            return None
        from modules.dust_cleanup import is_dust_position, position_qty_notional

        qty, notional = position_qty_notional(pos)
        thresh = config.effective_auto_dust_max_notional()
        if is_dust_position(qty, notional, max_notional=thresh):
            return self.execute_exit_with_auto_dust(
                symbol, reason=reason or "dust_exit", sleeve=sleeve
            )
        return None

    def execute_reduce_notional(
        self, symbol, reduce_notional, *, reason="reduce", sleeve=None, skip_dust: bool = False
    ):
        """Reduce long (sell) or short (buy cover) up to reduce_notional."""
        if not self._equity_trading_allowed(symbol):
            return None
        formatted_symbol, tif, is_crypto_sym = self.get_order_params(symbol)
        self._cancel_open_orders_for(symbol)
        pos = self._fresh_position(symbol)
        if pos is None:
            return None

        qty = self._position_available_qty(pos)
        price = float(pos.current_price or pos.avg_entry_price or 0)
        if qty == 0 or price <= 0:
            return None

        if not skip_dust:
            dust = self._maybe_dust_exit(
                symbol, pos, reason=reason or "dust_exit", sleeve=sleeve
            )
            if dust is not None:
                return dust

        reduce_notional = float(reduce_notional)
        side_label = "Sell" if qty > 0 else "Buy"
        if qty > 0:
            mv = qty * price
            sell_notional = min(reduce_notional, mv)
            min_n = self._min_notional()
            if sell_notional < min_n:
                if config.paper_aggressive_context() and mv <= min_n * 3:
                    return self.execute_full_exit(
                        symbol, reason=reason or "dust_exit", sleeve=sleeve
                    )
                return None
            sell_notional = self._skip_if_notional_invalid(
                sell_notional, symbol=symbol, op="execute_reduce_notional"
            )
            if sell_notional is None:
                return None
            # Draft paper VTI cash-need skip. Flag default OFF: this branch is skipped.
            if (
                getattr(config, "PAPER_VTI_CASH_NEED_SKIP", False)
                and config.normalize_symbol(symbol) == config.VTI_CORE_SYMBOL
            ):
                try:
                    from modules.vti_core import (
                        log_paper_vti_reduce_skip,
                        paper_vti_cash_need_skip_active,
                        paper_vti_reduce_skip_reason,
                    )

                    if paper_vti_cash_need_skip_active():
                        account = self._get_account()
                        equity = float(getattr(account, "equity", 0) or 0)
                        cash = float(getattr(account, "cash", 0) or 0)
                        vti_pct = (mv / equity) if equity > 0 else 0.0
                        skip = paper_vti_reduce_skip_reason(
                            vti_pct=vti_pct,
                            cash=cash,
                            reduce_notional=float(sell_notional),
                            enabled=True,
                        )
                        if skip:
                            log_paper_vti_reduce_skip(
                                skip,
                                vti_pct=vti_pct,
                                cash=cash,
                                reduce_notional=float(sell_notional),
                                symbol=symbol,
                            )
                            return None
                except Exception:
                    pass
            # Qty-capped to available — never let notional→qty overshoot broker free qty.
            raw_qty = min(qty, sell_notional / price)
            raw_qty = min(raw_qty, max(0.0, qty * (1.0 - 1e-9)))
            safe_qty = self._format_qty_for_alpaca(
                raw_qty, is_crypto=is_crypto_sym, max_qty=qty
            )
            if safe_qty is None:
                return None
            order = MarketOrderRequest(
                symbol=formatted_symbol,
                qty=safe_qty,
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
            raw_qty = min(abs_qty, cover_notional / price)
            raw_qty = min(raw_qty, max(0.0, abs_qty * (1.0 - 1e-9)))
            safe_qty = self._format_qty_for_alpaca(
                raw_qty, is_crypto=is_crypto_sym, max_qty=abs_qty
            )
            if safe_qty is None:
                return None
            order = MarketOrderRequest(
                symbol=formatted_symbol,
                qty=safe_qty,
                side=OrderSide.BUY,
                time_in_force=tif,
            )
        submitted = self._submit_order(
            order,
            symbol=symbol,
            side=side_label,
            reason=reason,
            sleeve=sleeve,
            qty=safe_qty,
        )
        if submitted is None:
            return None
        logger.info(
            "execute_reduce_notional submitted",
            extra={"symbol": symbol, "order_id": getattr(submitted, "id", None)},
        )
        return submitted

    def execute_full_exit(self, symbol, *, reason="exit", sleeve=None, skip_dust: bool = False):
        if not self._equity_trading_allowed(symbol):
            return None
        self._cancel_open_orders_for(symbol)
        pos = self._fresh_position(symbol)
        if pos is None:
            return None
        formatted_symbol, tif, is_crypto_sym = self.get_order_params(symbol)
        qty = self._position_available_qty(pos)
        price = float(pos.current_price or pos.avg_entry_price or 0)
        if qty == 0 or price <= 0:
            return None

        if not skip_dust:
            dust = self._maybe_dust_exit(
                symbol, pos, reason=reason or "dust_exit", sleeve=sleeve
            )
            if dust is not None:
                return dust

        side_label = "Sell" if qty > 0 else "Buy"
        abs_qty = abs(qty)
        trade_qty = max(0.0, abs_qty * (1.0 - 1e-9))
        safe_qty = self._format_qty_for_alpaca(
            trade_qty, is_crypto=is_crypto_sym, max_qty=abs_qty
        )
        if safe_qty is None:
            return None
        order = MarketOrderRequest(
            symbol=formatted_symbol,
            qty=safe_qty,
            side=OrderSide.SELL if qty > 0 else OrderSide.BUY,
            time_in_force=tif,
        )
        submitted = self._submit_order(
            order,
            symbol=symbol,
            side=side_label,
            reason=reason,
            sleeve=sleeve,
            qty=safe_qty,
        )
        if submitted is None:
            return None
        logger.info(
            "execute_full_exit submitted",
            extra={
                "symbol": symbol,
                "order_id": getattr(submitted, "id", None),
                "qty": safe_qty,
                "available": abs_qty,
            },
        )
        return submitted

    def execute_qty_exit(self, symbol, qty, *, reason="exit", sleeve=None, skip_dust: bool = False):
        """Market exit for an exact share/unit quantity (no notional rounding).

        Positive *qty* sells that many shares of a long; for shorts, positive *qty*
        covers (buys) that many shares. Caps to broker-available size.
        """
        if not self._equity_trading_allowed(symbol):
            return None
        self._cancel_open_orders_for(symbol)
        pos = self._fresh_position(symbol)
        if pos is None:
            return None
        formatted_symbol, tif, is_crypto_sym = self.get_order_params(symbol)
        held = self._position_available_qty(pos)
        if held == 0:
            return None
        try:
            want = abs(float(qty))
        except (TypeError, ValueError):
            return None
        if want <= 0:
            return None

        if not skip_dust:
            dust = self._maybe_dust_exit(
                symbol, pos, reason=reason or "dust_exit", sleeve=sleeve
            )
            if dust is not None:
                return dust

        trade_qty = min(want, abs(held))
        trade_qty = max(0.0, trade_qty * (1.0 - 1e-9))
        if trade_qty <= 0:
            return None
        if held > 0:
            side = OrderSide.SELL
            side_label = "Sell"
        else:
            side = OrderSide.BUY
            side_label = "Buy"
        safe_qty = self._format_qty_for_alpaca(
            trade_qty, is_crypto=is_crypto_sym, max_qty=abs(held)
        )
        if safe_qty is None or safe_qty == "0":
            return None
        order = MarketOrderRequest(
            symbol=formatted_symbol,
            qty=safe_qty,
            side=side,
            time_in_force=tif,
        )
        submitted = self._submit_order(
            order,
            symbol=symbol,
            side=side_label,
            reason=reason,
            sleeve=sleeve,
            qty=safe_qty,
        )
        if submitted is None:
            return None
        logger.info(
            "execute_qty_exit submitted",
            extra={
                "symbol": symbol,
                "qty": safe_qty,
                "side": side_label,
                "order_id": getattr(submitted, "id", None),
            },
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
        if self._is_unknown_asset(symbol):
            logger.info("execute_order skipped unknown asset %s", symbol)
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
                        or config.effective_opportunistic_short_enabled()
                    )
                )
                if not short_open_ok:
                    return None

        # Crypto (and any *-USD lookalike) may exist in SQLite but not on Alpaca.
        if is_crypto_sym and not reduce_only and not self._asset_tradable(symbol):
            return None

        self._cancel_open_orders_for(symbol)

        target_notional = notional if notional is not None else self.compute_notional()
        if target_notional is not None and not is_crypto_sym:
            target_notional = self._atr_adjust_notional(symbol, target_notional)
        if is_crypto_sym and side_lower == "buy":
            target_notional = deployment_sizing.apply_alpaca_crypto_fee_reserve(
                target_notional, equity=self._account_equity()
            )
        target_notional = self._skip_if_notional_invalid(
            target_notional, symbol=symbol, op="execute_order"
        )
        if target_notional is None:
            return None
        if side_lower == "buy":
            if self._blocks_new_active_ticker(symbol):
                logger.info(
                    "execute_order blocked: max active tickers",
                    extra={
                        "symbol": symbol,
                        "active": self.count_active_tickers(),
                        "max": config.effective_max_active_tickers(),
                    },
                )
                log_event(
                    "active_ticker_cap_block",
                    symbol=symbol,
                    active=self.count_active_tickers(),
                    max_active=config.effective_max_active_tickers(),
                )
                return None
            target_notional = self._apply_concentration_cap(symbol, target_notional)
            if target_notional is None or target_notional < self._min_notional():
                logger.info(
                    "execute_order blocked: concentration guard",
                    extra={"symbol": symbol},
                )
                return None
        elif side_lower == "sell":
            # Cap sell notional to broker-available position value.
            pos = self._fresh_position(symbol)
            if pos is not None:
                avail = abs(self._position_available_qty(pos))
                price = float(pos.current_price or pos.avg_entry_price or 0)
                if avail <= 0 or price <= 0:
                    return None
                dust = self._maybe_dust_exit(
                    symbol, pos, reason=reason or "dust_exit", sleeve=sleeve
                )
                if dust is not None:
                    return dust
                max_n = avail * price
                if target_notional > max_n:
                    target_notional = max_n
                if target_notional < self._min_notional():
                    return self.execute_full_exit(
                        symbol, reason=reason or "dust_exit", sleeve=sleeve
                    )
                # Prefer qty-capped sell over notional to avoid 403 overshoot.
                raw_qty = min(avail, float(target_notional) / price)
                raw_qty = min(raw_qty, max(0.0, avail * (1.0 - 1e-9)))
                safe_qty = self._format_qty_for_alpaca(
                    raw_qty, is_crypto=is_crypto_sym, max_qty=avail
                )
                if safe_qty is None:
                    return None
                order = MarketOrderRequest(
                    symbol=formatted_symbol,
                    qty=safe_qty,
                    side=OrderSide.SELL,
                    time_in_force=tif,
                )
                try:
                    submitted = self._submit_order(
                        order,
                        symbol=symbol,
                        side=side,
                        reason=reason,
                        sleeve=sleeve,
                        qty=safe_qty,
                    )
                except AlpacaValidationError as exc:
                    if is_unknown_asset_error(exc):
                        self._mark_unknown_asset(symbol, exc)
                        return None
                    raise
                if submitted is None:
                    return None
                order_id = getattr(submitted, "id", None)
                logger.info(
                    "order submitted",
                    extra={
                        "symbol": symbol,
                        "side": side.lower(),
                        "qty": safe_qty,
                        "order_id": order_id,
                    },
                )
                log_event(
                    "order_submitted",
                    symbol=symbol,
                    side=side.lower(),
                    qty=safe_qty,
                    order_id=order_id,
                )
                return submitted

        order_side = OrderSide.BUY if side_lower == "buy" else OrderSide.SELL
        if (
            side_lower == "buy"
            and not is_crypto_sym
            and self._is_not_fractionable(symbol)
        ):
            qty = self._whole_share_qty_for_notional(symbol, target_notional)
            if qty is None:
                logger.info(
                    "execute_order skipped: %s is not fractionable and no whole share "
                    "fits notional $%.2f",
                    symbol,
                    float(target_notional),
                )
                return None
            order = MarketOrderRequest(
                symbol=formatted_symbol,
                qty=qty,
                side=order_side,
                time_in_force=tif,
            )
            try:
                submitted = self._submit_order(
                    order,
                    symbol=symbol,
                    side=side,
                    reason=reason,
                    sleeve=sleeve,
                    qty=qty,
                )
            except AlpacaValidationError as exc:
                if is_unknown_asset_error(exc):
                    self._mark_unknown_asset(symbol, exc)
                    return None
                logger.info(
                    "execute_order skipped whole-share buy for %s: %s", symbol, exc
                )
                return None
            if submitted is None:
                return None
            order_id = getattr(submitted, "id", None)
            logger.info(
                "order submitted",
                extra={
                    "symbol": symbol,
                    "side": side.lower(),
                    "qty": qty,
                    "order_id": order_id,
                },
            )
            log_event(
                "order_submitted",
                symbol=symbol,
                side=side.lower(),
                qty=qty,
                order_id=order_id,
            )
            return submitted

        order = MarketOrderRequest(
            symbol=formatted_symbol,
            notional=target_notional,
            side=order_side,
            time_in_force=tif,
        )
        try:
            submitted = self._submit_order(
                order,
                symbol=symbol,
                side=side,
                reason=reason,
                sleeve=sleeve,
                notional=target_notional,
            )
        except AlpacaValidationError as exc:
            if is_unknown_asset_error(exc):
                self._mark_unknown_asset(symbol, exc)
                return None
            if is_not_fractionable_error(exc) and side_lower == "buy" and not is_crypto_sym:
                self._mark_not_fractionable(symbol)
                qty = self._whole_share_qty_for_notional(symbol, target_notional)
                if qty is None:
                    logger.info(
                        "execute_order skipped: %s not fractionable; cannot size "
                        "whole shares from $%.2f",
                        symbol,
                        float(target_notional),
                    )
                    return None
                logger.info(
                    "Retrying %s as %s whole shares (notional $%.2f not fractionable)",
                    symbol,
                    qty,
                    float(target_notional),
                )
                qty_order = MarketOrderRequest(
                    symbol=formatted_symbol,
                    qty=qty,
                    side=order_side,
                    time_in_force=tif,
                )
                try:
                    submitted = self._submit_order(
                        qty_order,
                        symbol=symbol,
                        side=side,
                        reason=reason,
                        sleeve=sleeve,
                        qty=qty,
                    )
                except AlpacaValidationError as retry_exc:
                    if is_unknown_asset_error(retry_exc):
                        self._mark_unknown_asset(symbol, retry_exc)
                    logger.info(
                        "execute_order skipped whole-share retry for %s: %s",
                        symbol,
                        retry_exc,
                    )
                    return None
            else:
                raise
        if submitted is None:
            return None
        order_id = getattr(submitted, "id", None)
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

    @staticmethod
    def _core_exempt_symbols() -> frozenset[str]:
        return frozenset(
            {
                config.normalize_symbol(config.VTI_CORE_SYMBOL),
                config.normalize_symbol(config.SPY_BOT_SYMBOL),
            }
        )

    def _is_core_exempt(self, symbol: str) -> bool:
        if config.nyse_allow_vanguard() and config.is_vanguard_or_index_etf(symbol):
            return True
        return config.normalize_symbol(symbol) in self._core_exempt_symbols()

    def _position_market_value(self, pos) -> float:
        from modules.dust_cleanup import position_qty_notional

        _qty, notional = position_qty_notional(pos)
        return float(notional)

    def list_active_tickers(self) -> list[str]:
        """Non-core open symbols counted toward MAX_ACTIVE_TICKERS."""
        active: list[str] = []
        for pos in self._get_positions():
            qty = float(getattr(pos, "qty", 0) or 0)
            if qty == 0:
                continue
            sym = config.normalize_symbol(self._normalize_pos_symbol(pos))
            if self._is_core_exempt(sym):
                continue
            active.append(sym)
        return sorted(set(active))

    def count_active_tickers(self) -> int:
        return len(self.list_active_tickers())

    def _blocks_new_active_ticker(self, symbol: str) -> bool:
        """True when buying a new name would exceed MAX_ACTIVE_TICKERS."""
        if self._is_core_exempt(symbol):
            return False
        sym = config.normalize_symbol(symbol)
        if self._find_position(sym) is not None:
            return False
        return self.count_active_tickers() >= config.effective_max_active_tickers()

    def _per_name_target_value(self, symbol: str, pos=None) -> tuple[float, float, str]:
        """Return (target_mv, equity, reason) for concentration / fat-loser caps."""
        equity = float(self._account_equity() or 0)
        sym = config.normalize_symbol(symbol)
        cap_pct = config.effective_per_name_max_pct_for_symbol(sym)
        target = equity * cap_pct if equity > 0 else 0.0
        reason = "per_name_cap"
        if (
            config.effective_nyse_fat_loser_enabled()
            and pos is not None
            and equity > 0
        ):
            try:
                from modules.cost_basis import sleeve_for_symbol

                if sleeve_for_symbol(sym) == "nyse" and not config.is_vanguard_or_index_etf(
                    sym
                ):
                    up_pct = float(getattr(pos, "unrealized_plpc", 0) or 0)
                    if up_pct <= float(config.PAPER_NYSE_FAT_LOSER_OPEN_PCT):
                        fat_pct = float(config.PAPER_NYSE_FAT_LOSER_TARGET_PCT)
                        fat_target = equity * fat_pct
                        if fat_target < target:
                            target = fat_target
                            reason = "nyse_fat_loser"
            except Exception:
                pass
        return target, equity, reason

    def _apply_concentration_cap(self, symbol: str, notional: float) -> float | None:
        """Cap buy notional so position ≤ symbol-specific per-name max of equity."""
        if not config.effective_concentration_guard_enabled():
            return notional
        if self._is_core_exempt(symbol):
            return notional
        if notional is None:
            return notional
        sym = config.normalize_symbol(symbol)
        pos = self._find_position(sym)
        cap_val, equity, reason = self._per_name_target_value(sym, pos)
        if equity <= 0:
            return notional
        current_val = self._position_market_value(pos) if pos is not None else 0.0
        room = max(0.0, cap_val - current_val)
        capped = min(float(notional), room)
        min_n = self._min_notional()
        cap_pct = (cap_val / equity) if equity else 0.0
        if capped < min_n:
            log_event(
                "concentration_guard_block",
                symbol=sym,
                equity=round(equity, 2),
                cap_pct=round(cap_pct, 4),
                reason=reason,
                current=round(current_val, 2),
                requested=round(float(notional), 2),
            )
            return None
        if capped + 1e-9 < float(notional):
            log_event(
                "concentration_guard_cap",
                symbol=sym,
                from_notional=round(float(notional), 2),
                to_notional=round(capped, 2),
                cap_pct=round(cap_pct, 4),
                reason=reason,
            )
        return round(capped, 2)

    def list_concentration_excess(self) -> list[dict]:
        """Positions above the per-name % cap (excludes VTI/SPY core)."""
        if not config.effective_concentration_guard_enabled():
            return []
        equity = float(self._account_equity() or 0)
        if equity <= 0:
            return []
        min_n = self._min_notional()
        excess: list[dict] = []
        for pos in self._get_positions():
            qty = float(getattr(pos, "qty", 0) or 0)
            if qty <= 0:
                continue
            sym = config.normalize_symbol(self._normalize_pos_symbol(pos))
            if self._is_core_exempt(sym):
                continue
            cap_val, _, reason = self._per_name_target_value(sym, pos)
            pos_val = self._position_market_value(pos)
            over = pos_val - cap_val
            if over < min_n:
                continue
            excess.append(
                {
                    "symbol": sym,
                    "market_value": round(pos_val, 2),
                    "cap_value": round(cap_val, 2),
                    "excess": round(over, 2),
                    "pct_of_equity": round(pos_val / equity, 4),
                    "reason": reason,
                }
            )
        return sorted(excess, key=lambda r: r["excess"], reverse=True)

    def trim_concentration_excess(self, *, dry_run: bool | None = None) -> list[dict]:
        """Sell down names above the per-name cap. dry_run defaults to executor.dry_run."""
        use_dry = self.dry_run if dry_run is None else bool(dry_run)
        actions: list[dict] = []
        for row in self.list_concentration_excess():
            sell_n = round(float(row["excess"]), 2)
            reason = str(row.get("reason") or "concentration_guard_trim")
            if reason == "nyse_fat_loser":
                sell_reason = "nyse_fat_loser_trim"
            else:
                sell_reason = "concentration_guard_trim"
            action = {
                **row,
                "action": "sell",
                "notional": sell_n,
                "status": "dry_run" if use_dry else "pending",
            }
            if use_dry:
                action["status"] = "dry_run"
                actions.append(action)
                continue
            submitted = self.execute_order(
                row["symbol"],
                "sell",
                notional=sell_n,
                reason=sell_reason,
            )
            action["status"] = "submitted" if submitted is not None else "failed"
            action["order_id"] = getattr(submitted, "id", None) if submitted else None
            actions.append(action)
            log_event(
                sell_reason,
                symbol=row["symbol"],
                notional=sell_n,
                status=action["status"],
                cap_value=row.get("cap_value"),
                market_value=row.get("market_value"),
            )
        return actions

    def cleanup_dust_positions(
        self,
        *,
        dry_run: bool = True,
        max_notional: float | None = None,
        max_qty: float | None = None,
        symbols: list[str] | None = None,
    ) -> list[dict]:
        """Close dust positions (default market value < AUTO_DUST_MAX_NOTIONAL, $10)."""
        from modules.dust_cleanup import (
            DEFAULT_DUST_MAX_QTY,
            cleanup_dust_positions,
        )

        threshold = (
            max_notional
            if max_notional is not None
            else config.effective_auto_dust_max_notional()
        )
        results = cleanup_dust_positions(
            self,
            dry_run=dry_run,
            max_notional=threshold,
            max_qty=max_qty if max_qty is not None else DEFAULT_DUST_MAX_QTY,
            symbols=symbols,
        )
        return [r.as_dict() for r in results]

    def enforce_portfolio_guards(self, *, dry_run: bool | None = None) -> dict:
        """Run concentration trim + auto-dust cleaner. Returns a summary dict."""
        use_dry = self.dry_run if dry_run is None else bool(dry_run)
        self.refresh_cache()
        active = self.list_active_tickers()
        concentration = self.trim_concentration_excess(dry_run=use_dry)
        dust: list[dict] = []
        if config.effective_auto_dust_cleaner_enabled():
            dust = self.cleanup_dust_positions(
                dry_run=use_dry,
                max_notional=config.effective_auto_dust_max_notional(),
            )
        summary = {
            "dry_run": use_dry,
            "active_tickers": active,
            "active_count": len(active),
            "max_active_tickers": config.effective_max_active_tickers(),
            "per_name_max_pct": config.effective_per_name_max_pct(),
            "nyse_per_name_max_pct": config.effective_nyse_per_name_max_pct()
            if config.PAPER_TRADING
            else None,
            "auto_dust_max_notional": config.effective_auto_dust_max_notional(),
            "concentration_trims": concentration,
            "dust_actions": dust,
        }
        log_event(
            "portfolio_guards",
            dry_run=use_dry,
            active_count=len(active),
            max_active=config.effective_max_active_tickers(),
            concentration_n=len(concentration),
            dust_n=len(dust),
        )
        return summary

    def execute_exit_with_auto_dust(
        self,
        symbol,
        *,
        reason: str = "exit",
        sleeve=None,
        max_notional: float | None = None,
        max_qty: float = 0.001,
    ):
        """Close tiny positions via dust cleanup; otherwise use a full exit."""
        if not self._equity_trading_allowed(symbol):
            return None
        pos = self._find_position(symbol)
        if pos is None:
            return None

        from modules import dust_cleanup

        threshold = (
            float(max_notional)
            if max_notional is not None
            else config.effective_auto_dust_max_notional()
        )
        qty, notional = dust_cleanup.position_qty_notional(pos)
        if dust_cleanup.is_dust_position(
            qty,
            notional,
            max_notional=threshold,
            max_qty=max_qty,
        ):
            logger.info(
                "auto-dust cleanup triggered",
                extra={
                    "symbol": symbol,
                    "qty": qty,
                    "notional": notional,
                    "max_notional": threshold,
                    "max_qty": max_qty,
                },
            )
            return dust_cleanup.close_dust_position(
                self,
                symbol,
                dry_run=getattr(self, "dry_run", False),
                max_notional=threshold,
                max_qty=max_qty,
            )
        # Avoid re-entering _maybe_dust_exit → this method.
        return self.execute_full_exit(
            symbol, reason=reason, sleeve=sleeve, skip_dust=True
        )


def get_trading_client(paper=None, credentials_fn=None, *, allow_live=None):
    """Return a cached TradingClient (utility scripts)."""
    from modules.alpaca_client import get_trading_client as _get_client

    return _get_client(paper=paper, credentials_fn=credentials_fn, allow_live=allow_live)
