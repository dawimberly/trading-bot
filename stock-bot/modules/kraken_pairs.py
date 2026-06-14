"""Map Alpaca / universe symbols to Kraken spot or Pro equity pair names."""

from __future__ import annotations

# Alpaca universe symbol -> Kraken REST pair (extend as you list new names)
ALPACA_TO_KRAKEN_PAIR: dict[str, str] = {
    "BTC-USD": "XBTUSD",
    "ETH-USD": "ETHUSD",
    "SOL-USD": "SOLUSD",
    "ADA-USD": "ADAUSD",
    "AVAX-USD": "AVAXUSD",
    "LINK-USD": "LINKUSD",
    "RENDER-USD": "RENDERUSD",
    "RENDER": "RENDERUSD",
    "BTC": "XBTUSD",
    "ETH": "ETHUSD",
    "SOL": "SOLUSD",
    "SPY": "SPY.EQ",
    "QQQ": "QQQ.EQ",
    "VOO": "VOOIUSD",
    "VTI": "VTI.EQ",
    "IWM": "IWM.EQ",
    "GLD": "GLD.EQ",
    "SLV": "SLVIUSD",
    "CPER": "CPER.EQ",
    "GUSH": "GUSH.EQ",
    "NASA": "NASA.EQ",
    "WFC": "WFC.EQ",
    "IONQ": "IONQ.EQ",
    "KTOS": "KTOS.EQ",
    "FTNT": "FTNT.EQ",
    "XOP": "XOP.EQ",
    "CVX": "CVX.EQ",
    "GE": "GE.EQ",
    "CMPS": "CMPS.EQ",
    "MKSI": "MKSI.EQ",
    "NVDA": "NVDA.EQ",
    "AAPL": "AAPL.EQ",
    "MSFT": "MSFT.EQ",
    "AMD": "AMD.EQ",
    "TSLA": "TSLA.EQ",
    "META": "META.EQ",
    "GOOGL": "GOOGL.EQ",
    "AMZN": "AMZN.EQ",
}

# Metals on Alpaca often have no Kraken ETF pair — mirror skips with reason
MIRROR_SKIP_SYMBOLS = frozenset({"CPER", "URA", "PPLT", "DBB", "GDX"})


def normalize_alpaca_symbol(symbol: str) -> str:
    import config

    return config.normalize_symbol(symbol)


def kraken_pair_for_symbol(symbol: str) -> str | None:
    """Return Kraken pair name or None if unknown."""
    key = normalize_alpaca_symbol(symbol)
    raw = (symbol or "").upper().strip()
    if raw in ALPACA_TO_KRAKEN_PAIR:
        return ALPACA_TO_KRAKEN_PAIR[raw]
    if key in MIRROR_SKIP_SYMBOLS:
        return None
    if key in ALPACA_TO_KRAKEN_PAIR:
        return ALPACA_TO_KRAKEN_PAIR[key]
    base = key.replace("-USD", "")
    if key.endswith("-USD"):
        return f"{base}USD"
    if len(base) <= 5 and base.isalpha() and base in (
        "BTC",
        "ETH",
        "SOL",
        "ADA",
        "AVAX",
        "LINK",
        "RENDER",
    ):
        return ALPACA_TO_KRAKEN_PAIR.get(f"{base}-USD") or f"{base}USD"
    if len(base) <= 6 and base.isalpha():
        return f"{base}.EQ"
    return None


def equity_pair_likely_unsupported(pair: str) -> bool:
    """Kraken Pro .EQ balances often are not in public AssetPairs (manual sell)."""
    return (pair or "").endswith(".EQ")


def ticker_from_balance_display(display: str) -> str:
    d = (display or "").upper()
    if d.endswith(".EQ"):
        return d[:-3]
    return d
