"""Dynamic core allocator — pick VTI / SPY / blend / cash as passive core."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).resolve().parent.parent / "core_allocator_state.json"

CORE_CHOICES = ("vti", "spy", "blend", "cash")
CORE_VTI_PCT: dict[str, float] = {
    "vti": 0.85,
    "spy": 0.40,
    "blend": 0.55,
    "cash": 0.0,
}

# Locked final (2026-06): SPY @ 40% — 90d Sharpe VTI 0.56 vs SPY 0.60.
_LOCKED_SPY_METRICS: dict[str, dict[str, float]] = {
    "vti": {"sharpe": 0.562, "calmar": 0.825, "max_dd": -0.0875, "ann_return": 0.0722},
    "spy": {"sharpe": 0.598, "calmar": 0.846, "max_dd": -0.0887, "ann_return": 0.0751},
    "blend": {"sharpe": 0.58, "calmar": 0.838, "max_dd": -0.0879, "ann_return": 0.0736},
    "cash": {"sharpe": 0.0, "calmar": 0.0, "max_dd": 0.0, "ann_return": 0.0},
}
_LOCKED_SPY_SCORES = {"vti": 0.2811, "spy": 0.2972, "blend": 0.2894, "cash": 0.0}

_state: dict[str, Any] = {
    "choice": "spy",
    "vti_pct": CORE_VTI_PCT["spy"],
    "last_review_bar": -10_000,
    "last_review_date": None,
    "metrics": dict(_LOCKED_SPY_METRICS),
    "scores": dict(_LOCKED_SPY_SCORES),
}


def effective_dynamic_core_enabled() -> bool:
    if not config.DYNAMIC_CORE_ENABLED:
        return False
    if config.paper_aggressive_context() or config.backtest_paper_sleeves_context():
        return True
    if config.DYNAMIC_CORE_LIVE_ENABLED and not config.PAPER_TRADING:
        return True
    return False


def _load_state() -> None:
    global _state
    if not STATE_FILE.is_file():
        return
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        _state.update(raw)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("core allocator state read failed: %s", exc)


def _save_state() -> None:
    try:
        STATE_FILE.write_text(json.dumps(_state, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("core allocator state write failed: %s", exc)


def reset_core_allocator_state() -> None:
    global _state
    _state = {
        "choice": "spy",
        "vti_pct": CORE_VTI_PCT["spy"],
        "last_review_bar": -10_000,
        "last_review_date": None,
        "metrics": dict(_LOCKED_SPY_METRICS),
        "scores": dict(_LOCKED_SPY_SCORES),
    }


def lock_core_allocator(choice: str | None = None) -> dict[str, Any]:
    """Apply locked passive-core choice (paper research final)."""
    pick = (choice or config.CORE_ALLOCATOR_LOCKED_CHOICE or "spy").strip().lower()
    if pick not in CORE_CHOICES:
        pick = "spy"
    result = {
        "choice": pick,
        "vti_pct": CORE_VTI_PCT[pick],
        "metrics": dict(_LOCKED_SPY_METRICS),
        "scores": dict(_LOCKED_SPY_SCORES),
    }
    apply_core_choice(result)
    return result


def _daily_returns(prices: pd.Series) -> pd.Series:
    s = prices.dropna().astype(float)
    if len(s) < 2:
        return pd.Series(dtype=float)
    return s.pct_change().dropna()


def _path_metrics(daily_returns: pd.Series) -> dict[str, float]:
    r = daily_returns.dropna()
    if len(r) < 10:
        return {"sharpe": 0.0, "calmar": 0.0, "max_dd": 0.0, "ann_return": 0.0}
    equity = (1.0 + r).cumprod()
    peak = equity.cummax()
    dd = float((equity / peak - 1.0).min())
    ann = float((equity.iloc[-1]) ** (252.0 / len(r)) - 1.0)
    vol = float(r.std() * np.sqrt(252))
    sharpe = ann / vol if vol > 1e-9 else 0.0
    calmar = ann / abs(dd) if dd < -1e-9 else ann
    return {
        "sharpe": round(sharpe, 3),
        "calmar": round(calmar, 3),
        "max_dd": round(dd, 4),
        "ann_return": round(ann, 4),
    }


def _composite_score(metrics: dict[str, float]) -> float:
    sharpe = float(metrics.get("sharpe", 0.0))
    calmar = float(metrics.get("calmar", 0.0))
    dd = abs(float(metrics.get("max_dd", 0.0)))
    calmar_c = min(max(calmar, -2.0), 3.0) / 3.0
    return round(sharpe * 0.40 + calmar_c * 0.30 - dd * 0.30, 4)


def _option_returns(data: pd.DataFrame, choice: str) -> pd.Series:
    vti_sym = config.VTI_CORE_SYMBOL
    spy_sym = config.SPY_BOT_SYMBOL
    if choice == "cash":
        idx = data.index
        if len(idx) < 2:
            return pd.Series(dtype=float)
        return pd.Series(0.0, index=idx[1:])
    if choice == "vti" and vti_sym in data.columns:
        return _daily_returns(data[vti_sym])
    if choice == "spy" and spy_sym in data.columns:
        return _daily_returns(data[spy_sym])
    if choice == "blend":
        parts = []
        if vti_sym in data.columns:
            parts.append(_daily_returns(data[vti_sym]))
        if spy_sym in data.columns:
            parts.append(_daily_returns(data[spy_sym]))
        if not parts:
            return pd.Series(dtype=float)
        aligned = pd.concat(parts, axis=1).dropna()
        if aligned.empty:
            return pd.Series(dtype=float)
        return aligned.mean(axis=1)
    return pd.Series(dtype=float)


def _dynamic_vti_spy_pct(vti_sharpe: float, spy_sharpe: float) -> float:
    """Map recent Sharpe edge to VTI core % within DYNAMIC_CORE_MIN/MAX_PCT."""
    lo = float(config.DYNAMIC_CORE_MIN_PCT)
    hi = float(config.DYNAMIC_CORE_MAX_PCT)
    mid = (lo + hi) / 2.0
    edge = float(vti_sharpe) - float(spy_sharpe)
    span = max(abs(vti_sharpe), abs(spy_sharpe), 0.05)
    tilt = max(-1.0, min(1.0, edge / span))
    half = (hi - lo) / 2.0
    return round(max(lo, min(hi, mid + half * tilt)), 4)


def compare_core_options(
    data: pd.DataFrame,
    *,
    end_idx: int | None = None,
    lookback: int | None = None,
) -> dict[str, Any]:
    """Score VTI / SPY / blend / cash over recent lookback window."""
    lookback = lookback or config.DYNAMIC_CORE_LOOKBACK_DAYS
    end_idx = len(data) - 1 if end_idx is None else int(end_idx)
    start = max(0, end_idx - lookback)
    window = data.iloc[start : end_idx + 1]
    if effective_dynamic_core_enabled():
        vti_rets = _option_returns(window, "vti")
        spy_rets = _option_returns(window, "spy")
        vti_m = _path_metrics(vti_rets)
        spy_m = _path_metrics(spy_rets)
        pct = _dynamic_vti_spy_pct(vti_m["sharpe"], spy_m["sharpe"])
        choice = "vti" if pct >= 0.40 else "spy"
        return {
            "choice": choice,
            "vti_pct": pct,
            "scores": {"vti": vti_m["sharpe"], "spy": spy_m["sharpe"]},
            "metrics": {"vti": vti_m, "spy": spy_m},
            "dynamic_range": [config.DYNAMIC_CORE_MIN_PCT, config.DYNAMIC_CORE_MAX_PCT],
        }
    scores: dict[str, float] = {}
    metrics: dict[str, dict[str, float]] = {}
    for choice in CORE_CHOICES:
        rets = _option_returns(window, choice)
        m = _path_metrics(rets)
        metrics[choice] = m
        scores[choice] = _composite_score(m)
    best = max(scores, key=scores.get)
    return {
        "choice": best,
        "vti_pct": CORE_VTI_PCT[best],
        "scores": scores,
        "metrics": metrics,
    }


def _resolve_core_price_data(
    trade_data: pd.DataFrame,
    *,
    end_idx: int | None,
    allocator_data: pd.DataFrame | None,
) -> tuple[pd.DataFrame, int | None]:
    """Pin allocator to baseline sim window when DEEP_HISTORY_INDICATORS_ONLY."""
    if not config.DEEP_HISTORY_INDICATORS_ONLY or allocator_data is None:
        return trade_data, end_idx
    price_data = allocator_data
    mapped = end_idx
    if end_idx is not None and 0 <= int(end_idx) < len(trade_data):
        ts = trade_data.index[int(end_idx)]
        loc = price_data.index.get_indexer([ts], method="pad")[0]
        mapped = max(0, int(loc)) if loc >= 0 else int(end_idx)
    return price_data, mapped


def choose_best_core(
    trade_data: pd.DataFrame,
    *,
    end_idx: int | None = None,
    lookback: int | None = None,
    allocator_data: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Pick best passive core using Sharpe/Calmar on the resolved price window."""
    price_data, mapped_idx = _resolve_core_price_data(
        trade_data, end_idx=end_idx, allocator_data=allocator_data
    )
    return compare_core_options(price_data, end_idx=mapped_idx, lookback=lookback)


