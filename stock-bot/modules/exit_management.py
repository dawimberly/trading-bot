"""Exit optimization — dynamic trails, partial profits, and time exits (paper).

Stacks with ATR sizing and conviction scaling for NYSE momentum, stat arb,
and protective short legs.
"""

from __future__ import annotations

import logging

from datetime import datetime, timedelta
from typing import Any

import config

logger = logging.getLogger(__name__)


def _clamp01(val: float) -> float:
    return max(0.0, min(1.0, float(val)))


def _regime_trail_scale(regime: str | None) -> float:
    try:
        from modules.regime_sizing import effective_regime_sizing_multiplier

        mult = float(effective_regime_sizing_multiplier(regime or ""))
        return max(0.75, min(1.25, 1.05 / max(0.25, mult)))
    except Exception:
        return 1.0


def compute_trailing_stop_plan(
    symbol: str,
    entry_price: float,
    atr: float,
    regime: str | None,
    conviction: float,
) -> dict[str, float]:
    """Build dynamic trail parameters from ATR, regime, and conviction."""
    entry = max(0.01, float(entry_price))
    atr_v = max(0.01, float(atr))
    conv = _clamp01(conviction)
    arm = float(getattr(config, "TRAIL_ARM_PCT", 0.50))
    pull = float(getattr(config, "TRAIL_PULLBACK_PCT", 0.35))
    if config.effective_exit_optimization_enabled():
        arm *= 0.88 + 0.22 * conv
        pull *= 1.08 - 0.18 * conv
    arm = round(max(0.05, min(0.95, arm)), 4)
    pull = round(max(0.08, min(0.65, pull)), 4)
    atr_mult = float(getattr(config, "ATR_RISK_MULTIPLE", 2.0))
    trail_dist = atr_v * atr_mult * (0.90 + 0.25 * conv) * _regime_trail_scale(regime)
    trail_dist = round(max(atr_v * 0.8, trail_dist), 4)
    stop_price = round(max(0.01, entry - trail_dist), 2)
    return {
        "symbol": config.normalize_symbol(symbol),
        "entry_price": entry,
        "stop_price": stop_price,
        "trail_distance": trail_dist,
        "arm_pct": arm,
        "pullback_pct": pull,
        "conviction": conv,
    }


def get_dynamic_trailing_stop(
    symbol: str,
    entry_price: float,
    atr: float,
    regime: str,
    conviction: float,
) -> float:
    """Initial protective stop (trail arms after *arm_pct* gain from entry)."""
    if not config.effective_exit_optimization_enabled():
        entry = float(entry_price)
        dist = max(0.01, float(atr)) * float(getattr(config, "ATR_RISK_MULTIPLE", 2.0))
        return round(max(0.01, entry - dist), 2)
    return compute_trailing_stop_plan(
        symbol, entry_price, atr, regime, conviction
    )["stop_price"]


def trailing_stop_triggered(
    entry_price: float,
    peak_price: float,
    current_price: float,
    *,
    symbol: str = "",
    atr: float | None = None,
    regime: str | None = None,
    conviction: float = 0.5,
    side: str = "long",
) -> bool:
    """True when price pulls back enough after arming the trail."""
    entry = float(entry_price)
    peak = float(peak_price)
    current = float(current_price)
    if entry <= 0 or peak <= 0 or current <= 0:
        return False
    atr_v = float(atr) if atr is not None and atr > 0 else entry * 0.02
    plan = compute_trailing_stop_plan(symbol, entry, atr_v, regime, conviction)
    if str(side).lower() == "short":
        gain = (entry - peak) / entry if entry > 0 else 0.0
        if gain < plan["arm_pct"]:
            return False
        rebound = (current - peak) / peak if peak > 0 else 0.0
        return rebound >= plan["pullback_pct"]
    gain = (peak - entry) / entry
    if gain < plan["arm_pct"]:
        return False
    return current <= peak * (1.0 - plan["pullback_pct"])


def should_partial_exit(
    position: dict[str, Any],
    current_price: float,
    hold_bars: int,
    profit_target: float,
) -> bool:
    """50% partial at 1:1 risk/reward when optimization is enabled."""
    del hold_bars
    if not config.effective_exit_optimization_enabled():
        return False
    if not config.effective_partial_exit_enabled():
        return False
    if position.get("partial_taken"):
        return False
    entry = float(position.get("entry_price") or position.get("avg_entry_price") or 0)
    if entry <= 0:
        return False
    stop = float(
        position.get("stop_price")
        or position.get("initial_stop")
        or entry * (1.0 - float(config.STOP_LOSS_PCT))
    )
    risk = abs(entry - stop)
    if risk <= 0:
        risk = entry * float(config.STOP_LOSS_PCT)
    rr = float(getattr(config, "PARTIAL_EXIT_RR", 1.0))
    target = float(profit_target) if profit_target else entry + risk * rr
    px = float(current_price)
    qty = float(position.get("qty") or position.get("quantity") or 1)
    if qty < 0:
        gain = entry - px
        target_gain = entry - target
    else:
        gain = px - entry
        target_gain = target - entry
    return gain >= risk * rr and gain >= target_gain * 0.98


def get_time_based_exit(hold_bars: int, max_hold: int = 35) -> bool:
    """Force exit when hold duration exceeds *max_hold* bars."""
    if not config.effective_exit_optimization_enabled():
        return False
    cap = int(max_hold or getattr(config, "EXIT_OPTIMIZATION_MAX_HOLD_BARS", 35))
    return int(hold_bars) >= cap


