#!/usr/bin/env python3
"""Cash / %VTI at each paper VTI reduce — band-trim vs cash-need.

Measure only. Does not change vti_core, Dynamic VTI, live Profile A, or .env.

Heuristic: if portal cycle cash (as-of) already covers the sell notional, the
reduce was unused-cash / band-trim, not cash-need.

Usage (from stock-bot/):
  python scripts/research/vti_reduce_cash_snapshot.py
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "analysis"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

from trade_reconciliation import read_journal_csv, resolve_paper_journal_path  # noqa: E402

OUT_MD = Path(__file__).resolve().parent / "vti_reduce_cash_snapshot_last.md"
OUT_JSON = Path(__file__).resolve().parent / "vti_reduce_cash_snapshot_last.json"
OUT_CSV = Path(__file__).resolve().parent / "vti_reduce_cash_snapshot_last.csv"
CT = ZoneInfo("America/Chicago")
PAPER_VTI_FLOOR = 0.40
STALE_CYCLE_HOURS = 24.0
DISCLAIMER = (
    "Measure only. No vti_core sell-rule. No live Profile A / Dynamic VTI / .env change. "
    "Do not promote from this file."
)


def _finite(val: Any) -> float | None:
    try:
        if val is None or (isinstance(val, float) and math.isnan(val)):
            return None
        x = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    return x


def classify_cash(cash: Any, sell_notional: Any) -> str:
    """cash_unused / cash_tight / cash_unknown (NaN cash is unknown, not tight)."""
    c = _finite(cash)
    n = _finite(sell_notional)
    if c is None or n is None or n <= 0:
        return "cash_unknown"
    return "cash_unused" if c >= n else "cash_tight"


def heuristic_label(kind: str) -> str:
    """Prior 58/17 split treated missing cash as cash_tight."""
    return "cash_tight" if kind == "cash_unknown" else kind


def would_skip_cash_need_rule(
    *,
    kind: str,
    vti_pct: Any,
    floor: float = PAPER_VTI_FLOOR,
) -> bool:
    """Candidate only: skip VTI sell when unused cash and VTI still >= paper floor."""
    if kind != "cash_unused":
        return False
    pct = _finite(vti_pct)
    if pct is None:
        return False
    return (pct / 100.0) >= float(floor)


def to_ct(ts: Any) -> pd.Timestamp | None:
    if ts is None or (isinstance(ts, float) and math.isnan(ts)):
        return None
    t = pd.Timestamp(ts)
    if pd.isna(t):
        return None
    if t.tzinfo is None:
        return t.tz_localize(CT)
    return t.tz_convert(CT)


def walk_vti_inventory(fills: list[dict]) -> list[dict]:
    """Chronological qty walk. Each fill gets qty_before / qty_after."""
    qty = 0.0
    out: list[dict] = []
    for row in sorted(fills, key=lambda r: (r["ts"], r.get("side") or "", r.get("qty") or 0)):
        rec = dict(row)
        q = abs(float(rec.get("qty") or 0.0))
        side = str(rec.get("side") or "").strip().lower()
        rec["qty_before"] = qty
        if side in ("buy", "buy_to_cover", "b"):
            qty += q
        else:
            qty = max(0.0, qty - q)
        rec["qty_after"] = qty
        out.append(rec)
    return out


def anchor_inventory(walked: list[dict], alpaca_qty: float | None) -> tuple[list[dict], float]:
    """Shift the 0-based walk so the last qty_after matches the live Alpaca position."""
    if not walked or alpaca_qty is None:
        return walked, 0.0
    delta = float(alpaca_qty) - float(walked[-1]["qty_after"])
    if abs(delta) < 1e-6:
        return walked, 0.0
    if walked[0]["qty_before"] + delta < -1e-6:
        return walked, 0.0
    out = []
    for rec in walked:
        adj = dict(rec)
        adj["qty_before"] = float(rec["qty_before"]) + delta
        adj["qty_after"] = float(rec["qty_after"]) + delta
        out.append(adj)
    return out, delta


def load_cycle_snapshots(journal_path: Path) -> pd.DataFrame:
    df, warnings = read_journal_csv(journal_path)
    if df.empty or "event" not in df.columns:
        empty = pd.DataFrame(columns=["ts", "equity", "cash"])
        empty.attrs["parse_warnings"] = warnings
        empty.attrs["n_cycles"] = 0
        return empty
    ev = df["event"].astype(str).str.strip().str.lower()
    cyc = df.loc[ev == "cycle"].copy()
    cyc["ts"] = pd.to_datetime(cyc["timestamp"], errors="coerce").map(to_ct)
    for col in ("equity", "cash"):
        cyc[col] = pd.to_numeric(cyc.get(col), errors="coerce")
    cyc = cyc.dropna(subset=["ts"]).sort_values("ts")
    cyc = cyc[["ts", "equity", "cash"]].reset_index(drop=True)
    cyc.attrs["parse_warnings"] = warnings
    cyc.attrs["n_cycles"] = int(len(cyc))
    return cyc


def asof_cycle(cycles: pd.DataFrame, ts: pd.Timestamp) -> dict[str, Any]:
    if cycles.empty or ts is None or pd.isna(ts):
        return {"equity": None, "cash": None, "cycle_lag_hours": None, "cycle_stale": True}
    hit = cycles.loc[cycles["ts"] <= ts]
    if hit.empty:
        return {"equity": None, "cash": None, "cycle_lag_hours": None, "cycle_stale": True}
    row = hit.iloc[-1]
    lag_h = (ts - row["ts"]).total_seconds() / 3600.0
    return {
        "equity": _finite(row["equity"]),
        "cash": _finite(row["cash"]),
        "cycle_lag_hours": round(lag_h, 3),
        "cycle_stale": lag_h > STALE_CYCLE_HOURS,
    }


def _side_of(order) -> str:
    raw = getattr(order, "side", "") or ""
    return str(raw).split(".")[-1].lower()


def _fetch_closed_vti(client, start: datetime, end: datetime) -> list:
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    collected: list = []
    seen: set[str] = set()
    until = end
    for _ in range(40):
        kwargs = dict(
            status=QueryOrderStatus.CLOSED,
            after=start,
            until=until,
            limit=500,
            nested=True,
        )
        try:
            req = GetOrdersRequest(symbols=["VTI"], **kwargs)
        except TypeError:
            req = GetOrdersRequest(**kwargs)
        batch = list(client.get_orders(filter=req))
        if not batch:
            break
        oldest = None
        for order in batch:
            oid = str(getattr(order, "id", "") or "")
            if oid and oid in seen:
                continue
            if oid:
                seen.add(oid)
            if str(getattr(order, "symbol", "")).upper() != "VTI":
                continue
            collected.append(order)
            ts = getattr(order, "filled_at", None) or getattr(order, "submitted_at", None)
            if ts is not None and (oldest is None or ts < oldest):
                oldest = ts
        if oldest is None or len(batch) < 500:
            break
        until = oldest - timedelta(microseconds=1)
        if until <= start:
            break
    return collected


def _orders_to_fills(orders: list) -> list[dict]:
    fills: list[dict] = []
    for order in orders:
        qty = _finite(getattr(order, "filled_qty", None)) or 0.0
        px = _finite(getattr(order, "filled_avg_price", None)) or 0.0
        if qty <= 0 or px <= 0:
            continue
        ts = to_ct(getattr(order, "filled_at", None) or getattr(order, "submitted_at", None))
        if ts is None:
            continue
        status = str(getattr(order, "status", "") or "").split(".")[-1].lower()
        if status in ("canceled", "cancelled", "expired", "rejected"):
            continue
        fills.append(
            {
                "ts": ts,
                "side": _side_of(order),
                "qty": qty,
                "price": px,
                "notional": round(qty * px, 2),
                "order_id": str(getattr(order, "id", "") or ""),
                "status": status,
            }
        )
    return fills


def _annotate_sells(walked: list[dict], cycles: pd.DataFrame) -> list[dict]:
    rows: list[dict] = []
    for rec in walked:
        if str(rec.get("side") or "").lower() not in ("sell", "sell_short", "s"):
            continue
        ts = rec["ts"]
        snap = asof_cycle(cycles, ts)
        qty_before = float(rec["qty_before"])
        px = float(rec["price"])
        sold = float(rec["qty"])
        vti_val = round(qty_before * px, 2)
        equity = snap["equity"]
        cash = snap["cash"]
        sell_notional = float(rec["notional"])
        vti_pct = None if not equity or equity <= 0 else round(100.0 * vti_val / equity, 1)
        cash_pct = None if not equity or equity <= 0 or cash is None else round(100.0 * cash / equity, 1)
        kind = classify_cash(cash, sell_notional)
        row = {
            "ts_ct": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "qty_sold": round(sold, 3),
            "px": round(px, 2),
            "qty_before": round(qty_before, 3),
            "vti_val": int(round(vti_val)),
            "equity": None if equity is None else int(round(equity)),
            "cash": None if cash is None else int(round(cash)),
            "cash_pct": cash_pct,
            "vti_pct": vti_pct,
            "sell_notional": round(sell_notional, 2),
            "kind": kind,
            "heuristic": heuristic_label(kind),
            "would_skip_cash_need": would_skip_cash_need_rule(kind=kind, vti_pct=vti_pct),
            "cycle_lag_hours": snap["cycle_lag_hours"],
            "cycle_stale": bool(snap["cycle_stale"]),
            "order_id": rec.get("order_id") or "",
        }
        rows.append(row)
    return rows


def _cluster_notes(rows: list[dict]) -> list[str]:
    notes: list[str] = []
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(str(r["ts_ct"])[:10], []).append(r)
    for day, chunk in by_day.items():
        if len(chunk) < 5:
            continue
        cash_pcts = [c["cash_pct"] for c in chunk if c["cash_pct"] is not None]
        vti_pcts = [c["vti_pct"] for c in chunk if c["vti_pct"] is not None]
        unused = sum(1 for c in chunk if c["heuristic"] == "cash_unused")
        notes.append(
            f"{day}: {len(chunk)} VTI sells"
            + (
                f", cash often {min(cash_pcts):.0f}–{max(cash_pcts):.0f}%"
                if cash_pcts
                else ""
            )
            + (
                f" at ~{pd.Series(vti_pcts).median():.0f}% VTI"
                if vti_pcts
                else ""
            )
            + f"; heuristic unused {unused}/{len(chunk)}"
        )
    return notes


def _write(rows: list[dict], meta: dict) -> None:
    n = len(rows)
    unused_h = sum(1 for r in rows if r["heuristic"] == "cash_unused")
    tight_h = sum(1 for r in rows if r["heuristic"] == "cash_tight")
    unused = sum(1 for r in rows if r["kind"] == "cash_unused")
    tight = sum(1 for r in rows if r["kind"] == "cash_tight")
    unknown = sum(1 for r in rows if r["kind"] == "cash_unknown")
    skip = sum(1 for r in rows if r["would_skip_cash_need"])
    payload = {
        "generated_at": meta["generated_at"],
        "disclaimer": DISCLAIMER,
        "journal_path": meta.get("journal_path"),
        "n_cycles": meta.get("n_cycles"),
        "n_vti_fills": meta.get("n_vti_fills"),
        "ending_qty_walk": meta.get("ending_qty_walk"),
        "qty_anchor": meta.get("qty_anchor"),
        "alpaca_qty": meta.get("alpaca_qty"),
        "n_sells": n,
        "cash_unused_heuristic": unused_h,
        "cash_tight_heuristic": tight_h,
        "cash_unused": unused,
        "cash_tight": tight,
        "cash_unknown": unknown,
        "would_skip_cash_need": skip,
        "cluster_notes": meta.get("cluster_notes") or [],
        "parse_warnings": meta.get("parse_warnings") or [],
        "rows": rows,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    pd.DataFrame(rows).to_csv(OUT_CSV, index=False)
    lines = [
        "# VTI reduce cash / %VTI snapshot",
        "",
        f"Generated: {meta['generated_at']}",
        "",
        f"**{DISCLAIMER}**",
        "",
        "As-of join: Alpaca paper closed VTI fills × portal `event=cycle` equity/cash "
        "(last cycle at or before fill, America/Chicago).",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| VTI sells | {n} |",
        f"| cash ≥ sell notional (unused, NaN→tight) | {unused_h} |",
        f"| cash < sell notional or missing (tight heuristic) | {tight_h} |",
        f"| cash_unused / cash_tight / cash_unknown | {unused} / {tight} / {unknown} |",
        f"| would skip under cash-need candidate (unused + VTI ≥ 40%) | {skip} |",
        f"| portal cycles | {meta.get('n_cycles')} |",
        f"| VTI fills walked | {meta.get('n_vti_fills')} |",
        f"| ending walk qty vs Alpaca | {meta.get('ending_qty_walk')} / {meta.get('alpaca_qty')} (anchor {meta.get('qty_anchor')}) |",
        "",
        "## Reading",
        "",
        "Most historical paper VTI sells happened while cash already covered the "
        "sell notional — band trim / target oscillation, not raising cash for NYSE.",
        "Candidate (not coded, freeze still on): skip VTI **sell** when cash covers "
        "the reduce **and** current VTI is still ≥ the 40% paper floor. Buys when "
        "underweight are unchanged. Live Profile A (85% VTI) is out of scope.",
        "",
        "## Clusters",
        "",
    ]
    notes = meta.get("cluster_notes") or []
    if notes:
        for nte in notes:
            lines.append(f"- {nte}")
    else:
        lines.append("- (no 5+ sell days)")
    lines.extend(
        [
            "",
            "## Sample rows",
            "",
            "| ts CT | qty sold | px | qty before | VTI $ | equity | cash | cash% | VTI% | label | skip? |",
            "|-------|---------:|---:|-----------:|------:|-------:|-----:|------:|-----:|-------|-------|",
        ]
    )
    show = rows[:8] + ([{"_sep": True}] if len(rows) > 16 else []) + rows[-8:]
    for r in show:
        if r.get("_sep"):
            lines.append("| … | | | | | | | | | | |")
            continue
        lines.append(
            f"| {r['ts_ct']} | {r['qty_sold']:.3f} | {r['px']:.2f} | {r['qty_before']:.3f} "
            f"| {r['vti_val']} | {r['equity']} | {r['cash']} | {r['cash_pct']} "
            f"| {r['vti_pct']} | {r['heuristic']} | {r['would_skip_cash_need']} |"
        )
    lines.extend(["", DISCLAIMER, ""])
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    from modules.alpaca_executor import get_trading_client

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print("--- VTI reduce cash / %VTI snapshot ---")
    print(DISCLAIMER)

    journal = resolve_paper_journal_path()
    if journal is None:
        print("FAIL: no paper journal")
        return 2
    cycles = load_cycle_snapshots(journal)
    print(f"Journal: {journal}")
    print(f"Cycle snapshots: {cycles.attrs.get('n_cycles')}")

    client = get_trading_client(paper=True, allow_live=False)
    end = datetime.now(timezone.utc)
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    orders = _fetch_closed_vti(client, start, end)
    fills = _orders_to_fills(orders)
    walked = walk_vti_inventory(fills)
    raw_ending = walked[-1]["qty_after"] if walked else 0.0

    alpaca_qty = None
    try:
        for pos in client.get_all_positions():
            if str(getattr(pos, "symbol", "")).upper() == "VTI":
                alpaca_qty = _finite(getattr(pos, "qty", None))
                break
    except Exception as exc:
        print(f"WARN: positions ({exc})")

    walked, qty_anchor = anchor_inventory(walked, alpaca_qty)
    ending = walked[-1]["qty_after"] if walked else 0.0
    if qty_anchor:
        print(f"Inventory anchor {qty_anchor:+.3f} sh (walk {raw_ending:.3f} -> Alpaca {alpaca_qty})")

    sells = _annotate_sells(walked, cycles)
    clusters = _cluster_notes(sells)
    meta = {
        "generated_at": generated,
        "journal_path": str(journal),
        "n_cycles": cycles.attrs.get("n_cycles"),
        "n_vti_fills": len(fills),
        "ending_qty_walk": round(ending, 3),
        "qty_anchor": round(qty_anchor, 3),
        "alpaca_qty": None if alpaca_qty is None else round(alpaca_qty, 3),
        "cluster_notes": clusters,
        "parse_warnings": cycles.attrs.get("parse_warnings") or [],
    }
    _write(sells, meta)

    unused_h = sum(1 for r in sells if r["heuristic"] == "cash_unused")
    tight_h = sum(1 for r in sells if r["heuristic"] == "cash_tight")
    skip = sum(1 for r in sells if r["would_skip_cash_need"])
    print(f"VTI sells {len(sells)}")
    print(f"cash >= sell notional (unused cash heuristic) {unused_h}")
    print(f"cash < sell notional {tight_h}")
    print(f"would skip under cash-need candidate {skip}")
    for nte in clusters:
        print(f"  {nte}")
    print(f"Wrote {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
