"""Alpaca + bot health snapshot for paper or live books."""

from __future__ import annotations

import json
import os
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from modules.alpaca_client import build_trading_client, reset_trading_client_cache
from modules.runtime_paths import (
    bot_process_running,
    find_bot_exe_pids,
    resolve_data_root,
    resolve_runtime_root,
    runtime_log_dir,
)

ROOT = resolve_runtime_root()
DATA_ROOT = resolve_data_root(ROOT)


def _log_dir_for_check(*, paper: bool) -> Path:
    """Prefer logs beside the actually running bot (source vs frozen dist/)."""
    root = resolve_runtime_root()
    if bot_process_running(paper=paper):
        if find_bot_exe_pids():
            return resolve_data_root(root) / "logs"
        return root / "logs"
    return runtime_log_dir(root)

def _is_paper_heartbeat_path(path: Path) -> bool:
    s = str(path).replace("\\", "/").lower()
    return (
        "paper_chase" in s
        or "/alpaca_paper/" in s
        or "/fund/paper/" in s
        or path.name == "paper_chase_heartbeat.json"
    )


def _load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _heartbeat_matches_book(hb: dict | None, path: Path, *, paper: bool) -> bool:
    if hb is not None and "paper" in hb:
        return bool(hb["paper"]) == paper
    s = str(path).replace("\\", "/").lower()
    if _is_paper_heartbeat_path(path):
        return paper
    if "/alpaca_live/" in s:
        return not paper
    if path.name == "bot_heartbeat.json":
        return bool(config.PAPER_TRADING) == paper
    if "paper" in s or path.name.startswith("paper"):
        return paper
    return not paper


def resolve_paper_heartbeat_path(root: Path | None = None) -> Path:
    """Find the freshest paper heartbeat (bot_heartbeat.json or paper_chase)."""
    project_root = root or resolve_runtime_root()
    data_root = resolve_data_root(project_root)
    candidates: list[Path] = []
    raw = (os.getenv("HEARTBEAT_FILE") or config.HEARTBEAT_FILE or "bot_heartbeat.json").strip()
    env_path = Path(raw)
    candidates.append(env_path if env_path.is_absolute() else data_root / env_path)
    candidates.append(data_root / "bot_heartbeat.json")
    candidates.append(data_root / "paper_chase_heartbeat.json")
    if data_root != project_root:
        candidates.append(project_root / "bot_heartbeat.json")
        candidates.append(project_root / "paper_chase_heartbeat.json")
    candidates.append(data_root / "data" / "fund" / "paper" / "bot_heartbeat.json")

    fund_dir = data_root / "data" / "fund"
    if fund_dir.is_dir():
        paper_hb = fund_dir / "paper" / "bot_heartbeat.json"
        if paper_hb.is_file():
            candidates.insert(0, paper_hb)
        for hb in fund_dir.glob("*/bot_heartbeat.json"):
            if hb.parent.name == "paper":
                candidates.append(hb)

    portal_users = project_root / "data" / "portal" / "users"
    if portal_users.is_dir():
        for hb in portal_users.glob("*/books/alpaca_paper/bot_heartbeat.json"):
            candidates.append(hb)

    seen: set[Path] = set()
    unique: list[Path] = []
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)

    canonical = config.resolve_heartbeat_file(paper=True).resolve()
    if canonical.is_file() and _heartbeat_matches_book(
        _load_json(canonical), canonical, paper=True
    ):
        return canonical

    existing = [
        p
        for p in unique
        if p.is_file() and _heartbeat_matches_book(_load_json(p), p, paper=True)
    ]
    if not existing:
        return canonical
    return max(existing, key=lambda p: p.stat().st_mtime)


def resolve_live_heartbeat_path(root: Path | None = None) -> Path:
    """Find the freshest live heartbeat (live_bot_heartbeat.json, portal live slot, fund/live, or cwd)."""
    project_root = root or resolve_runtime_root()
    data_root = resolve_data_root(project_root)
    candidates: list[Path] = []
    raw = (os.getenv("HEARTBEAT_FILE") or config.HEARTBEAT_FILE or "bot_heartbeat.json").strip()
    env_path = Path(raw)
    candidates.append(env_path if env_path.is_absolute() else data_root / env_path)
    candidates.append(data_root / "live_bot_heartbeat.json")
    candidates.append(data_root / "bot_heartbeat.json")
    if data_root != project_root:
        candidates.append(project_root / "bot_heartbeat.json")
    candidates.append(data_root / "data" / "fund" / "live" / "bot_heartbeat.json")

    fund_dir = data_root / "data" / "fund"
    if fund_dir.is_dir():
        live_hb = fund_dir / "live" / "bot_heartbeat.json"
        if live_hb.is_file():
            candidates.insert(0, live_hb)
        for hb in fund_dir.glob("*/bot_heartbeat.json"):
            if hb.parent.name != "paper":
                candidates.append(hb)

    portal_users = project_root / "data" / "portal" / "users"
    if portal_users.is_dir():
        for hb in portal_users.glob("*/books/alpaca_live/bot_heartbeat.json"):
            candidates.append(hb)
        for hb in portal_users.glob("*/bot_heartbeat.json"):
            candidates.append(hb)

    seen: set[Path] = set()
    unique: list[Path] = []
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen or _is_paper_heartbeat_path(resolved):
            continue
        seen.add(resolved)
        unique.append(resolved)

    existing = [
        p
        for p in unique
        if p.is_file() and _heartbeat_matches_book(_load_json(p), p, paper=False)
    ]
    if not existing:
        return config.resolve_heartbeat_file(paper=False)
    return max(existing, key=lambda p: p.stat().st_mtime)


