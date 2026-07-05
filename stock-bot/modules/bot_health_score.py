"""Bot Health Score (0–100) for Realistic Research weekly monitoring."""

from __future__ import annotations

from typing import Any


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
    """Return score 0–100 with component breakdown for weekly reports."""
    hb = hb or {}
    m30 = metrics_30d or {}
    mall = metrics_alltime or {}
    score = 100.0
    notes: list[str] = []
    components: dict[str, float | int | str | None] = {}

    age = heartbeat_age_min
    if age is None:
        try:
            age = float(hb.get("heartbeat_age_min") or 0)
        except (TypeError, ValueError):
            age = None
    if age is not None:
        components["heartbeat_age_min"] = round(age, 1)
        if age > 180:
            score -= 25
            notes.append("Stale heartbeat (>3h)")
        elif age > 60:
            score -= 10
            notes.append("Heartbeat >1h old")

    if hb.get("halted"):
        score -= 30
        notes.append("Trading halt active")

    wisdom = hb.get("wisdom") or {}
    if wisdom.get("paused"):
        score -= 8
        notes.append("Wisdom layer paused")
    sm = wisdom.get("sizing_multiplier")
    if sm is not None:
        try:
            mult = float(sm)
            components["sizing_multiplier"] = mult
            if mult < 0.75:
                score -= 12
                notes.append(f"Sizing multiplier {mult:.2f}×")
            elif mult < 0.95:
                score -= 5
        except (TypeError, ValueError):
            pass

    sh30 = m30.get("sharpe")
    if sh30 is not None:
        try:
            sh = float(sh30)
            components["sharpe_30d"] = round(sh, 2)
            if sh < 0:
                score -= 15
                notes.append("Negative 30d Sharpe")
            elif sh < 0.5:
                score -= 8
                notes.append("Weak 30d Sharpe")
            elif sh >= 1.5:
                score += 5
        except (TypeError, ValueError):
            pass

    sh_all = mall.get("sharpe")
    if sh_all is not None:
        try:
            components["sharpe_alltime"] = round(float(sh_all), 2)
        except (TypeError, ValueError):
            pass

    dd = m30.get("max_drawdown_pct")
    if dd is not None:
        try:
            dd_f = float(dd)
            components["max_dd_30d_pct"] = round(dd_f, 2)
            if dd_f <= -12:
                score -= 15
                notes.append(f"Deep 30d drawdown ({dd_f:.1f}%)")
            elif dd_f <= -8:
                score -= 8
        except (TypeError, ValueError):
            pass

    if bubble_score is not None:
        try:
            bub = float(bubble_score)
            components["bubble_score"] = round(bub, 3)
            if bub >= 0.65:
                score -= 10
                notes.append(f"Elevated bubble risk ({bub:.2f})")
            elif bub >= 0.50:
                score -= 4
        except (TypeError, ValueError):
            pass

    components["short_trades_week"] = int(short_trade_count)
    if stat_arb_pnl_week is not None:
        components["stat_arb_pnl_week"] = round(float(stat_arb_pnl_week), 2)

    dvs = hb.get("dynamic_vol_score")
    if dvs is not None:
        try:
            import config

            ann = float(dvs) * (252**0.5)
            ceiling = float(config.effective_vol_ceiling_pct())
            components["ann_vol_est"] = round(ann, 4)
            if ann > ceiling:
                score -= 10
                notes.append(f"Vol above ceiling ({ann:.0%})")
        except (TypeError, ValueError):
            pass

    final = int(round(max(0.0, min(100.0, score))))
    if final >= 85:
        grade = "Excellent"
    elif final >= 70:
        grade = "Good"
    elif final >= 50:
        grade = "Fair"
    else:
        grade = "Needs attention"

    return {
        "score": final,
        "grade": grade,
        "components": components,
        "notes": notes or ["No issues flagged"],
    }
