"""Ticker universe access — delegates to config.UNIVERSE."""

import config


def get_full_market_universe():
    """Return the configured trading universe."""
    return list(config.UNIVERSE)
