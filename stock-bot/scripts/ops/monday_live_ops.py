#!/usr/bin/env python3
"""Monday pre-market LIVE ops checklist — read-only, never places orders.

Surfaces manual review items for the LIVE Alpaca book before the week starts.

Run from stock-bot/:
  python scripts/ops/monday_live_ops.py

Exit 0 always (flags are informational).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("PYTHONTRADING_ROOT", str(ROOT))

# Live book only — set before config import so dotenv loads the live portal .env.
os.environ["PAPER_TRADING"] = "false"
os.environ.pop("PAPER_CHASE_MODE", None)
os.environ.pop("PAPER_AGGRESSIVE", None)


def _resolve_live_env_file() -> Path | None:
    """Prefer portal alpaca_live/.env; fall back to stock-bot/.env if it has APCA_*."""
    try:
        from modules.portal_paths import (
            bind_project_root,
            book_env_path,
            get_last_username,
            has_alpaca_config,
        )

        bind_project_root(ROOT)
        username = (get_last_username() or "").strip().lower()
        portal_users = ROOT / "data" / "portal" / "users"
        candidates: list[Path] = []
        if username and has_alpaca_config(username, "alpaca_live"):
            candidates.append(book_env_path(username, "alpaca_live"))
        if portal_users.is_dir():
            for env_path in portal_users.glob("*/books/alpaca_live/.env"):
                candidates.append(env_path)
        existing = [p for p in candidates if p.is_file()]
        if existing:
            return max(existing, key=lambda p: p.stat().st_mtime)
    except Exception:
        pass
    stock_env = ROOT / ".env"
    if stock_env.is_file():
        try:
            text = stock_env.read_text(encoding="utf-8", errors="replace")
            if "APCA_API_KEY_ID=" in text and "APCA_API_SECRET_KEY=" in text:
                return stock_env
        except OSError:
            pass
    return None


_live_env = _resolve_live_env_file()
if _live_env is not None:
    os.environ["PYTHONTRADING_ENV_FILE"] = str(_live_env)
os.environ.setdefault("ALLOW_LIVE_TRADING", "yes")

import config  # noqa: E402

# Re-assert after dotenv (root .env often pins PAPER_TRADING=true).
os.environ["PAPER_TRADING"] = "false"
config.PAPER_TRADING = False
if not getattr(config, "ALLOW_LIVE_TRADING", False):
    # Read-only ops still needs live API access acknowledgment.
    config.ALLOW_LIVE_TRADING = True
    os.environ["ALLOW_LIVE_TRADING"] = "yes"

from modules.alpaca_client import build_trading_client, reset_trading_client_cache  # noqa: E402
from modules.health_check import resolve_live_heartbeat_path  # noqa: E402

LONG_HOLD_DAYS = 30
STOP_REVIEW_PCT = -5.0
PROFIT_REVIEW_PCT = 10.0
DUST_NOTIONAL = 1.0
SCREENER_STALE_DAYS = 5.0
THINKING_STALE_HOURS = 24.0
LOW_CASH_PCT = 0.10
EXCESS_CASH_PCT = 0.30
SMALL_ACCOUNT_EQUITY = 500.0


@dataclass
class PositionRow:
    symbol: str
    qty: float
    avg_entry: float
    price: float
    unrealized_pct: float
    notional: float
    days_held: float | None = None
    flags: list[str] = field(default_factory=list)


@dataclass
class OpsFlag:
    code: str
    detail: str
    priority: int


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw: Any) -> datetime | None:
    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        text = str(raw).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            # thinking_engine / heartbeats often write naive local wall time
            local_tz = datetime.now().astimezone().tzinfo or timezone.utc
            dt = dt.replace(tzinfo=local_tz)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _section(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def _resolve_heartbeat() -> tuple[Path | None, dict | None]:
    """Prefer portal live heartbeat; fall back to root / data bot_heartbeat.json."""
    path = resolve_live_heartbeat_path(ROOT)
    hb = _load_json(path) if path.is_file() else None
    if hb is not None:
        return path, hb

    for candidate in (
        ROOT / "bot_heartbeat.json",
        ROOT / "data" / "bot_heartbeat.json",
        ROOT / "live_bot_heartbeat.json",
    ):
        hb = _load_json(candidate)
        if hb is not None and hb.get("paper") is not True:
            return candidate, hb
    return (path if path.is_file() else None), None


def _regime_banner(regime_raw: str | None) -> tuple[str, str | None]:
    """Return (display line, defensive|range|growth|euphoric|unknown code)."""
    regime = (regime_raw or "").strip()
    upper = regime.upper()
    if "RHYME_B" in upper or "RHYME_E" in upper:
        return "⚠️ DEFENSIVE — reduce new entries, protect capital", "DEFENSIVE"
    if "RHYME_D" in upper:
        return "📊 RANGE — selective entries, no force-deploy", "RANGE"
    if "RHYME_C" in upper:
        return "✅ STEADY GROWTH — standard risk", "GROWTH"
    if "RHYME_A" in upper:
        return "⚡ HIGH VOL / EUPHORIC — normal ops but watch size", "EUPHORIC"
    return "⚠️ REGIME UNKNOWN — check heartbeat path", "UNKNOWN"


def _days_held_from_heartbeat(hb: dict | None, symbol: str) -> float | None:
    if not hb:
        return None
    positions = hb.get("positions") or hb.get("open_positions") or {}
    if not isinstance(positions, dict):
        return None
    block = positions.get(symbol) or positions.get(symbol.upper()) or positions.get(
        symbol.lower()
    )
    if not isinstance(block, dict):
        return None
    for key in ("opened_at", "entry_at", "created_at", "buy_ts", "timestamp"):
        ts = _parse_ts(block.get(key))
        if ts is not None:
            return max(0.0, (_now_utc() - ts).total_seconds() / 86400.0)
    return None


def _approx_days_held(client, symbol: str, hb: dict | None) -> float | None:
    days = _days_held_from_heartbeat(hb, symbol)
    if days is not None:
        return days
    # Best-effort: oldest filled buy in recent order history (cap lookback).
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        req = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            symbols=[symbol],
            limit=50,
            nested=False,
        )
        orders = list(client.get_orders(filter=req))
        buy_ts: list[datetime] = []
        for order in orders:
            side = str(getattr(order, "side", "")).split(".")[-1].lower()
            status = str(getattr(order, "status", "")).split(".")[-1].lower()
            if side != "buy" or status not in ("filled", "partially_filled"):
                continue
            ts = getattr(order, "filled_at", None) or getattr(order, "submitted_at", None)
            parsed = _parse_ts(ts)
            if parsed is not None:
                buy_ts.append(parsed)
        if buy_ts:
            oldest = min(buy_ts)
            return max(0.0, (_now_utc() - oldest).total_seconds() / 86400.0)
    except Exception:
        return None
    return None


def step_account_health(client, hb: dict | None) -> tuple[list[PositionRow], list[OpsFlag]]:
    _section("1/6  Account health (LIVE positions)")
    flags: list[OpsFlag] = []
    rows: list[PositionRow] = []
    try:
        positions = list(client.get_all_positions())
    except Exception as exc:
        print(f"  ERROR fetching positions: {exc}")
        return rows, flags

    if not positions:
        print("  (no open positions)")
        return rows, flags

    print(
        f"  {'Ticker':<8} {'Qty':>12} {'Avg':>10} {'Px':>10} "
        f"{'UPnL%':>8} {'Notional':>10} {'Days':>6}  Flags"
    )
    print("  " + "-" * 78)

    for pos in positions:
        sym = config.normalize_symbol(getattr(pos, "symbol", "") or "")
        qty = float(getattr(pos, "qty", 0) or 0)
        avg = float(getattr(pos, "avg_entry_price", 0) or 0)
        px = float(getattr(pos, "current_price", 0) or 0)
        mv = abs(float(getattr(pos, "market_value", 0) or 0))
        if mv <= 0 and px > 0:
            mv = abs(qty * px)
        upl_pct = float(getattr(pos, "unrealized_plpc", 0) or 0) * 100.0
        days = _approx_days_held(client, sym, hb)
        row_flags: list[str] = []
        if days is not None and days > LONG_HOLD_DAYS:
            row_flags.append("LONG_HOLD")
            flags.append(
                OpsFlag(
                    "LONG_HOLD",
                    f"{sym} held ~{days:.0f}d (> {LONG_HOLD_DAYS}d)",
                    priority=60,
                )
            )
        if upl_pct < STOP_REVIEW_PCT:
            row_flags.append("STOP_REVIEW")
            flags.append(
                OpsFlag(
                    "STOP_REVIEW",
                    f"{sym} unrealized {upl_pct:+.1f}% (loss > 5%)",
                    priority=20,
                )
            )
        if upl_pct > PROFIT_REVIEW_PCT:
            row_flags.append("PROFIT_REVIEW")
            flags.append(
                OpsFlag(
                    "PROFIT_REVIEW",
                    f"{sym} unrealized {upl_pct:+.1f}% (gain > 10%)",
                    priority=50,
                )
            )
        dust = mv < DUST_NOTIONAL or (abs(qty) < 1e-3 and mv < 5.0)
        if dust:
            row_flags.append("DUST")
            flags.append(
                OpsFlag(
                    "DUST",
                    f"{sym} notional ${mv:.2f} < $1 — ignore or manual liquidate in Alpaca",
                    priority=30,
                )
            )
        days_s = f"{days:.0f}" if days is not None else "n/a"
        flag_s = ",".join(row_flags) if row_flags else "—"
        print(
            f"  {sym:<8} {qty:>12.6g} {avg:>10.2f} {px:>10.2f} "
            f"{upl_pct:>+7.1f}% {mv:>10.2f} {days_s:>6}  {flag_s}"
        )
        rows.append(
            PositionRow(
                symbol=sym,
                qty=qty,
                avg_entry=avg,
                price=px,
                unrealized_pct=upl_pct,
                notional=mv,
                days_held=days,
                flags=row_flags,
            )
        )
    return rows, flags


def step_regime(hb: dict | None, hb_path: Path | None) -> tuple[str | None, list[OpsFlag]]:
    _section("2/6  Regime")
    flags: list[OpsFlag] = []
    regime = None
    status = ""
    if hb:
        regime = str(hb.get("regime") or "").strip() or None
        status = str(hb.get("status") or "").strip().lower()
    banner, code = _regime_banner(regime)
    path_note = f"  path={hb_path}" if hb_path else "  path=(missing)"
    print(f"  Raw: {regime or '(missing)'}")
    if status:
        print(f"  Status: {status}")
    print(f"  {banner}")
    print(f"  {path_note}")
    regime_missing = not regime or regime.lower() in ("startup", "unknown")
    status_startup = status == "startup" or status.startswith("startup")
    if status_startup or regime_missing:
        print(
            "  Heartbeat not ready — wait 60–90s after owner_reset and re-run"
        )
    if code == "DEFENSIVE":
        flags.append(OpsFlag("DEFENSIVE", banner, priority=10))
    elif code == "UNKNOWN":
        flags.append(OpsFlag("REGIME_UNKNOWN", banner, priority=45))
    return regime, flags


def _file_age_days(path: Path, generated_at: Any = None) -> float | None:
    ages: list[float] = []
    ts = _parse_ts(generated_at)
    if ts is not None:
        ages.append(max(0.0, (_now_utc() - ts).total_seconds() / 86400.0))
    if path.is_file():
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            ages.append(max(0.0, (_now_utc() - mtime).total_seconds() / 86400.0))
        except OSError:
            pass
    return min(ages) if ages else None


def step_screener() -> list[OpsFlag]:
    _section("3/6  Screener freshness")
    flags: list[OpsFlag] = []
    uni_path = ROOT / "data" / "screener_universe.json"
    if not uni_path.is_file():
        uni_path = Path(getattr(config, "SCREENER_UNIVERSE_PATH", "data/screener_universe.json"))
        if not uni_path.is_absolute():
            uni_path = ROOT / uni_path

    sector_raw = getattr(config, "SECTOR_SCREENER_STATE_FILE", "") or (
        "data/sector_screener_state.json"
    )
    sector_path = Path(sector_raw)
    if not sector_path.is_absolute():
        sector_path = ROOT / sector_path

    uni = _load_json(uni_path) or {}
    sector = _load_json(sector_path) or {}
    tickers = uni.get("tickers") or []
    n = len(tickers) if isinstance(tickers, list) else 0
    uni_age = _file_age_days(uni_path, uni.get("generated_at"))
    sector_age = _file_age_days(sector_path, sector.get("date") or sector.get("generated_at"))

    # Prefer universe file; fall back to sector state age if universe missing.
    age = uni_age if uni_path.is_file() else sector_age
    source = uni_path if uni_path.is_file() else sector_path

    if age is None:
        print("  🔄 SCREENER STALE — no screener_universe.json / sector state found")
        flags.append(
            OpsFlag(
                "SCREENER_STALE",
                "screener file missing — refresh universe before open",
                priority=40,
            )
        )
        return flags

    print(f"  Source: {source}")
    print(f"  Age:    {age:.1f}d  (stale if > {SCREENER_STALE_DAYS:.0f}d)")
    if sector_path.is_file() and sector_age is not None:
        print(f"  Sector state age: {sector_age:.1f}d ({sector_path.name})")

    if age > SCREENER_STALE_DAYS:
        print("  🔄 SCREENER STALE — refresh universe before open")
        flags.append(
            OpsFlag(
                "SCREENER_STALE",
                f"screener age {age:.1f}d > {SCREENER_STALE_DAYS:.0f}d — refresh before open",
                priority=40,
            )
        )
    else:
        print(f"  ✅ Screener fresh — {n} tickers")
    return flags


def step_thinking() -> list[OpsFlag]:
    _section("4/6  Thinking engine (informational — live should not depend on it)")
    flags: list[OpsFlag] = []
    raw_name = getattr(config, "THINKING_ENGINE_OUTPUT_FILE", "thinking_engine_last.json")
    path = Path(raw_name)
    if not path.is_absolute():
        path = ROOT / path
    data = _load_json(path)
    if not data:
        print(f"  (no {path.name})")
        return flags

    ts = _parse_ts(data.get("timestamp"))
    hours = None
    if ts is not None:
        hours = max(0.0, (_now_utc() - ts).total_seconds() / 3600.0)
    narrative = str(
        data.get("narrative") or data.get("reasoning") or data.get("justification") or ""
    ).strip()
    conf = data.get("confidence")
    try:
        conf_f = float(conf) if conf is not None else None
    except (TypeError, ValueError):
        conf_f = None
    regime = str(data.get("regime") or "").strip() or "n/a"

    snippet = narrative[:160] + ("…" if len(narrative) > 160 else "")
    print(f"  File:      {path}")
    print(f"  Last run:  {ts.isoformat(timespec='seconds') if ts else 'n/a'}")
    if hours is not None:
        print(f"  Age:       {hours:.1f}h")
    print(f"  Regime:    {regime}")
    if conf_f is not None:
        conf_disp = f"{conf_f:.0%}" if conf_f <= 1.0 else f"{conf_f:.0f}"
        print(f"  Confidence:{conf_disp}")
    print(f"  Narrative: {snippet or '(empty)'}")

    if hours is not None and hours > THINKING_STALE_HOURS:
        print("  ⚠️ THINKING STALE (paper-relevant; live can ignore)")
        flags.append(
            OpsFlag(
                "THINKING_STALE",
                f"thinking_engine_last.json age {hours:.1f}h > {THINKING_STALE_HOURS:.0f}h",
                priority=42,
            )
        )
    return flags


def step_cash_allocation(
    client, hb: dict | None, positions: list[PositionRow], regime_raw: str | None
) -> list[OpsFlag]:
    _section("5/6  Cash / allocation")
    flags: list[OpsFlag] = []
    try:
        acct = client.get_account()
        equity = float(acct.equity)
        cash = float(acct.cash)
        bp = float(getattr(acct, "buying_power", 0) or 0)
    except Exception as exc:
        print(f"  ERROR fetching account: {exc}")
        return flags

    cash_pct = (cash / equity) if equity > 0 else 0.0
    print(f"  Equity:        ${equity:,.2f}")
    print(f"  Cash:          ${cash:,.2f}  ({cash_pct:.1%} of equity)")
    print(f"  Buying power:  ${bp:,.2f}")

    # VTI / core % from positions or heartbeat
    vti_notional = sum(r.notional for r in positions if r.symbol in ("VTI", "VOO", "ITOT"))
    core_pct = None
    if equity > 0 and vti_notional > 0:
        core_pct = vti_notional / equity
        print(f"  Core (VTI-like): {core_pct:.1%}  (${vti_notional:,.2f})")
    elif hb and hb.get("vti_core_pct") is not None:
        try:
            core_pct = float(hb["vti_core_pct"])
            print(f"  Core (heartbeat vti_core_pct): {core_pct:.1%}")
        except (TypeError, ValueError):
            pass
    else:
        print("  Core (VTI-like): n/a")

    if equity < SMALL_ACCOUNT_EQUITY:
        print(f"  Note: small-account mode (equity < ${SMALL_ACCOUNT_EQUITY:.0f})")

    if equity > 0 and cash_pct < LOW_CASH_PCT:
        print("  ⚠️ LOW CASH")
        flags.append(
            OpsFlag(
                "LOW_CASH",
                f"cash {cash_pct:.1%} of equity < 10%",
                priority=55,
            )
        )

    regime_u = (regime_raw or "").upper()
    deploy_ok = "RHYME_C" in regime_u or "RHYME_A" in regime_u
    if equity > 0 and cash_pct > EXCESS_CASH_PCT and deploy_ok:
        print("  💡 EXCESS CASH — optional deploy review")
        flags.append(
            OpsFlag(
                "EXCESS_CASH",
                f"cash {cash_pct:.1%} > 30% with regime C/A — optional deploy review",
                priority=70,
            )
        )
    return flags


def step_action_summary(all_flags: list[OpsFlag]) -> None:
    _section("6/6  Action summary")
    # Priority: DEFENSIVE > STOP_REVIEW > DUST > STALE data > PROFIT_REVIEW > rest
    priority_boost = {
        "DEFENSIVE": 10,
        "STOP_REVIEW": 20,
        "DUST": 30,
        "SCREENER_STALE": 40,
        "THINKING_STALE": 42,
        "REGIME_UNKNOWN": 45,
        "PROFIT_REVIEW": 50,
        "LOW_CASH": 55,
        "LONG_HOLD": 60,
        "EXCESS_CASH": 70,
    }
    ranked = sorted(
        all_flags,
        key=lambda f: (priority_boost.get(f.code, f.priority), f.code, f.detail),
    )
    # De-dupe identical code+detail
    seen: set[str] = set()
    unique: list[OpsFlag] = []
    for fl in ranked:
        key = f"{fl.code}|{fl.detail}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(fl)

    if not unique:
        print("  ✅ No action required")
        return

    print("  Flags (priority order):")
    for fl in unique:
        print(f"  • [{fl.code}] {fl.detail}")


def main() -> int:
    print("Monday LIVE ops checklist — READ-ONLY (never places orders)")
    print(f"Project: {ROOT}")
    if _live_env is not None:
        print(f"Live env: {_live_env}")
    else:
        print("Live env: (none resolved — using process env / stock-bot .env)")

    hb_path, hb = _resolve_heartbeat()
    reset_trading_client_cache()
    client = None
    all_flags: list[OpsFlag] = []
    positions: list[PositionRow] = []
    regime_raw: str | None = None

    try:
        key, secret = config.get_live_alpaca_credentials()
        # Never use paper keys: get_live_alpaca_credentials is APCA_* only.
        client = build_trading_client(key, secret, paper=False)
        status = config.alpaca_credentials_status(paper=False)
        print(
            f"Alpaca: LIVE key …{status.get('key_suffix')} "
            f"source={status.get('key_source')} base={status.get('base_url')}"
        )
    except Exception as exc:
        print(f"ERROR: cannot build LIVE Alpaca client: {exc}")
        print("Fix: set APCA_* in portal alpaca_live/.env (not PAPER_APCA_*).")
        # Still run non-API steps.
        _, regime_flags = step_regime(hb, hb_path)
        all_flags.extend(regime_flags)
        all_flags.extend(step_screener())
        all_flags.extend(step_thinking())
        step_action_summary(all_flags)
        print()
        print("Done (partial — no Alpaca account data). Exit 0.")
        return 0

    positions, pos_flags = step_account_health(client, hb)
    all_flags.extend(pos_flags)

    regime_raw, regime_flags = step_regime(hb, hb_path)
    all_flags.extend(regime_flags)

    all_flags.extend(step_screener())
    all_flags.extend(step_thinking())
    all_flags.extend(step_cash_allocation(client, hb, positions, regime_raw))

    step_action_summary(all_flags)
    print()
    print("Done. Exit 0 (informational only).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
