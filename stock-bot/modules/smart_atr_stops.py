"""Smart ATR stop-loss with conviction reevaluation (paper / Realistic Research).

Default protective stop at ``ATR_STOP_MULTIPLIER`` × ATR. At the first
reeval threshold (default −5% unrealized):
  • High conviction (RVOL > 2.5 OR catalyst > 70 OR insider cluster)
    → tighten stop to ``ATR_TIGHTEN_MULTIPLIER`` × ATR
  • Low conviction → cut size 50%, keep stop on the remainder
At the hard threshold (default −10%): full exit, no exceptions.

Live stays off unless ``SMART_STOPS_LIVE_ENABLED``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import config

logger = logging.getLogger(__name__)

_STATS: dict[str, int] = {
    "atr_exits": 0,
    "tighten": 0,
    "size_reduce": 0,
    "hard_exit": 0,
    "reevals": 0,
}


def reset_smart_stop_stats() -> None:
    for key in _STATS:
        _STATS[key] = 0


def smart_stop_stats() -> dict[str, int]:
    return dict(_STATS)


def _bump(key: str, n: int = 1) -> None:
    _STATS[key] = int(_STATS.get(key, 0)) + n


def parse_reeval_pcts(raw: str | None = None) -> list[float]:
    """Parse env list like ``[-5,-10]`` or ``-5,-10`` into fractions (−0.05, −0.10)."""
    text = (raw if raw is not None else os_getenv_reeval()).strip()
    if not text:
        return [-0.05, -0.10]
    nums = re.findall(r"-?\d+(?:\.\d+)?", text)
    out: list[float] = []
    for n in nums:
        v = float(n)
        # Percent points (−5) vs already-fraction (−0.05)
        if abs(v) > 1.0:
            v = v / 100.0
        out.append(v)
    if not out:
        return [-0.05, -0.10]
    out = sorted(out)  # most negative first: −0.10, −0.05
    return out


def os_getenv_reeval() -> str:
    import os

    return os.getenv("STOP_LOSS_REEVAL_PCTS", "[-5,-10]")


def reeval_thresholds() -> tuple[float, float]:
    """Return (soft_reeval_pct, hard_exit_pct) as negative fractions."""
    pcts = list(getattr(config, "STOP_LOSS_REEVAL_PCTS", None) or parse_reeval_pcts())
    if len(pcts) == 1:
        return float(pcts[0]), float(pcts[0])
    soft = max(pcts)  # −0.05 is greater than −0.10
    hard = min(pcts)
    return float(soft), float(hard)


def atr_stop_multiplier() -> float:
    return float(
        getattr(
            config,
            "ATR_STOP_MULTIPLIER",
            getattr(config, "ATR_RISK_MULTIPLE", 2.0),
        )
    )


def atr_tighten_multiplier() -> float:
    return float(getattr(config, "ATR_TIGHTEN_MULTIPLIER", 1.0))


def compute_stop_price(
    entry: float,
    atr: float,
    *,
    multiplier: float | None = None,
    side: str = "long",
) -> float:
    entry = max(0.01, float(entry))
    atr_v = max(0.01, float(atr))
    mult = float(multiplier if multiplier is not None else atr_stop_multiplier())
    dist = atr_v * mult
    if str(side).lower() == "short":
        return round(entry + dist, 2)
    return round(max(0.01, entry - dist), 2)


def ensure_initial_stop(
    meta: dict[str, Any],
    *,
    entry: float,
    atr: float,
    side: str = "long",
) -> dict[str, Any]:
    """Stamp default ATR stop on position meta if missing."""
    row = dict(meta or {})
    if row.get("smart_stop_price") and row.get("atr_stop_mult"):
        return row
    mult = atr_stop_multiplier()
    row["atr_stop_mult"] = mult
    row["smart_stop_price"] = compute_stop_price(entry, atr, multiplier=mult, side=side)
    row.setdefault("smart_reeval_done", False)
    row.setdefault("smart_tightened", False)
    row.setdefault("smart_size_reduced", False)
    return row


def is_high_conviction(
    symbol: str,
    data=None,
    *,
    bar_index: int | None = None,
) -> tuple[bool, str]:
    """RVOL > 2.5 OR catalyst > 70 OR insider cluster."""
    sym = config.normalize_symbol(symbol)
    reasons: list[str] = []

    rvol_thresh = float(getattr(config, "RVOL_MOMENTUM_BOOST_THRESHOLD", 2.5))
    rvol = _resolve_rvol(sym, data, bar_index=bar_index)
    if rvol is not None and rvol > rvol_thresh:
        reasons.append(f"rvol={rvol:.1f}x")

    cat_thresh = float(getattr(config, "SMART_STOP_CATALYST_MIN", 70.0))
    try:
        from modules.catalyst_scoring import score_catalysts

        score = float((score_catalysts(data, sym) or {}).get("score") or 0)
        if score > cat_thresh:
            reasons.append(f"catalyst={score:.0f}")
    except Exception as exc:
        logger.debug("smart-stop catalyst check skipped for %s: %s", sym, exc)

    try:
        from modules.insider_monitor import momentum_rank_boost

        if momentum_rank_boost(sym) > 0:
            reasons.append("insider_cluster")
    except Exception as exc:
        logger.debug("smart-stop insider check skipped for %s: %s", sym, exc)

    if reasons:
        return True, "+".join(reasons)
    return False, "low_conviction"


def _resolve_rvol(
    symbol: str,
    data,
    *,
    bar_index: int | None = None,
) -> float | None:
    if data is not None and hasattr(data, "columns") and bar_index is not None:
        try:
            sym = config.normalize_symbol(symbol)
            if sym in data.columns:
                closes = data[sym].dropna()
                # Map bar_index onto close series length when window is sliced
                i = min(int(bar_index), len(closes) - 1)
                from modules.vol_breakout_sleeve import _daily_rvol_proxy

                proxy = _daily_rvol_proxy(closes, i)
                if proxy is not None:
                    return float(proxy)
        except Exception as exc:
            logger.debug("smart-stop rvol proxy failed for %s: %s", symbol, exc)
    try:
        from modules.volume_analysis import calculate_rvol

        rvol = calculate_rvol(data, symbol)
        if rvol is not None:
            return float(rvol)
    except Exception as exc:
        logger.debug("smart-stop rvol fetch failed for %s: %s", symbol, exc)
    return None


def evaluate_smart_stop(
    *,
    symbol: str,
    entry: float,
    current: float,
    atr: float,
    meta: dict[str, Any],
    qty: float,
    data=None,
    bar_index: int | None = None,
    side: str = "long",
) -> dict[str, Any]:
    """Decide exit / reduce / tighten for one long (or short) position.

    Returns dict with keys:
      action: None | "exit" | "reduce"
      reason: str
      exit_code: str
      meta: updated meta
      reduce_frac: float (when action=reduce)
      stop_price: float
    """
    soft, hard = reeval_thresholds()
    is_short = str(side).lower() == "short" or float(qty) < 0
    side_key = "short" if is_short else "long"
    entry = float(entry)
    current = float(current)
    atr = max(0.01, float(atr))
    if entry <= 0 or current <= 0:
        return {"action": None, "meta": meta, "stop_price": None}

    pnl_pct = (current - entry) / entry
    if is_short:
        pnl_pct = -pnl_pct

    row = ensure_initial_stop(meta, entry=entry, atr=atr, side=side_key)
    stop_price = float(row.get("smart_stop_price") or 0)
    mult = float(row.get("atr_stop_mult") or atr_stop_multiplier())

    # 1) Hard exit at −10% (or configured hard threshold)
    if pnl_pct <= hard:
        _bump("hard_exit")
        return {
            "action": "exit",
            "reason": f"smart_hard_exit {pnl_pct:.2%}",
            "exit_code": "smart_hard_exit",
            "meta": row,
            "stop_price": stop_price,
            "reduce_frac": 0.0,
        }

    # 2) Soft reeval at −5%
    if pnl_pct <= soft and not row.get("smart_reeval_done"):
        _bump("reevals")
        high, why = is_high_conviction(symbol, data, bar_index=bar_index)
        row["smart_reeval_done"] = True
        if high:
            tight = atr_tighten_multiplier()
            row["atr_stop_mult"] = tight
            row["smart_tightened"] = True
            row["smart_stop_price"] = compute_stop_price(
                entry, atr, multiplier=tight, side=side_key
            )
            row["smart_reeval_reason"] = why
            _bump("tighten")
            stop_price = float(row["smart_stop_price"])
            # If already through the tightened stop, exit now
            hit = current <= stop_price if not is_short else current >= stop_price
            if hit:
                _bump("atr_exits")
                return {
                    "action": "exit",
                    "reason": f"smart_atr_tight {pnl_pct:.2%} ({why})",
                    "exit_code": "smart_atr_stop",
                    "meta": row,
                    "stop_price": stop_price,
                    "reduce_frac": 0.0,
                }
            return {
                "action": None,
                "reason": f"smart_tighten {why}",
                "exit_code": "",
                "meta": row,
                "stop_price": stop_price,
                "reduce_frac": 0.0,
            }
        # Low conviction → cut 50% once
        if not row.get("smart_size_reduced"):
            row["smart_size_reduced"] = True
            row["smart_reeval_reason"] = why
            _bump("size_reduce")
            return {
                "action": "reduce",
                "reason": f"smart_size_reduce {pnl_pct:.2%}",
                "exit_code": "smart_size_reduce",
                "meta": row,
                "stop_price": stop_price,
                "reduce_frac": 0.50,
            }
        row["smart_reeval_done"] = True

    # 3) ATR stop hit
    if stop_price > 0:
        hit = current <= stop_price if not is_short else current >= stop_price
        if hit:
            _bump("atr_exits")
            code = "smart_atr_stop"
            return {
                "action": "exit",
                "reason": f"{code} {pnl_pct:.2%} ({mult:.1f}x)",
                "exit_code": code,
                "meta": row,
                "stop_price": stop_price,
                "reduce_frac": 0.0,
            }

    return {
        "action": None,
        "reason": "",
        "exit_code": "",
        "meta": row,
        "stop_price": stop_price,
        "reduce_frac": 0.0,
    }


def format_smart_stops_banner() -> str | None:
    if not config.effective_smart_stops_enabled():
        return ">>> Smart ATR Stops: OFF"
    soft, hard = reeval_thresholds()
    return (
        f">>> Smart ATR Stops: ON "
        f"({atr_stop_multiplier():.1f}x → tighten {atr_tighten_multiplier():.1f}x "
        f"@ {soft:.0%} / hard {hard:.0%}) <<<"
    )


def dashboard_stop_label(
    entry: float,
    current: float,
    atr: float | None,
    *,
    side: str = "long",
    multiplier: float | None = None,
) -> tuple[str, str]:
    """Return (display_text, tag) for ATR Stop column color coding.

    Tags: atr_ok (green), atr_warn (amber), atr_danger (red), atr_none.
    """
    if atr is None or atr <= 0 or entry <= 0:
        return "—", "atr_none"
    mult = float(multiplier if multiplier is not None else atr_stop_multiplier())
    stop = compute_stop_price(entry, atr, multiplier=mult, side=side)
    label = f"${stop:,.2f} ({mult:.1f}x)"
    if current <= 0:
        return label, "atr_ok"
    is_short = str(side).lower() == "short"
    if is_short:
        dist = (stop - current) / current if current else 0.0
    else:
        dist = (current - stop) / current if current else 0.0
    # Dist to stop as fraction of price; small cushion → warn/danger
    if dist <= 0.005:
        return label, "atr_danger"
    if dist <= 0.02:
        return label, "atr_warn"
    return label, "atr_ok"
