"""No-order forward shadow: current MA50 rank vs four-horizon center rule.

Run after the bot's local 5-minute cache has refreshed, preferably after 10:20 ET.
This script reads cached bars only, writes a dated signal snapshot, and never
imports the broker or order-execution path. Re-running on the same date is safe.

From stock-bot/:
  python scripts/analysis/forward_shadow_compare.py
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import date, time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from modules.pit_universe import load_sp500_membership  # noqa: E402
from hypersphere_inward_funnel_backtest import (  # noqa: E402
    build_daily_features,
    _member_on,
)
from nyse_momentum_walkforward import _is_member, load_intraday_panel  # noqa: E402

OUT_JSONL = Path(__file__).with_name("forward_shadow_signals.jsonl")
OUT_CSV = Path(__file__).with_name("forward_shadow_outcomes.csv")
SIGNAL_TIME = time(10, 15)
HORIZONS = (1, 5, 10, 20)
TOP_N = 5
SLIP = 0.0005  # 5 bps each side, matching the exploratory replay


def _close_at_or_after_signal(panel: dict, day) -> tuple[int, np.ndarray] | None:
    times = panel["times"]
    dates = panel["dates"]
    bars = np.flatnonzero(dates == day)
    if not bars.size:
        return None
    eligible = bars[np.array([times[i].time() >= SIGNAL_TIME for i in bars])]
    if not eligible.size:
        return None
    i = int(eligible[0])
    return i, panel["close"][:, i]


def _signals(panel: dict, features: np.ndarray, session_i: int,
             membership_spans: dict) -> dict:
    day = panel["sessions"][session_i]
    signal = _close_at_or_after_signal(panel, day)
    if signal is None:
        raise RuntimeError(f"No bar at or after 10:15 ET for {day}")
    bar_i, marks = signal
    symbols = panel["symbols"]
    close = panel["close"]
    dates = panel["dates"]
    current_rank: list[tuple[float, str]] = []
    center_rank: list[tuple[float, str, list[float]]] = []
    feature_row = features[:, session_i, :]

    # Mirror the bot's core close/MA50 rank and above-MA entry gate using only
    # information through the 10:15 signal bar. Optional scanner boosts and
    # dynamic live-universe membership are deliberately outside this baseline.
    for s, symbol in enumerate(symbols):
        if not np.isfinite(marks[s]) or marks[s] <= 0:
            continue
        if not _is_member(membership_spans, symbol, day):
            continue
        hist_idx = np.flatnonzero((dates <= day) & np.isfinite(close[s, :]) & (close[s, :] > 0))
        # Keep the current 10:15 observation as the final input; earlier bars
        # may be present on the same day, as they are in the live bot matrix.
        hist_idx = hist_idx[hist_idx <= bar_i]
        if hist_idx.size < 20:
            continue
        window = min(50, hist_idx.size)
        ma = float(np.mean(close[s, hist_idx[-window:]]))
        base_score = float(marks[s] / ma - 1.0)
        if marks[s] > ma:
            current_rank.append((base_score, symbol))

        f = feature_row[s]
        if np.isfinite(f).all() and int(np.count_nonzero(f > 0)) >= 2:
            center_score = float(np.mean(f))
            if center_score > 0:
                center_rank.append((center_score, symbol, f.tolist()))

    current_rank.sort(reverse=True)
    center_rank.sort(reverse=True)
    return {
        "date": str(day),
        "signal_time_et": "10:15",
        "bar_timestamp": str(panel["times"][bar_i]),
        "baseline_ma50_top5": [
            {"symbol": sym, "score": round(score, 8)}
            for score, sym in current_rank[:TOP_N]
        ],
        "four_horizon_center_top5": [
            {"symbol": sym, "score": round(score, 8),
             "features_60d_20d_5d_intraday": [round(x, 8) for x in f]}
            for score, sym, f in center_rank[:TOP_N]
        ],
        "features": ["60-session momentum", "20-session momentum",
                     "5-session momentum", "prior-close-to-10:15 return"],
        "execution_assumption": "signal at 10:15 ET; evaluate from that bar close, less 5 bps each side",
        "orders_sent": False,
    }


def _daily_closes(panel: dict) -> tuple[list, np.ndarray]:
    sessions = list(panel["sessions"])
    dates = panel["dates"]
    close = panel["close"]
    daily = np.full((len(panel["symbols"]), len(sessions)), np.nan)
    for d, day in enumerate(sessions):
        bars = np.flatnonzero(dates == day)
        if not bars.size:
            continue
        vals = close[:, bars]
        valid = np.isfinite(vals) & (vals > 0)
        last = np.where(valid, np.arange(bars.size)[None, :], -1).max(axis=1)
        rows = np.flatnonzero(last >= 0)
        daily[rows, d] = vals[rows, last[rows]]
    return sessions, daily


def _write_outcomes(records: list[dict], sessions: list, daily: np.ndarray,
                    symbols: list[str]) -> int:
    loc = {sym: i for i, sym in enumerate(symbols)}
    session_loc = {str(day): i for i, day in enumerate(sessions)}
    rows = []
    for record in records:
        d = session_loc.get(record["date"])
        if d is None:
            continue
        for strategy_key, label in (("baseline_ma50_top5", "baseline_ma50"),
                                    ("four_horizon_center_top5", "four_horizon_center")):
            picks = record.get(strategy_key, [])
            for h in HORIZONS:
                rets = []
                for pick in picks:
                    s = loc.get(pick["symbol"])
                    start_i = d
                    end_i = d + h
                    if s is None or end_i >= daily.shape[1]:
                        continue
                    # Approximate entry at the 10:15 mark using stored score
                    # snapshot's per-symbol signal close, added below.
                    start = record.get("signal_prices", {}).get(pick["symbol"])
                    end = daily[s, end_i]
                    if start and np.isfinite(end) and start > 0:
                        rets.append(float(end / start * (1 - SLIP) / (1 + SLIP) - 1))
                rows.append({"date": record["date"], "strategy": label,
                             "horizon_sessions": h, "positions": len(rets),
                             "equal_weight_return_pct": round(100 * float(np.mean(rets)), 4) if rets else ""})
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "strategy", "horizon_sessions",
                                               "positions", "equal_weight_return_pct"])
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main() -> int:
    panel, meta = load_intraday_panel()
    sessions = list(panel["sessions"])
    # Snapshot the latest session with its 10:15 bar. Refuse stale cache so an
    # old cached date cannot quietly masquerade as a live forward observation.
    session_i = next((i for i in range(len(sessions) - 1, -1, -1)
                      if _close_at_or_after_signal(panel, sessions[i]) is not None), None)
    if session_i is None:
        raise RuntimeError("Cached data has no 10:15 ET signal bar")
    latest_day = sessions[session_i]
    age_sessions = len(sessions) - 1 - session_i
    if age_sessions > 0:
        raise RuntimeError(f"Latest usable signal session {latest_day} is not the latest cached session; refresh the cache first")
    latest_date = latest_day if isinstance(latest_day, date) else date.fromisoformat(str(latest_day))
    if (date.today() - latest_date).days > 4:
        raise RuntimeError(
            f"Latest cached signal is {latest_date}, more than four calendar days old; "
            "refresh the market cache before starting a forward shadow. No snapshot was written."
        )
    membership = load_sp500_membership()
    spans = _member_on(membership)
    features, _ = build_daily_features(panel, len(panel["sessions"]))
    snapshot = _signals(panel, features, session_i, spans)
    # Preserve the 10:15 prices needed for an honest forward return label.
    bar_i = int(np.flatnonzero((panel["dates"] == latest_day) &
                               np.array([ts.time() >= SIGNAL_TIME for ts in panel["times"]]))[0])
    snapshot["signal_prices"] = {
        sym: float(panel["close"][i, bar_i])
        for i, sym in enumerate(panel["symbols"])
        if np.isfinite(panel["close"][i, bar_i]) and panel["close"][i, bar_i] > 0
    }
    existing = []
    if OUT_JSONL.exists():
        existing = [json.loads(line) for line in OUT_JSONL.read_text(encoding="utf-8").splitlines() if line.strip()]
    if any(r.get("date") == snapshot["date"] for r in existing):
        print(f"Shadow snapshot for {snapshot['date']} already exists; no duplicate written.")
    else:
        with OUT_JSONL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(snapshot, separators=(",", ":")) + "\n")
        existing.append(snapshot)
        print(f"Recorded no-order shadow snapshot for {snapshot['date']}.")
    day_list, daily_close = _daily_closes(panel)
    n_rows = _write_outcomes(existing, day_list, daily_close, panel["symbols"])
    print(f"Cache: {meta['symbols']} symbols, {meta['sessions']} sessions through {latest_day}.")
    print("Current MA50 top 5: " + ", ".join(x["symbol"] for x in snapshot["baseline_ma50_top5"]))
    print("Four-horizon top 5: " + ", ".join(x["symbol"] for x in snapshot["four_horizon_center_top5"]))
    print(f"Updated realized-outcome table ({n_rows} strategy/horizon rows): {OUT_CSV}")
    print("No orders were sent. Scanner boosts are not included in the baseline ranking.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
