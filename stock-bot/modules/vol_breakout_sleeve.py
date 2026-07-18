"""ATR volatility-breakout sleeve — paper-only.

Entry: ATR expansion (current ATR vs recent baseline) plus a directional
break of the recent high, confirmed by RVOL and multi-timeframe alignment.
Size to ≤1% equity risk with an ATR stop and conviction scaling.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from modules.safe_io import read_json_file, write_json_file

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
_STATE_PATH = ROOT / "data" / "vol_breakout_state.json"
_SLEEVE = "VOL_BO"


def _state_path() -> Path:
    raw = getattr(config, "VOL_BREAKOUT_STATE_FILE", None)
    if raw:
        p = Path(str(raw))
        return p if p.is_absolute() else ROOT / p
    return _STATE_PATH


def _load_state() -> dict[str, Any]:
    raw = read_json_file(_state_path()) or {}
    return raw if isinstance(raw, dict) else {}


def _save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_file(path, state)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def vol_breakout_enabled(*, live: bool | None = None) -> bool:
    """Paper-only sleeve. Live always off (no live opt-in yet)."""
    if not bool(getattr(config, "VOL_BREAKOUT_ENABLED", True)):
        return False
    if live is True:
        return False
    return bool(
        config.PAPER_TRADING
        or config.paper_aggressive_context()
        or config.backtest_paper_sleeves_context()
        or config.is_realistic_research_active()
    )


def _risk_pct() -> float:
    # Hard ceiling 1% per trade.
    return _clamp(float(getattr(config, "VOL_BREAKOUT_RISK_PCT", 0.01)), 0.002, 0.01)


def _max_size_pct() -> float:
    return _clamp(float(getattr(config, "VOL_BREAKOUT_MAX_SIZE_PCT", 0.08)), 0.02, 0.15)


def _min_size_pct() -> float:
    return _clamp(float(getattr(config, "VOL_BREAKOUT_MIN_SIZE_PCT", 0.03)), 0.01, 0.10)


def _rr_target() -> float:
    return max(1.0, float(getattr(config, "VOL_BREAKOUT_RR", 1.5)))


def _atr_stop_mult() -> float:
    return max(
        0.5,
        float(
            getattr(
                config,
                "VOL_BREAKOUT_ATR_MULT",
                getattr(config, "ATR_RISK_MULTIPLE", 2.0),
            )
        ),
    )


def _expand_mult() -> float:
    return max(1.05, float(getattr(config, "VOL_BREAKOUT_ATR_EXPAND_MULT", 1.5)))


def _baseline_bars() -> int:
    return max(5, int(getattr(config, "VOL_BREAKOUT_ATR_BASELINE_BARS", 20)))


def _breakout_lookback() -> int:
    return max(5, int(getattr(config, "VOL_BREAKOUT_BREAKOUT_LOOKBACK", 20)))


def _rvol_min() -> float:
    return max(
        1.0,
        float(
            getattr(
                config,
                "VOL_BREAKOUT_RVOL_MIN",
                getattr(config, "RVOL_MIN_THRESHOLD", 2.0),
            )
        ),
    )


def _sleeve_cap_pct() -> float:
    return _clamp(float(getattr(config, "VOL_BREAKOUT_CAP_PCT", 0.12)), 0.03, 0.25)


def _max_positions() -> int:
    return max(1, int(getattr(config, "VOL_BREAKOUT_MAX_POSITIONS", 3)))


def _min_conviction() -> float:
    return _clamp(float(getattr(config, "VOL_BREAKOUT_MIN_CONVICTION", 0.45)), 0.0, 0.95)


def _atr_at(series, end_idx: int, period: int) -> float | None:
    """ATR proxy (|Δclose| rolling mean) ending at end_idx inclusive."""
    import numpy as np

    if end_idx < period:
        return None
    window = series.iloc[end_idx - period : end_idx + 1]
    if len(window) < period + 1:
        return None
    tr = window.diff().abs()
    atr = float(tr.iloc[1:].mean())
    if not np.isfinite(atr) or atr <= 0:
        return None
    return atr


def measure_atr_expansion(
    data,
    symbol: str,
    *,
    period: int | None = None,
    baseline_bars: int | None = None,
    end_idx: int | None = None,
) -> dict[str, Any] | None:
    """Current ATR vs mean of prior baseline ATRs (expansion ratio)."""
    import numpy as np

    sym = config.normalize_symbol(symbol)
    if data is None or not hasattr(data, "columns") or sym not in data.columns:
        return None
    period = max(2, int(period or getattr(config, "ATR_PERIOD", 14)))
    baseline = max(5, int(baseline_bars if baseline_bars is not None else _baseline_bars()))
    prices = data[sym].dropna()
    if len(prices) < period + baseline + 2:
        return None

    if end_idx is None:
        end_idx = len(prices) - 1
    end_idx = min(end_idx, len(prices) - 1)
    need = period + baseline + 1
    if end_idx < need:
        return None

    cur = _atr_at(prices, end_idx, period)
    if cur is None:
        return None

    prior_atrs: list[float] = []
    for j in range(end_idx - baseline, end_idx):
        a = _atr_at(prices, j, period)
        if a is not None:
            prior_atrs.append(a)
    if len(prior_atrs) < max(3, baseline // 3):
        return None
    base = float(sum(prior_atrs) / len(prior_atrs))
    if base <= 1e-12:
        return None
    ratio = cur / base
    if not np.isfinite(ratio):
        return None
    px = float(prices.iloc[end_idx])
    return {
        "symbol": sym,
        "atr": round(cur, 4),
        "atr_baseline": round(base, 4),
        "atr_expand": round(float(ratio), 3),
        "price": px,
        "expanded": float(ratio) >= _expand_mult(),
    }


def _price_breakout(data, symbol: str, *, lookback: int | None = None, end_idx: int | None = None) -> dict[str, Any] | None:
    """Close above prior N-day high (directional vol breakout)."""
    sym = config.normalize_symbol(symbol)
    if data is None or not hasattr(data, "columns") or sym not in data.columns:
        return None
    lookback = max(5, int(lookback if lookback is not None else _breakout_lookback()))
    prices = data[sym].dropna()
    if end_idx is None:
        end_idx = len(prices) - 1
    end_idx = min(end_idx, len(prices) - 1)
    if end_idx < lookback + 1:
        return None
    px = float(prices.iloc[end_idx])
    prior = prices.iloc[end_idx - lookback : end_idx]
    if prior.empty or px <= 0:
        return None
    high = float(prior.max())
    if high <= 0:
        return None
    return {
        "price": px,
        "breakout_high": round(high, 4),
        "broke_out": px > high,
        "breakout_pct": round((px / high - 1.0) * 100.0, 2),
    }


def collect_vol_breakout_signals(
    data,
    *,
    limit: int = 12,
    require_mtf: bool = True,
    require_conviction: bool = True,
    require_rvol: bool = True,
    regime: str | None = None,
) -> list[dict[str, Any]]:
    """ATR expansion + breakout setups with RVOL / MTF / conviction gates."""
    if data is None or not hasattr(data, "columns"):
        return []

    min_rvol = _rvol_min()
    symbols = [
        str(c)
        for c in data.columns
        if config._nyse_eligible_symbol(str(c))
    ][:80]

    scored: list[dict[str, Any]] = []
    for sym in symbols:
        expand = measure_atr_expansion(data, sym)
        if not expand or not expand.get("expanded"):
            continue
        brk = _price_breakout(data, sym)
        if not brk or not brk.get("broke_out"):
            continue

        rvol = None
        if require_rvol and (
            config.effective_rvol_scanner_enabled() or config.backtest_paper_sleeves_context()
        ):
            try:
                from modules.volume_analysis import calculate_rvol

                rvol = calculate_rvol(data, sym)
            except Exception as exc:
                logger.debug("vol breakout RVOL skipped for %s: %s", sym, exc)
                rvol = None
            # In live/paper with volume feed: require RVOL. If unavailable, soft-pass
            # only when ATR expansion is strong (>= expand_mult * 1.15).
            if rvol is None:
                if float(expand["atr_expand"]) < _expand_mult() * 1.15:
                    continue
            elif float(rvol) < min_rvol:
                continue
        elif require_rvol:
            # Scanner off — still prefer expansion strength.
            if float(expand["atr_expand"]) < _expand_mult() * 1.1:
                continue

        mtf_ok = True
        mtf_align = None
        if require_mtf and config.effective_multi_timeframe_enabled():
            try:
                from modules.multi_timeframe import (
                    check_multi_timeframe_alignment,
                    multi_timeframe_alignment_ok,
                )

                mtf_ok = multi_timeframe_alignment_ok(sym, data)
                mtf_align = check_multi_timeframe_alignment(sym, data)
            except Exception as exc:
                logger.debug("vol breakout MTF skipped for %s: %s", sym, exc)
                mtf_ok = True
        if not mtf_ok:
            continue

        conviction = None
        if require_conviction and config.effective_conviction_sizing_enabled():
            try:
                from modules.risk_management import compute_conviction_score

                conviction = compute_conviction_score(sym, data, regime, sleeve="nyse")
            except Exception as exc:
                logger.debug("vol breakout conviction skipped for %s: %s", sym, exc)
                conviction = None
        if conviction is not None and float(conviction) < _min_conviction():
            continue

        scored.append(
            {
                "symbol": sym,
                "price": expand["price"],
                "atr": expand["atr"],
                "atr_baseline": expand["atr_baseline"],
                "atr_expand": expand["atr_expand"],
                "breakout_high": brk["breakout_high"],
                "breakout_pct": brk["breakout_pct"],
                "rvol": rvol,
                "mtf_align": mtf_align,
                "conviction": conviction,
                "type": "atr_vol_breakout",
                "sleeve": _SLEEVE,
            }
        )

    scored.sort(
        key=lambda r: (
            float(r.get("atr_expand") or 0),
            float(r.get("rvol") or 0),
            float(r.get("breakout_pct") or 0),
        ),
        reverse=True,
    )
    return scored[:limit]


def size_vol_breakout_trade(
    equity: float,
    symbol: str,
    data,
    *,
    price: float | None = None,
    conviction: float | None = None,
    atr: float | None = None,
) -> dict[str, Any]:
    """≤1% risk / ATR stop; notional capped; conviction soft-scale."""
    from modules.risk_management import calculate_atr, conviction_scale

    equity = float(equity)
    sym = config.normalize_symbol(symbol)
    atr_val = atr if atr is not None else calculate_atr(data, sym)
    px = price
    if px is None or px <= 0:
        if data is not None and hasattr(data, "columns") and sym in data.columns:
            series = data[sym].dropna()
            if not series.empty:
                px = float(series.iloc[-1])
    if px is None or px <= 0:
        return {"ok": False, "reason": "no_price", "notional": 0.0}

    atr_mult = _atr_stop_mult()
    risk_pct = _risk_pct()
    max_pct = _max_size_pct()
    min_pct = _min_size_pct()
    risk_dollars = equity * risk_pct

    if atr_val is None or atr_val <= 0:
        stop_dist = px * 0.02 * atr_mult / 2.0
        method = "pct_fallback"
    else:
        stop_dist = float(atr_val) * atr_mult
        method = "atr"

    if stop_dist <= 0:
        return {"ok": False, "reason": "bad_stop", "notional": 0.0}

    shares = risk_dollars / stop_dist
    notional = round(shares * px, 2)
    notional = min(notional, round(equity * max_pct, 2))

    min_n = round(equity * min_pct, 2)
    if notional < min_n and equity >= 200:
        lift = min(min_n, round(risk_dollars * px / max(stop_dist * 0.75, px * 0.005), 2))
        notional = min(max(notional, lift), round(equity * max_pct, 2))

    if conviction is not None and config.effective_conviction_sizing_enabled():
        scale = conviction_scale(float(conviction), scale_band=(0.70, 1.10))
        notional = round(notional * scale, 2)
        notional = min(notional, round(equity * max_pct, 2))

    # Re-assert 1% risk ceiling after conviction lift.
    max_by_risk = round(risk_dollars * px / stop_dist, 2)
    notional = min(notional, max_by_risk, round(equity * max_pct, 2))
    notional = min(notional, config.effective_max_notional_per_order(equity))

    floor = config.effective_min_notional(equity)
    if notional < floor:
        return {
            "ok": False,
            "reason": "below_min_notional",
            "notional": notional,
            "stop_distance": round(stop_dist, 4),
            "price": px,
            "atr": atr_val,
            "method": method,
        }

    stop_price = round(max(0.01, px - stop_dist), 4)
    target_price = round(px + stop_dist * _rr_target(), 4)
    return {
        "ok": True,
        "symbol": sym,
        "notional": round(notional, 2),
        "price": round(px, 4),
        "atr": atr_val,
        "stop_distance": round(stop_dist, 4),
        "stop_price": stop_price,
        "target_price": target_price,
        "risk_pct": risk_pct,
        "max_size_pct": max_pct,
        "rr": _rr_target(),
        "method": method,
        "conviction": conviction,
    }


def _open_book(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    book = state.get("open") or {}
    return book if isinstance(book, dict) else {}


def _sleeve_open_value(executor, book: dict[str, dict[str, Any]]) -> float:
    total = 0.0
    for sym, meta in book.items():
        try:
            pos = executor._find_position(sym) if hasattr(executor, "_find_position") else None
            if pos is not None:
                qty = abs(float(getattr(pos, "qty", 0) or 0))
                px = float(getattr(pos, "current_price", 0) or 0)
                if qty > 0 and px > 0:
                    total += qty * px
                    continue
        except Exception as exc:
            logger.debug("vol breakout soft-fail: %s", exc)
        total += float((meta or {}).get("notional") or 0)
    return round(total, 2)


def _manage_open_exits(executor, state: dict[str, Any], *, journal=None) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    book = _open_book(state)
    if not book:
        return actions

    changed = False
    for sym in list(book.keys()):
        meta = book.get(sym) or {}
        try:
            pos = executor._find_position(sym) if hasattr(executor, "_find_position") else None
        except Exception:
            pos = None
        if pos is None or float(getattr(pos, "qty", 0) or 0) <= 0:
            book.pop(sym, None)
            changed = True
            continue

        current = float(getattr(pos, "current_price", 0) or 0)
        entry = float(meta.get("entry_price") or getattr(pos, "avg_entry_price", 0) or 0)
        stop = float(meta.get("stop_price") or 0)
        target = float(meta.get("target_price") or 0)
        if current <= 0 or entry <= 0:
            continue

        reason = None
        if stop > 0 and current <= stop:
            reason = "vol_bo_stop"
        elif target > 0 and current >= target:
            reason = "vol_bo_target"

        if not reason:
            peak = max(float(meta.get("peak") or entry), current)
            meta["peak"] = peak
            book[sym] = meta
            continue

        try:
            order = executor.execute_full_exit(sym, reason=reason, sleeve=_SLEEVE)
            ok = bool(order) and (
                executor.order_filled(order) if hasattr(executor, "order_filled") else True
            )
        except Exception as exc:
            logger.warning("vol breakout exit failed %s: %s", sym, exc)
            ok = False

        act = {
            "action": "sell",
            "symbol": sym,
            "reason": reason,
            "ok": ok,
            "price": current,
            "entry": entry,
            "pnl_pct": round((current - entry) / entry, 4) if entry else None,
        }
        actions.append(act)
        if ok:
            book.pop(sym, None)
            changed = True
            if journal and hasattr(journal, "log_exit"):
                try:
                    journal.log_exit(
                        sym,
                        "sell",
                        reason,
                        float(getattr(executor._get_account(), "equity", 0) or 0),
                    )
                except Exception as exc:
                    logger.debug("vol breakout soft-fail: %s", exc)
            try:
                from modules.strategy_performance import record_closed_trade

                record_closed_trade(
                    "vol_breakout",
                    symbol=sym,
                    pnl=float(act.get("pnl_pct") or 0) * float(meta.get("notional") or 0),
                    pnl_pct=float(act.get("pnl_pct") or 0) * 100.0,
                    notional=float(meta.get("notional") or 0),
                    source="paper" if config.PAPER_TRADING else "live",
                    tags=["vol_breakout"],
                )
            except Exception as exc:
                logger.debug("vol breakout trade record skipped: %s", exc)

    if changed:
        state["open"] = book
        state["updated_at"] = _now_iso()
        _save_state(state)
    else:
        state["open"] = book
    return actions


def run_vol_breakout_cycle(
    data,
    executor,
    *,
    regime: str = "",
    market_open: bool = True,
    live: bool = False,
    journal=None,
    yield_gated: bool = False,
) -> dict[str, Any]:
    """Scan / size / enter ATR vol breakouts; manage stop & RR exits."""
    result: dict[str, Any] = {
        "enabled": False,
        "live": False,
        "signals": [],
        "entries": [],
        "exits": [],
        "skipped": None,
    }
    if live:
        result["skipped"] = "paper_only"
        return result
    if not vol_breakout_enabled(live=False):
        result["skipped"] = "disabled"
        return result
    if not market_open:
        result["skipped"] = "market_closed"
        return result
    if config.effective_yield_gate(yield_gated, regime=regime):
        result["skipped"] = "yield_gate"
        return result
    if "RHYME_B" in str(regime or "").upper():
        result["skipped"] = "regime_b"
        return result

    result["enabled"] = True
    state = _load_state()
    books = state.get("books") if isinstance(state.get("books"), dict) else {}
    book_state = books.get("paper") if isinstance(books.get("paper"), dict) else {"open": {}}
    if "open" not in book_state:
        book_state = {"open": dict(book_state)} if book_state else {"open": {}}

    exits = _manage_open_exits(executor, book_state, journal=journal)
    result["exits"] = exits

    try:
        account = executor._get_account()
        equity = float(account.equity)
        cash = float(account.cash)
    except Exception as exc:
        result["skipped"] = f"account_error:{exc}"
        return result

    open_book = _open_book(book_state)
    cap = round(equity * _sleeve_cap_pct(), 2)
    open_val = _sleeve_open_value(executor, open_book)
    room = round(cap - open_val, 2)
    max_pos = _max_positions()
    result["cap"] = cap
    result["open_value"] = open_val
    result["room"] = room
    result["open_count"] = len(open_book)

    if len(open_book) >= max_pos or room < config.effective_min_notional(equity):
        books["paper"] = book_state
        state["books"] = books
        state["updated_at"] = _now_iso()
        _save_state(state)
        result["signals"] = collect_vol_breakout_signals(data, limit=8, regime=regime)
        result["skipped"] = (
            "no_room" if room < config.effective_min_notional(equity) else "max_positions"
        )
        return result

    signals = collect_vol_breakout_signals(data, limit=8, regime=regime)
    result["signals"] = signals

    held = set(open_book.keys())
    try:
        for pos in executor._get_positions():
            held.add(config.normalize_symbol(pos.symbol))
    except Exception as exc:
        logger.debug("vol breakout soft-fail: %s", exc)

    entries: list[dict[str, Any]] = []
    for sig in signals:
        if len(open_book) >= max_pos:
            break
        sym = sig["symbol"]
        if sym in held:
            continue
        sizing = size_vol_breakout_trade(
            equity,
            sym,
            data,
            price=float(sig.get("price") or 0) or None,
            conviction=sig.get("conviction"),
            atr=sig.get("atr"),
        )
        if not sizing.get("ok"):
            continue
        notional = min(float(sizing["notional"]), room, cash * 0.95)
        if notional < config.effective_min_notional(equity):
            continue
        try:
            order = executor.execute_order(
                sym,
                "buy",
                notional=notional,
                reason=f"vol_bo_x{sig.get('atr_expand')}",
                sleeve=_SLEEVE,
            )
            ok = bool(order) and (
                executor.order_filled(order) if hasattr(executor, "order_filled") else True
            )
        except Exception as exc:
            logger.warning("vol breakout entry failed %s: %s", sym, exc)
            ok = False

        entry_rec = {
            "action": "buy",
            "symbol": sym,
            "notional": notional,
            "ok": ok,
            "atr_expand": sig.get("atr_expand"),
            "rvol": sig.get("rvol"),
            "stop": sizing.get("stop_price"),
            "target": sizing.get("target_price"),
            "conviction": sig.get("conviction"),
            "mtf": sig.get("mtf_align"),
        }
        entries.append(entry_rec)
        if ok:
            open_book[sym] = {
                "entry_ts": _now_iso(),
                "entry_price": sizing.get("price"),
                "notional": notional,
                "stop_price": sizing.get("stop_price"),
                "target_price": sizing.get("target_price"),
                "atr": sizing.get("atr"),
                "atr_expand": sig.get("atr_expand"),
                "rvol": sig.get("rvol"),
                "breakout_high": sig.get("breakout_high"),
                "conviction": sig.get("conviction"),
                "peak": sizing.get("price"),
            }
            held.add(sym)
            room = round(room - notional, 2)
            cash = max(0.0, cash - notional)
            if journal and hasattr(journal, "log_signal"):
                try:
                    journal.log_signal(
                        sym,
                        "buy",
                        regime,
                        f"vol_bo:{sym}",
                        float(sig.get("atr_expand") or 0),
                        equity,
                        notional,
                    )
                except Exception as exc:
                    logger.debug("vol breakout soft-fail: %s", exc)

    book_state["open"] = open_book
    books["paper"] = book_state
    state["books"] = books
    state["last_signals"] = [
        {
            "symbol": s.get("symbol"),
            "atr_expand": s.get("atr_expand"),
            "rvol": s.get("rvol"),
            "price": s.get("price"),
            "conviction": s.get("conviction"),
            "mtf_align": s.get("mtf_align"),
        }
        for s in signals[:10]
    ]
    state["updated_at"] = _now_iso()
    _save_state(state)
    result["entries"] = entries
    return result


# --- Daily backtest proxy -----------------------------------------------------


def _daily_rvol_proxy(closes, i: int, lookback: int = 10) -> float | None:
    if i < lookback + 1:
        return None
    rets = []
    for j in range(i - lookback, i + 1):
        prev = float(closes.iloc[j - 1])
        cur = float(closes.iloc[j])
        if prev > 0:
            rets.append(abs(cur / prev - 1.0))
    if len(rets) < lookback:
        return None
    current = rets[-1]
    avg = sum(rets[:-1]) / max(1, len(rets) - 1)
    if avg <= 1e-12:
        return None
    return round(current / avg, 3)


def backtest_vol_breakout_candidates(window, i: int, *, limit: int = 5) -> list[dict[str, Any]]:
    """Daily proxy: ATR expansion + break prior N-day high + RVOL proxy."""
    if window is None or not hasattr(window, "columns"):
        return []
    min_rvol = _rvol_min()
    lookback = _breakout_lookback()
    cols = [
        str(c)
        for c in window.columns
        if config._nyse_eligible_symbol(str(c))
    ][:40]
    scored: list[dict[str, Any]] = []
    if i < lookback + 2 or i >= len(window):
        return []
    # Point-in-time slice so ATR/breakout never peek ahead.
    hist = window.iloc[: i + 1]
    for sym in cols:
        series = hist[sym].dropna()
        if len(series) < lookback + _baseline_bars() + int(getattr(config, "ATR_PERIOD", 14)) + 2:
            continue
        try:
            px = float(hist[sym].iloc[-1])
        except Exception:
            continue
        if not (px > 0) or (hasattr(px, "__float__") and px != px):  # NaN guard
            continue
        import math

        if math.isnan(px):
            continue

        expand = measure_atr_expansion(hist, sym)
        if not expand or not expand.get("expanded"):
            continue
        brk = _price_breakout(hist, sym)
        if not brk or not brk.get("broke_out"):
            continue

        rvol = _daily_rvol_proxy(series, len(series) - 1)
        if rvol is None:
            rvol = round(1.0 + float(expand["atr_expand"]) * 0.4, 3)
        if rvol < min_rvol * 0.85:
            continue

        ma = series.tail(20)
        if len(ma) >= 10 and px < float(ma.mean()):
            continue

        scored.append(
            {
                "symbol": config.normalize_symbol(sym),
                "price": px,
                "atr": expand["atr"],
                "atr_expand": expand["atr_expand"],
                "breakout_high": brk["breakout_high"],
                "breakout_pct": brk["breakout_pct"],
                "rvol": rvol,
                "type": "atr_vol_breakout",
            }
        )
    scored.sort(
        key=lambda r: (float(r.get("atr_expand") or 0), float(r.get("rvol") or 0)),
        reverse=True,
    )
    return scored[:limit]


def run_vol_breakout_backtest_day(
    window,
    executor,
    regime: str,
    i: int,
    pair_cooldown: dict | None = None,
    *,
    cooldown_bars: int = 3,
    volatility=None,
) -> int:
    """One backtest bar: manage exits + enter ATR expansion breakouts."""
    del volatility
    if not vol_breakout_enabled(live=False):
        return 0
    if not bool(getattr(config, "VOL_BREAKOUT_BACKTEST_ENABLED", True)):
        return 0
    if "RHYME_B" in str(regime or "").upper():
        return 0

    trades = 0
    state = getattr(executor, "_vol_bo_state", None)
    if not isinstance(state, dict):
        state = {"open": {}}
        executor._vol_bo_state = state
    open_book = _open_book(state)

    for sym in list(open_book.keys()):
        meta = open_book[sym]
        try:
            pos = executor._find_position(sym)
        except Exception:
            pos = None
        if pos is None or float(getattr(pos, "qty", 0) or 0) <= 0:
            open_book.pop(sym, None)
            continue
        try:
            current = float(window[sym].iloc[i])
        except Exception:
            current = float(getattr(pos, "current_price", 0) or 0)
        stop = float(meta.get("stop_price") or 0)
        target = float(meta.get("target_price") or 0)
        reason = None
        if stop > 0 and current <= stop:
            reason = "vol_bo_stop"
        elif target > 0 and current >= target:
            reason = "vol_bo_target"
        if not reason:
            continue
        order = executor.execute_full_exit(sym, reason=reason, sleeve=_SLEEVE)
        if order and (not hasattr(executor, "order_filled") or executor.order_filled(order)):
            open_book.pop(sym, None)
            trades += 1

    try:
        equity = float(executor._get_account().equity)
        cash = float(executor._get_account().cash)
    except Exception:
        return trades

    cap = round(equity * _sleeve_cap_pct(), 2)
    open_val = sum(float(m.get("notional") or 0) for m in open_book.values())
    room = round(cap - open_val, 2)
    if room < config.effective_min_notional(equity) or len(open_book) >= _max_positions():
        state["open"] = open_book
        return trades

    cooldown = pair_cooldown if isinstance(pair_cooldown, dict) else {}
    for sig in backtest_vol_breakout_candidates(window, i, limit=4):
        if len(open_book) >= _max_positions():
            break
        sym = sig["symbol"]
        pair_key = f"vol_bo:{sym}"
        last = cooldown.get(pair_key)
        if last is not None and (i - int(last)) < max(1, int(cooldown_bars)):
            continue
        if sym in open_book:
            continue
        try:
            if executor._find_position(sym) is not None:
                continue
        except Exception as exc:
            logger.debug("vol breakout soft-fail: %s", exc)

        sizing = size_vol_breakout_trade(
            equity, sym, window, price=float(sig["price"]), atr=sig.get("atr")
        )
        if not sizing.get("ok"):
            continue
        notional = min(float(sizing["notional"]), room, cash * 0.95)
        if notional < config.effective_min_notional(equity):
            continue
        order = executor.execute_order(
            sym, "buy", notional=notional, reason=pair_key, sleeve=_SLEEVE
        )
        if not order or (hasattr(executor, "order_filled") and not executor.order_filled(order)):
            continue
        open_book[sym] = {
            "entry_price": sizing.get("price"),
            "notional": notional,
            "stop_price": sizing.get("stop_price"),
            "target_price": sizing.get("target_price"),
            "atr_expand": sig.get("atr_expand"),
            "bar": i,
        }
        cooldown[pair_key] = i
        room = round(room - notional, 2)
        cash = max(0.0, cash - notional)
        trades += 1

    state["open"] = open_book
    executor._vol_bo_state = state
    return trades


def vol_breakout_dashboard_rows(data=None, *, limit: int = 8) -> list[dict[str, str]]:
    """Dashboard rows for ATR vol-breakout signals + open book."""
    rows: list[dict[str, str]] = []
    state = _load_state()
    books = state.get("books") if isinstance(state.get("books"), dict) else {}
    paper = books.get("paper") or {}
    open_book = paper.get("open") if isinstance(paper, dict) else {}
    if isinstance(open_book, dict):
        for sym, meta in open_book.items():
            rows.append(
                {
                    "Symbol": str(sym),
                    "Status": "OPEN",
                    "ATR×": f"{float(meta.get('atr_expand') or 0):.2f}x"
                    if meta.get("atr_expand")
                    else "—",
                    "RVOL": f"{float(meta.get('rvol') or 0):.1f}x" if meta.get("rvol") else "—",
                    "Stop": f"{float(meta.get('stop_price') or 0):.2f}",
                    "Target": f"{float(meta.get('target_price') or 0):.2f}",
                    "Conv": (
                        f"{float(meta.get('conviction')):.2f}"
                        if meta.get("conviction") is not None
                        else "—"
                    ),
                    "_tag": "orb_up",
                }
            )

    if data is None:
        try:
            from modules.pipeline_strategies import load_pipeline_data

            data = load_pipeline_data()
        except Exception:
            data = None
    if data is not None and vol_breakout_enabled():
        for sig in collect_vol_breakout_signals(data, limit=limit, regime=None):
            if any(r.get("Symbol") == sig["symbol"] and r.get("Status") == "OPEN" for r in rows):
                continue
            rvol = sig.get("rvol")
            rows.append(
                {
                    "Symbol": sig["symbol"],
                    "Status": "SIGNAL",
                    "ATR×": f"{float(sig.get('atr_expand') or 0):.2f}x",
                    "RVOL": f"{float(rvol):.1f}x" if rvol is not None else "—",
                    "Stop": "—",
                    "Target": f"H {float(sig.get('breakout_high') or 0):.2f}",
                    "Conv": (
                        f"{float(sig['conviction']):.2f}"
                        if sig.get("conviction") is not None
                        else "—"
                    ),
                    "_tag": "orb_up",
                }
            )
            if len(rows) >= limit:
                break
    return rows[:limit]


def format_vol_breakout_banner() -> str | None:
    if not bool(getattr(config, "VOL_BREAKOUT_ENABLED", True)):
        return ">>> Vol Breakout (ATR): OFF"
    return (
        f">>> Vol Breakout (ATR): ON paper-only "
        f"(expand>={_expand_mult():.1f}x, RVOL>={_rvol_min():.1f}, "
        f"risk<={_risk_pct():.0%}, RR {_rr_target():.1f}:1) <<<"
    )


def format_weekly_vol_breakout_note(data=None) -> str:
    if not vol_breakout_enabled():
        return ""
    try:
        if data is None:
            from modules.pipeline_strategies import load_pipeline_data

            data = load_pipeline_data()
        sigs = collect_vol_breakout_signals(data, limit=5, regime=None) if data is not None else []
        state = _load_state()
        paper = ((state.get("books") or {}).get("paper") or {}).get("open") or {}
        n_open = len(paper) if isinstance(paper, dict) else 0
        if not sigs and n_open == 0:
            return (
                f"Vol breakout: ON (ATR expand>={_expand_mult():.1f}x, "
                f"risk<={_risk_pct():.0%}, no signals)"
            )
        tops = ", ".join(
            f"{s['symbol']} {float(s.get('atr_expand') or 0):.1f}x" for s in sigs[:3]
        )
        return (
            f"Vol breakout: ON | open {n_open} | "
            f"signals {tops or '-'} | risk<={_risk_pct():.0%}"
        )
    except Exception as exc:
        logger.debug("vol breakout weekly note failed: %s", exc)
        return "Vol breakout: ON (paper)"


def format_telegram_weekly_vol_breakout_block(data=None) -> str:
    note = format_weekly_vol_breakout_note(data)
    if not note:
        return ""
    return f"\n\n{note}"
