"""Kraken spot helpers: market buy/sell with dry-run validate support."""

from __future__ import annotations

import config
from modules import kraken_budget
from modules.kraken_pairs import kraken_pair_for_symbol


def _credentials() -> tuple[str, str] | None:
    key, secret = config.get_kraken_credentials()
    if not key or not secret:
        return None
    return key, secret


def kraken_configured() -> bool:
    return _credentials() is not None


def trading_allowed() -> bool:
    """Live orders only when ALLOW_KRAKEN_TRADING and not KRAKEN_DRY_RUN."""
    return bool(config.ALLOW_KRAKEN_TRADING and not config.KRAKEN_DRY_RUN)


def autopilot_enabled() -> bool:
    return bool(config.KRAKEN_AUTOPILOT_ENABLED and kraken_configured())


def _cap_usd(amount: float) -> float:
    return kraken_budget.cap_buy_usd(amount)


def _submit_order(
    *,
    pair: str,
    side: str,
    volume: str,
    asset_class: str | None = None,
) -> dict:
    if not kraken_configured():
        return {"ok": False, "error": "Kraken credentials missing"}

    validate = config.KRAKEN_DRY_RUN or not config.ALLOW_KRAKEN_TRADING
    if not validate and not config.ALLOW_KRAKEN_TRADING:
        return {"ok": False, "error": "ALLOW_KRAKEN_TRADING not set"}

    from kraken.spot import Trade

    key, secret = _credentials()
    trade = Trade(key=key, secret=secret)
    extra = {}
    if asset_class:
        extra["asset_class"] = asset_class
    kwargs = {
        "ordertype": "market",
        "side": side,
        "pair": pair,
        "volume": volume,
        "validate": validate,
    }
    if extra:
        kwargs["extra_params"] = extra
    try:
        response = trade.create_order(**kwargs)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "pair": pair, "validate": validate}

    txids = (response or {}).get("txid") or []
    ok = bool(txids) or validate
    return {
        "ok": ok,
        "pair": pair,
        "side": side,
        "volume": volume,
        "txid": txids[0] if txids else None,
        "validate": validate,
        "dry_run": validate,
        "raw": response,
    }


def market_buy_usd(
    pair: str,
    usd_amount: float,
    *,
    asset_class: str | None = None,
) -> dict:
    """Market buy `pair` for approximately `usd_amount` USD (quote)."""
    if not kraken_configured():
        return {"ok": False, "error": "Kraken credentials missing"}
    if not config.ALLOW_KRAKEN_TRADING and not config.KRAKEN_DRY_RUN:
        return {"ok": False, "error": "ALLOW_KRAKEN_TRADING not set"}

    usd_amount = _cap_usd(usd_amount)
    if usd_amount < config.MIN_NOTIONAL:
        err = f"below min ${config.MIN_NOTIONAL}"
        if kraken_budget.cycle_budget_usd() > 0 and kraken_budget.cycle_buy_spent() >= kraken_budget.cycle_budget_usd():
            err = f"cycle buy budget ${kraken_budget.cycle_budget_usd():.0f} used"
        return {"ok": False, "error": err}

    from kraken.spot import Market

    market = Market()
    ticker = market.get_ticker(pair=pair)
    if not ticker:
        return {"ok": False, "error": "no ticker", "pair": pair}

    entry = next(iter(ticker.values()))
    price = float(entry["c"][0])
    if price <= 0:
        return {"ok": False, "error": "invalid price", "pair": pair}

    volume = round(usd_amount / price, 8)
    result = _submit_order(
        pair=pair,
        side="buy",
        volume=str(volume),
        asset_class=asset_class,
    )
    result.update({"usd": usd_amount, "price_ref": price, "asset_class": asset_class})
    if result.get("ok") and not result.get("validate") and not result.get("dry_run"):
        kraken_budget.record_buy(usd_amount)
    return result


def market_buy_equity_usd(pair: str, usd_amount: float, kind: str = "equity") -> dict:
    """Buy Kraken Pro equity (.EQ) or xStock pair."""
    asset_class = "tokenized_asset" if kind == "xstock" else None
    result = market_buy_usd(pair, usd_amount, asset_class=asset_class)
    result["kind"] = kind
    return result


def market_buy_symbol(symbol: str, usd_amount: float) -> dict:
    pair = kraken_pair_for_symbol(symbol)
    if not pair:
        return {"ok": False, "error": f"no Kraken pair for {symbol}"}
    kind = "xstock" if pair.endswith("xUSD") else "equity"
    return market_buy_equity_usd(pair, usd_amount, kind=kind)


def market_sell_volume(
    pair: str,
    volume: float,
    *,
    asset_class: str | None = None,
) -> dict:
    """Market sell base-asset volume."""
    if volume <= 0:
        return {"ok": False, "error": "zero volume"}
    if not kraken_configured():
        return {"ok": False, "error": "Kraken credentials missing"}
    if not config.ALLOW_KRAKEN_TRADING and not config.KRAKEN_DRY_RUN:
        return {"ok": False, "error": "ALLOW_KRAKEN_TRADING not set"}

    result = _submit_order(
        pair=pair,
        side="sell",
        volume=str(round(volume, 8)),
        asset_class=asset_class,
    )
    result["volume"] = volume
    return result


def market_sell_symbol(symbol: str, volume: float) -> dict:
    pair = kraken_pair_for_symbol(symbol)
    if not pair:
        return {"ok": False, "error": f"no Kraken pair for {symbol}"}
    asset_class = "tokenized_asset" if pair.endswith("xUSD") and "x" in pair else None
    if pair.endswith("xUSD") and pair.index("x") > 0:
        asset_class = "tokenized_asset"
    return market_sell_volume(pair, volume, asset_class=asset_class)


def market_sell_usd(symbol: str, usd_amount: float) -> dict:
    """Market sell approximately `usd_amount` of base asset (crypto)."""
    if not kraken_configured():
        return {"ok": False, "error": "Kraken credentials missing"}
    if not config.ALLOW_KRAKEN_TRADING and not config.KRAKEN_DRY_RUN:
        return {"ok": False, "error": "ALLOW_KRAKEN_TRADING not set"}

    pair = kraken_pair_for_symbol(symbol)
    if not pair:
        return {"ok": False, "error": f"no Kraken pair for {symbol}"}

    from kraken.spot import Market

    market = Market()
    ticker = market.get_ticker(pair=pair)
    if not ticker:
        return {"ok": False, "error": "no ticker", "pair": pair}
    price = float(next(iter(ticker.values()))["c"][0])
    if price <= 0:
        return {"ok": False, "error": "invalid price"}

    usd_amount = max(float(usd_amount), config.MIN_NOTIONAL)
    volume = round(usd_amount / price, 8)
    out = market_sell_volume(pair, volume)
    out["usd"] = usd_amount
    out["symbol"] = symbol
    return out
