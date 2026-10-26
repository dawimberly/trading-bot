"""Clean Telegram daily summary for books running ``h1d_trend_mix`` exclusively.

Replaces the legacy daily bodies (regime label, sleeve caps, health score,
entry-skip blockers, log paths) only when ``config.h1d_trend_mix_exclusive()``
is true for this book. Other books (Lab) keep the old format.

Reads only: account, positions, today's orders, the bot's SPY bars, the h1d
state file, the heartbeat and the book log. Never submits or cancels.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import config

_ET = ZoneInfo("America/New_York")
_CT = ZoneInfo("America/Chicago")
logger = logging.getLogger(__name__)

BOOK_LABELS = {"alpaca_live": "Live", "alpaca_paper_v2": "Medium", "alpaca_paper": "Lab"}
HB_STALE_MIN = 15
DUST_WEIGHT_PCT = 0.05  # positions below this weight are summarized, not listed


def h1d_summary_active() -> bool:
    try:
        return bool(config.h1d_trend_mix_exclusive())
    except Exception:
        return False


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def _scrub(text: Any) -> str:
    text = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<id>", str(text), flags=re.I)
    text = re.sub(r"(key|secret|token|password)[^\s]*\s*[=:]\s*\S+", r"\1=<hidden>", text, flags=re.I)
    text = re.sub(r"\b(PK|AK|CK)[A-Z0-9]{12,}\b", "<key>", text)
    text = re.sub(r"\b[A-Z0-9]{2,3}\d{6,}\b", "<acct>", text)
    return text[:200]


def next_rebalance_date(today: date) -> date:
    """First NYSE session of next month."""
    from modules.h1d_trend_mix import is_nyse_session

    d = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    for _ in range(10):
        if is_nyse_session(d):
            return d
        d += timedelta(days=1)
    return d


def in_mix_pct(weights: dict[str, float], targets: dict[str, float]) -> float | None:
    """Overlap with target: 100 * sum(min(actual, target)). Cash counts as off-mix."""
    if not targets:
        return None
    return 100.0 * sum(min(max(weights.get(s, 0.0), 0.0), w) for s, w in targets.items())


def classify_orders(orders: list[dict[str, Any]], strategy_symbols: set[str]) -> dict[str, list]:
    """Split today's orders into h1d trades, switch sells of legacy names, rejects."""
    out: dict[str, list] = {"h1d": [], "switch_sells": [], "rejected": []}
    for o in orders:
        status = str(o.get("status") or "").lower()
        sym = str(o.get("symbol") or "").upper()
        if status in ("rejected", "canceled", "cancelled", "expired"):
            out["rejected"].append(o)
        elif sym not in strategy_symbols and str(o.get("side") or "").lower() == "sell":
            out["switch_sells"].append(o)
        else:
            out["h1d"].append(o)
    return out


def _pct(v: float | None, signed: bool = True, d: int = 2) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.{d}f}%" if signed else f"{v:.{d}f}%"


def _money(v: float | None, signed: bool = False) -> str:
    if v is None:
        return "n/a"
    return f"{'+' if signed and v >= 0 else '-' if v < 0 else ''}${abs(v):,.2f}"


def _order_line(o: dict[str, Any]) -> str:
    side = str(o.get("side") or "?").upper()
    sym = o.get("symbol") or "?"
    status = str(o.get("status") or "?").lower()
    amt = _f(o.get("filled_value"))
    if amt is None:
        amt = _f(o.get("notional"))
    amt_s = f" {_money(amt)}" if amt is not None else ""
    return f"  {side} {sym}{amt_s} ({status})"


def _compact_orders(rows: list[dict[str, Any]], limit: int) -> list[str]:
    lines = [_order_line(o) for o in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"  ... +{len(rows) - limit} more")
    return lines


