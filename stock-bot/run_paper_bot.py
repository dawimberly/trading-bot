"""24/7 paper bot — Realistic Research v1.4 (locked default for alpaca_paper).

Uses run_all.py with PAPER_CHASE_MODE and enforce_realistic_research_profile():
  - Dynamic core VTI/SPY 30-50% (40% SPY fallback)
  - Stat Arb v1.4: 10–14 pairs, 1.6:1 RR, trailing stop, $25M liquidity filter
  - Protective + sector shorts 8-15% gross, RHYME_E exhaustion waiver
  - Runs alongside NYSE momentum (not pairs-only mode)
  - Dedicated 7% stat-arb sleeve with portfolio-vol scaling
  - Tail Risk Controls ON (vol ceiling, DD scaling, RHYME_B buffers, sector screener)
  - Friday weekly Telegram summary (after 4:30 PM ET)
Run:
    python run_paper_bot.py

Requires Alpaca **paper** keys (APCA_* + PAPER_TRADING=true) or research book
(PAPER_APCA_* + PAPER_CHASE_USE_RESEARCH_KEYS=yes).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

ROOT = Path(__file__).resolve().parent
RUN_ALL = ROOT / "run_all.py"
CRYPTO_VOL_ERROR_LOG = ROOT / "crypto_vol_sleeve_errors.log"


def _truthy(val: str | None) -> bool:
    return (val or "").strip().lower() in ("1", "true", "yes", "on")


def _crypto_vol_enabled(env: dict[str, str]) -> bool:
    return _truthy(env.get("CRYPTO_VOL_SLEEVE_ENABLED"))


def _crypto_vol_cycle_sec(env: dict[str, str]) -> int:
    raw = (
        env.get("CRYPTO_VOL_CYCLE_SEC")
        or env.get("PAPER_CHASE_CRYPTO_CYCLE_SEC")
        or "180"
    )
    try:
        return max(30, int(raw))
    except ValueError:
        return 180


def _paper_only_ok(env: dict[str, str]) -> bool:
    if _truthy(env.get("PAPER_CHASE_MODE")):
        return True
    paper = _truthy(env.get("PAPER_TRADING", "true"))
    live_allowed = _truthy(env.get("ALLOW_LIVE_TRADING"))
    return paper and not live_allowed


def _log_crypto_vol_error(exc: BaseException) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"{ts} {type(exc).__name__}: {exc}\n"
    tb = traceback.format_exc()
    if tb.strip():
        line += tb
    if not line.endswith("\n"):
        line += "\n"
    try:
        with open(CRYPTO_VOL_ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    print(f"WARNING: crypto vol sleeve error (logged to {CRYPTO_VOL_ERROR_LOG.name}): {exc}")


def _run_crypto_vol_cycle() -> None:
    from modules.crypto_vol_sleeve import run_crypto_vol_sleeve_cycle

    run_crypto_vol_sleeve_cycle(dry_run=False, paper_chase_context=True)


def _apply_paper_research_env(env: dict[str, str]) -> dict[str, str]:
    from config import apply_realistic_research_env

    env["PAPER_TRADING"] = "true"
    env["PAPER_CHASE_MODE"] = "1"
    env.setdefault("PAPER_AGGRESSIVE", "true")
    return apply_realistic_research_env(env)


def main() -> None:
    from modules.logging_utils import setup_project_logging

    setup_project_logging()
    load_dotenv(find_dotenv())

    env = os.environ.copy()
    env = _apply_paper_research_env(env)

    import config

    os.environ.update(
        {k: env[k] for k in env if k in config.REALISTIC_RESEARCH_ENV or k in (
            "PAPER_TRADING",
            "PAPER_CHASE_MODE",
            "HEARTBEAT_FILE",
            "PAPER_JOURNAL_CSV",
        )}
    )

    env.setdefault("HEARTBEAT_FILE", "paper_chase_heartbeat.json")
    env.setdefault("PAPER_JOURNAL_CSV", "paper_chase_journal.csv")

    crypto_enabled = _crypto_vol_enabled(env)
    crypto_cycle_sec = _crypto_vol_cycle_sec(env)

    python = sys.executable
    venv_py = ROOT / ".venv" / "Scripts" / "python.exe"
    if venv_py.is_file():
        python = str(venv_py)

    config.init_paper_chase_if_enabled()

    width = 72
    print("=" * width)
    print(config.format_paper_live_profile_line())
    print("=" * width)
    for line in config.format_realistic_research_startup_lines():
        print(line)
    print("-" * width)
    print(
        f"Paper bot engine: run_all.py | "
        f"Realistic Research v{config.REALISTIC_RESEARCH_VERSION} | "
        f"{config.REALISTIC_RESEARCH_TAGLINE} | deep-history indicators-only"
    )
    print(f"Heartbeat: {env['HEARTBEAT_FILE']} | Journal: {env['PAPER_JOURNAL_CSV']}")
    for line in config.paper_frequency_mode_lines():
        print(line)
    if crypto_enabled:
        if _paper_only_ok(env):
            print(
                f"--- Crypto vol sleeve: ON (every {crypto_cycle_sec}s, "
                f"errors -> {CRYPTO_VOL_ERROR_LOG.name}) ---"
            )
        else:
            print(
                "--- Crypto vol sleeve: enabled but paper-only guard failed "
                "(need PAPER_TRADING=true + no ALLOW_LIVE_TRADING, or PAPER_CHASE_MODE) "
                "— sleeve skipped ---"
            )
            crypto_enabled = False
    else:
        print("--- Crypto vol sleeve: OFF (set CRYPTO_VOL_SLEEVE_ENABLED=true) ---")

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        [python, str(RUN_ALL)],
        cwd=str(ROOT),
        env=env,
        creationflags=flags,
    )

    last_crypto = 0.0
    poll_sec = 5
    try:
        while proc.poll() is None:
            time.sleep(poll_sec)
            if not crypto_enabled or not _paper_only_ok(env):
                continue
            now = time.monotonic()
            if now - last_crypto < crypto_cycle_sec:
                continue
            last_crypto = now
            try:
                _run_crypto_vol_cycle()
            except Exception as exc:
                _log_crypto_vol_error(exc)
    except KeyboardInterrupt:
        print("\n--- Stopping paper bot ---")
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        sys.exit(130)

    sys.exit(proc.returncode if proc.returncode is not None else 0)


if __name__ == "__main__":
    main()
