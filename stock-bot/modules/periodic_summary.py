"""Periodic (default 3h) Telegram account summary — compact, non-spammy."""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import config
from modules import alerts

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_CT = ZoneInfo("America/Chicago")
_ROOT = Path(__file__).resolve().parents[1]
_SUMMARY_TS_PATH = _ROOT / "data" / "last_summary_timestamp.json"
# Paper 3h pulse: Mon–Fri [09:30, 15:00) America/Chicago. 15:00 is session close.
_PERIODIC_WINDOW_START_MIN = 9 * 60 + 30
_PERIODIC_WINDOW_END_MIN = 15 * 60


def _load_summary_ts() -> dict[str, Any]:
    if not _SUMMARY_TS_PATH.is_file():
        return {}
    try:
        raw = json.loads(_SUMMARY_TS_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _save_summary_ts(payload: dict[str, Any]) -> None:
    try:
        _SUMMARY_TS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SUMMARY_TS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("periodic summary timestamp write failed: %s", exc)


def in_periodic_summary_window(now: datetime | None = None) -> bool:
    """True only Mon–Fri while local Chicago time is in [09:30, 15:00).

    After 15:00 CT, before 09:30 CT, and weekends: do not send and do not
    start a 3h cycle. Weekly / error / fill alerts use other jobs.
    """
    t = now or datetime.now(_CT)
    if t.tzinfo is None:
        t = t.replace(tzinfo=_CT)
    else:
        t = t.astimezone(_CT)
    if t.weekday() >= 5:
        return False
    minutes = t.hour * 60 + t.minute
    return _PERIODIC_WINDOW_START_MIN <= minutes < _PERIODIC_WINDOW_END_MIN


def periodic_summary_due(interval_hours: float = 3.0, *, now: datetime | None = None) -> bool:
    """True when the cash-session window is open and the 3h interval has elapsed."""
    if not getattr(config, "TELEGRAM_ALERT_PERIODIC_SUMMARY", True):
        return False
    if not in_periodic_summary_window(now):
        return False
    hours = float(
        interval_hours
        if interval_hours is not None
        else getattr(config, "TELEGRAM_PERIODIC_SUMMARY_HOURS", 3.0)
    )
    hours = max(0.25, hours)
    cache = _load_summary_ts()
    raw = cache.get("last_sent_at")
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=_ET)
        clock = now or datetime.now(_ET)
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=_ET)
        else:
            clock = clock.astimezone(_ET)
        return clock - last.astimezone(_ET) >= timedelta(hours=hours)
    except (TypeError, ValueError):
        return True


def _equity_series(*, paper_chase: bool) -> list[tuple[datetime, float]]:
    try:
        from modules.status_metrics import _merge_journal_series

        return _merge_journal_series(paper_chase=paper_chase, live_only=not paper_chase)
    except Exception as exc:
        logger.debug("equity series unavailable: %s", exc)
        return []


def _max_drawdown_pct(equities: list[float]) -> float | None:
    if len(equities) < 2:
        return None
    peak = equities[0]
    max_dd = 0.0
    for eq in equities:
        if eq > peak:
            peak = eq
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)
    return round(max_dd * 100.0, 2)


def _sharpe_from_equity(equities: list[float], *, periods_per_year: float = 252.0) -> float | None:
    if len(equities) < 5:
        return None
    rets: list[float] = []
    for i in range(1, len(equities)):
        prev = equities[i - 1]
        if prev > 0:
            rets.append(equities[i] / prev - 1.0)
    if len(rets) < 4:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    if std < 1e-12:
        return 0.0
    return round((mean / std) * math.sqrt(periods_per_year), 2)


def _equity_marks_for_day(
    series: list[tuple[datetime, float]],
    *,
    day: date | None = None,
) -> list[float]:
    """Equity marks on an ET calendar day (prepend last prior mark as session open)."""
    target = day or datetime.now(_ET).date()
    prior: float | None = None
    todays: list[float] = []
    for ts, eq in series:
        t = ts
        if t.tzinfo is None:
            t = t.replace(tzinfo=_ET)
        else:
            t = t.astimezone(_ET)
        if t.date() < target:
            prior = float(eq)
        elif t.date() == target:
            todays.append(float(eq))
    if not todays:
        return []
    if prior is not None:
        return [prior, *todays]
    return todays


