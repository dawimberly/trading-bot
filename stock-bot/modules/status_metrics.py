"""Equity / performance helpers for status.py banner (since-start, rotating insight)."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path

import config

ROOT = Path(__file__).resolve().parents[1]
EQUITY_EVENTS = frozenset({"cycle", "fill", "startup", "halt", "signal"})


def status_inception_date() -> date:
    raw = (os.getenv("STATUS_INCEPTION_DATE") or "2026-01-01").strip()
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return date(2026, 1, 1)


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
        return override, inc, "env"

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
        limit = float(dl.get("limit_pct") or 0.0)
        if dl.get("tripped"):
            loss = dl.get("loss_pct")
            loss_s = f"{loss:.2f}%" if loss is not None else "?"
            return f"{label} TRIPPED ({loss_s} vs {limit:.0f}%)"
        loss = dl.get("loss_pct")
        if loss is not None:
            return f"{label} OK ({loss:+.2f}% today, limit {limit:.0f}%)"
        return f"{label} OK (limit {limit:.0f}%, no session anchor)"

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
    except Exception:
        pass
    return {"return_pct": None, "sharpe": None, "max_drawdown_pct": None, "source": "none"}


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


def thinking_tilt_snip(hb: dict | None) -> str:
    te = (hb or {}).get("thinking_engine") or {}
    for key in ("regime_narrative", "narrative", "apply_log"):
        val = te.get(key)
        if val:
            return str(val).split("|")[0].strip()[:90]
    try:
        from modules.thinking_engine import evaluate_live_apply_status

        mon = evaluate_live_apply_status()
        tilt = mon.get("recommended_tilt")
        if tilt:
            return str(tilt)[:90]
    except Exception:
        pass
    return "none (thinking engine off or no recent decision)"


_ROTATE_LABELS = (
    ("30d return", "return_pct", "{:+.1f}%"),
    ("Sharpe (30d)", "sharpe", "{:.2f}"),
    ("Max drawdown (30d)", "max_drawdown_pct", "{:.1f}%"),
)


def rotating_insight_line(*, hb: dict | None, paper_chase: bool, slot: int | None = None) -> str:
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
    vti = vti_allocation_pct(hb)
    variants.append(("VTI allocation", f"{vti:.1f}%" if vti is not None else "n/a"))
    variants.append(("Last Thinking tilt", thinking_tilt_snip(hb)))
    label, value = variants[slot % len(variants)]
    return f"Insight (rotates):  {label}: {value}"


def dashboard_stats_lines(
    *,
    equity: float | None,
    heartbeat: dict | None,
    paper_chase: bool,
    live_only: bool,
    extra_journal_paths: list[Path] | None = None,
) -> tuple[str, str, str]:
    """Two-line GUI banner + since-start detail (dashboard monitor)."""
    inception = status_inception_date()
    env_key = "STATUS_PAPER_START_EQUITY" if paper_chase else "STATUS_LIVE_START_EQUITY"
    start, start_date, _ = resolve_start_equity(
        paper_chase=paper_chase,
        live_only=live_only,
        inception=inception,
        env_override_key=env_key,
        extra_journal_paths=extra_journal_paths,
    )
    since_pct = pct_change(equity, start)
    regime = str((heartbeat or {}).get("regime") or "n/a")
    regime_short = regime.split(":")[-1].strip() if ":" in regime else regime
    try:
        from modules.trading_safety import get_daily_loss_status

        dl = get_daily_loss_status(paper=paper_chase, current_equity=equity)
    except Exception:
        dl = {"tripped": False, "limit_pct": 4.0 if paper_chase else 2.0, "loss_pct": None}
    book = "Paper" if paper_chase else "Live"
    limit = float(dl.get("limit_pct") or (4.0 if paper_chase else 2.0))
    if dl.get("tripped"):
        loss = dl.get("loss_pct")
        loss_s = f"{loss:.2f}%" if loss is not None else "?"
        breaker = f"Daily Breaker: {book} TRIPPED ({loss_s} vs {limit:.0f}%)"
    elif dl.get("loss_pct") is not None:
        breaker = f"Daily Breaker: {book} OK ({float(dl['loss_pct']):+.2f}% today, limit {limit:.0f}%)"
    else:
        breaker = f"Daily Breaker: {book} OK (limit {limit:.0f}%, no session anchor)"

    line1 = (
        f"Account Total: {_money(equity)}   ·   "
        f"Since Start: {fmt_pct(since_pct)}   ·   "
        f"Regime: {regime_short}"
    )
    line2 = f"{breaker}   ·   {rotating_insight_line(hb=heartbeat, paper_chase=paper_chase)}"
    since_detail = fmt_since_start_line(
        current=equity,
        start=start,
        start_date=start_date,
        inception=inception,
        label="Since Start",
    ).replace("      Since Start:", "Since Start:").strip()
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
