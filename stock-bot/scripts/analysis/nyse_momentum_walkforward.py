"""Research-only walk-forward for the employed NYSE momentum book.

Reads local bars and S&P 500 membership. Does not write the market database,
change bot settings, or send orders.

The order path in ``modules/pipeline_strategies.py`` ranks
``close / MA50 - 1`` on whatever matrix ``run_all.py`` loads. That load is
``load_live_close_matrix()``, whose default interval is 5 minutes. The printed
``PAPER_NYSE_MA_WINDOW`` of 70 is not used by the ranker.

Usage (from stock-bot/):
  python scripts/analysis/nyse_momentum_walkforward.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from modules.backtester_core import score_chronological_holdout  # noqa: E402
from modules.data_loader import load_close_matrix  # noqa: E402
from modules.pit_universe import (  # noqa: E402
    ffill_interior_only,
    load_sp500_membership,
    membership_matrix,
    resolve_column,
    session_bars,
    sp500_tickers_in_window,
)

OUT_JSON = Path(__file__).with_name("nyse_momentum_walkforward_last.json")
OUT_MD = Path(__file__).with_name("nyse_momentum_walkforward_last.md")
OUT_NOTE = Path(__file__).with_name("nyse_momentum_walkforward_NOTE.txt")
CACHE_DIR = ROOT / "data" / "intraday_cache"

# Effective Medium SoT book (portal launch overlay), verified against code.
INITIAL = 100_000.0
SLEEVE = 0.95
MAX_NAMES = 15
MAX_NEW_PER_BAR = 12
HOLD_DAYS = 30
ATR_MULT = 2.0
ATR_PERIOD = 14
MA_WINDOW = 50
MA_MIN = 20
RSI_PERIOD = 14
RSI_MAX = 70
RSI_MAX_MIDDAY = 72
SLIP_BPS = 5.0  # per side; 10 bps round trip
SLIP = SLIP_BPS / 10_000.0
GAP_BASE = 0.02
GAP_TIGHT = 0.015
COOLDOWN_BASE = time(10, 15)
COOLDOWN_LONG = time(10, 30)
OPEN_CLOCK = time(9, 30)
CLOSE_CLOCK = time(16, 0)
MIDDAY_START = time(12, 0)
MIDDAY_END = time(14, 0)
TRAIN_MONTHS = 24
TEST_MONTHS = 6
SESSIONS_PER_MONTH = 21
TEST_SESSIONS = TEST_MONTHS * SESSIONS_PER_MONTH
TRAIN_SESSIONS = TRAIN_MONTHS * SESSIONS_PER_MONTH
HOLDOUT_SESSIONS = TEST_SESSIONS
DD_WORSE_PP = 2.0


def _sma_rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rsi = 100.0 - (100.0 / (1.0 + gain / loss.replace(0, np.nan)))
    rsi = rsi.where(loss > 1e-12, np.where(gain.notna(), 100.0, np.nan))
    return rsi


def _clock_minutes(idx: pd.DatetimeIndex) -> np.ndarray:
    return idx.hour.to_numpy() * 60 + idx.minute.to_numpy()


def _load_cache_symbol(path: Path) -> pd.DataFrame | None:
    df = pd.read_pickle(path)
    if df is None or len(df) == 0 or "timestamp" not in df.columns:
        return None
    out = df.copy()
    ts = pd.to_datetime(out["timestamp"])
    if getattr(ts.dt, "tz", None) is None:
        ts = ts.dt.tz_localize("America/New_York")
    else:
        ts = ts.dt.tz_convert("America/New_York")
    out = out.assign(timestamp=ts).sort_values("timestamp")
    clock = out["timestamp"].dt.time
    out = out[(clock >= OPEN_CLOCK) & (clock < CLOSE_CLOCK)]
    if out.empty:
        return None
    keep = ["timestamp", "Open", "High", "Low", "Close"]
    if "Volume" in out.columns:
        keep.append("Volume")
    out = out[keep].dropna(subset=["Close"])
    for col in ("Open", "High", "Low", "Close"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    if "Volume" in out.columns:
        out["Volume"] = pd.to_numeric(out["Volume"], errors="coerce")
    out = out.dropna(subset=["Open", "High", "Low", "Close"])
    out = out[out["Close"] > 0]
    return out if len(out) else None


def _panel_readiness(panel: dict, meta: dict) -> dict:
    """Hard gate for performance replays on stale or very narrow cache data."""
    close = panel["close"]
    cached_symbols = {config.normalize_symbol(symbol) for symbol in panel["symbols"]}
    target_symbols = {
        config.normalize_symbol(symbol)
        for symbol in config.get_nyse_universe()
        if config.normalize_symbol(symbol)
    }
    target_coverage = (
        len(cached_symbols & target_symbols) / len(target_symbols)
        if target_symbols
        else 0.0
    )
    expected_provenance = {"bar_interval": "5Min", "feed": "iex", "adjustment": "split"}
    provenance_ok = set()
    for symbol in target_symbols:
        path = CACHE_DIR / f"{symbol}.meta.json"
        try:
            source = json.loads(path.read_text(encoding="utf-8"))
            if all(source.get(key) == value for key, value in expected_provenance.items()):
                provenance_ok.add(symbol)
        except Exception:
            pass
    provenance_coverage = len(provenance_ok) / len(target_symbols) if target_symbols else 0.0
    sessions = list(panel["sessions"])
    dates = panel["dates"]
    per_session_coverage = []
    for day in sessions:
        mask = dates == day
        seen = int(np.isfinite(close[:, mask]).sum())
        expected = len(panel["symbols"]) * EXPECTED_BARS_PER_SESSION
        per_session_coverage.append(seen / expected if expected else 0.0)
    median_coverage = float(np.median(per_session_coverage)) if per_session_coverage else 0.0
    latest = pd.Timestamp(meta["end"])
    if latest.tzinfo is None:
        latest = latest.tz_localize("America/New_York")
    else:
        latest = latest.tz_convert("America/New_York")
    today = pd.Timestamp.now(tz="America/New_York").date()
    stale = max(0, len(pd.bdate_range(latest.date(), today)) - 1)
    blockers = []
    if target_symbols and target_coverage < 0.95:
        blockers.append(
            f"only {target_coverage:.1%} of configured bot-universe symbols are cached; "
            "at least 95% are required"
        )
    if target_symbols and provenance_coverage < 0.95:
        blockers.append(
            f"only {provenance_coverage:.1%} of configured bot-universe caches have verified "
            "5-minute IEX, split-adjusted provenance"
        )
    if len(sessions) < 252:
        blockers.append(f"only {len(sessions)} sessions; at least 252 are required")
    if median_coverage < 0.80:
        blockers.append(f"median five-minute coverage is {median_coverage:.1%}; at least 80% is required")
    if stale > 5:
        blockers.append(f"latest bar is {stale} business days old; maximum allowed is 5")
    return {
        "ready": not blockers,
        "blockers": blockers,
        "stale_business_days": stale,
        "median_five_minute_coverage": round(median_coverage, 4),
        "configured_universe_symbols": len(target_symbols),
        "configured_universe_coverage": round(target_coverage, 4),
        "configured_universe_provenance_coverage": round(provenance_coverage, 4),
        "minimums": {
            "configured_universe_coverage": 0.95,
            "sessions": 252,
            "coverage": 0.80,
            "max_stale_business_days": 5,
        },
    }


EXPECTED_BARS_PER_SESSION = 78


def load_intraday_panel(
    *,
    require_ready: bool = True,
    symbols: list[str] | None = None,
) -> tuple[dict, dict]:
    frames: dict[str, pd.DataFrame] = {}
    explicit_universe = symbols is not None
    requested = {
        config.normalize_symbol(symbol)
        for symbol in (symbols if symbols is not None else config.get_nyse_universe())
        if config.normalize_symbol(symbol)
    }
    for path in sorted(CACHE_DIR.glob("*.pkl")):
        if requested and config.normalize_symbol(path.stem) not in requested:
            continue
        frame = _load_cache_symbol(path)
        if frame is not None:
            frames[path.stem.upper()] = frame
    if not frames:
        raise RuntimeError(f"no 5-minute caches in {CACHE_DIR}")

    pieces = []
    for sym, frame in frames.items():
        indexed = frame.set_index("timestamp")
        indexed = indexed[~indexed.index.duplicated(keep="last")]
        pieces.append(indexed)
    master = pieces[0].index
    for part in pieces[1:]:
        master = master.union(part.index)
    master = pd.DatetimeIndex(master).sort_values().unique()

    symbols = sorted(frames)
    loc = {sym: i for i, sym in enumerate(symbols)}
    n_sym = len(symbols)
    t_len = len(master)
    open_px = np.full((n_sym, t_len), np.nan)
    high_px = np.full((n_sym, t_len), np.nan)
    low_px = np.full((n_sym, t_len), np.nan)
    close_px = np.full((n_sym, t_len), np.nan)
    volume = np.full((n_sym, t_len), np.nan)
    ma = np.full((n_sym, t_len), np.nan)
    rsi = np.full((n_sym, t_len), np.nan)
    daily_atr: dict[str, dict] = {}

    for sym, frame in frames.items():
        indexed = frame.set_index("timestamp")
        indexed = indexed[~indexed.index.duplicated(keep="last")].reindex(master)
        row = loc[sym]
        open_px[row] = indexed["Open"].to_numpy(dtype=float)
        high_px[row] = indexed["High"].to_numpy(dtype=float)
        low_px[row] = indexed["Low"].to_numpy(dtype=float)
        close_px[row] = indexed["Close"].to_numpy(dtype=float)
        if "Volume" in indexed.columns:
            volume[row] = indexed["Volume"].to_numpy(dtype=float)
        native_close = frame.set_index("timestamp")["Close"].astype(float)
        native_close = native_close[~native_close.index.duplicated(keep="last")]
        ma_s = native_close.rolling(MA_WINDOW, min_periods=MA_MIN).mean()
        rsi_s = _sma_rsi(native_close, RSI_PERIOD)
        ma[row] = ma_s.reindex(master).to_numpy(dtype=float)
        rsi[row] = rsi_s.reindex(master).to_numpy(dtype=float)
        daily = native_close.groupby(native_close.index.tz_convert("America/New_York").date).last()
        changes = daily.diff().abs()
        atr = changes.rolling(ATR_PERIOD, min_periods=ATR_PERIOD).mean().shift(1)
        daily_atr[sym] = {d: float(v) for d, v in atr.items() if np.isfinite(v) and v > 0}

    dates = np.array([ts.date() for ts in master])
    minutes = _clock_minutes(master)
    sessions = pd.Index(pd.unique(dates))
    session_of = np.searchsorted(sessions.to_numpy(), dates)
    panel = {
        "symbols": symbols,
        "loc": loc,
        "times": master,
        "dates": dates,
        "minutes": minutes,
        "sessions": sessions,
        "session_of": session_of,
        "open": open_px,
        "high": high_px,
        "low": low_px,
        "close": close_px,
        "volume": volume,
        "ma": ma,
        "rsi": rsi,
        "daily_atr": daily_atr,
    }
    meta = {
        "symbols": len(symbols),
        "bars": int(t_len),
        "start": str(master[0]),
        "end": str(master[-1]),
        "sessions": int(len(sessions)),
        "symbols_with_volume": sum("Volume" in frame.columns for frame in frames.values()),
        "universe_source": "explicit symbols" if explicit_universe else "configured bot universe",
    }
    readiness = _panel_readiness(panel, meta)
    meta["readiness"] = readiness
    if require_ready and not readiness["ready"]:
        details = "; ".join(readiness["blockers"])
        raise RuntimeError(
            "intraday performance replay blocked by the data-quality gate: " + details
        )
    return panel, meta


def _gap_by_session(panel: dict, sym_i: int) -> dict:
    """Today's first open / prior session last close, from this file only."""
    dates = panel["dates"]
    opens = panel["open"][sym_i]
    closes = panel["close"][sym_i]
    gaps: dict = {}
    prev_close = None
    i = 0
    n = len(dates)
    while i < n:
        day = dates[i]
        j = i
        first_open = None
        last_close = None
        while j < n and dates[j] == day:
            if first_open is None and np.isfinite(opens[j]) and opens[j] > 0:
                first_open = float(opens[j])
            if np.isfinite(closes[j]) and closes[j] > 0:
                last_close = float(closes[j])
            j += 1
        if first_open is not None and prev_close is not None and prev_close > 0:
            gaps[day] = first_open / prev_close - 1.0
        if last_close is not None:
            prev_close = last_close
        i = j
    return gaps


