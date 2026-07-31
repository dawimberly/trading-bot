"""24/7 paper bot — Realistic Research v1.5 (locked default for alpaca_paper).

Uses run_all.py with PAPER_CHASE_MODE and enforce_realistic_research_profile():
  - RVOL + ORB + Catalyst Scoring + ATR sizing (v1.5 scanners)
  - Dynamic core VTI/SPY 30–50% (63d Sharpe); 40% SPY fallback when disabled
  - Stat Arb: 10–14 pairs, 1.6:1 RR, trailing stop, $25M liquidity filter
  - Tuned protective + sector shorts (8–18% gross, partial@1:1, trail 50%/35%)
  - Insider monitor + signal boosts + risk guard (paper only)
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

ROOT = Path(__file__).resolve().parent
RUN_ALL = ROOT / "run_all.py"
CRYPTO_VOL_ERROR_LOG = ROOT / "crypto_vol_sleeve_errors.log"


def _truthy(val: str | None, default: bool = False) -> bool:
    if val is None or str(val).strip() == "":
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _maybe_spawn_weekly_review(python: str, env: dict[str, str]) -> None:
    """Saturday-only: spawn weekly_review.py in background if report missing.

    Gated by WEEKLY_REVIEW_ENABLED=true (default false). Advisory only —
    weekly_review.py never applies .env or live parameter changes.
    Prefer Windows Task Scheduler (install_weekly_review_task.ps1) so the
    report opens for viewing even when the paper bot is not restarted Saturdays.
    """
    if not _truthy(env.get("WEEKLY_REVIEW_ENABLED")):
        return
    if datetime.now().weekday() != 5:  # Saturday
        return

    today = datetime.now().date()
    last_saturday = today - timedelta(days=(today.weekday() - 5) % 7)
    out = ROOT / "data" / f"weekly_review_{last_saturday.isoformat()}.md"
    if out.is_file():
        return
    script = ROOT / "scripts" / "analysis" / "weekly_review.py"
    if not script.is_file():
        print(f"--- Weekly review skipped: missing {script.name} ---")
        return
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        subprocess.Popen(
            [python, str(script)],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        print(
            f"--- Weekly review: spawned background job "
            f"(expect {out.name}; owner approval required before any .env change) ---"
        )
    except Exception as exc:
        print(f"--- Weekly review spawn failed: {exc} ---")


def _maybe_spawn_freeze_ops(python: str, env: dict[str, str]) -> None:
    """Spawn freeze daily hygiene (once/day) and Saturday confirm/deny plan.

    Prefer Task Scheduler (install_freeze_ops_tasks.ps1) so --open works interactively.
    Measure-only; never applies .env or live changes.
    """
    if not _truthy(env.get("FREEZE_OPS_ENABLED"), True):
        return
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    today = datetime.now().date()

    if _truthy(env.get("FREEZE_DAILY_HYGIENE_ENABLED"), True):
        daily_out = ROOT / "data" / f"freeze_daily_{today.isoformat()}.md"
        daily_script = ROOT / "scripts" / "analysis" / "freeze_daily_hygiene_memo.py"
        if daily_script.is_file() and not daily_out.is_file():
            try:
                subprocess.Popen(
                    [python, str(daily_script)],
                    cwd=str(ROOT),
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=flags,
                )
                print(f"--- Freeze daily hygiene: spawned (expect {daily_out.name}) ---")
            except Exception as exc:
                print(f"--- Freeze daily hygiene spawn failed: {exc} ---")

    if datetime.now().weekday() == 5 and _truthy(
        env.get("FREEZE_WEEKLY_PLAN_ENABLED"), True
    ):
        last_sat = today - timedelta(days=(today.weekday() - 5) % 7)
        weekly_out = ROOT / "data" / f"freeze_confirm_deny_{last_sat.isoformat()}.md"
        weekly_script = ROOT / "scripts" / "analysis" / "freeze_weekly_confirm_deny.py"
        if weekly_script.is_file() and not weekly_out.is_file():
            try:
                subprocess.Popen(
                    [python, str(weekly_script)],
                    cwd=str(ROOT),
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=flags,
                )
                print(
                    f"--- Freeze weekly confirm/deny: spawned (expect {weekly_out.name}) ---"
                )
            except Exception as exc:
                print(f"--- Freeze weekly plan spawn failed: {exc} ---")


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


def _autorestart_enabled(env: dict[str, str]) -> bool:
    raw = env.get("PAPER_SUPERVISOR_AUTORESTART")
    if raw is None:
        return True
    return _truthy(raw)


def _spawn_run_all(python: str, env: dict[str, str], flags: int) -> subprocess.Popen:
    return subprocess.Popen(
        [python, str(RUN_ALL)],
        cwd=str(ROOT),
        env=env,
        creationflags=flags,
    )


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
    env = apply_realistic_research_env(env)
    # REALISTIC_RESEARCH_ENV setdefaults THINKING_ENGINE_ENABLED=false. When paper
    # thinking is opted in (env file or explicit), keep the master gate aligned so
    # effective_thinking_engine_enabled() is True (banner shows ON, not "armed").
    paper_thinking_on = _truthy(
        env.get("PAPER_THINKING_ENGINE_ENABLED")
    ) or _truthy(os.getenv("PAPER_THINKING_ENGINE_ENABLED"))
    if paper_thinking_on:
        env["PAPER_THINKING_ENGINE_ENABLED"] = "true"
        env["THINKING_ENGINE_ENABLED"] = "true"
    return env


def _paper_chase_heartbeat_path(env: dict[str, str]) -> Path:
    """Canonical paper chase heartbeat for status.py (cwd-relative)."""
    raw = (env.get("PAPER_CHASE_HEARTBEAT") or "paper_chase_heartbeat.json").strip()
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def _fetch_paper_alpaca_equity() -> float | None:
    """Current Alpaca paper account equity (research keys when configured)."""
    try:
        import config
        from modules.alpaca_client import build_trading_client, reset_trading_client_cache

        key, secret = config.get_alpaca_credentials(paper=True)
        reset_trading_client_cache()
        client = build_trading_client(key, secret, paper=True)
        acct = client.get_account()
        return float(acct.equity)
    except Exception as exc:
        print(f"WARNING: paper heartbeat equity fetch failed: {exc}", flush=True)
        return None


def _force_write_paper_chase_heartbeat(env: dict[str, str]) -> None:
    """Write a minimal fresh paper_chase_heartbeat.json every supervisor cycle.

    Portal-managed run_all may write a different HEARTBEAT_FILE path; status.py
    still reads paper_chase_heartbeat.json — keep that file fresh regardless.
    Failures are logged and never abort the supervisor loop.
    """
    try:
        from modules.safe_io import write_json_atomic

        equity = _fetch_paper_alpaca_equity()
        payload = {
            "timestamp": datetime.now().isoformat(),
            "equity": equity,
            "book": "paper",
            "paper": True,
            "status": "supervisor_pulse",
            "source": "run_paper_bot",
        }
        path = _paper_chase_heartbeat_path(env)
        write_json_atomic(str(path), payload)
    except Exception as exc:
        print(f"WARNING: paper_chase_heartbeat write failed: {exc}", flush=True)


def main() -> None:
    from modules.logging_utils import setup_project_logging

    setup_project_logging(book="paper")
    load_dotenv(find_dotenv())

    env = os.environ.copy()
    env = _apply_paper_research_env(env)

    import config

    # Portal launches often setdefault THINKING_ENGINE_ENABLED=false before dotenv.
    # Re-sync master + paper thinking flags into both subprocess env and config.
    if _truthy(env.get("PAPER_THINKING_ENGINE_ENABLED")):
        env["THINKING_ENGINE_ENABLED"] = "true"
        os.environ["PAPER_THINKING_ENGINE_ENABLED"] = "true"
        os.environ["THINKING_ENGINE_ENABLED"] = "true"
        config.PAPER_THINKING_ENGINE_ENABLED = True
        config.THINKING_ENGINE_ENABLED = True

    os.environ.update(
        {k: env[k] for k in env if k in config.REALISTIC_RESEARCH_ENV or k in (
            "PAPER_TRADING",
            "PAPER_CHASE_MODE",
            "HEARTBEAT_FILE",
            "PAPER_JOURNAL_CSV",
            "PAPER_THINKING_ENGINE_ENABLED",
            "THINKING_ENGINE_ENABLED",
        )}
    )

    env.setdefault("HEARTBEAT_FILE", "paper_chase_heartbeat.json")
    env.setdefault("PAPER_JOURNAL_CSV", "paper_chase_journal.csv")
    if env.get("PAPER_APCA_API_KEY_ID") and env.get("PAPER_APCA_API_SECRET_KEY"):
        env.setdefault("PAPER_CHASE_USE_RESEARCH_KEYS", "yes")

    crypto_enabled = _crypto_vol_enabled(env)
    crypto_cycle_sec = _crypto_vol_cycle_sec(env)

    python = sys.executable
    # Prefer a venv that can actually import trading deps. stock-bot/.venv is often
    # incomplete; the repo-root .venv is the working one.
    candidates = [
        ROOT.parent / ".venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "Scripts" / "python.exe",
        Path(sys.executable),
    ]
    for cand in candidates:
        if not cand.is_file():
            continue
        try:
            probe = subprocess.run(
                [str(cand), "-c", "import alpaca, dotenv"],
                cwd=str(ROOT),
                capture_output=True,
                timeout=20,
                check=False,
            )
            if probe.returncode == 0:
                python = str(cand)
                break
        except Exception:
            continue
    else:
        # Fall back to whatever launched the supervisor.
        python = sys.executable

    _maybe_spawn_weekly_review(python, env)
    _maybe_spawn_freeze_ops(python, env)

    config.init_paper_chase_if_enabled()

    width = 72
    print("=" * width)
    print(config.format_paper_live_profile_line())
    print("=" * width)
    for line in config.format_realistic_research_startup_lines():
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode("ascii"))
    try:
        from modules.deployment_monitor import excess_cash_warning

        import config as _cfg

        _cfg.init_paper_chase_if_enabled()
        warn = excess_cash_warning()
        if warn:
            print(f"!!! {warn} !!!")
    except Exception:
        pass
    print("-" * width)
    print(config.format_telegram_automation_banner())
    u = config.get_nyse_universe()
    print(
        f"NYSE universe: {len(u)} tickers "
        f"({'dynamic+fixed' if config.USE_DYNAMIC_UNIVERSE else 'fixed only'})"
    )
    print(
        f"Paper bot engine: run_all.py | "
        f"Realistic Research v{config.REALISTIC_RESEARCH_VERSION} | "
        f"{config.REALISTIC_RESEARCH_TAGLINE} | deep-history indicators-only"
    )
    print(f"Heartbeat: {env['HEARTBEAT_FILE']} | Journal: {env['PAPER_JOURNAL_CSV']}")
    for line in config.paper_frequency_mode_lines():
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode("ascii"))
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

    from modules.heartbeat_watchdog import EXIT_WATCHDOG

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    autorestart = _autorestart_enabled(env)
    if autorestart:
        print(
            "--- Paper supervisor: auto-restart ON "
            "(restarts engine on watchdog/crash exit) ---"
        )

    proc = _spawn_run_all(python, env, flags)
    _force_write_paper_chase_heartbeat(env)

    last_crypto = 0.0
    poll_sec = 5
    restart_count = 0
    last_restart = 0.0
    try:
        while True:
            # Force-refresh paper_chase_heartbeat.json every supervisor tick so
            # status.py never shows a stale paper heartbeat while the engine runs.
            _force_write_paper_chase_heartbeat(env)

            # Thinking engine: daily / regime-change pulse (background, never blocks).
            # Gated by PAPER_THINKING_ENGINE_ENABLED inside the helper.
            try:
                from modules.thinking_engine import maybe_pulse_paper_thinking_supervisor

                maybe_pulse_paper_thinking_supervisor()
            except Exception as exc:
                print(f"WARNING: thinking supervisor pulse failed: {exc}", flush=True)

            rc = proc.poll()
            if rc is not None:
                # Engine exited. Clean (0) or auth/fatal (1) => stop supervisor.
                if not autorestart or rc in (0, 1):
                    sys.exit(rc if rc is not None else 0)
                # Back off harder if the engine keeps dying quickly (crash loop).
                now = time.monotonic()
                if now - last_restart < 30:
                    restart_count += 1
                else:
                    restart_count = 1
                last_restart = now
                backoff = min(60, 3 * restart_count)
                reason = "watchdog stall" if rc == EXIT_WATCHDOG else f"exit {rc}"
                print(
                    f"--- Engine stopped ({reason}); restarting paper engine in "
                    f"{backoff}s (attempt {restart_count}) ---",
                    flush=True,
                )
                time.sleep(backoff)
                proc = _spawn_run_all(python, env, flags)
                continue

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


if __name__ == "__main__":
    main()
