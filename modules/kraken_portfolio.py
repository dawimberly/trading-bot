"""Kraken holdings snapshot: USD values for rebalance."""

from __future__ import annotations

from typing import Any

import config
from modules.kraken_advisor import (
    CASH_ASSETS,
    LEVERAGED_TICKERS,
    fetch_kraken_balances,
    load_manual_positions,
)
from modules.kraken_pairs import kraken_pair_for_symbol, ticker_from_balance_display

CRYPTO_SYMBOLS = frozenset({"BTC", "ETH", "SOL", "RENDER", "ADA", "AVAX", "LINK", "XBT", "XETH"})


def _crypto_usd_value(symbol: str, amount: float) -> float | None:
    pair = kraken_pair_for_symbol(symbol if symbol != "BTC" else "BTC-USD")
    if not pair:
        return None
    try:
        from kraken.spot import Market

        ticker = Market().get_ticker(pair=pair)
        if not ticker:
            return None
        price = float(next(iter(ticker.values()))["c"][0])
        return round(amount * price, 2)
    except Exception:
        return None


def build_portfolio_snapshot() -> dict[str, Any]:
    """
    Return {ok, total_usd, holdings: [{ticker, usd, volume, kind}], error}.
    kind: cash | crypto | equity
    """
    bal = fetch_kraken_balances()
    manual = load_manual_positions()
    manual_by = {(p.get("ticker") or "").upper(): p for p in manual if p.get("ticker")}

    holdings: list[dict] = []
    if not bal.get("ok"):
        total = sum(float(p.get("usd") or 0) for p in manual)
        for t, p in manual_by.items():
            holdings.append(
                {
                    "ticker": t,
                    "usd": float(p.get("usd") or 0),
                    "volume": None,
                    "kind": "equity",
                }
            )
        return {
            "ok": bool(manual),
            "total_usd": round(total, 2),
            "holdings": holdings,
            "error": bal.get("error"),
        }

    for b in bal.get("balances") or []:
        disp = b.get("display", "").upper()
        amt = float(b.get("amount", 0))
        if amt <= 0:
            continue

        if disp in CASH_ASSETS:
            holdings.append({"ticker": "USD", "usd": amt, "volume": amt, "kind": "cash"})
            continue

        if disp in CRYPTO_SYMBOLS or (disp.endswith("USD") and len(disp) <= 8):
            sym = disp.replace("XBT", "BTC").replace("XETH", "ETH")
            if sym.endswith("USD"):
                sym = sym[:-3]
            usd = _crypto_usd_value(sym, amt)
            if usd is not None:
                holdings.append(
                    {"ticker": sym, "usd": usd, "volume": amt, "kind": "crypto"}
                )
            continue

        if disp.endswith(".EQ"):
            ticker = ticker_from_balance_display(disp)
            usd = None
            if ticker in manual_by:
                usd = float(manual_by[ticker].get("usd") or 0)
            holdings.append(
                {
                    "ticker": ticker,
                    "usd": usd or 0.0,
                    "volume": amt,
                    "kind": "equity",
                }
            )

    # Manual-only equities
    have = {h["ticker"] for h in holdings}
    for t, p in manual_by.items():
        if t in have:
            continue
        holdings.append(
            {
                "ticker": t,
                "usd": float(p.get("usd") or 0),
                "volume": None,
                "kind": "equity",
            }
        )

    total = round(sum(h["usd"] for h in holdings if h["usd"]), 2)
    return {"ok": True, "total_usd": total, "holdings": holdings, "error": None}


def holdings_by_ticker(snapshot: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for h in snapshot.get("holdings") or []:
        t = h["ticker"]
        if t in out:
            out[t]["usd"] = round(float(out[t]["usd"]) + float(h["usd"]), 2)
        else:
            out[t] = dict(h)
    return out