def _active_journal_path() -> Path | None:
    """Journal for the running book (portal bots set PAPER_JOURNAL_CSV)."""
    raw = Path(str(getattr(config, "PAPER_JOURNAL_CSV", None) or "paper_journal.csv"))
    path = raw if raw.is_absolute() else (_ROOT / raw)
    return path if path.is_file() else None


def _book_equity_series(*, paper_chase: bool | None = None) -> list[tuple[datetime, float]]:
    """Equity marks from the active book journal only (avoids stale merge filters)."""
    from modules.status_metrics import _read_equity_journal

    path = _active_journal_path()
    if path is not None:
        rows = _read_equity_journal(path)
        if rows:
            return rows
    paper = (
        bool(config.PAPER_TRADING or config.paper_chase_mode_enabled())
        if paper_chase is None
        else bool(paper_chase)
    )
    return _equity_series(paper_chase=paper)


def daily_sharpe_from_series(
    series: list[tuple[datetime, float]] | None = None,
    *,
    paper_chase: bool | None = None,
) -> float | None:
    """Session Sharpe from today's equity curve (annualized from intraday bars)."""
    if series is None:
        series = _book_equity_series(paper_chase=paper_chase)
    eqs = _equity_marks_for_day(series or [])
    if len(eqs) < 3:
        return None
    rets: list[float] = []
    for i in range(1, len(eqs)):
        prev = eqs[i - 1]
        if prev > 0:
            rets.append(eqs[i] / prev - 1.0)
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    if std < 1e-12:
        return 0.0
    # Annualize treating today's bar count as one full session.
    return round((mean / std) * math.sqrt(252.0 * len(rets)), 2)


