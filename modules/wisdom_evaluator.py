"""Rolling self-evaluation of wisdom modes (live journal + simulated sleeves)."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

import config
from backtester import MIN_HISTORY, WARMUP_CALENDAR_BUFFER, _ensure_daily_data
from backtester_wisdom import run_fund_backtest
from modules.data_loader import load_close_matrix
from modules.wayback_sentiment import load_monthly_web_sentiment
from modules.wisdom_journal import load_journal
from modules.wisdom_sentiment import MODES


def _scorecard_path() -> str:
    return getattr(config, "WISDOM_SCORECARD_FILE", "wisdom_scorecard.json")


def _eval_history_path() -> str:
    return getattr(config, "WISDOM_EVAL_HISTORY_FILE", "wisdom_evaluations.jsonl")


def _state_path() -> str:
    return getattr(config, "WISDOM_EVAL_STATE_FILE", "wisdom_eval_state.json")


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    with open(_state_path(), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _metrics_from_equity(series: pd.Series) -> dict:
    if len(series) < 2:
        return {
            "return_pct": 0.0,
            "sharpe": 0.0,
            "max_drawdown_pct": 0.0,
            "samples": len(series),
        }
    ret = series.pct_change().dropna()
    total = (series.iloc[-1] / series.iloc[0] - 1) * 100
    sharpe = (
        (ret.mean() / ret.std()) * np.sqrt(252) if ret.std() > 0 else 0.0
    )
    dd = ((series / series.cummax()) - 1).min() * 100
    return {
        "return_pct": round(float(total), 2),
        "sharpe": round(float(sharpe), 2),
        "max_drawdown_pct": round(float(dd), 2),
        "samples": int(len(series)),
    }


def _daily_equity_from_journal(df: pd.DataFrame) -> pd.Series:
    """Last equity per calendar day — matches daily sim bar cadence."""
    return df.groupby(df["timestamp"].dt.date)["equity"].last().astype(float)


def live_metrics(window_days: int) -> dict | None:
    df = load_journal()
    if df.empty:
        return None
    cutoff = datetime.now() - timedelta(days=window_days)
    df = df.loc[df["timestamp"] >= cutoff].copy()
    if df.empty:
        return None

    daily = _daily_equity_from_journal(df)
    active_mode = df["active_mode"].iloc[-1]
    pause_cycles = int(
        df["wisdom_paused"].astype(str).str.lower().isin(("true", "1", "yes")).sum()
    )
    metrics = _metrics_from_equity(daily)
    metrics.update(
        {
            "mode": active_mode,
            "gap_threshold": float(df["gap_threshold"].iloc[-1]),
            "equity_basis": "daily_last",
            "daily_samples": int(len(daily)),
            "intraday_cycles": int(len(df)),
            "cycles": int(len(df)),
            "pause_cycles": pause_cycles,
            "window_days": window_days,
            "from_date": str(daily.index.min()),
            "to_date": str(daily.index.max()),
            "start_equity": round(float(daily.iloc[0]), 2),
            "end_equity": round(float(daily.iloc[-1]), 2),
        }
    )
    return metrics


def _slice_backtest_data(
    data: pd.DataFrame, period_start: date, period_end: date
) -> pd.DataFrame:
    """Warmup through period_end so sim equity covers the live journal window."""
    warmup = period_start - timedelta(days=MIN_HISTORY + WARMUP_CALENDAR_BUFFER)
    warmup_ts = pd.Timestamp(warmup)
    end_ts = pd.Timestamp(period_end)
    if len(data) and data.index.tz is not None:
        warmup_ts = warmup_ts.tz_localize(data.index.tz)
        end_ts = end_ts.tz_localize(data.index.tz)
    return data.loc[(data.index >= warmup_ts) & (data.index <= end_ts)]


def _prepare_daily_backtest_data(
    period_start: date, period_end: date
) -> tuple[pd.DataFrame, date, date] | None:
    """Load daily bars, refresh if stale, slice warmup window, clamp period_end."""
    data = load_close_matrix(interval="1d")
    if not len(data) or data.index.max().date() < period_end:
        span = (period_end - period_start).days + MIN_HISTORY + 60
        data = _ensure_daily_data(span, refresh=True)

    data = _slice_backtest_data(data, period_start, period_end)
    if len(data) < MIN_HISTORY + 5:
        return None

    period_end = min(period_end, data.index[-1].date())
    return data, period_start, period_end


def simulate_modes(
    window_days: int,
    period_start: date | None = None,
    period_end: date | None = None,
) -> dict[str, dict]:
    if period_end is None:
        period_end = date.today()
    if period_start is None:
        period_start = period_end - timedelta(days=window_days)

    prep = _prepare_daily_backtest_data(period_start, period_end)
    if prep is None:
        return {}
    data, period_start, period_end = prep

    monthly_web = load_monthly_web_sentiment()
    results = {}
    for mode in MODES:
        try:
            row = run_fund_backtest(
                data,
                monthly_web,
                mode,
                gap_threshold=config.WISDOM_GAP_THRESHOLD,
            )
            aligned = _period_metrics_from_backtest_row(row, period_start, period_end)
            if "return_pct" in aligned:
                results[mode] = aligned
                if row.get("game_plan"):
                    results[mode]["game_plan"] = True
                    results[mode]["yield_gate_days"] = row.get("yield_gate_days", 0)
                    results[mode]["cash_trims"] = row.get("cash_trims", 0)
                    results[mode]["metal_trades"] = row.get("metal_trades", 0)
            else:
                results[mode] = {
                    "error": aligned.get("error", "alignment failed"),
                    "backtest_from": str(row["start"]),
                    "backtest_to": str(row["end"]),
                }
        except Exception as exc:
            results[mode] = {"error": str(exc)}
    return results


def live_metrics_for_month(year: int, month: int) -> dict | None:
    df = load_journal()
    if df.empty:
        return None
    start, end = _month_bounds(year, month)

    mask = (df["timestamp"].dt.date >= start) & (df["timestamp"].dt.date <= end)
    df = df.loc[mask]
    if df.empty:
        return None

    daily = _daily_equity_from_journal(df)
    pause_cycles = int(
        df["wisdom_paused"].astype(str).str.lower().isin(("true", "1", "yes")).sum()
    )
    metrics = _metrics_from_equity(daily)
    metrics.update(
        {
            "mode": df["active_mode"].iloc[-1],
            "gap_threshold": float(df["gap_threshold"].iloc[-1]),
            "equity_basis": "daily_last",
            "daily_samples": int(len(daily)),
            "intraday_cycles": int(len(df)),
            "cycles": int(len(df)),
            "pause_cycles": pause_cycles,
            "month": f"{year:04d}-{month:02d}",
            "from_date": str(start),
            "to_date": str(end),
            "start_equity": round(float(daily.iloc[0]), 2),
            "end_equity": round(float(daily.iloc[-1]), 2),
        }
    )
    return metrics


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return start, end


def _period_metrics_from_backtest_row(row: dict, period_start: date, period_end: date) -> dict:
    idx = pd.to_datetime(row.get("equity_index", []))
    vals = row.get("equity_values", [])
    if len(idx) < 2 or len(vals) < 2:
        return {"error": "insufficient equity curve"}
    curve = pd.Series(vals, index=idx)
    if curve.index.tz is not None:
        curve.index = curve.index.tz_localize(None)
    pstart = pd.Timestamp(period_start)
    pend = pd.Timestamp(period_end)
    sub = curve.loc[(curve.index >= pstart) & (curve.index <= pend)]
    if len(sub) < 2:
        return {
            "error": "no bars in alignment window",
            "curve_from": str(curve.index.min().date()),
            "curve_to": str(curve.index.max().date()),
        }
    metrics = _metrics_from_equity(sub.astype(float))
    metrics.update(
        {
            "orders": row.get("orders", 0),
            "paused_days": row.get("paused_days", 0),
            "from_date": str(period_start),
            "to_date": str(period_end),
        }
    )
    return metrics


def simulate_modes_for_month(year: int, month: int) -> dict[str, dict]:
    period_start, period_end = _month_bounds(year, month)
    prep = _prepare_daily_backtest_data(period_start, period_end)
    if prep is None:
        return {}
    data, period_start, period_end = prep

    monthly_web = load_monthly_web_sentiment()
    results = {}
    for mode in MODES:
        try:
            row = run_fund_backtest(
                data,
                monthly_web,
                mode,
                gap_threshold=config.WISDOM_GAP_THRESHOLD,
            )
            results[mode] = _period_metrics_from_backtest_row(row, period_start, period_end)
        except Exception as exc:
            results[mode] = {"error": str(exc)}
    return results


def _previous_month() -> tuple[str, int, int]:
    today = date.today()
    first = today.replace(day=1)
    last = first - timedelta(days=1)
    return last.strftime("%Y-%m"), last.year, last.month


def _monthly_file_path(month_key: str) -> str:
    return f"wisdom_monthly_{month_key}.json"


def run_monthly_rollup(year: int, month: int, force: bool = False) -> dict | None:
    if not config.WISDOM_MONTHLY_ENABLED:
        return None

    month_key = f"{year:04d}-{month:02d}"
    state = _load_state()
    if not force and state.get("last_monthly_rollup") == month_key:
        return None

    live = live_metrics_for_month(year, month)
    simulated = simulate_modes_for_month(year, month)
    valid = {k: v for k, v in simulated.items() if "return_pct" in v}
    best_sim = max(valid.items(), key=lambda x: x[1]["return_pct"])[0] if valid else None

    rollup = {
        "rolled_up_at": datetime.now().isoformat(timespec="seconds"),
        "month": month_key,
        "config": {
            "wisdom_mode": config.WISDOM_MODE,
            "gap_threshold": config.WISDOM_GAP_THRESHOLD,
        },
        "live": live,
        "simulated_modes": simulated,
        "best_sim_mode": best_sim,
        "recommendation": _recommendation(live, simulated),
    }
    if live and best_sim and best_sim in valid:
        rollup["live_vs_best_sim_return_pp"] = round(
            live.get("return_pct", 0) - valid[best_sim]["return_pct"], 2
        )

    path = _monthly_file_path(month_key)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rollup, f, indent=2)

    history = getattr(config, "WISDOM_MONTHLY_HISTORY_FILE", "wisdom_monthly_history.jsonl")
    with open(history, "a", encoding="utf-8") as f:
        f.write(json.dumps(rollup) + "\n")

    state["last_monthly_rollup"] = month_key
    state["last_monthly_rollup_at"] = rollup["rolled_up_at"]
    _save_state(state)
    return rollup


def maybe_run_monthly_rollup(force: bool = False) -> dict | None:
    """Roll up previous calendar month once; catch-up if bot was offline on the 1st."""
    if not config.WISDOM_MONTHLY_ENABLED:
        return None
    try:
        month_key, year, month = _previous_month()
        state = _load_state()
        if not force and state.get("last_monthly_rollup") == month_key:
            return None
        return run_monthly_rollup(year, month, force=force)
    except Exception as exc:
        print(f"Wisdom monthly rollup error (non-fatal): {exc}")
        return None


def _recommendation(live: dict | None, simulated: dict[str, dict]) -> str:
    valid = {
        k: v
        for k, v in simulated.items()
        if "return_pct" in v and "error" not in v
    }
    if not valid:
        return "Collect more journal data; simulation unavailable."
    best = max(valid.items(), key=lambda x: x[1]["return_pct"])
    if live is None:
        return (
            f"No live equity journal yet. Best {config.WISDOM_EVAL_DAYS}d sim: "
            f"{best[0]} ({best[1]['return_pct']:+.1f}%)."
        )
    live_mode = live.get("mode", config.WISDOM_MODE)
    live_ret = live.get("return_pct", 0.0)
    best_ret = best[1]["return_pct"]
    if live_mode == best[0]:
        return f"Stay on {live_mode} — matches best rolling sim ({live_ret:+.1f}% live)."
    gap = live_ret - best_ret
    if abs(gap) <= 2.0:
        return (
            f"Stay on {live_mode} — within 2% of best sim mode {best[0]} "
            f"({live_ret:+.1f}% vs {best_ret:+.1f}%)."
        )
    return (
        f"Consider reviewing {best[0]} — sim lead {best_ret - live_ret:.1f}pp over "
        f"{config.WISDOM_EVAL_DAYS}d (live {live_mode} {live_ret:+.1f}%). "
        f"Change WISDOM_MODE in .env manually; bot does not auto-switch."
    )


def run_evaluation(force: bool = False) -> dict | None:
    if not config.WISDOM_EVAL_ENABLED:
        return None

    today = date.today().isoformat()
    state = _load_state()
    if not force and state.get("last_eval_date") == today:
        return None

    window = config.WISDOM_EVAL_DAYS
    live = live_metrics(window)
    period_start = period_end = None
    if live:
        period_start = date.fromisoformat(str(live["from_date"]))
        period_end = date.fromisoformat(str(live["to_date"]))
    simulated = simulate_modes(window, period_start=period_start, period_end=period_end)
    best_sim = None
    valid = {k: v for k, v in simulated.items() if "return_pct" in v}
    if valid:
        best_sim = max(valid.items(), key=lambda x: x[1]["return_pct"])[0]

    scorecard = {
        "evaluated_at": datetime.now().isoformat(timespec="seconds"),
        "window_days": window,
        "config": {
            "wisdom_mode": config.WISDOM_MODE,
            "gap_threshold": config.WISDOM_GAP_THRESHOLD,
            "web_cache_hours": config.WEB_SENTIMENT_CACHE_HOURS,
            "game_plan_enabled": config.GAME_PLAN_ENABLED,
            "live_equity_basis": "daily_last",
        },
        "live": live,
        "simulated_modes": simulated,
        "best_sim_mode": best_sim,
        "recommendation": _recommendation(live, simulated),
    }
    if live and best_sim and best_sim in valid:
        scorecard["live_vs_best_sim_return_pp"] = round(
            live.get("return_pct", 0) - valid[best_sim]["return_pct"], 2
        )
    active_mode = (live or {}).get("mode", config.WISDOM_MODE)
    if live and active_mode in valid:
        scorecard["live_vs_active_sim_return_pp"] = round(
            live.get("return_pct", 0) - valid[active_mode]["return_pct"], 2
        )

    with open(_scorecard_path(), "w", encoding="utf-8") as f:
        json.dump(scorecard, f, indent=2)

    with open(_eval_history_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(scorecard) + "\n")

    state = _load_state()
    state["last_eval_date"] = today
    state["last_eval_at"] = scorecard["evaluated_at"]
    _save_state(state)
    return scorecard


def maybe_run_daily_evaluation(force: bool = False) -> dict | None:
    """Call once per bot cycle; runs at most one eval per calendar day."""
    try:
        return run_evaluation(force=force)
    except Exception as exc:
        print(f"Wisdom eval error (non-fatal): {exc}")
        return None
