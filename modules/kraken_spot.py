"""Kraken spot helpers (live crypto only — not wired to run_all fund loop)."""

from __future__ import annotations

import config


def _credentials() -> tuple[str, str] | None:
    key, secret = config.get_kraken_credentials()
    if not key or not secret:
        return None
    return key, secret


def kraken_configured() -> bool:
    return _credentials() is not None


def market_buy_usd(pair: str, usd_amount: float, *, asset_class: str | None = None) -> dict:
    """
    Market buy `pair` for approximately `usd_amount` USD (quote).
    Set asset_class='tokenized_asset' for Kraken xStocks (e.g. SPCXxUSD).
    """
    if not config.ALLOW_KRAKEN_TRADING:
        return {"ok": False, "error": "ALLOW_KRAKEN_TRADING not set"}
    creds = _credentials()
    if not creds:
        return {"ok": False, "error": "Kraken credentials missing"}

    usd_amount = round(float(usd_amount), 2)
    if usd_amount < config.MIN_NOTIONAL:
        return {"ok": False, "error": f"below min ${config.MIN_NOTIONAL}"}

    from kraken.spot import Market, Trade

    key, secret = creds
    market = Market()
    trade = Trade(key=key, secret=secret)

    ticker = market.get_ticker(pair=pair)
    if not ticker:
        return {"ok": False, "error": "no ticker"}

    entry = next(iter(ticker.values()))
    price = float(entry["c"][0])
    if price <= 0:
        return {"ok": False, "error": "invalid price"}

    volume = round(usd_amount / price, 8)
    extra_params = {}
    if asset_class:
        extra_params["asset_class"] = asset_class
    response = trade.create_order(
        ordertype="market",
        side="buy",
        pair=pair,
        volume=str(volume),
        **({"extra_params": extra_params} if extra_params else {}),
    )
    txids = (response or {}).get("txid") or []
    return {
        "ok": bool(txids),
        "pair": pair,
        "usd": usd_amount,
        "volume": volume,
        "price_ref": price,
        "txid": txids[0] if txids else None,
        "asset_class": asset_class,
        "raw": response,
    }


def market_buy_equity_usd(pair: str, usd_amount: float, kind: str = "xstock") -> dict:
    """Buy tokenized stock (xStock) or spot equity pair on Kraken."""
    asset_class = "tokenized_asset" if kind == "xstock" else None
    result = market_buy_usd(pair, usd_amount, asset_class=asset_class)
    result["kind"] = kind
    return result
