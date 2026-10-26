"""Exploratory outside-in, multi-horizon market-field backtest.

Market observations define each stock's four-scale state. A fixed 600-cell
provides a predeclared directional mesh over long-to-near-term momentum; one
local graph-smoothing step feeds a center BUY/WAIT readout. This is an isolated
research replay: it does not change live strategy code or broker settings.

The previously examined final-quarter holdout is deliberately excluded. The
development window is exploratory because earlier screens have used it.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from modules.pit_universe import load_sp500_membership  # noqa: E402
from nyse_momentum_walkforward import (  # noqa: E402
    ATR_MULT,
    ATR_PERIOD,
    INITIAL,
    MA_MIN,
    MA_WINDOW,
    RSI_MAX,
    SLIP,
    _is_member,
    _member_on,
    load_intraday_panel,
)
from hypersphere_600cell_backtest import build_600_cell_vertices  # noqa: E402


OUT_JSON = Path(__file__).with_name("hypersphere_inward_funnel_backtest_last.json")
OUT_MD = Path(__file__).with_name("hypersphere_inward_funnel_backtest_last.md")
SIGNAL_TIME = time(10, 15)
HORIZONS = (60, 20, 5)
MAX_HOLD_DAYS = 30
MAX_POSITIONS = 5
MAX_POSITION_PCT = 0.15
SLEEVE_CAP = 0.95
VOTE_SIZES = {2: 0.05, 3: 0.10, 4: 0.15}
SMOOTH_OWN = 0.65
SMOOTH_NEIGHBORS = 0.35


def build_mesh() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Build the 600-cell directional mesh and its 12-regular graph."""
    vertices = np.asarray(build_600_cell_vertices(), dtype=float)
    dots = vertices @ vertices.T
    np.fill_diagonal(dots, -np.inf)
    edge_cos = (1.0 + math.sqrt(5.0)) / 4.0
    adjacency = (dots >= edge_cos - 1e-10).astype(float)
    degrees = adjacency.sum(axis=1)
    edge_count = int(adjacency.sum() // 2)
    if len(vertices) != 120 or edge_count != 720 or not np.all(degrees == 12):
        raise RuntimeError(
            f"invalid 600-cell graph: vertices={len(vertices)}, edges={edge_count}, "
            f"degrees={sorted(set(degrees.astype(int)))}"
        )
    # The positive diagonal is the predeclared all-horizons-up buy hemisphere.
    buy_side = vertices.sum(axis=1) > 1e-12
    if not np.any(buy_side):
        raise RuntimeError("600-cell buy hemisphere is empty")
    return vertices, adjacency, buy_side


def build_daily_features(panel: dict, max_session: int) -> tuple[np.ndarray, dict]:
    """Return [60d, 20d, 5d, current 10:15] clipped volatility-scaled returns."""
    symbols = panel["symbols"]
    sessions = list(panel["sessions"][:max_session])
    dates = panel["dates"]
    closes = panel["close"]
    n_sym = len(symbols)
    n_days = len(sessions)

    daily_close = np.full((n_sym, n_days), np.nan, dtype=float)
    bars_by_day: list[np.ndarray] = []
    for d, day in enumerate(sessions):
        bars = np.flatnonzero(dates == day)
        bars_by_day.append(bars)
        if not bars.size:
            continue
        day_closes = closes[:, bars]
        valid = np.isfinite(day_closes) & (day_closes > 0)
        last = np.where(valid, np.arange(bars.size)[None, :], -1).max(axis=1)
        rows = np.flatnonzero(last >= 0)
        daily_close[rows, d] = day_closes[rows, last[rows]]

    daily_ret = np.full_like(daily_close, np.nan)
    daily_ret[:, 1:] = daily_close[:, 1:] / daily_close[:, :-1] - 1.0
    features = np.full((n_sym, n_days, 4), np.nan, dtype=float)
    for d in range(max(HORIZONS) + 1, n_days):
        vol_window = daily_ret[:, max(1, d - 20):d]
        enough_returns = np.isfinite(vol_window).sum(axis=1) >= 2
        daily_vol = np.full(n_sym, np.nan, dtype=float)
        if enough_returns.any():
            daily_vol[enough_returns] = np.nanstd(vol_window[enough_returns], axis=1, ddof=1)
        for k, horizon in enumerate(HORIZONS):
            past = daily_close[:, d - 1 - horizon]
            latest = daily_close[:, d - 1]
            valid = np.isfinite(past) & np.isfinite(latest) & (past > 0) & np.isfinite(daily_vol) & (daily_vol > 1e-8)
            raw = np.full(n_sym, np.nan)
            raw[valid] = latest[valid] / past[valid] - 1.0
            scale = daily_vol * math.sqrt(horizon)
            z = np.divide(raw, scale, out=np.full(n_sym, np.nan), where=np.isfinite(scale) & (scale > 1e-8))
            features[:, d, k] = np.tanh(z / 1.5)

        # Current-session return as known at each stock's 10:15 signal bar.
        bars = bars_by_day[d]
        if not bars.size:
            continue
        signal_bar = None
        for bar_i in bars:
            if panel["times"][bar_i].time() >= SIGNAL_TIME:
                signal_bar = int(bar_i)
                break
        prior_close = daily_close[:, d - 1]
        if signal_bar is None:
            continue
        mark = closes[:, signal_bar]
        valid = np.isfinite(mark) & (mark > 0) & np.isfinite(prior_close) & (prior_close > 0) & np.isfinite(daily_vol) & (daily_vol > 1e-8)
        raw = np.full(n_sym, np.nan)
        raw[valid] = mark[valid] / prior_close[valid] - 1.0
        z = np.divide(raw, daily_vol, out=np.full(n_sym, np.nan), where=np.isfinite(daily_vol) & (daily_vol > 1e-8))
        features[:, d, 3] = np.tanh(z / 1.5)

    audit = {
        "feature_order": ["60-session momentum", "20-session momentum", "5-session momentum", "prior-close-to-10:15 return"],
        "daily_volatility_window": 20,
        "normalization": "return / (trailing daily volatility * sqrt(horizon)); tanh(z / 1.5), using data available by the signal bar",
        "finite_feature_rows": int(np.isfinite(features).all(axis=2).sum()),
    }
    return features, audit


def build_gap_lookup(panel: dict) -> dict[tuple, float]:
    dates = panel["dates"]
    opens = panel["open"]
    closes = panel["close"]
    starts = np.r_[0, np.flatnonzero(dates[1:] != dates[:-1]) + 1]
    ends = np.r_[starts[1:] - 1, len(dates) - 1]
    out: dict[tuple, float] = {}
    for s, symbol in enumerate(panel["symbols"]):
        previous_close = None
        for start, end in zip(starts, ends):
            day = dates[start]
            op = opens[s, start:end + 1]
            cp = closes[s, start:end + 1]
            op_ok = np.flatnonzero(np.isfinite(op) & (op > 0))
            cp_ok = np.flatnonzero(np.isfinite(cp) & (cp > 0))
            if op_ok.size and previous_close and previous_close > 0:
                out[(symbol, day)] = float(op[int(op_ok[0])] / previous_close - 1.0)
            if cp_ok.size:
                previous_close = float(cp[int(cp_ok[-1])])
    return out


def mesh_center_score(features: np.ndarray, vertices: np.ndarray, adjacency: np.ndarray,
                      buy_side: np.ndarray, *, propagate: bool = True) -> float:
    raw = vertices @ features
    if propagate:
        neighbor_mean = adjacency @ raw / 12.0
        raw = SMOOTH_OWN * raw + SMOOTH_NEIGHBORS * neighbor_mean
    return float(np.mean(raw[buy_side]))


def simulate(panel: dict, features: np.ndarray, spans: dict, mode: str,
             vertices: np.ndarray, adjacency: np.ndarray, buy_side: np.ndarray,
             dev_sessions: int) -> dict:
    symbols = panel["symbols"]
    times = panel["times"]
    dates = panel["dates"]
    sessions = list(panel["sessions"][:dev_sessions])
    opens, highs, lows, closes = panel["open"], panel["high"], panel["low"], panel["close"]
    ma, rsi, atr_map = panel["ma"], panel["rsi"], panel["daily_atr"]
    session_index = {day: i for i, day in enumerate(sessions)}
    dev_bar_indices = np.flatnonzero(np.isin(dates, sessions))
    if not dev_bar_indices.size:
        raise RuntimeError("no bars in development window")
    last_bar = int(dev_bar_indices[-1])
    signal_bar_by_day: dict = {}
    for i in dev_bar_indices:
        day = dates[i]
        if day not in signal_bar_by_day and times[i].time() >= SIGNAL_TIME:
            signal_bar_by_day[day] = int(i)
    gap_by_sym_day = build_gap_lookup(panel)
    member_by_day: dict = {}
    cash = INITIAL
    positions: dict[int, dict] = {}
    trades: list[dict] = []
    fills: list[dict] = []
    equity_by_day: dict = {}
    entered_today: set[tuple] = set()

    def mark(i: int) -> float:
        total = cash
        for s, pos in positions.items():
            px = closes[s, i]
            if np.isfinite(px) and px > 0:
                total += pos["shares"] * float(px)
            else:
                total += pos["shares"] * pos["last_mark"]
        return float(total)

    def close_pos(s: int, i: int, raw_px: float, reason: str) -> None:
        nonlocal cash
        pos = positions[s]
        px = max(0.01, float(raw_px))
        fill = px * (1.0 - SLIP)
        proceeds = pos["shares"] * fill
        cash += proceeds
        trades.append({
            "symbol": symbols[s], "entry_date": str(pos["entry_day"]),
            "exit_date": str(dates[i]),
            "days_held": int((pd.Timestamp(dates[i]) - pd.Timestamp(pos["entry_day"])).days),
            "entry_rank": pos["rank"], "horizon_votes": pos["votes"],
            "ret_pct": (fill / pos["entry"] - 1.0) * 100.0,
            "pnl_usd": proceeds - pos["notional"], "reason": reason,
            "notional": pos["notional"],
        })
        fills.append({"session": session_index[dates[i]], "side": "sell", "notional": proceeds,
                      "cost": pos["shares"] * px * SLIP})
        del positions[s]

    for i in range(last_bar):
        day = dates[i]
        if i == 0 or dates[i - 1] != day:
            entered_today = set()

        for s, pos in list(positions.items()):
            if i < pos["entry_i"]:
                continue
            op, hi, lo = opens[s, i], highs[s, i], lows[s, i]
            if not all(np.isfinite(v) and v > 0 for v in (op, hi, lo)):
                continue
            pos["last_mark"] = float(closes[s, i]) if np.isfinite(closes[s, i]) and closes[s, i] > 0 else pos["last_mark"]
            if op <= pos["stop"]:
                close_pos(s, i, float(op), "stop_gap")
                continue
            if lo <= pos["stop"]:
                close_pos(s, i, float(pos["stop"]), "stop")
                continue
            if pd.Timestamp(day).date() >= pos["expiry"]:
                close_pos(s, i, float(op), "time")

        if signal_bar_by_day.get(day) == i and i + 1 < len(dates) and dates[i + 1] == day:
            day_i = session_index[day]
            if day_i < max(HORIZONS) + 1:
                continue
            if day not in member_by_day:
                member_by_day[day] = np.array([_is_member(spans, sym, day) for sym in symbols], dtype=bool)
            member = member_by_day[day]
            candidates = []
            for s, sym in enumerate(symbols):
                if s in positions or (day, s) in entered_today or not member[s]:
                    continue
                px, avg, rs = closes[s, i], ma[s, i], rsi[s, i]
                if not (np.isfinite(px) and np.isfinite(avg) and avg > 0 and px > avg):
                    continue
                if np.isfinite(rs) and rs >= RSI_MAX:
                    continue
                gap = gap_by_sym_day.get((sym, day))
                if gap is not None and gap > 0.02:
                    continue
                if mode == "baseline_ma50":
                    score = float(px / avg - 1.0)
                    votes = 0
                else:
                    state = features[s, day_i]
                    if not np.isfinite(state).all():
                        continue
                    votes = int(np.sum(state > 0))
                    if votes < 2:
                        continue
                    if mode == "four_horizon_mean_dynamic":
                        score = float(np.mean(state))
                    elif mode == "mesh_no_propagation_dynamic":
                        score = mesh_center_score(state, vertices, adjacency, buy_side, propagate=False)
                    else:
                        score = mesh_center_score(state, vertices, adjacency, buy_side)
                    if score <= 0:
                        continue
                candidates.append((score, votes, s))
            candidates.sort(reverse=True)

            free_slots = MAX_POSITIONS - len(positions)
            equity_now = mark(i)
            gross = sum(pos["shares"] * pos["last_mark"] for pos in positions.values())
            base_budget = min(equity_now * MAX_POSITION_PCT, equity_now * SLEEVE_CAP / MAX_POSITIONS)
            bought = 0
            for rank, (score, votes, s) in enumerate(candidates, start=1):
                if bought >= free_slots:
                    break
                raw = opens[s, i + 1]
                if not np.isfinite(raw) or raw <= 0:
                    continue
                size_pct = (MAX_POSITION_PCT if mode in ("baseline_ma50", "funnel_equal")
                            else VOTE_SIZES.get(votes, 0.0))
                target = min(base_budget, equity_now * size_pct)
                sleeve_room = max(0.0, equity_now * SLEEVE_CAP - gross)
                room = min(target, sleeve_room, cash)
                if room <= 1.0:
                    break
                fill = float(raw) * (1.0 + SLIP)
                shares = room / fill
                atr = atr_map[symbols[s]].get(day)
                if atr is None or atr <= 0:
                    atr = fill * 0.02
                stop = max(0.01, fill - ATR_MULT * float(atr))
                positions[s] = {
                    "shares": shares, "entry": fill, "stop": stop,
                    "expiry": (pd.Timestamp(day) + pd.Timedelta(days=MAX_HOLD_DAYS)).date(),
                    "entry_i": i + 1, "entry_day": dates[i + 1], "rank": rank,
                    "notional": room, "votes": votes, "last_mark": fill,
                    "center_score": score,
                }
                cash -= shares * fill
                entered_today.add((day, s))
                fills.append({"session": day_i, "side": "buy", "notional": room,
                              "cost": shares * float(raw) * SLIP})
                gross += room
                bought += 1

        if i + 1 == last_bar or dates[i + 1] != day:
            equity_by_day[day] = mark(i)

    # Liquidate open positions at the last development-window close.
    for s, pos in list(positions.items()):
        px = closes[s, last_bar]
        if not np.isfinite(px) or px <= 0:
            px = pos["last_mark"]
        close_pos(s, last_bar, float(px), "development_window_end")
    equity_by_day[sessions[-1]] = cash
    equity = [float(equity_by_day.get(day, INITIAL)) for day in sessions]
    ledger = INITIAL + sum(t["pnl_usd"] for t in trades)
    if abs(cash - ledger) > 0.05:
        raise RuntimeError(f"ledger mismatch: cash {cash:.2f}, ledger {ledger:.2f}")
    return {"equity": equity, "sessions": [str(x) for x in sessions], "trades": trades,
            "fills": fills, "end_equity": cash, "ledger_gap": cash - ledger}


def summarize(result: dict) -> dict:
    eq = np.asarray(result["equity"], dtype=float)
    returns = np.diff(np.r_[INITIAL, eq]) / np.maximum(np.r_[INITIAL, eq[:-1]], 1e-12)
    path = np.r_[INITIAL, eq]
    drawdown = path / np.maximum.accumulate(path) - 1.0
    sharpe = None
    if len(returns) >= 5 and np.std(returns, ddof=1) > 1e-12:
        sharpe = round(float(np.mean(returns) / np.std(returns, ddof=1) * np.sqrt(252)), 2)
    trades = result["trades"]
    costs = sum(fill["cost"] for fill in result["fills"])
    notionals = sum(fill["notional"] for fill in result["fills"])
    return {
        "return_pct": round((eq[-1] / INITIAL - 1.0) * 100, 2),
        "max_drawdown_pct": round(float(drawdown.min() * 100), 2),
        "sharpe": sharpe,
        "trades": len(trades),
        "win_rate_pct": round(100 * np.mean([t["ret_pct"] > 0 for t in trades]), 1) if trades else None,
        "avg_trade_pct": round(float(np.mean([t["ret_pct"] for t in trades])), 2) if trades else None,
        "median_hold_days": round(float(np.median([t["days_held"] for t in trades])), 1) if trades else None,
        "stop_exits": sum(t["reason"].startswith("stop") for t in trades),
        "time_exits": sum(t["reason"] == "time" for t in trades),
        "window_end_exits": sum(t["reason"] == "development_window_end" for t in trades),
        "slippage_cost_usd": round(float(costs), 2),
        "turnover": round(float(notionals / np.mean(path)), 2),
        "end_equity": round(float(eq[-1]), 2),
        "positions_by_votes": {
            str(v): sum(t["horizon_votes"] == v for t in trades)
            for v in (2, 3, 4)
        },
    }


def main() -> int:
    panel, data_meta = load_intraday_panel()
    full_sessions = len(panel["sessions"])
    dev_sessions = max(1, int(full_sessions * 0.75))
    vertices, adjacency, buy_side = build_mesh()
    features, feature_audit = build_daily_features(panel, dev_sessions)
    membership = load_sp500_membership()
    spans = _member_on(membership)
    basis = np.eye(4)
    propagated_coefficients = [
        mesh_center_score(basis[i], vertices, adjacency, buy_side, propagate=True)
        for i in range(4)
    ]
    raw_mesh_coefficients = [
        mesh_center_score(basis[i], vertices, adjacency, buy_side, propagate=False)
        for i in range(4)
    ]

    # One predeclared setup with fixed-rule ablations; no parameter sweep.
    results = {}
    for strategy, mode in (("ma50_top5_fixed", "baseline_ma50"),
                           ("inward_mesh_top5_fixed", "funnel_equal"),
                           ("inward_mesh_top5_conviction", "funnel_dynamic"),
                           ("inward_mesh_no_propagation_conviction", "mesh_no_propagation_dynamic"),
                           ("four_horizon_mean_conviction", "four_horizon_mean_dynamic")):
        result = simulate(panel, features, spans, mode, vertices, adjacency, buy_side, dev_sessions)
        results[strategy] = {"metrics": summarize(result), "trades": result["trades"]}
        print(f"{strategy}: return={results[strategy]['metrics']['return_pct']}% "
              f"maxDD={results[strategy]['metrics']['max_drawdown_pct']}% "
              f"trades={results[strategy]['metrics']['trades']}", flush=True)

    report = {
        "research_only": True,
        "data": data_meta,
        "window": {
            "role": "exploratory development only; the previously examined final-quarter holdout is not evaluated",
            "sessions_used": dev_sessions,
            "sessions_total_cached": full_sessions,
            "start": str(panel["sessions"][0]),
            "end": str(panel["sessions"][dev_sessions - 1]),
            "independent_validation": False,
        },
        "geometry": {
            "name": "regular 600-cell",
            "vertices": len(vertices),
            "edges": 720,
            "degree": 12,
            "feature_axes": list(feature_audit["feature_order"]),
            "buy_hemisphere_vertices": int(buy_side.sum()),
            "construction": "Each finite stock/session observation supplies four lagged horizon features. The 120 fixed 600-cell directions score that point; a single fixed 65/35 local-neighbor propagation feeds the center readout.",
            "center_rule": "Mean propagated score on the predeclared hemisphere whose weight coordinates sum positive; BUY candidate only when center score > 0 and at least 2 of 4 horizon features are positive.",
            "center_score_limit": "A heuristic ranking score, not an estimated expected return, calibrated probability, or proven profit-maximizing center.",
            "ablation": "Alongside the smoothed mesh, compare identical conviction sizing and gates using the unsmoothed mesh and a plain equal-weight mean of the four horizon features.",
            "center_reduction": {
                "smoothed_mesh_coefficients": propagated_coefficients,
                "unsmoothed_mesh_coefficients": raw_mesh_coefficients,
                "plain_equal_mean_coefficients": [0.25] * 4,
                "finding": "Both mesh center formulas assign the same coefficient to all four axes. Each is therefore a positive scalar multiple of the plain equal-weight mean. With a zero threshold, rankings and BUY/WAIT decisions are identical; graph propagation adds no new signal in this setup.",
            },
            "dynamic_size": "2 positive horizons = 5% equity; 3 = 10%; 4 = 15%; capped by 15% per position and 95% gross sleeve.",
            "interpretation": "This is a finite discretization and a center readout, not an infinite or quantum computer; all scores and geometry choices are fixed before this replay.",
        },
        "feature_audit": feature_audit,
        "execution": {
            "signal": "First cached 5-minute bar at or after 10:15 ET; features use only data through that bar.",
            "entry": "Next same-session 5-minute bar open plus 5 bps slippage.",
            "eligibility": "Point-in-time S&P 500 membership, current close above the bot's trailing 50-bar MA, RSI below 70, opening gap no more than 2%.",
            "selection": "Up to five holdings, highest center scores first.",
            "exits": "Existing 2x lagged 14-session ATR stop and 30-calendar-day time stop; no profit target; open positions liquidated at the development-window boundary.",
            "fees": "5 bps slippage per side; no commissions or market-impact model.",
            "baseline": "Same point-in-time universe, filters, next-bar execution, stop, time exit, costs, and five-position cap, ranked by the existing trailing 50-bar momentum score.",
        },
        "coverage_note": "Only cached symbols are available; historical scanner/social/catalyst signals and full-universe membership are not reconstructed.",
        "results": {key: value["metrics"] for key, value in results.items()},
        "trades": {key: value["trades"] for key, value in results.items()},
    }
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")

    rows = [
        "# Outside-in 600-cell market-field replay — exploratory only",
        "",
        f"Data: {data_meta['symbols']} cached names, {dev_sessions}/{full_sessions} sessions evaluated, {report['window']['start']} to {report['window']['end']}.",
        "",
        "The previously examined final-quarter holdout was not evaluated. This development interval has been used by prior screens, so these results are exploratory, not independent validation.",
        "",
        "Four data-defined scales are projected onto a fixed 120-direction 600-cell. A single 65/35 neighbor propagation feeds the center score. BUY candidates require a positive center and at least two positive horizon features. No parameter sweep or take-profit target was used.",
        "",
        f"Center algebra: the propagated mesh coefficients are {', '.join(f'{x:.6f}' for x in propagated_coefficients)} and the unpropagated coefficients are {', '.join(f'{x:.6f}' for x in raw_mesh_coefficients)}. Each mesh score is a positive scalar multiple of the plain equal-weight horizon mean, so all three conviction-sized versions produce the same trades. In this setup, the 600-cell and graph propagation add no decision information.",
        "",
        "| Strategy | Return | Max DD | Sharpe | Trades | Win rate | Avg trade | Median hold | Stops | Costs | Turnover |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, metrics in report["results"].items():
        rows.append(
            f"| {name} | {metrics['return_pct']:+.2f}% | {metrics['max_drawdown_pct']:.2f}% | "
            f"{metrics['sharpe']} | {metrics['trades']} | {metrics['win_rate_pct']}% | "
            f"{metrics['avg_trade_pct']}% | {metrics['median_hold_days']}d | "
            f"{metrics['stop_exits']} | ${metrics['slippage_cost_usd']:.0f} | {metrics['turnover']}x |"
        )
    rows.extend([
        "",
        "Dynamic size by positive horizon votes: 2/4 = 5% of equity; 3/4 = 10%; 4/4 = 15%, subject to the 15% position and 95% sleeve caps.",
        "",
        "This replay is not an independent out-of-sample validation and does not establish an edge. Cached coverage is incomplete; scanner boosts, commissions, spread variation, and market impact are absent.",
        "The center score is a heuristic ranking statistic, not a calibrated expected-return forecast or proof that the center maximizes profit.",
    ])
    OUT_MD.write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"Saved {OUT_MD}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
