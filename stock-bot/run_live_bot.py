"""24/7 live bot supervisor — Live Conservative (alpaca_live).

Wraps ``run_all.py`` the same way ``run_paper_bot.py`` wraps paper:
  - Restarts the engine on watchdog stall (exit 42) or unexpected crash
  - Does NOT restart on clean exit (0) or auth/fatal (1)
  - Pulses a fresh live heartbeat so status never looks stale while supervised

Live profile (enforced inside run_all when PAPER_TRADING=false):
  - High VTI core + small SPY trend sleeve
  - Research sleeves OFF (stat arb / shorts / RVOL-ORB / etc.)

Run (normally via portal / dashboard):
    python run_live_bot.py

Requires live Alpaca keys with PAPER_TRADING=false and ALLOW_LIVE_TRADING=yes
(portal book ``alpaca_live`` .env supplies these).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

ROOT = Path(__file__).resolve().parent
RUN_ALL = ROOT / "run_all.py"

# File overlay is allowlist-only. Never copy paper/research flags onto live.
_LIVE_ENV_DENY_EXACT = frozenset(
    {
        "PAPER_TRADING",
        "PAPER_CHASE_MODE",
        "PAPER_NYSE_MAX_ADDS_PER_SYMBOL",
        "PAPER_NYSE_ENTRY_HYGIENE_ENABLED",
        "PAPER_NYSE_SAME_DAY_REENTRY_BLOCK",
        "PAPER_NYSE_ATR_STOP_SLEEVE_COOLDOWN",
        "PAPER_NYSE_MIN_NOTIONAL",
        "PAPER_NYSE_PER_NAME_MAX_PCT",
        "PAPER_NYSE_MAX_OF_SLEEVE_PCT",
        "PAPER_MAX_EQUITY_TRADES",
    }
)
_LIVE_ENV_DENY_PREFIXES = ("PAPER_", "RESEARCH_")
_LIVE_ENV_ALLOW_EXACT = frozenset(
    {
        "ALLOW_LIVE_TRADING",
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
    }
)
_LIVE_ENV_ALLOW_PREFIXES = (
    "LIVE_",
    "TELEGRAM_",
    "ALPACA_",
    "SMTP_",
    "TAVILY_",
    "XAI_",
    "ERROR_WATCHER_",
)


def live_env_key_allowed(key: str) -> bool:
    """True if stock-bot/.env may overwrite inherited parent env for live."""
    k = str(key or "").strip()
    if not k:
        return False
    if k in _LIVE_ENV_DENY_EXACT:
        return False
    if k.startswith(_LIVE_ENV_DENY_PREFIXES):
        return False
    if k in _LIVE_ENV_ALLOW_EXACT:
        return True
    return any(k.startswith(p) for p in _LIVE_ENV_ALLOW_PREFIXES)


def overlay_live_stock_env(
    env: dict[str, str], path: Path | None = None
) -> dict[str, str]:
    """Apply allowed stock-bot/.env keys over *env*. File beats parent for those keys.

    Does not copy PAPER_TRADING / PAPER_* / RESEARCH_* / paper hygiene.
    """
    path = path if path is not None else ROOT / ".env"
    if not path.is_file():
        return env
    from dotenv import dotenv_values

    out = dict(env)
    for key, val in dotenv_values(path).items():
        if not key or val is None:
            continue
        if not live_env_key_allowed(key):
            continue
        out[str(key)] = str(val)
    return out


def _truthy(val: str | None, default: bool = False) -> bool:
    if val is None or str(val).strip() == "":
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _load_env() -> dict[str, str]:
    # Prefer already-injected portal env; fall back to local .env for missing keys.
    if not os.getenv("PORTAL_BOT_LAUNCH"):
        load_dotenv(find_dotenv(usecwd=True), override=False)
        local = ROOT / ".env"
        if local.is_file():
            load_dotenv(local, override=False)
    env = {k: str(v) for k, v in os.environ.items()}
    # Allowed file keys beat a stale dashboard parent env (not a full override=True).
    env = overlay_live_stock_env(env)
    for key, val in env.items():
        if live_env_key_allowed(key):
            os.environ[key] = val
    return env


def _autorestart_enabled(env: dict[str, str]) -> bool:
    return _truthy(env.get("LIVE_BOT_AUTORESTART"), True)


def _apply_live_env(env: dict[str, str]) -> dict[str, str]:
    """Hard-set live book guards (portal may already set these)."""
    env = dict(env)
    env["PAPER_TRADING"] = "false"
    env["ALLOW_LIVE_TRADING"] = "yes"
    env["PAPER_CHASE_MODE"] = "0"
    # Live cycles can exceed paper's old 300s when data/regime work is heavy.
    env.setdefault("HEARTBEAT_WATCHDOG_ENABLED", "true")
    env.setdefault("HEARTBEAT_WATCHDOG_TIMEOUT_SEC", "900")
    env.setdefault("LIVE_CONSERVATIVE_ENABLED", "true")
    return env


def _python() -> str:
    return sys.executable


def _spawn_run_all(python: str, env: dict[str, str], flags: int) -> subprocess.Popen:
    if not RUN_ALL.is_file():
        raise FileNotFoundError(f"Missing {RUN_ALL}")
    return subprocess.Popen(
        [python, "-u", str(RUN_ALL)],
        cwd=str(ROOT),
        env=env,
        creationflags=flags,
    )


def _live_heartbeat_path(env: dict[str, str]) -> Path:
    raw = (env.get("HEARTBEAT_FILE") or "").strip()
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else ROOT / path
    # Portal books set HEARTBEAT_FILE; fallback matches health_check live candidates.
    portal = ROOT / "data" / "portal" / "users"
    if portal.is_dir():
        hits = sorted(portal.glob("*/books/alpaca_live/bot_heartbeat.json"))
        if hits:
            return hits[0]
    return ROOT / "live_bot_heartbeat.json"


def _fetch_live_equity() -> float | None:
    try:
        import config
        from modules.alpaca_client import build_trading_client, reset_trading_client_cache

        key, secret = config.get_alpaca_credentials(paper=False)
        reset_trading_client_cache()
        client = build_trading_client(key, secret, paper=False)
        acct = client.get_account()
        return float(acct.equity)
    except Exception as exc:
        print(f"WARNING: live heartbeat equity fetch failed: {exc}", flush=True)
        return None


def _read_json(path: Path) -> dict:
    try:
        import json

        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _force_write_live_heartbeat(env: dict[str, str]) -> None:
    """Pulse timestamp/equity only — never wipe regime/gates from run_all."""
    try:
        from modules.safe_io import write_json_atomic

        equity = _fetch_live_equity()
        payload = {
            "timestamp": datetime.now().isoformat(),
            "equity": equity,
            "book": "live",
            "paper": False,
            "status": "supervisor_pulse",
            "source": "run_live_bot",
        }
        path = _live_heartbeat_path(env)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Portal path used to be replaced wholesale, which blanked regime /
        # entry_skip_reason on the dashboard every 5s. Merge like the cwd path.
        existing = _read_json(path)
        cwd_hb = ROOT / "bot_heartbeat.json"
        cwd_existing = _read_json(cwd_hb)
        if (
            not existing.get("regime")
            and cwd_existing.get("paper") is not True
            and cwd_existing.get("regime")
        ):
            # Portal was wiped by an older thin pulse — restore from cwd live hb.
            seed = dict(cwd_existing)
            seed.update(existing)
            existing = seed

        merged = dict(existing)
        merged.update(payload)
        if equity is None and existing.get("equity") is not None:
            merged["equity"] = existing.get("equity")
        write_json_atomic(str(path), merged)

        try:
            from modules import error_watcher

            error_watcher.warn_incomplete_heartbeat(
                merged, path=str(path), book="live"
            )
        except Exception:
            pass

        # Also refresh cwd copy used by some status tools
        try:
            if cwd_existing.get("paper") is True:
                pass  # don't overwrite paper book marker if mis-pointed
            else:
                cwd_merged = dict(cwd_existing)
                cwd_merged.update(payload)
                if equity is None and cwd_existing.get("equity") is not None:
                    cwd_merged["equity"] = cwd_existing.get("equity")
                write_json_atomic(str(cwd_hb), cwd_merged)
        except Exception:
            write_json_atomic(str(cwd_hb), merged)
    except Exception as exc:
        print(f"WARNING: live heartbeat write failed: {exc}", flush=True)


def main() -> None:
    env = _apply_live_env(_load_env())
    if _truthy(env.get("PAPER_TRADING")):
        print(
            "FATAL: run_live_bot requires PAPER_TRADING=false "
            "(use run_paper_bot for paper).",
            flush=True,
        )
        sys.exit(1)
    if not _truthy(env.get("ALLOW_LIVE_TRADING")):
        print("FATAL: ALLOW_LIVE_TRADING must be yes for live bot.", flush=True)
        sys.exit(1)

    print("=== Live bot supervisor (Live Conservative) ===", flush=True)
    print(f"  python: {_python()}", flush=True)
    print(f"  engine: {RUN_ALL.name}", flush=True)
    print(
        f"  watchdog timeout: {env.get('HEARTBEAT_WATCHDOG_TIMEOUT_SEC')}s "
        f"(autorestart={_autorestart_enabled(env)})",
        flush=True,
    )

    from modules.heartbeat_watchdog import EXIT_WATCHDOG

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    autorestart = _autorestart_enabled(env)
    python = _python()
    proc = _spawn_run_all(python, env, flags)
    _force_write_live_heartbeat(env)

    poll_sec = 5
    restart_count = 0
    last_restart = 0.0
    try:
        while True:
            _force_write_live_heartbeat(env)
            rc = proc.poll()
            if rc is not None:
                if not autorestart or rc in (0, 1):
                    sys.exit(rc if rc is not None else 0)
                now = time.monotonic()
                if now - last_restart < 30:
                    restart_count += 1
                else:
                    restart_count = 1
                last_restart = now
                backoff = min(60, 3 * restart_count)
                reason = "watchdog stall" if rc == EXIT_WATCHDOG else f"exit {rc}"
                print(
                    f"--- Engine stopped ({reason}); restarting live engine in "
                    f"{backoff}s (attempt {restart_count}) ---",
                    flush=True,
                )
                time.sleep(backoff)
                proc = _spawn_run_all(python, env, flags)
                continue
            time.sleep(poll_sec)
    except KeyboardInterrupt:
        print("\n--- Stopping live bot ---", flush=True)
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        sys.exit(130)


if __name__ == "__main__":
    main()
