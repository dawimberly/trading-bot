"""Live Kraken Pro buy when SPCX / SPCXx becomes tradable on Kraken."""

from __future__ import annotations

import json
import os
from datetime import datetime

import config
from modules.kraken_spot import kraken_configured, market_buy_equity_usd

STATE_FILE = "kraken_spcx_buy_state.json"


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


def maybe_buy_kraken_spcx(listing_snapshot: dict | None) -> dict | None:
    """
    One-shot Kraken market buy when SPCX (or SPCXx) is online on Kraken Pro API.
    Requires KRAKEN_SPCX_BUY_ENABLED=true and ALLOW_KRAKEN_TRADING=yes.
    """
    if not config.KRAKEN_SPCX_BUY_ENABLED or not listing_snapshot:
        return None

    kraken = listing_snapshot.get("kraken") or {}
    if not kraken.get("tradable"):
        return None
    if not kraken_configured():
        return {"ok": False, "error": "Kraken credentials not configured"}

    state = _load_state()
    if state.get("bought"):
        return None

    pair = kraken.get("pair")
    if not pair:
        return {"ok": False, "error": "no Kraken pair resolved"}

    usd = round(config.KRAKEN_SPCX_BUY_USD, 2)
    kind = kraken.get("kind") or "xstock"
    result = market_buy_equity_usd(pair, usd, kind=kind)
    result["at"] = datetime.now().isoformat(timespec="seconds")
    result["trigger"] = "kraken_spcx_listing_live"
    result["wsname"] = kraken.get("wsname")

    if result.get("ok"):
        state["bought"] = True
        state["bought_at"] = result["at"]
        state["usd"] = usd
        state["pair"] = pair
        state["txid"] = result.get("txid")
        _save_state(state)
    return result
