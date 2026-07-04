"""Shared data for Friday weekly account summaries."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import config
from modules.dynamic_universe import screener_universe_meta
from modules.sector_screener import load_sector_screener_snapshot
from modules.status_metrics import _merge_journal_series, fmt_pct, pct_change

_ET = ZoneInfo("America/New_York")
_TRADE_EVENTS = frozenset(
    {"signal", "fill", "exit", "entry", "buy", "sell", "game_plan"}
)


@dataclass
class WeeklySummaryData:
    account_label: str
    as_of: datetime
    equity: float
    cash: float
    invested_pct: float | None
    week_return_pct: float | None
    week_pnl_usd: float | None
    week_max_dd_pct: float | None
    vti_week_return_pct: float | None
    vs_vti_pct: float | None
    regime: str
    wisdom_mode: str
    volatility: str
    gap_tier: str
    sizing_multiplier: float | None
    sector_activity: str
    screener_meta: str
    top_positions: list[dict[str, Any]] = field(default_factory=list)
    open_positions: list[dict[str, Any]] = field(default_factory=list)
    key_trades: list[dict[str, Any]] = field(default_factory=list)
    sleeve_line: str = ""
    heartbeat_age_min: float | None = None


def week_id(d: date) -> str:
    return f"{d.isocalendar().year}-W{d.isocalendar().week:02d}"


def _week_start_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _load_heartbeat() -> dict[str, Any]:
    path = Path(config.HEARTBEAT_FILE)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _heartbeat_age_minutes(hb: dict[str, Any]) -> float | None:
    raw = hb.get("timestamp")
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_ET)
        delta = datetime.now(_ET) - ts.astimezone(_ET)
        return round(delta.total_seconds() / 60.0, 1)
    except (ValueError, TypeError):
        return None


def _resolve_account_snapshot(
    hb: dict[str, Any],
    *,
    equity: float | None,
    cash: float | None,
) -> tuple[float, float]:
    eq = equity
    ca = cash
    if eq is None:
        eq = float(hb.get("equity") or 0)
    if ca is None:
        ca = float(hb.get("cash") or 0)
    return eq, ca


def _resolve_wisdom(hb: dict[str, Any], wisdom: dict | None) -> dict[str, Any]:
    out = dict(hb.get("wisdom") or {})
    if wisdom:
        out.update(wisdom)
    return out


def _resolve_sleeves(hb: dict[str, Any], sleeves: dict | None) -> dict[str, Any]:
    if sleeves:
        return sleeves
    exp = hb.get("sleeve_exposure")
    if isinstance(exp, dict):
        merged = {"equity": hb.get("equity")}
        merged.update(exp)
        return merged
    return {}


def _equity_series_for_account(*, paper_chase: bool, live_only: bool) -> list[tuple[datetime, float]]:
    return _merge_journal_series(paper_chase=paper_chase, live_only=live_only)


def _week_equity_stats(
    series: list[tuple[datetime, float]],
    *,
    week_start: date,
    current_equity: float,
) -> tuple[float | None, float | None, float | None]:
    if current_equity <= 0:
        return None, None, None
    week_points = [(ts, eq) for ts, eq in series if ts.date() >= week_start]
    if not week_points:
        week_points = [(datetime.now(_ET), current_equity)]
    start_eq = week_points[0][1]
    if start_eq <= 0:
        return None, None, None
    week_ret = pct_change(current_equity, start_eq)
    week_pnl = current_equity - start_eq
    peak = start_eq
    max_dd = 0.0
    for _, eq in week_points + [(datetime.now(_ET), current_equity)]:
        peak = max(peak, eq)
        if peak > 0:
            max_dd = min(max_dd, (eq / peak - 1.0) * 100.0)
    return week_ret, week_pnl, max_dd if max_dd < 0 else 0.0


def _vti_week_return(week_start: date, as_of: date) -> float | None:
    try:
        from modules.data_loader import load_close_matrix

        sym = config.VTI_CORE_SYMBOL
        data = load_close_matrix(interval="1d")
        if sym not in data.columns:
            return None
        s = data[sym].dropna()
        if s.empty:
            return None
        start_slice = s.loc[str(week_start) :]
        end_slice = s.loc[: str(as_of)]
        if start_slice.empty or end_slice.empty:
            return None
        p0 = float(start_slice.iloc[0])
        p1 = float(end_slice.iloc[-1])
        if p0 <= 0:
            return None
        return (p1 / p0 - 1.0) * 100.0
    except Exception:
        return None


def _journal_paths() -> list[Path]:
    paths = [Path(config.PAPER_JOURNAL_CSV)]
    chase = Path("paper_chase_journal.csv")
    if chase.is_file():
        paths.insert(0, chase)
    wisdom = Path(getattr(config, "WISDOM_JOURNAL_FILE", "wisdom_journal.csv"))
    if wisdom.is_file():
        paths.append(wisdom)
    seen: set[Path] = set()
    out: list[Path] = []
    for p in paths:
        rp = p.resolve()
        if rp not in seen and p.is_file():
            seen.add(rp)
            out.append(p)
    return out


def _load_week_trades(week_start: date) -> list[dict[str, Any]]:
    import pandas as pd

    frames = []
    for path in _journal_paths():
        try:
            df = pd.read_csv(path)
            if not df.empty:
                frames.append(df)
        except Exception:
            continue
    if not frames:
        return []
    df = pd.concat(frames, ignore_index=True)
    if "timestamp" not in df.columns:
        return []
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"])
    df = df.loc[df["timestamp"].dt.date >= week_start]
    if "event" in df.columns:
        df = df.loc[df["event"].astype(str).str.lower().isin(_TRADE_EVENTS)]
    if df.empty:
        return []
    if "notional" in df.columns:
        df["_abs_n"] = pd.to_numeric(df["notional"], errors="coerce").abs()
        df = df.sort_values("_abs_n", ascending=False, na_position="last")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, row in df.iterrows():
        sym = str(row.get("symbol") or row.get("ticker") or "").strip()
        key = f"{row['timestamp']}|{sym}|{row.get('event')}|{row.get('side')}"
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "time": row["timestamp"].strftime("%Y-%m-%d %H:%M"),
                "event": str(row.get("event", "")),
                "symbol": sym or "-",
                "side": str(row.get("side", "") or "-"),
                "notional": float(row["_abs_n"])
                if "_abs_n" in row and pd.notna(row["_abs_n"])
                else None,
            }
        )
        if len(rows) >= 12:
            break
    return rows


def _position_rows(*, paper: bool) -> list[dict[str, Any]]:
    try:
        from modules.paper_journal import build_position_summary

        rows, _err = build_position_summary(paper=paper)
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "ticker": row.ticker,
                    "sleeve": row.sleeve,
                    "pnl_pct": row.unrealized_pnl_pct,
                    "pnl_usd": row.unrealized_pnl,
                    "mv": row.market_value,
                    "qty": row.qty,
                }
            )
        return out
    except Exception:
        return []


def _sector_activity_line() -> str:
    snap = load_sector_screener_snapshot() or {}
    active = snap.get("active_sectors") or []
    expanded = snap.get("expanded_sectors") or []
    added = snap.get("added_tickers") or []
    if not active and not expanded:
        return "No sector expansion this week."
    parts = []
    if active:
        parts.append("Active: " + ", ".join(active))
    if expanded:
        parts.append("Expanded: " + ", ".join(expanded))
    if added:
        preview = ", ".join(str(t) for t in added[:6])
        if len(added) > 6:
            preview += f" (+{len(added) - 6})"
        parts.append(f"Added: {preview}")
    return " | ".join(parts)


def _screener_meta_line() -> str:
    meta = screener_universe_meta()
    if not meta.get("exists"):
        return "Screener: no file yet."
    age = meta.get("age_days")
    age_s = f"{age:.1f}d" if age is not None else "n/a"
    return f"Universe: {meta.get('count', 0)} tickers ({age_s})"


def _sleeve_line(sleeves: dict | None, hb: dict[str, Any]) -> str:
    sleeves = sleeves or {}
    try:
        eq = float(sleeves.get("equity") or hb.get("equity") or 0)
        if eq <= 0:
            return ""
        spy = float(sleeves.get("spy_value") or 0)
        nyse = float(sleeves.get("nyse_value") or 0)
        crypto = float(sleeves.get("crypto_value") or 0)
        vti = float(sleeves.get("vti_core_value") or 0)
        invested = spy + nyse + crypto + vti
        return (
            f"VTI ${vti:,.0f} | SPY ${spy:,.0f} | NYSE ${nyse:,.0f} | "
            f"Crypto ${crypto:,.0f} ({invested / eq * 100:.0f}% deployed)"
        )
    except (TypeError, ValueError):
        return ""


def gather_weekly_summary(
    *,
    equity: float | None = None,
    cash: float | None = None,
    regime: str = "",
    wisdom: dict | None = None,
    sleeves: dict | None = None,
    paper: bool | None = None,
    heartbeat: dict[str, Any] | None = None,
) -> WeeklySummaryData:
    paper = config.PAPER_TRADING if paper is None else paper
    hb = heartbeat if heartbeat is not None else _load_heartbeat()
    equity, cash = _resolve_account_snapshot(hb, equity=equity, cash=cash)
    wisdom = _resolve_wisdom(hb, wisdom)
    sleeves = _resolve_sleeves(hb, sleeves)

    now_et = datetime.now(_ET)
    week_start = _week_start_monday(now_et.date())
    label = "Paper" if paper else "Live"

    invested_pct = None
    if equity > 0:
        invested_pct = max(0.0, min(100.0, (equity - cash) / equity * 100.0))

    series = _equity_series_for_account(
        paper_chase=config.paper_chase_mode_enabled(),
        live_only=not paper,
    )
    week_ret, week_pnl, week_dd = _week_equity_stats(
        series, week_start=week_start, current_equity=equity
    )
    vti_ret = _vti_week_return(week_start, now_et.date())
    vs_vti = None
    if week_ret is not None and vti_ret is not None:
        vs_vti = week_ret - vti_ret

    hb_regime = str(hb.get("regime") or "")
    positions = _position_rows(paper=paper)
    ranked = sorted(positions, key=lambda p: p.get("pnl_pct") or 0, reverse=True)

    return WeeklySummaryData(
        account_label=label,
        as_of=now_et,
        equity=equity,
        cash=cash,
        invested_pct=invested_pct,
        week_return_pct=week_ret,
        week_pnl_usd=week_pnl,
        week_max_dd_pct=week_dd,
        vti_week_return_pct=vti_ret,
        vs_vti_pct=vs_vti,
        regime=regime or hb_regime or str(wisdom.get("regime") or wisdom.get("mode") or "n/a"),
        wisdom_mode=str(wisdom.get("mode", config.WISDOM_MODE)),
        volatility=str(wisdom.get("gap_tier") or wisdom.get("volatility") or "n/a"),
        gap_tier=str(wisdom.get("gap_tier", "n/a")),
        sizing_multiplier=wisdom.get("sizing_multiplier"),
        top_positions=ranked[:5],
        open_positions=sorted(positions, key=lambda p: p.get("mv") or 0, reverse=True),
        key_trades=_load_week_trades(week_start),
        sector_activity=_sector_activity_line(),
        screener_meta=_screener_meta_line(),
        sleeve_line=_sleeve_line(sleeves, hb),
        heartbeat_age_min=_heartbeat_age_minutes(hb),
    )


def format_weekly_telegram_message(data: WeeklySummaryData) -> str:
    """Compact Friday summary for Telegram (4096 char limit)."""
    iso = data.as_of.isocalendar()
    header = (
        f"[PythonTrading {data.account_label}] Weekly summary\n"
        f"Week {iso.week} ({iso.year}) - {data.as_of:%a %b %d, %Y}\n"
    )
    pnl_s = f"${data.week_pnl_usd:+,.0f}" if data.week_pnl_usd is not None else "n/a"
    lines = [
        header,
        f"Equity: ${data.equity:,.2f} ({fmt_pct(data.week_return_pct)} week, {pnl_s} P&L)",
    ]
    if data.vti_week_return_pct is not None:
        lines.append(
            f"vs {config.VTI_CORE_SYMBOL}: {fmt_pct(data.vs_vti_pct)} "
            f"(bench {fmt_pct(data.vti_week_return_pct)})"
        )
    if data.week_max_dd_pct is not None:
        lines.append(f"Max DD: {data.week_max_dd_pct:.2f}%")
    if data.invested_pct is not None:
        lines.append(f"Cash: ${data.cash:,.0f} | Invested: {data.invested_pct:.0f}%")

    lines.append(f"\nRegime: {data.regime}")
    lines.append(f"Wisdom: {data.wisdom_mode} | Gap: {data.gap_tier}")
    if data.sleeve_line:
        lines.append(data.sleeve_line)

    if data.key_trades:
        lines.append("\nKey trades:")
        for t in data.key_trades[:5]:
            n = t.get("notional")
            n_s = f"${n:,.0f}" if n is not None else ""
            lines.append(f"  {t['time']} {t['side']} {t['symbol']} {n_s}".rstrip())
    else:
        lines.append("\nKey trades: none")

    lines.append(f"\nSector: {data.sector_activity}")
    lines.append(data.screener_meta)

    if data.top_positions:
        lines.append("\nTop positions:")
        for p in data.top_positions:
            lines.append(
                f"  {p['ticker']}: {p['pnl_pct']:+.1f}% (${p['pnl_usd']:+,.0f})"
            )

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n...(trunc)"
    return text