def format_h1d_summary(data: dict[str, Any]) -> str:
    as_of: datetime = data.get("as_of") or datetime.now(_ET)
    label = data.get("book_label") or "Book"
    lines = [f"{label} - H1D 50/50 mix - {as_of.astimezone(_ET):%a %Y-%m-%d %H:%M} ET", ""]
    lines.append(f"Equity:    {_money(_f(data.get('equity')))}")
    lines.append(f"Today:     {_money(_f(data.get('day_pnl')), signed=True)} ({_pct(_f(data.get('day_pct')))})")
    lines.append(
        f"Invested:  {_pct(_f(data.get('invested_pct')), signed=False, d=1)}  |  "
        f"Cash: {_pct(_f(data.get('cash_pct')), signed=False, d=1)}"
    )
    lines.append(f"In mix:    {_pct(_f(data.get('in_mix_pct')), signed=False, d=1)} of target")

    sig = data.get("signal") or {}
    if sig.get("risk_on") is None:
        lines.append("SPY trend: n/a (no SPY data)")
    else:
        state = "RISK-ON (SSO)" if sig["risk_on"] else "RISK-OFF (BIL)"
        lines.append(
            f"SPY trend: {state} - SPY {_f(sig.get('close')) or 0:,.2f} vs 200d "
            f"{_f(sig.get('sma')) or 0:,.2f} ({_pct(_f(sig.get('gap_pct')))})"
        )
    nr = data.get("next_rebalance")
    if nr:
        lines.append(f"Next rebal: {nr:%a %b %d} (sooner on a SPY signal flip)")

    lines += ["", "Positions (weight / today)"]
    allpos = list(data.get("positions") or [])
    pos = [p for p in allpos if (_f(p.get("weight_pct")) or 0.0) >= DUST_WEIGHT_PCT]
    dust = [p for p in allpos if p not in pos]
    if not allpos:
        lines.append("  (none)")
    for p in pos:
        off = "" if p.get("in_target", True) else "  [not in mix]"
        lines.append(
            f"  {p['symbol']:<4} {_pct(_f(p.get('weight_pct')), signed=False, d=1):>6}  "
            f"{_pct(_f(p.get('today_pct')))}{off}"
        )

    if dust:
        names = ", ".join(str(p.get("symbol")) for p in dust[:8]) + (" ..." if len(dust) > 8 else "")
        lines.append(f"  + {len(dust)} leftover dust (<{DUST_WEIGHT_PCT}%): {names}")

    orders = data.get("orders") or {}
    h1d, sw, rej = orders.get("h1d") or [], orders.get("switch_sells") or [], orders.get("rejected") or []
    lines += ["", "Orders today"]
    if not (h1d or sw or rej):
        lines.append("  none")
    if h1d:
        lines.append(f" H1D trades ({len(h1d)}):")
        lines += _compact_orders(h1d, 16)
    if sw:
        total = sum((_f(o.get("filled_value")) or 0.0) for o in sw)
        names = sorted({str(o.get("symbol")) for o in sw})
        lines.append(f" Switch sells of old names: {len(sw)} fills, {_money(total)} across {len(names)} names")
    if rej:
        lines.append(f" Rejected/canceled ({len(rej)}):")
        lines += _compact_orders(rej, 6)

    health = data.get("health") or {}
    errs = int(health.get("errors_24h") or 0)
    lines += ["", f"Errors 24h: {errs}  |  " + ("HALTED" if health.get("halted") else "not halted")]
    for e in (health.get("recent_errors") or [])[-2:]:
        lines.append(f"  {str(e)[:90]}")
    age = _f(health.get("hb_age_min"))
    if age is None:
        lines.append("Heartbeat: missing")
    else:
        stale = "  STALE" if age > HB_STALE_MIN else ""
        lines.append(f"Heartbeat: {health.get('hb_time', '?')} CT ({age:.0f} min ago){stale}")
    return "\n".join(lines)


# --------------------------------------------------------------------- gather
def _heartbeat_path() -> Path:
    return Path(getattr(config, "HEARTBEAT_FILE", "bot_heartbeat.json")).resolve()


