"""Optional first-day market buy when SPCX becomes tradable on Alpaca."""

from __future__ import annotations

import json
import os
from datetime import datetime

import config

STATE_FILE = "spacex_ipo_buy_state.json"


def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _account_key() -> str:
    return "paper" if config.PAPER_TRADING else "live"


def _ipo_buy_notional(executor) -> float | None:
    equity = float(executor.client.get_account().equity)
    cash = float(executor.client.get_account().cash)
    notional = min(
        config.SPACEX_IPO_BUY_NOTIONAL,
        equity * 0.25,
        cash * 0.95,
    )
    notional = round(notional, 2)
    min_n = config.effective_min_notional(equity)
    if notional < min_n:
        return None
    return notional


def maybe_buy_spacex_ipo(executor, listing_snapshot: dict | None) -> dict | None:
    """
    One-shot Alpaca market buy when SPCX becomes tradable.
    Live requires SPACEX_IPO_AUTO_BUY=true and ALLOW_LIVE_TRADING=yes.
    """
    if not config.SPACEX_IPO_AUTO_BUY or not listing_snapshot:
        return None
    if not config.PAPER_TRADING and not config.ALLOW_LIVE_TRADING:
        return None
    if not listing_snapshot.get("ready_to_buy_alpaca"):
        return None

    symbol = config.SPACEX_IPO_TICKER
    acct_key = _account_key()
    state = _load_state()
    bought_by_account = dict(state.get("bought_by_account") or {})
    if state.get("bought") and not bought_by_account:
        bought_by_account["paper"] = True
    if bought_by_account.get(acct_key):
        return None

    notional = _ipo_buy_notional(executor)
    if notional is None:
        return None

    order = executor.execute_order(symbol, "buy", notional=notional)
    result = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "symbol": symbol,
        "notional": notional,
        "account": acct_key,
        "ok": order is not None,
    }
    if order is not None:
        bought_by_account[acct_key] = True
        state["bought_by_account"] = bought_by_account
        state["bought"] = True
        state["bought_at"] = result["at"]
        state["notional"] = notional
        state["account"] = acct_key
        _save_state(state)
    return result