def partial_exit_fraction() -> float:
    return 0.50


def _events_path():
    from pathlib import Path

    return Path(getattr(config, "EXIT_EVENTS_FILE", "data/exit_events.json"))


def record_exit_event(
    reason: str,
    symbol: str,
    *,
    sleeve: str = "",
    partial: bool = False,
    notional: float | None = None,
) -> None:
    if not reason:
        return
    try:
        from modules.safe_io import read_json_file, write_json_atomic

        path = _events_path()
        payload = read_json_file(path) or {"events": []}
        events = list(payload.get("events") or [])
        events.append(
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "reason": str(reason),
                "symbol": config.normalize_symbol(symbol),
                "sleeve": sleeve,
                "partial": bool(partial),
                "notional": round(float(notional), 2) if notional is not None else None,
            }
        )
        payload["events"] = events[-400:]
        payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
        write_json_atomic(path, payload)
    except Exception as exc:
        logger.debug("exit mgmt soft-fail: %s", exc)


def exit_reason_summary(*, days: int = 7) -> dict[str, Any]:
    """Aggregate recent exit events by reason category."""
    try:
        from modules.safe_io import read_json_file

        payload = read_json_file(_events_path()) or {}
        events = list(payload.get("events") or [])
        cutoff = datetime.now() - timedelta(days=max(1, int(days)))
        counts: dict[str, int] = {
            "partial": 0,
            "trail": 0,
            "time": 0,
            "stop": 0,
            "other": 0,
        }
        recent: list[dict] = []
        for ev in events:
            try:
                ts = datetime.fromisoformat(str(ev.get("ts", "")).replace("Z", "+00:00"))
                if ts.tzinfo is not None:
                    ts = ts.replace(tzinfo=None)
                if ts < cutoff:
                    continue
            except (TypeError, ValueError):
                continue
            reason = str(ev.get("reason") or "").lower()
            bucket = "other"
            if "partial" in reason:
                bucket = "partial"
            elif "trail" in reason:
                bucket = "trail"
            elif "time" in reason or "max_hold" in reason:
                bucket = "time"
            elif "stop" in reason:
                bucket = "stop"
            counts[bucket] += 1
            recent.append(ev)
        return {
            "counts": counts,
            "total": sum(counts.values()),
            "recent": list(reversed(recent[-12:])),
        }
    except Exception:
        return {"counts": {}, "total": 0, "recent": []}


def format_exit_optimization_banner() -> str | None:
    if not config.effective_exit_optimization_enabled():
        return ">>> Exit Optimization: OFF"
    rr = float(getattr(config, "PARTIAL_EXIT_RR", 1.0))
    return f">>> Exit Optimization: ON (partial @ {rr:.1f}:1 + trail) <<<"


def format_weekly_exit_note() -> str:
    if not config.effective_exit_optimization_enabled():
        return ""
    summary = exit_reason_summary(days=7)
    c = summary.get("counts") or {}
    if not summary.get("total"):
        return (
            f"Exit optimization: ON | partial {c.get('partial', 0)} | "
            f"trail {c.get('trail', 0)} | time {c.get('time', 0)} | stop {c.get('stop', 0)}"
        )
    return (
        f"Exit optimization (7d): partial {c.get('partial', 0)} | "
        f"trail {c.get('trail', 0)} | time {c.get('time', 0)} | "
        f"stop {c.get('stop', 0)} | other {c.get('other', 0)}"
    )


def format_telegram_weekly_exit_block() -> str:
    note = format_weekly_exit_note()
    if not note:
        return ""
    return f"\n\n{note}"


def exit_dashboard_rows(*, days: int = 7) -> list[dict[str, str]]:
    summary = exit_reason_summary(days=days)
    rows: list[dict[str, str]] = []
    for ev in summary.get("recent") or []:
        rows.append(
            {
                "Time": str(ev.get("ts") or "")[:16],
                "Symbol": str(ev.get("symbol") or ""),
                "Reason": str(ev.get("reason") or ""),
                "Sleeve": str(ev.get("sleeve") or "—"),
                "Partial": "yes" if ev.get("partial") else "—",
            }
        )
    return rows


def exit_dashboard_status(*, days: int = 7) -> str:
    if not config.effective_exit_optimization_enabled():
        return ""
    summary = exit_reason_summary(days=days)
    c = summary.get("counts") or {}
    return (
        f"Exits {summary.get('total', 0)} (7d): "
        f"partial {c.get('partial', 0)} · trail {c.get('trail', 0)} · "
        f"time {c.get('time', 0)} · stop {c.get('stop', 0)}"
    )


def resolve_symbol_atr_and_conviction(
    executor,
    symbol: str,
    *,
    regime: str | None = None,
) -> tuple[float, float]:
    """Best-effort ATR and conviction for exit plans."""
    data = getattr(executor, "_sizing_data", None)
    atr = None
    try:
        from modules.risk_management import calculate_atr, compute_conviction_score

        if data is not None:
            atr = calculate_atr(data, symbol)
        conviction = compute_conviction_score(symbol, data, regime, sleeve="nyse")
    except Exception:
        conviction = 0.5
    if atr is None or atr <= 0:
        try:
            pos = executor._find_position(symbol)
            entry = float(getattr(pos, "avg_entry_price", 0) or 0)
            atr = entry * 0.02 if entry > 0 else 1.0
        except Exception:
            atr = 1.0
    return float(atr), float(conviction)
