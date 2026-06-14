"""Route Kraken orders: crypto spot, xStocks, legacy .EQ (not API-tradable)."""

from __future__ import annotations

import config
from modules.kraken_capabilities import probe_kraken_capabilities, stocks_api_available
from modules.kraken_pairs import kraken_pair_for_symbol
from modules.kraken_spot import market_buy_symbol, market_sell_symbol, market_sell_usd
from modules.kraken_xstocks import market_buy_xstock_usd, market_sell_xstock_volume, xstock_pair_for_ticker

CRYPTO_TICKERS = frozenset({"BTC", "ETH", "SOL", "RENDER", "ADA", "AVAX", "LINK", "USD"})


def execute_kraken_trade(trade: dict, *, capabilities: dict | None = None) -> dict:
    """
    Execute one rebalance trade dict {symbol, side, usd, volume}.
    No Telegram — returns result for logging.
    """
    cap = capabilities or probe_kraken_capabilities()
    sym = (trade.get("symbol") or "").upper()
    side = trade.get("side", "buy")
    usd = float(trade.get("usd") or 0)
    vol = trade.get("volume")

    if sym in CRYPTO_TICKERS and sym != "USD":
        if not cap.get("crypto_ok"):
            return {"ok": False, "error": cap.get("crypto_error"), "trade": trade}
        if side == "buy":
            out = market_buy_symbol(sym, usd)
        elif vol:
            out = market_sell_symbol(sym, float(vol))
        else:
            out = market_sell_usd(sym, usd)
        out["trade"] = trade
        return out

    if sym == "USD":
        return {"ok": False, "skipped": "cash rebalance not via API", "trade": trade}

    # Stocks: xStock first, then legacy VOOI-style spot equity
    if cap.get("xstock_ok") and xstock_pair_for_ticker(sym):
        if side == "buy":
            out = market_buy_xstock_usd(sym, usd)
        elif vol:
            out = market_sell_xstock_volume(sym, float(vol))
        else:
            return {"ok": False, "error": "sell needs volume for xStock", "trade": trade}
        out["trade"] = trade
        return out

    if cap.get("equity_spot_ok"):
        # Legacy Kraken equity spot (e.g. VOOIUSD) — not .EQ balances
        pair = kraken_pair_for_symbol(sym)
        if pair and ".EQ" not in pair:
            try:
                if side == "buy":
                    out = market_buy_symbol(sym, usd)
                elif vol:
                    out = market_sell_symbol(sym, float(vol))
                else:
                    out = market_sell_usd(sym, usd)
                out["trade"] = trade
                return out
            except Exception as exc:
                return {"ok": False, "error": str(exc)[:200], "trade": trade, "needs_app": True}

    reason = "stocks not API-tradable"
    if not stocks_api_available():
        xe = cap.get("xstock_error") or cap.get("equity_error") or ""
        if "US:" in xe or "restricted" in xe.lower():
            reason = "region restricts stock API (crypto still auto)"
        elif "Permission" in xe:
            reason = "enable xStocks permission on Kraken API key"
    return {
        "ok": False,
        "skipped": reason,
        "trade": trade,
        "needs_app": True,
    }
