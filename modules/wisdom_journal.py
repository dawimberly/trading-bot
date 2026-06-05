"""Append-only journal of wisdom layer state and account metrics (live bot)."""

from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path

import pandas as pd

import config
from modules.wayback_sentiment import load_monthly_web_sentiment
from modules.wisdom_sentiment import MODES, DEPRECATED_MODES, LIVE_MODES, entries_paused, regime_sentiment
from modules.market_context import get_volatility

JOURNAL_FIELDS = [
    "timestamp",
    "active_mode",
    "gap_threshold",
    "web_sentiment",
    "price_sentiment",
    "sentiment_gap",
    "regime",
    "volatility",
    "wisdom_paused",
    "equity",
    "cash",
    "crypto_trades",
    "spy_trades",
    "nyse_trades",
    "shadow_would_pause_baseline",
    "shadow_would_pause_arbitrage",
    "shadow_would_pause_web_regime",
    "shadow_would_pause_wisdom_pause",
    "shadow_would_pause_governor",
    "shadow_would_pause_dynamic",
    "sizing_multiplier",
    "spacex_ipo_narrative",
    "spacex_btc_headlines",
    "spacex_ipo_sentiment",
    "spacex_ipo_alert",
    "spacex_spcx_perp",
    "crypto_spacex_override",
    "notes",
]

_monthly_web_cache: pd.Series | None = None


def _path() -> str:
    return getattr(config, "WISDOM_JOURNAL_FILE", "wisdom_journal.csv")


def _monthly_web() -> pd.Series:
    global _monthly_web_cache
    if _monthly_web_cache is None:
        _monthly_web_cache = load_monthly_web_sentiment()
    return _monthly_web_cache


def _ensure_header() -> None:
    path = _path()
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=JOURNAL_FIELDS).writeheader()
        return
    with open(path, newline="", encoding="utf-8") as f:
        header = next(csv.reader(f), [])
    missing = [c for c in JOURNAL_FIELDS if c not in header]
    if not missing:
        return
    df = pd.read_csv(path)
    for col in missing:
        df[col] = ""
    tmp = f"{path}.tmp"
    df[JOURNAL_FIELDS].to_csv(tmp, index=False)
    os.replace(tmp, path)


def _shadow_pauses(data, ts, monthly_web: pd.Series, gap_threshold: float) -> dict[str, bool]:
    vol = get_volatility(data)
    pauses = {}
    for mode in MODES:
        _sent, web, gap = regime_sentiment(
            data,
            ts,
            monthly_web,
            mode=mode,
            gap_threshold=gap_threshold,
        )
        pauses[mode] = entries_paused(
            mode, web, gap, gap_threshold, data=data, vol=vol
        )
    return pauses


def log_cycle(
    data,
    ts,
    wisdom: dict,
    *,
    equity: float,
    cash: float,
    crypto_trades: int = 0,
    spy_trades: int = 0,
    nyse_trades: int = 0,
    spacex_ipo: dict | None = None,
    crypto_gate: dict | None = None,
    notes: str = "",
) -> None:
    _ensure_header()
    gap_threshold = config.WISDOM_GAP_THRESHOLD
    monthly_web = _monthly_web()
    shadows = _shadow_pauses(data, ts, monthly_web, gap_threshold)

    summary = (spacex_ipo or {}).get("summary") or {}
    row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "active_mode": wisdom.get("wisdom_mode", config.WISDOM_MODE),
        "gap_threshold": gap_threshold,
        "web_sentiment": wisdom.get("web_sentiment", ""),
        "price_sentiment": wisdom.get("price_sentiment", ""),
        "sentiment_gap": wisdom.get("sentiment_gap", ""),
        "regime": wisdom.get("regime", ""),
        "volatility": wisdom.get("volatility", ""),
        "wisdom_paused": wisdom.get("wisdom_paused", False),
        "equity": round(equity, 2),
        "cash": round(cash, 2),
        "crypto_trades": crypto_trades,
        "spy_trades": spy_trades,
        "nyse_trades": nyse_trades,
        "shadow_would_pause_baseline": shadows.get("baseline", False),
        "shadow_would_pause_arbitrage": shadows.get("arbitrage", False),
        "shadow_would_pause_web_regime": shadows.get("web_regime", False),
        "shadow_would_pause_wisdom_pause": shadows.get("wisdom_pause", False),
        "shadow_would_pause_governor": shadows.get("governor", False),
        "shadow_would_pause_dynamic": shadows.get("dynamic", False),
        "sizing_multiplier": wisdom.get("sizing_multiplier", ""),
        "spacex_ipo_narrative": summary.get("narrative", ""),
        "spacex_btc_headlines": summary.get("btc_linked_count", ""),
        "spacex_ipo_sentiment": summary.get("avg_sentiment", ""),
        "spacex_ipo_alert": (spacex_ipo or {}).get("alert", False),
        "spacex_spcx_perp": summary.get("spcx_perp_count", ""),
        "crypto_spacex_override": (crypto_gate or {}).get("spacex_override", False),
        "notes": notes,
    }
    path = Path(_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=JOURNAL_FIELDS).writerow(row)


def load_journal() -> pd.DataFrame:
    path = _path()
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return pd.DataFrame(columns=JOURNAL_FIELDS)
    return pd.read_csv(path, parse_dates=["timestamp"])