def local_health(heartbeat: Path, now: datetime | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    now_ct = (now or datetime.now(_CT)).astimezone(_CT)
    try:
        hb = json.loads(heartbeat.read_text(encoding="utf-8"))
    except Exception:
        hb = {}
    ts = hb.get("timestamp")
    if ts:
        try:
            t = datetime.fromisoformat(ts)
            if t.tzinfo is None:
                t = t.replace(tzinfo=_CT)  # bots write naive local time
            out["hb_age_min"] = max(0.0, (now_ct - t).total_seconds() / 60)
            out["hb_time"] = t.astimezone(_CT).strftime("%I:%M %p")
        except ValueError:
            pass
    out["halted"] = bool(hb.get("halted"))
    errs: list[str] = []
    log = heartbeat.parent / "bot.log"
    if log.is_file():
        try:
            with open(log, "rb") as fh:
                fh.seek(max(0, log.stat().st_size - 400_000))
                rows = fh.read().decode("utf-8", "replace").splitlines()
            cutoff = (now_ct.replace(tzinfo=None) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
            for ln in rows:
                if (" ERROR " in ln or " CRITICAL " in ln) and ln[:16] >= cutoff:
                    errs.append(_scrub(ln))
        except Exception:
            pass
    out["errors_24h"] = len(errs)
    out["recent_errors"] = errs[-2:]
    return out


def _spy_signal(now_et: datetime) -> dict[str, Any]:
    from modules import h1d_trend_mix as h

    session = h.next_open_session_date(now_et)
    bars = sorted(h._load_spy_bars() or [])
    used = [c for d, c in bars if d < session]
    if len(used) < h.SMA_WINDOW:
        return {"risk_on": None}
    sma = sum(used[-h.SMA_WINDOW:]) / h.SMA_WINDOW
    close = used[-1]
    return {"risk_on": close > sma, "close": close, "sma": sma, "gap_pct": 100.0 * (close / sma - 1.0)}


def gather_h1d_summary(broker: Any = None, *, now: datetime | None = None) -> dict[str, Any]:
    from modules import h1d_trend_mix as h

    now_et = (now or datetime.now(_ET)).astimezone(_ET)
    book = os.getenv("TRADING_BOOK_ID", "").strip()
    if broker is None:
        broker = h._read_only_for_book(book)
    acct = broker._get_account()
    equity = _f(getattr(acct, "equity", None)) or 0.0
    cash = _f(getattr(acct, "cash", None)) or 0.0
    last_eq = _f(getattr(acct, "last_equity", None))
    sig = _spy_signal(now_et)
    targets = h.target_weights(bool(sig["risk_on"])) if sig.get("risk_on") is not None else {}
    weights: dict[str, float] = {}
    positions = []
    for p in broker._get_positions() or []:
        sym = str(p.symbol).upper()
        mv = _f(p.market_value) or 0.0
        if equity > 0 and mv:
            weights[sym] = mv / equity
        ct = _f(getattr(p, "change_today", None))
        positions.append({"symbol": sym, "weight_pct": 100.0 * mv / equity if equity else None,
                          "today_pct": 100.0 * ct if ct is not None else None,
                          "in_target": (sym in targets) if targets else True})
    positions.sort(key=lambda r: -(r["weight_pct"] or 0))
    orders: list[dict[str, Any]] = []
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        since = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = broker.client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.ALL, after=since, limit=500))
        for o in rows:
            fq, avg = _f(o.filled_qty), _f(o.filled_avg_price)
            orders.append({"symbol": str(o.symbol), "side": str(getattr(o.side, "value", o.side)),
                           "status": str(getattr(o.status, "value", o.status)), "notional": _f(o.notional),
                           "filled_value": fq * avg if fq and avg else None})
    except Exception as exc:
        logger.debug("orders unavailable: %s", type(exc).__name__)
    return {
        "as_of": now_et,
        "book_label": BOOK_LABELS.get(book, book or "Book"),
        "equity": equity,
        "day_pnl": equity - last_eq if last_eq else None,
        "day_pct": 100.0 * (equity / last_eq - 1.0) if last_eq else None,
        "cash_pct": 100.0 * cash / equity if equity else None,
        "invested_pct": 100.0 * (1.0 - cash / equity) if equity else None,
        "in_mix_pct": in_mix_pct(weights, targets),
        "signal": sig,
        "next_rebalance": next_rebalance_date(now_et.date()),
        "positions": positions,
        "orders": classify_orders(orders, set(h.STRATEGY_SYMBOLS)),
        "health": local_health(_heartbeat_path()),
    }


def build_h1d_summary(broker: Any = None) -> str | None:
    """Formatted body, or None when unavailable (caller falls back to legacy)."""
    try:
        return format_h1d_summary(gather_h1d_summary(broker))
    except Exception as exc:
        logger.warning("h1d summary unavailable: %s", type(exc).__name__)
        return None
