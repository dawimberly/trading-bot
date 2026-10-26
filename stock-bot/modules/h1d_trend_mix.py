"""Opt-in monthly SPY-trend / diversified ETF mix (``h1d_trend_mix``).

Research sleeve, not a proven edge. Off unless ``H1D_TREND_MIX_ENABLED`` is set
on that book's env. Does not change Live or Medium profile defaults.

Rules (frozen):
- 50% trend sleeve: SSO when SPY's prior completed close is above its 200-day
  SMA, otherwise BIL. Signal uses that close; the order is for the next
  regular-session open.
- 50% diversified sleeve: equal weight XLB XLE XLF XLI XLK XLP XLU XLV XLY
  SPY TLT GLD.
- Rebalance both sleeves at the first caught open of a new month, on a trend
  flip, and once when the book has no saved state.
- Skip notionals below the dust threshold. Missing inputs produce no orders.

``H1D_TREND_MIX_EXCLUSIVE`` (default off) replaces the book's other entry
sleeves and winds down positions that are not in the current target.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import config

STRATEGY_ID = "h1d_trend_mix"
SMA_WINDOW = 200
SPY_BAR_LOOKBACK = 450  # real NYSE sessions kept for the SMA
SLEEVE_WEIGHT = 0.5
TREND_SIGNAL = "SPY"
TREND_RISK_ON = "SSO"
TREND_RISK_OFF = "BIL"
DIVERSIFIED_SYMBOLS: tuple[str, ...] = (
    "XLB",
    "XLE",
    "XLF",
    "XLI",
    "XLK",
    "XLP",
    "XLU",
    "XLV",
    "XLY",
    "SPY",
    "TLT",
    "GLD",
)
STRATEGY_SYMBOLS: frozenset[str] = frozenset(
    (*DIVERSIFIED_SYMBOLS, TREND_RISK_ON, TREND_RISK_OFF)
)
# Documented proposal only. Unset ``H1D_TREND_MIX_DISABLE_HARD_STOPS`` follows
# exclusive mode, so stops stay on until a book replaces its other sleeves.
DISABLE_HARD_STOPS = True
ENTRY_FRESHNESS_MINUTES = 7
DEFAULT_DUST_NOTIONAL = 1.0
_REBALANCE_REASONS = frozenset({"initial", "month_start", "trend_flip"})
_ET = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)


def entry_freshness_minutes() -> int:
    raw = os.getenv("H1D_TREND_MIX_ENTRY_FRESHNESS_MIN", str(ENTRY_FRESHNESS_MINUTES))
    try:
        return max(1, int(str(raw).strip()))
    except (TypeError, ValueError):
        return ENTRY_FRESHNESS_MINUTES


def dust_notional() -> float:
    raw = os.getenv("H1D_TREND_MIX_DUST_NOTIONAL", str(DEFAULT_DUST_NOTIONAL))
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_DUST_NOTIONAL
    if not math.isfinite(value) or value < 0:
        return DEFAULT_DUST_NOTIONAL
    return value


def _env_true(name: str) -> bool | None:
    """True/false when the env is set. None when it is missing or blank."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return None
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def disable_hard_stops() -> bool:
    """Skip fixed and smart −10% stops on this strategy's ETFs.

    Explicit ``H1D_TREND_MIX_DISABLE_HARD_STOPS`` wins. When it is unset,
    exemption follows exclusive mode. While the strategy is off this is false.
    """
    if not config.effective_h1d_trend_mix_enabled():
        return False
    explicit = _env_true("H1D_TREND_MIX_DISABLE_HARD_STOPS")
    if explicit is None:
        return bool(config.h1d_trend_mix_exclusive())
    return bool(explicit)


def _normalize(symbol: str) -> str:
    return str(symbol or "").replace("/", "-").strip().upper()


def is_h1d_symbol(symbol: str) -> bool:
    return _normalize(symbol) in STRATEGY_SYMBOLS


def guards_bypass_symbol(symbol: str, *, exclusive: bool) -> bool:
    """Per-name cap and auto-dust skip this ETF only in exclusive mode."""
    return bool(exclusive) and is_h1d_symbol(symbol)


def other_entry_blocked(
    sleeve: str | None,
    side: str,
    has_position: bool,
    exclusive: bool,
) -> bool:
    """Block new entries from sleeves other than H1D.

    Sells of an existing position still pass. H1D orders pass. Buys and
    sell-to-open from every other sleeve are blocked while exclusive is on.
    """
    if not exclusive:
        return False
    if str(sleeve or "").strip().upper() == "H1D":
        return False
    side_l = str(side or "").strip().lower()
    if side_l == "buy":
        return True
    if side_l == "sell" and not has_position:
        return True
    return False


