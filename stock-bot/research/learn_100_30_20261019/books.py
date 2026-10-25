"""Continuous evaluation books. Selection is an input, never recomputed here."""

from __future__ import annotations

import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ABC = HERE.parent / "nyse_cash_abc"
WFS = HERE.parent / "walk_forward_select"
sys.path.insert(0, str(ABC))
sys.path.insert(0, str(WFS))

from engine import INITIAL_CASH, simulate  # noqa: E402
from market import slice_market, spy_bars, spy_dividends  # noqa: E402
from metrics import fold_metrics, spy_total_return  # noqa: E402
from plan import WARMUP, baseline_spec  # noqa: E402
from run_rotation import action_hits, material_block  # noqa: E402
from select_rule import NON_STRATEGY_REASONS, completed_trades  # noqa: E402
from stitch import simulate_continuous  # noqa: E402


def spec_dict(row: dict) -> dict:
    return {
        "ma_win": int(row["ma_win"]),
        "max_hold": int(row["max_hold"]),
        "atr_mult": float(row["atr_mult"]),
    }


def learner_schedule(dates: list[str], folds: list[dict], choices: dict[int, dict]) -> list:
    """A decision on session i uses the policy of the fold that contains session i+1."""
    by_day = {}
    for fold in folds:
        chosen = choices[int(fold["fold"])]
        for day in fold["eval_dates"]:
            by_day[day] = chosen
    schedule = []
    for i, _day in enumerate(dates):
        nxt = dates[i + 1] if i + 1 < len(dates) else None
        schedule.append(None if nxt not in by_day else dict(by_day[nxt]))
    return schedule


def fixed_schedule(dates: list[str], folds: list[dict], spec: dict) -> list:
    allowed = {day for fold in folds for day in fold["eval_dates"]}
    schedule = []
    for i, _day in enumerate(dates):
        nxt = dates[i + 1] if i + 1 < len(dates) else None
        schedule.append(dict(spec) if nxt in allowed else None)
    return schedule


def run_continuous(market: dict, dates: list[str], schedule: list, slip: float, *, liquidate: bool) -> dict:
    if len(schedule) != len(dates):
        raise RuntimeError("schedule length")
    bars, member, spy, actions = slice_market(market, dates)
    out = simulate_continuous(
        bars,
        member,
        dates,
        spy,
        schedule,
        decision_start=WARMUP,
        dividends=actions["dividends"],
        distributions=actions["distributions"],
        renames=actions["renames"],
        slip=slip,
        initial_cash=INITIAL_CASH,
        liquidate_terminal=liquidate,
        open_book=not liquidate,
    )
    block = material_block(action_hits(out["trade_log"], market["actions"], dates, market["lookup"]))
    out["action_block"] = {
        "blocked": bool(block["blocked"]),
        "priced_total": block["priced_total"],
        "hit_count": len(block["hits"]),
        "hits": block["hits"],
    }
    return out


def run_engine_baseline(market: dict, dates: list[str], slip: float) -> dict:
    bars, member, spy, actions = slice_market(market, dates)
    spec = baseline_spec()
    return simulate(
        bars,
        member,
        dates,
        spy,
        arm="A",
        decision_start=WARMUP,
        dividends=actions["dividends"],
        distributions=actions["distributions"],
        renames=actions["renames"],
        ma_win=spec["ma_win"],
        max_hold=spec["max_hold"],
        atr_mult=spec["atr_mult"],
        slip=slip,
        initial_cash=INITIAL_CASH,
    )


def engine_matches_stitch(engine: dict, stitch: dict) -> dict:
    keys = ("ending_equity", "max_drawdown", "trades", "cost_dollars", "turnover", "dividend_dollars", "net_return")
    gaps = {key: float(engine[key]) - float(stitch[key]) for key in keys}
    return {"matches": all(abs(value) < 1e-6 for value in gaps.values()), "engine_minus_stitch": gaps}


def eval_path(result: dict, eval_dates: list[str]) -> list[float]:
    by_day = {day: i for i, day in enumerate(result["equity_dates"])}
    first = by_day[eval_dates[0]]
    entering = INITIAL_CASH if first == 0 else result["equity_path"][first - 1]
    return [entering] + [result["equity_path"][by_day[day]] for day in eval_dates]


def path_stats(path: list[float], dates: list[str] | None = None) -> dict:
    """`path[0]` is equity entering the first session. Later points are session closes."""
    rets = [cur / prev - 1.0 for prev, cur in zip(path, path[1:]) if prev > 0]
    mean = sum(rets) / len(rets) if rets else None
    std = None
    sharpe = None
    if len(rets) >= 2 and mean is not None:
        var = sum((value - mean) ** 2 for value in rets) / (len(rets) - 1)
        std = math.sqrt(var)
        if std > 1e-12:
            sharpe = mean / std * math.sqrt(252.0)
    peak = path[0]
    peak_i = 0
    worst = 0.0
    worst_peak = 0
    worst_trough = 0
    for i, value in enumerate(path):
        if value >= peak:
            peak = value
            peak_i = i
        dd = (peak - value) / peak if peak > 0 else 0.0
        if dd > worst:
            worst = dd
            worst_peak = peak_i
            worst_trough = i
    recovery = None
    target = path[worst_peak]
    for i in range(worst_trough + 1, len(path)):
        if path[i] >= target - 1e-8:
            recovery = i - worst_trough
            break
    start_label = None if not dates else dates[0]
    end_label = None if not dates else dates[-1]
    return {
        "sessions": len(path) - 1,
        "start": start_label,
        "end": end_label,
        "starting_equity": path[0],
        "ending_equity": path[-1],
        "net_profit": path[-1] - path[0],
        "net_return": path[-1] / path[0] - 1.0 if path[0] else None,
        "max_drawdown": worst,
        "recovery_sessions_from_trough": recovery,
        "recovered": recovery is not None,
        "daily_observations": len(rets),
        "mean_daily_return": mean,
        "std_daily_return": std,
        "sharpe_sqrt252": sharpe,
        "sharpe_assumptions": "Daily simple equity returns, sample standard deviation with ddof=1, risk-free rate zero, scaled by sqrt(252). A 30-session fold Sharpe is descriptive only.",
    }