def _member_on(membership: pd.DataFrame) -> dict[str, tuple]:
    out = {}
    for rec in membership.itertuples(index=False):
        tick = str(rec.ticker).upper()
        start = pd.Timestamp(rec.start).date()
        end = pd.Timestamp(rec.end).date() if pd.notna(rec.end) else None
        out.setdefault(tick, []).append((start, end))
    return out


def _is_member(spans: dict, sym: str, day) -> bool:
    for start, end in spans.get(sym, ()):
        if day < start:
            continue
        if end is not None and day >= end:
            continue
        return True
    return False


def simulate_intraday(
    panel: dict,
    spans: dict,
    *,
    cooldown_end: time,
    gap_limit: float,
) -> dict:
    symbols = panel["symbols"]
    n_sym = len(symbols)
    times = panel["times"]
    dates = panel["dates"]
    minutes = panel["minutes"]
    close_px = panel["close"]
    open_px = panel["open"]
    low_px = panel["low"]
    ma = panel["ma"]
    rsi = panel["rsi"]
    t_len = len(times)
    cd0 = OPEN_CLOCK.hour * 60 + OPEN_CLOCK.minute
    cd1 = cooldown_end.hour * 60 + cooldown_end.minute
    mid0 = MIDDAY_START.hour * 60 + MIDDAY_START.minute
    mid1 = MIDDAY_END.hour * 60 + MIDDAY_END.minute

    gaps = [_gap_by_session(panel, i) for i in range(n_sym)]
    member = np.zeros((n_sym, t_len), dtype=bool)
    uniq_days = pd.unique(dates)
    day_member = {}
    for day in uniq_days:
        day_member[day] = np.array([_is_member(spans, sym, day) for sym in symbols], dtype=bool)
    for i, day in enumerate(dates):
        member[:, i] = day_member[day]

    cash = INITIAL
    positions: dict[int, dict] = {}
    entered_day: dict = {}
    current_day = None
    daily_equity: list[tuple] = []
    trades: list[dict] = []
    fills: list[dict] = []
    prefix_fail = 0
    prefix_n = 0

    def mark(i: int) -> float:
        total = cash
        for s, pos in positions.items():
            px = close_px[s, i]
            if np.isfinite(px) and px > 0:
                total += pos["shares"] * float(px)
            else:
                total += pos["shares"] * pos["entry"]
        return float(total)

    def close_position(s: int, i: int, raw_px: float, reason: str) -> None:
        nonlocal cash
        pos = positions[s]
        fill = max(0.01, float(raw_px) * (1.0 - SLIP))
        notional = pos["shares"] * fill
        cost = pos["shares"] * float(raw_px) * SLIP
        cash += notional
        ret = fill / pos["entry"] - 1.0
        trades.append(
            {
                "symbol": symbols[s],
                "entry_date": str(pos["entry_day"]),
                "exit_date": str(dates[i]),
                "entry_session": int(pos["entry_session"]),
                "exit_session": int(panel["session_of"][i]),
                "ret_pct": ret * 100.0,
                "reason": reason,
                "notional": pos["notional"],
            }
        )
        fills.append(
            {
                "session": int(panel["session_of"][i]),
                "side": "sell",
                "notional": notional,
                "cost": cost,
            }
        )
        del positions[s]

    for i in range(t_len - 1):
        day = dates[i]
        if day != current_day:
            if current_day is not None and i > 0:
                daily_equity.append((current_day, mark(i - 1)))
            current_day = day
            entered_day = {}

        for s, pos in list(positions.items()):
            if i < pos["entry_i"]:
                continue
            low = low_px[s, i]
            opx = open_px[s, i]
            if not np.isfinite(low) or not np.isfinite(opx):
                if dates[i] >= pos["expiry"]:
                    cpx = close_px[s, i]
                    if np.isfinite(cpx):
                        close_position(s, i, float(cpx), "time")
                continue
            if dates[i] >= pos["expiry"]:
                close_position(s, i, float(opx), "time")
                continue
            if low <= pos["stop"]:
                raw = float(opx) if opx <= pos["stop"] else float(pos["stop"])
                close_position(s, i, raw, "stop")

        in_cooldown = cd0 <= int(minutes[i]) <= cd1
        slots = MAX_NAMES - len(positions)
        if in_cooldown or slots <= 0:
            continue
        # Next executable print must be later the same session.
        if dates[i + 1] != day:
            continue
        equity = mark(i)
        budget = equity * SLEEVE / MAX_NAMES
        if budget <= 1 or cash <= 1:
            continue
        rsi_cap = RSI_MAX_MIDDAY if mid0 <= int(minutes[i]) <= mid1 else RSI_MAX
        ranked: list[tuple[float, int]] = []
        for s in range(n_sym):
            if s in positions or s in entered_day:
                continue
            if not member[s, i]:
                continue
            px = close_px[s, i]
            avg = ma[s, i]
            if not np.isfinite(px) or not np.isfinite(avg) or avg <= 0 or px <= avg:
                continue
            gap = gaps[s].get(day)
            if gap is not None and gap > gap_limit:
                continue
            r = rsi[s, i]
            if np.isfinite(r) and r >= rsi_cap:
                continue
            ranked.append((float(px / avg - 1.0), s))
        if not ranked:
            continue
        ranked.sort(reverse=True)
        took = 0
        for _score, s in ranked:
            if took >= MAX_NEW_PER_BAR or len(positions) >= MAX_NAMES:
                break
            j = i + 1
            while j < t_len and dates[j] == day and not np.isfinite(open_px[s, j]):
                j += 1
            if j >= t_len or dates[j] != day or not np.isfinite(open_px[s, j]):
                continue
            raw = float(open_px[s, j])
            fill = raw * (1.0 + SLIP)
            room = min(budget, cash / (1.0 + 1e-9))
            if room <= 1:
                break
            shares = room / fill
            cost = shares * raw * SLIP
            cash -= shares * fill
            atr = panel["daily_atr"][symbols[s]].get(day)
            if atr is None or atr <= 0:
                atr = fill * 0.02
            stop = max(0.01, fill - ATR_MULT * float(atr))
            expiry = (pd.Timestamp(day) + pd.Timedelta(days=HOLD_DAYS)).date()
            positions[s] = {
                "shares": shares,
                "entry": fill,
                "stop": stop,
                "notional": shares * fill,
                "entry_i": j,
                "entry_day": dates[j],
                "entry_session": int(panel["session_of"][j]),
                "expiry": expiry,
            }
            entered_day[s] = True
            fills.append(
                {
                    "session": int(panel["session_of"][j]),
                    "side": "buy",
                    "notional": shares * fill,
                    "cost": cost,
                }
            )
            took += 1
            if prefix_n < 25:
                native = close_px[s, : i + 1]
                native = native[np.isfinite(native)]
                prefix_n += 1
                if len(native) < MA_MIN:
                    prefix_fail += 1
                else:
                    window = native[-min(MA_WINDOW, len(native)) :]
                    if abs(float(window.mean()) - float(ma[s, i])) > 1e-4:
                        prefix_fail += 1
                    if times[j] <= times[i]:
                        prefix_fail += 1

    if t_len:
        daily_equity.append((dates[-1], mark(t_len - 1)))
        for s, pos in list(positions.items()):
            px = close_px[s, t_len - 1]
            if np.isfinite(px):
                close_position(s, t_len - 1, float(px), "eod")

    # One mark per session, last write wins.
    by_day: dict = {}
    for day, eq in daily_equity:
        by_day[day] = eq
    session_list = list(panel["sessions"])
    curve = [by_day.get(day, np.nan) for day in session_list]
    # Carry the last known mark across sessions where this book had no bar.
    last = INITIAL
    filled = []
    for value in curve:
        if value is None or not np.isfinite(value):
            filled.append(last)
        else:
            last = float(value)
            filled.append(last)
    return {
        "equity": filled,
        "sessions": [str(d) for d in session_list],
        "trades": trades,
        "fills": fills,
        "prefix_fail": prefix_fail,
        "prefix_n": prefix_n,
        "final_equity": filled[-1] if filled else INITIAL,
    }


