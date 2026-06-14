"""Crypto vol mean-reversion sleeve — paper-only, isolated PAPER_APCA book (v4 filters)."""

from __future__ import annotations

import csv
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import config
from backtest_crypto_vol import (
    DROP_PCT,
    LOSS_COOLDOWN_HOURS,
    RSI_MAX_V4,
    RSI_MIN_V4,
    SMA_PERIOD,
    SPY_GATE_PCT,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    TIMEOUT_BARS,
    UNIVERSE_V4,
    enrich,
    entry_signal,
    fetch_alpaca_hourly,
    hour_allowed_utc,
    load_spy_daily_returns,
    set_entry_params,
)
from modules import deployment_sizing

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
HEARTBEAT_PATH = ROOT / "crypto_vol_heartbeat.json"
JOURNAL_PATH = ROOT / "crypto_vol_journal.csv"
COOLDOWN_PATH = ROOT / "crypto_vol_cooldown.json"

# v4 universe: WIF, BONK, RENDER, SOL, AVAX (no ARB)
UNIVERSE = UNIVERSE_V4
set_entry_params(DROP_PCT, RSI_MAX_V4, RSI_MIN_V4)

CRYPTO_VOL_SYMBOLS = frozenset(
    config.normalize_symbol(sym) for sym in UNIVERSE.values()
)

SLEEVE_CAP_PCT = 0.15
POSITION_PCT = 0.05
MAX_POSITIONS = 3
FETCH_DAYS = 30


def get_crypto_vol_alpaca_credentials() -> tuple[str, str] | None:
    key = (
        os.getenv("CRYPTO_VOL_APCA_API_KEY_ID")
        or os.getenv("PAPER_APCA_API_KEY_ID")
    )
    secret = (
        os.getenv("CRYPTO_VOL_APCA_API_SECRET_KEY")
        or os.getenv("PAPER_APCA_API_SECRET_KEY")
    )
    if key and secret:
        return key, secret
    return None


def crypto_vol_paper_available() -> bool:
    return get_crypto_vol_alpaca_credentials() is not None


def _paper_chase_active(*, paper_chase_context: bool = False) -> bool:
    if paper_chase_context:
        return True
    return os.getenv("PAPER_CHASE_MODE", "").lower() in ("1", "true", "yes")


def _paper_only_blocked(*, paper_chase_context: bool = False) -> str | None:
    """Return a reason string when the sleeve must not run; None if paper-only OK."""
    if _paper_chase_active(paper_chase_context=paper_chase_context):
        return None
    if not config.PAPER_TRADING:
        return "PAPER_TRADING=false"
    if config.ALLOW_LIVE_TRADING:
        return "ALLOW_LIVE_TRADING=yes"
    return None


def _order_symbol(label: str) -> str:
    """Universe label (WIF/USD) -> executor symbol (WIF-USD)."""
    return config.normalize_symbol(label)


def _universe_label(sym_norm: str) -> str:
    return next(
        (k for k, v in UNIVERSE.items() if config.normalize_symbol(v) == sym_norm),
        sym_norm,
    )


def _position_summaries(executor) -> list[dict]:
    return [
        {
            "symbol": _universe_label(config.normalize_symbol(p.symbol)),
            "qty": str(p.qty),
            "market_value": round(_position_value(p), 2),
            "avg_entry": float(p.avg_entry_price or 0),
        }
        for p in _crypto_vol_positions(executor)
    ]


def _spy_gate_blocks_entries(now: datetime) -> tuple[bool, str | None]:
    spy = load_spy_daily_returns()
    if spy is None:
        return False, None
    day = pd.Timestamp(now.astimezone(timezone.utc).date())
    spy_ret = spy.get(day, float("nan"))
    if pd.isna(spy_ret):
        return False, None
    if float(spy_ret) < SPY_GATE_PCT:
        return True, f"spy_return_{float(spy_ret):.2f}pct"
    return False, None


