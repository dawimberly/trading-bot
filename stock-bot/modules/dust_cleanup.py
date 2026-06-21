"""Close fractional dust positions via Alpaca close_position (qty-safe, not notional)."""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

import config
from modules.alpaca_client import AlpacaCriticalError, call_with_retry, is_transient_alpaca_error
from modules.logging_utils import log_event

if TYPE_CHECKING:
    from modules.alpaca_executor import AlpacaExecutor

logger = logging.getLogger(__name__)

# Treat as dust when notional OR qty is below these thresholds (either triggers cleanup).
DEFAULT_DUST_MAX_NOTIONAL = float(
    __import__("os").getenv("DUST_MAX_NOTIONAL", "1")
)
DEFAULT_DUST_MAX_QTY = float(__import__("os").getenv("DUST_MAX_QTY", "0.001"))
CLOSE_RETRIES = int(__import__("os").getenv("DUST_CLOSE_RETRIES", "3"))


@dataclass
class DustCloseResult:
    symbol: str
    qty: float
    notional: float
    status: str  # dry_run | closed | skipped | error
    detail: str
    order_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def position_qty_notional(pos) -> tuple[float, float]:
    """Return (signed_qty, abs_notional_usd) for an Alpaca position."""
    qty = float(getattr(pos, "qty", 0) or 0)
    mv = getattr(pos, "market_value", None)
    if mv is not None:
        try:
            notional = abs(float(mv))
            if notional > 0:
                return qty, notional
        except (TypeError, ValueError):
            pass
    price = float(getattr(pos, "current_price", None) or getattr(pos, "avg_entry_price", 0) or 0)
    return qty, abs(qty * price)


def is_dust_position(
    qty: float,
    notional: float,
    *,
    max_notional: float = DEFAULT_DUST_MAX_NOTIONAL,
    max_qty: float = DEFAULT_DUST_MAX_QTY,
) -> bool:
    """Dust if market value < max_notional OR abs(qty) < max_qty."""
    if notional > 0 and notional < max_notional:
        return True
    if abs(qty) > 0 and abs(qty) < max_qty:
        return True
    return False


def list_dust_positions(
    executor: AlpacaExecutor,
    *,
    max_notional: float = DEFAULT_DUST_MAX_NOTIONAL,
    max_qty: float = DEFAULT_DUST_MAX_QTY,
) -> list[tuple[Any, float, float]]:
    """Return [(position, qty, notional), ...] for dust holdings only."""
    executor.refresh_cache()
    out: list[tuple[Any, float, float]] = []
    for pos in executor._get_positions():
        qty, notional = position_qty_notional(pos)
        if qty == 0:
            continue
        if is_dust_position(qty, notional, max_notional=max_notional, max_qty=max_qty):
            sym = executor._normalize_pos_symbol(pos)
            out.append((pos, qty, notional))
            logger.debug(
                "dust candidate symbol=%s qty=%s notional=%s",
                sym,
                qty,
                round(notional, 6),
            )
    return out


def _is_skippable_close_error(exc: BaseException) -> bool:
    if isinstance(exc, APIError):
        status = getattr(exc, "status_code", None)
        msg = str(exc).lower()
        if status in (403, 422):
            return True
        if "insufficient" in msg or "notional must be" in msg or "does not exist" in msg:
            return True
    text = str(exc).lower()
    return "does not exist" in text or "position not found" in text


def _close_via_qty_order(executor: AlpacaExecutor, pos, qty: float) -> Any:
    """Fallback: qty-based market order (works when notional orders are rejected)."""
    symbol = executor._normalize_pos_symbol(pos)
    formatted_symbol, tif, is_crypto_sym = executor.get_order_params(symbol)
    executor._cancel_open_orders_for(symbol)
    abs_qty = abs(qty)
    if abs_qty <= 0:
        raise ValueError(f"zero qty for {symbol}")

    if qty > 0:
        side = OrderSide.SELL
    else:
        side = OrderSide.BUY

    order = MarketOrderRequest(
        symbol=formatted_symbol,
        qty=abs_qty if is_crypto_sym else str(abs_qty),
        side=side,
        time_in_force=tif,
    )
    submitted = call_with_retry(
        executor.client.submit_order,
        order_data=order,
        op_name="dust_qty_close",
        max_attempts=CLOSE_RETRIES,
    )
    executor._invalidate_cache()
    return submitted


