"""Protective / sector short activity for dashboard and Telegram (paper only)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import config


def _is_short_journal_row(row) -> bool:
    sleeve = str(row.get("sleeve") or "").upper()
    pk = str(row.get("pair_key") or row.get("reason") or row.get("notes") or "")
    strategy = str(row.get("strategy") or "")
    return (
        sleeve == "SHORT"
        or strategy == "opportunistic_short"
        or "/SHORT/" in pk.upper()
        or "SECTOR_SHORT" in pk.upper()
    )


def _parse_ts(val) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _week_cutoff() -> datetime:
    return datetime.now() - timedelta(days=7)


def _load_paper_journal_df():
    try:
        from modules.paper_journal import read_journal

        return read_journal(tail=500)
    except Exception:
        return None


def gather_short_activity(
    *,
    positions_df=None,
    journal_df=None,
    regime: str = "",
) -> dict[str, Any]:
    """Build short sleeve snapshot from Alpaca positions and paper journal."""
    out: dict[str, Any] = {
        "enabled": config.effective_opportunistic_short_enabled(),
        "regime": regime or "",
        "open_positions": [],
        "gross_short_usd": 0.0,
        "recent_fires": [],
        "week_trades": [],
        "week_pnl_usd": None,
        "week_notional_usd": 0.0,
        "trigger_reasons": [],
        "banner": config.format_opportunistic_short_banner()
        if config.effective_opportunistic_short_enabled()
        else "Protective Shorts: OFF",
    }
    if not out["enabled"]:
        return out

    if positions_df is not None and not getattr(positions_df, "empty", True):
        for _, row in positions_df.iterrows():
            try:
                qty = float(row.get("Qty") or row.get("qty") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            if qty >= 0:
                continue
            sym = str(row.get("Symbol") or row.get("symbol") or "")
            try:
                mv = abs(float(row.get("Value $") or row.get("market_value") or 0))
            except (TypeError, ValueError):
                mv = 0.0
            out["open_positions"].append(
                {"symbol": sym, "qty": qty, "value_usd": mv}
            )
            out["gross_short_usd"] += mv

    if journal_df is None:
        journal_df = _load_paper_journal_df()

    if journal_df is None or getattr(journal_df, "empty", True):
        return out

    cutoff = _week_cutoff()
    reasons: list[str] = []
    week_rows: list[dict] = []
    for _, row in journal_df.iterrows():
        if not _is_short_journal_row(row):
            continue
        ts = _parse_ts(row.get("timestamp"))
        if ts and ts.replace(tzinfo=None) < cutoff.replace(tzinfo=None):
            continue
        sym = str(row.get("symbol") or row.get("ticker") or "")
        side = str(row.get("side") or "").lower()
        event = str(row.get("event") or "")
        pk = str(row.get("pair_key") or row.get("reason") or row.get("notes") or "")
        try:
            notional = float(row.get("notional") or 0)
        except (TypeError, ValueError):
            notional = 0.0
        if notional != notional:
            notional = 0.0
        entry = {
            "time": ts.strftime("%m-%d %H:%M") if ts else "",
            "symbol": sym or pk.split("/")[0],
            "side": side,
            "event": event,
            "notional": notional,
            "reason": pk[:120],
        }
        week_rows.append(entry)
        out["week_notional_usd"] += abs(notional)
        if side == "sell" or event in ("fill", "entry", "signal"):
            trig = pk
            if "|" in pk and "RHYME" in pk.upper():
                reasons.append(trig.split("|sector=")[0][:80])
            elif trig and trig not in reasons:
                reasons.append(trig[:80])

    week_rows.sort(key=lambda r: r.get("time") or "", reverse=True)
    out["week_trades"] = week_rows[:12]
    out["recent_fires"] = [r for r in week_rows if r.get("side") == "sell"][:5]
    out["trigger_reasons"] = reasons[:5]
    return out


def short_activity_dashboard_rows(snapshot: dict[str, Any]) -> tuple[list[dict], str | None]:
    """Rows for dashboard DataTable + optional error/status."""
    if not snapshot.get("enabled"):
        return [], "Protective shorts off (paper only)"
    rows: list[dict] = []
    for pos in snapshot.get("open_positions") or []:
        rows.append(
            {
                "Type": "Open",
                "Symbol": pos.get("symbol", ""),
                "Detail": f"{pos.get('qty', 0):.2f} sh",
                "Notional": f"${float(pos.get('value_usd') or 0):,.0f}",
                "Trigger": "—",
                "_tag": "short_open",
            }
        )
    for fire in snapshot.get("recent_fires") or []:
        rows.append(
            {
                "Type": "Fire",
                "Symbol": fire.get("symbol", ""),
                "Detail": fire.get("event", "entry"),
                "Notional": f"${abs(float(fire.get('notional') or 0)):,.0f}",
                "Trigger": (fire.get("reason") or "")[:48],
                "_tag": "short_fire",
            }
        )
    if not rows and not snapshot.get("week_trades"):
        return [], None
    return rows, None


def format_short_activity_status(snapshot: dict[str, Any]) -> str:
    """One-line summary for dashboard header."""
    if not snapshot.get("enabled"):
        return "Off (paper only)"
    n_open = len(snapshot.get("open_positions") or [])
    n_fires = len(snapshot.get("recent_fires") or [])
    gross = float(snapshot.get("gross_short_usd") or 0)
    parts = [f"{n_open} open", f"{n_fires} fire(s) this week"]
    if gross > 0:
        parts.append(f"${gross:,.0f} gross")
    return " · ".join(parts)


def format_shorts_telegram_block(
    *,
    positions_df=None,
    journal_df=None,
    regime: str = "",
    equity: float | None = None,
) -> str:
    """Compact block for /shorts and /status."""
    snap = gather_short_activity(
        positions_df=positions_df, journal_df=journal_df, regime=regime
    )
    if not snap.get("enabled"):
        return "Protective shorts: OFF (paper only)."
    lines = ["Protective shorts (paper):"]
    lines.append(snap.get("banner") or "")
    if regime:
        lines.append(f"Regime: {regime}")
    gross = float(snap.get("gross_short_usd") or 0)
    if equity and equity > 0 and gross > 0:
        lines.append(f"Exposure: ${gross:,.0f} ({100 * gross / equity:.1f}% gross)")
    elif gross > 0:
        lines.append(f"Gross short: ${gross:,.0f}")
    opens = snap.get("open_positions") or []
    if opens:
        labels = ", ".join(
            f"{p['symbol']} ({abs(p['qty']):.0f})" for p in opens[:4]
        )
        lines.append(f"Open: {labels}")
    else:
        lines.append("Open: none")
    fires = snap.get("recent_fires") or []
    lines.append(f"Week fires: {len(fires)}")
    for reason in snap.get("trigger_reasons") or []:
        lines.append(f"  • {reason}")
    if not fires and not opens:
        lines.append("No short activity this week.")
    return "\n".join(lines)


def format_weekly_shorts_telegram_block(
    *,
    short_trades: list[dict] | None = None,
    short_pnl_usd: float | None = None,
) -> str:
    """Friday Telegram addon — protective shorts block."""
    if not config.effective_opportunistic_short_enabled():
        return ""
    trades = short_trades or []
    lines = ["\n— Protective Shorts —", config.format_opportunistic_short_banner()]
    lines.append(f"Trades this week: {len(trades)}")
    if short_pnl_usd is not None:
        lines.append(f"Short PnL (est.): ${short_pnl_usd:+,.2f}")
    reasons: list[str] = []
    for t in trades[:5]:
        r = str(t.get("reason") or "")
        if r and r not in reasons:
            reasons.append(r.split("|sector=")[0][:72])
    if reasons:
        lines.append("Triggers: " + "; ".join(reasons[:3]))
    elif not trades:
        lines.append("No short entries this week.")
    return "\n".join(lines)
