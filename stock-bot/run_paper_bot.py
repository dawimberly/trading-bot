"""24/7 paper Sharpe-chase bot — Best Paper Bot stack, isolated from live ~$100.

Uses run_all.py with PAPER_CHASE_MODE and enforce_best_paper_stack():
  - Dynamic VTI (40-75%), dynamic risk (1-3%), original stat arb, vol overlay, options
  - Overlap filter, NYSE conditional-on-SPY, adaptive chunk, co-fire budget
  - Thinking engine opt-in (PAPER_THINKING_ENGINE_ENABLED=true + Ollama)
  - Locked OFF: macro regime, risk parity, stat arb optimized, social, equity pairs, SPY MA exit

Run:
    python run_paper_bot.py

Requires Alpaca **paper** keys (APCA_* + PAPER_TRADING=true) or research book
(PAPER_APCA_* + PAPER_CHASE_USE_RESEARCH_KEYS=yes).

Backtests peak ~1.0–1.8 Sharpe by window — 3.0 is the chase target, not proven.
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


def main() -> None:
    from modules.logging_utils import setup_project_logging

    setup_project_logging()
    load_dotenv(find_dotenv())
    os.environ["PAPER_TRADING"] = "true"
    os.environ["PAPER_CHASE_MODE"] = "1"

    env = os.environ.copy()
    env.setdefault("PAPER_AGGRESSIVE", "true")
    env.setdefault("HEARTBEAT_FILE", "paper_chase_heartbeat.json")
    env.setdefault("PAPER_JOURNAL_CSV", "paper_chase_journal.csv")

    crypto_enabled = _crypto_vol_enabled(env)
    crypto_cycle_sec = _crypto_vol_cycle_sec(env)

    python = sys.executable
    venv_py = ROOT / ".venv" / "Scripts" / "python.exe"
    if venv_py.is_file():
        python = str(venv_py)

    print("--- Paper Sharpe chase (run_all.py + PAPER_CHASE_MODE) ---")
    print(f"--- Heartbeat: {env['HEARTBEAT_FILE']} | Journal: {env['PAPER_JOURNAL_CSV']} ---")
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
