"""Insider Signal Boost Strategy v1.5 — tiered boosts for paper research only.

Targets existing edges (momentum, stat arb, protective shorts) with strict risk
guards. Does not create standalone trades — only tilts ranking/sizing within
sleeves that already have signals.
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any

import config
from modules.insider_monitor import (
    _format_value,
    _sig_type,
    _ticker_in_strong_sector,
    get_cluster_buy_signals,
    get_executive_sell_signals,
    get_recent_insider_signals,
)
from modules.safe_io import read_json_file, write_json_file

logger = logging.getLogger(__name__)

_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "insider_signal_handler_state.json"
_IMPACT_LOG = Path(__file__).resolve().parent.parent / "logs" / "insider_impact.log"
_MODERATE_MIN_SCORE = 70
_SHORT_SELL_MIN_SCORE = 75
_CACHE: dict[str, Any] = {}
_BOOST_TRADE_COUNTERS: dict[str, int] = {"momentum": 0, "stat_arb": 0, "short": 0}


def reset_insider_boost_trade_counters() -> None:
    global _BOOST_TRADE_COUNTERS
    _BOOST_TRADE_COUNTERS = {"momentum": 0, "stat_arb": 0, "short": 0}


def record_insider_boost_trade(category: str) -> None:
    key = str(category or "").strip().lower()
    if key not in _BOOST_TRADE_COUNTERS:
        return
    _BOOST_TRADE_COUNTERS[key] = int(_BOOST_TRADE_COUNTERS.get(key, 0)) + 1


def insider_boost_trade_counts() -> dict[str, int]:
    total = sum(int(v) for v in _BOOST_TRADE_COUNTERS.values())
    return {**_BOOST_TRADE_COUNTERS, "total": total}


def _load_state() -> dict[str, Any]:
    if _CACHE.get("state") is not None:
        return dict(_CACHE["state"])
    state = read_json_file(_STATE_FILE) or {}
    _CACHE["state"] = state
    return dict(state)


def _save_state(state: dict[str, Any]) -> None:
    write_json_file(_STATE_FILE, state)
    _CACHE["state"] = dict(state)


def _signal_value(sig: dict[str, Any]) -> float:
    try:
        return float(sig.get("value") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _classify_buy_tier(sig: dict[str, Any]) -> int:
    """Tier 1: cluster >=5 or CEO buy. Tier 2: 3-4 insiders or large exec. Tier 3: moderate."""
    st = _sig_type(sig)
    insiders = int(sig.get("insiders_count") or 0)
    score = int(sig.get("score") or 0)
    role = str(sig.get("role") or "").lower()
    val = _signal_value(sig)

    if st == "insider_buy" and role == "ceo":
        return 1
    if st == "cluster_buy":
        if (
            insiders >= config.INSIDER_TIER1_CLUSTER_MIN
            and score >= config.INSIDER_TIER1_CLUSTER_MIN_SCORE
        ):
            return 1
        if insiders >= config.INSIDER_TIER2_CLUSTER_MIN:
            return 2
    if role in ("ceo", "cfo") and val >= config.INSIDER_LARGE_EXEC_VALUE_USD:
        return 2
    if score >= _MODERATE_MIN_SCORE and insiders >= 2:
        return 3
    if score >= _MODERATE_MIN_SCORE and st in ("cluster_buy", "insider_buy"):
        return 3
    return 0


def _classify_sell_tier(sig: dict[str, Any]) -> int:
    """Tier 1/2 executive sells for short candidate ranking."""
    role = str(sig.get("role") or "").lower()
    score = int(sig.get("score") or 0)
    val = _signal_value(sig)
    if role not in ("ceo", "cfo", "executive"):
        return 0
    if score >= _SHORT_SELL_MIN_SCORE and role in ("ceo", "cfo"):
        return 1
    if val >= config.INSIDER_LARGE_EXEC_VALUE_USD or score >= _MODERATE_MIN_SCORE:
        return 2
    return 0


def _tier_momentum_boost(tier: int, *, strong_sector: bool = False) -> float:
    cap = float(config.INSIDER_CLUSTER_BOOST_MAX)
    table = {
        1: float(config.INSIDER_TIER1_MOMENTUM_BOOST),
        2: float(config.INSIDER_TIER2_MOMENTUM_BOOST),
        3: float(config.INSIDER_TIER3_MOMENTUM_BOOST),
    }
    boost = table.get(tier, 0.0)
    if strong_sector and tier > 0:
        boost = round(min(cap, boost + 0.02), 4)
    return round(min(boost, cap), 4)


def _tier_stat_arb_mult(tier: int) -> float:
    table = {
        1: float(config.INSIDER_TIER1_STAT_ARB_MULT),
        2: float(config.INSIDER_TIER2_STAT_ARB_MULT),
        3: 1.06,
    }
    return round(table.get(tier, 1.0), 4)


def _tier_short_base(tier: int, *, bubble_score_100: float, regime: str = "") -> float:
    if tier <= 0:
        return 0.0
    base = float(
        config.INSIDER_TIER1_SHORT_BOOST if tier == 1 else config.INSIDER_TIER2_SHORT_BOOST
    )
    cap = float(config.INSIDER_SELL_SHORT_BOOST_MAX)
    rhyme_e = "RHYME_E" in str(regime or "")
    if bubble_score_100 > config.INSIDER_BUBBLE_SHORT_AMPLIFY_SCORE or rhyme_e:
        base = float(config.INSIDER_SHORT_AMPLIFIED_BOOST)
    return round(min(max(base, 0.0), cap), 4)


def _stack_scanner_extras(sym: str, boost: float) -> float:
    """Optional RVOL/ORB/catalyst extras on top of tier momentum boost."""
    cap = float(config.INSIDER_CLUSTER_BOOST_MAX)
    if sym and config.effective_rvol_scanner_enabled():
        try:
            from modules.volume_analysis import insider_cluster_rvol_momentum_extra

            boost = round(min(boost + insider_cluster_rvol_momentum_extra(sym), cap), 4)
        except Exception as exc:
            logger.debug("RVOL cluster momentum boost skipped for %s: %s", sym, exc)
    if sym and config.effective_orb_enabled():
        try:
            from modules.orb_strategy import orb_insider_cluster_extra

            boost = round(min(boost + orb_insider_cluster_extra(sym), cap), 4)
        except Exception as exc:
            logger.debug("ORB cluster boost skipped for %s: %s", sym, exc)
    if sym and config.effective_catalyst_scoring_enabled():
        try:
            from modules.catalyst_scoring import catalyst_insider_cluster_extra

            boost = round(min(boost + catalyst_insider_cluster_extra(sym), cap), 4)
        except Exception as exc:
            logger.debug("catalyst cluster boost skipped for %s: %s", sym, exc)
    return round(min(boost, cap), 4)


def _resolve_market_context(
    *,
    bubble_score_100: float | None,
    regime: str | None,
) -> tuple[float, str, bool, bool]:
    score_100 = float(bubble_score_100 or 0.0)
    reg = str(regime or "")
    if bubble_score_100 is None:
        try:
            from modules.bubble_risk import compute_bubble_risk_from_live_context

            ctx = compute_bubble_risk_from_live_context(regime=reg) or {}
            score_100 = float(ctx.get("score_100") or 0.0)
            if not reg:
                reg = str(ctx.get("regime") or "")
        except Exception as exc:
            logger.debug("bubble context fetch failed for insider gate: %s", exc)
    rhyme_b = "RHYME_B" in reg
    rhyme_b_panic = rhyme_b and "Panic" in reg
    bear_regime = rhyme_b or "RHYME_E" in reg
    return score_100, reg, rhyme_b_panic, bear_regime


def _apply_risk_guard(
    *,
    momentum_boosts: dict[str, float],
    stat_arb_boosts: dict[str, float],
    short_boosts: dict[str, dict[str, Any]],
    bubble_score_100: float,
    rhyme_b_panic: bool,
    bear_regime: bool,
    regime: str,
) -> tuple[dict[str, float], dict[str, float], dict[str, dict[str, Any]], list[str]]:
    notes: list[str] = []
    if not config.effective_insider_risk_guard_enabled():
        return momentum_boosts, stat_arb_boosts, short_boosts, notes

    bullish_suppress = float(config.INSIDER_BUBBLE_BULLISH_SUPPRESS)
    legacy_suppress = float(config.INSIDER_RISK_BUBBLE_SUPPRESS)
    suppress_at = min(bullish_suppress, legacy_suppress)

    if bubble_score_100 > suppress_at:
        if momentum_boosts:
            momentum_boosts = {k: 0.0 for k in momentum_boosts}
            notes.append(f"bullish boosts suppressed (bubble {bubble_score_100:.0f}>{suppress_at:.0f})")
        if stat_arb_boosts:
            stat_arb_boosts = {
                k: 1.0 + max(0.0, v - 1.0) * 0.2 for k, v in stat_arb_boosts.items()
            }
            notes.append("stat arb insider mult damped 80%")

    if "RHYME_B" in regime and momentum_boosts:
        rhyme_mult = float(config.INSIDER_RHYME_B_BULLISH_MULT)
        momentum_boosts = {k: round(v * rhyme_mult, 4) for k, v in momentum_boosts.items()}
        stat_arb_boosts = {
            k: round(1.0 + (v - 1.0) * rhyme_mult, 4) for k, v in stat_arb_boosts.items()
        }
        notes.append(f"RHYME_B: bullish insider boosts halved ({rhyme_mult:.0%})")

    if bear_regime and short_boosts:
        cap = float(config.INSIDER_SELL_SHORT_BOOST_MAX)
        for sym, meta in short_boosts.items():
            boosted = float(meta.get("base") or 0.0) * 1.25
            meta["base"] = round(min(boosted, cap * 1.08), 4)
            meta["bear_regime_amplified"] = True
            short_boosts[sym] = meta
        notes.append("short boosts amplified (bear regime)")

    if rhyme_b_panic and short_boosts:
        cap = float(config.INSIDER_SELL_SHORT_BOOST_MAX)
        for sym, meta in short_boosts.items():
            boosted = float(meta.get("base") or 0.0) * 1.15
            meta["base"] = round(min(boosted, cap * 1.10), 4)
            meta["rhyme_b_amplified"] = True
            short_boosts[sym] = meta
        notes.append("short boosts amplified (RHYME_B panic)")

    return momentum_boosts, stat_arb_boosts, short_boosts, notes


def _insider_boosted_holdings(executor) -> set[str]:
    """Symbols currently held with an active insider momentum boost."""
    if executor is None:
        return set()
    state = _load_state()
    boosts = state.get("momentum_boosts") or {}
    held: set[str] = set()
    try:
        positions = executor._get_positions()
    except Exception:
        return held
    for pos in positions:
        try:
            qty = float(pos.qty)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        sym = config.normalize_symbol(str(pos.symbol))
        if float(boosts.get(sym, 0.0)) > 0:
            held.add(sym)
    return held


def apply_insider_signals_to_strategies(
    *,
    bubble_score_100: float | None = None,
    regime: str | None = None,
) -> dict[str, Any]:
    """Evaluate insider signals and cache tiered boosts for momentum, stat arb, shorts."""
    if not config.effective_insider_signal_boost_enabled():
        empty = {
            "enabled": False,
            "version": "1.5",
            "applied_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "strong_clusters": [],
            "short_candidates": [],
            "momentum_boosts": {},
            "stat_arb_boosts": {},
            "short_boosts": {},
            "signal_tiers": {},
            "top_signals": [],
            "summary": "insider signal boost off",
            "risk_guard_notes": [],
        }
        _save_state(empty)
        return empty

    bubble_100, reg, rhyme_b_panic, bear_regime = _resolve_market_context(
        bubble_score_100=bubble_score_100,
        regime=regime,
    )

    clusters = get_cluster_buy_signals(min_insiders=2, days=7)
    ceo_buys = [
        s
        for s in get_recent_insider_signals(days=7, min_score=60)
        if _sig_type(s) == "insider_buy"
        and str(s.get("role") or "").lower() == "ceo"
        and s.get("ticker")
    ]
    exec_sells = get_executive_sell_signals(min_value=100_000, days=7)

    momentum_boosts: dict[str, float] = {}
    stat_arb_boosts: dict[str, float] = {}
    signal_tiers: dict[str, int] = {}

    buy_sources = list(clusters) + [c for c in ceo_buys if c not in clusters]
    for sig in buy_sources:
        sym = config.normalize_symbol(str(sig.get("ticker") or ""))
        if not sym:
            continue
        tier = _classify_buy_tier(sig)
        if tier <= 0:
            continue
        prev_tier = int(signal_tiers.get(sym, 0))
        if tier > prev_tier:
            signal_tiers[sym] = tier
        strong = _ticker_in_strong_sector(sym)
        mom = _stack_scanner_extras(sym, _tier_momentum_boost(tier, strong_sector=strong))
        sa = _tier_stat_arb_mult(tier)
        if sym in momentum_boosts:
            mom = max(mom, momentum_boosts[sym])
            sa = max(sa, stat_arb_boosts.get(sym, 1.0))
        momentum_boosts[sym] = mom
        stat_arb_boosts[sym] = sa

    strong_clusters = [
        c
        for c in clusters
        if _classify_buy_tier(c) >= 2
    ]
    short_candidates: list[dict[str, Any]] = []
    short_boosts: dict[str, dict[str, Any]] = {}
    for sell in exec_sells:
        tier = _classify_sell_tier(sell)
        if tier <= 0:
            continue
        sym = config.normalize_symbol(str(sell.get("ticker") or ""))
        if not sym:
            continue
        short_candidates.append(sell)
        signal_tiers[sym] = max(int(signal_tiers.get(sym, 0)), tier)
        role = str(sell.get("role") or "").lower()
        short_boosts[sym] = {
            "base": _tier_short_base(tier, bubble_score_100=bubble_100, regime=reg),
            "bubble_mult": 1.0,
            "score": int(sell.get("score") or 0),
            "role": role,
            "value": sell.get("value"),
            "tier": tier,
        }

    momentum_boosts, stat_arb_boosts, short_boosts, guard_notes = _apply_risk_guard(
        momentum_boosts=momentum_boosts,
        stat_arb_boosts=stat_arb_boosts,
        short_boosts=short_boosts,
        bubble_score_100=bubble_100,
        rhyme_b_panic=rhyme_b_panic,
        bear_regime=bear_regime,
        regime=reg,
    )

    if config.effective_historical_news_enabled():
        try:
            from modules.historical_news import headline_momentum_boosts

            hl_boosts = headline_momentum_boosts()
            for sym, boost in hl_boosts.items():
                momentum_boosts[sym] = round(
                    max(float(momentum_boosts.get(sym, 0.0)), float(boost)), 4
                )
                stat_arb_boosts[sym] = round(
                    max(float(stat_arb_boosts.get(sym, 1.0)), 1.0 + float(boost) * 0.5), 4
                )
            if hl_boosts:
                guard_notes.append("historical headline mention boosts")
        except Exception as exc:
            logger.debug("historical headline boosts unavailable: %s", exc)

    top_signals: list[dict[str, Any]] = []
    ranked_buys = sorted(
        buy_sources,
        key=lambda s: (-_classify_buy_tier(s), -int(s.get("score") or 0)),
    )
    for cluster in ranked_buys[:3]:
        row = dict(cluster)
        sym = config.normalize_symbol(str(cluster.get("ticker") or ""))
        row["boost_tier"] = signal_tiers.get(sym, _classify_buy_tier(cluster))
        top_signals.append(row)
    for sell in sorted(short_candidates, key=lambda s: -_classify_sell_tier(s))[:3]:
        if len(top_signals) >= 6:
            break
        row = dict(sell)
        sym = config.normalize_symbol(str(sell.get("ticker") or ""))
        row["boost_tier"] = signal_tiers.get(sym, _classify_sell_tier(sell))
        top_signals.append(row)

    parts: list[str] = []
    t1 = [s for s in top_signals if int(s.get("boost_tier") or 0) == 1]
    if t1:
        names = ", ".join(
            f"{s.get('ticker')} T{s.get('boost_tier')}" for s in t1[:3]
        )
        parts.append(f"tier-1 signals: {names}")
    if strong_clusters:
        names = ", ".join(
            f"{c.get('ticker')} ({c.get('insiders_count')} insiders)"
            for c in strong_clusters[:4]
        )
        parts.append(f"cluster buys: {names}")
    if short_candidates:
        names = ", ".join(
            f"{s.get('ticker')} ({str(s.get('role') or 'exec').upper()})"
            for s in short_candidates[:4]
        )
        parts.append(f"exec sells -> short watch: {names}")
    if guard_notes:
        parts.append("risk guard: " + "; ".join(guard_notes))

    state = {
        "enabled": True,
        "version": "1.5",
        "applied_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "strong_clusters": [
            config.normalize_symbol(str(c.get("ticker") or "")) for c in strong_clusters
        ],
        "short_candidates": [
            config.normalize_symbol(str(s.get("ticker") or "")) for s in short_candidates
        ],
        "momentum_boosts": momentum_boosts,
        "stat_arb_boosts": stat_arb_boosts,
        "short_boosts": short_boosts,
        "signal_tiers": signal_tiers,
        "top_signals": top_signals[:6],
        "summary": "; ".join(parts) if parts else "no actionable insider boosts this cycle",
        "cluster_count": len(strong_clusters),
        "short_count": len(short_candidates),
        "bubble_score_100": bubble_100,
        "regime": reg,
        "risk_guard_notes": guard_notes,
    }
    _save_state(state)
    _maybe_log_daily_impact(state)
    logger.info(
        "Insider boost v1.5: %d buy boosts, %d short candidates (bubble=%.0f)",
        len(momentum_boosts),
        len(short_candidates),
        bubble_100,
    )
    return state


def _maybe_log_daily_impact(state: dict[str, Any]) -> None:
    if not state.get("enabled"):
        return
    today = datetime.date.today().isoformat()
    last = str(state.get("last_impact_log_date") or "")
    if last == today:
        return
    _IMPACT_LOG.parent.mkdir(parents=True, exist_ok=True)
    mom = state.get("momentum_boosts") or {}
    sa = state.get("stat_arb_boosts") or {}
    shorts = state.get("short_boosts") or {}
    tiers = state.get("signal_tiers") or {}
    top_mom = sorted(mom.items(), key=lambda x: -x[1])[:3]
    top_sa = sorted(
        ((k, v) for k, v in sa.items() if float(v) > 1.0),
        key=lambda x: -x[1],
    )[:3]
    entry = {
        "date": today,
        "version": state.get("version", "1.5"),
        "signal_count": len(get_recent_insider_signals(days=7, min_score=60)),
        "strong_clusters": len(state.get("strong_clusters") or []),
        "short_candidates": len(state.get("short_candidates") or []),
        "tiers": {k: int(v) for k, v in list(tiers.items())[:8]},
        "top_momentum": {k: round(v, 4) for k, v in top_mom},
        "top_stat_arb": {k: round(v, 4) for k, v in top_sa},
        "top_shorts": {
            k: round(float((v or {}).get("base") or 0), 4) for k, v in list(shorts.items())[:3]
        },
        "risk_guard_notes": list(state.get("risk_guard_notes") or []),
        "bubble_score_100": state.get("bubble_score_100"),
        "summary": str(state.get("summary") or "")[:300],
    }
    try:
        with _IMPACT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        state["last_impact_log_date"] = today
        _save_state(state)
    except OSError as exc:
        logger.debug("Insider impact log write failed: %s", exc)


def get_weekly_impact_summary(*, days: int = 7) -> list[str]:
    if not _IMPACT_LOG.is_file():
        return ["- No insider impact log yet."]
    try:
        lines = _IMPACT_LOG.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return ["- Insider impact log unreadable."]
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    entries: list[dict[str, Any]] = []
    for line in lines[-days * 2 :]:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        d = str(row.get("date") or "")
        try:
            if datetime.date.fromisoformat(d) < cutoff:
                continue
        except ValueError:
            pass
        entries.append(row)
    if not entries:
        return ["- No insider impact entries this week."]
    out = ["**Insider Boost v1.5 impact (daily log):**"]
    for row in entries[-5:]:
        guard = row.get("risk_guard_notes") or []
        guard_s = f" | guard: {', '.join(guard)}" if guard else ""
        tiers = row.get("tiers") or {}
        tier_s = f" | tiers: {tiers}" if tiers else ""
        out.append(
            f"- {row.get('date')}: {row.get('signal_count', 0)} signals, "
            f"{row.get('strong_clusters', 0)} clusters, "
            f"{row.get('short_candidates', 0)} short watch{tier_s}{guard_s}"
        )
    return out


def get_thinking_context() -> dict[str, Any]:
    """Context block for Kimi / thinking engine — top 3 tiered signals."""
    state = _load_state()
    if not state.get("enabled"):
        apply_insider_signals_to_strategies()
        state = _load_state()
    top = list(state.get("top_signals") or [])[:3]
    summary = str(state.get("summary") or "")
    guard = state.get("risk_guard_notes") or []
    if guard:
        summary = f"{summary}; guard: {', '.join(guard)}" if summary else f"guard: {', '.join(guard)}"

    tier_lines = []
    cluster_buys: list[dict[str, Any]] = []
    executive_sells: list[dict[str, Any]] = []
    for sig in top:
        tk = sig.get("ticker") or sig.get("company") or "?"
        tier = int(sig.get("boost_tier") or signal_tier(str(tk)) or 0)
        st = _sig_type(sig)
        tier_lines.append(f"{tk} {st} tier-{tier} s{sig.get('score', 0)}")
        row = {"ticker": tk, "score": sig.get("score"), "line": tier_lines[-1], "boost_tier": tier}
        if st in ("executive_sell",) or "sell" in st:
            executive_sells.append(row)
        else:
            cluster_buys.append(row)

    return {
        "insider_signals": top,
        "insider_high_score_signals": top,
        "insider_summary": summary,
        "insider_cluster_count": int(state.get("cluster_count") or 0),
        "insider_short_candidates": list(state.get("short_candidates") or []),
        "insider_tier_lines": tier_lines,
        "insider_cluster_lines": [r["line"] for r in cluster_buys[:3]],
        "insider_sell_lines": [r["line"] for r in executive_sells[:3]],
        "insider_cluster_buys": cluster_buys,
        "insider_executive_sells": executive_sells,
        "insider_high_score_buys": cluster_buys,
        "insider_high_score_sells": executive_sells,
        "insider_boost_enabled": True,
        "insider_boost_version": "1.5",
        "insider_bubble_score_100": state.get("bubble_score_100"),
        "insider_signal_tiers": dict(state.get("signal_tiers") or {}),
    }


def signal_tier(symbol: str) -> int:
    state = _load_state()
    sym = config.normalize_symbol(symbol)
    return int((state.get("signal_tiers") or {}).get(sym, 0))


def momentum_rank_boost(symbol: str, executor=None) -> float:
    state = _load_state()
    if not state.get("enabled"):
        return 0.0
    sym = config.normalize_symbol(symbol)
    boost = float((state.get("momentum_boosts") or {}).get(sym, 0.0))
    if boost <= 0:
        return 0.0
    held = _insider_boosted_holdings(executor)
    if executor is not None and sym not in held:
        if len(held) >= int(config.INSIDER_MAX_BOOSTED_POSITIONS):
            return 0.0
    return boost


def stat_arb_long_boost(symbol: str) -> float:
    state = _load_state()
    if not state.get("enabled"):
        return 1.0
    sym = config.normalize_symbol(symbol)
    return float((state.get("stat_arb_boosts") or {}).get(sym, 1.0))


def short_candidate_boost(symbol: str, bubble_score: float) -> float:
    state = _load_state()
    if not state.get("enabled"):
        return 0.0
    sym = config.normalize_symbol(symbol)
    meta = (state.get("short_boosts") or {}).get(sym)
    if not meta:
        return 0.0
    base = float(meta.get("base") or 0.0)
    bubble_100 = float(bubble_score) * 100.0 if bubble_score <= 1.0 else float(bubble_score)
    regime = str(state.get("regime") or "")
    if bubble_100 >= config.INSIDER_BUBBLE_SHORT_AMPLIFY_SCORE or "RHYME_E" in regime:
        base = max(base, float(config.INSIDER_SHORT_AMPLIFIED_BOOST))
    elif bubble_100 < config.SHORT_BUBBLE_SCORE_MIN * 85:
        base *= 0.55
    cap = float(config.INSIDER_SELL_SHORT_BOOST_MAX)
    return min(base, cap)


def cap_insider_boost_notional(
    symbol: str,
    notional: float | None,
    equity: float,
    executor=None,
) -> float | None:
    """Cap insider-boosted NYSE entries to INSIDER_SINGLE_NAME_CAP_PCT of equity."""
    if notional is None or equity <= 0:
        return notional
    sym = config.normalize_symbol(symbol)
    if momentum_rank_boost(sym, executor) <= 0:
        return notional
    cap_val = round(equity * config.INSIDER_SINGLE_NAME_CAP_PCT, 2)
    current = 0.0
    if executor is not None:
        try:
            for pos in executor._get_positions():
                if config.normalize_symbol(str(pos.symbol)) != sym:
                    continue
                current = abs(float(getattr(pos, "market_value", 0) or 0))
                break
        except Exception:
            current = 0.0
    room = max(0.0, cap_val - current)
    capped = min(float(notional), room)
    min_n = config.effective_min_notional(equity)
    if capped < min_n:
        return None
    return round(capped, 2)


def get_short_candidate_tickers() -> list[str]:
    state = _load_state()
    if not state.get("enabled"):
        return []
    return list(state.get("short_candidates") or [])


def get_boost_snapshot() -> dict[str, Any]:
    return _load_state()


def format_insider_boost_startup_banner() -> str | None:
    """Startup line for Insider Boost v1.5 active multipliers."""
    if not config.effective_insider_signal_boost_enabled():
        return None
    state = _load_state()
    if not state.get("enabled"):
        try:
            apply_insider_signals_to_strategies()
            state = _load_state()
        except Exception as exc:
            logger.debug("insider boost startup refresh failed: %s", exc)
            return "Insider Boost v1.5: ON (awaiting first cycle)"
    tiers = state.get("signal_tiers") or {}
    t1 = sum(1 for t in tiers.values() if int(t) == 1)
    t2 = sum(1 for t in tiers.values() if int(t) == 2)
    return (
        f"Insider Boost v1.5: ON | T1={t1} T2={t2} | "
        f"momentum +{config.INSIDER_TIER1_MOMENTUM_BOOST}/+{config.INSIDER_TIER2_MOMENTUM_BOOST} | "
        f"stat-arb x{config.INSIDER_TIER1_STAT_ARB_MULT}/x{config.INSIDER_TIER2_STAT_ARB_MULT} | "
        f"short +{config.INSIDER_TIER1_SHORT_BOOST}->{config.INSIDER_SHORT_AMPLIFIED_BOOST} "
        f"(bubble>{config.INSIDER_BUBBLE_SHORT_AMPLIFY_SCORE:.0f}/RHYME_E) | "
        f"max {config.INSIDER_MAX_BOOSTED_POSITIONS} names @ "
        f"{config.INSIDER_SINGLE_NAME_CAP_PCT:.0%} cap"
    )


def format_telegram_weekly_insider_block() -> str:
    if not config.effective_insider_monitor_enabled():
        return ""
    lines = format_telegram_top_signals(limit=3)
    if not lines or lines == ["Insider: none this week"]:
        return "\n\nInsider Boost v1.5: no high-quality signals this week."
    snap = get_boost_snapshot()
    block = "\n\n" + "\n".join(lines)
    tiers = snap.get("signal_tiers") or {}
    if tiers:
        tier_txt = ", ".join(f"{k} T{v}" for k, v in list(tiers.items())[:5])
        block += f"\nAttribution tiers: {tier_txt}"
    clusters = snap.get("strong_clusters") or []
    shorts = snap.get("short_candidates") or []
    notes: list[str] = []
    if clusters:
        notes.append(f"Clusters: {', '.join(clusters[:4])}")
    if shorts:
        notes.append(f"Exec sells watch: {', '.join(shorts[:4])}")
    summary = str(snap.get("summary") or "")
    if summary and summary != "insider signal boost off":
        notes.append(summary[:180])
    if notes:
        block += "\n" + "\n".join(notes)
    return block


def format_telegram_top_signals(*, limit: int = 3) -> list[str]:
    state = _load_state()
    if not state.get("enabled"):
        apply_insider_signals_to_strategies()
        state = _load_state()
    signals = list(state.get("top_signals") or get_recent_insider_signals(days=7, min_score=55))[:limit]
    if not signals:
        return ["Insider: none this week"]
    lines = ["Insider Boost v1.5 (top signals):"]
    for sig in signals:
        tk = sig.get("ticker") or sig.get("company") or "?"
        val_s = _format_value(sig.get("value"))
        val_part = f" {val_s}" if val_s else ""
        st = _sig_type(sig)
        tier = int(sig.get("boost_tier") or 0)
        tier_part = f" T{tier}" if tier else ""
        lines.append(f"  {st}: {tk}{tier_part} (s{sig.get('score', 0)}{val_part})")
    return lines
