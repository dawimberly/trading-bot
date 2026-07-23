"""Paper-aggressive crypto sleeve v2: mean-reversion dips + momentum breakouts."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

import config
from modules.pipeline_strategies import regime_entries_paused

logger = logging.getLogger(__name__)

RSI_PERIOD = 14
RSI_MIN = 32.0
RSI_MAX = 42.0
SMA_PERIOD = 10
DROP_LOOKBACK = 4
DROP_PCT = -0.03
BREAKOUT_PERIOD = 20
RANGE_SPIKE_MULT = 1.35
MOMENTUM_BARS = 4
MOMENTUM_MIN = 0.01
TAKE_PROFIT_PCT = 0.08
STOP_LOSS_PCT = 0.05
TIMEOUT_BARS = 10
MAX_OPEN = 3  # fallback; paper uses effective_crypto_max_pairs()


def _max_open_positions() -> int:
    return config.effective_crypto_max_pairs()
MAX_ENTRIES_PER_BAR = 2


def _compute_rsi(close: pd.Series, period: int = RSI_PERIOD) -> float | None:
    if len(close) < period + 1:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain.iloc[-1] / (avg_loss.iloc[-1] + 1e-12)
    rsi = 100 - (100 / (1 + rs))
    return float(rsi) if np.isfinite(rsi) else None


def crypto_v2_universe(data_columns) -> list[str]:
    """Major Alpaca crypto pairs present in data (paper v2 list)."""
    wanted = config.paper_crypto_v2_symbols()
    cols = set(data_columns)
    return [s for s in wanted if s in cols]


def crypto_market_filter(data) -> tuple[bool, str]:
    """
    Soft crypto-specific gate (replaces broad SPY block):
    BTC trend + aggregate alt 10d return + crash guard.
    """
    cols = crypto_v2_universe(data.columns)
    if not cols:
        return False, "no_universe"

    btc = "BTC-USD"
    if btc in data.columns:
        prices = data[btc].dropna()
        if len(prices) >= 20:
            ma20 = float(prices.rolling(20).mean().iloc[-1])
            current = float(prices.iloc[-1])
            btc_trend_ok = current >= ma20 * 0.96
            crash = len(prices) >= 6 and (current / float(prices.iloc[-6]) - 1) < -0.18
        else:
            btc_trend_ok = True
            crash = False
    else:
        btc_trend_ok = True
        crash = False

    alt_rets = []
    for sym in cols:
        if sym == btc:
            continue
        p = data[sym].dropna()
        if len(p) >= 10:
            alt_rets.append(float(p.iloc[-1] / p.iloc[-10] - 1))
    alt_trend_ok = (float(np.mean(alt_rets)) > -0.10) if alt_rets else True

    if crash:
        return False, "btc_crash_guard"
    if btc_trend_ok or alt_trend_ok:
        return True, "crypto_trend_ok"
    return False, "crypto_trend_weak"


@dataclass
class CryptoV2State:
    open_positions: dict = field(default_factory=dict)
    closed_trades: list = field(default_factory=list)


def _book(executor) -> CryptoV2State:
    book = getattr(executor, "_crypto_v2_book", None)
    if book is None:
        book = CryptoV2State()
        executor._crypto_v2_book = book
    return book


def mean_reversion_signal(data, symbol: str) -> tuple[bool, dict]:
    prices = data[symbol].dropna()
    if len(prices) < max(SMA_PERIOD, DROP_LOOKBACK, RSI_PERIOD) + 2:
        return False, {}
    rsi = _compute_rsi(prices)
    sma = float(prices.rolling(SMA_PERIOD).mean().iloc[-1])
    current = float(prices.iloc[-1])
    ret_n = float(current / prices.iloc[-1 - MOMENTUM_BARS] - 1)
    if rsi is None:
        return False, {}
    ok = (
        RSI_MIN <= rsi <= RSI_MAX
        and current < sma
        and ret_n <= DROP_PCT
    )
    meta = {"entry_type": "mean_reversion", "rsi": round(rsi, 1), "ret_4d": round(ret_n, 4)}
    return ok, meta


def breakout_signal(data, symbol: str) -> tuple[bool, dict]:
    prices = data[symbol].dropna()
    if len(prices) < BREAKOUT_PERIOD + MOMENTUM_BARS + 2:
        return False, {}
    window = prices.tail(BREAKOUT_PERIOD + 1)
    current = float(window.iloc[-1])
    prior_high = float(window.iloc[:-1].max())
    ret_n = float(current / prices.iloc[-1 - MOMENTUM_BARS] - 1)
    ranges = prices.pct_change().abs().tail(BREAKOUT_PERIOD)
    avg_range = float(ranges.mean()) if len(ranges) else 0.0
    last_range = float(abs(prices.pct_change().iloc[-1]))
    range_spike = last_range >= avg_range * RANGE_SPIKE_MULT if avg_range > 0 else False
    ok = current > prior_high and range_spike and ret_n >= MOMENTUM_MIN
    meta = {
        "entry_type": "breakout",
        "ret_4d": round(ret_n, 4),
        "range_spike": round(last_range / (avg_range + 1e-9), 2),
    }
    return ok, meta


def _process_exits(executor, data, prices_row, now) -> int:
    st = _book(executor)
    exits = 0
    for symbol, pos in list(st.open_positions.items()):
        if symbol not in data.columns:
            continue
        price = float(prices_row.get(symbol) or 0)
        if price <= 0:
            continue
        entry = float(pos["entry_price"])
        pnl_pct = price / entry - 1
        bars = now - int(pos["entry_bar"])
        reason = None
        if pnl_pct >= TAKE_PROFIT_PCT:
            reason = "take_profit"
        elif pnl_pct <= -STOP_LOSS_PCT:
            reason = "stop_loss"
        elif bars >= TIMEOUT_BARS:
            reason = "timeout"
        if not reason:
            continue
        qty = executor.portfolio.positions.get(symbol, 0)
        if qty > 0 and hasattr(executor, "execute_full_exit"):
            executor.execute_full_exit(symbol)
        elif qty > 0:
            executor.execute_order(symbol, "sell", notional=qty * price)
        st.closed_trades.append(
            {
                **pos,
                "exit_price": round(price, 4),
                "pnl_pct": round(pnl_pct * 100, 2),
                "exit_reason": reason,
                "bars_held": bars,
            }
        )
        st.open_positions.pop(symbol, None)
        exits += 1
    return exits


def run_crypto_dual_sleeve(
    data,
    executor,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_bars=None,
    max_trades: int = MAX_ENTRIES_PER_BAR,
    log_fn=None,
    portfolio_manager=None,
    volatility=None,
    spacex_snapshot=None,
):
    """Dual-entry crypto sleeve — paper aggressive only."""
    del spacex_snapshot
    att = getattr(executor, "_attribution", None)
    if not config.effective_crypto_v2_enabled():
        return 0
    if regime_entries_paused(regime, data):
        if att:
            att.record_crypto_reject("regime_paused")
        return 0

    prices_row = data.iloc[-1]
    trades = _process_exits(executor, data, prices_row, now)

    st = _book(executor)
    max_open = _max_open_positions()
    if len(st.open_positions) >= max_open:
        if att:
            att.record_crypto_reject("max_pairs")
        return trades

    notional = None
    if hasattr(executor, "compute_crypto_notional"):
        notional = executor.compute_crypto_notional()
    if notional is None:
        if att:
            att.record_crypto_reject("no_room")
        return trades

    allowed, gate_reason = crypto_market_filter(data)
    if not allowed:
        if att:
            att.record_crypto_reject(gate_reason or "vol_gate")
        return trades
    if att:
        att.record_crypto_vol_gate_pass()

    slots = max_open - len(st.open_positions)
    max_new = min(max_trades, slots)
    intents = []
    symbols = crypto_v2_universe(data.columns)
    scan_count = 0
    for symbol in symbols:
        if len(intents) >= max_new:
            break
        if symbol in st.open_positions:
            continue
        for signal_fn in (mean_reversion_signal, breakout_signal):
            ok, meta = signal_fn(data, symbol)
            if not ok:
                continue
            scan_count += 1
            key = f"{symbol}:{meta['entry_type']}"
            if cooldown_bars is not None and key in pair_cooldown:
                if now - pair_cooldown[key] < cooldown_bars:
                    continue
            intents.append({"symbol": symbol, "pair_key": key, **meta})
            break
    if att:
        att.record_crypto_scan_signals(scan_count)
        att.record_crypto_intents(len(intents))

    for intent in intents:
        symbol = intent["symbol"]
        order = executor.execute_order(
            symbol, "buy", notional=notional, sleeve="Crypto", strategy="crypto"
        )
        if order is None:
            if att:
                att.record_crypto_reject("min_notional")
            continue
        if not hasattr(executor, "portfolio"):
            continue
        qty = executor.portfolio.positions.get(symbol, 0)
        if qty <= 0:
            if att:
                att.record_crypto_reject("min_notional")
            continue
        entry_price = float(prices_row.get(symbol) or 0)
        st.open_positions[symbol] = {
            "symbol": symbol,
            "entry_type": intent["entry_type"],
            "entry_price": entry_price,
            "entry_bar": now,
            "notional": notional,
            "gate": gate_reason,
            **{k: intent[k] for k in ("rsi", "ret_4d", "range_spike") if k in intent},
        }
        pair_cooldown[intent["pair_key"]] = now
        trades += 1
        if att:
            att.on_crypto_entry(intent["pair_key"], symbol=symbol)
        if portfolio_manager:
            portfolio_manager.add_position(intent["pair_key"], 0, 0)
        if log_fn:
            log_fn(
                symbol,
                "buy",
                regime,
                intent["pair_key"],
                0.0,
                notional,
                pair_msg=f"crypto_v2 {intent['entry_type']}",
            )
    return trades


def summarize_crypto_v2_trades_from_executor(executor) -> dict:
    return summarize_crypto_v2_trades(getattr(executor, "_crypto_v2_book", None))


def summarize_crypto_v2_trades(book: CryptoV2State | None) -> dict:
    if book is None:
        return {
            "trade_count": 0,
            "win_rate_pct": 0.0,
            "avg_pnl_pct": 0.0,
            "mean_reversion_trades": 0,
            "breakout_trades": 0,
            "samples": [],
        }
    closed = list(book.closed_trades)
    if not closed:
        return {
            "trade_count": 0,
            "win_rate_pct": 0.0,
            "avg_pnl_pct": 0.0,
            "mean_reversion_trades": 0,
            "breakout_trades": 0,
            "open_positions": len(book.open_positions),
            "samples": [],
        }
    pnls = [float(t["pnl_pct"]) for t in closed]
    wins = sum(1 for p in pnls if p > 0)
    mr = sum(1 for t in closed if t.get("entry_type") == "mean_reversion")
    bo = sum(1 for t in closed if t.get("entry_type") == "breakout")
    samples = sorted(closed, key=lambda t: abs(t["pnl_pct"]), reverse=True)[:6]
    return {
        "trade_count": len(closed),
        "win_rate_pct": round(100 * wins / len(closed), 1),
        "avg_pnl_pct": round(float(np.mean(pnls)), 2),
        "mean_reversion_trades": mr,
        "breakout_trades": bo,
        "open_positions": len(book.open_positions),
        "samples": samples,
    }
