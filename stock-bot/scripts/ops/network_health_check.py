#!/usr/bin/env python3
"""Read-only Alpaca DNS / TCP / clock health check (paper-api focus).

No orders. No strategy changes. Use after transient_network errors to see if
paper-api.alpaca.markets is reachable again.

Run from stock-bot/:
  python scripts/ops/network_health_check.py

Exit codes:
  0 = DNS + TCP + clock OK          → NETWORK HEALTHY
  1 = auth failure                  → AUTH FAIL
  2 = DNS/network failure           → NETWORK DOWN
  3 = partial (e.g. DNS OK, clock other failure)
"""

from __future__ import annotations

import json
import os
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("PYTHONTRADING_ROOT", str(ROOT))

PAPER_HOST = "paper-api.alpaca.markets"
LIVE_HOST = "api.alpaca.markets"
TCP_PORT = 443
TCP_TIMEOUT_SEC = 8.0
BOT_ERRORS_PATH = ROOT / "logs" / "bot_errors.jsonl"


def _resolve_paper_env_file() -> Path | None:
    try:
        from modules.portal_paths import (
            bind_project_root,
            book_env_path,
            get_last_username,
            has_alpaca_config,
        )

        bind_project_root(ROOT)
        username = (get_last_username() or "").strip().lower()
        portal = ROOT / "data" / "portal" / "users"
        candidates: list[Path] = []
        if username and has_alpaca_config(username, "alpaca_paper"):
            candidates.append(book_env_path(username, "alpaca_paper"))
        if portal.is_dir():
            candidates.extend(portal.glob("*/books/alpaca_paper/.env"))
        existing = [p for p in candidates if p.is_file()]
        if existing:
            return max(existing, key=lambda p: p.stat().st_mtime)
    except Exception:
        pass
    stock = ROOT / ".env"
    return stock if stock.is_file() else None


_paper_env = _resolve_paper_env_file()
if _paper_env is not None:
    os.environ.setdefault("PYTHONTRADING_ENV_FILE", str(_paper_env))
os.environ.setdefault("PAPER_TRADING", "true")

import config  # noqa: E402

# Re-assert paper after dotenv (root .env may pin differently).
os.environ["PAPER_TRADING"] = "true"
config.PAPER_TRADING = True


def _ok(label: str, detail: str = "") -> None:
    suffix = f" - {detail}" if detail else ""
    print(f"  {label}: OK{suffix}")


def _fail(label: str, detail: str = "") -> None:
    suffix = f" - {detail}" if detail else ""
    print(f"  {label}: FAIL{suffix}")


def check_dns(host: str) -> tuple[bool, list[str]]:
    """Resolve host; return (ok, address strings)."""
    try:
        infos = socket.getaddrinfo(host, TCP_PORT, type=socket.SOCK_STREAM)
        addrs: list[str] = []
        seen: set[str] = set()
        for info in infos:
            sockaddr = info[4]
            ip = str(sockaddr[0])
            if ip not in seen:
                seen.add(ip)
                addrs.append(ip)
        return True, addrs
    except OSError as exc:
        return False, [str(exc)]


def check_tcp(host: str, port: int = TCP_PORT, timeout: float = TCP_TIMEOUT_SEC) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{host}:{port}"
    except OSError as exc:
        return False, str(exc)


def check_alpaca_clock() -> tuple[str, dict[str, Any]]:
    """Return (class, details) where class is ok|transient_network|auth|other."""
    from modules.alpaca_client import (
        AlpacaAuthError,
        AlpacaTransientNetworkError,
        call_with_retry,
        clear_network_backoff,
        get_trading_client,
        is_transient_network_error,
    )

    clear_network_backoff()
    try:
        client = get_trading_client(paper=True)
        clock = call_with_retry(client.get_clock, op_name="get_clock")
        ts = getattr(clock, "timestamp", None)
        is_open = bool(getattr(clock, "is_open", False))
        return "ok", {
            "is_open": is_open,
            "timestamp": str(ts) if ts is not None else None,
        }
    except AlpacaAuthError as exc:
        return "auth", {"error": str(exc)[:300]}
    except AlpacaTransientNetworkError as exc:
        return "transient_network", {"error": str(exc)[:300]}
    except Exception as exc:
        if is_transient_network_error(exc):
            return "transient_network", {"error": str(exc)[:300]}
        return "other", {"error": f"{type(exc).__name__}: {exc}"[:300]}


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        text = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def summarize_bot_errors(path: Path = BOT_ERRORS_PATH, *, hours: float = 1.0) -> None:
    print("\n--- Recent bot_errors (last hour) ---")
    if not path.is_file():
        print(f"  (no file: {path})")
        return

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        print(f"  (read failed: {exc})")
        return

    for line in lines[-20:]:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)

    if not rows:
        print("  (no recent rows in last 20 lines)")
        return

    # Prefer error_watcher classifier when available.
    try:
        from modules.error_watcher import classify_error_class
    except Exception:
        classify_error_class = None  # type: ignore[assignment]

    def _klass(row: dict[str, Any]) -> str:
        explicit = str(row.get("error_class") or "").strip()
        if explicit:
            return explicit
        if classify_error_class is not None:
            return classify_error_class(
                row.get("error"),
                error_type=str(row.get("error_type") or ""),
                reason=str(row.get("reason") or ""),
            )
        return "other"

    in_window = []
    for row in rows:
        ts = _parse_ts(row.get("ts"))
        if ts is None or ts >= cutoff:
            in_window.append(row)

    counts = {"transient_network": 0, "auth": 0, "other": 0}
    for row in in_window:
        k = _klass(row)
        if k == "transient_network":
            counts["transient_network"] += 1
        elif k == "auth":
            counts["auth"] += 1
        else:
            counts["other"] += 1

    print(
        f"  last_20_lines scanned={len(rows)} | in_last_hour={len(in_window)} | "
        f"transient_network={counts['transient_network']} "
        f"auth={counts['auth']} other={counts['other']}"
    )

    newest = rows[-1]
    nts = _parse_ts(newest.get("ts"))
    age = ""
    if nts is not None:
        age_sec = max(0.0, (datetime.now(timezone.utc) - nts).total_seconds())
        if age_sec < 120:
            age = f"{age_sec:.0f}s ago"
        elif age_sec < 7200:
            age = f"{age_sec / 60:.0f}m ago"
        else:
            age = f"{age_sec / 3600:.1f}h ago"
    print(
        f"  newest: error_class={_klass(newest)} age={age or '?'} "
        f"event={newest.get('event') or '?'} "
        f"err={str(newest.get('error') or '')[:120]}"
    )


