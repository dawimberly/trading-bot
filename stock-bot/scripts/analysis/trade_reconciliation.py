"""Match Alpaca fills to paper_journal signals; estimate notional/slippage vs sim sizing.

Run:
  python scripts/analysis/trade_reconciliation.py
  python scripts/analysis/trade_reconciliation.py --days 30 --json report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config
from modules.alpaca_executor import get_trading_client
from modules.wisdom_evaluator import filter_paper_journal, resolve_live_only


def _sim_notional(equity: float, cash: float | None = None) -> float:
    """Backtest-style per-trade notional (min risk cap, max order, cash headroom)."""
    base = cash if cash is not None else equity
    risk = config.effective_risk_per_trade(equity)
    max_order = config.effective_max_notional_per_order(equity)
    min_n = config.effective_min_notional(equity)
    raw = min(equity * risk, max_order, base * 0.95)
    return round(max(min_n, raw), 2)


def _normalize_symbol(symbol: str) -> str:
    return config.normalize_symbol(symbol)


def _order_filled_notional(order) -> float | None:
    qty = float(getattr(order, "filled_qty", None) or 0)
    avg = getattr(order, "filled_avg_price", None)
    if qty <= 0 or avg is None:
        notional = getattr(order, "notional", None)
        if notional is not None:
            try:
                return round(float(notional), 2)
            except (TypeError, ValueError):
                return None
        return None
    try:
        return round(qty * float(avg), 2)
    except (TypeError, ValueError):
        return None


def _fetch_filled_orders(client, start: datetime, end: datetime) -> list:
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    req = GetOrdersRequest(
        status=QueryOrderStatus.CLOSED,
        after=start,
        until=end,
        limit=500,
        nested=True,
    )
    orders = list(client.get_orders(filter=req))
    orders.sort(key=lambda o: getattr(o, "filled_at", None) or getattr(o, "submitted_at", None))
    return orders


def _load_journal_signals(
    period_start: date,
    period_end: date,
    *,
    live_only: bool | None = None,
    min_equity: float | None = None,
) -> pd.DataFrame:
    path = Path(config.PAPER_JOURNAL_CSV)
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["timestamp"])
    live_only = resolve_live_only(live_only)
    df, _segment = filter_paper_journal(
        df,
        live_only=live_only,
        min_equity=min_equity,
    )
    mask = (df["ts"].dt.date >= period_start) & (df["ts"].dt.date <= period_end)
    events = df.loc[mask & df["event"].isin(["signal", "exit"])].copy()
    events["symbol_norm"] = events["symbol"].astype(str).map(_normalize_symbol)
    events["side_norm"] = events["side"].astype(str).str.lower()
    return events


def _match_orders_to_signals(
    signals: pd.DataFrame,
    orders: list,
    *,
    match_minutes: int = 30,
) -> tuple[list[dict], list[dict], list]:
    matched = []
    unmatched_signals = []
    used_orders: set[str] = set()
    window = timedelta(minutes=match_minutes)

    order_rows = []
    for order in orders:
        filled_at = getattr(order, "filled_at", None) or getattr(order, "submitted_at", None)
        if filled_at is None:
            continue
        filled_ts = pd.Timestamp(filled_at).tz_localize(None)
        order_rows.append(
            {
                "id": str(order.id),
                "symbol": _normalize_symbol(order.symbol),
                "side": str(getattr(order, "side", "")).lower().split(".")[-1],
                "filled_ts": filled_ts,
                "filled_notional": _order_filled_notional(order),
                "order": order,
            }
        )

    for _, sig in signals.iterrows():
        sig_ts = pd.Timestamp(sig["ts"]).tz_localize(None) if sig["ts"].tzinfo else pd.Timestamp(sig["ts"])
        sym = sig["symbol_norm"]
        side = sig["side_norm"]
        if not sym or not side:
            unmatched_signals.append(
                {
                    "timestamp": str(sig["timestamp"]),
                    "symbol": sig.get("symbol", ""),
                    "side": sig.get("side", ""),
                    "reason": "missing symbol or side",
                }
            )
            continue

        journal_notional = None
        try:
            if pd.notna(sig.get("notional")) and str(sig.get("notional")).strip():
                journal_notional = round(float(sig["notional"]), 2)
        except (TypeError, ValueError):
            journal_notional = None

        equity = None
        try:
            if pd.notna(sig.get("equity")):
                equity = float(sig["equity"])
        except (TypeError, ValueError):
            equity = None

        sim_expected = _sim_notional(equity) if equity else None

        best = None
        best_delta = None
        for row in order_rows:
            if row["id"] in used_orders:
                continue
            if row["symbol"] != sym or row["side"] != side:
                continue
            delta = abs(row["filled_ts"] - sig_ts)
            if delta > window:
                continue
            if best is None or delta < best_delta:
                best = row
                best_delta = delta

        if best is None:
            unmatched_signals.append(
                {
                    "timestamp": str(sig["timestamp"]),
                    "symbol": sym,
                    "side": side,
                    "journal_notional": journal_notional,
                    "sim_notional": sim_expected,
                    "reason": "no Alpaca fill within match window",
                }
            )
            continue

        used_orders.add(best["id"])
        filled = best["filled_notional"]
        slippage_vs_journal = None
        if journal_notional and filled:
            slippage_vs_journal = round(filled - journal_notional, 2)
        slippage_vs_sim = None
        if sim_expected and filled:
            slippage_vs_sim = round(filled - sim_expected, 2)

        matched.append(
            {
                "signal_time": str(sig["timestamp"]),
                "fill_time": best["filled_ts"].isoformat(sep=" ", timespec="seconds"),
                "symbol": sym,
                "side": side,
                "event": sig.get("event", "signal"),
                "journal_notional": journal_notional,
                "sim_notional": sim_expected,
                "filled_notional": filled,
                "slippage_vs_journal": slippage_vs_journal,
                "slippage_vs_sim": slippage_vs_sim,
                "match_lag_sec": int(best_delta.total_seconds()) if best_delta else None,
                "order_id": best["id"],
            }
        )

    unmatched_orders = [r for r in order_rows if r["id"] not in used_orders]
    return matched, unmatched_signals, unmatched_orders


def build_reconciliation_report(
    *,
    window_days: int | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
    match_minutes: int = 30,
    live_only: bool | None = None,
    min_equity: float | None = None,
) -> dict:
    window_days = window_days or config.WISDOM_EVAL_DAYS
    if period_end is None:
        period_end = date.today()
    if period_start is None:
        period_start = period_end - timedelta(days=window_days)

    live_only = resolve_live_only(live_only)
    signals = _load_journal_signals(
        period_start,
        period_end,
        live_only=live_only,
        min_equity=min_equity,
    )
    trade_signals = signals.loc[signals["event"] == "signal"] if not signals.empty else signals

    report: dict = {
        "window": {"from": str(period_start), "to": str(period_end)},
        "live_only": live_only,
        "signal_count": int(len(trade_signals)),
        "exit_events": int((signals["event"] == "exit").sum()) if not signals.empty else 0,
        "matched_trades": 0,
        "unmatched_signals": 0,
        "unmatched_orders": 0,
        "alpaca_error": None,
    }

    try:
        client = get_trading_client()
        start_dt = datetime.combine(period_start, datetime.min.time())
        end_dt = datetime.combine(period_end, datetime.max.time())
        orders = _fetch_filled_orders(client, start_dt, end_dt)
    except Exception as exc:
        report["alpaca_error"] = str(exc)
        report["unmatched_signals"] = int(len(trade_signals))
        return report

    matched, unmatched_signals, unmatched_orders = _match_orders_to_signals(
        signals,
        orders,
        match_minutes=match_minutes,
    )

    journal_slippage = [
        m["slippage_vs_journal"]
        for m in matched
        if m.get("slippage_vs_journal") is not None
    ]
    sim_slippage = [
        m["slippage_vs_sim"] for m in matched if m.get("slippage_vs_sim") is not None
    ]

    report.update(
        {
            "alpaca_filled_orders": len(orders),
            "matched_trades": len(matched),
            "unmatched_signals": len(unmatched_signals),
            "unmatched_orders": len(unmatched_orders),
            "avg_slippage_vs_journal": round(sum(journal_slippage) / len(journal_slippage), 2)
            if journal_slippage
            else None,
            "avg_slippage_vs_sim": round(sum(sim_slippage) / len(sim_slippage), 2)
            if sim_slippage
            else None,
            "matched": matched,
            "unmatched_signal_details": unmatched_signals[:25],
            "unmatched_order_details": [
                {
                    "fill_time": r["filled_ts"].isoformat(sep=" ", timespec="seconds"),
                    "symbol": r["symbol"],
                    "side": r["side"],
                    "filled_notional": r["filled_notional"],
                }
                for r in unmatched_orders[:25]
            ],
        }
    )
    return report


def _print_report(report: dict) -> None:
    w = report["window"]
    print(f"=== Trade reconciliation {w['from']} -> {w['to']} ===")
    print(f"Journal signals:     {report['signal_count']}")
    print(f"Journal exits:       {report.get('exit_events', 0)}")
    if report.get("alpaca_error"):
        print(f"Alpaca:              ERROR — {report['alpaca_error']}")
        print(f"Unmatched signals:   {report['unmatched_signals']}")
        return
    print(f"Alpaca filled orders:{report.get('alpaca_filled_orders', 0)}")
    print(f"Matched trades:      {report['matched_trades']}")
    print(f"Unmatched signals:   {report['unmatched_signals']}")
    print(f"Unmatched orders:    {report['unmatched_orders']}")
    if report.get("avg_slippage_vs_journal") is not None:
        print(f"Avg slip vs journal: ${report['avg_slippage_vs_journal']:+,.2f}")
    if report.get("avg_slippage_vs_sim") is not None:
        print(f"Avg slip vs sim:     ${report['avg_slippage_vs_sim']:+,.2f}")
    for row in report.get("matched", [])[:10]:
        print(
            f"  {row['signal_time']} {row['side']} {row['symbol']} "
            f"journal=${row.get('journal_notional')} fill=${row.get('filled_notional')} "
            f"sim=${row.get('sim_notional')}"
        )
    if report["matched_trades"] > 10:
        print(f"  ... {report['matched_trades'] - 10} more (use --json for full list)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Live journal vs Alpaca fill reconciliation")
    parser.add_argument("--days", type=int, default=None, help="Lookback days (default: WISDOM_EVAL_DAYS)")
    parser.add_argument("--match-minutes", type=int, default=30, help="Max signal-to-fill lag")
    parser.add_argument("--json", dest="json_path", default=None, help="Write full report JSON")
    args = parser.parse_args()

    report = build_reconciliation_report(
        window_days=args.days,
        match_minutes=args.match_minutes,
    )
    _print_report(report)

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\nSaved -> {args.json_path}")


if __name__ == "__main__":
    main()