def _load_cooldowns() -> dict[str, str]:
    if not COOLDOWN_PATH.is_file():
        return {}
    try:
        with open(COOLDOWN_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cooldowns(data: dict[str, str]) -> None:
    COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(COOLDOWN_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def _prune_cooldowns(data: dict[str, str], now: datetime) -> dict[str, str]:
    out: dict[str, str] = {}
    for coin, until in data.items():
        dt = _parse_ts(until)
        if dt and dt > now:
            out[coin] = until
    return out


def _active_cooldown_coins(now: datetime) -> list[str]:
    data = _prune_cooldowns(_load_cooldowns(), now)
    return sorted(data.keys())


def _in_cooldown(coin: str, now: datetime) -> bool:
    data = _prune_cooldowns(_load_cooldowns(), now)
    until = _parse_ts(data.get(coin))
    return until is not None and now < until


def _set_loss_cooldown(coin: str, now: datetime) -> None:
    data = _prune_cooldowns(_load_cooldowns(), now)
    until = now + timedelta(hours=LOSS_COOLDOWN_HOURS)
    data[coin] = until.isoformat(timespec="seconds")
    _save_cooldowns(data)


def _crypto_vol_positions(executor) -> list:
    out = []
    for pos in executor.client.get_all_positions():
        if config.normalize_symbol(pos.symbol) in CRYPTO_VOL_SYMBOLS:
            out.append(pos)
    return out


def _position_value(pos) -> float:
    mv = getattr(pos, "market_value", None)
    if mv is not None:
        return abs(float(mv))
    return abs(float(pos.qty) * float(pos.current_price or 0))


def crypto_vol_sleeve_value(executor) -> float:
    return sum(_position_value(p) for p in _crypto_vol_positions(executor))


def _journal_open_entries(account_label: str) -> dict[str, dict]:
    """Open sleeve lots from journal buys not fully sold (paper book isolation)."""
    book: dict[str, float] = {}
    entries: dict[str, dict] = {}
    if not JOURNAL_PATH.is_file():
        return entries
    with open(JOURNAL_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("account") != account_label:
                continue
            sym = (row.get("symbol") or "").strip()
            if not sym:
                continue
            try:
                n = float(row.get("notional") or 0)
            except (TypeError, ValueError):
                continue
            action = row.get("action")
            if action == "buy":
                book[sym] = round(book.get(sym, 0.0) + n, 2)
                entries[sym] = {
                    "notional": book[sym],
                    "entry_price": row.get("entry_price"),
                    "entry_time": row.get("timestamp"),
                }
            elif action == "sell":
                book[sym] = round(max(0.0, book.get(sym, 0.0) - n), 2)
                if book.get(sym, 0.0) <= 0:
                    entries.pop(sym, None)
                elif sym in entries:
                    entries[sym]["notional"] = book[sym]
    return entries


def _today_realized_pnl(account_label: str = "paper") -> float:
    if not JOURNAL_PATH.is_file():
        return 0.0
    today = datetime.now(timezone.utc).date()
    total = 0.0
    exit_re = re.compile(r"exit@([0-9.]+)")
    with open(JOURNAL_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("account") != account_label or row.get("action") != "sell":
                continue
            ts = _parse_ts(row.get("timestamp"))
            if ts is None or ts.date() != today:
                continue
            try:
                notional = float(row.get("notional") or 0)
                entry_price = float(row.get("entry_price") or 0)
            except (TypeError, ValueError):
                continue
            if notional <= 0 or entry_price <= 0:
                continue
            notes = row.get("notes") or ""
            m = exit_re.search(notes)
            if not m:
                continue
            exit_price = float(m.group(1))
            total += notional * (exit_price / entry_price - 1.0)
    return round(total, 2)


def _log_action(row: dict) -> None:
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "timestamp",
        "account",
        "action",
        "symbol",
        "notional",
        "entry_price",
        "exit_reason",
        "ret_4h",
        "rsi14",
        "ok",
        "notes",
    ]
    write_header = not JOURNAL_PATH.is_file() or JOURNAL_PATH.stat().st_size == 0
    with open(JOURNAL_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if write_header:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fields})


def _write_heartbeat(payload: dict) -> None:
    HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(HEARTBEAT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _check_exit_live(
    entry_price: float,
    entry_time: datetime | None,
    row,
    *,
    now: datetime,
) -> tuple[float, str] | None:
    stop = entry_price * (1 - STOP_LOSS_PCT)
    target = entry_price * (1 + TAKE_PROFIT_PCT)
    if row["Low"] <= stop:
        return stop, "stop_loss"
    if row["High"] >= target:
        return target, "take_profit"
    if entry_time is not None:
        hold_hours = (now - entry_time).total_seconds() / 3600
        if hold_hours >= TIMEOUT_BARS:
            return float(row["Close"]), "timeout"
    return None


def _latest_signal_row(symbol_label: str):
    df = fetch_alpaca_hourly(symbol_label, days=FETCH_DAYS)
    if df is None or df.empty:
        return None, None
    enriched = enrich(df)
    if len(enriched) < max(SMA_PERIOD, 14, 4) + 1:
        return None, None
    row = enriched.iloc[-1]
    return row, enriched


def _size_buy_notional(equity: float, sleeve_value: float, cash: float) -> float | None:
    cap = round(equity * SLEEVE_CAP_PCT, 2)
    room = round(cap - sleeve_value, 2)
    raw = round(min(equity * POSITION_PCT, room, cash * 0.95), 2)
    if raw < config.effective_min_notional(equity):
        return None
    return deployment_sizing.apply_alpaca_crypto_fee_reserve(raw, equity=equity)


def _process_exits(
    executor,
    *,
    account_label: str,
    dry_run: bool,
    now: datetime,
) -> list[dict]:
    actions: list[dict] = []
    journal_entries = _journal_open_entries(account_label)
    positions = {
        config.normalize_symbol(p.symbol): p for p in _crypto_vol_positions(executor)
    }

    for sym_norm, pos in positions.items():
        label = _universe_label(sym_norm)
        row, _ = _latest_signal_row(UNIVERSE.get(label, label))
        if row is None:
            continue

        entry_price = float(pos.avg_entry_price or pos.current_price or 0)
        j = journal_entries.get(label) or journal_entries.get(sym_norm)
        if j and j.get("entry_price"):
            try:
                entry_price = float(j["entry_price"])
            except (TypeError, ValueError):
                pass
        entry_time = _parse_ts((j or {}).get("entry_time"))
        if entry_price <= 0:
            continue

        hit = _check_exit_live(entry_price, entry_time, row, now=now)
        if hit is None:
            continue

        exit_price, reason = hit
        sell_n = round(_position_value(pos), 2)
        min_n = config.effective_min_notional(float(executor.client.get_account().equity))
        if sell_n < min_n:
            continue

        act = {
            "account": account_label,
            "action": "sell",
            "symbol": label,
            "notional": sell_n,
            "entry_price": entry_price,
            "exit_reason": reason,
            "ok": False,
            "notes": f"exit@{exit_price:.6f}",
        }
        if not dry_run:
            order = executor.execute_full_exit(_order_symbol(label))
            act["ok"] = order is not None
            actions.append(act)
            _log_action(
                {
                    "timestamp": now.isoformat(timespec="seconds"),
                    **act,
                }
            )
            if reason == "stop_loss" and act["ok"]:
                _set_loss_cooldown(label, now)
        else:
            act["ok"] = True
            act["notes"] = f"dry-run {act['notes']}"
            actions.append(act)
            if reason == "stop_loss":
                act["cooldown_until"] = (
                    now + timedelta(hours=LOSS_COOLDOWN_HOURS)
                ).isoformat(timespec="seconds")
    return actions


def _process_entries(
    executor,
    *,
    account_label: str,
    equity: float,
    dry_run: bool,
    now: datetime,
    entry_filters: dict,
) -> list[dict]:
    actions: list[dict] = []
    held = {
        config.normalize_symbol(p.symbol)
        for p in _crypto_vol_positions(executor)
    }
    open_count = len(held)
    if open_count >= MAX_POSITIONS:
        return actions

    if entry_filters.get("spy_gate"):
        return actions
    if not entry_filters.get("hour_ok", True):
        return actions

    sleeve_value = crypto_vol_sleeve_value(executor)
    cash = float(executor.client.get_account().cash)

    for label in UNIVERSE:
        if open_count >= MAX_POSITIONS:
            break
        sym_norm = config.normalize_symbol(UNIVERSE[label])
        if sym_norm in held:
            continue
        if _in_cooldown(label, now):
            continue

        row, _ = _latest_signal_row(UNIVERSE[label])
        if row is None or not entry_signal(row):
            continue

        buy_n = _size_buy_notional(equity, sleeve_value, cash)
        if buy_n is None:
            continue

        act = {
            "account": account_label,
            "action": "buy",
            "symbol": label,
            "notional": buy_n,
            "entry_price": round(float(row["Close"]), 6),
            "exit_reason": "",
            "ret_4h": round(float(row["ret_4h"]), 4) if row.get("ret_4h") == row.get("ret_4h") else "",
            "rsi14": round(float(row["rsi14"]), 2) if row.get("rsi14") == row.get("rsi14") else "",
            "ok": False,
            "notes": (
                f"drop<{DROP_PCT:.0%} rsi{RSI_MIN_V4:.0f}-{RSI_MAX_V4:.0f}"
            ),
        }
        if not dry_run:
            order = executor.execute_order(_order_symbol(label), "buy", notional=buy_n)
            act["ok"] = order is not None
            if act["ok"]:
                executor.refresh_cache()
                sleeve_value = crypto_vol_sleeve_value(executor)
                cash = float(executor.client.get_account().cash)
                open_count += 1
                held.add(sym_norm)
            actions.append(act)
            _log_action(
                {
                    "timestamp": now.isoformat(timespec="seconds"),
                    **act,
                }
            )
        else:
            act["ok"] = True
            act["notes"] = f"dry-run {act['notes']}"
            open_count += 1
            sleeve_value = round(sleeve_value + buy_n, 2)
            actions.append(act)
    return actions


def run_crypto_vol_sleeve_cycle(*, dry_run: bool = False, paper_chase_context: bool = False) -> dict:
    """
    Paper crypto vol sleeve: mean-reversion entries on 1h bars, isolated book.
    v4 filters: SPY gate, UTC hour windows, RSI 32-42, 48h loss cooldown.
    Refuses to run unless paper-only (PAPER_TRADING=true, no ALLOW_LIVE_TRADING)
    or PAPER_CHASE_MODE / paper_chase_context from run_paper_bot.
    """
    now = datetime.now(timezone.utc)
    spy_blocked, spy_reason = _spy_gate_blocks_entries(now)
    hour_ok = hour_allowed_utc(now)
    result: dict = {
        "enabled": True,
        "dry_run": dry_run,
        "timestamp": now.isoformat(timespec="seconds"),
        "paper_ok": False,
        "paper_equity": None,
        "sleeve_value": 0.0,
        "sleeve_cap_pct": SLEEVE_CAP_PCT,
        "position_pct": POSITION_PCT,
        "max_positions": MAX_POSITIONS,
        "universe": list(UNIVERSE.keys()),
        "filters": {
            "spy_gate": spy_blocked,
            "spy_reason": spy_reason,
            "hour_ok": hour_ok,
            "rsi_band": [RSI_MIN_V4, RSI_MAX_V4],
            "loss_cooldown_hours": LOSS_COOLDOWN_HOURS,
        },
        "cooldown_coins": _active_cooldown_coins(now),
        "active_positions": [],
        "last_signal_time": None,
        "today_pnl": _today_realized_pnl(),
        "exit_actions": [],
        "entry_actions": [],
        "signals": [],
    }

    blocked = _paper_only_blocked(paper_chase_context=paper_chase_context)
    if blocked:
        msg = f"crypto_vol_sleeve: {blocked} — refusing to run (paper-only sleeve)"
        logger.warning(msg)
        print(f"WARNING: {msg}")
        result["blocked"] = blocked
        result["enabled"] = False
        _write_heartbeat(result)
        return result

    creds = get_crypto_vol_alpaca_credentials()
    if not creds:
        result["error"] = "missing PAPER_APCA_* credentials"
        _write_heartbeat(result)
        return result

    last_signal_time: str | None = None
    try:
        from modules.alpaca_executor import AlpacaExecutor

        executor = AlpacaExecutor(paper=True, credentials_fn=lambda: creds)
        account = executor.client.get_account()
        equity = float(account.equity)
        result["paper_equity"] = equity
        result["paper_ok"] = True
        result["cash"] = float(account.cash)
        result["sleeve_value"] = crypto_vol_sleeve_value(executor)

        result["active_positions"] = _position_summaries(executor)

        for label in UNIVERSE:
            row, _ = _latest_signal_row(UNIVERSE[label])
            if row is None:
                result["signals"].append({"symbol": label, "signal": False, "reason": "no_data"})
                continue
            sig = bool(entry_signal(row))
            if sig:
                last_signal_time = now.isoformat(timespec="seconds")
            result["signals"].append(
                {
                    "symbol": label,
                    "signal": sig,
                    "close": round(float(row["Close"]), 6),
                    "ret_4h": round(float(row["ret_4h"]), 4)
                    if row.get("ret_4h") == row.get("ret_4h")
                    else None,
                    "rsi14": round(float(row["rsi14"]), 2)
                    if row.get("rsi14") == row.get("rsi14")
                    else None,
                    "below_sma10": bool(
                        row.get("sma10") == row.get("sma10")
                        and row["Close"] < row["sma10"]
                    ),
                    "in_cooldown": _in_cooldown(label, now),
                }
            )

        prev_hb: dict | None = None
        if HEARTBEAT_PATH.is_file():
            try:
                with open(HEARTBEAT_PATH, encoding="utf-8") as f:
                    prev_hb = json.load(f)
            except (json.JSONDecodeError, OSError):
                prev_hb = None
        if last_signal_time:
            result["last_signal_time"] = last_signal_time
        elif prev_hb and prev_hb.get("last_signal_time"):
            result["last_signal_time"] = prev_hb["last_signal_time"]

        entry_filters = {
            "spy_gate": spy_blocked,
            "hour_ok": hour_ok,
        }

        result["exit_actions"] = _process_exits(
            executor,
            account_label="paper",
            dry_run=dry_run,
            now=now,
        )
        if result["exit_actions"] and not dry_run:
            executor.refresh_cache()
            result["sleeve_value"] = crypto_vol_sleeve_value(executor)
            result["today_pnl"] = _today_realized_pnl()
            result["cooldown_coins"] = _active_cooldown_coins(now)

        result["entry_actions"] = _process_entries(
            executor,
            account_label="paper",
            equity=equity,
            dry_run=dry_run,
            now=now,
            entry_filters=entry_filters,
        )
        if not dry_run:
            result["sleeve_value"] = crypto_vol_sleeve_value(executor)
            result["active_positions"] = _position_summaries(executor)

    except Exception as exc:
        result["paper_error"] = str(exc)
        logger.exception("crypto_vol_sleeve cycle failed")

    _write_heartbeat(result)
    return result
