"""A/B test deployment sizing: baseline vs adaptive chunk vs co-fire vs both.

Run from repo root:
  python scripts/analysis/deployment_efficiency_ab.py
  python scripts/analysis/deployment_efficiency_ab.py --days 500
  python scripts/analysis/deployment_efficiency_ab.py --days 2000
  python scripts/analysis/deployment_efficiency_ab.py --max
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from backtester import MIN_HISTORY, _ensure_daily_data, run_backtest

OUT_MD = Path(__file__).with_name("deployment_efficiency_results.md")
OUT_CSV = Path(__file__).with_name("deployment_efficiency_ab.csv")

VARIANTS = (
    ("baseline", False, False),
    ("adaptive_only", True, False),
    ("cofire_only", False, True),
    ("both", True, True),
)


@contextlib.contextmanager
def deployment_flags(adaptive: bool, cofire: bool):
    saved = (
        config.ADAPTIVE_CHUNK_ENABLED,
        config.COFIRE_BUDGET_ENABLED,
        config.ADAPTIVE_CHUNK_MAX_PCT,
        config.COFIRE_BUDGET_PCT,
    )
    config.ADAPTIVE_CHUNK_ENABLED = adaptive
    config.COFIRE_BUDGET_ENABLED = cofire
    try:
        yield
    finally:
        (
            config.ADAPTIVE_CHUNK_ENABLED,
            config.COFIRE_BUDGET_ENABLED,
            config.ADAPTIVE_CHUNK_MAX_PCT,
            config.COFIRE_BUDGET_PCT,
        ) = saved


def _run_variant(data, name: str, adaptive: bool, cofire: bool, window: str) -> dict:
    with deployment_flags(adaptive, cofire):
        row = run_backtest(data, track_spy_fill=True, verbose=False)
    fill = row.pop("spy_fill")
    row["variant"] = name
    row["window"] = window
    row["adaptive_chunk"] = adaptive
    row["cofire_budget"] = cofire
    row["spy_cycles_to_90pct"] = fill.get("cycles_to_90pct")
    row["spy_trades_to_90pct"] = fill.get("trades_to_90pct")
    row["spy_hours_to_90pct"] = fill.get("hours_to_90pct")
    row["spy_reached_90pct"] = fill.get("reached_90pct")
    row["spy_total_buys"] = fill.get("spy_buys")
    return row


def _slice_window(data, days: int | None, use_max: bool):
    if use_max:
        return data
    if days is None:
        return data
    need = days + MIN_HISTORY
    if len(data) <= need:
        return data
    return data.iloc[-need:]


def run_ab(data, window_label: str) -> list[dict]:
    rows = []
    for name, adaptive, cofire in VARIANTS:
        print(f"  {window_label} / {name} ...", flush=True)
        rows.append(_run_variant(data, name, adaptive, cofire, window_label))
    return rows


def _fmt_table(rows: list[dict]) -> str:
    lines = [
        "| Variant | Return | Sharpe | Max DD | SPY 90% cycles | SPY 90% trades | SPY 90% hrs | Orders |",
        "|---------|--------|--------|--------|----------------|----------------|-------------|--------|",
    ]
    for r in rows:
        c90 = r.get("spy_cycles_to_90pct")
        t90 = r.get("spy_trades_to_90pct")
        h90 = r.get("spy_hours_to_90pct")
        lines.append(
            f"| {r['variant']} "
            f"| {r['total_return_pct']:+.2f}% "
            f"| {r['sharpe']:.2f} "
            f"| {r['max_drawdown_pct']:.2f}% "
            f"| {c90 if c90 is not None else '—'} "
            f"| {t90 if t90 is not None else '—'} "
            f"| {h90 if h90 is not None else '—'} "
            f"| {r['total_orders']} |"
        )
    return "\n".join(lines)


def _recommend(all_rows: list[dict]) -> str:
    by_window: dict[str, list[dict]] = {}
    for r in all_rows:
        by_window.setdefault(r["window"], []).append(r)

    lines = ["## Recommendation", ""]
    best_overall = None
    best_score = (-999.0, -999.0)

    for window, rows in by_window.items():
        base = next(r for r in rows if r["variant"] == "baseline")
        lines.append(f"### {window}")
        for r in rows:
            if r["variant"] == "baseline":
                continue
            c_base = base.get("spy_cycles_to_90pct")
            c_var = r.get("spy_cycles_to_90pct")
            speed = ""
            if c_base is not None and c_var is not None and c_base > 0:
                pct_faster = round(100 * (1 - c_var / c_base), 1)
                speed = f", fill {pct_faster:+.1f}% vs baseline cycles"
            lines.append(
                f"- **{r['variant']}**: return {r['total_return_pct'] - base['total_return_pct']:+.2f} pp, "
                f"Sharpe {r['sharpe'] - base['sharpe']:+.2f}, "
                f"MaxDD {r['max_drawdown_pct'] - base['max_drawdown_pct']:+.2f} pp{speed}"
            )

        for r in rows:
            fill_score = r.get("spy_cycles_to_90pct") or 9999
            score = (r["sharpe"], -fill_score)
            if score > best_score:
                best_score = score
                best_overall = (window, r)

    lines.append("")
    if best_overall:
        w, r = best_overall
        if r["variant"] == "both":
            lines.append(
                f"**Enable both flags** on window `{w}`: best Sharpe ({r['sharpe']:.2f}) "
                f"with fastest SPY fill ({r.get('spy_cycles_to_90pct')} cycles). "
                f"Set `ADAPTIVE_CHUNK_ENABLED=true` and `COFIRE_BUDGET_ENABLED=true`."
            )
        elif r["variant"] == "adaptive_only":
            lines.append(
                f"**Adaptive chunk only** wins on `{w}` — larger solo-sleeve chunks when room > 5×. "
                f"Set `ADAPTIVE_CHUNK_ENABLED=true`."
            )
        elif r["variant"] == "cofire_only":
            lines.append(
                f"**Co-fire pool only** wins on `{w}` — pooled 6% cycle budget when 2+ sleeves fire. "
                f"Set `COFIRE_BUDGET_ENABLED=true`."
            )
        else:
            lines.append("**Keep baseline 2%** — sizing improvements did not improve risk-adjusted returns.")
    return "\n".join(lines)


def write_report(all_rows: list[dict], paths: tuple[Path, Path]) -> None:
    md_path, csv_path = paths
    sections = [
        "# Deployment Efficiency A/B",
        "",
        f"Config: `RISK_PER_TRADE={config.RISK_PER_TRADE}`, "
        f"`ADAPTIVE_CHUNK_MAX_PCT={config.ADAPTIVE_CHUNK_MAX_PCT}`, "
        f"`COFIRE_BUDGET_PCT={config.COFIRE_BUDGET_PCT}`, "
        f"`MAX_NOTIONAL_PER_ORDER={config.MAX_NOTIONAL_PER_ORDER}`.",
        "",
        "SPY fill metric: cycles/trades/hours from first SPY buy signal to 90% of SPY sleeve cap.",
        "",
    ]
    windows = sorted({r["window"] for r in all_rows})
    for w in windows:
        sections.append(f"## Window: {w}")
        sections.append("")
        sections.append(_fmt_table([r for r in all_rows if r["window"] == w]))
        sections.append("")
    sections.append(_recommend(all_rows))
    md_path.write_text("\n".join(sections), encoding="utf-8")

    import csv

    keys = sorted({k for r in all_rows for k in r if k != "regime_counts"})
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in all_rows:
            w.writerow(r)
    print(f"Wrote {md_path}")
    print(f"Wrote {csv_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, action="append", help="Window length(s), e.g. 500 2000")
    parser.add_argument("--max", action="store_true", help="Also run max-history window")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    day_windows = args.days or [500, 2000]
    print("Loading daily data ...", flush=True)
    data_full = _ensure_daily_data(max(day_windows), refresh=args.refresh, use_max=False)
    if len(data_full) < MIN_HISTORY + 10:
        print("Insufficient data; run: python fetch_data.py --daily --days 2000")
        sys.exit(1)

    all_rows: list[dict] = []
    for d in day_windows:
        label = f"{d}d"
        slice_data = _slice_window(data_full, d, False)
        print(f"--- Window {label} ({len(slice_data)} bars) ---")
        all_rows.extend(run_ab(slice_data, label))

    if args.max:
        data_max = _ensure_daily_data(0, refresh=args.refresh, use_max=True)
        label = "max"
        print(f"--- Window {label} ({len(data_max)} bars) ---")
        all_rows.extend(run_ab(data_max, label))

    write_report(all_rows, (OUT_MD, OUT_CSV))
    print(json.dumps(all_rows, indent=2, default=str))


if __name__ == "__main__":
    main()
