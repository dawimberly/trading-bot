"""Report-only geopolitical event windows with fidelity grades.

Reads research_macro.db + event_calendar.json. Never writes prod DB.
No trading signals, no promote language, no param search.

Usage (from stock-bot/):
  python scripts/research/geopolitical_event_study/run_event_study.py

Writes:
  scripts/analysis/geopolitical_event_study_last.md
  scripts/analysis/geopolitical_event_study_last.json
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
PKG = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

RESEARCH_DB = ROOT / "data" / "research" / "geopolitical" / "research_macro.db"
CALENDAR = PKG / "event_calendar.json"
OUT_MD = ROOT / "scripts" / "analysis" / "geopolitical_event_study_last.md"
OUT_JSON = ROOT / "scripts" / "analysis" / "geopolitical_event_study_last.json"

DISCLAIMER = (
    "REPORT ONLY - freeze on; labels only; no trade signals; no promote; "
    "no live/paper default changes"
)

# Windows relative to event_date (trading days approximated via calendar days)
PRE_DAYS = 20
IMMEDIATE_DAYS = 5
POST_20 = 20
POST_60 = 60

# Series required for each fidelity grade (must exist in research DB with coverage)
GRADE_REQUIREMENTS = {
    "macro_only": ["WTI", "VIX"],
    "partial_strategy_proxy": ["SPY", "VIX", "WTI"],
    "full_freeze_compatible": ["VTI", "SPY", "XLE", "GLD", "VIX", "WTI"],
}


def _load_series(conn: sqlite3.Connection, name: str) -> pd.Series:
    table = f"{name}_daily"
    try:
        df = pd.read_sql_query(f'SELECT Date, Close FROM "{table}" ORDER BY Date', conn)
    except Exception:
        return pd.Series(dtype=float)
    if df.empty:
        return pd.Series(dtype=float)
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
    df = df.dropna()
    s = df.set_index("Date")["Close"].sort_index()
    s.index = pd.DatetimeIndex(s.index).tz_localize(None).normalize()
    return s.astype(float)


def _manifest_from_db(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    try:
        rows = conn.execute(
            "SELECT name, source, source_id, first_date, last_date, rows, checksum FROM series_meta"
        ).fetchall()
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, source, source_id, first, last, n, checksum in rows:
        out[name] = {
            "source": source,
            "source_id": source_id,
            "first_date": first,
            "last_date": last,
            "rows": n,
            "checksum": checksum,
        }
    return out


def _series_covers(meta: dict[str, Any] | None, event_date: pd.Timestamp, need_end: pd.Timestamp) -> bool:
    if not meta or not meta.get("first_date") or not meta.get("last_date"):
        return False
    first = pd.Timestamp(meta["first_date"])
    last = pd.Timestamp(meta["last_date"])
    return first <= event_date - pd.Timedelta(days=PRE_DAYS) and last >= need_end


def _assign_grade(
    event: dict[str, Any],
    meta_map: dict[str, dict[str, Any]],
) -> tuple[str, list[str]]:
    """Highest achievable grade given research coverage around the event."""
    ed = pd.Timestamp(event["event_date"])
    need_end = ed + pd.Timedelta(days=POST_60)
    notes: list[str] = []
    # Prefer calendar hint but never above what data supports
    preferred = event.get("tier") or "macro_only"
    order = ["full_freeze_compatible", "partial_strategy_proxy", "macro_only"]
    # Start from preferred and fall down
    start_idx = order.index(preferred) if preferred in order else 2
    for grade in order[start_idx:]:
        req = GRADE_REQUIREMENTS[grade]
        missing = [s for s in req if not _series_covers(meta_map.get(s), ed, need_end)]
        if not missing:
            if grade != preferred:
                notes.append(f"calendar tier {preferred} downshifted to {grade} by coverage")
            return grade, notes
        notes.append(f"{grade} blocked; missing/short: {', '.join(missing)}")
    return "insufficient_data", notes


def _window_return(series: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> float | None:
    if series.empty:
        return None
    w = series.loc[(series.index >= start) & (series.index <= end)]
    if len(w) < 2:
        # nearest available brackets
        before = series.loc[series.index <= start]
        after = series.loc[series.index >= end]
        if before.empty or after.empty:
            return None
        a = float(before.iloc[-1])
        b = float(after.iloc[0])
        if a <= 0:
            return None
        return (b / a - 1.0) * 100.0
    a = float(w.iloc[0])
    b = float(w.iloc[-1])
    if a <= 0:
        return None
    return (b / a - 1.0) * 100.0


def _level_at(series: pd.Series, ts: pd.Timestamp) -> float | None:
    if series.empty:
        return None
    before = series.loc[series.index <= ts]
    if before.empty:
        return None
    return float(before.iloc[-1])


def _event_row(
    event: dict[str, Any],
    series_map: dict[str, pd.Series],
    meta_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    ed = pd.Timestamp(event["event_date"])
    grade, grade_notes = _assign_grade(event, meta_map)
    pre_s = ed - pd.Timedelta(days=PRE_DAYS)
    imm_e = ed + pd.Timedelta(days=IMMEDIATE_DAYS)
    p20_e = ed + pd.Timedelta(days=POST_20)
    p60_e = ed + pd.Timedelta(days=POST_60)

    row: dict[str, Any] = {
        "id": event["id"],
        "label": event["label"],
        "event_date": event["event_date"],
        "region": event.get("region"),
        "calendar_tier": event.get("tier"),
        "achieved_grade": grade,
        "grade_notes": grade_notes,
        "sources": event.get("sources") or [],
        "metrics": {},
    }
    for name in ("SPY", "VTI", "XLE", "GLD", "VIX", "WTI", "CL_F", "GC_F"):
        s = series_map.get(name)
        if s is None or s.empty:
            continue
        m: dict[str, Any] = {
            "pre_20d_ret_pct": _window_return(s, pre_s, ed),
            "imm_5d_ret_pct": _window_return(s, ed, imm_e),
            "post_20d_ret_pct": _window_return(s, ed, p20_e),
            "post_60d_ret_pct": _window_return(s, ed, p60_e),
            "level_at_event": _level_at(s, ed),
            "level_pre": _level_at(s, pre_s),
        }
        if name in ("VIX", "WTI") and m["level_at_event"] is not None and m["level_pre"] is not None:
            pre = float(m["level_pre"])
            if pre != 0:
                m["level_change_pct_vs_pre20"] = (float(m["level_at_event"]) / pre - 1.0) * 100.0
        row["metrics"][name] = m
    return row


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.2f}%"


def _fmt_num(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{v:.2f}"


def _write_md(payload: dict[str, Any]) -> str:
    lines = [
        "# Geopolitical event study (report only)",
        "",
        f"Generated: {payload.get('generated_at')}",
        f"Research DB: `{payload.get('research_db')}`",
        "",
        f"**{DISCLAIMER}**",
        "",
        "## Fidelity legend",
        "",
        "| Grade | Meaning |",
        "|-------|---------|",
        "| macro_only | VIX/WTI (+ available equity proxies) |",
        "| partial_strategy_proxy | SPY + VIX + WTI coverage around event |",
        "| full_freeze_compatible | VTI/SPY/XLE/GLD/VIX/WTI all cover the window |",
        "| insufficient_data | Cannot grade — skip or macro-only if any series |",
        "",
        "## Series manifest (research store)",
        "",
        "| Series | Source | First | Last | Rows | Checksum |",
        "|--------|--------|-------|------|------|----------|",
    ]
    for name, m in (payload.get("series_manifest") or {}).items():
        lines.append(
            f"| {name} | {m.get('source')}:{m.get('source_id')} | {m.get('first_date')} | "
            f"{m.get('last_date')} | {m.get('rows')} | `{m.get('checksum')}` |"
        )

    lines.extend(["", "## Event windows", ""])
    for ev in payload.get("events") or []:
        lines.append(f"### {ev.get('event_date')} — {ev.get('label')}")
        lines.append("")
        lines.append(
            f"- Grade: **{ev.get('achieved_grade')}** "
            f"(calendar tier: {ev.get('calendar_tier')})"
        )
        for n in ev.get("grade_notes") or []:
            lines.append(f"- Note: {n}")
        srcs = ev.get("sources") or []
        if srcs:
            lines.append("- Sources:")
            for s in srcs:
                lines.append(f"  - [{s.get('name')}]({s.get('url')})")
        metrics = ev.get("metrics") or {}
        if metrics:
            lines.append("")
            lines.append(
                "| Series | Pre-20d | Imm-5d | Post-20d | Post-60d | Level@event |"
            )
            lines.append(
                "|--------|---------|--------|----------|----------|-------------|"
            )
            for name, m in metrics.items():
                lines.append(
                    f"| {name} | {_fmt_pct(m.get('pre_20d_ret_pct'))} | "
                    f"{_fmt_pct(m.get('imm_5d_ret_pct'))} | "
                    f"{_fmt_pct(m.get('post_20d_ret_pct'))} | "
                    f"{_fmt_pct(m.get('post_60d_ret_pct'))} | "
                    f"{_fmt_num(m.get('level_at_event'))} |"
                )
        lines.append("")

    lines.extend(
        [
            "## Interpretation rules",
            "",
            "- Do not retune the bot from these windows.",
            "- Do not treat headlines as alpha features.",
            "- 2022 Russia/Ukraine row is macro/proxy context for the STRICT "
            "**price-path / realized-vol** stress notebook (VIX gates partially simulated in prod).",
            "- Full freeze-profile claims only where grade = `full_freeze_compatible`.",
            "",
            f"**{DISCLAIMER}**",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    if not RESEARCH_DB.is_file():
        print(f"Missing research DB: {RESEARCH_DB}")
        print("Run backfill_research_macro.py first.")
        return 1
    calendar = json.loads(CALENDAR.read_text(encoding="utf-8"))
    events = calendar.get("events") or []

    conn = sqlite3.connect(str(RESEARCH_DB))
    try:
        meta_map = _manifest_from_db(conn)
        series_map = {name: _load_series(conn, name) for name in meta_map}
        rows = [_event_row(ev, series_map, meta_map) for ev in events]
    finally:
        conn.close()

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "research_db": str(RESEARCH_DB.relative_to(ROOT)),
        "disclaimer": DISCLAIMER,
        "series_manifest": meta_map,
        "events": rows,
        "ok": True,
    }
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    md = _write_md(payload)
    OUT_MD.write_text(md, encoding="utf-8")
    print(md.encode("ascii", errors="replace").decode("ascii"))
    print(f"\nWrote {OUT_MD.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
