"""Passive VTI core sleeve — index anchor; active bot runs on the remainder."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import config


def vti_core_value(executor) -> float:
    pos = executor._find_position(config.VTI_CORE_SYMBOL)
    if pos is None:
        return 0.0
    return executor._position_market_value(pos)


def _iso_week_id(d: date | None = None) -> str:
    day = d or date.today()
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _weekly_state_path() -> Path:
    """Per-book state next to the heartbeat file."""
    try:
        hb = Path(config.resolve_heartbeat_file())
        return hb.with_name("vti_rebalance_weekly.json")
    except Exception:
        return Path("vti_rebalance_weekly.json")


def _weekly_already_done(week: str) -> bool:
    path = _weekly_state_path()
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(payload, dict) and str(payload.get("week_id") or "") == week
    except (OSError, json.JSONDecodeError, TypeError):
        return False


def _mark_weekly_done(week: str, *, reason: str) -> None:
    path = _weekly_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        from modules.safe_io import write_json_atomic

        write_json_atomic(
            str(path),
            {
                "week_id": week,
                "reason": reason,
                "as_of": date.today().isoformat(),
            },
        )
    except Exception:
        try:
            path.write_text(
                json.dumps(
                    {
                        "week_id": week,
                        "reason": reason,
                        "as_of": date.today().isoformat(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass


def rebalance_vti_core(
    executor,
    *,
    market_open: bool,
    vol_score: float | None = None,
    macro_stress: bool = False,
    volatility: str | None = None,
) -> dict:
    """
    Hold VTI at VTI_CORE_PCT of equity.

    Cadence:
      - drift (default): rebalance when |current - target| / equity exceeds band
      - weekly: one open-session resize evaluation per ISO week (Live + Medium SoT)
    """
    if not config.vti_core_enabled():
        return {"enabled": False}
    if not market_open:
        return {"enabled": True, "skipped": True, "reason": "equity session closed"}

    cadence = config.effective_vti_rebalance_cadence()
    week = _iso_week_id()
    if cadence == "weekly" and _weekly_already_done(week):
        return {
            "enabled": True,
            "skipped": True,
            "reason": f"weekly resize already done ({week})",
            "cadence": "weekly",
            "week_id": week,
        }

    account = executor._get_account()
    equity = float(account.equity)
    if equity <= 0:
        return {"enabled": True, "skipped": True, "reason": "no equity"}

    core_pct = config.vti_core_allocation_pct(
        equity=equity,
        vol_score=vol_score,
        macro_stress=macro_stress,
        volatility=volatility,
    )
    target = round(equity * core_pct, 2)
    current = round(vti_core_value(executor), 2)
    min_n = config.effective_min_notional(equity)
    drift_pct = abs(current - target) / equity if equity else 0.0
    # Weekly resize: still honor drift band so tiny noise doesn't trade.
    band = config.effective_vti_rebalance_drift_pct()

    result = {
        "enabled": True,
        "symbol": config.VTI_CORE_SYMBOL,
        "target_pct": core_pct,
        "paper_aggressive": config.paper_aggressive_context(),
        "target_value": target,
        "current_value": current,
        "drift_pct": round(drift_pct, 4),
        "cadence": cadence,
        "week_id": week if cadence == "weekly" else None,
    }

    def _done(reason: str) -> dict:
        if cadence == "weekly":
            _mark_weekly_done(week, reason=reason)
            result["week_marked"] = True
        return result

    if current > 0 and drift_pct < band:
        result["skipped"] = True
        result["reason"] = f"within {band:.0%} drift band"
        return _done(result["reason"])

    delta = round(target - current, 2)
    if abs(delta) < min_n:
        result["skipped"] = True
        result["reason"] = "below min notional"
        return _done(result["reason"])

    symbol = config.VTI_CORE_SYMBOL
    if delta > 0:
        cash = float(getattr(account, "cash", 0) or 0)
        # Never buy VTI into margin — leave a small cash reserve for active sleeves.
        try:
            reserve_pct = min(0.08, float(config.effective_cash_buffer_pct()) * 0.5)
        except Exception:
            reserve_pct = 0.05
        reserve = max(equity * reserve_pct, min_n)
        buyable = max(0.0, cash - reserve)
        if buyable < min_n:
            result["skipped"] = True
            result["reason"] = "insufficient cash (reserve)"
            result["cash"] = round(cash, 2)
            # Do not mark week — retry later if cash frees up.
            return result
        delta = min(delta, round(buyable, 2))
        order = executor.execute_order(symbol, "buy", notional=delta)
        result["action"] = "buy"
    else:
        order = executor.execute_reduce_notional(symbol, -delta)
        result["action"] = "sell"

    result["notional"] = abs(delta)
    result["ok"] = order is not None and (
        executor.order_filled(order, max_wait=3.0) if order else False
    )
    if result.get("ok"):
        executor.refresh_cache()
        result["current_value"] = round(vti_core_value(executor), 2)
        return _done(f"{result['action']} ok")
    # Failed fill — leave week unmarked so we can retry.
    result["skipped"] = True
    result["reason"] = "order not filled"
    return result
