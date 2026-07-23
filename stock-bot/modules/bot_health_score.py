"""Backward-compatible wrapper — prefer modules.bot_health.calculate_health_score."""

from __future__ import annotations

from typing import Any

from modules.bot_health import calculate_health_score, gather_health_context, _clamp_score, _grade


def compute_bot_health_score(
    *,
    hb: dict[str, Any] | None = None,
    metrics_30d: dict[str, Any] | None = None,
    metrics_alltime: dict[str, Any] | None = None,
    bubble_score: float | None = None,
    stat_arb_pnl_week: float | None = None,
    short_trade_count: int = 0,
    heartbeat_age_min: float | None = None,
) -> dict[str, Any]:
    """Weekly report adapter around calculate_health_score."""
    ctx = gather_health_context(hb)
    if bubble_score is not None:
        try:
            b = float(bubble_score)
            ctx["bubble_score_100"] = b * 100.0 if b <= 1.0 else b
        except (TypeError, ValueError):
            pass
    if short_trade_count:
        ctx["short_fires_week"] = max(int(short_trade_count), ctx.get("short_fires_week") or 0)

    result = calculate_health_score(**ctx)

    if heartbeat_age_min is not None and heartbeat_age_min > 120:
        result = dict(result)
        notes = list(result.get("notes") or [])
        notes.append(f"Stale heartbeat ({heartbeat_age_min:.0f}m)")
        adj = float(result["score"]) - 8
        result["score"] = _clamp_score(adj)
        result["grade"] = _grade(result["score"])
        from modules.bot_health import health_color

        result["color"] = health_color(result["score"])
        result["notes"] = notes

    m30 = metrics_30d or {}
    mall = metrics_alltime or {}
    components = dict(result.get("components") or {})
    if m30.get("sharpe") is not None:
        components["sharpe_30d"] = m30.get("sharpe")
    if mall.get("sharpe") is not None:
        components["sharpe_alltime"] = mall.get("sharpe")
    if stat_arb_pnl_week is not None:
        components["stat_arb_pnl_week"] = round(float(stat_arb_pnl_week), 2)
    components["short_trades_week"] = int(short_trade_count)
    result["components"] = components
    return result
