"""Benchmark and attribution for saved baseline A. Does not alter the strategy.

Writes results/baseline_benchmark.json and results/baseline_benchmark.md.
Does not overwrite abc, rotation, or final-report artifacts.
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path

from engine import INITIAL_CASH, SLIP, adjusted_dividend_rate, buy_px, sell_px
from membership import research_membership
from run_abc import (
    DATA,
    DB_PATH,
    RESULTS,
    WARMUP,
    build_windows,
    dividend_map,
    load_tables,
    member_on,
    membership_index,
    panel_for,
)
from run_rotation import alias_fi, run_arm

OUT_JSON = RESULTS / "baseline_benchmark.json"
OUT_MD = RESULTS / "baseline_benchmark.md"
SAVED = RESULTS / "abc_results.json"
SPY_DIST = DATA / "spy_distributions.json"


def load_spy_dividends(dates: list[str]) -> dict[str, float]:
    """Same session mapping as dividend_map. No SPY splits are on file, so the cash rate is unchanged."""
    payload = json.loads(SPY_DIST.read_text(encoding="utf-8"))
    if payload.get("splits") or payload.get("stock_dividends"):
        raise SystemExit("BLOCKED: SPY split or stock dividend needs an explicit adjustment before a total return")
    session_set = set(dates)
    out: dict[str, float] = {}
    for row in payload["cash_dividends"]:
        rate = adjusted_dividend_rate(float(row["rate"]), row["ex_date"], [])
        if rate <= 0:
            continue
        day = row["ex_date"] if row["ex_date"] in session_set else next((d for d in dates if d >= row["ex_date"]), None)
        if day is None:
            continue
        out[day] = out.get(day, 0.0) + rate
    return out


def load_context():
    calendar = json.loads((DATA / "calendar.json").read_text(encoding="utf-8"))
    windows = build_windows(calendar)
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    bars, divs, splits, _notes = load_tables(conn)
    conn.close()
    membership = membership_index(research_membership())
    lookup = {}
    for rec in bars.itertuples(index=False):
        lookup.setdefault(rec.symbol, {})[rec.session] = rec
    alias_fi(lookup)
    div_all = dividend_map(divs, splits, windows["span"])
    return windows, lookup, membership, div_all


def panel(dates, lookup, membership, div_all):
    symbols = sorted(
        s for s in set(lookup) - {"SPY"} if any(member_on(membership, s, d) for d in dates[WARMUP:])
    )
    for needed in ("FI", "FISV"):
        if needed in lookup and needed not in symbols and any(member_on(membership, needed, d) for d in dates):
            symbols.append(needed)
    symbols = sorted(set(symbols))
    bars = {}
    for sym, series in lookup.items():
        bars.setdefault(sym, series)
    # panel_for expects Bar objects, which the stored rows already match via attribute access
    # only if they are Bar. Convert through the same constructor the runners use.
    from engine import Bar

    bar_lookup = {
        sym: {day: Bar(rec.open, rec.high, rec.low, rec.close) for day, rec in series.items()}
        for sym, series in lookup.items()
    }
    frame, member, spy = panel_for(dates, symbols, bar_lookup, membership)
    div_window = {s: {d: v for d, v in days.items() if d in set(dates)} for s, days in div_all.items()}
    return frame, member, spy, div_window, bar_lookup


def drawdown(path: list[float]) -> float:
    peak = path[0]
    worst = 0.0
    for value in path:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def ann_vol(path: list[float]) -> float:
    rets = [cur / prev - 1.0 for prev, cur in zip(path, path[1:]) if prev > 0]
    if len(rets) < 2:
        return float("nan")
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252.0)


def spy_total_return(dates: list[str], spy_bars: dict, dividends: dict[str, float], invest_frac: float) -> dict:
    """Buy on the first open after the first evaluation close. Dividends reinvest at that ex-date open.

    Cash that is not invested at the open earns zero interest and is never rebalanced into SPY.
    `spy_bars` is the SPY session map.
    """
    cash = float(INITIAL_CASH)
    parked = 0.0
    shares = 0.0
    dividend_cash = 0.0
    dividend_events = []
    path = []
    buy_i = WARMUP + 1
    last = len(dates) - 1
    entry = None
    for i, day in enumerate(dates):
        bar = spy_bars.get(day)
        if bar is None:
            raise RuntimeError(f"SPY bar missing {day}")
        credited = 0.0
        if i > 0 and shares > 0:
            rate = float(dividends.get(day, 0.0) or 0.0)
            if rate > 0:
                credited = shares * rate
                cash += credited
                dividend_cash += credited
                fill = buy_px(bar.open, SLIP)
                bought = cash - parked
                if bought > 1.0:
                    new_shares = bought / fill
                    cash -= bought
                    shares += new_shares
                    dividend_events.append(
                        {"session": day, "rate": rate, "cash": credited, "reinvest_open": bar.open, "shares": new_shares}
                    )
        if i == buy_i:
            budget = float(INITIAL_CASH) * invest_frac
            parked = float(INITIAL_CASH) - budget
            fill = buy_px(bar.open, SLIP)
            shares += budget / fill
            cash -= budget
            entry = {"session": day, "open": bar.open, "fill": fill, "dollars": budget, "shares": budget / fill}
        if i == last:
            if shares > 0:
                fill = sell_px(bar.close, SLIP)
                cash += shares * fill
                shares = 0.0
        if i >= WARMUP:
            equity = cash + shares * bar.close
            path.append({"session": day, "equity": equity, "cash": cash, "shares": shares})
    if not dividend_events:
        raise RuntimeError("SPY dividends were not credited; refusing a price-only total return")
    equities = [row["equity"] for row in path]
    end = equities[-1]
    return {
        "invest_frac": invest_frac,
        "entry": entry,
        "terminal_session": dates[-1],
        "terminal_close": spy_bars[dates[-1]].close,
        "dividend_cash": dividend_cash,
        "dividend_events": len(dividend_events),
        "ending_equity": end,
        "net_return": end / INITIAL_CASH - 1.0,
        "max_drawdown": drawdown(equities),
        "ann_vol": ann_vol(equities),
        "path": path,
        "dividends": dividend_events,
    }


def monthly(path: list[dict]) -> list[dict]:
    start = path[0]["equity"]
    buckets: dict[str, dict] = {}
    order = []
    prev = start
    # The first mark is the window base, not a return by itself.
    for row in path[1:]:
        month = row["session"][:7]
        if month not in buckets:
            buckets[month] = {"month": month, "start": prev, "end": row["equity"]}
            order.append(month)
        buckets[month]["end"] = row["equity"]
        prev = row["equity"]
    out = []
    for month in order:
        row = buckets[month]
        dollar = row["end"] - row["start"]
        out.append(
            {
                "month": month,
                "start_equity": row["start"],
                "end_equity": row["end"],
                "dollar": dollar,
                "return": dollar / row["start"] if row["start"] else None,
            }
        )
    return out


def ticker_pnl(trades: list[dict]) -> tuple[list[dict], dict]:
    grouped = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "distribution_pnl": 0.0})
    dist_pnl = 0.0
    for trade in trades:
        sym = trade["symbol"]
        grouped[sym]["pnl"] += trade["pnl"]
        grouped[sym]["trades"] += 1
        distributed = trade["reason"] == "distribution_fraction" or (
            sym == "GEV" and trade["entry_date"] == "2024-04-02" and trade["reason"] != "distribution_fraction"
        )
        if distributed:
            grouped[sym]["distribution_pnl"] += trade["pnl"]
            dist_pnl += trade["pnl"]
    rows = [
        {"symbol": sym, "pnl": row["pnl"], "trades": row["trades"], "distribution_pnl": row["distribution_pnl"]}
        for sym, row in grouped.items()
    ]
    rows.sort(key=lambda row: (-row["pnl"], row["symbol"]))
    return rows, {"distribution_pnl": dist_pnl}


def positive_slice(rows: list[dict], n: int, positive_sum: float, net: float) -> dict:
    chosen = [row for row in rows if row["pnl"] > 0][:n]
    dollars = sum(row["pnl"] for row in chosen)
    return {
        "n": n,
        "symbols": [row["symbol"] for row in chosen],
        "dollars": dollars,
        "share_of_positive_pnl": dollars / positive_sum if positive_sum else None,
        "share_of_net_pnl": dollars / net if net else None,
    }


def main() -> None:
    saved = json.loads(SAVED.read_text(encoding="utf-8"))
    windows, lookup, membership, div_all = load_context()
    spy_div = load_spy_dividends(windows["span"])
    if not spy_div:
        raise SystemExit("BLOCKED: SPY cash dividends are unavailable; not using a price-only total return")
    report = {
        "assumptions": {
            "starting_cash": INITIAL_CASH,
            "slippage": "5 bps per side on the execution price, once; not also a fee",
            "spy_execution": "Buy at the open of the session after the first evaluation close. Sell at the final evaluation close.",
            "dividends": "SPY cash dividends are credited once, only if shares were held at the prior close. Rates come from data/spy_distributions.json: Alpaca cash rates through 2026-06-18, plus 2026-09-18 at 1.888834 because Alpaca omitted it. That ex-date is on State Street's 2026 SPY schedule; the amount matches Wall Street Horizon and Investing.com, and those pages match Alpaca for March and June 2026. No SPY split applies. Prices are Alpaca Adjustment.SPLIT and are not dividend-adjusted. Dividend cash is reinvested in SPY at that session's open, paying 5 bps.",
            "cash_interest": "Zero. Uninvested cash earns nothing, matching the strategy ledger.",
            "mix_95_5": "95 percent of starting cash buys SPY. 5 percent stays cash at zero interest and is not rebalanced. Dividends on the SPY shares are reinvested into SPY.",
            "excess_return": "Window total return of A minus the benchmark window total return. Not beta-adjusted.",
        },
        "spy_dividend_price_check": {
            "ex_date": "2024-03-15",
            "rate": spy_div["2024-03-15"],
            "prior_close": lookup["SPY"]["2024-03-14"].close,
            "open": lookup["SPY"]["2024-03-15"].open,
            "close": lookup["SPY"]["2024-03-15"].close,
            "note": "The traded close falls by more than the cash dividend. The OHLC is not dividend-adjusted, so the cash dividend is added once.",
        },
        "windows": {},
    }
    for label in ("prior", "recent"):
        dates = windows[label]
        frame, member, spy, div_window, bar_lookup = panel(dates, lookup, membership, div_all)
        journal = []
        out = run_arm(frame, member, dates, spy, div_window, "A", journal=journal)
        marks = [row for row in journal if row["kind"] == "mark"]
        equities = [row["equity"] for row in marks]
        stored = saved["arms"][label]["A"]
        checks = {
            "ending_equity_matches": abs(out["ending_equity"] - stored["ending_equity"]) < 1e-6,
            "max_drawdown_matches": abs(drawdown(equities) - stored["max_drawdown"]) < 1e-12,
            "stored_ending_equity": stored["ending_equity"],
            "stored_max_drawdown": stored["max_drawdown"],
            "replay_ending_equity": out["ending_equity"],
            "replay_max_drawdown": drawdown(equities),
            "marks": len(marks),
        }
        if not checks["ending_equity_matches"] or not checks["max_drawdown_matches"]:
            raise SystemExit(f"BLOCKED: replay does not match saved A for {label}: {checks}")
        spy_bars = bar_lookup["SPY"]
        full = spy_total_return(dates, spy_bars, spy_div, 1.0)
        mix = spy_total_return(dates, spy_bars, spy_div, 0.95)
        net = out["ending_equity"] - INITIAL_CASH
        trade_pnl = sum(t["pnl"] for t in out["trade_log"])
        rows, dist = ticker_pnl(out["trade_log"])
        positive = [row for row in rows if row["pnl"] > 0]
        positive_sum = sum(row["pnl"] for row in positive)
        losers = sorted(rows, key=lambda row: (row["pnl"], row["symbol"]))[:10]
        winners = sorted(out["trade_log"], key=lambda t: -t["pnl"])
        top_trades = [
            {
                "symbol": t["symbol"],
                "reason": t["reason"],
                "entry_date": t["entry_date"],
                "exit_date": t["exit_date"],
                "pnl": t["pnl"],
                "shares": t["shares"],
            }
            for t in winners[:10]
        ]
        positive_trades = [t for t in winners if t["pnl"] > 0]
        drop1 = positive_trades[0]["pnl"] if positive_trades else 0.0
        drop5 = sum(t["pnl"] for t in positive_trades[:5])
        months = monthly(marks)
        pos_months = [m for m in months if m["dollar"] > 0]
        best = max(months, key=lambda m: m["dollar"])
        pos_month_dollars = sum(m["dollar"] for m in pos_months)
        identity = trade_pnl + out["dividend_dollars"] - net
        report["windows"][label] = {
            "start": dates[WARMUP],
            "end": dates[-1],
            "first_spy_buy_session": dates[WARMUP + 1],
            "verification": checks,
            "baseline": {
                "net_return": out["net_return"],
                "ending_equity": out["ending_equity"],
                "max_drawdown": checks["replay_max_drawdown"],
                "ann_vol": ann_vol(equities),
                "net_pnl": net,
                "trade_pnl": trade_pnl,
                "distribution_pnl": dist["distribution_pnl"],
                "trading_pnl_ex_distribution": trade_pnl - dist["distribution_pnl"],
                "dividends": out["dividend_dollars"],
                "costs_already_inside_pnl": out["cost_dollars"],
                "identity_residual": identity,
            },
            "spy_100": {k: v for k, v in full.items() if k not in {"path", "dividends"}},
            "spy_95_5": {k: v for k, v in mix.items() if k not in {"path", "dividends"}},
            "excess_vs_spy_100": out["net_return"] - full["net_return"],
            "excess_vs_spy_95_5": out["net_return"] - mix["net_return"],
            "concentration": {
                "positive_tickers": len(positive),
                "positive_ticker_pnl": positive_sum,
                "top": {str(n): positive_slice(rows, n, positive_sum, net) for n in (1, 5, 10)},
                "losers": losers,
                "trade_winners": top_trades,
                "ge_pnl": next((row["pnl"] for row in rows if row["symbol"] == "GE"), 0.0),
                "gev_pnl": next((row["pnl"] for row in rows if row["symbol"] == "GEV"), 0.0),
                "gev_distribution_pnl": next((row["distribution_pnl"] for row in rows if row["symbol"] == "GEV"), 0.0),
            },
            "months": months,
            "best_month": {
                **best,
                "share_of_positive_monthly_dollars": best["dollar"] / pos_month_dollars if pos_month_dollars else None,
                "share_of_net_pnl": best["dollar"] / net if net else None,
            },
            "dependence_diagnostic": {
                "label": "Arithmetic subtraction from saved final P&L. Not a rerun. Removing these trades would change later cash, sizing, and trades.",
                "saved_final_pnl": net,
                "drop_largest_1_trade_pnl": drop1,
                "pnl_after_dropping_1": net - drop1,
                "return_after_dropping_1": (INITIAL_CASH + net - drop1) / INITIAL_CASH - 1.0,
                "dropped_1": top_trades[:1],
                "drop_largest_5_trade_pnl": drop5,
                "pnl_after_dropping_5": net - drop5,
                "return_after_dropping_5": (INITIAL_CASH + net - drop5) / INITIAL_CASH - 1.0,
                "dropped_5": top_trades[:5],
            },
        }
        # Keep the dividend audit list out of the huge path but record rates used in-window.
        used = [
            {"session": day, "rate": spy_div[day]}
            for day in dates[WARMUP + 1 :]
            if day in spy_div
        ]
        report["windows"][label]["spy_dividends_credited_on"] = used
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    OUT_MD.write_text(render(report), encoding="utf-8")
    print("wrote", OUT_JSON)


def pct(value: float) -> str:
    return f"{100.0 * value:.2f}%"


def render(report: dict) -> str:
    lines = [
        "# Baseline A benchmark and attribution",
        "",
        "Baseline A was replayed and not changed. Figures below are checked against `results/abc_results.json`.",
        "",
        "SPY total return buys at the first open after the first evaluation close and sells at the final close, with 5 bps per side. SPY cash dividends were not in the research dividend table. Rates are in `data/spy_distributions.json`: Alpaca cash rates through 2026-06-18, plus the 2026-09-18 rate of 1.888834 because Alpaca did not return it. That ex-date is on State Street's 2026 SPY schedule, and the amount matches Wall Street Horizon and Investing.com, whose March and June 2026 rates match Alpaca. No SPY split is applied. A dividend is credited once when the shares were held at the prior close, then reinvested in SPY at that session's open with 5 bps, including a final-session dividend that is sold at the closing liquidation. Prices are not dividend-adjusted. Cash earns zero interest. The 95/5 book invests 95 percent of starting cash and leaves 5 percent in cash with no rebalancing.",
        "",
        "Excess return is the window total return of A minus the benchmark. It is not beta-adjusted.",
        "",
    ]
    for label, row in report["windows"].items():
        base = row["baseline"]
        spy = row["spy_100"]
        mix = row["spy_95_5"]
        lines.append(f"## {label} {row['start']} → {row['end']}")
        lines.append("")
        lines.append(
            f"Saved A equity {row['verification']['stored_ending_equity']:.8f} and max drawdown {row['verification']['stored_max_drawdown']:.12f} match the replay."
        )
        lines.append("")
        lines.append("| Book | Return | Max DD | Ann. vol | Excess vs A | End equity |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        lines.append(
            f"| A | {pct(base['net_return'])} | {pct(base['max_drawdown'])} | {pct(base['ann_vol'])} |  | ${base['ending_equity']:,.2f} |"
        )
        lines.append(
            f"| SPY total return | {pct(spy['net_return'])} | {pct(spy['max_drawdown'])} | {pct(spy['ann_vol'])} | {100 * row['excess_vs_spy_100']:+.2f} pp | ${spy['ending_equity']:,.2f} |"
        )
        lines.append(
            f"| 95% SPY / 5% cash | {pct(mix['net_return'])} | {pct(mix['max_drawdown'])} | {pct(mix['ann_vol'])} | {100 * row['excess_vs_spy_95_5']:+.2f} pp | ${mix['ending_equity']:,.2f} |"
        )
        lines.append("")
        lines.append(
            f"SPY buy {spy['entry']['session']} open {spy['entry']['open']:.4f}. "
            f"Terminal close {spy['terminal_close']:.4f}. "
            f"Dividend cash reinvested ${spy['dividend_cash']:,.2f} across {spy['dividend_events']} ex-dates."
        )
        lines.append("")
        lines.append(
            f"P&L identity: trading ex-distribution ${base['trading_pnl_ex_distribution']:,.2f} "
            f"+ distribution ${base['distribution_pnl']:,.2f} "
            f"+ dividends ${base['dividends']:,.2f} "
            f"= net ${base['net_pnl']:,.2f}. Residual {base['identity_residual']:.3e}. "
            f"Slippage of ${base['costs_already_inside_pnl']:,.2f} is already inside those P&L figures."
        )
        lines.append("")
        conc = row["concentration"]
        lines.append("| Slice | Dollars | Share of positive ticker P&L | Share of net P&L | Names |")
        lines.append("|---|---:|---:|---:|---|")
        for n in ("1", "5", "10"):
            item = conc["top"][n]
            lines.append(
                f"| Top {n} | ${item['dollars']:,.2f} | {pct(item['share_of_positive_pnl'])} | {pct(item['share_of_net_pnl'])} | {', '.join(item['symbols'])} |"
            )
        lines.append("")
        lines.append("Largest losing tickers:")
        lines.append("")
        for item in conc["losers"][:5]:
            lines.append(f"- {item['symbol']}: ${item['pnl']:,.2f} across {item['trades']} trades")
        lines.append("")
        lines.append("Largest winning trades:")
        lines.append("")
        for item in conc["trade_winners"][:5]:
            lines.append(
                f"- {item['symbol']} {item['entry_date']} → {item['exit_date']} {item['reason']}: ${item['pnl']:,.2f}"
            )
        lines.append("")
        if conc["gev_distribution_pnl"]:
            lines.append(
                f"GE ticker P&L ${conc['ge_pnl']:,.2f}. GEV ticker P&L ${conc['gev_pnl']:,.2f}, of which the distributed lot and fractional cash are ${conc['gev_distribution_pnl']:,.2f}. Those are not added again on top of the ticker totals."
            )
            lines.append("")
        best = row["best_month"]
        lines.append(
            f"Best month {best['month']}: {pct(best['return'])}, ${best['dollar']:,.2f}. "
            f"That is {pct(best['share_of_positive_monthly_dollars'])} of positive monthly dollar gains"
            + (f" and {pct(best['share_of_net_pnl'])} of net P&L." if best["share_of_net_pnl"] is not None else ".")
        )
        lines.append("")
        lines.append("| Month | Return | Dollar |")
        lines.append("|---|---:|---:|")
        for month in row["months"]:
            lines.append(f"| {month['month']} | {pct(month['return'])} | ${month['dollar']:,.2f} |")
        lines.append("")
        diag = row["dependence_diagnostic"]
        lines.append(
            f"Attribution diagnostic, not a backtest: saved P&L ${diag['saved_final_pnl']:,.2f}. "
            f"After subtracting the largest trade (${diag['drop_largest_1_trade_pnl']:,.2f}), P&L is ${diag['pnl_after_dropping_1']:,.2f} ({pct(diag['return_after_dropping_1'])}). "
            f"After subtracting the largest five (${diag['drop_largest_5_trade_pnl']:,.2f}), P&L is ${diag['pnl_after_dropping_5']:,.2f} ({pct(diag['return_after_dropping_5'])})."
        )
        lines.append("")
    lines.append("These windows were already inspected. They are not untouched out-of-sample evidence. This comparison does not make A production-ready and does not apply to Lab 8.")
    lines.append("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
