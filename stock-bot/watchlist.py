"""Watchlist helpers derived from config.UNIVERSE (no duplicate ticker lists)."""

import config


def get_full_watchlist():
    """Return all configured universe tickers."""
    return list(config.UNIVERSE)


def get_benchmarks():
    return [t for t in config.UNIVERSE if t in ("SPY", "QQQ", "VTI", "IWM")]


def get_stocks():
    return [t for t in config.UNIVERSE if not config.is_crypto(t)]


def get_crypto():
    return [t for t in config.UNIVERSE if config.is_crypto(t)]