def trade_stats(trades: list[dict]) -> dict:
    done = completed_trades(trades)
    wins = [row for row in done if float(row["pnl"]) > 0]
    losses = [row for row in done if float(row["pnl"]) < 0]
    flats = [row for row in done if float(row["pnl"]) == 0]
    by_symbol: dict[str, float] = {}
    for row in done:
        by_symbol[row["symbol"]] = by_symbol.get(row["symbol"], 0.0) + float(row["pnl"])
    ranked = sorted(by_symbol.items(), key=lambda item: -item[1])
    positive = sum(value for _sym, value in ranked if value > 0)
    top = ranked[0][1] if ranked else 0.0
    top3 = sum(value for _sym, value in ranked[:3])
    excluded = {}
    for row in trades:
        if row["reason"] in NON_STRATEGY_REASONS:
            excluded[row["reason"]] = excluded.get(row["reason"], 0) + 1
    return {
        "completed_round_trips": len(done),
        "wins": len(wins),
        "losses": len(losses),
        "flats": len(flats),
        "win_rate": (len(wins) / len(done)) if done else None,
        "avg_win": (sum(float(row["pnl"]) for row in wins) / len(wins)) if wins else None,
        "avg_loss": (sum(float(row["pnl"]) for row in losses) / len(losses)) if losses else None,
        "excluded_reason_counts": excluded,
        "top_symbol": None if not ranked else {"symbol": ranked[0][0], "pnl": ranked[0][1]},
        "top_symbol_share_of_positive_pnl": (top / positive) if positive > 0 else None,
        "top3_share_of_positive_pnl": (top3 / positive) if positive > 0 else None,
        "symbol_pnl": ranked[:15],
    }


def fold_rows(result: dict, folds: list[dict], choices: dict | None) -> list[dict]:
    rows = []
    for fold in folds:
        row = fold_metrics(result, fold["eval_dates"], INITIAL_CASH)
        row["fold"] = fold["fold"]
        if choices is not None:
            selected = choices[int(fold["fold"])]
            row["selected"] = spec_dict(selected)
            row["candidate_id"] = selected.get("candidate_id")
            row["fallback"] = selected.get("choice") == "baseline_fallback"
        done = [
            trade
            for trade in completed_trades(result["trade_log"])
            if trade["exit_date"] in set(fold["eval_dates"])
        ]
        row["completed_round_trips"] = len(done)
        row["wins"] = sum(1 for trade in done if float(trade["pnl"]) > 0)
        row["losses"] = sum(1 for trade in done if float(trade["pnl"]) < 0)
        rows.append(row)
    return rows


def slice_return(result: dict, dates: list[str]) -> float:
    path = eval_path(result, dates)
    return path[-1] / path[0] - 1.0


def year_slices(result: dict, eval_dates: list[str]) -> list[dict]:
    years = []
    current = None
    bucket: list[str] = []
    for day in eval_dates:
        year = day[:4]
        if current is None:
            current = year
        if year != current:
            years.append(_year_row(result, current, bucket))
            bucket = []
            current = year
        bucket.append(day)
    if bucket and current:
        years.append(_year_row(result, current, bucket))
    return years


def _year_row(result: dict, year: str, dates: list[str]) -> dict:
    path = eval_path(result, dates)
    stats = path_stats(path, dates)
    return {"year": year, "sessions": len(dates), "net_return": stats["net_return"], "max_drawdown": stats["max_drawdown"]}


def spy_book(market: dict, dates: list[str], slip: float) -> dict:
    dividends = spy_dividends(dates)
    if not dividends:
        raise RuntimeError("SPY cash dividends are missing; refusing a price-only total return")
    bars = spy_bars(market, dates)
    out = spy_total_return(dates, bars, dividends, slip, INITIAL_CASH)
    out["dividend_events"] = len(out["dividends"])
    return out


def public_book(result: dict, eval_dates: list[str]) -> dict:
    path = eval_path(result, eval_dates)
    stats = path_stats(path, eval_dates)
    trades = trade_stats(result["trade_log"])
    block = result.get("action_block") or {}
    return {
        **stats,
        "turnover": result["turnover"],
        "cost_dollars": result["cost_dollars"],
        "avg_exposure": result["avg_exposure"],
        "dividend_dollars": result["dividend_dollars"],
        "reconciled": result["reconciled"],
        "distribution_unpriced": result["distribution_unpriced"],
        "unresolved_holdings": result["unresolved_holdings"],
        "stale_terminals": result["stale_terminals"],
        "valuation_inconclusive": result.get("valuation_inconclusive"),
        "action_blocked": bool(block.get("blocked")),
        "action_hit_count": block.get("hit_count"),
        "action_priced_total": block.get("priced_total"),
        "action_hits": block.get("hits"),
        "trades": trades,
        "ending_cash": result["ending_cash"],
    }