def _heartbeat_path(paper: bool) -> Path:
    if paper:
        return resolve_paper_heartbeat_path()
    return resolve_live_heartbeat_path()


def _bot_process_running(*, paper: bool) -> bool:
    return bot_process_running(paper=paper)


def _mtime_age_minutes(path: Path) -> float | None:
    if not path.is_file():
        return None
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - mtime).total_seconds() / 60.0)
    except OSError:
        return None


def _heartbeat_age_minutes(hb: dict | None, *, path: Path) -> float | None:
    ages: list[float] = []
    if hb and hb.get("timestamp"):
        try:
            ts = datetime.fromisoformat(str(hb["timestamp"]).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            ages.append(
                max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() / 60.0)
            )
        except (TypeError, ValueError):
            pass
    mtime_age = _mtime_age_minutes(path)
    if mtime_age is not None:
        ages.append(mtime_age)
    if not ages:
        return None
    return min(ages)


def _heartbeat_stale_suffix(
    age_min: float | None, *, paper: bool, path: Path, hb: dict | None
) -> str:
    if age_min is None:
        age_min = _mtime_age_minutes(path)
    if age_min is None:
        return ""
    running = _bot_process_running(paper=paper)
    if hb and hb.get("status") == "starting" and age_min < 10:
        return "  ·  STARTING"
    if running and age_min < 5:
        return "  ·  WARMING UP"
    if running and age_min <= 30:
        return f"  ·  age {age_min:.0f} min (bot running)"
    if age_min > 90:
        return f"  ·  STALE ({age_min:.0f} min old — check heartbeat path)"
    if age_min > 30:
        return f"  ·  age {age_min:.0f} min"
    return f"  ·  age {age_min:.0f} min"


_ERROR_LINE = re.compile(
    r"(ERROR|CRITICAL|cycle_error|Traceback|Exception|Alpaca auth failed|401 unauthorized)",
    re.IGNORECASE,
)


@dataclass
class HealthReport:
    book: str
    paper: bool
    lines: list[str]

    def text(self) -> str:
        return "\n".join(self.lines)


def _credentials_for_book(paper: bool) -> tuple[str, str]:
    return config.get_alpaca_credentials(paper=paper)


def _scan_log_errors(*, paper: bool, tail_lines: int = 400, max_show: int = 10) -> list[str]:
    """Recent error-ish lines from run_all.log and events.log."""
    log_dir = _log_dir_for_check(paper=paper)
    candidates: list[tuple[str, str]] = []
    for name in ("run_all.log", "events.log"):
        path = log_dir / name
        if not path.is_file():
            rotated = sorted(log_dir.glob(f"{name}.*"), reverse=True)
            path = rotated[0] if rotated else path
        if not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in deque(handle, maxlen=tail_lines):
                    text = line.rstrip()
                    if _ERROR_LINE.search(text):
                        candidates.append((name, text))
        except OSError:
            continue
    # De-dupe consecutive identical lines; keep order
    seen: set[str] = set()
    out: list[str] = []
    for name, text in candidates:
        key = text[-160:]
        if key in seen:
            continue
        seen.add(key)
        short = text if len(text) <= 140 else text[:137] + "…"
        out.append(f"  [{name}] {short}")
        if len(out) >= max_show:
            break
    return out


def _fetch_orders_24h(client) -> list:
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    start = datetime.now(timezone.utc) - timedelta(hours=24)
    req = GetOrdersRequest(
        status=QueryOrderStatus.ALL,
        after=start,
        limit=100,
        nested=True,
    )
    orders = list(client.get_orders(filter=req))

    def _sort_key(o):
        for attr in ("filled_at", "submitted_at", "created_at"):
            ts = getattr(o, attr, None)
            if ts is not None:
                return ts
        return datetime.min.replace(tzinfo=timezone.utc)

    orders.sort(key=_sort_key, reverse=True)
    return orders


