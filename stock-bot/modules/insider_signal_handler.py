"""Apply insider monitor signals to paper trading strategies (Realistic Research v1.4)."""

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
_STRONG_CLUSTER_MIN_SCORE = 80
_STRONG_CLUSTER_MIN_INSIDERS = 4
_SHORT_SELL_MIN_SCORE = 75
_CACHE: dict[str, Any] = {}


def _load_state() -> dict[str, Any]:
    if _CACHE.get("state") is not None:
        return dict(_CACHE["state"])
    state = read_json_file(_STATE_FILE) or {}
    _CACHE["state"] = state
    return dict(state)


def _save_state(state: dict[str, Any]) -> None:
    write_json_file(_STATE_FILE, state)
    _CACHE["state"] = dict(state)


def _cluster_momentum_boost(cluster: dict[str, Any]) -> float:
    """Momentum rank boost: +0.10 to INSIDER_CLUSTER_BOOST_MAX (stronger for >=6 insiders)."""
    sym = config.normalize_symbol(str(cluster.get("ticker") or ""))
    insiders = int(cluster.get("insiders_count") or 0)
    cap = float(config.INSIDER_CLUSTER_BOOST_MAX)
    floor = 0.10
    strong = _ticker_in_strong_sector(sym) if sym else False
    if insiders >= 6:
        boost = cap if strong else max(floor, cap - 0.03)
    elif insiders >= 4:
        boost = min(cap, 0.15 if strong else 0.12)
    else:
        boost = 0.11 if strong else floor
    return round(min(boost, cap), 4)


def _cluster_stat_arb_mult(cluster: dict[str, Any]) -> float:
    """Stat arb long multiplier: 1.08 - 1.15."""
    sym = config.normalize_symbol(str(cluster.get("ticker") or ""))
    insiders = int(cluster.get("insiders_count") or 0)
    strong = _ticker_in_strong_sector(sym) if sym else False
    if insiders >= 6:
        return 1.15 if strong else 1.12
    if insiders >= 4:
        return 1.12 if strong else 1.10
    return 1.08


def _exec_short_base(sell: dict[str, Any], *, bubble_score_norm: float) -> float:
    """Executive sell short boost: 0.22 - INSIDER_SELL_SHORT_BOOST_MAX."""
    role = str(sell.get("role") or "").lower()
    cap = float(config.INSIDER_SELL_SHORT_BOOST_MAX)
    if role == "ceo":
        base = 0.26
    elif role == "cfo":
        base = 0.24
    else:
        base = 0.22
    if bubble_score_norm > 0.70 and role in ("ceo", "cfo"):
        base = min(cap, base + 0.06)
    elif bubble_score_norm > 0.70:
        base = min(cap, base + 0.04)
    return round(min(max(base, 0.22), cap), 4)


def _resolve_market_context(
    *,
    bubble_score_100: float | None,
    regime: str | None,
) -> tuple[float, str, bool]:
    score_100 = float(bubble_score_100 or 0.0)
    reg = str(regime or "")
    if bubble_score_100 is None:
        try:
            from modules.bubble_risk import compute_bubble_risk_from_live_context

            ctx = compute_bubble_risk_from_live_context(regime=reg) or {}
            score_100 = float(ctx.get("score_100") or 0.0)
            if not reg:
                reg = str(ctx.get("regime") or "")
        except Exception:
            pass
    rhyme_b_panic = "RHYME_B" in reg and "Panic" in reg
    return score_100, reg, rhyme_b_panic


def _apply_risk_guard(
    *,
    momentum_boosts: dict[str, float],
    stat_arb_boosts: dict[str, float],
    short_boosts: dict[str, dict[str, Any]],
    bubble_score_100: float,
    rhyme_b_panic: bool,
) -> tuple[dict[str, float], dict[str, float], dict[str, dict[str, Any]], list[str]]:
    notes: list[str] = []
    if not config.effective_insider_risk_guard_enabled():
        return momentum_boosts, stat_arb_boosts, short_boosts, notes

    suppress = float(config.INSIDER_RISK_BUBBLE_SUPPRESS)
    if bubble_score_100 > suppress:
        if momentum_boosts:
            momentum_boosts = {k: 0.0 for k in momentum_boosts}
            notes.append(f"bullish boosts suppressed (bubble {bubble_score_100:.0f}>{suppress:.0f})")
        if stat_arb_boosts:
            stat_arb_boosts = {
                k: 1.0 + max(0.0, v - 1.0) * 0.2 for k, v in stat_arb_boosts.items()
            }
            notes.append("stat arb insider mult damped 80%")

    if rhyme_b_panic and short_boosts:
        cap = float(config.INSIDER_SELL_SHORT_BOOST_MAX)
        for sym, meta in short_boosts.items():
            boosted = float(meta.get("base") or 0.0) * 1.25
            meta["base"] = round(min(boosted, cap * 1.05), 4)
            meta["rhyme_b_amplified"] = True
            short_boosts[sym] = meta
        notes.append("short boosts amplified (RHYME_B panic)")

    return momentum_boosts, stat_arb_boosts, short_boosts, notes


