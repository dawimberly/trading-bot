"""Lightweight weekly screener refresh for NYSE momentum + stat-arb equity universe."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import config

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGE_DAYS = int(
    __import__("os").getenv("PAPER_UNIVERSE_REFRESH_DAYS", "7")
)


def screener_universe_age_days() -> float | None:
    path = Path(config.SCREENER_UNIVERSE_PATH)
    if not path.is_file():
        return None
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return (datetime.now(timezone.utc) - mtime).total_seconds() / 86400.0


def screener_universe_meta() -> dict:
    path = Path(config.SCREENER_UNIVERSE_PATH)
    if not path.is_file():
        return {"exists": False, "path": str(path), "age_days": None, "count": 0}
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        tickers = payload.get("tickers") or []
    except (OSError, json.JSONDecodeError):
        tickers = []
    age = screener_universe_age_days()
    return {
        "exists": True,
        "path": str(path),
        "age_days": round(age, 1) if age is not None else None,
        "count": len(tickers),
        "generated_at": payload.get("generated_at") if tickers else None,
    }


def maybe_refresh_screener_universe(
    *,
    force: bool = False,
    max_age_days: int | None = None,
) -> dict:
    """
    Refresh data/screener_universe.json when stale (default weekly).
    Paper aggressive only unless USE_DYNAMIC_UNIVERSE is set globally.
    """
    if not config.effective_paper_dynamic_universe():
        return {"action": "disabled", "reason": "paper_dynamic_universe_off"}

    max_age = max_age_days if max_age_days is not None else DEFAULT_MAX_AGE_DAYS
    age = screener_universe_age_days()
    if not force and age is not None and age < max_age:
        return {
            "action": "fresh",
            "age_days": round(age, 1),
            "max_age_days": max_age,
            **screener_universe_meta(),
        }

    try:
        from scripts.analysis.universe_screener import run_screener

        result = run_screener()
        logger.info(
            "dynamic_universe refreshed: %s tickers",
            len(result.get("tickers") or []),
        )
        return {"action": "refreshed", **result, **screener_universe_meta()}
    except Exception as exc:
        logger.warning("dynamic_universe refresh failed: %s", exc)
        return {
            "action": "failed",
            "error": str(exc),
            **screener_universe_meta(),
        }
