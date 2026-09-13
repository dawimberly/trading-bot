"""
Phase 1: Sneaky Pivot v2

Structure (levels, C1/C2 identity, stop/target geometry) computed on 15m bars.
Execution (trigger fill, TOD attribution, premature-stop detection) simulated
on 1m bars. This is the fix for the coarse 90d 15m-only run.

Research-only. Not wired to live/paper.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal, Optional

import pandas as pd

from harness import BarCache, tod_bucket, DEFAULT_FEE_MODEL, FeeModel


Side = Literal["long", "short"]


# --------------------------------------------------------------------------
# Levels (15m structure)
# --------------------------------------------------------------------------

@dataclass
class Levels:
    range_high: float
    range_low: float
    swing_high: float
    swing_low: float


def compute_levels(bars_daily: pd.DataFrame, session_date: dt.date) -> Optional[Levels]:
    """
    range_high/low = prior day's high/low.
    swing_high = next prior day whose high exceeds range_high.
    swing_low  = next prior day whose low is below range_low.
    """
    prior = bars_daily[bars_daily.index.date < session_date]
    if len(prior) < 2:
        return None

    range_high = prior.iloc[-1]["high"]
    range_low = prior.iloc[-1]["low"]

    swing_high = None
    for h in prior.iloc[-2::-1]["high"]:
        if h > range_high:
            swing_high = h
            break

    swing_low = None
    for l in prior.iloc[-2::-1]["low"]:
        if l < range_low:
            swing_low = l
            break

    if swing_high is None or swing_low is None:
        return None

    return Levels(range_high=range_high, range_low=range_low,
                  swing_high=swing_high, swing_low=swing_low)


# --------------------------------------------------------------------------
# 15m signal detection (C1 touch + C2 confirmation)
# --------------------------------------------------------------------------

@dataclass
class PendingSetup:
    side: Side
    trigger_level: float          # C2's high (long) / low (short) to break
    stop: float
    target: float
    confirm_bar_ts: pd.Timestamp  # timestamp of the confirming (sneaky) candle
    confirm_window_end_ts: pd.Timestamp  # last 15m bar we'll allow a breakout by


def find_setup_15m(day_bars_15m: pd.DataFrame, levels: Levels,
                    confirm_window: int = 3,
                    breakout_window_bars: int = 8) -> Optional[PendingSetup]:
    """
    day_bars_15m: single session's 15m bars, in order, DatetimeIndex.
    Generalized "sneaky candle": any confirming reversal candle within
    confirm_window bars of the initial touch, not strictly candle #2.
    Set confirm_window=1 to force the strict "candle 2 only" reading.
    """
    if len(day_bars_15m) < 2:
        return None

    c1 = day_bars_15m.iloc[0]
    touched_low = c1["low"] <= levels.range_low
    touched_high = c1["high"] >= levels.range_high
    if not touched_low and not touched_high:
        return None

    side: Side = "long" if touched_low else "short"

    n = len(day_bars_15m)
    for i in range(1, min(confirm_window + 1, n)):
        candle = day_bars_15m.iloc[i]
        is_green = candle["close"] > candle["open"]
        is_red = candle["close"] < candle["open"]
        confirmed = (side == "long" and is_green) or (side == "short" and is_red)
        if not confirmed:
            continue

        trigger_level = candle["high"] if side == "long" else candle["low"]
        confirm_window_end_idx = min(i + breakout_window_bars, n - 1)

        return PendingSetup(
            side=side,
            trigger_level=trigger_level,
            stop=levels.swing_low if side == "long" else levels.swing_high,
            target=levels.range_high if side == "long" else levels.range_low,
            confirm_bar_ts=day_bars_15m.index[i],
            confirm_window_end_ts=day_bars_15m.index[confirm_window_end_idx],
        )

    return None


# --------------------------------------------------------------------------
# 1m fill simulation
# --------------------------------------------------------------------------

@dataclass
class Trade:
    symbol: str
    session_date: dt.date
    side: Side
    entry_ts: pd.Timestamp
    entry_price: float
    stop: float
    target: float
    exit_ts: Optional[pd.Timestamp]
    exit_price: Optional[float]
    exit_reason: Literal["target", "stop", "eod", "no_fill"]
    minutes_since_open_at_entry: int
    tod_bucket_at_entry: str
    r_multiple: Optional[float]
    pnl_bps: Optional[float]


def simulate_fill_1m(setup: PendingSetup, bars_1m_session: pd.DataFrame,
                      session_open_ts: pd.Timestamp) -> Optional[tuple[pd.Timestamp, float]]:
    """
    Scan 1m bars from just after the confirm candle through the breakout
    window, return (entry_ts, entry_price) on the first 1m bar that trades
    through the trigger level. None if never triggered in-window.
    """
    window = bars_1m_session[
        (bars_1m_session.index > setup.confirm_bar_ts) &
        (bars_1m_session.index <= setup.confirm_window_end_ts)
    ]
    for ts, bar in window.iterrows():
        if setup.side == "long" and bar["high"] > setup.trigger_level:
            return ts, max(setup.trigger_level, bar["open"])
        if setup.side == "short" and bar["low"] < setup.trigger_level:
            return ts, min(setup.trigger_level, bar["open"])
    return None


def simulate_trade_1m(setup: PendingSetup, symbol: str, session_date: dt.date,
                       bars_1m_session: pd.DataFrame, session_open_ts: pd.Timestamp,
                       session_close_ts: pd.Timestamp,
                       fees: FeeModel = DEFAULT_FEE_MODEL,
                       asset_class: Literal["equity", "crypto"] = "equity") -> Optional[Trade]:
    fill = simulate_fill_1m(setup, bars_1m_session, session_open_ts)
    if fill is None:
        return None
    entry_ts, entry_price = fill

    minutes_since_open = int((entry_ts - session_open_ts).total_seconds() // 60)
    bucket = tod_bucket(minutes_since_open)

    # walk forward on 1m bars from entry to find stop/target/EOD
    post_entry = bars_1m_session[bars_1m_session.index > entry_ts]
    exit_ts, exit_price, exit_reason = None, None, "eod"

    for ts, bar in post_entry.iterrows():
        if setup.side == "long":
            hit_stop = bar["low"] <= setup.stop
            hit_target = bar["high"] >= setup.target
        else:
            hit_stop = bar["high"] >= setup.stop
            hit_target = bar["low"] <= setup.target

        # conservative: if both hit in same 1m bar, assume stop first
        if hit_stop:
            exit_ts, exit_price, exit_reason = ts, setup.stop, "stop"
            break
        if hit_target:
            exit_ts, exit_price, exit_reason = ts, setup.target, "target"
            break

    if exit_ts is None:
        # no stop/target hit -- close at end of session
        last_bar = bars_1m_session[bars_1m_session.index <= session_close_ts].iloc[-1]
        exit_ts, exit_price, exit_reason = last_bar.name, last_bar["close"], "eod"

    cost_bps = fees.round_trip_cost_bps(asset_class)
    if setup.side == "long":
        raw_pnl_bps = (exit_price - entry_price) / entry_price * 1e4
        risk = entry_price - setup.stop
        r_multiple = (exit_price - entry_price) / risk if risk > 0 else None
    else:
        raw_pnl_bps = (entry_price - exit_price) / entry_price * 1e4
        risk = setup.stop - entry_price
        r_multiple = (entry_price - exit_price) / risk if risk > 0 else None

    pnl_bps = raw_pnl_bps - cost_bps

    return Trade(
        symbol=symbol, session_date=session_date, side=setup.side,
        entry_ts=entry_ts, entry_price=entry_price,
        stop=setup.stop, target=setup.target,
        exit_ts=exit_ts, exit_price=exit_price, exit_reason=exit_reason,
        minutes_since_open_at_entry=minutes_since_open,
        tod_bucket_at_entry=bucket,
        r_multiple=r_multiple, pnl_bps=pnl_bps,
    )


# --------------------------------------------------------------------------
# Full run over a universe
# --------------------------------------------------------------------------

def run_sneaky_pivot(cache: BarCache, symbols: list[str],
                      asset_class: Literal["equity", "crypto"] = "equity",
                      confirm_window: int = 3,
                      breakout_window_bars: int = 8,
                      fees: FeeModel = DEFAULT_FEE_MODEL) -> pd.DataFrame:
    """
    Returns a DataFrame of Trade rows across the whole universe/date range
    currently loaded in `cache`. Feed this into rhyme/TOD reporting.
    """
    trades: list[Trade] = []

    for sym in symbols:
        daily = cache.bars_daily[sym]
        bars_15m = cache.bars_15m[sym]
        bars_1m = cache.bars_1m[sym]

        for session_date in cache.sessions(sym):
            day_15m = bars_15m[bars_15m.index.date == session_date]
            if day_15m.empty:
                continue

            levels = compute_levels(daily, session_date)
            if levels is None:
                continue

            setup = find_setup_15m(day_15m, levels, confirm_window=confirm_window,
                                    breakout_window_bars=breakout_window_bars)
            if setup is None:
                continue

            day_1m = bars_1m[bars_1m.index.date == session_date]
            if day_1m.empty:
                continue

            session_open_ts = day_1m.index[0]
            session_close_ts = day_1m.index[-1]

            trade = simulate_trade_1m(
                setup, sym, session_date, day_1m,
                session_open_ts, session_close_ts,
                fees=fees, asset_class=asset_class,
            )
            if trade is not None:
                trades.append(trade)

    if not trades:
        return pd.DataFrame()

    return pd.DataFrame([t.__dict__ for t in trades])
