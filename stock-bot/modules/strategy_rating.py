"""Strategy rating feedback loop — paper sizing tilts from recent performance.

Builds on ``modules.strategy_performance`` (journals / strategy_metrics.db).
When ``STRATEGY_RATING_ENABLED`` is on (paper only by default), maps rolling
scores → clipped size multipliers (default 0.8–1.2) applied modestly at
notional-sizing time.

Live stays off unless ``STRATEGY_RATING_LIVE_ENABLED=true``.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import config

logger = logging.getLogger(__name__)

# Sleeve / book labels → primary strategy_id for feedback sizing.
SLEEVE_STRATEGY_MAP: dict[str, str] = {
    "NYSE": "nyse_momentum_base",
    "SPY": "spy_trend",
    "STAT_ARB": "stat_arb",
    "STATARB": "stat_arb",
    "PAIR": "stat_arb",
    "SHORT": "protective_short",
    "PROTECTIVE_SHORT": "protective_short",
    "OPPORTUNISTIC_SHORT": "protective_short",
    "ORB": "orb_breakout",
    "RVOL": "rvol_momentum",
    "CATALYST": "catalyst_scoring",
    "CRYPTO": "nyse_momentum_base",
}

_cache: dict[str, Any] = {
    "ts": 0.0,
    "days": 0,
    "strategies": {},
    "stack_mult": 1.0,
}


def _clip_mult(value: float) -> float:
    lo = float(getattr(config, "STRATEGY_RATING_MULT_MIN", 0.8) or 0.8)
    hi = float(getattr(config, "STRATEGY_RATING_MULT_MAX", 1.2) or 1.2)
    if lo > hi:
        lo, hi = hi, lo
    return round(max(lo, min(hi, float(value))), 4)


def score_to_size_multiplier(score: float, *, trade_count: int = 0) -> float:
    """Map 0–100 risk-adjusted score → size mult.

    Score 50 → 1.0; Excellent (~85+) → near max; Weak → near min.
    Below ``STRATEGY_RATING_MIN_TRADES`` → neutral 1.0.
    """
    min_trades = int(getattr(config, "STRATEGY_RATING_MIN_TRADES", 5) or 5)
    if int(trade_count) < min_trades:
        return 1.0
    lo = float(getattr(config, "STRATEGY_RATING_MULT_MIN", 0.8) or 0.8)
    hi = float(getattr(config, "STRATEGY_RATING_MULT_MAX", 1.2) or 1.2)
    # Linear map: score 0→lo, 50→1.0, 100→hi
    s = max(0.0, min(100.0, float(score)))
    if s <= 50.0:
        mult = lo + (1.0 - lo) * (s / 50.0)
    else:
        mult = 1.0 + (hi - 1.0) * ((s - 50.0) / 50.0)
    return _clip_mult(mult)


def _lookback_days() -> int:
    return max(7, int(getattr(config, "STRATEGY_RATING_LOOKBACK_DAYS", 365) or 365))


def _cache_ttl_sec() -> float:
    return max(60.0, float(getattr(config, "STRATEGY_RATING_CACHE_SEC", 3600) or 3600))


def refresh_ratings(*, days: int | None = None, force: bool = False) -> dict[str, Any]:
    """Pull rolling metrics and cache per-strategy multipliers."""
    days = int(days) if days is not None else _lookback_days()
    now = time.time()
    if (
        not force
        and _cache["strategies"]
        and _cache["days"] == days
        and (now - float(_cache["ts"])) < _cache_ttl_sec()
    ):
        return snapshot()

    from modules.strategy_performance import (
        STRATEGY_IDS,
        STRATEGY_LABELS,
        _compute_metrics,
        _fetch_trades,
        sync_from_journal,
    )

    if config.PAPER_TRADING or config.paper_aggressive_context():
        try:
            sync_from_journal()
        except Exception as exc:
            logger.debug("strategy_rating journal sync skipped: %s", exc)

    # Prefer exact lookback; strategy_performance windows are 30/90/all —
    # fetch by cutoff days so 365d works.
    metrics = _compute_metrics(_fetch_trades(days))
    strategies: dict[str, dict[str, Any]] = {}
    active_mults: list[float] = []
    for sid in STRATEGY_IDS:
        row = metrics.get(sid) or {}
        score = float(row.get("risk_adjusted_score") or 0.0)
        trades = int(row.get("trade_count") or 0)
        mult = score_to_size_multiplier(score, trade_count=trades)
        strategies[sid] = {
            "strategy_id": sid,
            "label": STRATEGY_LABELS.get(sid, sid),
            "score": round(score, 1),
            "rating": row.get("rating") or "No data",
            "trade_count": trades,
            "return_pct": row.get("return_pct", 0.0),
            "sharpe": row.get("sharpe", 0.0),
            "win_rate_pct": row.get("win_rate_pct", 0.0),
            "pnl_contribution": row.get("pnl_contribution", 0.0),
            "size_mult": mult,
        }
        if trades >= int(getattr(config, "STRATEGY_RATING_MIN_TRADES", 5) or 5):
            active_mults.append(mult)

    # Stack mult = geometric-ish mean of active (modest: pull toward 1.0).
    if active_mults:
        geo = 1.0
        for m in active_mults:
            geo *= float(m)
        geo = geo ** (1.0 / len(active_mults))
        blend = float(getattr(config, "STRATEGY_RATING_STACK_BLEND", 0.5) or 0.5)
        blend = max(0.0, min(1.0, blend))
        stack = 1.0 + blend * (geo - 1.0)
        stack = _clip_mult(stack)
    else:
        stack = 1.0

    _cache["ts"] = now
    _cache["days"] = days
    _cache["strategies"] = strategies
    _cache["stack_mult"] = stack
    return snapshot()


def snapshot() -> dict[str, Any]:
    return {
        "as_of_ts": _cache["ts"],
        "lookback_days": _cache["days"] or _lookback_days(),
        "enabled": bool(config.effective_strategy_rating_enabled()),
        "stack_mult": float(_cache["stack_mult"] or 1.0),
        "strategies": dict(_cache["strategies"] or {}),
    }


def strategy_size_multiplier(strategy_id: str | None) -> float:
    """Per-strategy size mult (1.0 when disabled / unknown / thin data)."""
    if not config.effective_strategy_rating_enabled():
        return 1.0
    if not strategy_id:
        return float(snapshot().get("stack_mult") or 1.0) if _cache["strategies"] else 1.0
    if not _cache["strategies"]:
        try:
            refresh_ratings()
        except Exception as exc:
            logger.debug("strategy_rating refresh failed: %s", exc)
            return 1.0
    row = (_cache["strategies"] or {}).get(str(strategy_id))
    if not row:
        return 1.0
    return float(row.get("size_mult") or 1.0)


def sleeve_size_multiplier(sleeve: str | None) -> float:
    if not sleeve:
        return strategy_size_multiplier(None)
    key = str(sleeve).strip().upper()
    sid = SLEEVE_STRATEGY_MAP.get(key)
    if sid is None:
        # Soft match
        for k, v in SLEEVE_STRATEGY_MAP.items():
            if k in key:
                sid = v
                break
    return strategy_size_multiplier(sid)


def resolve_strategy_id(
    *,
    strategy_id: str | None = None,
    sleeve: str | None = None,
    tags: list[str] | None = None,
) -> str | None:
    if strategy_id:
        return str(strategy_id)
    tags_l = [t.lower() for t in (tags or [])]
    if "catalyst" in tags_l:
        return "catalyst_scoring"
    if "orb" in tags_l:
        return "orb_breakout"
    if "rvol" in tags_l:
        return "rvol_momentum"
    if "insider" in tags_l:
        return "insider_cluster"
    if sleeve:
        key = str(sleeve).strip().upper()
        return SLEEVE_STRATEGY_MAP.get(key)
    return None


def apply_strategy_rating_to_notional(
    notional: float | None,
    *,
    strategy_id: str | None = None,
    sleeve: str | None = None,
    tags: list[str] | None = None,
) -> float | None:
    """Multiply notional by clipped rating mult. No-op when disabled."""
    if notional is None:
        return None
    if not config.effective_strategy_rating_enabled():
        return notional
    sid = resolve_strategy_id(strategy_id=strategy_id, sleeve=sleeve, tags=tags)
    mult = strategy_size_multiplier(sid)
    if abs(mult - 1.0) < 1e-6:
        return round(float(notional), 2)
    return round(float(notional) * mult, 2)


def stack_risk_multiplier() -> float:
    """Modest portfolio-level tilt for ``effective_risk_per_trade`` (paper)."""
    if not config.effective_strategy_rating_enabled():
        return 1.0
    if not getattr(config, "STRATEGY_RATING_APPLY_STACK_RISK", True):
        return 1.0
    if not _cache["strategies"]:
        try:
            refresh_ratings()
        except Exception:
            return 1.0
    return float(_cache.get("stack_mult") or 1.0)


def format_strategy_rating_banner() -> str | None:
    if not config.effective_strategy_rating_enabled():
        return None
    try:
        snap = refresh_ratings()
    except Exception as exc:
        logger.debug("strategy rating banner unavailable: %s", exc)
        return ">>> Strategy Rating Feedback: ON (warming up) <<<"
    strategies = snap.get("strategies") or {}
    ranked = sorted(
        [v for v in strategies.values() if int(v.get("trade_count") or 0) > 0],
        key=lambda x: float(x.get("score") or 0),
        reverse=True,
    )
    days = snap.get("lookback_days") or _lookback_days()
    stack = float(snap.get("stack_mult") or 1.0)
    if not ranked:
        return (
            f">>> Strategy Rating Feedback: ON | {days}d lookback | "
            f"stack x{stack:.2f} | collecting closed trades <<<"
        )
    parts = []
    for row in ranked[:4]:
        label = str(row.get("label") or row.get("strategy_id") or "?")
        short = label.split()[0]
        if "RVOL" in label:
            short = "RVOL"
        elif "ORB" in label:
            short = "ORB"
        elif "Stat" in label:
            short = "StatArb"
        elif "NYSE" in label:
            short = "NYSE"
        elif "Protective" in label:
            short = "Shorts"
        parts.append(f"{short} x{float(row.get('size_mult') or 1):.2f}")
    return (
        f">>> Strategy Rating Feedback: ON | {days}d | stack x{stack:.2f} | "
        f"{', '.join(parts)} <<<"
    )


def rankings_table(*, days: int | None = None) -> list[dict[str, Any]]:
    snap = refresh_ratings(days=days, force=True)
    rows = list((snap.get("strategies") or {}).values())
    rows.sort(key=lambda r: float(r.get("score") or 0), reverse=True)
    return rows
