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


def maybe_buy_spacex_ipo(executor, listing_snapshot: dict | None) -> dict | None:
    """
    One-shot Alpaca market buy when SPCX becomes tradable.
    Paper-only unless you explicitly run live with SPACEX_IPO_AUTO_BUY=true.
    Default: enabled on paper accounts only.
    """
    if not config.SPACEX_IPO_AUTO_BUY or not listing_snapshot:
        return None
    if not config.PAPER_TRADING:
        return None
    if not listing_snapshot.get("ready_to_buy_alpaca"):
        return None

    symbol = config.SPACEX_IPO_TICKER
    state = _load_state()
    if state.get("bought"):
        return None

    notional = round(config.SPACEX_IPO_BUY_NOTIONAL, 2)
    if notional < config.MIN_NOTIONAL:
        return None

    order = executor.execute_order(symbol, "buy", notional=notional)
    result = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "symbol": symbol,
        "notional": notional,
        "ok": order is not None,
    }
    if order is not None:
        state["bought"] = True
        state["bought_at"] = result["at"]
        state["notional"] = notional
        _save_state(state)
    return result