def close_dust_position(
    executor: AlpacaExecutor,
    symbol: str,
    *,
    dry_run: bool = True,
    max_notional: float = DEFAULT_DUST_MAX_NOTIONAL,
    max_qty: float = DEFAULT_DUST_MAX_QTY,
) -> DustCloseResult:
    """Close one dust position if present. Idempotent — skips when gone or not dust."""
    sym = config.normalize_symbol(symbol)
    executor.refresh_cache()
    pos = executor._find_position(sym)
    if pos is None:
        return DustCloseResult(sym, 0.0, 0.0, "skipped", "no open position")

    qty, notional = position_qty_notional(pos)
    if not is_dust_position(qty, notional, max_notional=max_notional, max_qty=max_qty):
        return DustCloseResult(
            sym,
            qty,
            round(notional, 4),
            "skipped",
            f"not dust (qty={qty}, notional=${notional:.4f})",
        )

    if dry_run:
        return DustCloseResult(
            sym,
            qty,
            round(notional, 4),
            "dry_run",
            "would close full position",
        )

    alpaca_sym = getattr(pos, "symbol", sym)
    last_err: str | None = None

    for attempt in range(1, CLOSE_RETRIES + 1):
        try:
            executor._cancel_open_orders_for(sym)
            order = call_with_retry(
                executor.client.close_position,
                alpaca_sym,
                op_name="close_position",
                max_attempts=1,
            )
            executor._invalidate_cache()
            oid = str(getattr(order, "id", "") or "")
            log_event(
                "dust_close",
                symbol=sym,
                qty=qty,
                notional=round(notional, 4),
                order_id=oid,
                method="close_position",
            )
            return DustCloseResult(
                sym,
                qty,
                round(notional, 4),
                "closed",
                "close_position",
                order_id=oid or None,
            )
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            if _is_skippable_close_error(exc) and "does not exist" in last_err.lower():
                executor._invalidate_cache()
                if executor._find_position(sym) is None:
                    return DustCloseResult(sym, qty, round(notional, 4), "skipped", "already closed")
            logger.warning(
                "close_position failed for %s (attempt %s/%s): %s",
                sym,
                attempt,
                CLOSE_RETRIES,
                exc,
            )
            if attempt < CLOSE_RETRIES and (
                isinstance(exc, APIError) and is_transient_alpaca_error(exc)
            ):
                time.sleep(0.5 * attempt)
                continue
            break

    try:
        order = _close_via_qty_order(executor, pos, qty)
        oid = str(getattr(order, "id", "") or "")
        log_event(
            "dust_close",
            symbol=sym,
            qty=qty,
            notional=round(notional, 4),
            order_id=oid,
            method="qty_market",
        )
        return DustCloseResult(
            sym,
            qty,
            round(notional, 4),
            "closed",
            "qty market order",
            order_id=oid or None,
        )
    except Exception as exc:  # noqa: BLE001
        detail = last_err or str(exc)
        if _is_skippable_close_error(exc):
            executor.refresh_cache()
            if executor._find_position(sym) is None:
                return DustCloseResult(sym, qty, round(notional, 4), "skipped", "already closed")
        logger.error("dust close failed for %s: %s", sym, detail)
        log_event("dust_close_error", symbol=sym, error=detail)
        return DustCloseResult(sym, qty, round(notional, 4), "error", detail)


def cleanup_dust_positions(
    executor: AlpacaExecutor,
    *,
    dry_run: bool = True,
    max_notional: float = DEFAULT_DUST_MAX_NOTIONAL,
    max_qty: float = DEFAULT_DUST_MAX_QTY,
    symbols: list[str] | None = None,
) -> list[DustCloseResult]:
    """Scan all holdings and close dust. Safe to re-run (idempotent)."""
    if symbols:
        return [
            close_dust_position(
                executor,
                sym,
                dry_run=dry_run,
                max_notional=max_notional,
                max_qty=max_qty,
            )
            for sym in symbols
        ]

    dust = list_dust_positions(
        executor, max_notional=max_notional, max_qty=max_qty
    )
    results: list[DustCloseResult] = []
    for pos, qty, notional in dust:
        sym = executor._normalize_pos_symbol(pos)
        if dry_run:
            results.append(
                DustCloseResult(
                    sym,
                    qty,
                    round(notional, 4),
                    "dry_run",
                    "would close full position",
                )
            )
            continue
        results.append(
            close_dust_position(
                executor,
                sym,
                dry_run=False,
                max_notional=max_notional,
                max_qty=max_qty,
            )
        )
        time.sleep(0.25)
    return results


def format_cleanup_report(
    results: list[DustCloseResult],
    *,
    paper: bool,
    dry_run: bool,
) -> str:
    mode = "PAPER" if paper else "LIVE"
    header = f"Dust cleanup ({mode}) — {'DRY RUN' if dry_run else 'EXECUTED'}"
    if not results:
        return header + "\n  (no dust positions found)"
    lines = [header]
    for r in results:
        lines.append(
            f"  {r.symbol:12} qty={r.qty:>14.8g}  ${r.notional:>8.4f}  "
            f"{r.status:8}  {r.detail}"
            + (f"  order={r.order_id}" if r.order_id else "")
        )
    closed = sum(1 for r in results if r.status == "closed")
    skipped = sum(1 for r in results if r.status == "skipped")
    errors = sum(1 for r in results if r.status == "error")
    dry = sum(1 for r in results if r.status == "dry_run")
    lines.append(
        f"Summary: {len(results)} row(s) — "
        f"closed={closed} skipped={skipped} errors={errors} dry_run={dry}"
    )
    return "\n".join(lines)
