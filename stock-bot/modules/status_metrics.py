"""Equity / performance helpers for status.py banner (since-start, rotating insight)."""

from __future__ import annotations

import logging

import os
from datetime import date, datetime, timedelta
from pathlib import Path

import config

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
EQUITY_EVENTS = frozenset({"cycle", "fill", "startup", "halt", "signal"})


def status_inception_date() -> date:
    raw = (os.getenv("STATUS_INCEPTION_DATE") or "2026-01-01").strip()
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return date(2026, 1, 1)


def _env_date(key: str) -> date | None:
    raw = (os.getenv(key) or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def start_equity_env_key(*, paper_chase: bool, book_id: str | None = None) -> str:
    """Per-book baseline. Medium and aggressive do not share one paper start."""
    if book_id == "alpaca_paper_v2":
        return "STATUS_MEDIUM_START_EQUITY"
    if book_id == "alpaca_paper":
        return "STATUS_AGGRESSIVE_START_EQUITY"
    if paper_chase:
        return "STATUS_PAPER_START_EQUITY"
    return "STATUS_LIVE_START_EQUITY"


def _env_float(key: str) -> float | None:
    raw = (os.getenv(key) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _read_csv_tail(path: Path, max_rows: int) -> "pd.DataFrame":
    from modules.csv_utils import read_csv_tail as _safe_read_csv_tail

    return _safe_read_csv_tail(path, max_rows)


def _read_equity_journal(path: Path, *, tail_rows: int = 12_000) -> list[tuple[datetime, float]]:
    if not path.is_file():
        return []
    try:
        import pandas as pd

        # Large paper journals can freeze the dashboard if read fully on the UI thread.
        if tail_rows > 0 and path.stat().st_size > 256_000:
            from modules.csv_utils import coerce_trade_journal_df, read_csv_tail

            df = coerce_trade_journal_df(read_csv_tail(path, tail_rows))
            if "timestamp" in df.columns:
                df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        else:
            from modules.csv_utils import coerce_trade_journal_df, read_csv_file

            df = coerce_trade_journal_df(read_csv_file(path))
            if "timestamp" in df.columns:
                df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    except Exception:
        return []
    if df.empty or "equity" not in df.columns:
        return []
    if "event" in df.columns:
        df = df.loc[df["event"].astype(str).isin(EQUITY_EVENTS)].copy()
    df["equity"] = pd.to_numeric(df["equity"], errors="coerce")
    df = df.dropna(subset=["timestamp", "equity"])
    if df.empty:
        return []
    df = df.sort_values("timestamp")
    return [(row.timestamp.to_pydatetime(), float(row.equity)) for _, row in df.iterrows()]


def _journal_paths(*, paper_chase: bool) -> list[Path]:
    paths: list[Path] = []
    if paper_chase:
        chase = os.getenv("PAPER_CHASE_JOURNAL", "paper_chase_journal.csv")
        paths.append(ROOT / chase)
    paths.append(ROOT / config.PAPER_JOURNAL_CSV)
    hb_journal = os.getenv("JOURNAL_CSV", "").strip()
    if hb_journal:
        paths.append(Path(hb_journal))
    seen: set[Path] = set()
    out: list[Path] = []
    for p in paths:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            out.append(p)
    return out


def _merge_journal_series(
    *,
    paper_chase: bool,
    live_only: bool,
    extra_paths: list[Path] | None = None,
) -> list[tuple[datetime, float]]:
    import pandas as pd

    parts: list[tuple[datetime, float]] = []
    if extra_paths:
        for path in extra_paths:
            parts.extend(_read_equity_journal(path))
    for path in _journal_paths(paper_chase=paper_chase):
        parts.extend(_read_equity_journal(path))
    if not parts:
        return []
    df = pd.DataFrame(parts, columns=["timestamp", "equity"])
    df = df.drop_duplicates(subset=["timestamp"], keep="last").sort_values("timestamp")
    if live_only:
        try:
            from modules.wisdom_evaluator import filter_paper_journal

            seg, _ = filter_paper_journal(df, live_only=True)
            if not seg.empty and "equity" in seg.columns:
                df = seg[["timestamp", "equity"]].copy()
        except Exception:
            cap = float(getattr(config, "SMALL_ACCOUNT_EQUITY_THRESHOLD", 500))
            df = df.loc[df["equity"] < cap * 2]
    elif paper_chase:
        try:
            from modules.wisdom_evaluator import filter_paper_journal

            seg, _ = filter_paper_journal(df, live_only=False)
            if not seg.empty and "equity" in seg.columns:
                large = seg.loc[seg["equity"].astype(float) >= 1000]
                df = large if not large.empty else seg[["timestamp", "equity"]].copy()
        except Exception:
            df = df.loc[df["equity"] >= 1000]
    return [(row.timestamp.to_pydatetime(), float(row.equity)) for _, row in df.iterrows()]


def resolve_start_equity(
    *,
    paper_chase: bool,
    live_only: bool,
    inception: date | None = None,
    env_override_key: str,
    extra_journal_paths: list[Path] | None = None,
) -> tuple[float | None, date | None, str]:
    """Return (start_equity, effective_start_date, source_label)."""
    override = _env_float(env_override_key)
    if override is not None and override > 0:
        inc = inception or status_inception_date()
        dated = _env_date(env_override_key.replace("EQUITY", "DATE"))
        return override, dated or inc, "env"

    inc = inception or status_inception_date()
    inc_dt = datetime.combine(inc, datetime.min.time())
    series = _merge_journal_series(
        paper_chase=paper_chase,
        live_only=live_only,
        extra_paths=extra_journal_paths,
    )
    if not series:
        return None, None, "no journal"

    after = [(ts, eq) for ts, eq in series if ts.date() >= inc]
    pick = after if after else series
    ts0, eq0 = pick[0]
    return eq0, ts0.date(), "journal"


def pct_change(current: float | None, start: float | None) -> float | None:
    if current is None or start is None or start <= 0:
        return None
    return (current / start - 1.0) * 100.0


def fmt_pct(pct: float | None, *, signed: bool = True) -> str:
    if pct is None:
        return "n/a"
    if signed:
        return f"{pct:+.1f}%"
    return f"{pct:.1f}%"


def fmt_since_start_line(
    *,
    current: float | None,
    start: float | None,
    start_date: date | None,
    inception: date,
    label: str = "Since Start",
) -> str:
    pct = pct_change(current, start)
    if pct is None:
        return f"      {label}: n/a (set STATUS_*_START_EQUITY in .env or wait for journal equity rows)"
    basis = start_date.isoformat() if start_date else inception.isoformat()
    if start_date and start_date > inception:
        basis = f"{basis} (first journal point after {inception.isoformat()})"
    else:
        basis = inception.isoformat()
    cur_s = f"${current:,.2f}" if current is not None else "n/a"
    st_s = f"${start:,.2f}" if start is not None else "n/a"
    return f"      {label}: {fmt_pct(pct)} since {basis} ({st_s} -> {cur_s})"


def account_total_line(*, live_eq: float | None, paper_eq: float | None) -> str:
    parts: list[str] = []
    if live_eq is not None:
        parts.append(f"Live {_money(live_eq)}")
    if paper_eq is not None:
        parts.append(f"Paper {_money(paper_eq)}")
    if live_eq is not None and paper_eq is not None:
        total = live_eq + paper_eq
        return f"Account Total:  {_money(total)}  ({' + '.join(parts)})"
    if paper_eq is not None:
        return f"Account Total:  {_money(paper_eq)}  (Paper only)"
    if live_eq is not None:
        return f"Account Total:  {_money(live_eq)}  (Live only)"
    return "Account Total:  n/a"


def combined_since_start_pct(
    *,
    live_eq: float | None,
    paper_eq: float | None,
    live_start: float | None,
    paper_start: float | None,
) -> float | None:
    if live_eq is not None and paper_eq is not None:
        if live_start is None or paper_start is None:
            return None
        total_now = live_eq + paper_eq
        total_start = live_start + paper_start
        if total_start <= 0:
            return None
        return (total_now / total_start - 1.0) * 100.0
    if paper_eq is not None:
        return pct_change(paper_eq, paper_start)
    if live_eq is not None:
        return pct_change(live_eq, live_start)
    return None


def _money(val: float | None) -> str:
    if val is None:
        return "n/a"
    return f"${val:,.2f}"


def daily_breaker_banner(*, live_active: bool, live_dl: dict, paper_dl: dict) -> str:
    def _one(label: str, dl: dict) -> str:
        raw_limit = dl.get("limit_pct")
        limit_s = f"{float(raw_limit):.0f}%" if raw_limit not in (None, "") else "unavailable"
        if dl.get("tripped"):
            loss = dl.get("loss_pct")
            loss_s = f"{loss:.2f}%" if loss is not None else "?"
            return f"{label} TRIPPED ({loss_s} vs {limit_s})"
        loss = dl.get("loss_pct")
        if loss is not None:
            return f"{label} {loss:+.2f}% vs strategy anchor, limit {limit_s} (not the broker close)"
        return f"{label} strategy anchor unavailable; not verified"

    if live_active:
        return f"Daily Breaker:  {_one('Live', live_dl)} | {_one('Paper', paper_dl)}"
    return f"Daily Breaker:  {_one('Paper', paper_dl)}"


def _load_scorecard() -> dict | None:
    path = Path(getattr(config, "WISDOM_SCORECARD_FILE", "wisdom_scorecard.json"))
    if not path.is_file():
        path = ROOT / "wisdom_scorecard.json"
    if not path.is_file():
        return None
    try:
        import json

        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def metrics_30d(*, paper_chase: bool) -> dict:
    """30d return / Sharpe / max DD from wisdom scorecard or journal."""
    scorecard = _load_scorecard()
    live = (scorecard or {}).get("live") or {}
    if live.get("return_pct") is not None and int(live.get("window_days") or 0) == 30:
        return {
            "return_pct": float(live.get("return_pct") or 0),
            "sharpe": float(live.get("sharpe") or 0),
            "max_drawdown_pct": float(live.get("max_drawdown_pct") or 0),
            "source": "scorecard",
        }
    try:
        from modules.wisdom_evaluator import live_metrics

        book = "paper" if paper_chase else "live"
        m = live_metrics(30, book_type=book, live_only=not paper_chase)
        if m:
            return {
                "return_pct": float(m.get("return_pct") or 0),
                "sharpe": float(m.get("sharpe") or 0),
                "max_drawdown_pct": float(m.get("max_drawdown_pct") or 0),
                "source": "journal",
            }
    except Exception as exc:
        logger.debug("status metrics soft-fail: %s", exc)
    return {"return_pct": None, "sharpe": None, "max_drawdown_pct": None, "source": "none"}


def _selected_book_breaker(
    *,
    book_id: str | None,
    equity: float | None,
    heartbeat: dict | None,
    now: datetime | None = None,
    state_path: Path | None = None,
) -> str:
    from zoneinfo import ZoneInfo

    from modules.dashboard_safety import format_daily_breaker, read_safety_snapshot, safety_state_path
    from modules.trading_books import BOOKS

    limit_ratio = None
    spec = BOOKS.get(book_id or "")
    if spec and "default_paper" in spec:
        try:
            from modules.trading_safety import daily_loss_limit_pct

            limit_ratio = float(daily_loss_limit_pct(paper=bool(spec.get("default_paper"))))
        except Exception:
            limit_ratio = None
    bounds = None
    if isinstance(heartbeat, dict):
        if heartbeat.get("book_id") and str(heartbeat.get("book_id")) != str(book_id):
            bounds = None
        else:
            sched = heartbeat.get("scan_schedule")
            bounds = sched if isinstance(sched, dict) else None
    clock = now or datetime.now(ZoneInfo("America/New_York"))
    try:
        snapshot = read_safety_snapshot(
            book_id=book_id,
            state_path=state_path or safety_state_path(),
            now=clock,
            equity=equity,
            session_bounds=bounds,
            limit_ratio=limit_ratio,
        )
    except Exception:
        return f"Daily Breaker: {book_id or 'book'} — safety state unavailable"
    return format_daily_breaker(snapshot)


def vti_allocation_pct(hb: dict | None) -> float | None:
    if not hb:
        return None
    caps = hb.get("sleeve_caps") or {}
    raw = caps.get("vti_core")
    if raw is None:
        exp = hb.get("sleeve_exposure") or {}
        eq = float(exp.get("equity") or hb.get("equity") or 0)
        vti_val = float(exp.get("vti_core_value") or 0)
        if eq > 0 and vti_val > 0:
            return vti_val / eq * 100.0
        return None
    try:
        return float(raw) * 100.0
    except (TypeError, ValueError):
        return None


def thinking_pill_snapshot(heartbeat: dict | None, book_id: str | None) -> dict:
    from modules.dashboard_safety import thinking_pill_snapshot as _pill

    return _pill(heartbeat, book_id)


def thinking_tilt_snip(
    hb: dict | None, *, book_id: str | None = None, now: datetime | None = None
) -> str:
    from modules.dashboard_safety import thinking_rotation_text

    bounds = None
    if isinstance(hb, dict) and isinstance(hb.get("scan_schedule"), dict):
        bounds = hb.get("scan_schedule")
    return thinking_rotation_text(hb, book_id=book_id, now=now, session_bounds=bounds)


_ROTATE_LABELS = (
    ("30d return", "return_pct", "{:+.1f}%"),
    ("Sharpe (30d)", "sharpe", "{:.2f}"),
    ("Max drawdown (30d)", "max_drawdown_pct", "{:.1f}%"),
)


def rotating_insight_line(
    *,
    hb: dict | None,
    paper_chase: bool,
    slot: int | None = None,
    vti_label: str | None = None,
    book_id: str | None = None,
    now: datetime | None = None,
) -> str:
    """One-line stat that rotates across 30d return, Sharpe, max DD, VTI %, thinking tilt."""
    if slot is None:
        slot = (datetime.now().minute // 3) % 5
    m30 = metrics_30d(paper_chase=paper_chase)
    variants: list[tuple[str, str]] = []
    for label, key, fmt in _ROTATE_LABELS:
        val = m30.get(key)
        if val is not None:
            variants.append((label, fmt.format(float(val))))
        else:
            variants.append((label, "n/a"))
    if vti_label:
        variants.append(("VTI", vti_label))
    else:
        vti = vti_allocation_pct(hb)
        variants.append(("VTI allocation", f"{vti:.1f}%" if vti is not None else "n/a"))
    variants.append(("Thinking", thinking_tilt_snip(hb, book_id=book_id, now=now)))
    label, value = variants[slot % len(variants)]
    return f"Insight (rotates):  {label}: {value}"


def dashboard_stats_lines(
    *,
    equity: float | None,
    heartbeat: dict | None,
    paper_chase: bool,
    live_only: bool,
    extra_journal_paths: list[Path] | None = None,
    book_id: str | None = None,
    vti_label: str | None = None,
    account_performance: dict | None = None,
    safety_now: datetime | None = None,
    safety_state: Path | None = None,
    insight_slot: int | None = None,
) -> tuple[str, str, str]:
    """Two-line GUI banner + since-start detail (dashboard monitor)."""
    from modules.dashboard_accounting import investment_text, daily_text

    performance = account_performance or {}
    if performance.get("book_id") != book_id:
        performance = {}
    since_detail = investment_text(performance)
    regime = str((heartbeat or {}).get("regime") or "n/a")
    regime_short = regime.split(":")[-1].strip() if ":" in regime else regime
    breaker = _selected_book_breaker(
        book_id=book_id,
        equity=equity,
        heartbeat=heartbeat,
        now=safety_now,
        state_path=safety_state,
    )

    line1 = (
        f"Account Total: {_money(equity)}   ·   "
        f"{since_detail}   ·   {daily_text(performance)}   ·   "
        f"Regime: {regime_short}"
    )
    line2 = (
        f"{breaker}   ·   "
        f"{rotating_insight_line(hb=heartbeat, paper_chase=paper_chase, vti_label=vti_label, book_id=book_id, slot=insight_slot, now=safety_now)}"
    )
    return line1, line2, since_detail


def top_banner_lines(
    *,
    live_eq: float | None,
    paper_eq: float | None,
    live_start: float | None,
    paper_start: float | None,
    inception: date,
    regime: str,
    live_active: bool,
    live_dl: dict,
    paper_dl: dict,
    paper_hb: dict | None,
    live_hb: dict | None,
) -> list[str]:
    combined_pct = combined_since_start_pct(
        live_eq=live_eq,
        paper_eq=paper_eq,
        live_start=live_start,
        paper_start=paper_start,
    )
    since_s = fmt_pct(combined_pct) if combined_pct is not None else "n/a"
    primary_hb = paper_hb if paper_eq is not None else live_hb
    return [
        account_total_line(live_eq=live_eq, paper_eq=paper_eq),
        f"Since Start:    {since_s} since {inception.isoformat()}",
        f"Regime:         {regime}",
        daily_breaker_banner(live_active=live_active, live_dl=live_dl, paper_dl=paper_dl),
        rotating_insight_line(hb=primary_hb, paper_chase=paper_eq is not None),
    ]
