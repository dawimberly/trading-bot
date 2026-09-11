"""Allowlisted runtime auto-recovery for cycle errors.

Ollama may choose among skip_cycle / backoff / retry_soon / needs_human.
It never writes source, never restarts the process, never places orders.
Known Alpaca 5xx and DNS failures use a deterministic plan (no LLM wait).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import config

logger = logging.getLogger(__name__)

ALLOWED_ACTIONS = frozenset({"skip_cycle", "backoff", "retry_soon", "needs_human"})
_MIN_BACKOFF_SEC = 5.0
_TRANSIENT_RETRY_SEC = 15.0
_NETWORK_BACKOFF_SEC = 30.0
_last_tg_at: float = 0.0
_TG_COOLDOWN_SEC = 45 * 60.0

_OLLAMA_SYSTEM = (
    "You recover a live PythonTrading bot after a cycle error. "
    "Never suggest code edits, shell commands, git, or placing orders. "
    "Pick one action from: skip_cycle, backoff, retry_soon, needs_human. "
    "For Alpaca Internal Server Error / HTTP 5xx use retry_soon. "
    "For DNS/timeout use backoff. For auth failures use needs_human. "
    "Reply JSON only."
)


@dataclass
class AutofixPlan:
    action: str
    sleep_sec: float
    source: str
    reason: str
    error_class: str = "other"
    extra: dict[str, Any] = field(default_factory=dict)

    def loop_sleep_sec(self, default_cycle_sec: float) -> float:
        """Seconds run_all should sleep before the next cycle."""
        default = max(1.0, float(default_cycle_sec))
        wait = max(_MIN_BACKOFF_SEC, float(self.sleep_sec or _TRANSIENT_RETRY_SEC))
        cap = float(getattr(config, "ERROR_AUTOFIX_MAX_BACKOFF_SEC", 120) or 120)
        wait = min(wait, cap)
        if self.action == "retry_soon":
            return min(default, wait)
        if self.action == "backoff":
            return max(default, wait)
        return default


def enabled() -> bool:
    return bool(getattr(config, "ERROR_AUTOFIX_ENABLED", True))


def _clamp_sleep(sec: Any, fallback: float) -> float:
    try:
        val = float(sec)
    except (TypeError, ValueError):
        val = fallback
    cap = float(getattr(config, "ERROR_AUTOFIX_MAX_BACKOFF_SEC", 120) or 120)
    return max(_MIN_BACKOFF_SEC, min(cap, val))


def deterministic_plan(error: str, *, error_class: str | None = None) -> AutofixPlan:
    try:
        from modules import error_watcher

        klass = error_class or error_watcher.classify_error_class(error)
    except Exception:
        klass = error_class or "other"
    if klass == "transient_api":
        return AutofixPlan(
            action="retry_soon",
            sleep_sec=_TRANSIENT_RETRY_SEC,
            source="rules",
            reason="Alpaca 5xx — retry next cycle soon, do not treat as an order",
            error_class=klass,
        )
    if klass == "transient_network":
        return AutofixPlan(
            action="backoff",
            sleep_sec=_NETWORK_BACKOFF_SEC,
            source="rules",
            reason="DNS/network — back off, keep supervisor running",
            error_class=klass,
        )
    if klass == "auth":
        return AutofixPlan(
            action="needs_human",
            sleep_sec=_NETWORK_BACKOFF_SEC,
            source="rules",
            reason="Auth failure — do not auto-trade through it",
            error_class=klass,
        )
    if klass == "order_reject":
        return AutofixPlan(
            action="skip_cycle",
            sleep_sec=_TRANSIENT_RETRY_SEC,
            source="rules",
            reason="Order validation reject — skip, do not retry the same payload",
            error_class=klass,
        )
    return AutofixPlan(
        action="skip_cycle",
        sleep_sec=float(getattr(config, "CYCLE_INTERVAL_SEC", 60) or 60),
        source="rules",
        reason="Unknown cycle error — skip this cycle",
        error_class=klass,
    )


def _sanitize_ollama_plan(raw: dict[str, Any], seed: AutofixPlan) -> AutofixPlan | None:
    if not isinstance(raw, dict) or raw.get("parse_error") or raw.get("service_error"):
        return None
    action = str(raw.get("action") or "").strip().lower()
    if action not in ALLOWED_ACTIONS:
        return None
    sleep = _clamp_sleep(raw.get("backoff_sec", seed.sleep_sec), seed.sleep_sec)
    reason = str(raw.get("reason") or seed.reason)[:240]
    return AutofixPlan(
        action=action,
        sleep_sec=sleep,
        source="ollama",
        reason=reason,
        error_class=seed.error_class,
    )


def consult_ollama(error: str, seed: AutofixPlan) -> AutofixPlan | None:
    if not bool(getattr(config, "ERROR_AUTOFIX_OLLAMA", True)):
        return None
    if seed.error_class in ("transient_api", "transient_network", "auth"):
        # Known recoveries must not wait on the LLM.
        return None
    try:
        from modules.ollama_client import ollama_available, ollama_json
    except Exception:
        return None
    try:
        if not ollama_available():
            return None
    except Exception:
        return None
    timeout = int(getattr(config, "ERROR_AUTOFIX_OLLAMA_TIMEOUT_SEC", 12) or 12)
    prompt = (
        f"error_class: {seed.error_class}\n"
        f"seed_action: {seed.action}\n"
        f"seed_backoff_sec: {seed.sleep_sec}\n"
        f"error: {str(error)[:800]}\n"
        'Reply: {"action":"skip_cycle|backoff|retry_soon|needs_human",'
        '"backoff_sec":15,"reason":"short"}'
    )
    try:
        raw = ollama_json(
            prompt,
            system=_OLLAMA_SYSTEM,
            timeout_sec=timeout,
            retries=1,
        )
    except Exception as exc:
        logger.info("error_autofix Ollama consult failed: %s", exc)
        return None
    return _sanitize_ollama_plan(raw, seed)


def propose(error: str, *, error_class: str | None = None) -> AutofixPlan:
    seed = deterministic_plan(error, error_class=error_class)
    ollama_plan = consult_ollama(error, seed)
    return ollama_plan or seed


def _maybe_telegram(plan: AutofixPlan) -> None:
    if not bool(getattr(config, "ERROR_AUTOFIX_TELEGRAM", True)):
        return
    if plan.action == "needs_human":
        return
    if not bool(getattr(config, "TELEGRAM_ALERT_ERRORS", True)):
        return
    global _last_tg_at
    now = time.time()
    if now - _last_tg_at < _TG_COOLDOWN_SEC:
        return
    try:
        from modules.alerts import broadcast
    except Exception:
        return
    mode = "PAPER" if config.PAPER_TRADING else "LIVE"
    subject = f"[PythonTrading {mode}] Auto-fix {plan.action}"
    body = (
        f"{subject}\n"
        f"Class:  {plan.error_class}\n"
        f"Source: {plan.source}\n"
        f"Wait:   {plan.sleep_sec:.0f}s then next cycle\n"
        f"{plan.reason}"
    )
    try:
        if broadcast(subject, body, category="error"):
            _last_tg_at = now
    except Exception as exc:
        logger.info("error_autofix telegram failed: %s", exc)


def apply_plan(plan: AutofixPlan, *, error: str = "") -> AutofixPlan:
    try:
        from modules import error_watcher

        error_watcher.log_action(
            "autofix",
            action=plan.action,
            source=plan.source,
            error_class=plan.error_class,
            sleep_sec=plan.sleep_sec,
            reason=plan.reason[:240],
            error=str(error)[:400],
        )
    except Exception:
        pass
    logger.warning(
        "AUTOFIX %s via %s (class=%s sleep=%.0fs): %s",
        plan.action,
        plan.source,
        plan.error_class,
        plan.sleep_sec,
        plan.reason[:160],
    )
    _maybe_telegram(plan)
    return plan


def handle_cycle_error(error: str, *, error_class: str | None = None) -> AutofixPlan:
    """Classify, optionally consult Ollama, log, Telegram. Safe no-op if disabled."""
    if not enabled():
        return AutofixPlan(
            action="skip_cycle",
            sleep_sec=float(getattr(config, "CYCLE_INTERVAL_SEC", 60) or 60),
            source="disabled",
            reason="ERROR_AUTOFIX_ENABLED=false",
            error_class=error_class or "other",
        )
    plan = propose(error, error_class=error_class)
    return apply_plan(plan, error=error)
