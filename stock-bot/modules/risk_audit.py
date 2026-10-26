"""Read-only fill slippage + exposure audit. Does not place orders or write .env."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import config
from modules.alpaca_client import get_trading_client
from modules.data_loader import _close_column, _date_column, daily_table_name, safe_sql_table

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "scripts" / "analysis" / "audit_fills_last.json"
OUT_MD = ROOT / "scripts" / "analysis" / "audit_fills_last.md"
RESEARCH_SIDE_BPS = 5.0  # 10 bps round-trip / 2
FLAG_MEDIAN_BPS = 8.0


def _sym(raw: Any) -> str:
    return config.normalize_symbol(str(raw or ""))


def sleeve_for_audit_symbol(symbol: str) -> str:
    """VTI core / NYSE / metal / crypto. Stat-arb legs sit in NYSE without pair tags."""
    sym = _sym(symbol)
    if not sym:
        return "other"
    if config.is_crypto(sym):
        return "crypto"
    if config.is_metal_symbol(sym):
        return "metal"
    if sym == getattr(config, "VTI_CORE_SYMBOL", "VTI") or sym in ("VOO", "ITOT"):
        return "vti"
    return "nyse"


def _as_et(ts) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        dt = ts
    else:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET)


class _PriorCloseCache:
    def __init__(self) -> None:
        self._conn: sqlite3.Connection | None = None
        self._series: dict[str, list[tuple[datetime, float]]] = {}

    def _db(self) -> sqlite3.Connection | None:
        path = config.resolve_db_path()
        if not Path(path).is_file():
            return None
        if self._conn is None:
            self._conn = sqlite3.connect(str(path))
        return self._conn

    def prior_close(self, symbol: str, fill_et: datetime) -> float | None:
        sym = _sym(symbol)
        if not sym:
            return None
        rows = self._series.get(sym)
        if rows is None:
            rows = self._load(sym)
            self._series[sym] = rows
        fill_day = fill_et.date()
        prior = None
        for day, px in rows:
            if day.date() >= fill_day:
                break
            prior = px
        return prior

    def _load(self, symbol: str) -> list[tuple[datetime, float]]:
        conn = self._db()
        if conn is None:
            return []
        table = daily_table_name(symbol)
        try:
            safe = safe_sql_table(table)
        except ValueError:
            return []
        close_col = _close_column(conn, table)
        date_col = _date_column(conn, table)
        if not close_col or not date_col:
            return []
        try:
            cur = conn.execute(
                f'SELECT "{date_col}", "{close_col}" FROM "{safe}" ORDER BY "{date_col}"'
            )
        except sqlite3.Error:
            return []
        out: list[tuple[datetime, float]] = []
        for raw_d, raw_px in cur:
            try:
                px = float(raw_px)
            except (TypeError, ValueError):
                continue
            if px <= 0:
                continue
            dt = datetime.fromisoformat(str(raw_d)[:19]) if raw_d else None
            if dt is None:
                continue
            out.append((dt, px))
        return out

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _fetch_closed_orders(client, *, start: datetime, end: datetime) -> list:
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    orders: list = []
    cursor_end = end
    for _ in range(8):
        req = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            after=start,
            until=cursor_end,
            limit=500,
            nested=True,
        )
        batch = list(client.get_orders(filter=req) or [])
        if not batch:
            break
        orders.extend(batch)
        oldest = min(
            (
                getattr(o, "filled_at", None) or getattr(o, "submitted_at", None)
                for o in batch
            ),
            default=None,
        )
        if oldest is None or len(batch) < 500:
            break
        if isinstance(oldest, datetime):
            cursor_end = oldest if oldest.tzinfo else oldest.replace(tzinfo=timezone.utc)
        else:
            break
        if cursor_end <= start:
            break
    seen: set[str] = set()
    uniq = []
    for o in orders:
        oid = str(getattr(o, "id", "") or "")
        if oid and oid in seen:
            continue
        if oid:
            seen.add(oid)
        uniq.append(o)
    return uniq


def _order_row(order, cache: _PriorCloseCache) -> dict[str, Any] | None:
    qty = float(getattr(order, "filled_qty", None) or 0)
    avg = getattr(order, "filled_avg_price", None)
    if qty <= 0 or avg is None:
        return None
    try:
        fill_px = float(avg)
    except (TypeError, ValueError):
        return None
    if fill_px <= 0:
        return None
    filled_at = getattr(order, "filled_at", None) or getattr(order, "submitted_at", None)
    fill_et = _as_et(filled_at)
    if fill_et is None:
        return None
    symbol = _sym(getattr(order, "symbol", ""))
    side = str(getattr(order, "side", "")).lower().split(".")[-1]
    bench = cache.prior_close(symbol, fill_et)
    slip = None
    if bench and bench > 0:
        raw = (fill_px / bench - 1.0) * 10_000.0
        slip = raw if side == "buy" else -raw
    return {
        "id": str(getattr(order, "id", "") or ""),
        "symbol": symbol,
        "side": side,
        "qty": round(qty, 6),
        "fill_px": round(fill_px, 4),
        "bench_px": None if bench is None else round(float(bench), 4),
        "benchmark": "sqlite_prior_close",
        "slippage_bps": None if slip is None else round(float(slip), 2),
        "sleeve": sleeve_for_audit_symbol(symbol),
        "filled_at_et": fill_et.strftime("%Y-%m-%d %H:%M"),
        "notional": round(qty * fill_px, 2),
    }


def _summarize(rows: list[dict]) -> dict[str, Any]:
    priced = [r for r in rows if r.get("slippage_bps") is not None]
    def _bucket(items: list[dict]) -> dict[str, Any]:
        if not items:
            return {"n": 0, "median_bps": None, "mean_bps": None, "p90_bps": None}
        xs = sorted(float(r["slippage_bps"]) for r in items)
        n = len(xs)
        mid = xs[n // 2] if n % 2 else 0.5 * (xs[n // 2 - 1] + xs[n // 2])
        p90 = xs[min(n - 1, int(round(0.9 * (n - 1))))]
        return {
            "n": n,
            "median_bps": round(mid, 2),
            "mean_bps": round(sum(xs) / n, 2),
            "p90_bps": round(p90, 2),
        }

    by_sleeve: dict[str, Any] = {}
    for sleeve in ("vti", "nyse", "crypto", "metal", "other"):
        chunk = [r for r in priced if r.get("sleeve") == sleeve]
        if chunk:
            by_sleeve[sleeve] = _bucket(chunk)
    overall = _bucket(priced)
    med = overall.get("median_bps")
    overall["flag"] = bool(med is not None and abs(float(med)) >= FLAG_MEDIAN_BPS)
    overall["vs_research_side_bps"] = RESEARCH_SIDE_BPS
    return {
        "fills": len(rows),
        "priced": len(priced),
        "overall": overall,
        "by_sleeve": by_sleeve,
    }


def audit_book_fills(client, *, days: int, label: str) -> dict[str, Any]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=max(1, int(days)))
    cache = _PriorCloseCache()
    try:
        orders = _fetch_closed_orders(client, start=start, end=end)
        rows = []
        for order in orders:
            row = _order_row(order, cache)
            if row:
                rows.append(row)
    finally:
        cache.close()
    return {
        "book": label,
        "days": int(days),
        "window": f"{start.date()} -> {end.date()}",
        "benchmark": "sqlite prior session close (daily backtest SoT). Not live mid at signal.",
        "summary": _summarize(rows),
        "sample": rows[:12],
    }


def run_fill_audit(*, days: int = 14) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "research_only": True,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "days": int(days),
        "note": (
            "Advisory only. Slippage = buy (fill/prior_close-1), sell opposite, in bps. "
            "Does not place orders. Research costs 5 bps/side (10 bps RT)."
        ),
        "books": {},
    }
    try:
        paper = get_trading_client(paper=True)
        payload["books"]["paper"] = audit_book_fills(paper, days=days, label="paper")
    except Exception as exc:
        payload["books"]["paper"] = {"book": "paper", "error": str(exc)[:240]}
    try:
        live = get_trading_client(paper=False, allow_live=True)
        payload["books"]["live"] = audit_book_fills(live, days=days, label="live")
    except Exception as exc:
        payload["books"]["live"] = {"book": "live", "error": str(exc)[:240]}
    return payload


def write_fill_audit(payload: dict[str, Any]) -> tuple[Path, Path]:
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    lines = [
        "# Fill-quality audit (advisory)",
        "",
        payload.get("note") or "",
        "",
        f"Generated: {payload.get('generated')}",
        f"**Window:** {payload.get('days')}d",
        f"**Benchmark:** sqlite prior close (not NBBO at signal)",
        "",
    ]
    for key in ("live", "paper"):
        book = (payload.get("books") or {}).get(key) or {}
        lines.append(f"## {key}")
        lines.append("")
        if book.get("error"):
            lines.append(f"Error: {book['error']}")
            lines.append("")
            continue
        sm = book.get("summary") or {}
        ov = sm.get("overall") or {}
        flag = " FLAG" if ov.get("flag") else ""
        lines.append(
            f"Fills {sm.get('fills', 0)} (priced {sm.get('priced', 0)}) | "
            f"median {ov.get('median_bps')} bps | mean {ov.get('mean_bps')} bps | "
            f"p90 {ov.get('p90_bps')} bps{flag}"
        )
        lines.append("")
        sleeves = sm.get("by_sleeve") or {}
        if sleeves:
            lines.append("| sleeve | n | median bps | mean bps | p90 bps |")
            lines.append("|--------|--:|----------:|---------:|--------:|")
            for sleeve, row in sleeves.items():
                lines.append(
                    f"| {sleeve} | {row.get('n')} | {row.get('median_bps')} | "
                    f"{row.get('mean_bps')} | {row.get('p90_bps')} |"
                )
            lines.append("")
    lines.append(
        f"Flag if |median| >= {FLAG_MEDIAN_BPS:.0f} bps vs research {RESEARCH_SIDE_BPS:.0f} bps/side."
    )
    lines.append("")
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    return OUT_JSON, OUT_MD


def _pos_value(pos) -> float:
    mv = getattr(pos, "market_value", None)
    if mv is not None:
        try:
            return abs(float(mv))
        except (TypeError, ValueError):
            pass
    try:
        return abs(float(pos.qty or 0) * float(pos.current_price or 0))
    except (TypeError, ValueError):
        return 0.0


def exposure_snapshot(client, *, label: str, paper: bool) -> dict[str, Any]:
    account = client.get_account()
    equity = float(account.equity)
    cash = float(account.cash)
    positions = list(client.get_all_positions() or [])
    sleeves: dict[str, float] = defaultdict(float)
    names = []
    for pos in positions:
        val = _pos_value(pos)
        sym = _sym(pos.symbol)
        sleeve = sleeve_for_audit_symbol(sym)
        sleeves[sleeve] += val
        pct = (val / equity) if equity > 0 else 0.0
        names.append({"symbol": sym, "sleeve": sleeve, "value": round(val, 2), "pct": round(pct, 4)})
    names.sort(key=lambda r: r["value"], reverse=True)
    name_cap = (
        float(config.effective_per_name_max_pct())
        if paper
        else float(config.PER_NAME_MAX_PCT)
    )
    nyse_cap = float(config.effective_nyse_sleeve_cap_pct()) if paper else float(
        getattr(config, "NYSE_SLEEVE_CAP_PCT", 0.67)
    )
    halt_dd = (
        float(config.PAPER_HALT_RESUME_DRAWDOWN_PCT)
        if paper
        else float(config.HALT_RESUME_DRAWDOWN_PCT)
    )
    daily_lim = (
        float(config.THINKING_DAILY_LOSS_LIMIT_PAPER)
        if paper
        else float(config.THINKING_DAILY_LOSS_LIMIT_LIVE)
    )
    flags = []
    for row in names:
        if row["pct"] > name_cap + 1e-6:
            flags.append(
                f"{row['symbol']} {row['pct']:.1%} > per-name cap {name_cap:.0%}"
            )
    nyse_pct = (sleeves.get("nyse", 0.0) / equity) if equity > 0 else 0.0
    if nyse_pct > nyse_cap + 1e-6:
        flags.append(f"NYSE sleeve {nyse_pct:.1%} > cap {nyse_cap:.0%}")
    sleeve_pct = {
        k: round(v / equity, 4) if equity > 0 else 0.0 for k, v in sorted(sleeves.items())
    }
    return {
        "book": label,
        "equity": round(equity, 2),
        "cash": round(cash, 2),
        "cash_pct": round(cash / equity, 4) if equity > 0 else None,
        "n_positions": len(positions),
        "sleeve_pct": sleeve_pct,
        "largest": names[:8],
        "caps": {
            "per_name": round(name_cap, 4),
            "nyse_sleeve": round(nyse_cap, 4),
            "daily_loss": round(daily_lim, 4),
            "halt_dd": round(halt_dd, 4),
        },
        "flags": flags,
        "advisory": True,
    }


def load_last_fill_audit() -> dict[str, Any] | None:
    if not OUT_JSON.is_file():
        return None
    try:
        return json.loads(OUT_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
