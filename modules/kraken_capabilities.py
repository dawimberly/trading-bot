"""Probe what Kraken API can trade with the configured key (cached daily)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import config

CACHE_FILE = Path(__file__).resolve().parents[1] / "kraken_capabilities.json"


def _load() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    CACHE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def probe_kraken_capabilities(*, force: bool = False) -> dict:
    """
    Return {crypto_ok, xstock_ok, equity_spot_ok, crypto_error, xstock_error, date}.
    """
    cached = _load()
    today = date.today().isoformat()
    if not force and cached.get("date") == today:
        return cached

    result = {
        "date": today,
        "crypto_ok": False,
        "xstock_ok": False,
        "equity_spot_ok": False,
        "crypto_error": None,
        "xstock_error": None,
        "equity_error": None,
    }
    key, secret = config.get_kraken_credentials()
    if not key or not secret:
        result["crypto_error"] = "no API keys"
        _save(result)
        return result

    from kraken.spot import Trade

    trade = Trade(key=key, secret=secret)

    try:
        trade.create_order(
            pair="ETHUSD",
            side="buy",
            ordertype="market",
            volume="0.001",
            validate=True,
        )
        result["crypto_ok"] = True
    except Exception as exc:
        result["crypto_error"] = str(exc)[:200]

    try:
        # VOO has no xStock pair; SPYxUSD is the standard probe pair.
        trade.create_order(
            pair="SPYxUSD",
            side="buy",
            ordertype="market",
            volume="0.01",
            validate=True,
            extra_params={"asset_class": "tokenized_asset"},
        )
        result["xstock_ok"] = True
    except Exception as exc:
        msg = str(exc)
        result["xstock_error"] = msg[:200]
        if "Permission denied" in msg:
            result["xstock_error"] = "API key needs xStocks/tokenized permission"

    try:
        trade.create_order(
            pair="VOOIUSD",
            side="buy",
            ordertype="market",
            volume="0.01",
            validate=True,
        )
        result["equity_spot_ok"] = True
    except Exception as exc:
        result["equity_error"] = str(exc)[:200]

    _save(result)
    return result


def stocks_api_available() -> bool:
    cap = probe_kraken_capabilities()
    return bool(cap.get("xstock_ok") or cap.get("equity_spot_ok"))


def crypto_api_available() -> bool:
    return bool(probe_kraken_capabilities().get("crypto_ok"))
