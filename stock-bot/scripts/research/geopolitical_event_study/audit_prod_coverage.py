"""Read-only audit of prod market_data.db coverage (honesty check).

Never writes prod DB. Freeze-safe.

Usage (from stock-bot/):
  python scripts/research/geopolitical_event_study/audit_prod_coverage.py

Writes:
  scripts/analysis/audit_prod_coverage_last.md
  scripts/analysis/audit_prod_coverage_last.json
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402

OUT_MD = ROOT / "scripts" / "analysis" / "audit_prod_coverage_last.md"
OUT_JSON = ROOT / "scripts" / "analysis" / "audit_prod_coverage_last.json"

KEY_SERIES = (
    "SPY",
    "VTI",
    "XLE",
    "GLD",
    "VIX",
    "^VIX",
    "TLT",
    "XOM",
    "CL=F",
    "GC=F",
)

CUTOFFS = (
    "1990-01-01",
    "2000-01-01",
    "2004-01-01",
    "2008-01-01",
    "2014-01-01",
    "2020-01-01",
    "2022-01-01",
)


def _table_stats(conn: sqlite3.Connection, table: str) -> dict[str, Any] | None:
    try:
        row = conn.execute(
            f'SELECT min(Date), max(Date), count(*), count(Close) FROM "{table}"'
        ).fetchone()
    except Exception as exc:
        return {"table": table, "error": str(exc)}
    if not row:
        return None
    mn, mx, n, n_close = row
    return {
        "table": table,
        "symbol": table[: -len("_daily")] if table.endswith("_daily") else table,
        "first": str(mn)[:10] if mn else None,
        "last": str(mx)[:10] if mx else None,
        "rows": int(n or 0),
        "close_rows": int(n_close or 0),
    }


def audit() -> dict[str, Any]:
    db = config.resolve_db_path()
    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "db_path": str(db),
        "db_size_mb": round(db.stat().st_size / 1048576, 2) if db.is_file() else 0.0,
        "disclaimer": (
            "Union panel start != full-bot testability. "
            "2022 STRICT = price-path / realized-vol stress; VIX-dependent behavior partially simulated."
        ),
        "key_series": {},
        "missing_key": [],
        "daily_table_count": 0,
        "coverage_cutoffs": {},
        "earliest_10": [],
        "caveats": [],
    }
    if not db.is_file():
        payload["error"] = "prod DB missing"
        return payload

    conn = sqlite3.connect(str(db))
    try:
        tabs = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%_daily'"
            )
        ]
        payload["daily_table_count"] = len(tabs)
        tab_set = set(tabs)

        for sym in KEY_SERIES:
            t = f"{sym}_daily"
            if t not in tab_set:
                payload["missing_key"].append(sym)
                payload["key_series"][sym] = {"present": False}
                continue
            st = _table_stats(conn, t) or {}
            st["present"] = True
            payload["key_series"][sym] = st

        all_stats: list[dict[str, Any]] = []
        for t in tabs:
            st = _table_stats(conn, t)
            if st and st.get("first"):
                all_stats.append(st)

        earliest = sorted(all_stats, key=lambda s: (s.get("first") or "9999", s.get("symbol") or ""))
        payload["earliest_10"] = earliest[:10]

        for y in CUTOFFS:
            n = sum(
                1
                for s in all_stats
                if (s.get("first") or "9999") <= y and (s.get("last") or "") >= "2026-01-01"
            )
            payload["coverage_cutoffs"][y] = {"symbols_reaching": n, "of": len(all_stats)}

        # Honesty flags
        spy = payload["key_series"].get("SPY") or {}
        gld = payload["key_series"].get("GLD") or {}
        vix = payload["key_series"].get("VIX") or {}
        if spy.get("present") and (spy.get("first") or "") > "1995-01-01":
            payload["caveats"].append(
                f"SPY in prod DB starts {spy.get('first')} — truncated vs ETF inception (~1993)."
            )
        if gld.get("present") and (gld.get("first") or "") > "2005-01-01":
            payload["caveats"].append(
                f"GLD in prod DB starts {gld.get('first')} — truncated vs ETF inception (~2004)."
            )
        if not vix.get("present") or (vix.get("first") or "") > "2022-01-01":
            payload["caveats"].append(
                f"VIX in prod DB starts {vix.get('first') or 'MISSING'} — "
                "2022 stress cannot use true VIX level/rising gates."
            )
        if "CL=F" in payload["missing_key"] or "GC=F" in payload["missing_key"]:
            payload["caveats"].append(
                "WTI/gold futures missing from prod DB — oil/gold shock studies need research store / FRED."
            )
        if earliest:
            payload["caveats"].append(
                f"Panel union start {earliest[0].get('first')} is driven by "
                f"{earliest[0].get('symbol')} et al. — not full-strategy coverage."
            )
    finally:
        conn.close()

    # Content hash of key findings for audit trail
    blob = json.dumps(
        {
            "db": payload["db_path"],
            "keys": payload["key_series"],
            "cutoffs": payload["coverage_cutoffs"],
        },
        sort_keys=True,
        default=str,
    ).encode()
    payload["audit_sha256"] = hashlib.sha256(blob).hexdigest()[:16]
    return payload


def _write_md(p: dict[str, Any]) -> str:
    lines = [
        "# Prod market_data.db coverage audit",
        "",
        f"Generated: {p.get('generated_at')}",
        f"DB: `{p.get('db_path')}` ({p.get('db_size_mb')} MB)",
        f"Daily tables: {p.get('daily_table_count')}",
        f"Audit hash: `{p.get('audit_sha256')}`",
        "",
        f"**{p.get('disclaimer')}**",
        "",
        "## Key series",
        "",
        "| Series | Present | First | Last | Rows |",
        "|--------|---------|-------|------|------|",
    ]
    for sym, st in (p.get("key_series") or {}).items():
        if not st.get("present"):
            lines.append(f"| {sym} | NO | — | — | — |")
        else:
            lines.append(
                f"| {sym} | YES | {st.get('first')} | {st.get('last')} | {st.get('rows')} |"
            )
    lines.extend(["", "## Coverage cutoffs (symbols with first<=cutoff and last>=2026)", ""])
    for y, c in (p.get("coverage_cutoffs") or {}).items():
        lines.append(f"- {y}: **{c.get('symbols_reaching')}** / {c.get('of')}")
    lines.extend(["", "## Earliest 10 tables (union start drivers)", ""])
    for s in p.get("earliest_10") or []:
        lines.append(f"- {s.get('symbol')}: {s.get('first')} -> {s.get('last')} ({s.get('rows')} rows)")
    lines.extend(["", "## Caveats", ""])
    for c in p.get("caveats") or []:
        lines.append(f"- {c}")
    if p.get("missing_key"):
        lines.append(f"- Missing key symbols: {', '.join(p['missing_key'])}")
    lines.extend(
        [
            "",
            "## Next step",
            "",
            "- Build separate research store: "
            "`python scripts/research/geopolitical_event_study/backfill_research_macro.py`",
            "- Do **not** overwrite prod `market_data.db` during freeze.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    payload = audit()
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    md = _write_md(payload)
    OUT_MD.write_text(md, encoding="utf-8")
    print(md.encode("ascii", errors="replace").decode("ascii"))
    print(f"\nWrote {OUT_MD.name}")
    return 0 if not payload.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