def apply_insider_signals_to_strategies(
    *,
    bubble_score_100: float | None = None,
    regime: str | None = None,
) -> dict[str, Any]:
    """Evaluate insider signals and cache boosts for momentum, stat arb, and shorts."""
    if not config.effective_insider_signal_boost_enabled():
        empty = {
            "enabled": False,
            "applied_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "strong_clusters": [],
            "short_candidates": [],
            "momentum_boosts": {},
            "stat_arb_boosts": {},
            "short_boosts": {},
            "top_signals": [],
            "summary": "insider signal boost off",
            "risk_guard_notes": [],
        }
        _save_state(empty)
        return empty

    bubble_100, reg, rhyme_b_panic = _resolve_market_context(
        bubble_score_100=bubble_score_100,
        regime=regime,
    )
    bubble_norm = bubble_100 / 100.0

    clusters = get_cluster_buy_signals(min_insiders=3, days=7)
    exec_sells = get_executive_sell_signals(min_value=100_000, days=7)

    strong_clusters = [
        c
        for c in clusters
        if int(c.get("score") or 0) >= _STRONG_CLUSTER_MIN_SCORE
        and int(c.get("insiders_count") or 0) >= _STRONG_CLUSTER_MIN_INSIDERS
    ]
    short_candidates = [
        s
        for s in exec_sells
        if int(s.get("score") or 0) >= _SHORT_SELL_MIN_SCORE
        and str(s.get("role") or "").lower() in ("ceo", "cfo")
    ]

    momentum_boosts: dict[str, float] = {}
    stat_arb_boosts: dict[str, float] = {}
    for cluster in strong_clusters:
        sym = config.normalize_symbol(str(cluster.get("ticker") or ""))
        if not sym:
            continue
        momentum_boosts[sym] = _cluster_momentum_boost(cluster)
        stat_arb_boosts[sym] = _cluster_stat_arb_mult(cluster)

    for cluster in clusters:
        sym = config.normalize_symbol(str(cluster.get("ticker") or ""))
        if not sym or sym in momentum_boosts:
            continue
        if int(cluster.get("score") or 0) < _STRONG_CLUSTER_MIN_SCORE:
            continue
        momentum_boosts[sym] = _cluster_momentum_boost(cluster)
        stat_arb_boosts[sym] = _cluster_stat_arb_mult(cluster)

    short_boosts: dict[str, dict[str, Any]] = {}
    for sell in short_candidates:
        sym = config.normalize_symbol(str(sell.get("ticker") or ""))
        if not sym:
            continue
        role = str(sell.get("role") or "").lower()
        short_boosts[sym] = {
            "base": _exec_short_base(sell, bubble_score_norm=bubble_norm),
            "bubble_mult": 1.25,
            "score": int(sell.get("score") or 0),
            "role": role,
            "value": sell.get("value"),
        }

    momentum_boosts, stat_arb_boosts, short_boosts, guard_notes = _apply_risk_guard(
        momentum_boosts=momentum_boosts,
        stat_arb_boosts=stat_arb_boosts,
        short_boosts=short_boosts,
        bubble_score_100=bubble_100,
        rhyme_b_panic=rhyme_b_panic,
    )

    top_signals: list[dict[str, Any]] = []
    for cluster in strong_clusters[:3]:
        top_signals.append(cluster)
    for sell in short_candidates:
        if len(top_signals) >= 5:
            break
        if sell not in top_signals:
            top_signals.append(sell)
    if len(top_signals) < 3:
        for sig in get_recent_insider_signals(days=7, min_score=60):
            if len(top_signals) >= 3:
                break
            if sig in top_signals:
                continue
            top_signals.append(sig)

    parts: list[str] = []
    if strong_clusters:
        names = ", ".join(
            f"{c.get('ticker')} ({c.get('insiders_count')} insiders, s{c.get('score')})"
            for c in strong_clusters[:4]
        )
        parts.append(f"strong cluster buys: {names}")
    if short_candidates:
        names = ", ".join(
            f"{s.get('ticker')} ({str(s.get('role') or 'exec').upper()}, s{s.get('score')})"
            for s in short_candidates[:4]
        )
        parts.append(f"CEO/CFO sells -> short watch: {names}")
    if guard_notes:
        parts.append("risk guard: " + "; ".join(guard_notes))

    state = {
        "enabled": True,
        "applied_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "strong_clusters": [config.normalize_symbol(str(c.get("ticker") or "")) for c in strong_clusters],
        "short_candidates": [config.normalize_symbol(str(s.get("ticker") or "")) for s in short_candidates],
        "momentum_boosts": momentum_boosts,
        "stat_arb_boosts": stat_arb_boosts,
        "short_boosts": short_boosts,
        "top_signals": top_signals[:5],
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
        "Insider signal boost: %d strong clusters, %d short candidates (bubble=%.0f)",
        len(strong_clusters),
        len(short_candidates),
        bubble_100,
    )
    return state


