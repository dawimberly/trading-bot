"""RVOL + ORB momentum sleeve — paper-first, optional small live book.

Entry: upside break of the 30-min opening range with RVOL >= ORB_RVOL_MIN,
gated by multi-timeframe alignment and conviction. Size to ~1% equity risk
with an ATR stop and 1.5:1 reward target; notional capped at 5–10% of equity.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from modules.safe_io import read_json_file, write_json_file

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
_STATE_PATH = ROOT / "data" / "orb_momentum_state.json"
_SLEEVE = "ORB_MOM"


def _state_path() -> Path:
    raw = getattr(config, "ORB_MOMENTUM_STATE_FILE", None)
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


def orb_momentum_enabled(*, live: bool | None = None) -> bool:
    """True when the sleeve may trade on this book."""
    if not bool(getattr(config, "ORB_MOMENTUM_ENABLED", True)):
        return False
    if not config.effective_orb_enabled() and not config.effective_rvol_scanner_enabled():
        # Allow live opt-in even when scanners are paper-gated, if live flag set.
        if not (live and config.orb_momentum_live_sleeve_enabled()):
            return False
    if live is True:
        return config.orb_momentum_live_sleeve_enabled()
    if live is False:
        return bool(
            config.PAPER_TRADING
            or config.paper_aggressive_context()
            or config.backtest_paper_sleeves_context()
            or config.is_realistic_research_active()
        )
    # Auto: paper path, or live when opt-in.
    if config.PAPER_TRADING or config.paper_aggressive_context() or config.backtest_paper_sleeves_context():
        return True
    return config.orb_momentum_live_sleeve_enabled()


def _max_size_pct() -> float:
    lo = float(getattr(config, "ORB_MOMENTUM_MIN_SIZE_PCT", 0.05))
    hi = float(getattr(config, "ORB_MOMENTUM_MAX_SIZE_PCT", 0.10))
    if hi < lo:
        lo, hi = hi, lo
    return _clamp(hi, 0.02, 0.25)


def _risk_pct() -> float:
    return _clamp(float(getattr(config, "ORB_MOMENTUM_RISK_PCT", 0.01)), 0.002, 0.03)


def _rr_target() -> float:
    return max(1.0, float(getattr(config, "ORB_MOMENTUM_RR", 1.5)))


def _atr_mult() -> float:
    return max(
        0.5,
        float(
            getattr(
                config,
                "ORB_MOMENTUM_ATR_MULT",
                getattr(config, "ATR_RISK_MULTIPLE", 2.0),
            )
        ),
    )


def _sleeve_cap_pct(*, live: bool = False) -> float:
    if live:
        return _clamp(float(getattr(config, "ORB_MOMENTUM_LIVE_CAP_PCT", 0.05)), 0.01, 0.15)
    return _clamp(float(getattr(config, "ORB_MOMENTUM_CAP_PCT", 0.15)), 0.03, 0.40)


def _max_positions(*, live: bool = False) -> int:
    if live:
        return max(1, int(getattr(config, "ORB_MOMENTUM_LIVE_MAX_POSITIONS", 1)))
    return max(1, int(getattr(config, "ORB_MOMENTUM_MAX_POSITIONS", 3)))


def _min_conviction() -> float:
    return _clamp(float(getattr(config, "ORB_MOMENTUM_MIN_CONVICTION", 0.45)), 0.0, 0.95)


def collect_orb_momentum_signals(
    data,
    *,
    limit: int = 12,
    require_mtf: bool = True,
    require_conviction: bool = True,
    regime: str | None = None,
) -> list[dict[str, Any]]:
    """Upside ORB + RVOL setups that pass MTF / conviction gates."""
    if not config.effective_orb_enabled() and not config.backtest_paper_sleeves_context():
        # Live opt-in may still scan via orb_strategy if ORB_ENABLED.
        if not bool(getattr(config, "ORB_ENABLED", False)):
            return []

    from modules.orb_strategy import get_orb_signals

    minutes = int(getattr(config, "ORB_BREAKOUT_MINUTES", 30))
    min_rvol = float(getattr(config, "ORB_RVOL_MIN", 2.0))
    raw = get_orb_signals(data, minutes=minutes, volume_filter=True, limit=max(limit * 2, 20))
    upside = [s for s in raw if s.get("type") == "up_breakout"]

    out: list[dict[str, Any]] = []
    for sig in upside:
        sym = config.normalize_symbol(sig.get("symbol"))
        if not sym:
            continue
        rvol = sig.get("rvol")
        if rvol is None or float(rvol) < min_rvol:
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
                logger.debug("ORB momentum MTF check skipped for %s: %s", sym, exc)
                mtf_ok = True
        if not mtf_ok:
            continue

        conviction = None
        if require_conviction and config.effective_conviction_sizing_enabled():
            try:
                from modules.risk_management import compute_conviction_score

                conviction = compute_conviction_score(
                    sym, data, regime, sleeve="nyse"
                )
            except Exception as exc:
                logger.debug("ORB momentum conviction skipped for %s: %s", sym, exc)
                conviction = None
        if conviction is not None and float(conviction) < _min_conviction():
            continue

        out.append(
            {
                **sig,
                "symbol": sym,
                "mtf_align": mtf_align,
                "conviction": conviction,
                "sleeve": _SLEEVE,
            }
        )
        if len(out) >= limit:
            break
    return out


def size_orb_momentum_trade(
    equity: float,
    symbol: str,
    data,
    *,
    price: float | None = None,
    conviction: float | None = None,
) -> dict[str, Any]:
    """1% risk / ATR stop, notional capped at max size pct (5–10%)."""
    from modules.risk_management import calculate_atr, conviction_scale

    equity = float(equity)
    sym = config.normalize_symbol(symbol)
    atr = calculate_atr(data, sym)
    px = price
    if px is None or px <= 0:
        if data is not None and hasattr(data, "columns") and sym in data.columns:
            series = data[sym].dropna()
            if not series.empty:
                px = float(series.iloc[-1])
    if px is None or px <= 0:
        return {"ok": False, "reason": "no_price", "notional": 0.0}

    atr_mult = _atr_mult()
    risk_pct = _risk_pct()
    max_pct = _max_size_pct()
    min_pct = float(getattr(config, "ORB_MOMENTUM_MIN_SIZE_PCT", 0.05))
    risk_dollars = equity * risk_pct

    if atr is None or atr <= 0:
        # Fallback: 2% stop distance when ATR unavailable.
        stop_dist = px * 0.02 * atr_mult / 2.0
        method = "pct_fallback"
    else:
        stop_dist = float(atr) * atr_mult
        method = "atr"

    if stop_dist <= 0:
        return {"ok": False, "reason": "bad_stop", "notional": 0.0}

    shares = risk_dollars / stop_dist
    notional = round(shares * px, 2)
    notional = min(notional, round(equity * max_pct, 2))

    # Soft floor toward min size when ATR sizing is tiny on small accounts,
    # but never exceed max_pct or available risk budget * 1.5.
    min_n = round(equity * min_pct, 2)
    if notional < min_n and equity >= 200:
        # Only lift toward min when stop is tight enough that 1% risk still fits.
        lift = min(min_n, round(risk_dollars * px / max(stop_dist * 0.75, px * 0.005), 2))
        notional = min(max(notional, lift), round(equity * max_pct, 2))

    if conviction is not None and config.effective_conviction_sizing_enabled():
        # Keep conviction scale conservative (0.7x–1.15x) for this sleeve.
        scale = conviction_scale(float(conviction), scale_band=(0.70, 1.15))
        notional = round(notional * scale, 2)
        notional = min(notional, round(equity * max_pct, 2))

    notional = min(notional, config.effective_max_notional_per_order(equity))
    floor = config.effective_min_notional(equity)
    if notional < floor:
        return {
            "ok": False,
            "reason": "below_min_notional",
            "notional": notional,
            "stop_distance": round(stop_dist, 4),
            "price": px,
            "atr": atr,
            "method": method,
        }

    stop_price = round(max(0.01, px - stop_dist), 4)
    target_price = round(px + stop_dist * _rr_target(), 4)
    return {
        "ok": True,
        "symbol": sym,
        "notional": round(notional, 2),
        "price": round(px, 4),
        "atr": atr,
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
            logger.debug("ORB momentum soft-fail: %s", exc)
        total += float((meta or {}).get("notional") or 0)
    return round(total, 2)


def _manage_open_exits(executor, state: dict[str, Any], *, journal=None) -> list[dict[str, Any]]:
    """Exit ORB momentum names at stop or 1.5R target."""
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
            reason = "orb_stop"
        elif target > 0 and current >= target:
            reason = "orb_target_1.5r"

        if not reason:
            # Refresh peak for optional trail later.
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
            logger.warning("ORB momentum exit failed %s: %s", sym, exc)
            ok = False
            order = None

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
                    journal.log_exit(sym, "sell", reason, float(getattr(executor._get_account(), "equity", 0) or 0))
                except Exception as exc:
                    logger.debug("ORB momentum soft-fail: %s", exc)
            try:
                from modules.strategy_performance import record_closed_trade

                record_closed_trade(
                    "orb_breakout",
                    symbol=sym,
                    pnl=float(act.get("pnl_pct") or 0) * float(meta.get("notional") or 0),
                    pnl_pct=float(act.get("pnl_pct") or 0) * 100.0,
                    notional=float(meta.get("notional") or 0),
                    source="paper" if config.PAPER_TRADING else "live",
                    tags=["orb_momentum"],
                )
            except Exception as exc:
                logger.debug("ORB momentum trade record skipped: %s", exc)

    if changed:
        state["open"] = book
        state["updated_at"] = _now_iso()
        _save_state(state)
    else:
        state["open"] = book
    return actions


def run_orb_momentum_cycle(
    data,
    executor,
    *,
    regime: str = "",
    market_open: bool = True,
    live: bool = False,
    journal=None,
    yield_gated: bool = False,
) -> dict[str, Any]:
    """Scan / size / enter ORB+RVOL momentum; manage stop & 1.5R exits."""
    result: dict[str, Any] = {
        "enabled": False,
        "live": live,
        "signals": [],
        "entries": [],
        "exits": [],
        "skipped": None,
    }
    if not orb_momentum_enabled(live=live):
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
    book_key = "live" if live else "paper"
    books = state.get("books") if isinstance(state.get("books"), dict) else {}
    book_state = books.get(book_key) if isinstance(books.get(book_key), dict) else {"open": {}}
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
    cap = round(equity * _sleeve_cap_pct(live=live), 2)
    open_val = _sleeve_open_value(executor, open_book)
    room = round(cap - open_val, 2)
    max_pos = _max_positions(live=live)
    result["cap"] = cap
    result["open_value"] = open_val
    result["room"] = room
    result["open_count"] = len(open_book)

    if len(open_book) >= max_pos or room < config.effective_min_notional(equity):
        books[book_key] = book_state
        state["books"] = books
        state["updated_at"] = _now_iso()
        _save_state(state)
        result["signals"] = collect_orb_momentum_signals(data, limit=8, regime=regime)
        result["skipped"] = "no_room" if room < config.effective_min_notional(equity) else "max_positions"
        return result

    signals = collect_orb_momentum_signals(data, limit=8, regime=regime)
    result["signals"] = signals

    held = set(open_book.keys())
    try:
        for pos in executor._get_positions():
            held.add(config.normalize_symbol(pos.symbol))
    except Exception as exc:
        logger.debug("ORB momentum soft-fail: %s", exc)

    entries: list[dict[str, Any]] = []
    for sig in signals:
        if len(open_book) >= max_pos:
            break
        sym = sig["symbol"]
        if sym in held:
            continue
        sizing = size_orb_momentum_trade(
            equity,
            sym,
            data,
            price=float(sig.get("price") or 0) or None,
            conviction=sig.get("conviction"),
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
                reason=f"orb_rvol_{sig.get('rvol')}",
                sleeve=_SLEEVE,
            )
            ok = bool(order) and (
                executor.order_filled(order) if hasattr(executor, "order_filled") else True
            )
        except Exception as exc:
            logger.warning("ORB momentum entry failed %s: %s", sym, exc)
            ok = False
            order = None

        entry_rec = {
            "action": "buy",
            "symbol": sym,
            "notional": notional,
            "ok": ok,
            "rvol": sig.get("rvol"),
            "or_high": sig.get("or_high"),
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
                "rvol": sig.get("rvol"),
                "or_high": sig.get("or_high"),
                "conviction": sig.get("conviction"),
                "peak": sizing.get("price"),
                "live": live,
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
                        f"orb_mom:{sym}",
                        float(sig.get("rvol") or 0),
                        equity,
                        notional,
                    )
                except Exception as exc:
                    logger.debug("ORB momentum soft-fail: %s", exc)

    book_state["open"] = open_book
    books[book_key] = book_state
    state["books"] = books
    state["last_signals"] = [
        {
            "symbol": s.get("symbol"),
            "rvol": s.get("rvol"),
            "or_high": s.get("or_high"),
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


# --- Daily backtest proxy (close-only data; OR ≈ prior N-day high) -------------


def _daily_rvol_proxy(closes, i: int, lookback: int = 10) -> float | None:
    """Proxy RVOL from absolute return vs recent mean absolute return."""
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


def backtest_orb_momentum_candidates(window, i: int, *, limit: int = 5) -> list[dict[str, Any]]:
    """Daily proxy signals: break prior 5d high + RVOL proxy >= ORB_RVOL_MIN."""
    if window is None or not hasattr(window, "columns"):
        return []
    min_rvol = float(getattr(config, "ORB_RVOL_MIN", 2.0))
    lookback = 5
    cols = [
        str(c)
        for c in window.columns
        if config._nyse_eligible_symbol(str(c))
    ][:40]
    scored: list[dict[str, Any]] = []
    for sym in cols:
        series = window[sym].dropna()
        if len(series) < lookback + 3:
            continue
        # Align to window index position.
        if i >= len(window):
            continue
        try:
            px = float(window[sym].iloc[i])
        except Exception:
            continue
        if not (px > 0):
            continue
        prior = window[sym].iloc[max(0, i - lookback) : i]
        prior = prior.dropna()
        if prior.empty:
            continue
        or_high = float(prior.max())
        if px <= or_high:
            continue
        rvol = _daily_rvol_proxy(window[sym].dropna(), min(i, len(window[sym].dropna()) - 1))
        # Fallback: use breakout extension as weak volume proxy.
        if rvol is None:
            ext = px / or_high - 1.0
            rvol = round(1.0 + ext * 40.0, 3)  # ~2.0x when +2.5% extension
        if rvol < min_rvol:
            continue
        # Daily MTF proxy: price above 20d MA.
        ma = window[sym].iloc[max(0, i - 19) : i + 1].dropna()
        if len(ma) >= 10 and px < float(ma.mean()):
            continue
        scored.append(
            {
                "symbol": config.normalize_symbol(sym),
                "price": px,
                "or_high": round(or_high, 4),
                "rvol": rvol,
                "type": "up_breakout",
                "breakout_pct": round((px / or_high - 1.0) * 100.0, 2),
            }
        )
    scored.sort(key=lambda r: (float(r.get("rvol") or 0), float(r.get("breakout_pct") or 0)), reverse=True)
    return scored[:limit]


def run_orb_momentum_backtest_day(
    window,
    executor,
    regime: str,
    i: int,
    pair_cooldown: dict | None = None,
    *,
    cooldown_bars: int = 3,
    volatility=None,
) -> int:
    """One backtest bar: manage exits + enter daily ORB proxy names."""
    del volatility
    if not orb_momentum_enabled(live=False):
        return 0
    if not bool(getattr(config, "ORB_MOMENTUM_BACKTEST_ENABLED", True)):
        return 0
    if "RHYME_B" in str(regime or "").upper():
        return 0

    trades = 0
    state = getattr(executor, "_orb_mom_state", None)
    if not isinstance(state, dict):
        state = {"open": {}}
        executor._orb_mom_state = state
    open_book = _open_book(state)

    # Exits
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
            reason = "orb_stop"
        elif target > 0 and current >= target:
            reason = "orb_target_1.5r"
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

    cap = round(equity * _sleeve_cap_pct(live=False), 2)
    open_val = sum(float(m.get("notional") or 0) for m in open_book.values())
    room = round(cap - open_val, 2)
    if room < config.effective_min_notional(equity) or len(open_book) >= _max_positions(live=False):
        state["open"] = open_book
        return trades

    cooldown = pair_cooldown if isinstance(pair_cooldown, dict) else {}
    for sig in backtest_orb_momentum_candidates(window, i, limit=4):
        if len(open_book) >= _max_positions(live=False):
            break
        sym = sig["symbol"]
        pair_key = f"orb_mom:{sym}"
        last = cooldown.get(pair_key)
        if last is not None and (i - int(last)) < max(1, int(cooldown_bars)):
            continue
        if sym in open_book:
            continue
        try:
            if executor._find_position(sym) is not None:
                continue
        except Exception as exc:
            logger.debug("ORB momentum soft-fail: %s", exc)

        sizing = size_orb_momentum_trade(equity, sym, window, price=float(sig["price"]))
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
            "bar": i,
        }
        cooldown[pair_key] = i
        room = round(room - notional, 2)
        cash = max(0.0, cash - notional)
        trades += 1

    state["open"] = open_book
    executor._orb_mom_state = state
    return trades


def orb_momentum_dashboard_rows(data=None, *, limit: int = 8) -> list[dict[str, str]]:
    """Dashboard rows for ORB momentum signals + open book."""
    rows: list[dict[str, str]] = []
    state = _load_state()
    books = state.get("books") if isinstance(state.get("books"), dict) else {}
    for book_name in ("paper", "live"):
        book = books.get(book_name) or {}
        open_book = book.get("open") if isinstance(book, dict) else {}
        if not isinstance(open_book, dict):
            continue
        for sym, meta in open_book.items():
            rows.append(
                {
                    "Symbol": str(sym),
                    "Status": f"OPEN ({book_name})",
                    "RVOL": f"{float(meta.get('rvol') or 0):.1f}x" if meta.get("rvol") else "—",
                    "Stop": f"{float(meta.get('stop_price') or 0):.2f}",
                    "Target": f"{float(meta.get('target_price') or 0):.2f}",
                    "Conv": f"{float(meta.get('conviction')):.2f}" if meta.get("conviction") is not None else "—",
                    "_tag": "orb_up",
                }
            )

    if data is None:
        try:
            from modules.pipeline_strategies import load_pipeline_data

            data = load_pipeline_data()
        except Exception:
            data = None
    if data is not None and orb_momentum_enabled():
        for sig in collect_orb_momentum_signals(data, limit=limit, regime=None):
            if any(r.get("Symbol") == sig["symbol"] and r.get("Status", "").startswith("OPEN") for r in rows):
                continue
            rvol = sig.get("rvol")
            rows.append(
                {
                    "Symbol": sig["symbol"],
                    "Status": "SIGNAL",
                    "RVOL": f"{float(rvol):.1f}x" if rvol is not None else "—",
                    "Stop": "—",
                    "Target": f"OR {float(sig.get('or_high') or 0):.2f}",
                    "Conv": f"{float(sig['conviction']):.2f}" if sig.get("conviction") is not None else "—",
                    "_tag": "orb_up",
                }
            )
            if len(rows) >= limit:
                break
    return rows[:limit]


def format_orb_momentum_banner() -> str | None:
    if not bool(getattr(config, "ORB_MOMENTUM_ENABLED", True)):
        return ">>> ORB Momentum Sleeve: OFF"
    live = "LIVE opt-in ON" if config.orb_momentum_live_sleeve_enabled() else "paper-only"
    return (
        f">>> ORB Momentum Sleeve: ON ({int(config.ORB_BREAKOUT_MINUTES)}m, "
        f"RVOL>={config.ORB_RVOL_MIN:.1f}x, risk {_risk_pct():.0%}, "
        f"max {_max_size_pct():.0%}, RR {_rr_target():.1f}:1, {live}) <<<"
    )
