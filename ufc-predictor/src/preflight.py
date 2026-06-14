"""Pre-flight checklist before watch mode or live alerts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

import config
from src.heartbeat import heartbeat_age_minutes, read_heartbeat
from src.model_freshness import model_needs_retrain, stale_model_warning


def run_preflight(
    *,
    profile: str | None = None,
    printer: Callable[[str], None] | None = None,
) -> int:
    """Returns 0 on pass, 1 on fail."""
    out = printer or print
    if profile:
        config.UFC_PROFILE = profile.strip().lower()
    config.apply_profile_overrides()

    def ok(msg: str) -> None:
        out(f"[OK]   {msg}")

    def warn(msg: str) -> None:
        out(f"[WARN] {msg}")

    def fail(msg: str) -> None:
        out(f"[FAIL] {msg}")

    out("\n=== UFC Predictor Pre-flight ===")
    out(f"Profile: {config.UFC_PROFILE}")
    out(f"Time:    {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n")

    passed = True

    if config.RAW_FIGHTS_CSV.is_file():
        ok(f"Fights CSV exists ({config.RAW_FIGHTS_CSV})")
    else:
        fail("No fights.csv — run: python main.py --refresh-data")
        passed = False

    if config.PROCESSED_FEATURES_CSV.is_file():
        ok(f"Features exist ({config.PROCESSED_FEATURES_CSV})")
    else:
        fail("No fight_features.csv — run: python main.py --refresh-data")
        passed = False

    if config.DEFAULT_MODEL_PATH.is_file() or config.LEGACY_MODEL_PATH.is_file():
        path = config.DEFAULT_MODEL_PATH if config.DEFAULT_MODEL_PATH.is_file() else config.LEGACY_MODEL_PATH
        ok(f"Model artifact: {path.name}")
    else:
        fail("No trained model — run: python main.py --train")
        passed = False

    needs, reason = model_needs_retrain()
    if needs:
        warn(f"Model stale: {reason}")
    else:
        stale = stale_model_warning()
        if stale:
            warn(stale)
        else:
            ok("Model fingerprint matches features")

    if config.ODDS_API_KEY:
        ok("THE_ODDS_API_KEY set (edge + alerts enabled)")
    else:
        warn("THE_ODDS_API_KEY not set — edge alerts will be empty")

    if config.DISCORD_WEBHOOK or (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        ok("Alert channel configured (Discord and/or Telegram)")
    else:
        warn("No Discord/Telegram — use --dry-run or set webhooks in .env")

    ps = config.profile_settings()
    ok(
        f"Profile caps: card {ps['max_card_risk_fraction']:.1%}, "
        f"daily loss {ps['daily_loss_limit_fraction']:.1%}, "
        f"max DD {ps['max_drawdown_fraction']:.1%}"
    )

    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ok(f"Log dir: {config.LOG_DIR}")

    hb = read_heartbeat()
    age = heartbeat_age_minutes()
    if hb:
        ok(f"Last heartbeat: {hb.get('updated_at', '?')} ({age:.0f}m ago)" if age else "Heartbeat on disk")
    else:
        ok("No heartbeat yet (normal before first --watch)")

    out("")
    if passed:
        out("RESULT: PASS — safe to run watch/alerts")
        return 0
    out("RESULT: FAIL — fix items above before production watch")
    return 1
