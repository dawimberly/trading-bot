"""Alpaca crypto universe helpers — Path B expanded list with liquidity filter."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import config
from modules.safe_io import read_json_file, write_json_file

logger = logging.getLogger(__name__)

CACHE_PATH = Path(os.getenv("ALPACA_CRYPTO_UNIVERSE_CACHE", "data/alpaca_crypto_universe.json"))

STABLECOIN_PAIRS = frozenset(
    {
        "USDC-USD",
        "USDT-USD",
        "DAI-USD",
        "USDG-USD",
        "PYUSD-USD",
    }
)

# Path B fallback — Alpaca-tradable majors beyond static UNIVERSE (refresh overwrites cache)
STATIC_ALPACA_CRYPTO_EXTRA = [
    "XRP-USD",
    "TRX-USD",
    "BNB-USD",
    "CRV-USD",
    "GRT-USD",
    "MKR-USD",
    "YFI-USD",
    "BAT-USD",
    "COMP-USD",
    "LDO-USD",
    "ETC-USD",
    "XTZ-USD",
    "ALGO-USD",
    "SAND-USD",
    "MANA-USD",
    "AXS-USD",
    "CHZ-USD",
    "ENJ-USD",
    "1INCH-USD",
    "ZEC-USD",
    "XLM-USD",
    "HBAR-USD",
    "EOS-USD",
    "FLOW-USD",
    "ICP-USD",
    "POL-USD",
]


def _skip_symbol(symbol: str) -> bool:
    sym = config.normalize_symbol(symbol)
    return sym in STABLECOIN_PAIRS or not sym.endswith("-USD")


def fetch_alpaca_tradable_crypto(*, refresh: bool = False) -> list[str]:
    """Pull active Alpaca crypto pairs; cache to disk (Path B refresh)."""
    if not refresh and CACHE_PATH.is_file():
        cached = read_json_file(CACHE_PATH)
        symbols = cached.get("symbols") or []
        if symbols:
            return [str(s) for s in symbols]

    symbols: list[str] = []
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import AssetClass, AssetStatus
        from alpaca.trading.requests import GetAssetsRequest

        key, secret = config.get_alpaca_credentials()
        client = TradingClient(key, secret, paper=config.PAPER_TRADING)
        assets = client.get_all_assets(
            GetAssetsRequest(asset_class=AssetClass.CRYPTO, status=AssetStatus.ACTIVE)
        )
        for asset in assets:
            if not getattr(asset, "tradable", True):
                continue
            sym = config.normalize_symbol(str(asset.symbol))
            if _skip_symbol(sym):
                continue
            symbols.append(sym)
    except Exception as exc:
        logger.warning("Alpaca crypto asset fetch failed: %s", exc)

    if not symbols:
        base = set(config.base_crypto_universe())
        symbols = sorted(set(STATIC_ALPACA_CRYPTO_EXTRA) | base)

    symbols = sorted(set(symbols))
    write_json_file(
        CACHE_PATH,
        {
            "symbols": symbols,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "source": "alpaca" if symbols else "static",
        },
    )
    return symbols


def expanded_crypto_symbols(*, refresh: bool = False) -> list[str]:
    """Base UNIVERSE crypto + Alpaca-expanded extras (no duplicates)."""
    base = config.base_crypto_universe()
    if not config.effective_crypto_universe_expanded():
        return list(base)
    extra = fetch_alpaca_tradable_crypto(refresh=refresh)
    return sorted(set(base) | set(extra))


def filter_liquid_crypto_columns(
    symbols: list[str],
    data: pd.DataFrame,
    *,
    min_bars: int | None = None,
    max_symbols: int | None = None,
) -> list[str]:
    """Keep symbols with enough history; rank by median close × coverage (liquidity proxy)."""
    min_bars = min_bars or config.CRYPTO_EXPANDED_MIN_BARS
    max_symbols = max_symbols or config.CRYPTO_EXPANDED_MAX_SYMBOLS
    base = set(config.base_crypto_universe())
    scored: list[tuple[float, str]] = []
    for sym in symbols:
        if sym not in data.columns:
            continue
        series = data[sym].dropna()
        if len(series) < min_bars:
            if sym in base:
                scored.append((1e12, sym))
            continue
        med = float(series.median()) if len(series) else 0.0
        coverage = len(series) / max(len(data), 1)
        scored.append((med * coverage, sym))
    if not scored:
        return [s for s in symbols if s in data.columns]
    scored.sort(reverse=True)
    kept = [sym for _, sym in scored[:max_symbols]]
    return sorted(set(kept) | {s for s in base if s in data.columns})


def crypto_trading_columns(
    data: pd.DataFrame,
    *,
    expanded: bool | None = None,
) -> list[str]:
    """Crypto columns used by sleeve / stat-arb pair scans."""
    use_expanded = (
        config.effective_crypto_universe_expanded()
        if expanded is None
        else bool(expanded)
    )
    if use_expanded:
        universe = expanded_crypto_symbols()
    else:
        universe = config.base_crypto_universe()
    cols = [s for s in universe if s in data.columns]
    if use_expanded:
        return filter_liquid_crypto_columns(cols, data)
    return cols


def prefetch_expanded_crypto_history(
    *,
    days: int | None = None,
    refresh: bool = False,
    use_max: bool = False,
) -> list[str]:
    """Ensure expanded crypto tickers have daily SQLite history for backtests."""
    symbols = fetch_alpaca_tradable_crypto(refresh=refresh)
    base = set(config.base_crypto_universe())
    extra = [s for s in symbols if s not in base]
    if not extra:
        return symbols
    from fetch_data import fetch_daily_history_for_tickers

    fetch_daily_history_for_tickers(
        extra,
        days=days,
        use_max=use_max,
    )
    return symbols