def hard_stop_exempt(symbol: str) -> bool:
    """True when this book's hard stop should skip an H1D ETF.

    Requires the strategy to be on, the symbol to be one of its ETFs, and
    either an explicit disable flag or exclusive mode when the flag is unset.
    """
    if not disable_hard_stops():
        return False
    return is_h1d_symbol(symbol)


def apply_hard_stop_exemption(
    symbol: str,
    *,
    stop_hit: bool,
    atr_stop_hit: bool,
    smart_exit_code: str = "",
) -> tuple[bool, bool]:
    """Drop fixed stops and the smart −10% hard exit for exempt ETFs.

    ATR stops, the −5% smart reeval, trailing stops, and max-hold are left
    to the caller. A failure here must not widen stops, so unknown symbols
    and a disabled strategy return the inputs unchanged.
    """
    if not hard_stop_exempt(symbol):
        return bool(stop_hit), bool(atr_stop_hit)
    code = str(smart_exit_code or "")
    if code == "smart_hard_exit":
        return False, False
    if stop_hit and not atr_stop_hit:
        return False, bool(atr_stop_hit)
    return bool(stop_hit), bool(atr_stop_hit)


def target_weights(risk_on: bool) -> dict[str, float]:
    """Sleeve weights. Sum is 1. Trend symbol is SSO or BIL, never both."""
    count = len(DIVERSIFIED_SYMBOLS)
    each = SLEEVE_WEIGHT / count
    weights = {symbol: each for symbol in DIVERSIFIED_SYMBOLS[:-1]}
    allocated = sum(weights.values())
    weights[DIVERSIFIED_SYMBOLS[-1]] = SLEEVE_WEIGHT - allocated
    weights[TREND_RISK_ON if risk_on else TREND_RISK_OFF] = SLEEVE_WEIGHT
    return weights


def allocate_notionals(weights: dict[str, float], equity: float) -> dict[str, float]:
    """Largest-remainder cents so targets sum to ``equity`` on the cent grid."""
    cents_equity = int(round(float(equity) * 100))
    names = list(weights)
    raw = [float(weights[name]) * cents_equity for name in names]
    floors = [int(math.floor(value)) for value in raw]
    leftover = cents_equity - sum(floors)
    order = sorted(
        range(len(names)),
        key=lambda index: (raw[index] - floors[index], weights[names[index]]),
        reverse=True,
    )
    while leftover > 0 and order:
        for index in order:
            if leftover <= 0:
                break
            floors[index] += 1
            leftover -= 1
    return {names[index]: floors[index] / 100.0 for index in range(len(names))}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def is_nyse_session(day: date) -> bool:
    """True for NYSE trading days (weekday and not an exchange holiday).

    Calendar-padded rows (weekends, holidays, forward-filled synthetic bars)
    must never enter the 200-session SMA.
    """
    if isinstance(day, datetime):
        day = day.date()
    if not isinstance(day, date) or day.weekday() >= 5:
        return False
    try:
        from modules.nyse_calendar import nyse_holidays

        return day not in nyse_holidays(day.year)
    except Exception:
        return True


def real_session_bars(
    bars: list[tuple[date, float]] | None,
) -> list[tuple[date, float]] | None:
    """Keep only NYSE-session bars, newest ``SPY_BAR_LOOKBACK`` sessions."""
    kept = [(day, close) for day, close in (bars or []) if is_nyse_session(day)]
    kept.sort(key=lambda row: row[0])
    return kept[-SPY_BAR_LOOKBACK:] or None


def completed_closes(
    bars: list[tuple[date, float]] | None,
    *,
    session_date: date,
) -> list[float] | None:
    """Closes of sessions strictly before ``session_date``.

    Today's bar is dropped so the open trade cannot see that session's close.
    Non-finite closes are discarded. Fewer than 200 completed closes → None.
    """
    if not bars or not isinstance(session_date, date):
        return None
    by_day: dict[date, float] = {}
    for raw_day, raw_close in bars:
        if isinstance(raw_day, datetime):
            raw_day = raw_day.date()
        if not isinstance(raw_day, date):
            return None
        if raw_day >= session_date:
            continue
        close = _finite(raw_close)
        if close is None or close <= 0:
            continue
        by_day[raw_day] = close
    ordered = [by_day[day] for day in sorted(by_day)]
    if len(ordered) < SMA_WINDOW:
        return None
    return ordered


