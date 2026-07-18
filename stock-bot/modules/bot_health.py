"""Bot Health Score (0–100) — lightweight paper-bot health gauge."""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any

import config

logger = logging.getLogger(__name__)

_BASE_SCORE = 75.0
_MAX_TOTAL_PENALTY = 22.0
_BUBBLE_HIGH = 60.0
_GOOD_FILL_RATE_PCT = 28.0
_EXCESS_NO_ROOM_PCT = 30.0
_AUX_CACHE_TTL_SEC = 300.0

_AUX_CACHE: dict[str, tuple[float, Any]] = {}


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
    """Semantic color: green (>=85), yellow (70-84), red (<70)."""
    if score >= 85:
        return "green"
    if score >= 70:
        return "yellow"
    return "red"


def _cached(key: str, fn, ttl: float = _AUX_CACHE_TTL_SEC):
    now = time.monotonic()
    hit = _AUX_CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = fn()
    _AUX_CACHE[key] = (now, value)
    return value


def _market_quiet_tape() -> bool:
    """Weekends / outside regular NYSE hours — RVOL/ORB/Catalyst often empty."""
    try:
        from zoneinfo import ZoneInfo

        et = dt.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        et = dt.datetime.now()
    if et.weekday() >= 5:
        return True
    minutes = et.hour * 60 + et.minute
    open_m = 9 * 60 + 30
    close_m = 16 * 60
    return minutes < open_m or minutes >= close_m


def _scanner_health_snapshot() -> dict[str, Any]:
    """RVOL / ORB / catalyst hit counts (cached; skipped on quiet tape)."""
    quiet = _market_quiet_tape()
    snap: dict[str, Any] = {
        "quiet_tape": quiet,
        "rvol_hits": 0,
        "orb_hits": 0,
        "catalyst_hits": 0,
        "scanners_on": bool(
            config.effective_rvol_scanner_enabled()
            or config.effective_orb_enabled()
            or config.effective_catalyst_scoring_enabled()
        ),
    }
    if quiet or not snap["scanners_on"]:
        return snap
    try:
        from modules.pipeline_strategies import load_pipeline_data

        data = load_pipeline_data(interval="1d")
        if data is None or getattr(data, "empty", True):
            return snap
        cols = [
            str(c)
            for c in data.columns
            if str(c) in ("SPY", "VTI", "NVDA", "AAPL", "MSFT", "QQQ")
            or config._nyse_eligible_symbol(str(c))
        ][:18]
        if cols:
            data = data[cols]
        if config.effective_rvol_scanner_enabled():
            from modules.volume_analysis import get_high_rvol_stocks

            snap["rvol_hits"] = len(
                get_high_rvol_stocks(
                    data, min_rvol=float(config.RVOL_MIN_THRESHOLD), limit=5
                )
            )
        if config.effective_orb_enabled():
            from modules.volume_analysis import get_orb_signals

            snap["orb_hits"] = len(
                get_orb_signals(data, minutes=int(config.ORB_BREAKOUT_MINUTES), limit=5)
            )
        if config.effective_catalyst_scoring_enabled():
            from modules.catalyst_scoring import get_top_catalyst_stocks

            snap["catalyst_hits"] = len(
                get_top_catalyst_stocks(
                    data, min_score=float(config.CATALYST_MIN_SCORE), limit=5
                )
            )
    except Exception as exc:
        logger.debug("scanner activity snapshot unavailable for health score: %s", exc)
    return snap


def _atr_sizing_ok() -> bool:
    if not config.effective_atr_sizing_enabled():
        return False
    try:
        from modules.pipeline_strategies import load_pipeline_data
        from modules.risk_management import get_atr_risk_size

        data = load_pipeline_data(interval="1d")
        sym = config.SPY_BOT_SYMBOL
        result = get_atr_risk_size(100_000.0, sym, data)
        return (
            str(result.get("method") or "") == "atr"
            and float(result.get("notional") or 0) > 0
            and float(result.get("atr") or 0) > 0
        )
    except Exception:
        return False


