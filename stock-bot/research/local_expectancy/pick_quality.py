"""Selector-only forward returns. No exits in the primary number.

The live ranker is close / MA50 - 1, one name, fill at the next open.
Stops, sizing, and the halt are a separate decomposition.
"""

from __future__ import annotations

import random
from statistics import mean, median

MA_WINDOW = 50
ENTRY_TOLERANCE = 0.01
HORIZONS = (1, 5, 20)
HIT_THRESHOLDS = (0.05, 0.10, 0.20)
PRIMARY_HORIZON = 20
BOOTSTRAP_DRAWS = 2000
RANDOM_SEED = 0
VTI_BLOCK = 20
VTI_SEED = 1
STOP = 0.05
MAX_HOLD_SESSIONS = 30
HALT = 0.15
CLIP = 0.095
NAME_CAP = 0.10
SLEEVE = 0.95
MAX_NAMES = 10


def momentum_score(closes: list[float]) -> float | None:
    """Live score. None when the name is ineligible. The last close is the decision."""
    if len(closes) < MA_WINDOW:
        return None
    window = closes[-MA_WINDOW:]
    average = sum(window) / MA_WINDOW
    last = window[-1]
    if average <= 0 or not last > average * (1 - ENTRY_TOLERANCE):
        return None
    return last / average - 1.0


def select_symbol(scored: list[tuple[float, str]]) -> str | None:
    if not scored:
        return None
    scored = sorted(scored, key=lambda row: (-row[0], row[1]))
    return scored[0][1]


def forward_open(opens: list[float | None], entry_index: int, horizon: int) -> float | None:
    """Open-to-open return over `horizon` sessions. Entry open is not a signal close."""
    exit_index = entry_index + horizon
    if entry_index < 0 or exit_index >= len(opens):
        return None
    entry = opens[entry_index]
    exit_open = opens[exit_index]
    if entry is None or exit_open is None or entry <= 0:
        return None
    return exit_open / entry - 1.0


def path_hit(closes: list[float | None], entry_index: int, horizon: int, entry: float, threshold: float) -> bool | None:
    """True when a completed close before the exit open has reached the threshold."""
    if entry <= 0:
        return None
    last = entry_index + horizon - 1
    if last >= len(closes) or entry_index < 0:
        return None
    seen = [price for price in closes[entry_index : last + 1] if price is not None]
    if not seen:
        return None
    return max(seen) / entry - 1.0 >= threshold


def p_value_ge(draws: list[float], observed: float) -> float:
    """One-sided. Draws at least as large as the observed pick."""
    count = sum(1 for value in draws if value >= observed)
    return (1 + count) / (len(draws) + 1)


def turnover_parts(buy_notional: float, sell_notional: float, nav: list[float]) -> dict:
    """Window totals and the annualized rates. These are different numbers."""
    sessions = len(nav)
    mean_nav = mean(nav) if nav else 0.0
    if mean_nav <= 0 or sessions == 0:
        empty = {
            "one_way_buy": None,
            "one_way_buy_annualized": None,
            "two_sided": None,
            "two_sided_annualized": None,
            "sessions": sessions,
        }
        return empty
    one_way = buy_notional / mean_nav
    two_sided = (buy_notional + sell_notional) / (2 * mean_nav)
    scale = 252 / sessions
    return {
        "one_way_buy": one_way,
        "one_way_buy_annualized": one_way * scale,
        "two_sided": two_sided,
        "two_sided_annualized": two_sided * scale,
        "sessions": sessions,
    }


def signal_change_rate(symbols: list[str]) -> float | None:
    if len(symbols) < 2:
        return None
    changes = sum(1 for left, right in zip(symbols, symbols[1:]) if left != right)
    return changes / (len(symbols) - 1)


def _closes_through(bars: dict[str, tuple[float, float]], sessions: list[str], symbol: str, end: int) -> list[float]:
    out = []
    series = bars[symbol]
    for index in range(end + 1):
        bar = series.get(sessions[index])
        if bar is not None:
            out.append(bar[1])
    return out