def _order_row(order) -> str:
    sym = config.normalize_symbol(getattr(order, "symbol", "") or "")
    side = str(getattr(order, "side", "")).split(".")[-1].lower()
    status = str(getattr(order, "status", "")).split(".")[-1].lower()
    qty = float(getattr(order, "filled_qty", None) or getattr(order, "qty", None) or 0)
    avg = getattr(order, "filled_avg_price", None)
    notional = getattr(order, "notional", None)
    if avg is not None and qty:
        val = f"${float(qty) * float(avg):,.2f}"
    elif notional:
        val = f"${float(notional):,.2f}"
    else:
        val = "—"
    ts = getattr(order, "filled_at", None) or getattr(order, "submitted_at", None)
    ts_s = str(ts)[:19].replace("T", " ") if ts else "—"
    return f"  {ts_s}  {sym:10}  {side:4}  {status:12}  {val}"


def run_health_check(*, paper: bool) -> HealthReport:
    book = "PAPER" if paper else "LIVE"
    lines: list[str] = [f"=== {book} health check ==="]

    try:
        key, secret = _credentials_for_book(paper)
    except ValueError as exc:
        lines.append(f"ERROR: {exc}")
        return HealthReport(book=book, paper=paper, lines=lines)

    reset_trading_client_cache()
    try:
        client = build_trading_client(key, secret, paper=paper)
        acct = client.get_account()
        positions = list(client.get_all_positions())
    except Exception as exc:  # noqa: BLE001
        lines.append(f"ERROR: Alpaca API — {exc}")
        return HealthReport(book=book, paper=paper, lines=lines)

    equity = float(acct.equity)
    cash = float(acct.cash)
    cash_pct = (cash / equity * 100) if equity > 0 else 0.0
    invested_pct = max(0.0, (equity - cash) / equity * 100) if equity > 0 else 0.0

    lines.append(f"Equity:     ${equity:,.2f}")
    lines.append(f"Cash:       ${cash:,.2f}  ({cash_pct:.1f}% of equity)")
    lines.append(f"Invested:   {invested_pct:.1f}%  ·  status={str(acct.status).split('.')[-1]}")

    hb_path = _heartbeat_path(paper)
    hb = _load_json(hb_path)
    if hb:
        regime = hb.get("regime") or "—"
        ts = str(hb.get("timestamp") or "—")[:19]
        age_min = _heartbeat_age_minutes(hb, path=hb_path)
        age_suffix = _heartbeat_stale_suffix(
            age_min, paper=paper, path=hb_path, hb=hb
        )
        path_note = f"  ·  file={hb_path.name}" if hb_path.name else ""
        lines.append(f"Heartbeat:  {ts}  ·  regime={regime}{age_suffix}{path_note}")
        if hb.get("last_cycle_error"):
            err_at = str(hb.get("last_cycle_error_at") or "")[:19]
            err = str(hb["last_cycle_error"])[:120]
            lines.append(f"  last_cycle_error ({err_at}): {err}")
    else:
        lines.append(f"Heartbeat:  (no {_heartbeat_path(paper).name})")

    lines.append("")
    lines.append(f"Open positions ({len(positions)}):")
    if not positions:
        lines.append("  (none)")
    else:
        rows = []
        for pos in positions:
            sym = config.normalize_symbol(pos.symbol)
            qty = float(pos.qty)
            mv = abs(float(getattr(pos, "market_value", 0) or 0))
            if mv <= 0:
                price = float(getattr(pos, "current_price", 0) or 0)
                mv = abs(qty * price)
            upl = float(getattr(pos, "unrealized_pl", 0) or 0)
            rows.append((mv, sym, qty, upl))
        rows.sort(reverse=True)
        for mv, sym, qty, upl in rows[:12]:
            lines.append(f"  {sym:10}  qty={qty:>12.6g}  ${mv:>10,.2f}  P&L ${upl:+,.2f}")
        if len(rows) > 12:
            lines.append(f"  … and {len(rows) - 12} more")

    lines.append("")
    lines.append("Orders (last 24h):")
    try:
        orders = _fetch_orders_24h(client)
        if not orders:
            lines.append("  (none)")
        else:
            for order in orders[:15]:
                lines.append(_order_row(order))
            if len(orders) > 15:
                lines.append(f"  … and {len(orders) - 15} more")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"  ERROR fetching orders: {exc}")

    lines.append("")
    lines.append("Recent log errors:")
    log_errors = _scan_log_errors(paper=paper)
    if log_errors:
        lines.extend(log_errors)
    else:
        lines.append("  (none in last log tail)")

    return HealthReport(book=book, paper=paper, lines=lines)


def format_multi_book_report(reports: list[HealthReport]) -> str:
    parts = [r.text() for r in reports]
    return "\n\n".join(parts)