def _closed_trades_fifo(fills_df) -> list[dict[str, Any]]:
    """FIFO-match buy/sell fills into closed trades with realized P&L."""
    from collections import defaultdict, deque

    import pandas as pd

    if fills_df is None or getattr(fills_df, "empty", True):
        return []
    work = fills_df.copy()
    if "timestamp" not in work.columns:
        return []
    work["timestamp"] = pd.to_datetime(work["timestamp"], errors="coerce")
    work = work.dropna(subset=["timestamp"]).sort_values("timestamp")

    longs: dict[str, deque] = defaultdict(deque)
    closed: list[dict[str, Any]] = []

    for _, row in work.iterrows():
        sym = config.normalize_symbol(str(row.get("symbol") or row.get("ticker") or ""))
        side = str(row.get("side") or "").lower()
        event = str(row.get("event") or "").lower()
        if event in ("exit", "sell", "close") and not side:
            side = "sell"
        if event in ("entry", "buy", "fill", "signal", "game_plan") and not side:
            side = "buy"
        if event in ("signal",) and side not in ("buy", "sell"):
            continue
        if not sym or side not in ("buy", "sell"):
            continue
        try:
            price = float(row.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        try:
            qty = float(row.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        try:
            notional = float(row.get("notional") or 0)
        except (TypeError, ValueError):
            notional = 0.0
        if qty <= 0 and price > 0 and abs(notional) > 0:
            qty = abs(notional) / price
        if price <= 0 and qty > 0 and abs(notional) > 0:
            price = abs(notional) / qty
        if price <= 0 or qty <= 0:
            continue
        ts = row["timestamp"]
        if side == "buy":
            longs[sym].append({"price": price, "qty": qty, "ts": ts})
            continue
        remaining = qty
        while remaining > 1e-9 and longs[sym]:
            lot = longs[sym][0]
            match = min(remaining, float(lot["qty"]))
            entry_px = float(lot["price"])
            pnl = (price - entry_px) * match
            pnl_pct = 100.0 * (pnl / (entry_px * match)) if entry_px > 0 and match > 0 else 0.0
            closed.append(
                {
                    "symbol": sym,
                    "qty": match,
                    "entry": entry_px,
                    "exit": price,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "sold_at": ts,
                }
            )
            lot["qty"] = float(lot["qty"]) - match
            remaining -= match
            if float(lot["qty"]) <= 1e-9:
                longs[sym].popleft()
    return closed


def _alpaca_fills_df(*, paper: bool, limit: int = 200):
    """Closed Alpaca orders as a fill frame (qty/price present)."""
    import pandas as pd

    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        from modules.alpaca_client import get_trading_client

        client = get_trading_client(paper=paper)
        req = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            limit=max(int(limit), 80),
            nested=True,
        )
        orders = list(client.get_orders(filter=req))
    except Exception as exc:
        logger.debug("alpaca fills for daily summary unavailable: %s", exc)
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for order in orders:
        qty = float(getattr(order, "filled_qty", None) or 0)
        avg = getattr(order, "filled_avg_price", None)
        if qty <= 0 or avg is None:
            continue
        filled_at = getattr(order, "filled_at", None) or getattr(order, "submitted_at", None)
        try:
            price = float(avg)
        except (TypeError, ValueError):
            continue
        sym = config.normalize_symbol(getattr(order, "symbol", "") or "")
        if not sym:
            continue
        side = str(getattr(order, "side", "") or "").split(".")[-1].lower()
        rows.append(
            {
                "timestamp": filled_at,
                "event": "fill",
                "symbol": sym,
                "side": side,
                "notional": round(qty * price, 2),
                "qty": qty,
                "price": price,
            }
        )
    return pd.DataFrame(rows)


def top_trades_today(
    *,
    limit: int = 3,
    paper_chase: bool | None = None,
    day: date | None = None,
) -> list[dict[str, Any]]:
    """Best closed trades by realized $ P&L that exited today (ET)."""
    import pandas as pd

    target = day or datetime.now(_ET).date()
    paper = (
        bool(config.PAPER_TRADING or config.paper_chase_mode_enabled())
        if paper_chase is None
        else bool(paper_chase)
    )

    closed: list[dict[str, Any]] = []
    fills = _alpaca_fills_df(paper=paper, limit=250)
    if fills is not None and not getattr(fills, "empty", True):
        closed = _closed_trades_fifo(fills)

    if not closed:
        try:
            from modules.paper_journal import read_journal

            path = _active_journal_path()
            df = read_journal(path=path, tail=8000) if path else read_journal(tail=8000)
        except Exception as exc:
            logger.debug("top trades journal read failed: %s", exc)
            df = None
        if df is not None and not getattr(df, "empty", True):
            if "event" in df.columns:
                match_events = {"fill", "exit", "sell", "close", "entry", "buy"}
                df = df.loc[df["event"].astype(str).str.lower().isin(match_events)].copy()
            closed = _closed_trades_fifo(df)

    todays: list[dict[str, Any]] = []
    for trade in closed:
        sold = trade.get("sold_at")
        if sold is None or (isinstance(sold, float) and pd.isna(sold)):
            continue
        try:
            ts = pd.Timestamp(sold)
            if ts.tzinfo is None:
                ts = ts.tz_localize(_ET)
            else:
                ts = ts.tz_convert(_ET)
        except Exception:
            continue
        if ts.date() != target:
            continue
        todays.append(trade)
    todays.sort(key=lambda t: float(t.get("pnl") or 0.0), reverse=True)
    return todays[: max(1, int(limit))]


def format_daily_sharpe_and_trades_block(
    *,
    paper_chase: bool | None = None,
    limit: int = 3,
) -> str:
    """Telegram block: Daily Sharpe + top N trades (no log paths)."""
    paper = (
        bool(config.PAPER_TRADING or config.paper_chase_mode_enabled())
        if paper_chase is None
        else bool(paper_chase)
    )
    series = _book_equity_series(paper_chase=paper)
    sharpe = daily_sharpe_from_series(series, paper_chase=paper)
    trades = top_trades_today(limit=limit, paper_chase=paper)
    lines = [
        f"Daily Sharpe: {sharpe if sharpe is not None else 'n/a'}",
        "",
        f"Top {limit} trades today",
    ]
    if not trades:
        lines.append("  (none closed yet)")
    else:
        for i, t in enumerate(trades, 1):
            sym = t.get("symbol") or "?"
            pnl = float(t.get("pnl") or 0.0)
            pnl_pct = t.get("pnl_pct")
            pct_s = f" ({float(pnl_pct):+.1f}%)" if pnl_pct is not None else ""
            qty = t.get("qty")
            qty_s = f" x{float(qty):.4g}" if qty is not None else ""
            lines.append(f"  {i}. {sym}{qty_s}: ${pnl:+,.2f}{pct_s}")
    return "\n".join(lines)


def _slice_last_days(
    series: list[tuple[datetime, float]], days: int
) -> list[tuple[datetime, float]]:
    if not series:
        return []
    cutoff = datetime.now(_ET) - timedelta(days=days)
    out: list[tuple[datetime, float]] = []
    for ts, eq in series:
        t = ts
        if t.tzinfo is None:
            t = t.replace(tzinfo=_ET)
        else:
            t = t.astimezone(_ET)
        if t >= cutoff:
            out.append((ts, eq))
    return out or series[-min(len(series), 40) :]


def _trade_stats() -> dict[str, Any]:
    """Aggregate all-time trade count / win rate from strategy performance DB."""
    try:
        from modules.strategy_performance import get_strategy_ratings

        ratings = get_strategy_ratings(days=30)
        all_time = ratings.get("all_time") or {}
        trades = 0
        wins_weight = 0.0
        for sid, m in all_time.items():
            if not isinstance(m, dict):
                continue
            n = int(m.get("trade_count") or 0)
            if n <= 0:
                continue
            trades += n
            wins_weight += (float(m.get("win_rate_pct") or 0.0) / 100.0) * n
        win_rate = round(100.0 * wins_weight / trades, 1) if trades else None
        return {"total_trades": trades, "win_rate_pct": win_rate}
    except Exception as exc:
        logger.debug("trade stats unavailable: %s", exc)
        return {"total_trades": 0, "win_rate_pct": None}


def _top_signal_lines(*, limit: int = 3) -> list[str]:
    lines: list[str] = []
    try:
        from modules.insider_monitor import format_telegram_insider_lines

        ins = format_telegram_insider_lines(limit=limit)
        seen: set[str] = set()
        if ins:
            for line in ins:
                if line.lower().startswith("insider"):
                    continue
                text = line.strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                lines.append(text)
    except Exception as exc:
        logger.debug("insider signals for summary skipped: %s", exc)

    try:
        from modules.pipeline_strategies import load_pipeline_data
        from modules.volume_analysis import get_orb_signals

        data = load_pipeline_data(interval="1d")
        if data is not None and not getattr(data, "empty", True):
            orbs = get_orb_signals(
                data, minutes=int(config.ORB_BREAKOUT_MINUTES), limit=limit
            )
            for row in orbs[:limit]:
                sym = row.get("symbol") or "?"
                side = str(row.get("type") or "breakout").replace("_", " ")
                rvol = row.get("rvol")
                rvol_s = f" rvol={rvol:.1f}" if isinstance(rvol, (int, float)) else ""
                lines.append(f"ORB {side}: {sym}{rvol_s}")
    except Exception as exc:
        logger.debug("ORB signals for summary skipped: %s", exc)

    return lines[: max(limit, 3)]


def _bot_health_line(*, regime: str) -> str:
    try:
        from modules.bot_health import (
            calculate_health_score,
            format_health_telegram,
            gather_health_context,
        )

        ctx = gather_health_context({"regime": regime})
        health = calculate_health_score(**ctx)
        return format_health_telegram(health)
    except Exception as exc:
        logger.debug("bot health for summary skipped: %s", exc)
        return "Bot health: n/a"


def gather_periodic_summary(
    *,
    equity: float | None = None,
    cash: float | None = None,
    regime: str = "",
    sleeves: dict | None = None,
) -> dict[str, Any]:
    paper = bool(config.PAPER_TRADING or config.paper_chase_mode_enabled())
    series = _book_equity_series(paper_chase=paper)
    eqs = [eq for _, eq in series]
    day_eqs = _equity_marks_for_day(series)
    roll_series = _slice_last_days(series, 30)
    roll_eqs = [eq for _, eq in roll_series]

    eq_now = float(equity) if equity is not None else (eqs[-1] if eqs else 0.0)
    cash_now = float(cash) if cash is not None else 0.0
    cash_pct = (cash_now / eq_now) if eq_now > 0 else None
    invested_pct = (1.0 - cash_pct) if cash_pct is not None else None

    day_start = day_eqs[0] if day_eqs else None
    day_pnl = (eq_now - day_start) if day_start is not None else None
    day_ret = (
        100.0 * (eq_now / day_start - 1.0) if day_start and day_start > 0 else None
    )

    all_start = eqs[0] if eqs else None
    all_ret = (
        100.0 * (eq_now / all_start - 1.0) if all_start and all_start > 0 else None
    )

    active_exposure = None
    if sleeves:
        try:
            spy = float(sleeves.get("spy_value") or 0)
            nyse = float(sleeves.get("nyse_value") or 0)
            crypto = float(sleeves.get("crypto_value") or 0)
            active_exposure = spy + nyse + crypto
        except (TypeError, ValueError):
            active_exposure = None

    form_pnl: dict[str, Any] = {}
    try:
        from modules.pnl_baseline import performance_pnl

        form_pnl = performance_pnl(eq_now, paper=paper)
    except Exception as exc:
        logger.debug("form performance pnl skipped: %s", exc)
        form_pnl = {}

    trades = _trade_stats()
    return {
        "as_of": datetime.now(_ET),
        "equity": eq_now,
        "cash": cash_now,
        "cash_pct": cash_pct,
        "invested_pct": invested_pct,
        "day_pnl": day_pnl,
        "day_return_pct": day_ret,
        "day_sharpe_30d": _sharpe_from_equity(roll_eqs),
        "day_max_dd_pct": _max_drawdown_pct(roll_eqs),
        "form_pnl": form_pnl.get("pnl"),
        "form_return_pct": form_pnl.get("return_pct"),
        "form_label": form_pnl.get("label") or "Since form",
        "form_start_date": form_pnl.get("start_date"),
        "form_baseline_equity": form_pnl.get("baseline_equity"),
        "all_return_pct": all_ret,
        "all_sharpe": _sharpe_from_equity(eqs),
        "all_max_dd_pct": _max_drawdown_pct(eqs),
        "win_rate_pct": trades.get("win_rate_pct"),
        "total_trades": trades.get("total_trades") or 0,
        "active_exposure": active_exposure,
        "regime": regime or "n/a",
        "health_line": _bot_health_line(regime=regime or ""),
        "signals": _top_signal_lines(limit=3),
        "paper": paper,
    }


def format_periodic_summary_message(data: dict[str, Any]) -> str:
    mode = "PAPER" if data.get("paper") else "LIVE"
    as_of = data.get("as_of") or datetime.now(_ET)
    cash_pct = data.get("cash_pct")
    inv_pct = data.get("invested_pct")
    day_pnl = data.get("day_pnl")
    day_ret = data.get("day_return_pct")
    form_pnl = data.get("form_pnl")
    form_ret = data.get("form_return_pct")
    form_label = str(data.get("form_label") or "Since form")
    form_start = data.get("form_start_date") or ""
    day_sharpe = data.get("day_sharpe_30d")
    day_dd = data.get("day_max_dd_pct")
    all_ret = data.get("all_return_pct")
    all_sharpe = data.get("all_sharpe")
    wr = data.get("win_rate_pct")
    trades = int(data.get("total_trades") or 0)
    exposure = data.get("active_exposure")

    def _money(v: float | None) -> str:
        return f"${v:,.2f}" if v is not None else "n/a"

    def _pct(v: float | None, *, signed: bool = True) -> str:
        if v is None:
            return "n/a"
        return f"{v:+.2f}%" if signed else f"{v:.1f}%"

    form_header = form_label
    if form_start:
        form_header = f"{form_label} ({form_start})"

    lines = [
        f"PythonTrading {mode} — {as_of:%Y-%m-%d %H:%M} ET",
        "",
        form_header,
        f"  PnL:     {_money(form_pnl)} ({_pct(form_ret)})",
        "",
        "Today / rolling 30d",
        f"  Day PnL: {_money(day_pnl)} ({_pct(day_ret)})",
        f"  Sharpe:  {day_sharpe if day_sharpe is not None else 'n/a'} (30d)",
        f"  Max DD:  {_pct(day_dd, signed=False)} (30d)",
        f"  Cash:    {_pct(100.0 * cash_pct if cash_pct is not None else None, signed=False)}"
        f" | Invested {_pct(100.0 * inv_pct if inv_pct is not None else None, signed=False)}",
        f"  Active:  {_money(exposure)}",
        "",
        "All-time",
        f"  Return:  {_pct(all_ret)}",
        f"  Sharpe:  {all_sharpe if all_sharpe is not None else 'n/a'}",
        f"  Win rate: {_pct(wr, signed=False)} | Trades {trades}",
        "",
        f"Regime: {data.get('regime') or 'n/a'}",
        str(data.get("health_line") or "Bot health: n/a"),
    ]
    signals = list(data.get("signals") or [])
    if signals:
        lines.append("")
        lines.append("Top signals")
        lines.extend(f"  {s}" for s in signals[:4])
    else:
        lines.append("")
        lines.append("Top signals: none")
    return "\n".join(lines)


def send_periodic_summary(
    interval_hours: float = 3.0,
    *,
    equity: float | None = None,
    cash: float | None = None,
    regime: str = "",
    sleeves: dict | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> bool:
    """Send a compact Telegram summary every ``interval_hours`` (idempotent).

    Intended for the paper research bot. Live uses ``send_daily_live_summary``.
    Schedule: Mon–Fri [09:30, 15:00) America/Chicago via ``periodic_summary_due``.
    Message body is unchanged.
    """
    hours = float(
        interval_hours
        if interval_hours is not None
        else getattr(config, "TELEGRAM_PERIODIC_SUMMARY_HOURS", 3.0)
    )
    if not force and not periodic_summary_due(hours):
        return False
    if not dry_run and not alerts.alerts_configured():
        return False

    data = gather_periodic_summary(
        equity=equity,
        cash=cash,
        regime=regime,
        sleeves=sleeves,
    )
    message = format_periodic_summary_message(data)
    subject = (
        f"[PythonTrading {'PAPER' if data.get('paper') else 'LIVE'}] "
        f"{hours:g}h summary"
    )
    if dry_run:
        print(message)
        return True

    ok = alerts.broadcast(subject, message, category="periodic_summary")
    if ok:
        _save_summary_ts(
            {
                "last_sent_at": datetime.now(_ET).isoformat(),
                "interval_hours": hours,
                "equity": data.get("equity"),
                "regime": data.get("regime"),
            }
        )
        print(f"--- Periodic Telegram summary sent ({hours:g}h) ---")
    return ok


# --- Live bot: once-daily post-close summary (separate from paper 3h) ---

_LIVE_DAILY_TS_PATH = _ROOT / "data" / "last_live_daily_summary.json"


def _load_live_daily_ts() -> dict[str, Any]:
    if not _LIVE_DAILY_TS_PATH.is_file():
        return {}
    try:
        raw = json.loads(_LIVE_DAILY_TS_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _save_live_daily_ts(payload: dict[str, Any]) -> None:
    try:
        _LIVE_DAILY_TS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LIVE_DAILY_TS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("live daily summary timestamp write failed: %s", exc)


def _parse_hhmm(raw: str | None, *, default: tuple[int, int] = (16, 0)) -> tuple[int, int]:
    text = (raw or "").strip()
    try:
        hour_s, minute_s = text.split(":", 1)
        return int(hour_s), int(minute_s)
    except (ValueError, TypeError):
        return default


def live_daily_summary_due(*, market_open: bool | None = None) -> bool:
    """True on weekdays after TELEGRAM_LIVE_DAILY_SUMMARY_TIME ET when market is closed."""
    if not getattr(config, "TELEGRAM_ALERT_LIVE_DAILY_SUMMARY", True):
        return False
    if config.PAPER_TRADING or config.paper_chase_mode_enabled():
        return False
    now_et = datetime.now(_ET)
    if now_et.weekday() >= 5:
        return False
    if market_open is True:
        return False
    target_h, target_m = _parse_hhmm(
        getattr(config, "TELEGRAM_LIVE_DAILY_SUMMARY_TIME", None),
        default=(16, 0),
    )
    if (now_et.hour, now_et.minute) < (target_h, target_m):
        return False
    today = now_et.date().isoformat()
    cache = _load_live_daily_ts()
    return cache.get("last_sent_date") != today


def _top_live_positions(*, limit: int = 5) -> list[dict[str, Any]]:
    try:
        from modules.weekly_summary import _position_rows

        rows = _position_rows(paper=False)
    except Exception as exc:
        logger.debug("live positions unavailable: %s", exc)
        return []
    ranked = sorted(
        rows,
        key=lambda p: abs(float(p.get("mv") or 0)),
        reverse=True,
    )
    return ranked[:limit]


def _format_sleeve_lines(sleeves: dict | None) -> list[str]:
    if not sleeves:
        return ["  (none)"]
    parts: list[tuple[str, float]] = []
    mapping = (
        ("VTI", "vti_core_value"),
        ("SPY", "spy_value"),
        ("NYSE", "nyse_value"),
        ("Crypto", "crypto_value"),
        ("Metal", "metal_value"),
        ("Stat arb", "stat_arb_value"),
        ("Shorts", "short_value"),
    )
    for label, key in mapping:
        try:
            val = float(sleeves.get(key) or 0)
        except (TypeError, ValueError):
            val = 0.0
        if val > 1.0:
            parts.append((label, val))
    if not parts:
        return ["  (flat / no active sleeve MV)"]
    parts.sort(key=lambda x: x[1], reverse=True)
    return [f"  {label}: ${val:,.2f}" for label, val in parts]


def gather_live_daily_summary(
    *,
    equity: float | None = None,
    cash: float | None = None,
    regime: str = "",
    sleeves: dict | None = None,
) -> dict[str, Any]:
    """Live-book metrics for the post-close daily Telegram."""
    series = _book_equity_series(paper_chase=False)
    eqs = [eq for _, eq in series]
    day_series = _equity_marks_for_day(series)
    day_eqs = list(day_series)
    roll_series = _slice_last_days(series, 30)
    roll_eqs = [eq for _, eq in roll_series]

    eq_now = float(equity) if equity is not None else (eqs[-1] if eqs else 0.0)
    cash_now = float(cash) if cash is not None else 0.0
    cash_pct = (cash_now / eq_now) if eq_now > 0 else None
    invested_pct = (1.0 - cash_pct) if cash_pct is not None else None

    day_start = day_eqs[0] if day_eqs else (eqs[0] if eqs else None)
    day_pnl = (eq_now - day_start) if day_start is not None else None
    day_ret = (
        100.0 * (eq_now / day_start - 1.0) if day_start and day_start > 0 else None
    )

    return {
        "as_of": datetime.now(_ET),
        "equity": eq_now,
        "cash": cash_now,
        "cash_pct": cash_pct,
        "invested_pct": invested_pct,
        "day_pnl": day_pnl,
        "day_return_pct": day_ret,
        "daily_sharpe": daily_sharpe_from_series(series, paper_chase=False),
        "sharpe_30d": _sharpe_from_equity(roll_eqs),
        "max_dd_pct": _max_drawdown_pct(roll_eqs),
        "regime": regime or "n/a",
        "health_line": _bot_health_line(regime=regime or ""),
        "positions": _top_live_positions(limit=5),
        "sleeve_lines": _format_sleeve_lines(sleeves),
        "top_trades": top_trades_today(limit=3, paper_chase=False),
    }


def format_live_daily_summary_message(data: dict[str, Any]) -> str:
    as_of = data.get("as_of") or datetime.now(_ET)
    cash_pct = data.get("cash_pct")
    inv_pct = data.get("invested_pct")
    day_pnl = data.get("day_pnl")
    day_ret = data.get("day_return_pct")
    daily_sharpe = data.get("daily_sharpe")
    sharpe = data.get("sharpe_30d")
    max_dd = data.get("max_dd_pct")

    def _money(v: float | None) -> str:
        return f"${v:,.2f}" if v is not None else "n/a"

    def _pct(v: float | None, *, signed: bool = True) -> str:
        if v is None:
            return "n/a"
        return f"{v:+.2f}%" if signed else f"{v:.1f}%"

    lines = [
        f"PythonTrading LIVE — Daily close {as_of:%Y-%m-%d %H:%M} ET",
        "",
        f"Equity:    {_money(data.get('equity'))}",
        f"Cash:      {_money(data.get('cash'))} "
        f"({_pct(100.0 * cash_pct if cash_pct is not None else None, signed=False)})",
        f"Invested:  {_pct(100.0 * inv_pct if inv_pct is not None else None, signed=False)}",
        f"Day PnL:   {_money(day_pnl)} ({_pct(day_ret)})",
        f"Daily Sharpe: {daily_sharpe if daily_sharpe is not None else 'n/a'}",
        f"30d Sharpe: {sharpe if sharpe is not None else 'n/a'}",
        f"Max DD:     {_pct(max_dd, signed=False)} (30d)",
        "",
        f"Regime: {data.get('regime') or 'n/a'}",
        str(data.get("health_line") or "Bot health: n/a"),
        "",
        "Top 3 trades today",
    ]
    trades = list(data.get("top_trades") or [])
    if not trades:
        lines.append("  (none closed yet)")
    else:
        for i, t in enumerate(trades[:3], 1):
            sym = t.get("symbol") or "?"
            pnl = float(t.get("pnl") or 0.0)
            pnl_pct = t.get("pnl_pct")
            pct_s = f" ({float(pnl_pct):+.1f}%)" if pnl_pct is not None else ""
            qty = t.get("qty")
            qty_s = f" x{float(qty):.2f}" if qty is not None else ""
            lines.append(f"  {i}. {sym}{qty_s}: ${pnl:+,.2f}{pct_s}")

    lines.append("")
    lines.append("Active sleeves")
    lines.extend(list(data.get("sleeve_lines") or ["  (none)"]))

    positions = list(data.get("positions") or [])
    lines.append("")
    lines.append("Top positions")
    if not positions:
        lines.append("  (none)")
    else:
        for p in positions[:5]:
            tk = p.get("ticker") or "?"
            mv = p.get("mv")
            pnl = p.get("pnl_pct")
            sleeve = p.get("sleeve") or ""
            mv_s = f"${float(mv):,.0f}" if mv is not None else "n/a"
            pnl_s = f"{float(pnl):+.1f}%" if pnl is not None else "n/a"
            sleeve_s = f" [{sleeve}]" if sleeve else ""
            lines.append(f"  {tk}{sleeve_s}: {mv_s} ({pnl_s})")
    return "\n".join(lines)


def send_daily_live_summary(
    *,
    equity: float | None = None,
    cash: float | None = None,
    regime: str = "",
    sleeves: dict | None = None,
    market_open: bool | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> bool:
    """Send live bot daily close summary once per weekday after 16:00 ET.

    Separate from the paper 3-hour ``send_periodic_summary`` path. Idempotent via
    ``data/last_live_daily_summary.json`` (same once-per-day pattern as weekly).
    """
    if not force and not live_daily_summary_due(market_open=market_open):
        return False
    if config.PAPER_TRADING or config.paper_chase_mode_enabled():
        if not force:
            return False
    if not dry_run and not alerts.alerts_configured():
        return False

    data = gather_live_daily_summary(
        equity=equity,
        cash=cash,
        regime=regime,
        sleeves=sleeves,
    )
    message = format_live_daily_summary_message(data)
    subject = "[PythonTrading LIVE] Daily close summary"
    if dry_run:
        print(message)
        return True

    ok = alerts.broadcast(subject, message, category="live_daily_summary")
    if ok:
        now_et = datetime.now(_ET)
        _save_live_daily_ts(
            {
                "last_sent_date": now_et.date().isoformat(),
                "last_sent_at": now_et.isoformat(),
                "equity": data.get("equity"),
                "regime": data.get("regime"),
            }
        )
        print("--- Live daily Telegram summary sent ---")
    return ok
