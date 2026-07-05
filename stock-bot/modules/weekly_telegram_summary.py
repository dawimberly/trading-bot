"""Friday after-close weekly summary via Telegram."""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from modules import alerts
from modules.weekly_summary import (
    WeeklySummaryData,
    format_weekly_telegram_message,
    gather_weekly_summary,
    week_id,
)

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")


def _parse_summary_time_et() -> tuple[int, int]:
    raw = (getattr(config, "TELEGRAM_WEEKLY_SUMMARY_TIME", None) or "16:30").strip()
    try:
        hour_s, minute_s = raw.split(":", 1)
        return int(hour_s), int(minute_s)
    except (ValueError, TypeError):
        return 16, 30


def weekly_telegram_due(*, market_open: bool | None = None, test_mode: bool = False) -> bool:
    """True on Friday after TELEGRAM_WEEKLY_SUMMARY_TIME once market is closed."""
    if test_mode:
        return True
    if not config.telegram_weekly_summary_enabled():
        return False
    now_et = datetime.now(_ET)
    if now_et.weekday() != 4:
        return False
    target_h, target_m = _parse_summary_time_et()
    if (now_et.hour, now_et.minute) < (target_h, target_m):
        return False
    if market_open is True:
        return False
    state = alerts._load_state()
    return state.get("last_weekly_telegram_summary_week") != week_id(now_et.date())


def _mark_weekly_sent() -> None:
    state = alerts._load_state()
    state["last_weekly_telegram_summary_week"] = week_id(datetime.now(_ET).date())
    state["last_weekly_telegram_summary_at"] = datetime.now().isoformat()
    alerts._save_state(state)


def send_weekly_telegram_summary(
    *,
    test_mode: bool = False,
    dry_run: bool = False,
    equity: float | None = None,
    cash: float | None = None,
    regime: str = "",
    wisdom: dict | None = None,
    sleeves: dict | None = None,
    market_open: bool | None = None,
) -> bool:
    """Send Friday weekly summary to Telegram (or test_mode / dry_run)."""
    if not config.telegram_weekly_summary_enabled() and not test_mode:
        return False
    if not weekly_telegram_due(market_open=market_open, test_mode=test_mode or dry_run):
        return False

    tg = config.get_telegram_config()
    if not dry_run and not tg:
        msg = "Weekly Telegram skipped: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set."
        logger.warning(msg)
        print(f"--- {msg} ---")
        return False

    data = gather_weekly_summary(
        equity=equity,
        cash=cash,
        regime=regime,
        wisdom=wisdom,
        sleeves=sleeves,
    )
    message = format_weekly_telegram_message(data)
    if config.paper_chase_mode_enabled():
        try:
            from modules.weekly_report import format_telegram_research_addon, gather_weekly_report

            report = gather_weekly_report(
                equity=equity,
                cash=cash,
                regime=regime,
                wisdom=wisdom,
                sleeves=sleeves,
            )
            message += format_telegram_research_addon(report)
        except Exception as exc:
            logger.debug("Telegram research addon skipped: %s", exc)

    if dry_run:
        print(message)
        return True

    if alerts.send_telegram(message):
        if not test_mode:
            _mark_weekly_sent()
        line = (
            f"Weekly Telegram summary SENT | {data.account_label} | "
            f"equity ${data.equity:,.2f} | week {data.week_return_pct or 0:+.1f}%"
        )
        logger.info(line)
        print(f"--- {line} ---")
        return True

    print("--- Weekly Telegram summary FAILED ---")
    return False


def maybe_weekly_telegram_summary(
    *,
    equity: float,
    cash: float,
    regime: str = "",
    wisdom: dict | None = None,
    sleeves: dict | None = None,
    market_open: bool | None = None,
    force: bool = False,
) -> bool:
    """Wrapper used by run_all.py."""
    return send_weekly_telegram_summary(
        test_mode=force,
        equity=equity,
        cash=cash,
        regime=regime,
        wisdom=wisdom,
        sleeves=sleeves,
        market_open=market_open,
    )