def _bar(bars, symbol, day):
    return bars[symbol].get(day)


def daily_selections(
    sessions: list[str],
    bars: dict[str, dict[str, tuple[float, float]]],
    symbols: list[str],
    *,
    window_start: str,
    window_end: str,
) -> list[dict]:
    """Top name each day. The entry open has to exist. No forward-return filter."""
    start = sessions.index(window_start)
    end = sessions.index(window_end)
    rows = []
    for index in range(start, end):
        scored = []
        for symbol in symbols:
            if _bar(bars, symbol, sessions[index]) is None:
                continue
            score = momentum_score(_closes_through(bars, sessions, symbol, index))
            if score is not None:
                scored.append((score, symbol))
        pick = select_symbol(scored)
        if pick is None:
            continue
        entry_bar = _bar(bars, pick, sessions[index + 1])
        if entry_bar is None or entry_bar[0] <= 0:
            continue
        rows.append(
            {
                "decision_index": index,
                "entry_index": index + 1,
                "decision": sessions[index],
                "symbol": pick,
                "eligible_count": len(scored),
            }
        )
    return rows


def decision_rows(
    sessions: list[str],
    bars: dict[str, dict[str, tuple[float, float]]],
    symbols: list[str],
    *,
    window_start: str,
    window_end: str,
    horizon: int,
) -> list[dict]:
    """One row per decision whose open-to-open horizon finishes inside the window."""
    start = sessions.index(window_start)
    end = sessions.index(window_end)
    rows = []
    for index in range(start, end + 1):
        if index + 1 + horizon > end:
            continue
        scored = []
        for symbol in symbols:
            if _bar(bars, symbol, sessions[index]) is None:
                continue
            score = momentum_score(_closes_through(bars, sessions, symbol, index))
            if score is None:
                continue
            scored.append((score, symbol))
        pick = select_symbol(scored)
        if pick is None:
            continue
        entry_day = sessions[index + 1]
        exit_day = sessions[index + 1 + horizon]
        entry_bar = _bar(bars, pick, entry_day)
        exit_bar = _bar(bars, pick, exit_day)
        if entry_bar is None or exit_bar is None or entry_bar[0] <= 0:
            continue
        eligible = []
        for _score, symbol in scored:
            entered = _bar(bars, symbol, entry_day)
            exited = _bar(bars, symbol, exit_day)
            if entered is None or exited is None or entered[0] <= 0:
                continue
            eligible.append(symbol)
        if pick not in eligible:
            continue
        entry = entry_bar[0]
        pick_return = exit_bar[0] / entry - 1.0
        member_returns = []
        hits = {threshold: 0 for threshold in HIT_THRESHOLDS}
        pick_hits = {}
        for symbol in eligible:
            entered = _bar(bars, symbol, entry_day)[0]
            exited = _bar(bars, symbol, exit_day)[0]
            member_returns.append(exited / entered - 1.0)
        vti_entry = _bar(bars, "VTI", entry_day)
        vti_exit = _bar(bars, "VTI", exit_day)
        vti_return = None
        if vti_entry and vti_exit and vti_entry[0] > 0:
            vti_return = vti_exit[0] / vti_entry[0] - 1.0
        close_path = []
        for step in range(horizon):
            bar = _bar(bars, pick, sessions[index + 1 + step])
            close_path.append(None if bar is None else bar[1])
        for threshold in HIT_THRESHOLDS:
            flag = path_hit(close_path, 0, horizon, entry, threshold)
            pick_hits[threshold] = bool(flag)
        rows.append(
            {
                "decision": sessions[index],
                "entry": entry_day,
                "exit": exit_day,
                "symbol": pick,
                "eligible": eligible,
                "pick_return": pick_return,
                "member_returns": member_returns,
                "eligible_average": mean(member_returns),
                "vti_return": vti_return,
                "path_hits": pick_hits,
                "entry_index": index + 1,
            }
        )
    return rows