def prior_close_signal(closes: list[float] | None) -> bool | None:
    """True when the last completed close is above its trailing 200-close SMA.

    Equal or below is false (hold BIL). Too-short or non-finite input is None.
    """
    if not closes or len(closes) < SMA_WINDOW:
        return None
    window = closes[-SMA_WINDOW:]
    if any(_finite(value) is None or float(value) <= 0 for value in window):
        return None
    sma = sum(float(value) for value in window) / SMA_WINDOW
    if sma <= 0:
        return None
    return float(window[-1]) > sma


def signal_for_session(
    bars: list[tuple[date, float]] | None,
    *,
    session_date: date,
) -> bool | None:
    return prior_close_signal(completed_closes(bars, session_date=session_date))


def _month_key(session_date: date) -> str:
    return f"{session_date.year:04d}-{session_date.month:02d}"


def rebalance_reason(
    *,
    risk_on: bool,
    session_date: date,
    state: dict[str, Any],
) -> str | None:
    """Why to rebalance, or None when the book should hold."""
    signal = "risk_on" if risk_on else "risk_off"
    saved = state.get("signal")
    saved_month = state.get("last_rebalance_month")
    if saved not in ("risk_on", "risk_off") or not saved_month:
        return "initial"
    if saved != signal:
        return "trend_flip"
    if str(saved_month) != _month_key(session_date):
        return "month_start"
    return None


def plan_rebalance(
    weights: dict[str, float],
    positions: dict[str, float],
    equity: float,
    *,
    dust: float,
) -> list[dict[str, Any]]:
    """Notional deltas versus targets. Sells first. Dust deltas omitted.

    Positions outside ``STRATEGY_SYMBOLS`` are ignored so this sleeve does not
    liquidate the rest of the book.
    """
    targets = allocate_notionals(weights, equity)
    deltas: list[tuple[str, float, float]] = []
    for symbol, target in targets.items():
        current = float(positions.get(symbol, 0.0))
        deltas.append((symbol, float(weights[symbol]), target - current))
    for symbol, market_value in positions.items():
        if symbol in weights or symbol not in STRATEGY_SYMBOLS:
            continue
        if market_value > 0:
            deltas.append((symbol, 0.0, -float(market_value)))
    orders: list[dict[str, Any]] = []
    for symbol, weight, delta in deltas:
        if abs(delta) < float(dust):
            continue
        notional = round(abs(delta), 2)
        if notional <= 0 or notional < float(dust):
            continue
        current = float(positions.get(symbol, 0.0))
        row: dict[str, Any] = {
            "symbol": symbol,
            "side": "buy" if delta > 0 else "sell",
            "notional": notional,
            "target_weight": weight,
            "strategy": STRATEGY_ID,
        }
        if delta < 0:
            row["position_value"] = current
        orders.append(row)
    orders.sort(key=lambda row: (row["side"] != "sell", row["symbol"]))
    return orders


def wind_down_orders(
    positions: dict[str, float],
    *,
    dust: float,
) -> list[dict[str, Any]]:
    """Sell holdings that are not in this strategy's symbol list.

    Strategy names that dropped out of the target (SSO vs BIL) are sold by
    ``plan_rebalance``. This list is only the rest of the book.
    """
    orders: list[dict[str, Any]] = []
    for symbol in sorted(positions):
        market_value = float(positions[symbol])
        if market_value <= 0:
            continue
        notional = round(market_value, 2)
        if notional <= 0 or notional < float(dust):
            continue
        orders.append(
            {
                "symbol": symbol,
                "side": "sell",
                "notional": notional,
                "target_weight": 0.0,
                "position_value": market_value,
                "strategy": STRATEGY_ID,
                "wind_down": True,
            }
        )
    return orders