def apply_core_choice(result: dict[str, Any], *, bar_index: int | None = None) -> None:
    global _state
    _state["choice"] = result["choice"]
    _state["vti_pct"] = float(result["vti_pct"])
    _state["scores"] = result.get("scores", {})
    _state["metrics"] = result.get("metrics", {})
    _state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if bar_index is not None:
        _state["last_review_bar"] = int(bar_index)
    else:
        _state["last_review_date"] = date.today().isoformat()
    _save_state()


def maybe_refresh_core_allocation(
    data: pd.DataFrame,
    *,
    bar_index: int | None = None,
    force: bool = False,
    allocator_data: pd.DataFrame | None = None,
) -> dict[str, Any] | None:
    """Re-run comparison every DYNAMIC_CORE_REVIEW_DAYS bars (backtest) or calendar days (live)."""
    if config.effective_core_allocator_locked():
        return None
    if not effective_dynamic_core_enabled():
        return None
    review = int(config.DYNAMIC_CORE_REVIEW_DAYS)
    if bar_index is not None:
        if not force and (bar_index - int(_state.get("last_review_bar", -10_000))) < review:
            return None
    else:
        last = _state.get("last_review_date")
        if not force and last == date.today().isoformat():
            return None
    price_data, mapped_bar = _resolve_core_price_data(
        data, end_idx=bar_index, allocator_data=allocator_data
    )
    if len(price_data) < max(20, config.DYNAMIC_CORE_LOOKBACK_DAYS // 2):
        return None
    result = choose_best_core(
        data,
        end_idx=bar_index,
        allocator_data=allocator_data,
    )
    apply_core_choice(result, bar_index=mapped_bar if bar_index is not None else bar_index)
    return result


def refresh_core_allocation_from_data(
    data: pd.DataFrame,
    *,
    end_idx: int | None = None,
    allocator_data: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Force refresh (startup / manual)."""
    if not effective_dynamic_core_enabled():
        return {}
    result = choose_best_core(
        data,
        end_idx=end_idx,
        allocator_data=allocator_data,
    )
    _, mapped_bar = _resolve_core_price_data(
        data, end_idx=end_idx, allocator_data=allocator_data
    )
    apply_core_choice(result, bar_index=mapped_bar if end_idx is not None else end_idx)
    return result


def effective_vti_core_pct(
    equity: float | None = None,
    *,
    vol_score: float | None = None,
    macro_stress: bool = False,
    volatility: str | None = None,
) -> float | None:
    """Dynamic or locked passive core % when allocator is active; else None (use legacy path)."""
    if config.effective_core_allocator_locked():
        choice = config.CORE_ALLOCATOR_LOCKED_CHOICE
        if choice not in CORE_CHOICES:
            choice = current_core_choice()
        return round(min(0.95, max(0.0, float(CORE_VTI_PCT.get(choice, 0.40)))), 6)
    if not effective_dynamic_core_enabled():
        return None
    return round(min(0.95, max(0.0, float(_state.get("vti_pct", CORE_VTI_PCT["vti"])))), 6)


def current_core_choice() -> str:
    return str(_state.get("choice", "vti"))


def core_allocator_snapshot() -> dict[str, Any]:
    return {
        "choice": current_core_choice(),
        "vti_pct": float(_state.get("vti_pct", CORE_VTI_PCT["vti"])),
        "metrics": dict(_state.get("metrics") or {}),
    }


def format_core_allocator_banner() -> str | None:
    locked = config.effective_core_allocator_locked()
    if not locked and not effective_dynamic_core_enabled():
        return None
    choice = current_core_choice().upper()
    pct = float(_state.get("vti_pct", CORE_VTI_PCT.get(current_core_choice(), 0.40)))
    metrics = _state.get("metrics") or _LOCKED_SPY_METRICS
    vti_m = metrics.get("vti", {})
    spy_m = metrics.get("spy", {})
    vti_sh = vti_m.get("sharpe", 0.0)
    spy_sh = spy_m.get("sharpe", 0.0)
    prefix = "LOCKED " if locked else ("DYNAMIC " if effective_dynamic_core_enabled() else "")
    range_note = ""
    if effective_dynamic_core_enabled():
        range_note = (
            f" [{config.DYNAMIC_CORE_MIN_PCT:.0%}-{config.DYNAMIC_CORE_MAX_PCT:.0%}]"
        )
    return (
        f">>> CORE ALLOCATOR {prefix}chose {choice} @ {pct:.0%}{range_note} "
        f"(Sharpe VTI {vti_sh:.2f} vs SPY {spy_sh:.2f})"
    )


_load_state()