def _member_hits(bars, sessions, row, horizon) -> dict[str, list[bool]]:
    entry_index = sessions.index(row["entry"])
    out = {symbol: [] for symbol in row["eligible"]}
    for symbol in row["eligible"]:
        entry = _bar(bars, symbol, row["entry"])[0]
        flags = []
        close_path = []
        for step in range(horizon):
            bar = _bar(bars, symbol, sessions[entry_index + step])
            close_path.append(None if bar is None else bar[1])
        for threshold in HIT_THRESHOLDS:
            flag = path_hit(close_path, 0, horizon, entry, threshold)
            flags.append(bool(flag))
        out[symbol] = flags
    return out


def summarize_horizon(
    rows: list[dict],
    bars,
    sessions: list[str],
    horizon: int,
    *,
    draws: int = BOOTSTRAP_DRAWS,
    seed: int = RANDOM_SEED,
) -> dict:
    if not rows:
        return {"days": 0}
    pick_returns = [row["pick_return"] for row in rows]
    eligible_gap = [row["pick_return"] - row["eligible_average"] for row in rows]
    vti_pairs = [(row["pick_return"], row["vti_return"]) for row in rows if row["vti_return"] is not None]
    vti_gap = [pick - bench for pick, bench in vti_pairs]
    random_means = []
    random_hit_rates = {threshold: [] for threshold in HIT_THRESHOLDS}
    hit_cache = [_member_hits(bars, sessions, row, horizon) for row in rows]
    pick_hit_rates = {
        threshold: mean(1.0 if row["path_hits"][threshold] else 0.0 for row in rows)
        for threshold in HIT_THRESHOLDS
    }
    rng = random.Random(seed)
    for _draw in range(draws):
        sampled = []
        hit_counts = {threshold: 0 for threshold in HIT_THRESHOLDS}
        for row, cache in zip(rows, hit_cache):
            choice = rng.randrange(len(row["eligible"]))
            sampled.append(row["member_returns"][choice])
            symbol = row["eligible"][choice]
            for threshold, flag in zip(HIT_THRESHOLDS, cache[symbol]):
                hit_counts[threshold] += 1 if flag else 0
        random_means.append(mean(sampled))
        for threshold in HIT_THRESHOLDS:
            random_hit_rates[threshold].append(hit_counts[threshold] / len(rows))
    pick_mean = mean(pick_returns)
    random_mean = mean(random_means)
    hit_p = {
        f"{int(threshold * 100)}": {
            "pick_rate": pick_hit_rates[threshold],
            "random_mean_rate": mean(random_hit_rates[threshold]),
            "excess": pick_hit_rates[threshold] - mean(random_hit_rates[threshold]),
            "p_value": p_value_ge(random_hit_rates[threshold], pick_hit_rates[threshold]),
        }
        for threshold in HIT_THRESHOLDS
    }
    terminal_hits = {}
    for threshold in HIT_THRESHOLDS:
        rate = mean(1.0 if value >= threshold else 0.0 for value in pick_returns)
        random_rates = []
        rng_terminal = random.Random(seed)
        for _draw in range(draws):
            count = 0
            for row in rows:
                choice = rng_terminal.randrange(len(row["eligible"]))
                count += 1 if row["member_returns"][choice] >= threshold else 0
            random_rates.append(count / len(rows))
        terminal_hits[f"{int(threshold * 100)}"] = {
            "pick_rate": rate,
            "random_mean_rate": mean(random_rates),
            "excess": rate - mean(random_rates),
            "p_value": p_value_ge(random_rates, rate),
        }
    vti_block = block_bootstrap_mean(vti_gap, VTI_BLOCK, draws, VTI_SEED) if vti_gap else []
    return {
        "days": len(rows),
        "choice_days": sum(1 for row in rows if len(row["eligible"]) >= 2),
        "mean_eligible_names": mean(len(row["eligible"]) for row in rows),
        "pick_mean": pick_mean,
        "pick_median": median(pick_returns),
        "random_mean": random_mean,
        "excess_vs_random_mean": pick_mean - random_mean,
        "p_value_vs_random": p_value_ge(random_means, pick_mean),
        "excess_vs_eligible_mean": mean(eligible_gap),
        "excess_vs_eligible_median": median(eligible_gap),
        "excess_vs_vti_mean": mean(vti_gap) if vti_gap else None,
        "excess_vs_vti_median": median(vti_gap) if vti_gap else None,
        "vti_days": len(vti_gap),
        "vti_block_p05": percentile(vti_block, 0.05) if vti_block else None,
        "path_hit_vs_random": hit_p,
        "terminal_hit_vs_random": terminal_hits,
        "signal_change_rate": signal_change_rate([row["symbol"] for row in rows]),
    }


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(q * (len(ordered) - 1))))
    return ordered[index]