def _defer(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deferred: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["status"] = "deferred_below_min"
        deferred.append(item)
    return deferred


def _batch_residuals(
    group: list[dict[str, Any]],
    side: str,
    broker_min: float,
) -> list[dict[str, Any]]:
    """Fold same-side residuals into the largest when their sum clears the min.

    A batched sell is capped at the host position so the order cannot short.
    If the capped total is still under the minimum, the whole group waits.
    """
    if not group:
        return []
    host = max(group, key=lambda row: (float(row["notional"]), row["symbol"]))
    total = round(sum(float(row["notional"]) for row in group), 2)
    if side == "sell":
        cap = _finite(host.get("position_value"))
        if cap is None:
            return _defer(group)
        total = min(total, round(float(cap), 2))
    if total + 1e-9 < float(broker_min):
        return _defer(group)
    batched = dict(host)
    batched["notional"] = total
    batched["status"] = "batched"
    batched["batched_from"] = [
        row["symbol"] for row in group if row["symbol"] != host["symbol"]
    ]
    return [batched]


def apply_broker_min(
    orders: list[dict[str, Any]],
    broker_min: float,
) -> list[dict[str, Any]]:
    """Keep orders the broker will accept. ``broker_min <= 0`` leaves them as-is.

    A sell that closes a name with target weight 0 and is under the minimum
    is marked ``full_exit`` so the dust path can close it. Partial trims of
    names that stay in the target are batched or deferred, never full exits.
    """
    if broker_min <= 0:
        return [dict(row) for row in orders]
    kept: list[dict[str, Any]] = []
    residuals: dict[str, list[dict[str, Any]]] = {"buy": [], "sell": []}
    for order in orders:
        row = dict(order)
        notional = float(row["notional"])
        if row.get("full_exit") or notional + 1e-9 >= float(broker_min):
            kept.append(row)
            continue
        closing = row["side"] == "sell" and float(row.get("target_weight") or 0.0) == 0.0
        if closing:
            row["full_exit"] = True
            row["status"] = "full_exit_below_min"
            kept.append(row)
            continue
        residuals[str(row["side"])].append(row)
    for side, group in residuals.items():
        kept.extend(_batch_residuals(group, side, broker_min))
    kept.sort(key=lambda row: (row["side"] != "sell", row["symbol"]))
    return kept


def _aware(moment: datetime) -> datetime | None:
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        return None
    return moment.astimezone(_ET)


def entry_window_open(
    now_et: datetime,
    session_open: datetime,
    *,
    freshness_min: int,
    decision_at: datetime | None = None,
) -> bool:
    """True only in the first ``freshness_min`` after the regular open.

    A decision timestamp older than the same window is stale. Naive datetimes
    fail closed.
    """
    now = _aware(now_et)
    opened = _aware(session_open)
    if now is None or opened is None:
        return False
    if now < opened:
        return False
    if now - opened > timedelta(minutes=int(freshness_min)):
        return False
    if decision_at is None:
        return True
    decided = _aware(decision_at)
    if decided is None:
        return False
    age = now - decided
    return timedelta(0) <= age <= timedelta(minutes=int(freshness_min))


def _classify_positions(
    positions: dict[str, float] | None,
) -> tuple[dict[str, float], dict[str, float]] | None:
    """Split holdings into strategy ETFs and everything else.

    Any non-finite or negative value fails the cycle, including names this
    sleeve does not trade. Zeros are dropped.
    """
    if positions is None or not isinstance(positions, dict):
        return None
    strategy: dict[str, float] = {}
    others: dict[str, float] = {}
    for symbol, raw in positions.items():
        name = _normalize(symbol)
        if not name:
            return None
        value = _finite(raw)
        if value is None or value < 0:
            return None
        if value == 0:
            continue
        if name in STRATEGY_SYMBOLS:
            strategy[name] = value
        else:
            others[name] = value
    return strategy, others


def _empty_plan(reason: str, **extra: Any) -> dict[str, Any]:
    plan = {
        "strategy": STRATEGY_ID,
        "reason": reason,
        "risk_on": None,
        "weights": {},
        "orders": [],
        "preview_orders": [],
        "execute": False,
        "persist": False,
        "send": False,
        "in_window": False,
    }
    plan.update(extra)
    return plan


def build_plan(
    *,
    spy_bars: list[tuple[date, float]] | None,
    session_date: date,
    now_et: datetime,
    session_open: datetime,
    market_open: bool,
    positions: dict[str, float] | None,
    equity: float | None,
    state: dict[str, Any] | None,
    dust: float = DEFAULT_DUST_NOTIONAL,
    freshness_min: int = ENTRY_FRESHNESS_MINUTES,
    decision_at: datetime | None = None,
    exclusive: bool = False,
    broker_min: float = 0.0,
) -> dict[str, Any]:
    """Pure plan. ``orders`` is empty unless the entry may actually be sent.

    ``preview_orders`` still lists the next open when the window is closed.
    Exclusive mode adds wind-down sells for holdings outside the strategy.
    """
    if state is None:
        return _empty_plan("state_unreadable")
    if not isinstance(session_date, date):
        return _empty_plan("missing_data")
    risk_on = signal_for_session(spy_bars, session_date=session_date)
    if risk_on is None:
        return _empty_plan("missing_data")
    weights = target_weights(risk_on)
    classified = _classify_positions(positions)
    account = _finite(equity)
    if classified is None or account is None or account <= 0:
        return _empty_plan(
            "missing_data",
            risk_on=risk_on,
            weights=weights,
            exclusive=bool(exclusive),
        )
    strategy_held, others = classified
    if not exclusive:
        others = {}
    due = rebalance_reason(risk_on=risk_on, session_date=session_date, state=state)
    preview: list[dict[str, Any]] = []
    if due:
        preview.extend(plan_rebalance(weights, strategy_held, account, dust=dust))
    if exclusive and others:
        preview.extend(wind_down_orders(others, dust=dust))
    preview = apply_broker_min(preview, broker_min)
    wants_wind = bool(exclusive) and any(
        row.get("wind_down") or row.get("full_exit") for row in preview
    )
    fresh = bool(market_open) and entry_window_open(
        now_et,
        session_open,
        freshness_min=freshness_min,
        decision_at=decision_at,
    )
    if not market_open:
        reason = "market_closed"
    elif not fresh:
        reason = "stale_entry"
    elif due is None and wants_wind:
        reason = "wind_down"
    elif due is None:
        reason = "hold"
    else:
        reason = due
    actionable = reason in _REBALANCE_REASONS or reason == "wind_down"
    show_preview = bool(due) or wants_wind
    return {
        "strategy": STRATEGY_ID,
        "reason": reason,
        "risk_on": risk_on,
        "weights": weights,
        "equity": account,
        "exclusive": bool(exclusive),
        "broker_min": float(broker_min),
        "preview_orders": preview if show_preview else [],
        "orders": preview if actionable else [],
        "execute": bool(actionable and preview),
        "persist": reason in _REBALANCE_REASONS,
        "send": False,
        "in_window": bool(fresh),
        "signal": "risk_on" if risk_on else "risk_off",
        "rebalance_month": _month_key(session_date),
    }


def _order_line(order: dict[str, Any]) -> str:
    extra: list[str] = []
    if order.get("wind_down"):
        extra.append("wind-down")
    if order.get("full_exit"):
        extra.append("full-exit")
    status = str(order.get("status") or "")
    if status == "deferred_below_min":
        extra.append("deferred below broker minimum")
    elif status == "batched":
        folded = ",".join(order.get("batched_from") or [])
        extra.append(f"batched {folded}".strip())
    suffix = f" ({'; '.join(extra)})" if extra else ""
    return (
        f"  {str(order['side']).upper()} {order['symbol']} "
        f"notional ${float(order['notional']):.2f}{suffix}"
    )


def format_plan(plan: dict[str, Any], *, dry_run: bool) -> str:
    tag = "DRY-RUN" if dry_run else "LIVE"
    reason = plan.get("reason") or "unknown"
    weights = plan.get("weights") or {}
    lines = [f"h1d_trend_mix [{tag}] {reason}"]
    book = os.getenv("TRADING_BOOK_ID", "").strip()
    if book:
        lines[0] += f" book={book}"
    if plan.get("exclusive"):
        lines.append("  exclusive: other entry sleeves paused")
    if plan.get("preview_only"):
        lines.append("  preview-only: strategy flag is off for this book")
    equity = _finite(plan.get("equity"))
    if equity is not None and equity > 0:
        lines.append(f"  equity ${equity:.2f}")
    if weights:
        ordered = sorted(weights, key=lambda symbol: (-float(weights[symbol]), symbol))
        parts = [f"{symbol}={float(weights[symbol]) * 100:.2f}%" for symbol in ordered]
        total = sum(float(value) for value in weights.values()) * 100.0
        lines.append("  targets " + " ".join(parts) + f" sum={total:.2f}%")
    else:
        lines.append("  targets none")
    shown = list(plan.get("preview_orders") or plan.get("orders") or [])
    if not shown:
        lines.append("  orders: none")
    for order in shown:
        lines.append(_order_line(order))
    broker_min = _finite(plan.get("broker_min"))
    deferred = [
        order["symbol"]
        for order in shown
        if order.get("status") == "deferred_below_min"
    ]
    if deferred and broker_min is not None and broker_min > 0:
        lines.append(
            "  expected drift: a deferred name can sit just under "
            f"${broker_min:.2f} off target"
        )
    if shown and not plan.get("in_window"):
        lines.append("  next open")
    elif shown and plan.get("in_window"):
        lines.append("  this open")
    if dry_run or (shown and not plan.get("send")):
        lines.append("  not sent")
    return "\n".join(lines)


def default_state_path() -> Path:
    override = os.getenv("H1D_TREND_MIX_STATE_FILE", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "data" / "h1d_trend_mix_state.json"


def load_state(path: Path | str) -> dict[str, Any] | None:
    """Missing file → initial state. Corrupt file → None (no trades)."""
    file_path = Path(path)
    if not file_path.is_file():
        return {}
    try:
        import json

        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    signal = payload.get("signal")
    month = payload.get("last_rebalance_month")
    if signal not in (None, "risk_on", "risk_off"):
        return None
    if month is not None and (
        not isinstance(month, str) or len(month) != 7 or month[4] != "-"
    ):
        return None
    return {"signal": signal, "last_rebalance_month": month}


def save_state(path: Path | str, *, signal: str, month: str) -> bool:
    from modules.safe_io import write_json_file

    if signal not in ("risk_on", "risk_off"):
        return False
    return write_json_file(
        path,
        {
            "strategy": STRATEGY_ID,
            "signal": signal,
            "last_rebalance_month": month,
        },
    )


def _session_open_from_client(client: Any, now_et: datetime) -> datetime | None:
    try:
        from modules.scan_schedule import resolve_session_bounds

        opened, _closed = resolve_session_bounds(client, now_et)
    except Exception:
        return None
    return _aware(opened) if isinstance(opened, datetime) else None


def _confirm_market_open(client: Any) -> bool:
    try:
        from modules.market_hours import is_equity_market_open

        return bool(is_equity_market_open(client))
    except Exception:
        return False


def _load_spy_bars() -> list[tuple[date, float]] | None:
    try:
        from modules.data_loader import load_close_matrix

        # Raw SPY rows only: no forward-fill and no calendar padding, so each
        # row is a real bar. Non-sessions are dropped below.
        frame = load_close_matrix(interval="1d", tickers=["SPY"], ffill=False)
    except Exception:
        return None
    if frame is None or getattr(frame, "empty", True) or "SPY" not in getattr(frame, "columns", []):
        return None
    bars: list[tuple[date, float]] = []
    for stamp, raw in frame["SPY"].items():
        close = _finite(raw)
        if close is None or close <= 0:
            continue
        try:
            day = stamp.date() if hasattr(stamp, "date") else None
        except Exception:
            day = None
        if not isinstance(day, date) or isinstance(day, datetime):
            continue
        bars.append((day, close))
    return real_session_bars(bars)


def _broker_positions(executor: Any) -> dict[str, float] | None:
    try:
        rows = executor._get_positions()
    except Exception:
        return None
    positions: dict[str, float] = {}
    for row in rows or []:
        symbol = _normalize(getattr(row, "symbol", ""))
        if not symbol:
            return None
        market_value = getattr(row, "market_value", None)
        if market_value is None:
            qty = _finite(getattr(row, "qty", None))
            price = _finite(getattr(row, "current_price", None))
            if qty is None or price is None:
                return None
            market_value = qty * price
        value = _finite(market_value)
        if value is None or value < 0:
            return None
        if value == 0:
            continue
        positions[symbol] = value
    return positions


def _broker_equity(executor: Any) -> float | None:
    try:
        account = executor._get_account()
        equity = _finite(getattr(account, "equity", None))
    except Exception:
        return None
    if equity is None or equity <= 0:
        return None
    return equity


def _submit_orders(executor: Any, orders: list[dict[str, Any]], reason: str) -> bool:
    """Submit sells then buys. False if any sized order is not accepted.

    Deferred residuals are skipped. A full exit closes the position even when
    the notional is under the broker minimum. If every order is deferred,
    return False so state is not saved and the next open tries again.
    """
    broker_min = float(config.effective_min_notional())
    submitted_any = False
    deferred_any = False
    for order in orders:
        if order.get("status") == "deferred_below_min":
            deferred_any = True
            continue
        label = f"{STRATEGY_ID}:{reason}"
        try:
            if order.get("full_exit"):
                submitted = executor.execute_full_exit(
                    order["symbol"],
                    reason=label,
                    sleeve="H1D",
                )
            else:
                notional = float(order["notional"])
                if notional < broker_min:
                    order["status"] = "skipped_broker_min"
                    continue
                submitted = executor.execute_order(
                    order["symbol"],
                    order["side"],
                    notional=notional,
                    reason=label,
                    sleeve="H1D",
                )
        except Exception:
            order["status"] = "error"
            return False
        if submitted is None:
            order["status"] = "rejected"
            return False
        order["status"] = "submitted"
        submitted_any = True
    if deferred_any and not submitted_any:
        return False
    return True


def run_h1d_trend_mix_cycle(
    executor: Any = None,
    *,
    market_open: bool = False,
    now_et: datetime | None = None,
    session_open: datetime | None = None,
    session_date: date | None = None,
    spy_bars: list[tuple[date, float]] | None = None,
    positions: dict[str, float] | None = None,
    equity: float | None = None,
    state: dict[str, Any] | None = None,
    dry_run: bool | None = None,
    state_path: Path | str | None = None,
    confirm_market: bool = True,
    allow_send: bool = True,
    enabled: bool | None = None,
    exclusive: bool | None = None,
    broker_min: float | None = None,
    announce: bool = True,
) -> dict[str, Any]:
    """One cycle. Dry-run prints the plan and does not submit or persist."""
    if enabled is None:
        enabled = config.effective_h1d_trend_mix_enabled()
    if not enabled:
        return _empty_plan("disabled")

    if exclusive is None:
        exclusive = bool(config.h1d_trend_mix_exclusive())
    if broker_min is None:
        broker_min = float(config.effective_min_notional())
    if dry_run is None:
        dry_run = bool(config.h1d_trend_mix_dry_run()) or bool(
            getattr(executor, "dry_run", False)
        )
    dry_run = bool(dry_run) or not allow_send

    now = now_et or datetime.now(_ET)
    now_aware = _aware(now) if isinstance(now, datetime) else None
    if now_aware is None:
        plan = _empty_plan("stale_entry")
        plan["dry_run"] = dry_run
        plan["report"] = format_plan(plan, dry_run=dry_run)
        if announce:
            print(plan["report"])
        return plan

    client = getattr(executor, "client", None)
    opened = session_open
    if opened is None and client is not None:
        opened = _session_open_from_client(client, now_aware)
    if opened is None:
        opened = now_aware.replace(hour=9, minute=30, second=0, microsecond=0)

    market = bool(market_open)
    if confirm_market:
        if client is None:
            market = False
        else:
            market = market and _confirm_market_open(client)
    if getattr(executor, "equity_session_open", None) is False:
        market = False

    path = Path(state_path) if state_path is not None else default_state_path()
    loaded_state = state if state is not None else load_state(path)
    bars = spy_bars if spy_bars is not None else _load_spy_bars()
    held = positions if positions is not None else (
        _broker_positions(executor) if executor is not None else None
    )
    account = equity if equity is not None else (
        _broker_equity(executor) if executor is not None else None
    )
    day = session_date or now_aware.date()
    plan = build_plan(
        spy_bars=bars,
        session_date=day,
        now_et=now_aware,
        session_open=opened,
        market_open=market,
        positions=held,
        equity=account,
        state=loaded_state,
        dust=dust_notional(),
        freshness_min=entry_freshness_minutes(),
        decision_at=now_aware,
        exclusive=bool(exclusive),
        broker_min=float(broker_min),
    )
    window = entry_window_open(
        now_aware,
        opened,
        freshness_min=entry_freshness_minutes(),
        decision_at=now_aware,
    )
    plan["in_window"] = bool(plan.get("in_window") or (market and window))
    plan["dry_run"] = dry_run
    if dry_run:
        plan["execute"] = False
        plan["send"] = False
    elif plan.get("execute") and allow_send and executor is not None:
        accepted = _submit_orders(executor, list(plan["orders"]), str(plan["reason"]))
        plan["send"] = bool(accepted)
        if not accepted:
            plan["persist"] = False
    else:
        plan["send"] = False
        if plan.get("execute"):
            plan["persist"] = False

    if (
        plan.get("persist")
        and not dry_run
        and plan.get("signal")
        and plan.get("rebalance_month")
    ):
        saved = save_state(
            path,
            signal=str(plan["signal"]),
            month=str(plan["rebalance_month"]),
        )
        plan["persisted"] = bool(saved)
    plan["report"] = format_plan(plan, dry_run=bool(dry_run))
    if announce and (dry_run or plan.get("in_window") or plan.get("send")):
        print(plan["report"])
    elif announce:
        logger.info("h1d_trend_mix %s; no orders", plan.get("reason"))
    return plan


class _ReadOnlyBroker:
    """GET positions and equity. ``execute_*`` cannot submit."""

    def __init__(self, executor: Any) -> None:
        self._executor = executor
        self.client = getattr(executor, "client", None)
        self.dry_run = True
        self.equity_session_open = True

    def _get_positions(self) -> Any:
        return self._executor._get_positions()

    def _get_account(self) -> Any:
        return self._executor._get_account()

    def execute_order(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("dry-run broker cannot submit orders")

    def execute_full_exit(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("dry-run broker cannot submit orders")


def _parse_cli(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m modules.h1d_trend_mix",
        description=(
            "Preview h1d_trend_mix orders for the next open. "
            "This command has no submit path."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read positions and print the next-open orders. Required with --book.",
    )
    parser.add_argument(
        "--book",
        default="",
        help="Book id: alpaca_live, alpaca_paper_v2, or alpaca_paper.",
    )
    parser.add_argument(
        "--user",
        default="",
        help="Portal username whose book env should be read.",
    )
    parser.add_argument(
        "--env-file",
        default="",
        help="Book .env to read. Used instead of --user when set.",
    )
    parser.add_argument(
        "--exclusive",
        action="store_true",
        help="Also list wind-down sells for holdings outside the target mix.",
    )
    return parser.parse_args(argv)


def next_open_session_date(now_et: datetime) -> date:
    """Weekday session this preview is planning.

    Before 16:00 ET on a weekday, today's close is still open, so that close
    is left out of the signal. After the cash close, the next weekday is the
    trade session and today's close counts. Exchange holidays are not skipped.
    """
    now = _aware(now_et) or datetime.now(_ET)
    close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now.weekday() < 5 and now < close:
        return now.date()
    day = now.date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _book_env_path(book: str, user: str, env_file: str) -> Path | None:
    if env_file:
        return Path(env_file)
    if not user:
        return None
    from modules.portal_paths import book_env_path

    return book_env_path(user, book)


def _load_book_env(path: Path, book: str) -> str | None:
    """Load a book env for reads. Returns an error string, or None."""
    if not path.is_file():
        return f"book env not found for {book}"
    try:
        from dotenv import load_dotenv

        load_dotenv(path, override=True)
    except Exception as exc:
        return f"could not read book env ({type(exc).__name__})"
    os.environ["TRADING_BOOK_ID"] = book
    os.environ["PYTHONTRADING_ENV_FILE"] = str(path)
    return None


def _read_only_for_book(book: str) -> _ReadOnlyBroker:
    from modules.alpaca_executor import AlpacaExecutor
    from modules.trading_books import BOOKS

    spec = BOOKS.get(book) or {}
    if spec.get("platform") != "alpaca":
        raise ValueError(f"unknown book {book}")
    paper = bool(spec.get("default_paper", True))
    allow_live = bool(spec.get("allow_live_default", False))
    raw = AlpacaExecutor(paper=paper, allow_live=allow_live)
    raw.dry_run = True
    return _ReadOnlyBroker(raw)


def _print_read_failure(book: str, detail: str) -> None:
    print(f"h1d_trend_mix dry-run book={book}: {detail}")
    print("  orders: none")
    print("  not sent")


def _illustrate() -> None:
    """Flat $300 preview. Does not read an account and cannot submit."""
    print("h1d_trend_mix CLI is dry-run only and does not submit orders.")
    print("Notionals use a flat $300 illustration, not a live account.")
    run_h1d_trend_mix_cycle(
        dry_run=True,
        allow_send=False,
        confirm_market=False,
        enabled=True,
        market_open=True,
        positions={},
        equity=300.0,
        state={},
        exclusive=False,
        broker_min=0.0,
    )


def main(argv: list[str] | None = None) -> int:
    """Preview the next open. There is no flag that submits orders."""
    args = _parse_cli(argv)
    book = str(args.book or "").strip()
    if book and not args.dry_run:
        print("Pass --dry-run. This command has no submit path.")
        return 2
    if not book:
        _illustrate()
        return 0

    path = _book_env_path(book, str(args.user or "").strip(), str(args.env_file or "").strip())
    if path is None:
        _print_read_failure(book, "pass --user or --env-file")
        return 0
    loaded = _load_book_env(path, book)
    if loaded:
        _print_read_failure(book, loaded)
        return 0

    env_enabled = bool(config.effective_h1d_trend_mix_enabled())
    exclusive = bool(args.exclusive) or bool(config.h1d_trend_mix_exclusive())
    try:
        broker = _read_only_for_book(book)
        positions = _broker_positions(broker)
        equity = _broker_equity(broker)
        market = _confirm_market_open(broker.client)
    except Exception as exc:
        _print_read_failure(book, f"read failed ({type(exc).__name__})")
        return 0
    if positions is None or equity is None:
        _print_read_failure(book, "positions or equity unavailable")
        return 0

    now = datetime.now(_ET)
    opened = None
    if broker.client is not None:
        opened = _session_open_from_client(broker.client, now)
    plan = run_h1d_trend_mix_cycle(
        broker,
        market_open=bool(market),
        now_et=now,
        session_open=opened,
        session_date=next_open_session_date(now),
        positions=positions,
        equity=equity,
        dry_run=True,
        allow_send=False,
        confirm_market=False,
        enabled=True,
        exclusive=exclusive,
        announce=False,
    )
    plan["preview_only"] = not env_enabled
    plan["exclusive"] = exclusive
    plan["report"] = format_plan(plan, dry_run=True)
    print(plan["report"])
    close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now.weekday() < 5 and now < close and not plan.get("in_window"):
        print("  signal uses completed closes only; today's close is not included yet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
