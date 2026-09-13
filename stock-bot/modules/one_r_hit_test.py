"""Measure-only 1R target test (paper). Does not change orders.

At a NYSE buy fill, stamp the same 1:1 target the exit engine uses
(entry + ATR-stop distance × PARTIAL_EXIT_RR). Later, if price trades
through that target, journal ``1r_hit`` once. Full exit journals ``1r_close``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import config
from modules.smart_atr_stops import compute_stop_price

logger = logging.getLogger(__name__)

_SKIP_SLEEVES = frozenset({"vti", "core", "vti_core", "spy", "crypto", "metal"})


def walk_path(
    closes,
    entry_i: int,
    stop: float,
    target: float,
    max_hold: int,
    *,
    take_1r: bool,
) -> tuple[int, float, str, bool]:
    """Daily close path. Returns (exit_i, exit_px, reason, touched_1r)."""
    n = len(closes)
    last = min(int(entry_i) + int(max_hold), n - 1)
    touched = False
    for j in range(int(entry_i) + 1, last + 1):
        px = float(closes[j])
        if px != px or px <= 0:
            continue
        if px >= target:
            touched = True
            if take_1r:
                return j, px, "1r", True
        if px <= stop:
            return j, px, "stop", touched
    px = float(closes[last])
    return last, px, "time", touched


def enabled() -> bool:
    return bool(getattr(config, "PAPER_TRADING", False))


def _rr() -> float:
    try:
        return max(0.25, float(getattr(config, "PARTIAL_EXIT_RR", 1.0)))
    except (TypeError, ValueError):
        return 1.0


def plan_from_entry(
    entry: float,
    atr: float,
    *,
    rr: float | None = None,
    stop_multiplier: float | None = None,
) -> dict[str, float]:
    """Stop + 1R target in dollars. Same distance math as smart ATR + partial @ RR."""
    entry_v = max(0.01, float(entry))
    atr_v = max(0.0, float(atr))
    stop = float(
        compute_stop_price(entry_v, atr_v, side="long", multiplier=stop_multiplier)
    )
    risk = max(0.01, entry_v - stop)
    rr_v = float(rr if rr is not None else _rr())
    target = round(entry_v + risk * rr_v, 2)
    return {
        "entry": round(entry_v, 4),
        "atr": round(atr_v, 4),
        "stop": stop,
        "risk": round(risk, 4),
        "rr": rr_v,
        "target": target,
        "target_pct": round(risk * rr_v / entry_v, 6),
        "stop_pct": round(risk / entry_v, 6),
    }


def annotate_notes(plan: dict[str, float]) -> str:
    pct = float(plan["target_pct"]) * 100.0
    return (
        f"1r_target={plan['target']:.2f} ({pct:.2f}%) "
        f"stop={plan['stop']:.2f} atr={plan['atr']:.4f}"
    )


def _state_path() -> Path:
    raw = getattr(config, "PAPER_JOURNAL_CSV", "") or "paper_chase_journal.csv"
    path = Path(raw)
    if not path.is_absolute():
        path = Path(config.PROJECT_ROOT) / path if hasattr(config, "PROJECT_ROOT") else Path(raw)
    return path.with_name("one_r_hit_test.json")


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.is_file():
        return {"open": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("open", {})
            return data
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return {"open": {}}


def _save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def _sleeve_ok(sleeve: str, symbol: str) -> bool:
    if config.is_crypto(symbol):
        return False
    key = str(sleeve or "").strip().lower()
    if key in _SKIP_SLEEVES:
        return False
    if config.normalize_symbol(symbol) == config.normalize_symbol(
        getattr(config, "VTI_CORE_SYMBOL", "VTI")
    ):
        return False
    return True


def stamp_buy(
    executor,
    *,
    symbol: str,
    entry: float,
    sleeve: str,
    order_id: str = "",
) -> str:
    """Record 1R plan for a paper NYSE buy. Returns journal-notes suffix (may be empty)."""
    if not enabled() or entry <= 0:
        return ""
    if not _sleeve_ok(sleeve, symbol):
        return ""
    sym = config.normalize_symbol(symbol)
    atr = None
    try:
        from modules.risk_management import calculate_atr

        atr = calculate_atr(getattr(executor, "_sizing_data", None), sym)
    except Exception:
        atr = None
    if atr is None or atr <= 0:
        atr = float(entry) * 0.02
    plan = plan_from_entry(entry, atr)
    state = _load_state()
    state["open"][sym] = {
        **plan,
        "symbol": sym,
        "order_id": str(order_id or ""),
        "opened_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "high": round(float(entry), 4),
        "low": round(float(entry), 4),
        "hit": False,
        "hit_at": "",
    }
    try:
        _save_state(state)
    except OSError as exc:
        logger.debug("one_r_hit_test state write skipped: %s", exc)
    return annotate_notes(plan)


def observe_open_positions(executor) -> None:
    """Update high-water; journal 1r_hit / 1r_close. Never raises into the cycle."""
    if not enabled():
        return
    try:
        _observe(executor)
    except Exception:
        logger.debug("one_r_hit_test observe skipped", exc_info=True)


def _observe(executor) -> None:
    from modules import trade_journal

    state = _load_state()
    open_plans: dict[str, Any] = state.get("open") or {}
    if not open_plans:
        return
    held: dict[str, Any] = {}
    try:
        for pos in executor._get_positions() or []:
            sym = config.normalize_symbol(getattr(pos, "symbol", "") or "")
            if not sym:
                continue
            held[sym] = pos
    except Exception:
        return

    changed = False
    closed: list[str] = []
    for sym, plan in list(open_plans.items()):
        pos = held.get(sym)
        if pos is None:
            _journal_close(trade_journal, plan, still_open=False)
            closed.append(sym)
            changed = True
            continue
        try:
            px = float(getattr(pos, "current_price", 0) or 0)
        except (TypeError, ValueError):
            px = 0.0
        if px <= 0:
            continue
        high = max(float(plan.get("high") or 0), px)
        low = min(float(plan.get("low") or px), px)
        if high != plan.get("high") or low != plan.get("low"):
            plan["high"] = round(high, 4)
            plan["low"] = round(low, 4)
            changed = True
        target = float(plan.get("target") or 0)
        if target > 0 and high + 1e-9 >= target and not plan.get("hit"):
            plan["hit"] = True
            plan["hit_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            changed = True
            entry = float(plan.get("entry") or 0) or 1.0
            trade_journal.log_event(
                "1r_hit",
                symbol=sym,
                side="buy",
                price=round(high, 4),
                notes=(
                    f"hit 1r_target={target:.2f} mfe={100.0 * (high - entry) / entry:.2f}% "
                    f"{annotate_notes(plan)}"
                ),
            )
    for sym in closed:
        open_plans.pop(sym, None)
    if changed:
        state["open"] = open_plans
        _save_state(state)


def _journal_close(trade_journal, plan: dict[str, Any], *, still_open: bool) -> None:
    sym = str(plan.get("symbol") or "")
    entry = float(plan.get("entry") or 0) or 1.0
    high = float(plan.get("high") or entry)
    hit = bool(plan.get("hit"))
    mfe = 100.0 * (high - entry) / entry
    trade_journal.log_event(
        "1r_close",
        symbol=sym,
        side="sell",
        notes=(
            f"hit={int(hit)} mfe={mfe:.2f}% target_pct={100.0 * float(plan.get('target_pct') or 0):.2f}% "
            f"{annotate_notes(plan)}"
        ),
    )
    _ = still_open
