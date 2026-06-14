"""Production watch loop: poll upcoming card and alert on new value bets."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import pandas as pd

import config
from src.alerts import (
    alert_fingerprint,
    dispatch_alerts,
    format_alert_text,
    generate_alerts,
    should_send_alert,
)
from src.bet_journal import log_watch_tick
from src.heartbeat import write_heartbeat
from src.logging_utils import log_event
from src.risk_manager import check_bankroll_safety

logger = logging.getLogger(__name__)


def run_prediction_cycle(
    *,
    event_index: int = 0,
    refresh_data: bool = False,
    use_odds: bool = True,
    explain: bool = False,
    event_name: str | None = None,
    use_cache: bool = True,
) -> tuple[pd.DataFrame, str]:
    """Load card, score fights (cached), return predictions + event label."""
    from src.card_cache import predict_card_cached
    from src.data_loader import get_upcoming_card, load_fights
    from src.predictor import merge_predictions_with_odds

    card = get_upcoming_card(event_index=event_index, force_refresh=refresh_data)
    fights = load_fights()
    name = event_name or "Upcoming card"
    if "event_name" in card.columns and card["event_name"].notna().any():
        name = str(card["event_name"].dropna().iloc[0])
    elif "event" in card.columns and card["event"].notna().any():
        name = str(card["event"].dropna().iloc[0])

    preds = predict_card_cached(
        card,
        fights,
        name,
        explain=explain,
        use_cache=use_cache,
    )
    if use_odds and config.ODDS_API_KEY and "odds_matched" not in preds.columns:
        preds = merge_predictions_with_odds(preds, force_refresh=refresh_data)

    if "event_name" in preds.columns and preds["event_name"].notna().any():
        name = str(preds["event_name"].dropna().iloc[0])
    return preds, name


def apply_quick_odds_to_predictions(
    preds: pd.DataFrame,
    *,
    event_name: str = "",
) -> pd.DataFrame:
    """Fast BetNow + DraftKings refresh; returns preds with updated odds/edges."""
    from src.dashboard_service import run_quick_odds_refresh

    result = run_quick_odds_refresh(preds, event_label=event_name)
    overview = result.get("books", {}).get("Overview", {})
    updated = overview.get("predictions")
    return updated if isinstance(updated, pd.DataFrame) and not updated.empty else preds


def watch_cycle(
    *,
    last_fingerprint: str | None,
    event_name: str | None = None,
    min_edge: float | None = None,
    explain: bool = False,
    preds: pd.DataFrame | None = None,
    bankroll: float | None = None,
) -> tuple[dict[str, Any], str | None, bool, str]:
    """
    One watch iteration: generate alerts and decide if notification is new.

    Returns (alert_data, new_fingerprint, should_notify, block_reason).
    """
    if preds is None:
        preds, event_name = run_prediction_cycle(explain=explain)

    bankroll = bankroll or config.INITIAL_BANKROLL
    allowed, block_reason = check_bankroll_safety(bankroll)
    if not allowed:
        alert_data = {
            "available": False,
            "event_name": event_name,
            "block_reason": block_reason,
            "singles_count": 0,
            "parlays_count": 0,
        }
        return alert_data, last_fingerprint, False, block_reason

    alert_data = generate_alerts(
        preds,
        min_edge=min_edge,
        event_name=event_name,
        bankroll=bankroll,
    )
    fp = alert_fingerprint(alert_data)
    is_new = fp != last_fingerprint
    ok, reason = should_send_alert(alert_data)
    should_notify = alert_data.get("available") and is_new and ok
    if not ok and is_new:
        logger.debug("Watch: alert blocked — %s", reason)
    return alert_data, fp, should_notify, reason if not ok else ""


def watch_loop(
    *,
    poll_minutes: int | None = None,
    event_index: int = 0,
    event_name: str | None = None,
    refresh_data: bool = False,
    use_odds: bool = True,
    explain: bool = False,
    discord: bool = False,
    telegram: bool = False,
    dry_run: bool = False,
    min_edge: float | None = None,
    max_iterations: int | None = None,
    bankroll: float | None = None,
    on_alert: Callable[[dict[str, Any]], None] | None = None,
    auto_odds: bool = False,
    card_check_minutes: int | None = None,
    odds_refresh_minutes: int | None = None,
) -> None:
    """
    Poll upcoming event; alert only on new value bets.

    With ``auto_odds``, card/feature checks use ``card_check_minutes`` (default 45)
    and BetNow + DraftKings odds refresh every ``odds_refresh_minutes`` (default 12).

    Blocks until KeyboardInterrupt or ``max_iterations`` reached.
    """
    from src.dashboard_service import detect_card_change
    from src.preflight import run_preflight

    pf = run_preflight(profile=config.UFC_PROFILE, printer=logger.info)
    if pf != 0:
        logger.error("Pre-flight failed — fix issues before watch mode.")
        raise SystemExit(pf)

    card_poll = card_check_minutes or config.WATCH_CARD_CHECK_MINUTES
    odds_poll = odds_refresh_minutes or config.WATCH_AUTO_ODDS_MINUTES
    poll = poll_minutes or (odds_poll if auto_odds else config.ALERT_POLL_MINUTES)
    bankroll = bankroll or config.INITIAL_BANKROLL
    log_event(
        "watch_start",
        profile=config.UFC_PROFILE,
        poll_minutes=poll,
        dry_run=dry_run,
        auto_odds=auto_odds,
        card_check_minutes=card_poll,
        odds_refresh_minutes=odds_poll if auto_odds else None,
    )
    logger.info(
        "Watch mode — profile=%s | dry_run=%s | auto_odds=%s | card=%sm | odds=%sm",
        config.UFC_PROFILE,
        dry_run,
        auto_odds,
        card_poll,
        odds_poll if auto_odds else poll,
    )

    last_fp: str | None = None
    iteration = 0
    cached_preds: pd.DataFrame | None = None
    cached_event = event_name or ""
    last_card_check = 0.0
    last_odds_refresh = 0.0
    tick_seconds = 60

    def _dispatch_tick(
        preds: pd.DataFrame,
        ev_name: str,
        *,
        tick_kind: str,
    ) -> None:
        nonlocal last_fp, iteration
        iteration += 1
        block_reason = ""
        try:
            alert_data, fp, notify, block_reason = watch_cycle(
                last_fingerprint=last_fp,
                event_name=ev_name,
                min_edge=min_edge,
                explain=explain,
                preds=preds,
                bankroll=bankroll,
            )
            write_heartbeat(
                status="ok",
                event_name=ev_name,
                iteration=iteration,
                singles_count=alert_data.get("singles_count", 0),
                parlays_count=alert_data.get("parlays_count", 0),
                last_alert_sent=notify,
                block_reason=block_reason,
                extra={"profile": config.UFC_PROFILE, "tick_kind": tick_kind},
            )
            log_watch_tick(
                iteration=iteration,
                event_name=ev_name,
                singles=alert_data.get("singles_count", 0),
                parlays=alert_data.get("parlays_count", 0),
                notified=notify,
                block_reason=block_reason,
            )
            if notify:
                print(format_alert_text(alert_data))
                if on_alert:
                    on_alert(alert_data)
                status = dispatch_alerts(
                    alert_data,
                    discord=discord,
                    telegram=telegram,
                    dry_run=dry_run,
                    respect_cooldown=not dry_run,
                )
                if status.get("skipped"):
                    logger.info("Dispatch skipped: %s", status.get("skip_reason"))
                elif status.get("sent") or dry_run:
                    logger.info(
                        "Alert dispatched (discord=%s telegram=%s)",
                        status["discord"],
                        status["telegram"],
                    )
                last_fp = fp
            else:
                logger.info(
                    "Watch %s #%s — no new alerts (%s singles, %s parlays)%s",
                    tick_kind,
                    iteration,
                    alert_data.get("singles_count", 0),
                    alert_data.get("parlays_count", 0),
                    f" [{block_reason}]" if block_reason else "",
                )
        except Exception as exc:
            logger.error("Watch cycle error (%s): %s", tick_kind, exc)
            write_heartbeat(status="error", iteration=iteration, block_reason=str(exc))

    while True:
        now = time.time()
        due_card = cached_preds is None or (now - last_card_check) >= card_poll * 60
        due_odds = (
            auto_odds
            and cached_preds is not None
            and (now - last_odds_refresh) >= odds_poll * 60
        )
        due_legacy = not auto_odds and (cached_preds is None or (now - last_card_check) >= poll * 60)

        try:
            if due_card or due_legacy:
                last_card_check = now
                if auto_odds:
                    changed, ev_name, _ = detect_card_change(event_index=event_index)
                    if cached_preds is not None and not changed:
                        logger.info("Card unchanged (%s) — skipping feature engineering", ev_name)
                    else:
                        if changed and cached_preds is not None:
                            logger.info("New card detected: %s — incremental feature run", ev_name)
                        preds, ev_name = run_prediction_cycle(
                            event_index=event_index,
                            refresh_data=refresh_data or changed,
                            use_odds=False,
                            explain=explain,
                            event_name=event_name,
                            use_cache=True,
                        )
                        cached_preds = preds
                        cached_event = ev_name
                        cached_preds = apply_quick_odds_to_predictions(
                            cached_preds,
                            event_name=cached_event,
                        )
                        last_odds_refresh = now
                        _dispatch_tick(cached_preds, cached_event, tick_kind="card")
                else:
                    preds, ev_name = run_prediction_cycle(
                        event_index=event_index,
                        refresh_data=refresh_data,
                        use_odds=use_odds,
                        explain=explain,
                        event_name=event_name,
                        use_cache=True,
                    )
                    cached_preds = preds
                    cached_event = ev_name
                    _dispatch_tick(cached_preds, cached_event, tick_kind="poll")

            elif due_odds:
                last_odds_refresh = now
                logger.info("Quick odds refresh (%s)…", cached_event)
                cached_preds = apply_quick_odds_to_predictions(
                    cached_preds,
                    event_name=cached_event,
                )
                _dispatch_tick(cached_preds, cached_event, tick_kind="odds")

        except Exception as exc:
            logger.error("Watch loop error: %s", exc)
            write_heartbeat(status="error", iteration=iteration, block_reason=str(exc))

        if max_iterations and iteration >= max_iterations:
            logger.info("Watch loop finished after %s iterations.", iteration)
            break

        try:
            time.sleep(tick_seconds)
        except KeyboardInterrupt:
            logger.info("Watch mode stopped.")
            log_event("watch_stop", iteration=iteration)
            break
