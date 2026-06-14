"""Crypto sleeve vol gate + optional SpaceX IPO narrative override."""

from __future__ import annotations

import config
from modules.pipeline_strategies import regime_entries_paused

VALID_NARRATIVES = frozenset({"hot_btc_narrative", "btc_tied", "spcx_perp_active"})


def spacex_crypto_override(spacex_snapshot: dict | None) -> tuple[bool, str]:
    """
    Allow crypto trading when SpaceX IPO ↔ BTC / SPCX-perp narrative is active,
    even if cross-asset 5m volatility reads Low.
    """
    if not config.SPACEX_IPO_CRYPTO_OVERRIDE:
        return False, "override_disabled"
    if not spacex_snapshot:
        return False, "no_spacex_snapshot"

    summary = spacex_snapshot.get("summary") or {}
    narrative = summary.get("narrative", "")
    btc_linked = int(summary.get("btc_linked_count") or 0)
    spcx_perp = int(summary.get("spcx_perp_count") or 0)
    sentiment = float(summary.get("avg_sentiment") or 0.0)

    if sentiment < config.SPACEX_CRYPTO_OVERRIDE_MIN_SENTIMENT:
        return False, f"sentiment_{sentiment:+.2f}_below_min"

    headline_ok = (
        narrative in VALID_NARRATIVES
        and btc_linked >= config.SPACEX_CRYPTO_OVERRIDE_MIN_BTC_HEADLINES
    )
    perp_ok = spcx_perp >= config.SPACEX_CRYPTO_OVERRIDE_MIN_SPCX_PERP

    if headline_ok:
        return True, f"narrative_{narrative}_btc_{btc_linked}"
    if perp_ok:
        return True, f"spcx_perp_{spcx_perp}"
    return False, f"narrative_{narrative}_btc_{btc_linked}_perp_{spcx_perp}"


def crypto_trading_allowed(
    volatility: str,
    regime: str,
    *,
    spacex_snapshot: dict | None = None,
    data=None,
    sentiment: float | None = None,
) -> dict:
    """
    Return whether crypto entries are allowed and why.
    Keys: allowed, vol, regime, spacex_override, reason
    """
    if regime_entries_paused(regime, data, sentiment):
        return {
            "allowed": False,
            "vol": volatility,
            "regime": regime,
            "spacex_override": False,
            "reason": "regime_paused",
        }

    if not config.effective_crypto_vol_only():
        return {
            "allowed": True,
            "vol": volatility,
            "regime": regime,
            "spacex_override": False,
            "reason": "vol_only_off",
        }

    if volatility == "High":
        return {
            "allowed": True,
            "vol": volatility,
            "regime": regime,
            "spacex_override": False,
            "reason": "vol_high",
        }

    override, override_reason = spacex_crypto_override(spacex_snapshot)
    if override:
        return {
            "allowed": True,
            "vol": volatility,
            "regime": regime,
            "spacex_override": True,
            "reason": override_reason,
        }

    return {
        "allowed": False,
        "vol": volatility,
        "regime": regime,
        "spacex_override": False,
        "reason": "vol_low",
    }


def crypto_target_allowed(
    volatility: str,
    regime: str,
    *,
    spacex_snapshot: dict | None = None,
) -> bool:
    """Whether rebalance / sleeve targets may hold crypto (not necessarily trade)."""
    return crypto_trading_allowed(
        volatility, regime, spacex_snapshot=spacex_snapshot
    )["allowed"]