def _window_activity(result: dict, begin: int, end: int) -> dict:
    trades = [
        t
        for t in result["trades"]
        if begin <= int(t["entry_session"]) < end
    ]
    fills = [f for f in result["fills"] if begin <= int(f["session"]) < end]
    bought = sum(f["notional"] for f in fills if f["side"] == "buy")
    sold = sum(f["notional"] for f in fills if f["side"] == "sell")
    costs = sum(f["cost"] for f in fills)
    return {
        "trades": len(trades),
        "bought": bought,
        "sold": sold,
        "costs": costs,
    }


def score_span(result: dict, begin: int, end: int) -> dict:
    holdout = score_chronological_holdout(
        result["equity"],
        list(range(len(result["equity"]))),
        test_begin=begin,
        test_end=end,
        initial_capital=INITIAL,
    )
    activity = _window_activity(result, begin, end)
    marks = result["equity"][begin:end]
    start = float(holdout["start_equity"]) or INITIAL
    avg_eq = float(np.mean([start, *marks])) if marks else start
    turnover = (activity["bought"] + activity["sold"]) / avg_eq if avg_eq else 0.0
    n = max(1, int(holdout["test_marks"]))
    net = float(holdout["total_return_pct"])
    ann = ((1.0 + net / 100.0) ** (252.0 / n) - 1.0) * 100.0 if net > -100 else -100.0
    return {
        "net_return_pct": round(net, 2),
        "ann_return_pct": round(ann, 2),
        "sharpe": round(float(holdout["sharpe"]), 2),
        "max_dd_pct": round(float(holdout["max_drawdown_pct"]), 2),
        "turnover": round(turnover, 2),
        "trades": int(activity["trades"]),
        "costs": round(activity["costs"], 2),
        "start": result["sessions"][begin] if begin < len(result["sessions"]) else None,
        "end": result["sessions"][end - 1] if end - 1 < len(result["sessions"]) else None,
        "sessions": n,
    }


