"""How each ticker can be traded on Kraken via API (xStock, equity spot, or app-only .EQ)."""

from __future__ import annotations

from modules.kraken_capabilities import probe_kraken_capabilities
from modules.kraken_pairs import ALPACA_TO_KRAKEN_PAIR, equity_pair_likely_unsupported
from modules.kraken_xstocks import xstock_pair_for_ticker

# Legacy Kraken equity spot pairs (not .EQ) that validate when equity_spot_ok
EQUITY_SPOT_PAIRS = {
    "VOO": "VOOIUSD",
    "SLV": "SLVIUSD",
}


def resolve_tradable_route(symbol: str, *, capabilities: dict | None = None) -> dict:
    """
    Return route metadata for a ticker.

    route:
      - crypto: spot crypto pair
      - xstock: tokenized_asset (SPYxUSD, etc.)
      - equity_spot: legacy spot equity (VOOIUSD, SLVIUSD)
      - eq_manual: .EQ balance — sell/buy in Kraken app only
      - none: unknown / skipped
    """
    cap = capabilities or probe_kraken_capabilities()
    sym = (symbol or "").upper().strip()
    if not sym or sym == "USD":
        return {"route": "none", "pair": None, "api_ok": False, "symbol": sym}

    crypto_pairs = {
        "BTC": "XBTUSD",
        "ETH": "ETHUSD",
        "SOL": "SOLUSD",
        "ADA": "ADAUSD",
        "AVAX": "AVAXUSD",
        "LINK": "LINKUSD",
        "RENDER": "RENDERUSD",
    }
    if sym in crypto_pairs:
        return {
            "route": "crypto",
            "pair": crypto_pairs[sym],
            "api_ok": bool(cap.get("crypto_ok")),
            "symbol": sym,
        }

    xpair = xstock_pair_for_ticker(sym)
    if xpair and cap.get("xstock_ok"):
        return {"route": "xstock", "pair": xpair, "api_ok": True, "symbol": sym}
    if xpair and not cap.get("xstock_ok"):
        return {
            "route": "xstock",
            "pair": xpair,
            "api_ok": False,
            "symbol": sym,
            "blocker": cap.get("xstock_error") or "xStocks permission required",
        }

    spot = EQUITY_SPOT_PAIRS.get(sym)
    if not spot:
        mapped = ALPACA_TO_KRAKEN_PAIR.get(sym)
        if mapped and ".EQ" not in mapped:
            spot = mapped
    if spot and cap.get("equity_spot_ok"):
        return {"route": "equity_spot", "pair": spot, "api_ok": True, "symbol": sym}

    mapped = ALPACA_TO_KRAKEN_PAIR.get(sym)
    if mapped and equity_pair_likely_unsupported(mapped):
        return {
            "route": "eq_manual",
            "pair": mapped,
            "api_ok": False,
            "symbol": sym,
            "blocker": "Kraken Pro .EQ - migrate to xStock or equity spot in app",
        }

    if xpair:
        return {
            "route": "xstock",
            "pair": xpair,
            "api_ok": False,
            "symbol": sym,
            "blocker": cap.get("xstock_error"),
        }

    return {"route": "none", "pair": None, "api_ok": False, "symbol": sym}


def format_route_line(route: dict) -> str:
    sym = route.get("symbol", "?")
    r = route.get("route", "none")
    if route.get("api_ok"):
        return f"{sym}: API ({r}, {route.get('pair')})"
    blocker = route.get("blocker") or "not API-tradable"
    return f"{sym}: MANUAL APP - {blocker}"
