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
    except Exception:
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


def _live_profile_line() -> str:
    vti = config.SMALL_ACCOUNT_VTI_CORE_PCT
    return (
        f"Profile A (live) | VTI ~{vti:.0%} | risk {config.SMALL_ACCOUNT_RISK_PER_TRADE:.0%} | "
        f"max ${config.SMALL_ACCOUNT_MAX_NOTIONAL:.0f}/order"
    )


def _live_flags() -> str:
    parts = [
        _flag("dyn_vti", False),
        _flag("overlap", config.NYSE_OVERLAP_FILTER_ENABLED),
        _flag("chunk", config.ADAPTIVE_CHUNK_ENABLED),
        _flag("cofire", config.COFIRE_BUDGET_ENABLED),
        _flag("macro", config.MACRO_REGIME_ADAPTOR_ENABLED),
        _flag("social", config.SOCIAL_SLEEVE_ENABLED),
        _flag("spy_exit", config.SPY_EXIT_ON_MA_BREAK),
        _flag("thinking", False),
    ]
    return " | ".join(parts)


def _thinking_safety_line() -> str:
    s = config.get_thinking_safety_summary()
    approval = "required" if s["manual_approval_live"] else "off"
    return (
        f"tilt_cap=±{s['max_sleeve_delta_pp']:.0f}% | "
        f"daily_loss_live={s['daily_loss_limit_live_pct']:.0f}% | "
        f"daily_loss_paper={s['daily_loss_limit_paper_pct']:.0f}% | "
        f"live_approval={approval} | amplify={'on' if s['confidence_amplify'] else 'off'}"
    )


def _paper_thinking_safety_line() -> str:
    s = config.get_thinking_safety_summary()
    return (
        f"thinking={'on' if s['paper_thinking_enabled'] else 'off (opt-in)'} | "
        f"tilt_cap=±{s['max_sleeve_delta_pp']:.0f}% | "
        f"daily_loss={s['daily_loss_limit_paper_pct']:.0f}%"
    )


def _paper_flags() -> tuple[str, str]:
    on_line, off_line = config.format_best_paper_status_lines()
    return on_line, off_line


def _universe_line() -> str:
    try:
        from modules.dynamic_universe import screener_universe_meta

        meta = screener_universe_meta()
        if not meta.get("exists"):
            return "universe: static (no screener file)"
        age = meta.get("age_days")
        age_s = f"{age:.1f}d old" if age is not None else "unknown age"
        return f"universe: {meta.get('count', 0)} tickers | screener {age_s}"
    except Exception:
        return "universe: n/a"


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
    logger.info(_live_profile_line())
    logger.info(f"Live flags:  {_live_flags()}")
    logger.info(f"Thinking safety: {_thinking_safety_line()}")
    logger.info("Paper Profile B (Best Paper Bot v2)")
    paper_on, paper_off = _paper_flags()
    logger.info(f"Paper ON:  {paper_on}")
    logger.info(f"Paper OFF (locked): {paper_off}")
    logger.info(f"Paper thinking safety: {_paper_thinking_safety_line()}")
    logger.info(_universe_line())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
