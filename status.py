"""At-a-glance bot status: live + paper equity, regime, key flags.

Run: python status.py
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

import config

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
LIVE_HEARTBEAT = Path(os.getenv("HEARTBEAT_FILE", config.HEARTBEAT_FILE))
PAPER_HEARTBEAT = Path(os.getenv("PAPER_CHASE_HEARTBEAT", "paper_chase_heartbeat.json"))


def _load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _fmt_equity(val: float | None) -> str:
    if val is None:
        return "n/a"
    return f"${val:,.2f}"


def _heartbeat_equity(hb: dict | None) -> float | None:
    if not hb:
        return None
    try:
        return float(hb.get("equity"))
    except (TypeError, ValueError):
        return None


def _heartbeat_regime(hb: dict | None) -> str | None:
    if not hb:
        return None
    regime = hb.get("regime")
    return str(regime) if regime else None


def _alpaca_equity(*, paper: bool, credentials_fn=None) -> float | None:
    try:
        from alpaca.trading.client import TradingClient

        cred_fn = credentials_fn or config.get_alpaca_credentials
        key, secret = cred_fn()
        client = TradingClient(key, secret, paper=paper)
        return float(client.get_account().equity)
    except Exception as exc:
        logger.debug("_alpaca_equity lookup failed", exc_info=True)
        return None


def _paper_research_equity() -> float | None:
    try:
        from modules.social_sleeve import get_social_alpaca_credentials

        creds = get_social_alpaca_credentials()
        if not creds:
            return None
        return _alpaca_equity(paper=True, credentials_fn=lambda: creds)
    except Exception:
        logger.debug("_paper_research_equity failed", exc_info=True)
        return None


def _resolve_equity(hb: dict | None, *, paper: bool, research: bool = False) -> float | None:
    eq = _heartbeat_equity(hb)
    if eq is not None:
        return eq
    if research:
        return _paper_research_equity()
    live_paper = paper or config.PAPER_TRADING
    return _alpaca_equity(paper=live_paper)


def _flag(name: str, val: bool) -> str:
    return f"{name}={'on' if val else 'off'}"


def _live_flags() -> str:
    parts = [
        _flag("dyn_vti", False),
        _flag("overlap", config.NYSE_OVERLAP_FILTER_ENABLED),
        _flag("chunk", config.ADAPTIVE_CHUNK_ENABLED),
        _flag("cofire", config.COFIRE_BUDGET_ENABLED),
        _flag("macro", config.MACRO_REGIME_ADAPTOR_ENABLED),
        _flag("social", config.SOCIAL_SLEEVE_ENABLED),
        _flag("spy_exit", config.SPY_EXIT_ON_MA_BREAK),
    ]
    return " | ".join(parts)


def _paper_flags() -> str:
    config.set_paper_aggressive_context(True)
    try:
        pf = config.get_paper_feature_flags()
        parts = [
            _flag("dyn_vti", config.PAPER_DYNAMIC_VTI_ENABLED),
            _flag("dyn_risk", config.PAPER_DYNAMIC_RISK_ENABLED),
            _flag("overlap", pf.get("nyse_overlap", False)),
            _flag("chunk", pf.get("adaptive_chunk", False)),
            _flag("cofire", pf.get("cofire_budget", False)),
            _flag("macro", config.PAPER_MACRO_REGIME_ADAPTOR_ENABLED),
            _flag("options", config.PAPER_OPTIONS_SLEEVE_ENABLED),
            _flag("stat_arb", config.PAPER_STAT_ARB_ENABLED),
            _flag("pairs", config.PAPER_MARKET_NEUTRAL_PAIRS),
            _flag("eq_pairs", config.PAPER_EQUITY_PAIRS),
            _flag("vol", config.PAPER_VOL_TRADING_ENABLED),
            "vol_live=log_only",
            _flag("social", config.PAPER_SOCIAL_SLEEVE_ENABLED),
            _flag("spy_exit", pf.get("spy_exit_on_ma_break", False)),
        ]
        return " | ".join(parts)
    finally:
        config.set_paper_aggressive_context(False)


def main() -> None:
    live_hb = _load_json(LIVE_HEARTBEAT if LIVE_HEARTBEAT.is_absolute() else ROOT / LIVE_HEARTBEAT)
    paper_hb = _load_json(ROOT / PAPER_HEARTBEAT)

    live_eq = _resolve_equity(live_hb, paper=False)
    paper_eq = _resolve_equity(paper_hb, paper=True)
    if paper_eq is None:
        paper_eq = _paper_research_equity()

    regime = _heartbeat_regime(live_hb) or _heartbeat_regime(paper_hb) or "n/a"

    ts = ""
    if live_hb and live_hb.get("timestamp"):
        ts = f" @ {live_hb['timestamp'][:19]}"
    elif paper_hb and paper_hb.get("timestamp"):
        ts = f" @ {paper_hb['timestamp'][:19]}"
    else:
        ts = f" @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

    logger.info(
        f"Live {_fmt_equity(live_eq)} | Paper {_fmt_equity(paper_eq)} | "
        f"Regime {regime}{ts}"
    )
    logger.info(f"Live flags:  {_live_flags()}")
    logger.info(f"Paper flags: {_paper_flags()}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