def _clean_news_pool() -> bool:
    try:
        from modules.historical_news import (
            clean_headline,
            get_historical_headlines,
            is_financial_headline,
            is_junk_headline,
        )

        rows = get_historical_headlines(dt.date.today(), days_back=2)
        if rows:
            return all(
                is_financial_headline(str(r.get("title") or ""))
                and not is_junk_headline(str(r.get("title") or ""))
                for r in rows
            )
    except Exception as exc:
        logger.debug("historical headline quality check unavailable: %s", exc)
    try:
        from modules.thinking_news import get_news_digest_for_thinking

        digest = get_news_digest_for_thinking(max_items=6)
        lines = digest.get("headlines") or []
        if not lines:
            return True
        from modules.historical_news import (
            clean_headline,
            is_financial_headline,
            is_junk_headline,
        )

        return all(
            is_financial_headline(clean_headline(str(line)))
            and not is_junk_headline(str(line))
            for line in lines
        )
    except Exception:
        return True


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
    insider_signal_count = 0
    bubble_score_100: float | None = None
    try:
        from modules.insider_signal_handler import get_boost_snapshot
        from modules.insider_monitor import get_recent_insider_signals

        ins = get_boost_snapshot()
        strong_cluster_count = len(ins.get("strong_clusters") or [])
        insider_signal_count = len(get_recent_insider_signals(days=7, min_score=60))
        bub = ins.get("bubble_score_100")
        if bub is not None:
            bubble_score_100 = float(bub)
    except Exception as exc:
        logger.debug("insider snapshot unavailable for health score: %s", exc)

    if bubble_score_100 is None:
        try:
            from modules.bubble_risk import compute_bubble_risk_from_live_context

            ctx = compute_bubble_risk_from_live_context(regime=regime, hb=hb)
            if ctx:
                bubble_score_100 = float(ctx.get("score_100") or 0)
        except Exception as exc:
            logger.debug("bubble risk context unavailable for health score: %s", exc)

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
        except Exception as exc:
            logger.debug("short activity snapshot unavailable for health score: %s", exc)

    scanner = _cached("scanner", _scanner_health_snapshot)
    atr_ok = _cached("atr_ok", _atr_sizing_ok, ttl=600.0)
    news_clean = _cached("news_clean", _clean_news_pool, ttl=600.0)

    sharpe_30d = None
    sharpe_all = None
    sharpe_trend = "flat"
    sharpe_trend_delta = None
    sharpe_trend_note = ""
    try:
        from modules.sharpe_history import sharpe_trend_for_health

        st = sharpe_trend_for_health()
        sharpe_30d = st.get("sharpe_30d")
        sharpe_all = st.get("sharpe_all")
        sharpe_trend = str(st.get("trend") or "flat")
        sharpe_trend_delta = st.get("trend_delta")
        sharpe_trend_note = str(st.get("note") or "")
    except Exception as exc:
        logger.debug("sharpe trend for health unavailable: %s", exc)

    thinking = {
        "enabled": False,
        "ollama_ok": False,
        "status": "OFF",
        "fallback": False,
        "age_hours": None,
        "confidence": None,
        "validation_score": None,
    }
    try:
        from modules.thinking_engine import thinking_health_snapshot

        thinking = _cached("thinking", thinking_health_snapshot, ttl=120.0)
    except Exception as exc:
        logger.debug("thinking health snapshot unavailable: %s", exc)

    return {
        "hb": hb,
        "regime": regime,
        "strong_cluster_count": strong_cluster_count,
        "insider_signal_count": insider_signal_count,
        "stat_arb_fill_rate_pct": stat_arb_fill_rate_pct,
        "bubble_score_100": bubble_score_100,
        "short_fires_week": short_fires_week,
        "no_room_rate_pct": no_room_rate_pct,
        "cycles": cycles,
        "traded_cycles": traded,
        "quiet_tape": bool(scanner.get("quiet_tape")),
        "rvol_hits": int(scanner.get("rvol_hits") or 0),
        "orb_hits": int(scanner.get("orb_hits") or 0),
        "catalyst_hits": int(scanner.get("catalyst_hits") or 0),
        "scanners_on": bool(scanner.get("scanners_on")),
        "atr_sizing_ok": bool(atr_ok),
        "news_pool_clean": bool(news_clean),
        "sharpe_30d": sharpe_30d,
        "sharpe_all": sharpe_all,
        "sharpe_trend": sharpe_trend,
        "sharpe_trend_delta": sharpe_trend_delta,
        "sharpe_trend_note": sharpe_trend_note,
        "thinking_enabled": bool(thinking.get("enabled")),
        "thinking_ollama_ok": bool(thinking.get("ollama_ok")),
        "thinking_status": str(thinking.get("status") or "OFF"),
        "thinking_fallback": bool(thinking.get("fallback")),
        "thinking_age_hours": thinking.get("age_hours"),
        "thinking_confidence": thinking.get("confidence"),
        "thinking_validation_score": thinking.get("validation_score"),
    }


