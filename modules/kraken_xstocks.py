"""Kraken xStock (tokenized_asset) pair resolution and orders."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from functools import lru_cache

import config
from modules.kraken_spot import _cap_usd, _credentials, _submit_order

_PAIR_CACHE: dict[str, str] = {}


@lru_cache(maxsize=1)
def _load_xstock_pairs() -> dict[str, str]:
    """Ticker (VOO) -> wsname pair key (VOOxUSD)."""
    url = "https://api.kraken.com/0/public/AssetPairs?" + urllib.parse.urlencode(
        {"aclass_base": "tokenized_asset"}
    )
    with urllib.request.urlopen(url, timeout=30) as resp:
        raw = json.loads(resp.read().decode())
    out: dict[str, str] = {}
    for altname, info in (raw.get("result") or {}).items():
        if (info.get("status") or "").lower() not in ("online", "reduce_only"):
            continue
        base = (info.get("base") or "").upper()
        ws = info.get("wsname") or altname
        # VOOx -> VOO
        if base.endswith("X") and len(base) > 1:
            ticker = base[:-1]
            if altname.endswith("USD") and "SPV" not in altname:
                out[ticker] = altname
    return out


def xstock_pair_for_ticker(ticker: str) -> str | None:
    t = (ticker or "").upper()
    if t in _PAIR_CACHE:
        return _PAIR_CACHE[t]
    pairs = _load_xstock_pairs()
    return pairs.get(t)


def market_buy_xstock_usd(ticker: str, usd: float) -> dict:
    pair = xstock_pair_for_ticker(ticker)
    if not pair:
        return {"ok": False, "error": f"no xStock pair for {ticker}"}
    usd = _cap_usd(usd)
    if usd < config.MIN_NOTIONAL:
        return {"ok": False, "error": "below min notional"}

    from kraken.spot import Market

    market = Market()
    ticker_data = market.get_ticker(pair=pair)
    if not ticker_data:
        return {"ok": False, "error": "no ticker"}
    price = float(next(iter(ticker_data.values()))["c"][0])
    volume = round(usd / price, 8)
    result = _submit_order(
        pair=pair,
        side="buy",
        volume=str(volume),
        asset_class="tokenized_asset",
    )
    result.update({"usd": usd, "ticker": ticker, "pair": pair})
    return result


def market_sell_xstock_volume(ticker: str, volume: float) -> dict:
    pair = xstock_pair_for_ticker(ticker)
    if not pair:
        return {"ok": False, "error": f"no xStock pair for {ticker}"}
    return _submit_order(
        pair=pair,
        side="sell",
        volume=str(round(volume, 8)),
        asset_class="tokenized_asset",
    )