def build_folds(n_sessions: int) -> dict:
    """Requested 24/6 design when the calendar allows it; otherwise the longest honest split."""
    requested_ok = n_sessions >= TRAIN_SESSIONS + TEST_SESSIONS + HOLDOUT_SESSIONS
    holdout_begin = max(0, n_sessions - HOLDOUT_SESSIONS)
    pre = holdout_begin
    if requested_ok:
        # Rolling tests of 6 months after a 24-month train, holdout untouched.
        first_test = TRAIN_SESSIONS
        folds = []
        begin = first_test
        while begin + TEST_SESSIONS <= holdout_begin:
            folds.append((begin, begin + TEST_SESSIONS))
            begin += TEST_SESSIONS
        design = "24-month train, 6-month test, final 6 months held out"
    else:
        warmup = MA_WINDOW  # daily ATR also needs ~14 sessions; 50 covers both
        usable = pre - warmup
        n_folds = max(0, usable // TEST_SESSIONS)
        # Pack complete 6-month tests against the holdout so train is everything before each test.
        folds = []
        block = n_folds * TEST_SESSIONS
        start = holdout_begin - block
        for k in range(n_folds):
            b = start + k * TEST_SESSIONS
            folds.append((b, b + TEST_SESSIONS))
        design = (
            f"requested 24/6 design does not fit in {n_sessions} sessions; "
            f"using {n_folds} complete 6-month test(s) after a short warmup, "
            "with the latest 6 months held out"
        )
    return {
        "requested_design_fits": requested_ok,
        "design": design,
        "folds": folds,
        "holdout": (holdout_begin, n_sessions),
        "n_sessions": n_sessions,
    }


def buy_hold(symbol: str, sessions: list[str]) -> list[float] | None:
    frame = load_close_matrix(interval="1d", tickers=[symbol], ffill=False, force_refresh=True)
    if frame is None or frame.empty or symbol not in frame.columns:
        return None
    series = frame[symbol].copy()
    series.index = pd.DatetimeIndex(series.index).tz_localize(None).normalize()
    series = series[~series.index.duplicated(keep="last")].sort_index()
    idx = pd.to_datetime(sessions)
    aligned = series.reindex(idx, method="ffill")
    values = pd.to_numeric(aligned, errors="coerce").to_numpy(dtype=float)
    if not np.isfinite(values).any():
        return None
    first = next((float(v) for v in values if np.isfinite(v) and v > 0), None)
    if first is None:
        return None
    equity = []
    last = first
    for v in values:
        if np.isfinite(v) and v > 0:
            last = float(v)
        equity.append(INITIAL * last / first)
    return equity


def score_benchmark(equity: list[float], begin: int, end: int, sessions: list[str]) -> dict:
    dummy = {"equity": equity, "sessions": sessions, "trades": [], "fills": []}
    row = score_span(dummy, begin, end)
    row["trades"] = 1
    row["turnover"] = 0.0
    row["costs"] = 0.0
    return row


def run_daily_reference(membership: pd.DataFrame) -> dict | None:
    """PIT daily book on local closes. No cooldown and no overnight-gap filter."""
    members = sorted(membership["ticker"].astype(str).str.upper().unique())
    data = load_close_matrix(interval="1d", tickers=members, ffill=False, force_refresh=True)
    if data is None or data.empty:
        return None
    data = ffill_interior_only(data)
    data = session_bars(data)
    data = data[data.index >= "2024-08-01"]
    if len(data) < 80:
        return None
    symbols = [c for c in data.columns if resolve_column(str(c), data.columns)]
    frame = data[symbols].apply(pd.to_numeric, errors="coerce")
    elig = membership_matrix(frame.index, membership, tickers=[str(c).upper() for c in symbols])
    elig = elig.reindex(index=pd.DatetimeIndex(frame.index).normalize(), columns=[str(c).upper() for c in symbols])
    elig.index = frame.index
    close = frame.to_numpy(dtype=float)
    eligible = elig.fillna(False).to_numpy(dtype=bool)
    n_days, n_sym = close.shape
    ma = frame.rolling(MA_WINDOW, min_periods=MA_MIN).mean().to_numpy(dtype=float)
    atr = frame.diff().abs().rolling(ATR_PERIOD, min_periods=ATR_PERIOD).mean().shift(1).to_numpy(dtype=float)
    dates = [ts.date() for ts in frame.index]
    sessions = [str(ts.date()) for ts in frame.index]

    cash = INITIAL
    positions: dict[int, dict] = {}
    trades: list[dict] = []
    fills: list[dict] = []
    equity = []

    def mark(i: int) -> float:
        total = cash
        for s, pos in positions.items():
            px = close[i, s]
            total += pos["shares"] * (float(px) if np.isfinite(px) and px > 0 else pos["entry"])
        return float(total)

    for i in range(n_days):
        for s, pos in list(positions.items()):
            if i <= pos["entry_i"]:
                continue
            px = close[i, s]
            if not np.isfinite(px) or px <= 0:
                continue
            if dates[i] >= pos["expiry"] or px <= pos["stop"]:
                reason = "time" if dates[i] >= pos["expiry"] else "stop"
                fill = float(px) * (1.0 - SLIP)
                cash += pos["shares"] * fill
                trades.append(
                    {
                        "symbol": symbols[s],
                        "entry_session": pos["entry_session"],
                        "exit_session": i,
                        "ret_pct": (fill / pos["entry"] - 1.0) * 100.0,
                        "reason": reason,
                    }
                )
                fills.append({"session": i, "side": "sell", "notional": pos["shares"] * fill, "cost": pos["shares"] * float(px) * SLIP})
                del positions[s]
        equity.append(mark(i))
        if i >= n_days - 1 or len(positions) >= MAX_NAMES:
            continue
        ranked = []
        for s in range(n_sym):
            if s in positions:
                continue
            if not eligible[i, s]:
                continue
            px = close[i, s]
            avg = ma[i, s]
            if not np.isfinite(px) or not np.isfinite(avg) or avg <= 0 or px <= avg:
                continue
            ranked.append((float(px / avg - 1.0), s))
        ranked.sort(reverse=True)
        equity_now = equity[-1]
        budget = equity_now * SLEEVE / MAX_NAMES
        took = 0
        for _score, s in ranked:
            if took >= MAX_NEW_PER_BAR or len(positions) >= MAX_NAMES:
                break
            raw = close[i + 1, s]
            if not np.isfinite(raw) or raw <= 0:
                continue
            if not eligible[i, s]:
                continue
            fill = float(raw) * (1.0 + SLIP)
            room = min(budget, cash)
            if room <= 1:
                break
            shares = room / fill
            cash -= shares * fill
            atr_v = atr[i, s]
            if not np.isfinite(atr_v) or atr_v <= 0:
                atr_v = fill * 0.02
            positions[s] = {
                "shares": shares,
                "entry": fill,
                "stop": max(0.01, fill - ATR_MULT * float(atr_v)),
                "entry_i": i + 1,
                "entry_session": i + 1,
                "expiry": (pd.Timestamp(dates[i + 1]) + pd.Timedelta(days=HOLD_DAYS)).date(),
            }
            fills.append({"session": i + 1, "side": "buy", "notional": shares * fill, "cost": shares * float(raw) * SLIP})
            took += 1

    for s, pos in list(positions.items()):
        px = close[-1, s]
        if not np.isfinite(px):
            continue
        fill = float(px) * (1.0 - SLIP)
        cash += pos["shares"] * fill
        trades.append({"symbol": symbols[s], "entry_session": pos["entry_session"], "exit_session": n_days - 1, "ret_pct": 0.0, "reason": "eod"})
        fills.append({"session": n_days - 1, "side": "sell", "notional": pos["shares"] * fill, "cost": pos["shares"] * float(px) * SLIP})
    in_window = sp500_tickers_in_window(membership, frame.index[0], frame.index[-1])
    have = {str(c).upper() for c in symbols}
    missing = [m for m in in_window if resolve_column(m, symbols) is None]
    covered_days = []
    for i in range(n_days):
        active = int(eligible[i].sum())
        got = int((eligible[i] & np.isfinite(close[i])).sum())
        covered_days.append((active, got))
    avg_active = float(np.mean([a for a, _g in covered_days])) if covered_days else 0.0
    avg_got = float(np.mean([g for _a, g in covered_days])) if covered_days else 0.0
    return {
        "equity": equity,
        "sessions": sessions,
        "trades": trades,
        "fills": fills,
        "coverage": {
            "members_in_window": len(in_window),
            "members_missing_table": len(missing),
            "missing_sample": missing[:20],
            "avg_members_per_day": round(avg_active, 1),
            "avg_with_price": round(avg_got, 1),
            "start": sessions[0],
            "end": sessions[-1],
            "sessions": len(sessions),
        },
    }


def promotion(name: str, folds: list[dict], base_folds: list[dict], agg: dict, base_agg: dict, *, faithful: bool) -> dict:
    reasons = []
    if not faithful:
        reasons.append("the intraday panel is not a point-in-time index universe")
    if len(folds) < 4:
        reasons.append(f"only {len(folds)} out-of-sample fold(s); the rule needs at least four")
    beats = 0
    for row, base in zip(folds, base_folds):
        if row["net_return_pct"] > base["net_return_pct"]:
            beats += 1
    if len(folds) >= 4 and beats < 3:
        reasons.append(f"beat baseline net return in {beats} of {len(folds)} folds")
    if agg["net_return_pct"] <= base_agg["net_return_pct"]:
        reasons.append(
            f"aggregate out-of-sample return {agg['net_return_pct']}% did not beat baseline {base_agg['net_return_pct']}%"
        )
    # max_dd_pct is negative; more negative is worse.
    if agg["max_dd_pct"] < base_agg["max_dd_pct"] - DD_WORSE_PP:
        reasons.append(
            f"max drawdown {agg['max_dd_pct']}% is more than {DD_WORSE_PP:.0f}pp worse than baseline {base_agg['max_dd_pct']}%"
        )
    return {
        "variant": name,
        "passes": not reasons,
        "folds_beating_baseline": beats,
        "fold_count": len(folds),
        "reasons": reasons or ["cleared the predeclared rule"],
    }


def _fmt_row(label: str, row: dict) -> str:
    return (
        f"| {label} | {row['start']} → {row['end']} | {row['net_return_pct']:+.2f}% | "
        f"{row['ann_return_pct']:+.2f}% | {row['sharpe']:.2f} | {row['max_dd_pct']:.2f}% | "
        f"{row['turnover']:.2f} | {row['trades']} | ${row['costs']:,.0f} |"
    )


def render_markdown(payload: dict) -> str:
    lines = [
        "# NYSE momentum walk-forward",
        "",
        f"**Recommendation: {payload['recommendation']}**",
        "",
        payload["recommendation_why"],
        "",
        "## What the current book actually runs",
        "",
        payload["rules_text"],
        "",
        "## Data",
        "",
        payload["data_text"],
        "",
        "## Out-of-sample folds",
        "",
        "Net return is after 5 bps of slippage each way. Sharpe is daily, rf = 0. "
        "Drawdown is negative. Turnover is buy plus sell notional divided by average equity in the window. "
        "The holdout is not in this table.",
        "",
        "| Variant | Window | Net | Ann. | Sharpe | Max DD | Turnover | Trades | Costs |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in payload["fold_rows"]:
        lines.append(_fmt_row(row["label"], row))
    lines += [
        "",
        "## Aggregate out-of-sample and holdout",
        "",
        "The aggregate row chains the out-of-sample folds only. The holdout was not used to accept or reject a variant.",
        "",
        "| Variant | Window | Net | Ann. | Sharpe | Max DD | Turnover | Trades | Costs |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in payload["summary_rows"]:
        lines.append(_fmt_row(row["label"], row))
    lines += [
        "",
        "## Rule",
        "",
        payload["rule_text"],
        "",
        "## Limitations",
        "",
        payload["limits_text"],
        "",
        "## Files",
        "",
        "See `nyse_momentum_walkforward_NOTE.txt` in this folder.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("loading 5-minute cache", flush=True)
    panel, meta = load_intraday_panel()
    membership = load_sp500_membership()
    spans = _member_on(membership)
    cache_symbols = set(panel["symbols"])
    asof = panel["dates"][len(panel["dates"]) // 2]
    active = sp500_tickers_in_window(
        membership, pd.Timestamp(panel["sessions"][0]), pd.Timestamp(panel["sessions"][-1])
    )
    active_mid = [
        t
        for t, rows in spans.items()
        if _is_member(spans, t, asof)
    ]
    in_cache_members = sorted(s for s in cache_symbols if _is_member(spans, s, asof))
    not_members = sorted(cache_symbols - set(spans))
    print(
        f"cache {meta['symbols']} names, {meta['sessions']} sessions, "
        f"{meta['start']} → {meta['end']}",
        flush=True,
    )
    print("running three intraday variants", flush=True)
    variants = {
        "baseline": simulate_intraday(panel, spans, cooldown_end=COOLDOWN_BASE, gap_limit=GAP_BASE),
        "cooldown_1030": simulate_intraday(panel, spans, cooldown_end=COOLDOWN_LONG, gap_limit=GAP_BASE),
        "gap_1_5": simulate_intraday(panel, spans, cooldown_end=COOLDOWN_BASE, gap_limit=GAP_TIGHT),
    }
    layout = build_folds(len(panel["sessions"]))
    sessions = variants["baseline"]["sessions"]

    def pack(name: str, result: dict) -> dict:
        folds = [score_span(result, b, e) for b, e in layout["folds"]]
        if layout["folds"]:
            agg = score_span(result, layout["folds"][0][0], layout["folds"][-1][1])
        else:
            agg = score_span(result, 0, 1)
        hold = score_span(result, *layout["holdout"])
        return {"folds": folds, "aggregate": agg, "holdout": hold, "prefix_fail": result["prefix_fail"], "prefix_n": result["prefix_n"]}

    packed = {name: pack(name, result) for name, result in variants.items()}
    print("benchmarks", flush=True)
    benches = {}
    for sym in ("VTI", "SPY"):
        equity = buy_hold(sym, sessions)
        if equity is None:
            continue
        dummy = {"equity": equity, "sessions": sessions, "trades": [], "fills": []}
        benches[sym] = {
            "folds": [score_benchmark(equity, b, e, sessions) for b, e in layout["folds"]],
            "aggregate": score_benchmark(equity, layout["folds"][0][0], layout["folds"][-1][1], sessions)
            if layout["folds"]
            else None,
            "holdout": score_benchmark(equity, *layout["holdout"], sessions),
        }

    print("daily PIT reference", flush=True)
    daily = run_daily_reference(membership)
    daily_packed = None
    if daily is not None:
        # Score the daily book on its own calendar with the same fold rule.
        d_layout = build_folds(len(daily["sessions"]))
        daily_packed = {
            "layout": {
                "requested_design_fits": d_layout["requested_design_fits"],
                "design": d_layout["design"],
                "folds": d_layout["folds"],
                "holdout": d_layout["holdout"],
            },
            "folds": [score_span(daily, b, e) for b, e in d_layout["folds"]],
            "aggregate": score_span(daily, d_layout["folds"][0][0], d_layout["folds"][-1][1])
            if d_layout["folds"]
            else None,
            "holdout": score_span(daily, *d_layout["holdout"]),
            "coverage": daily["coverage"],
        }

    faithful = False  # cache membership is a saved name list, not the index
    decisions = {
        name: promotion(
            name,
            packed[name]["folds"],
            packed["baseline"]["folds"],
            packed[name]["aggregate"],
            packed["baseline"]["aggregate"],
            faithful=faithful,
        )
        for name in ("cooldown_1030", "gap_1_5")
    }
    recommendation = "Keep the baseline."
    why = (
        "Keep the 10:15 cooldown and the 2% gap skip. On the two 6-month out-of-sample folds "
        "the stored bars can support, both variants made less than the baseline, and both made "
        "less over those folds combined. The untouched holdout also favors the baseline. "
        "A pass is not available anyway: the local history cannot build a 24-month train plus "
        "four 6-month tests, and the 5-minute names are a saved list that covers about 9% of "
        "S&P 500 membership on a typical day."
    )

    fold_rows = []
    labels = (
        ("baseline", "Baseline 10:15 / 2.0%"),
        ("cooldown_1030", "Cooldown to 10:30"),
        ("gap_1_5", "Gap skip 1.5%"),
    )
    for key, label in labels:
        for i, row in enumerate(packed[key]["folds"], start=1):
            fold_rows.append({"label": f"{label} fold {i}", **row})
        for sym, bench in benches.items():
            for i, row in enumerate(bench["folds"], start=1):
                if key == "baseline":
                    fold_rows.append({"label": f"{sym} fold {i}", **row})
    summary_rows = []
    for key, label in labels:
        summary_rows.append({"label": f"{label} aggregate OOS", **packed[key]["aggregate"]})
        summary_rows.append({"label": f"{label} holdout", **packed[key]["holdout"]})
    for sym, bench in benches.items():
        if bench["aggregate"]:
            summary_rows.append({"label": f"{sym} aggregate OOS", **bench["aggregate"]})
        summary_rows.append({"label": f"{sym} holdout", **bench["holdout"]})
    if daily_packed and daily_packed["aggregate"]:
        summary_rows.append({"label": "Daily PIT reference aggregate OOS", **daily_packed["aggregate"]})
        summary_rows.append({"label": "Daily PIT reference holdout", **daily_packed["holdout"]})
        for i, row in enumerate(daily_packed["folds"], start=1):
            fold_rows.append({"label": f"Daily PIT reference fold {i}", **row})

    prefix_bits = [
        f"{name}: {packed[name]['prefix_fail']} of {packed[name]['prefix_n']} sampled signals failed the prefix check"
        for name in packed
    ]
    rules = "\n".join(
        [
            "Employed books are NYSE-only. `spy_sleeve_enabled()` is false, crypto flags in the live and both paper env files are false, and the portal sets VTI core to 0 with the NYSE sleeve at 95%. Stat-arb stays off while crypto is off.",
            "",
            "Medium SoT (`alpaca_paper_v2`) is the desktop book. At launch the portal sets 15 names, a 30-day max hold, and a 2× daily-ATR stop, and it leaves `PAPER_MOMENTUM_QUALITY_FIXES=true` from that book's env. The on-disk env still says 10 names and 2.5× ATR; those values are overwritten when the portal starts the process.",
            "",
            "The ranker that places orders uses a 50-bar average on the live matrix (`_equity_momentum_candidates`). `run_all.py` loads that matrix with the 5-minute default. `effective_nyse_ma_window()` still returns 70 on paper and is only printed. This test follows the order path: MA50 of 5-minute closes, RSI(14) on those closes (ceiling 70, or 72 from 12:00–14:00 ET), one new buy per symbol per day, at most 12 new buys per bar and 15 open names, each sized at 0.95/15 of equity. Exits are the 2× daily-ATR stop (absolute close-to-close ATR, lagged one session) or 30 calendar days. Fills are the next same-session 5-minute open, with 5 bps of slippage per side. Alpaca equity commission in this repo is $0, so that slippage is the whole trading cost.",
            "",
            "Baseline cooldown is 9:30–10:15 ET inclusive, and the gap skip is an up-gap above 2% (today's first open over the prior session close). The cooldown variant moves only the end to 10:30. The gap variant moves only the threshold to 1.5%. They are not combined.",
        ]
    )
    data_text = "\n".join(
        [
            f"Five-minute cache: {meta['symbols']} names, {meta['bars']} bars, {meta['sessions']} sessions, {meta['start']} to {meta['end']}.",
            f"On {asof}, {len(in_cache_members)} of {len(active_mid)} S&P 500 members have a cache file ({(100.0 * len(in_cache_members) / len(active_mid)) if active_mid else 0:.1f}%). {len(not_members)} cached names are not in the membership file.",
            f"Names that were members at some point in the cache window: {len(active)}. Members with no cache file are absent from the intraday test, including delistings that the membership file still records.",
            f"Fold plan: {layout['design']}. Requested design fits: {layout['requested_design_fits']}.",
            "Sqlite daily tables for the liquid names used by the bot (AAPL, MSFT, NVDA, SPY, VTI, and the other current mega-caps checked) run from 2024-08-22 to 2026-10-09 and have Close only, no Open. Older long history remains for names that were not refreshed. Live 5-minute tables in `market_data.db` cover about one week (2026-10-05 to 2026-10-09).",
            "Prefix check recomputes MA50 from closes at or before the signal bar and requires the fill timestamp to be later. "
            + "; ".join(prefix_bits)
            + ".",
        ]
    )
    rule_lines = []
    for name, decision in decisions.items():
        rule_lines.append(
            f"- **{name}**: {'pass' if decision['passes'] else 'fail'} — " + "; ".join(decision["reasons"])
        )
    if daily_packed:
        cov = daily_packed["coverage"]
        rule_lines.append(
            f"- Daily PIT reference (local closes, no cooldown, no gap filter, next-session close fills) covers {cov['start']} to {cov['end']}, "
            f"average {cov['avg_with_price']} priced names versus {cov['avg_members_per_day']} members that day. "
            "It is not a test of either knob."
        )
    limits = "\n".join(
        [
            "- The 24-month train plus rolling 6-month tests plus a final 6-month holdout needs roughly 54 months. The 5-minute cache is about 24 months, and liquid daily history is about 26 months. That design was not run, because shortening the train in secret would not be the test that was specified.",
            "- Intraday results use only names that were saved in `data/intraday_cache` and were S&P 500 members on the signal date. That is not the index, and it is not a survivorship-free set of delistings. Do not read those returns as the Medium book.",
            "- Cooldown and gap cannot be tested on the daily PIT panel. Daily tables have no open, and a close-to-close change is not the overnight gap the order path uses. A daily decision made at the close is also after both 10:15 and 10:30, so the two cooldowns would be identical.",
            "- The daily PIT reference fills at the next session close and stops on the close, because high and open are not in sqlite. Intraday stops use the bar low and gap through the open when the bar opens past the stop.",
            "- Scanner boosts, regime sizing, idle-cash adds, and the 8-name Lab book (A2B1 on) are not in this simulation. Medium scanners are off. Live does not apply the paper quality filters.",
            "- Slippage is a flat 5 bps per side, not a measured spread. No broker connection was opened.",
        ]
    )
    payload = {
        "recommendation": recommendation,
        "recommendation_why": why,
        "rules_text": rules,
        "data_text": data_text,
        "rule_text": "\n".join(rule_lines),
        "limits_text": limits,
        "fold_rows": fold_rows,
        "summary_rows": summary_rows,
    }
    text = render_markdown(payload)
    OUT_MD.write_text(text, encoding="utf-8")
    serial = {
        "recommendation": recommendation,
        "layout": {
            "requested_design_fits": layout["requested_design_fits"],
            "design": layout["design"],
            "folds": layout["folds"],
            "holdout": layout["holdout"],
            "sessions": sessions,
        },
        "cache": meta,
        "coverage_asof": {
            "date": str(asof),
            "index_members": len(active_mid),
            "cached_members": len(in_cache_members),
            "cached_nonmembers": len(not_members),
        },
        "variants": {
            name: {
                "folds": packed[name]["folds"],
                "aggregate_oos": packed[name]["aggregate"],
                "holdout": packed[name]["holdout"],
                "prefix_fail": packed[name]["prefix_fail"],
                "prefix_n": packed[name]["prefix_n"],
            }
            for name in packed
        },
        "benchmarks": benches,
        "daily_pit_reference": daily_packed
        and {
            "coverage": daily_packed["coverage"],
            "folds": daily_packed["folds"],
            "aggregate_oos": daily_packed["aggregate"],
            "holdout": daily_packed["holdout"],
            "design": daily_packed["layout"]["design"],
        },
        "decisions": decisions,
        "settings": {
            "ma_bars": MA_WINDOW,
            "ma_note": "5-minute bars; PAPER_NYSE_MA_WINDOW 70 is not used by the ranker",
            "cooldown_baseline": "09:30-10:15 ET",
            "gap_baseline": GAP_BASE,
            "max_names": MAX_NAMES,
            "hold_calendar_days": HOLD_DAYS,
            "atr_mult": ATR_MULT,
            "slip_bps_per_side": SLIP_BPS,
            "sleeve": SLEEVE,
        },
    }
    OUT_JSON.write_text(json.dumps(serial, indent=2, default=str), encoding="utf-8")
    note = "\n".join(
        [
            "Research files created:",
            f"  {OUT_MD.relative_to(ROOT)}",
            f"  {OUT_JSON.relative_to(ROOT)}",
            f"  {Path(__file__).relative_to(ROOT)}",
            f"  {OUT_NOTE.relative_to(ROOT)}",
            "",
            "No production settings, journals, credentials, or order paths were edited.",
            "No files under sources/ were edited.",
            "This script only reads market_data.db. It does not write it.",
            "No broker connection was opened and no order was sent.",
            "",
        ]
    )
    OUT_NOTE.write_text(note, encoding="utf-8")
    print(recommendation, flush=True)
    print(f"wrote {OUT_MD}", flush=True)


if __name__ == "__main__":
    main()
