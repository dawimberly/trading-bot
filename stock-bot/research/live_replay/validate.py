"""Compare recorded Live sessions with the isolated replay. Read-only broker tape."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from research.live_replay.inventory import BOOK, SAFETY, bar_inventory, journal_inventory, log_inventory
from research.live_replay.recorded import num, parse_ct
from research.live_replay.settings import (
    PATH,
    live_decision_flags,
    parse_env_file,
    resolve_effective,
)
from research.live_replay.sim_broker import SimBroker

ET = ZoneInfo("America/New_York")
DAYS = ("2026-10-15", "2026-10-16")
STOCK_ENV = Path(r"C:\Users\Owner\PythonTrading\stock-bot\.env")
OUT = Path(__file__).resolve().parent / "results" / "oct15_16.json"
FILL_TOLERANCE = 0.05


def _settings() -> dict:
    book = parse_env_file((BOOK / ".env").read_text(encoding="utf-8"))
    stock = parse_env_file(STOCK_ENV.read_text(encoding="utf-8")) if STOCK_ENV.is_file() else {}
    # The portal parent environment from those mornings was not saved.
    # Stock-bot/.env is applied only as the fill-in step, matching a child
    # whose parent did not already export an overlay key.
    effective = resolve_effective(parent_env={}, book_env=book, stock_env=stock)
    public = {key: value for key, value in effective.items() if value != "<set>"}
    return {
        "precedence": list(PATH),
        "flags": live_decision_flags(effective),
        "effective": {
            key: public[key]
            for key in (
                "PAPER_TRADING",
                "ALLOW_LIVE_TRADING",
                "VTI_CORE_ENABLED",
                "LIVE_CONSERVATIVE_ENABLED",
                "LIVE_VTI_CORE_PCT",
                "LIVE_SMALL_ACTIVE_SLEEVE_PCT",
                "LIVE_ACTIVE_SLEEVE_CHOICE",
                "NYSE_SLEEVE_CAP_PCT",
                "PER_NAME_MAX_PCT",
                "MAX_ACTIVE_TICKERS",
                "CONCENTRATION_TRIM_MIN_PCT",
                "AUTO_DUST_MAX_NOTIONAL",
                "PAPER_NYSE_A2B1_ENABLED",
                "VTI_REBALANCE_CADENCE",
                "DYNAMIC_CORE_ENABLED",
            )
            if key in public
        },
    }


def _broker_tape() -> dict:
    """GET historical orders and activities. Descriptions are dropped."""
    logging.disable(logging.CRITICAL)
    book = parse_env_file((BOOK / ".env").read_text(encoding="utf-8"))
    raw = {}
    for line in (BOOK / ".env").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        raw[key.strip()] = value.strip().strip('"').strip("'")
    key = raw.get("APCA_API_KEY_ID") or ""
    secret = raw.get("APCA_API_SECRET_KEY") or ""
    if not key or not secret:
        return {"ok": False, "error": "book credentials missing"}
    if book.get("PAPER_TRADING") == "true":
        return {"ok": False, "error": "book file is paper; refused"}
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest, GetPortfolioHistoryRequest
        from modules.alpaca_client import build_trading_client

        client = build_trading_client(key, secret, paper=False)
        start = datetime(2026, 10, 14, 0, 0, tzinfo=ET)
        end = datetime(2026, 10, 17, 0, 0, tzinfo=ET)
        orders = client.get_orders(
            filter=GetOrdersRequest(
                status=QueryOrderStatus.ALL, after=start, until=end, limit=500, nested=True
            )
        )
        cleaned = []
        for order in orders:
            cleaned.append(
                {
                    "id": str(getattr(order, "id", "") or ""),
                    "symbol": str(getattr(order, "symbol", "") or ""),
                    "side": str(getattr(order, "side", "") or "").split(".")[-1].lower(),
                    "status": str(getattr(order, "status", "") or "").split(".")[-1].lower(),
                    "qty": _f(getattr(order, "filled_qty", None) or getattr(order, "qty", None)),
                    "price": _f(getattr(order, "filled_avg_price", None)),
                    "notional": _f(getattr(order, "notional", None)),
                    "submitted_at": _iso(getattr(order, "submitted_at", None)),
                    "filled_at": _iso(getattr(order, "filled_at", None)),
                }
            )
        equity = []
        try:
            history = client.get_portfolio_history(
                GetPortfolioHistoryRequest(period="1W", timeframe="1D")
            )
            stamps = list(getattr(history, "timestamp", None) or [])
            values = list(getattr(history, "equity", None) or [])
            for stamp, value in zip(stamps, values):
                when = datetime.fromtimestamp(int(stamp), ET).isoformat()
                equity.append({"timestamp_et": when, "equity": _f(value)})
        except Exception as exc:
            equity = [{"error": type(exc).__name__}]
        return {
            "ok": True,
            "orders": cleaned,
            "activities": [],
            "activity_note": "this Alpaca SDK build has no account-activity method",
            "equity": equity,
        }
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}


def _f(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(ET).isoformat()
    return str(value)


def _match_fills(journal_fills: list[dict], orders: list[dict]) -> dict:
    used = set()
    pairs = []
    journal_only = []
    for fill in journal_fills:
        stamp = parse_ct(fill.get("timestamp") or "")
        symbol = (fill.get("symbol") or "").upper()
        side = (fill.get("side") or "").lower()
        best = None
        best_delta = None
        for index, order in enumerate(orders):
            if index in used:
                continue
            if order.get("symbol", "").upper() != symbol or order.get("side") != side:
                continue
            filled = order.get("filled_at") or order.get("submitted_at")
            if stamp is None or not filled:
                delta = 10**9
            else:
                try:
                    when = datetime.fromisoformat(filled)
                except ValueError:
                    delta = 10**9
                else:
                    delta = abs((when - stamp.astimezone(ET)).total_seconds())
            if best_delta is None or delta < best_delta:
                best = index
                best_delta = delta
        if best is None or best_delta is None or best_delta > 180:
            journal_only.append(fill.get("timestamp"))
            continue
        used.add(best)
        order = orders[best]
        j_notional = num(fill.get("notional"))
        o_notional = order.get("notional")
        if o_notional is None and order.get("qty") and order.get("price"):
            o_notional = round(order["qty"] * order["price"], 4)
        gap = None
        if j_notional is not None and o_notional is not None:
            gap = round(j_notional - o_notional, 4)
        kind = "fill_model" if gap is not None and abs(gap) <= FILL_TOLERANCE else "notional_diff"
        if gap is None:
            kind = "notional_missing"
        j_qty = num(fill.get("qty"))
        b_qty = order.get("qty")
        qty_class = "qty_missing"
        if j_qty is not None and b_qty:
            qty_class = "fill_model" if abs(j_qty - b_qty) <= max(1e-6, abs(b_qty) * 0.002) else "qty_diff"
        pairs.append(
            {
                "symbol": symbol,
                "side": side,
                "journal_ct": fill.get("timestamp"),
                "broker_et": order.get("filled_at"),
                "journal_qty": j_qty,
                "broker_qty": b_qty,
                "journal_price": num(fill.get("price")),
                "broker_price": order.get("price"),
                "journal_notional": j_notional,
                "broker_notional": o_notional,
                "signal_notional": num(fill.get("signal_notional")) if fill.get("signal_notional") not in (None, "") else None,
                "notional_class": kind,
                "qty_class": qty_class,
                "status": order.get("status"),
            }
        )
    broker_only = [orders[index] for index in range(len(orders)) if index not in used]
    return {"matched": pairs, "journal_only": journal_only, "broker_only": broker_only}


def _note_pnl(note: str) -> float | None:
    for token in (note or "").split():
        if token.endswith("%"):
            try:
                return float(token[:-1]) / 100.0
            except ValueError:
                return None
    return None


def compare_session(day: str, day_info: dict, orders: list[dict]) -> dict:
    fills = [row for row in day_info["trade_rows"] if row["event"] == "fill"]
    signals = [row for row in day_info["trade_rows"] if row["event"] == "signal"]
    exits = [row for row in day_info["trade_rows"] if row["event"] == "exit"]
    for fill in fills:
        stamp = parse_ct(fill["timestamp_ct"])
        match = None
        for signal in signals:
            if signal["symbol"] == fill["symbol"] and signal["side"] == fill["side"]:
                sig_at = parse_ct(signal["timestamp_ct"])
                if stamp and sig_at and abs((sig_at - stamp).total_seconds()) <= 5:
                    match = num(signal["notional"])
        fill["signal_notional"] = match
    # Rebind signal notionals onto the matcher via a closure-free pass after match.
    comparison = _match_fills(
        [
            {
                "timestamp": row["timestamp_ct"],
                "symbol": row["symbol"],
                "side": row["side"],
                "qty": row["qty"],
                "price": row["price"],
                "notional": row["notional"],
                "signal_notional": row.get("signal_notional"),
            }
            for row in fills
        ],
        [order for order in orders if (order.get("filled_at") or "").startswith(day) or (order.get("submitted_at") or "").startswith(day)],
    )
    for pair in comparison["matched"]:
        if pair["signal_notional"] is not None and pair["journal_notional"] is not None:
            delta = round(pair["signal_notional"] - pair["journal_notional"], 4)
            pair["signal_vs_journal_notional"] = delta
            pair["signal_class"] = "fill_model" if abs(delta) <= FILL_TOLERANCE else "decision_or_sizing"
    exit_checks = []
    for row in exits:
        note = row["notes"] or ""
        recorded = _note_pnl(note)
        exit_checks.append(
            {
                "symbol": row["symbol"],
                "reason": row["exit_reason"],
                "note": note,
                "recorded_pnl": recorded,
                "exit_row_has_price": bool(row.get("price")),
                "fixed_stop_matches_recorded_pnl": row["exit_reason"] == "stop_loss"
                and recorded is not None
                and recorded <= -0.05,
                "independent_mark": False,
            }
        )
    hours = day_info["cycle_hours_ct"]
    startups = day_info["startups_ct"]
    incomplete = []
    if day == "2026-10-16":
        if not any(hour in hours for hour in ("01", "02", "03", "04", "05", "06", "07")):
            incomplete.append("no cycles from 01:00 through 07:59 CT")
        if startups and startups[0] >= "2026-10-16 08:30:00":
            incomplete.append("process started at " + startups[0] + " CT, after the 08:30 CT cash open")
    if not day_info["trade_rows"] and not hours:
        incomplete.append("no journal rows")
    incomplete.append("candidate ranking, skips, and the close matrix were not stored")
    return {
        "day": day,
        "label": "incomplete",
        "reasons": incomplete,
        "fills": comparison,
        "exits": exit_checks,
        "cycle_hours_ct": hours,
    }


def build_report() -> dict:
    settings = _settings()
    journal = journal_inventory(DAYS)
    bars = bar_inventory()
    logs = log_inventory(DAYS)
    tape = _broker_tape()
    orders = tape.get("orders") or [] if tape.get("ok") else []
    sessions = [compare_session(day, journal["days"][day], orders) for day in DAYS]
    safety = {}
    if SAFETY.is_file():
        payload = json.loads(SAFETY.read_text(encoding="utf-8"))
        safety = payload.get("alpaca_live") or {}
    broker_sim = SimBroker()
    try:
        broker_sim.submit_order()
        send_blocked = False
    except Exception:
        send_blocked = True
    gaps = [
        "The live cycle loads the 5-minute close matrix. MA50 is 50 of those bars. The journal label /MA50 is that bar count.",
        "No point-in-time copy of that matrix or of the candidate list was stored, so rankings were not recomputed.",
        "market_data.db is a rolling store (traded names currently start 2026-10-12). It was not used to rank.",
        "Cooldown, sleeve room, and the quote at each exit decision were not stored.",
        "The portal parent environment was not saved. MAX_ACTIVE_TICKERS comes from stock-bot/.env fill-in when the parent did not export it.",
        "LIVE_MAX_ACTIVE_TICKERS_MIN and LIVE_MAX_ACTIVE_TICKERS_MAX are in the book file and are not read by code.",
        "trading_safety_state.json keeps only the latest live day, so October 15 circuit state is gone.",
        "Journal cycle rows do not store the ranked book or per-name skip reasons.",
        "Bot log lines for order submission do not include the symbol.",
    ]
    return {
        "faithful": False,
        "strategy_search": False,
        "baseline_a_used": False,
        "order_send_blocked": send_blocked,
        "books_left_unchanged": ["alpaca_live", "alpaca_paper_v2", "alpaca_paper"],
        "path": [
            "modules/portal_bot.py user_bot_env(alpaca_live)",
            "run_all.py main",
            "AlpacaExecutor.enforce_portfolio_guards (concentration trim and dust; first two live cycles skip concentration)",
            "modules/position_exits.py run_position_exits (fixed 5% stop on this book)",
            "modules/pipeline_strategies.py run_nyse_momentum_and_stat_arb",
            "run_equity_strategy MA50 rank, then sizing and execute_order",
            "run_idle_cash_scale_in returns immediately on Live",
            "stat arb and equity pairs stay off",
        ],
        "settings": settings,
        "gaps": gaps,
        "inventory": {"journal": journal, "bars": bars, "log": logs, "safety_latest": safety},
        "broker": {"ok": tape.get("ok"), "error": tape.get("error"), "equity": tape.get("equity"), "activity_count": len(tape.get("activities") or [])},
        "sessions": sessions,
    }


def main() -> None:
    report = build_report()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"wrote {OUT}")
    print(f"faithful={report['faithful']} sessions={len(report['sessions'])}")


if __name__ == "__main__":
    main()