def block_bootstrap_mean(values: list[float], block: int, draws: int, seed: int) -> list[float]:
    if not values:
        return []
    rng = random.Random(seed)
    width = len(values)
    block = max(1, min(block, width))
    last_start = width - block
    out = []
    for _draw in range(draws):
        sample: list[float] = []
        while len(sample) < width:
            start = rng.randrange(last_start + 1)
            sample.extend(values[start : start + block])
        out.append(mean(sample[:width]))
    return out


def same_close_bias(
    sessions: list[str],
    bars: dict[str, dict[str, tuple[float, float]]],
    rows: list[dict],
    horizon: int,
) -> float | None:
    """Mean of the forbidden close-to-close return minus the open-to-open return."""
    gaps = []
    for row in rows:
        decision_index = sessions.index(row["decision"])
        signal = _bar(bars, row["symbol"], row["decision"])
        later = _bar(bars, row["symbol"], sessions[decision_index + horizon])
        if signal is None or later is None or signal[1] <= 0:
            continue
        forbidden = later[1] / signal[1] - 1.0
        gaps.append(forbidden - row["pick_return"])
    if not gaps:
        return None
    return mean(gaps)


def trade_exits(
    sessions: list[str],
    bars: dict[str, dict[str, tuple[float, float]]],
    rows: list[dict],
    *,
    last_index: int,
) -> dict:
    """Per-pick exits inside the window. The stop sells at the next open. No sizing, no halt."""
    time_only = []
    stopped = []
    fantasy = []
    gaps = []
    primary = []
    for row in rows:
        entry_index = row["entry_index"]
        time_index = entry_index + MAX_HOLD_SESSIONS
        if time_index + 1 > last_index:
            continue
        symbol = row["symbol"]
        entry_bar = _bar(bars, symbol, sessions[entry_index])
        time_open = _bar(bars, symbol, sessions[time_index + 1])
        if entry_bar is None or time_open is None or entry_bar[0] <= 0:
            continue
        entry = entry_bar[0]
        stop_index = None
        for index in range(entry_index, time_index + 1):
            bar = _bar(bars, symbol, sessions[index])
            if bar is not None and bar[1] <= entry * (1 - STOP):
                stop_index = index
                break
        if stop_index is None:
            time_return = time_open[0] / entry - 1.0
            time_only.append(time_return)
            stopped.append(time_return)
            fantasy.append(time_return)
            primary.append(row["pick_return"])
            continue
        if stop_index + 1 > last_index:
            continue
        trigger = _bar(bars, symbol, sessions[stop_index])
        nxt = _bar(bars, symbol, sessions[stop_index + 1])
        if trigger is None or nxt is None or trigger[1] <= 0:
            continue
        time_only.append(time_open[0] / entry - 1.0)
        stopped.append(nxt[0] / entry - 1.0)
        fantasy.append(trigger[1] / entry - 1.0)
        gaps.append(nxt[0] / trigger[1] - 1.0)
        primary.append(row["pick_return"])
    if not time_only:
        return {"trades": 0}
    return {
        "trades": len(time_only),
        "mean_primary_20_session": mean(primary),
        "mean_time_exit_30_next_open": mean(time_only),
        "mean_stop_or_30_next_open": mean(stopped),
        "mean_stop_or_30_fantasy_close": mean(fantasy),
        "mean_overnight_gap_on_stops": mean(gaps) if gaps else None,
        "stopped_trades": len(gaps),
        "stop_minus_time_exit": mean(stopped) - mean(time_only),
        "gap_fill_minus_fantasy": mean(stopped) - mean(fantasy),
    }