def calculate_health_score(
    *,
    hb: dict[str, Any] | None = None,
    regime: str = "",
    strong_cluster_count: int = 0,
    insider_signal_count: int = 0,
    stat_arb_fill_rate_pct: float | None = None,
    bubble_score_100: float | None = None,
    short_fires_week: int = 0,
    no_room_rate_pct: float | None = None,
    quiet_tape: bool = False,
    rvol_hits: int = 0,
    orb_hits: int = 0,
    catalyst_hits: int = 0,
    scanners_on: bool = False,
    atr_sizing_ok: bool = False,
    news_pool_clean: bool = False,
    sharpe_30d: float | None = None,
    sharpe_all: float | None = None,
    sharpe_trend: str | None = None,
    sharpe_trend_delta: float | None = None,
    sharpe_trend_note: str | None = None,
    thinking_enabled: bool = False,
    thinking_ollama_ok: bool = False,
    thinking_status: str | None = None,
    thinking_fallback: bool = False,
    thinking_age_hours: float | None = None,
    thinking_confidence: float | None = None,
    thinking_validation_score: float | None = None,
    **_: Any,
) -> dict[str, Any]:
    """
    Paper-focused health score (0–100).

    Baseline 75 with positive v1.5 stack factors; penalties capped at 22 pts.
    Quiet tape (weekends / off-hours): no scanner-empty penalties.
    Thinking engine (Ollama) contributes when enabled for the active book.
    """
    hb = hb or {}
    reg = regime or str(hb.get("regime") or "")
    score = float(_BASE_SCORE)
    penalty_used = 0.0
    notes: list[str] = []
    adjustments: list[dict[str, Any]] = [
        {"label": "base", "delta": _BASE_SCORE},
    ]

    def _apply(delta: float, label: str, note: str | None = None) -> None:
        nonlocal score, penalty_used
        if delta < 0:
            room = _MAX_TOTAL_PENALTY - penalty_used
            if room <= 0:
                return
            applied = -min(abs(delta), room)
            penalty_used += abs(applied)
            delta = applied
        score += delta
        adjustments.append({"label": label, "delta": delta})
        if note:
            notes.append(note)

    reg_u = reg.upper()
    if "RHYME_C" in reg_u or "RHYME_D" in reg_u:
        _apply(8.0, "regime_stable", "Stable regime (C/D)")
    elif "RHYME_A" in reg_u:
        _apply(6.0, "regime_stable", "Risk-on regime (A)")
    elif "RHYME_E" in reg_u:
        _apply(5.0, "regime_stable", "Bear regime managed (E)")
    elif reg_u and "RHYME_B" not in reg_u:
        _apply(5.0, "regime_stable", "Regime stable")

    if config.effective_atr_sizing_enabled():
        if atr_sizing_ok:
            _apply(5.0, "atr_sizing", "ATR sizing active (SPY probe)")
        else:
            _apply(2.0, "atr_sizing_cfg", "ATR sizing enabled")

    # Thinking engine (system-wide; paper ON / live OFF by default)
    if thinking_enabled or config.effective_thinking_engine_enabled():
        status = str(thinking_status or "").upper()
        age = thinking_age_hours
        conf = thinking_confidence
        if status == "ON" and not thinking_fallback:
            _apply(5.0, "thinking_ok", "Thinking engine ON (LLM)")
            try:
                if conf is not None and float(conf) >= 0.70:
                    _apply(2.0, "thinking_conf", f"Thinking conf {float(conf):.0%}")
            except (TypeError, ValueError):
                pass
            try:
                vs = thinking_validation_score
                if vs is not None and float(vs) >= 70:
                    _apply(2.0, "thinking_valid", f"Thinking validation {float(vs):.0f}")
            except (TypeError, ValueError):
                pass
        elif status == "FALLBACK" or thinking_fallback:
            _apply(2.0, "thinking_fallback", "Thinking heuristic fallback active")
            if not thinking_ollama_ok:
                _apply(-3.0, "thinking_ollama_down", "Ollama unreachable for thinking")
        else:
            _apply(1.0, "thinking_enabled", "Thinking engine enabled")
        if age is not None and age > 48:
            _apply(-5.0, "thinking_stale", f"Thinking snapshot very stale ({age:.0f}h)")
        elif age is not None and age > 36:
            _apply(-3.0, "thinking_stale", f"Thinking snapshot stale ({age:.0f}h)")

    clusters = int(strong_cluster_count or 0)
    signals = int(insider_signal_count or 0)
    if clusters >= 2:
        _apply(10.0, "insider_clusters", f"Strong insider clusters ({clusters})")
    elif clusters == 1:
        _apply(8.0, "insider_clusters", "Strong insider cluster")
    elif signals >= 5:
        _apply(8.0, "insider_signals", f"Quality insider signals ({signals})")
    elif signals >= 1:
        _apply(5.0, "insider_signals", f"Insider signals present ({signals})")

    # Long-term Sharpe trend (permanent history module).
    trend = str(sharpe_trend or "").lower()
    if trend == "improving":
        _apply(5.0, "sharpe_trend_up", sharpe_trend_note or "Sharpe 30d improving")
    elif trend == "deteriorating":
        _apply(-5.0, "sharpe_trend_down", sharpe_trend_note or "Sharpe 30d deteriorating")
    elif sharpe_all is not None:
        try:
            all_s = float(sharpe_all)
            if all_s >= 1.25:
                _apply(4.0, "sharpe_all_strong", f"All-time Sharpe strong ({all_s:.2f})")
            elif all_s >= 0.75:
                _apply(2.0, "sharpe_all_ok", f"All-time Sharpe OK ({all_s:.2f})")
            elif all_s < 0.0:
                _apply(-4.0, "sharpe_all_neg", f"All-time Sharpe negative ({all_s:.2f})")
        except (TypeError, ValueError):
            pass

    tail_guard_triggered = False
    if hb.get("halted"):
        tail_guard_triggered = True
        _apply(-20.0, "halted", "Trading halt active")

    wisdom = hb.get("wisdom") or {}
    if wisdom.get("paused"):
        tail_guard_triggered = True
        _apply(-5.0, "wisdom_paused", "Wisdom layer paused entries")

    sm = wisdom.get("sizing_multiplier")
    sizing_stress = False
    if sm is not None:
        try:
            mult = float(sm)
            if mult < 0.75:
                sizing_stress = True
                tail_guard_triggered = True
                _apply(-6.0, "sizing_stress", f"Tail-risk sizing cut ({mult:.2f}x)")
            elif mult < 0.90:
                sizing_stress = True
                _apply(-3.0, "sizing_trim", f"Sizing trimmed ({mult:.2f}x)")
        except (TypeError, ValueError):
            pass

    vol_ceiling_hit = False
    if config.effective_tail_risk_controls():
        dvs = hb.get("dynamic_vol_score")
        if dvs is not None:
            try:
                ann = float(dvs) * (252**0.5)
                ceiling = float(config.effective_vol_ceiling_pct())
                if ann > ceiling:
                    vol_ceiling_hit = True
                    tail_guard_triggered = True
                    _apply(-5.0, "vol_ceiling", f"Vol above ceiling (~{ann:.0%})")
            except (TypeError, ValueError):
                pass

    if config.effective_tail_risk_controls() and not tail_guard_triggered:
        _apply(8.0, "tail_guard_clear", "Tail-risk guards clear")

    if news_pool_clean:
        _apply(5.0, "news_clean", "Clean financial headline pool")

    if scanners_on:
        if quiet_tape:
            _apply(3.0, "quiet_tape", "Quiet tape — scanner idle expected")
        else:
            if rvol_hits > 0:
                _apply(3.0, "rvol_active", f"RVOL setups ({rvol_hits})")
            else:
                _apply(-2.0, "rvol_idle", "No RVOL setups (active session)")
            if orb_hits > 0:
                _apply(3.0, "orb_active", f"ORB breakouts ({orb_hits})")
            else:
                _apply(-1.0, "orb_idle", "No ORB breakouts (active session)")
            if catalyst_hits > 0:
                _apply(3.0, "catalyst_active", f"Catalyst hits ({catalyst_hits})")
            else:
                _apply(-1.0, "catalyst_idle", "No catalyst hits (active session)")

    if stat_arb_fill_rate_pct is not None and stat_arb_fill_rate_pct >= _GOOD_FILL_RATE_PCT:
        _apply(
            8.0,
            "stat_arb_fill",
            f"Good entry fill rate ({stat_arb_fill_rate_pct:.0f}%)",
        )

    bub = bubble_score_100
    if bub is not None and bub >= _BUBBLE_HIGH and short_fires_week > 0:
        _apply(
            -12.0,
            "bubble_shorts",
            f"High bubble ({bub:.0f}/100) + shorts active ({short_fires_week} fire(s))",
        )
    elif bub is not None and bub >= 75 and short_fires_week > 0:
        _apply(-6.0, "bubble_shorts_mild", f"Very high bubble + shorts ({bub:.0f}/100)")

    if no_room_rate_pct is not None and no_room_rate_pct >= _EXCESS_NO_ROOM_PCT:
        _apply(
            -8.0,
            "no_room",
            f"Excessive no_room rejects ({no_room_rate_pct:.0f}% of skips)",
        )

    if "RHYME_B" in reg_u:
        _apply(-6.0, "regime_b", "RHYME_B panic — defensive posture")
    elif "RHYME_E" in reg_u and not sizing_stress:
        _apply(-2.0, "regime_e", "RHYME_E bear — elevated caution")

    try:
        from modules.strategy_performance import strategy_health_score_bonus

        bonus, strat_note = strategy_health_score_bonus()
        if bonus and strat_note:
            _apply(bonus, "strategy_performance", strat_note)
    except Exception as exc:
        logger.debug("strategy performance bonus unavailable for health score: %s", exc)

    final = _clamp_score(score)
    return {
        "score": final,
        "grade": _grade(final),
        "color": health_color(final),
        "notes": notes or ["Baseline healthy — v1.5 stack OK"],
        "adjustments": adjustments,
        "components": {
            "strong_cluster_count": strong_cluster_count,
            "insider_signal_count": insider_signal_count,
            "stat_arb_fill_rate_pct": stat_arb_fill_rate_pct,
            "bubble_score_100": bubble_score_100,
            "short_fires_week": short_fires_week,
            "no_room_rate_pct": no_room_rate_pct,
            "regime": reg,
            "quiet_tape": quiet_tape,
            "rvol_hits": rvol_hits,
            "orb_hits": orb_hits,
            "catalyst_hits": catalyst_hits,
            "atr_sizing_ok": atr_sizing_ok,
            "news_pool_clean": news_pool_clean,
            "sharpe_30d": sharpe_30d,
            "sharpe_all": sharpe_all,
            "sharpe_trend": sharpe_trend,
            "sharpe_trend_delta": sharpe_trend_delta,
            "penalty_used": round(penalty_used, 1),
        },
    }


def format_health_line(result: dict[str, Any] | None = None, *, hb: dict | None = None) -> str:
    """Single-line banner: Health: 92/100 (Excellent)."""
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
    band = {"green": "Green", "yellow": "Yellow", "red": "Red"}.get(
        str(result.get("color") or ""), ""
    )
    head = f"Bot Health: {score}/100 — {grade}"
    if band:
        head += f" ({band})"
    lines = [head]
    for note in (result.get("notes") or [])[:4]:
        lines.append(f"  • {note}")
    return "\n".join(lines)