def _maybe_log_daily_impact(state: dict[str, Any]) -> None:
    """Append one JSON line per day to logs/insider_impact.log."""
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
    top_mom = sorted(mom.items(), key=lambda x: -x[1])[:3]
    top_sa = sorted(
        ((k, v) for k, v in sa.items() if float(v) > 1.0),
        key=lambda x: -x[1],
    )[:3]
    entry = {
        "date": today,
        "signal_count": len(get_recent_insider_signals(days=7, min_score=60)),
        "strong_clusters": len(state.get("strong_clusters") or []),
        "short_candidates": len(state.get("short_candidates") or []),
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
    """Markdown bullets from insider_impact.log for weekly report."""
    if not _IMPACT_LOG.is_file():
        return ["- No insider impact log yet."]
    try:
        lines = _IMPACT_LOG.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return ["- Insider impact log unreadable."]
    import json as _json

    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    entries: list[dict[str, Any]] = []
    for line in lines[-days * 2 :]:
        line = line.strip()
        if not line:
            continue
        try:
            row = _json.loads(line)
        except _json.JSONDecodeError:
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
    out = ["**Insider impact (daily log):**"]
    for row in entries[-5:]:
        guard = row.get("risk_guard_notes") or []
        guard_s = f" | guard: {', '.join(guard)}" if guard else ""
        out.append(
            f"- {row.get('date')}: {row.get('signal_count', 0)} signals, "
            f"{row.get('strong_clusters', 0)} clusters, "
            f"{row.get('short_candidates', 0)} short watch{guard_s}"
        )
    return out


def get_thinking_context() -> dict[str, Any]:
    """Context block for Kimi / thinking engine prompts."""
    state = _load_state()
    if not state.get("enabled"):
        apply_insider_signals_to_strategies()
        state = _load_state()
    top = list(state.get("top_signals") or [])[:8]
    summary = str(state.get("summary") or "")
    guard = state.get("risk_guard_notes") or []
    if guard:
        summary = f"{summary}; guard: {', '.join(guard)}" if summary else f"guard: {', '.join(guard)}"
    return {
        "insider_signals": top,
        "insider_summary": summary,
        "insider_cluster_count": int(state.get("cluster_count") or 0),
        "insider_short_candidates": list(state.get("short_candidates") or []),
        "insider_boost_enabled": True,
        "insider_bubble_score_100": state.get("bubble_score_100"),
    }


def momentum_rank_boost(symbol: str) -> float:
    state = _load_state()
    if not state.get("enabled"):
        return 0.0
    sym = config.normalize_symbol(symbol)
    return float((state.get("momentum_boosts") or {}).get(sym, 0.0))


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
    if float(bubble_score) >= config.SHORT_BUBBLE_SCORE_MIN:
        base *= float(meta.get("bubble_mult") or 1.0)
    elif float(bubble_score) < config.SHORT_BUBBLE_SCORE_MIN * 0.85:
        base *= 0.55
    cap = float(config.INSIDER_SELL_SHORT_BOOST_MAX) * 1.05
    return min(base, cap)


def get_short_candidate_tickers() -> list[str]:
    state = _load_state()
    if not state.get("enabled"):
        return []
    return list(state.get("short_candidates") or [])


def get_boost_snapshot() -> dict[str, Any]:
    """Last applied insider boost state (may be empty until first cycle)."""
    return _load_state()


def format_telegram_weekly_insider_block() -> str:
    """Top 3 insider signals + boost note for Friday weekly Telegram."""
    if not config.effective_insider_monitor_enabled():
        return ""
    lines = format_telegram_top_signals(limit=3)
    if not lines or lines == ["Insider: none this week"]:
        return "\n\nInsider: no high-quality signals this week."
    snap = get_boost_snapshot()
    block = "\n\n" + "\n".join(lines)
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
    lines = ["Insider signals (top):"]
    for sig in signals:
        tk = sig.get("ticker") or sig.get("company") or "?"
        val_s = _format_value(sig.get("value"))
        val_part = f" {val_s}" if val_s else ""
        st = _sig_type(sig)
        lines.append(f"  {st}: {tk} (s{sig.get('score', 0)}{val_part})")
    return lines