def summarize_paper_heartbeat() -> None:
    print("\n--- Paper heartbeat ---")
    path: Path | None = None
    portal = ROOT / "data" / "portal" / "users"
    portal_hbs: list[Path] = []
    if portal.is_dir():
        portal_hbs = [
            p for p in portal.glob("*/books/alpaca_paper/bot_heartbeat.json") if p.is_file()
        ]
    if portal_hbs:
        path = max(portal_hbs, key=lambda p: p.stat().st_mtime)
    else:
        try:
            from modules.health_check import resolve_paper_heartbeat_path

            cand = resolve_paper_heartbeat_path(ROOT)
            if cand is not None and cand.is_file():
                path = cand
        except Exception:
            path = None

    if path is None or not path.is_file():
        print("  (no paper heartbeat found)")
        return

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  path={path}")
        print(f"  (unreadable: {exc})")
        return

    ts = _parse_ts(payload.get("timestamp") or payload.get("ts"))
    age = "?"
    if ts is not None:
        if ts.tzinfo is None:
            age_sec = max(0.0, (datetime.now() - ts).total_seconds())
        else:
            age_sec = max(
                0.0,
                (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds(),
            )
        if age_sec < 120:
            age = f"{age_sec:.0f}s"
        elif age_sec < 7200:
            age = f"{age_sec / 60:.0f}m"
        else:
            age = f"{age_sec / 3600:.1f}h"

    status = payload.get("status") or "-"
    err_class = payload.get("last_cycle_error_class") or "-"
    err = str(payload.get("last_cycle_error") or "")[:100] or "-"
    print(f"  path={path}")
    print(
        f"  age={age} status={status} "
        f"error_class={err_class} "
        f"last_cycle_error={err}"
    )


def main() -> int:
    env_note = str(_paper_env) if _paper_env else "(none)"
    print("--- Alpaca network health (paper, read-only) ---")
    print(f"env: {env_note}")
    print(f"time: {datetime.now().isoformat(timespec='seconds')}")

    print("\n--- DNS ---")
    dns_paper_ok, paper_addrs = check_dns(PAPER_HOST)
    dns_live_ok, live_addrs = check_dns(LIVE_HOST)
    if dns_paper_ok:
        _ok(PAPER_HOST, ", ".join(paper_addrs[:6]))
    else:
        _fail(PAPER_HOST, paper_addrs[0] if paper_addrs else "resolve failed")
    if dns_live_ok:
        _ok(LIVE_HOST, ", ".join(live_addrs[:6]))
    else:
        _fail(LIVE_HOST, live_addrs[0] if live_addrs else "resolve failed")

    print("\n--- TCP 443 ---")
    tcp_ok, tcp_detail = check_tcp(PAPER_HOST)
    if tcp_ok:
        _ok(f"{PAPER_HOST}:{TCP_PORT}", tcp_detail)
    else:
        _fail(f"{PAPER_HOST}:{TCP_PORT}", tcp_detail)

    print("\n--- Alpaca clock (paper client) ---")
    clock_class, clock_info = check_alpaca_clock()
    if clock_class == "ok":
        _ok(
            "get_clock",
            f"is_open={clock_info.get('is_open')} timestamp={clock_info.get('timestamp')}",
        )
    elif clock_class == "auth":
        _fail("get_clock", f"auth — {clock_info.get('error')}")
    elif clock_class == "transient_network":
        _fail("get_clock", f"transient_network — {clock_info.get('error')}")
    else:
        _fail("get_clock", f"other — {clock_info.get('error')}")

    summarize_bot_errors()
    summarize_paper_heartbeat()

    dns_ok = dns_paper_ok
    clock_ok = clock_class == "ok"
    auth_fail = clock_class == "auth"
    network_fail = (
        (not dns_ok)
        or (not tcp_ok)
        or (clock_class == "transient_network")
    )
    healthy = dns_ok and tcp_ok and clock_ok

    print("\n--- Summary ---")
    if healthy:
        print("NETWORK HEALTHY")
        return 0
    if auth_fail and dns_ok and tcp_ok:
        print("AUTH FAIL")
        return 1
    if network_fail:
        print("NETWORK DOWN")
        return 2
    print("PARTIAL")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
