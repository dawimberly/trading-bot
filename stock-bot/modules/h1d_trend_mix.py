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
"""

from __future__ import annotations

import logging
import math
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import config

STRATEGY_ID = "h1d_trend_mix"
SMA_WINDOW = 200
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
# Proposed for this strategy only. Inert while the strategy flag is off, so
# Medium/Live stop defaults stay as they are.
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


def disable_hard_stops() -> bool:
    """Strategy-config proposal. Unset env follows ``DISABLE_HARD_STOPS``."""
    raw = os.getenv("H1D_TREND_MIX_DISABLE_HARD_STOPS")
    if raw is None or not str(raw).strip():
        return bool(DISABLE_HARD_STOPS)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _normalize(symbol: str) -> str:
    return str(symbol or "").replace("/", "-").strip().upper()


def hard_stop_exempt(symbol: str) -> bool:
    """True only when this strategy is on and its config exempts the symbol.

    While ``H1D_TREND_MIX_ENABLED`` is off this is always false, so existing
    5% / smart −10% stops on Medium and Live are unchanged.
    """
    if not config.effective_h1d_trend_mix_enabled():
        return False
    if not disable_hard_stops():
        return False
    return _normalize(symbol) in STRATEGY_SYMBOLS


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
        orders.append(
            {
                "symbol": symbol,
                "side": "buy" if delta > 0 else "sell",
                "notional": notional,
                "target_weight": weight,
                "strategy": STRATEGY_ID,
            }
        )
    orders.sort(key=lambda row: (row["side"] != "sell", row["symbol"]))
    return orders


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


def _positions_ok(positions: dict[str, float] | None) -> dict[str, float] | None:
    if positions is None or not isinstance(positions, dict):
        return None
    cleaned: dict[str, float] = {}
    for symbol, raw in positions.items():
        name = _normalize(symbol)
        if name not in STRATEGY_SYMBOLS:
            continue
        value = _finite(raw)
        if value is None or value < 0:
            return None
        cleaned[name] = value
    return cleaned


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
) -> dict[str, Any]:
    """Pure plan. ``orders`` is empty unless the entry may actually be sent."""
    if state is None:
        return _empty_plan("state_unreadable")
    if not isinstance(session_date, date):
        return _empty_plan("missing_data")
    risk_on = signal_for_session(spy_bars, session_date=session_date)
    if risk_on is None:
        return _empty_plan("missing_data")
    weights = target_weights(risk_on)
    held = _positions_ok(positions)
    account = _finite(equity)
    if held is None or account is None or account <= 0:
        return _empty_plan("missing_data", risk_on=risk_on, weights=weights)
    due = rebalance_reason(risk_on=risk_on, session_date=session_date, state=state)
    preview = (
        plan_rebalance(weights, held, account, dust=dust) if due else []
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
    elif due is None:
        reason = "hold"
    else:
        reason = due
    actionable = reason in _REBALANCE_REASONS
    return {
        "strategy": STRATEGY_ID,
        "reason": reason,
        "risk_on": risk_on,
        "weights": weights,
        "preview_orders": preview if due else [],
        "orders": preview if actionable else [],
        "execute": bool(actionable and preview),
        "persist": bool(actionable),
        "send": False,
        "in_window": bool(fresh),
        "signal": "risk_on" if risk_on else "risk_off",
        "rebalance_month": _month_key(session_date),
    }


def format_plan(plan: dict[str, Any], *, dry_run: bool) -> str:
    tag = "DRY-RUN" if dry_run else "LIVE"
    reason = plan.get("reason") or "unknown"
    weights = plan.get("weights") or {}
    lines = [f"h1d_trend_mix [{tag}] {reason}"]
    book = os.getenv("TRADING_BOOK_ID", "").strip()
    if book:
        lines[0] += f" book={book}"
    if weights:
        ordered = sorted(weights, key=lambda symbol: (-float(weights[symbol]), symbol))
        parts = [f"{symbol}={float(weights[symbol]) * 100:.2f}%" for symbol in ordered]
        total = sum(float(value) for value in weights.values()) * 100.0
        lines.append("  targets " + " ".join(parts) + f" sum={total:.2f}%")
    else:
        lines.append("  targets none")
    shown = plan.get("preview_orders") or plan.get("orders") or []
    if not shown:
        lines.append("  orders: none")
    for order in shown:
        lines.append(
            f"  {str(order['side']).upper()} {order['symbol']} "
            f"notional ${float(order['notional']):.2f}"
        )
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

        frame = load_close_matrix(interval="1d", days=450)
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
    return bars or None


def _broker_positions(executor: Any) -> dict[str, float] | None:
    try:
        rows = executor._get_positions()
    except Exception:
        return None
    positions: dict[str, float] = {}
    for row in rows or []:
        symbol = _normalize(getattr(row, "symbol", ""))
        if symbol not in STRATEGY_SYMBOLS:
            continue
        market_value = getattr(row, "market_value", None)
        if market_value is None:
            qty = _finite(getattr(row, "qty", None))
            price = _finite(getattr(row, "current_price", None))
            if qty is None or price is None:
                return None
            market_value = qty * price
        value = _finite(market_value)
        if value is None:
            return None
        positions[symbol] = abs(value) if value < 0 else value
        if value < 0:
            return None
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
    """Submit sells then buys. False if any sized order is not accepted."""
    broker_min = float(config.effective_min_notional())
    for order in orders:
        notional = float(order["notional"])
        if notional < broker_min:
            order["status"] = "skipped_broker_min"
            continue
        try:
            submitted = executor.execute_order(
                order["symbol"],
                order["side"],
                notional=notional,
                reason=f"{STRATEGY_ID}:{reason}",
                sleeve="H1D",
            )
        except Exception:
            order["status"] = "error"
            return False
        if submitted is None:
            order["status"] = "rejected"
            return False
        order["status"] = "submitted"
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
) -> dict[str, Any]:
    """One cycle. Dry-run prints the plan and does not submit or persist."""
    if enabled is None:
        enabled = config.effective_h1d_trend_mix_enabled()
    if not enabled:
        return _empty_plan("disabled")

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
    if dry_run or plan.get("in_window") or plan.get("send"):
        print(plan["report"])
    else:
        logger.info("h1d_trend_mix %s; no orders", plan.get("reason"))
    return plan


def main() -> None:
    """Preview targets and orders. This entry point never submits.

    Notionals use a flat $300 illustration so the preview does not read an
    account. The book process uses that book's equity when dry-run is on.
    """
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
    )


if __name__ == "__main__":
    main()