def portfolio_book(
    sessions: list[str],
    bars: dict[str, dict[str, tuple[float, float]]],
    decisions: dict[int, str],
    *,
    window_start: str,
    window_end: str,
    clip: float,
    name_cap: float,
    sleeve: float,
    max_names: int,
    use_stop: bool,
    use_halt: bool,
    slip_bps: float,
) -> dict:
    """One NAV path. Buys and sells fill at opens. A stop is decided on the close."""
    start = sessions.index(window_start)
    end = sessions.index(window_end)
    cash = 1.0
    positions: dict[str, dict] = {}
    pending_buys: list[str] = []
    pending_sells: list[str] = []
    peak = 1.0
    halted = False
    buy_notional = 0.0
    sell_notional = 0.0
    nav = []
    slip = slip_bps / 10000

    def price(symbol: str, day: str, field: int) -> float | None:
        bar = _bar(bars, symbol, day)
        return None if bar is None else bar[field]

    def equity_of(day: str, field: int) -> float:
        gross = 0.0
        for symbol, position in positions.items():
            mark = price(symbol, day, field)
            if mark is None:
                mark = position["entry"]
            gross += position["qty"] * mark
        return cash + gross

    for index in range(start, end + 1):
        day = sessions[index]
        still_selling = []
        for symbol in pending_sells:
            if symbol not in positions:
                continue
            raw = price(symbol, day, 0)
            if raw is None:
                still_selling.append(symbol)
                continue
            position = positions.pop(symbol)
            fill = raw * (1 - slip)
            proceeds = position["qty"] * fill
            cash += proceeds
            sell_notional += proceeds
        pending_sells = still_selling
        if not halted:
            for symbol in pending_buys:
                if symbol in positions or len(positions) >= max_names:
                    continue
                raw = price(symbol, day, 0)
                if raw is None or raw <= 0:
                    continue
                equity = equity_of(day, 0)
                gross = equity - cash
                dollars = min(cash, equity * clip, equity * name_cap, max(0.0, equity * sleeve - gross))
                if dollars <= 1e-8:
                    continue
                fill = raw * (1 + slip)
                cash -= dollars
                buy_notional += dollars
                positions[symbol] = {"qty": dollars / fill, "entry": fill, "entry_index": index}
        pending_buys = []
        marked = equity_of(day, 1)
        nav.append(marked)
        peak = max(peak, marked)
        if use_halt and marked <= peak * (1 - HALT):
            halted = True
        for symbol, position in list(positions.items()):
            if any(item == symbol for item in pending_sells):
                continue
            raw = price(symbol, day, 1)
            reason = False
            if use_stop and raw is not None and raw <= position["entry"] * (1 - STOP):
                reason = True
            elif index - position["entry_index"] >= MAX_HOLD_SESSIONS:
                reason = True
            if reason and index < end:
                pending_sells.append(symbol)
        if not halted and index in decisions and len(positions) < max_names:
            pending_buys.append(decisions[index])
    if positions:
        day = sessions[end]
        for symbol in list(positions):
            raw = price(symbol, day, 1)
            if raw is None:
                continue
            position = positions.pop(symbol)
            fill = raw * (1 - slip)
            proceeds = position["qty"] * fill
            cash += proceeds
            sell_notional += proceeds
        if nav:
            nav[-1] = cash
    parts = turnover_parts(buy_notional, sell_notional, nav)
    peak_nav = nav[0] if nav else 1.0
    max_dd = 0.0
    for value in nav:
        peak_nav = max(peak_nav, value)
        if peak_nav > 0:
            max_dd = max(max_dd, (peak_nav - value) / peak_nav)
    total = (nav[-1] / nav[0] - 1.0) if len(nav) >= 2 else 0.0
    return {
        "return": total,
        "max_drawdown": max_dd,
        "halted": halted,
        "turnover": parts,
    }
