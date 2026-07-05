"""Bot Health Score (0–100) — lightweight paper-bot health gauge."""

from __future__ import annotations

from typing import Any

import config

_BASE_SCORE = 70.0
_BUBBLE_HIGH = 60.0
_GOOD_FILL_RATE_PCT = 28.0
_EXCESS_NO_ROOM_PCT = 30.0


def _clamp_score(raw: float) -> int:
    return int(round(max(0.0, min(100.0, raw))))


def _grade(score: int) -> str:
    if score >= 85:
        return "Excellent"
    if score >= 70:
        return "Good"
    if score >= 50:
        return "Fair"
    return "Needs attention"


def health_color(score: int) -> str:
    """Semantic color key: green | yellow | red."""
    if score >= 80:
        return "green"
    if score >= 65:
        return "yellow"
    return "red"


def gather_health_context(
    hb: dict[str, Any] | None = None,
    *,
    journal_df=None,
    short_snap: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect inputs for calculate_health_score from heartbeat + live modules."""
    hb = hb or {}
    regime = str(hb.get("regime") or "")

    strong_cluster_count = 0
    bubble_score_100: float | None = None
    try:
        from modules.insider_signal_handler import get_boost_snapshot

        ins = get_boost_snapshot()
        strong_cluster_count = len(ins.get("strong_clusters") or [])
        bub = ins.get("bubble_score_100")
        if bub is not None:
            bubble_score_100 = float(bub)
    except Exception:
        pass

    if bubble_score_100 is None:
        try:
            from modules.bubble_risk import compute_bubble_risk_from_live_context

            ctx = compute_bubble_risk_from_live_context(regime=regime, hb=hb)
            if ctx:
                bubble_score_100 = float(ctx.get("score_100") or 0)
        except Exception:
            pass

    daily = hb.get("entry_skip_daily") or {}
    cycles = int(daily.get("cycles") or 0)
    traded = int(daily.get("traded_cycles") or 0)
    by_cat = dict(daily.get("by_category") or {})
    skipped = max(0, cycles - traded)
    no_room = int(by_cat.get("no_room") or 0)
    no_room_rate_pct = (
        100.0 * no_room / max(skipped, 1) if skipped > 0 else None
    )
    stat_arb_fill_rate_pct = (
        100.0 * traded / max(cycles, 1) if cycles >= 3 else None
    )

    short_fires_week = 0
    if short_snap:
        short_fires_week = len(short_snap.get("recent_fires") or [])
    elif journal_df is not None:
        try:
            from modules.short_activity import gather_short_activity

            sa = gather_short_activity(journal_df=journal_df, regime=regime)
            short_fires_week = len(sa.get("recent_fires") or [])
        except Exception:
            pass

    return {
        "hb": hb,
        "regime": regime,
        "strong_cluster_count": strong_cluster_count,
        "stat_arb_fill_rate_pct": stat_arb_fill_rate_pct,
        "bubble_score_100": bubble_score_100,
        "short_fires_week": short_fires_week,
        "no_room_rate_pct": no_room_rate_pct,
        "cycles": cycles,
        "traded_cycles": traded,
    }


def calculate_health_score(
    *,
    hb: dict[str, Any] | None = None,
    regime: str = "",
    strong_cluster_count: int = 0,
    stat_arb_fill_rate_pct: float | None = None,
    bubble_score_100: float | None = None,
    short_fires_week: int = 0,
    no_room_rate_pct: float | None = None,
    **_: Any,
) -> dict[str, Any]:
    """
    Paper-focused health score (0–100).

    Base 70; +10 strong insider clusters; +8 good stat-arb activity rate;
    -15 high bubble + shorts firing; -10 excessive no_room; regime/tail tweaks.
    """
    hb = hb or {}
    reg = regime or str(hb.get("regime") or "")
    score = float(_BASE_SCORE)
    notes: list[str] = []
    adjustments: list[dict[str, Any]] = [
        {"label": "base", "delta": _BASE_SCORE},
    ]

    def _apply(delta: float, label: str, note: str | None = None) -> None:
        nonlocal score
        score += delta
        adjustments.append({"label": label, "delta": delta})
        if note:
            notes.append(note)

    if strong_cluster_count >= 1:
        _apply(10.0, "insider_clusters", f"Strong insider cluster buys ({strong_cluster_count})")
    if stat_arb_fill_rate_pct is not None and stat_arb_fill_rate_pct >= _GOOD_FILL_RATE_PCT:
        _apply(
            8.0,
            "stat_arb_fill",
            f"Good entry fill rate ({stat_arb_fill_rate_pct:.0f}%)",
        )

    bub = bubble_score_100
    if bub is not None and bub >= _BUBBLE_HIGH and short_fires_week > 0:
        _apply(
            -15.0,
            "bubble_shorts",
            f"High bubble ({bub:.0f}/100) + shorts active ({short_fires_week} fire(s))",
        )
    elif bub is not None and bub >= 75 and short_fires_week > 0:
        _apply(-8.0, "bubble_shorts_mild", f"Very high bubble + shorts ({bub:.0f}/100)")

    if no_room_rate_pct is not None and no_room_rate_pct >= _EXCESS_NO_ROOM_PCT:
        _apply(
            -10.0,
            "no_room",
            f"Excessive no_room rejects ({no_room_rate_pct:.0f}% of skips)",
        )

    reg_u = reg.upper()
    if "RHYME_B" in reg_u:
        _apply(-8.0, "regime_b", "RHYME_B panic — defensive posture")
    elif "RHYME_E" in reg_u:
        _apply(-4.0, "regime_e", "RHYME_E bear — elevated caution")
    elif "RHYME_C" in reg_u or "RHYME_D" in reg_u:
        _apply(3.0, "regime_calm", "Calmer regime (C/D)")

    if hb.get("halted"):
        _apply(-20.0, "halted", "Trading halt active")

    wisdom = hb.get("wisdom") or {}
    if wisdom.get("paused"):
        _apply(-5.0, "wisdom_paused", "Wisdom layer paused entries")
    sm = wisdom.get("sizing_multiplier")
    if sm is not None:
        try:
            mult = float(sm)
            if mult < 0.75:
                _apply(-6.0, "sizing_stress", f"Tail-risk sizing cut ({mult:.2f}×)")
            elif mult < 0.90:
                _apply(-3.0, "sizing_trim", f"Sizing trimmed ({mult:.2f}×)")
        except (TypeError, ValueError):
            pass

    if config.effective_tail_risk_controls():
        dvs = hb.get("dynamic_vol_score")
        if dvs is not None:
            try:
                ann = float(dvs) * (252**0.5)
                ceiling = float(config.effective_vol_ceiling_pct())
                if ann > ceiling:
                    _apply(-5.0, "vol_ceiling", f"Vol above ceiling (~{ann:.0%})")
            except (TypeError, ValueError):
                pass

    final = _clamp_score(score)
    return {
        "score": final,
        "grade": _grade(final),
        "color": health_color(final),
        "notes": notes or ["Baseline healthy — no major flags"],
        "adjustments": adjustments,
        "components": {
            "strong_cluster_count": strong_cluster_count,
            "stat_arb_fill_rate_pct": stat_arb_fill_rate_pct,
            "bubble_score_100": bubble_score_100,
            "short_fires_week": short_fires_week,
            "no_room_rate_pct": no_room_rate_pct,
            "regime": reg,
        },
    }


def format_health_line(result: dict[str, Any] | None = None, *, hb: dict | None = None) -> str:
    """Single-line banner: Health: 82/100 (Good)."""
    if result is None:
        ctx = gather_health_context(hb)
        result = calculate_health_score(**ctx)
    score = int(result.get("score") or 0)
    grade = str(result.get("grade") or "")
    return f">>> Bot Health: {score}/100 ({grade}) <<<"


def format_health_telegram(result: dict[str, Any] | None = None, *, hb: dict | None = None) -> str:
    if result is None:
        ctx = gather_health_context(hb)
        result = calculate_health_score(**ctx)
    score = int(result.get("score") or 0)
    grade = str(result.get("grade") or "")
    lines = [f"Bot Health: {score}/100 — {grade}"]
    for note in (result.get("notes") or [])[:3]:
        lines.append(f"  • {note}")
    return "\n".join(lines)
