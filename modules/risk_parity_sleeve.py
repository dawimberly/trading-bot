"""Bridgewater-style risk parity / All Weather + Millennium pod drawdown (paper only)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import config
from modules.safe_io import read_json_file, write_json_file

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
POD_STATE_FILE = ROOT / config.POD_RISK_STATE_FILE

_CAP_KEYS = ("vti_core", "spy", "crypto", "nyse", "metal", "cash_buffer")
_ASSET_COLS = {
    "equities": ("SPY", "VTI"),
    "bonds": ("TLT",),
    "gold": ("GLD",),
    "commodities": ("USO", "CPER"),
}
_REGIME_PRIORS = {
    "growth": {"equities": 0.42, "bonds": 0.18, "gold": 0.12, "commodities": 0.13, "cash": 0.15},
    "inflation": {"equities": 0.18, "bonds": 0.10, "gold": 0.24, "commodities": 0.33, "cash": 0.15},
    "recession": {"equities": 0.14, "bonds": 0.34, "gold": 0.26, "commodities": 0.11, "cash": 0.15},
    "balanced": {"equities": 0.28, "bonds": 0.22, "gold": 0.18, "commodities": 0.17, "cash": 0.15},
}
POD_KEYS = ("spy", "crypto", "nyse", "stat_arb", "vol", "options")


def _series_vol(data: pd.DataFrame, symbols: tuple[str, ...], lookback: int = 20) -> float:
    vols: list[float] = []
    for sym in symbols:
        if sym not in data.columns:
            continue
        rets = data[sym].astype(float).pct_change().tail(lookback).dropna()
        if len(rets) >= 5:
            vols.append(float(rets.std()))
    if not vols:
        return 0.015
    return max(float(np.mean(vols)), 1e-4)


def detect_economic_regime(
    data: pd.DataFrame,
    regime: str,
    vol: str,
    *,
    macro_stress: bool = False,
) -> str:
    """Classify growth / inflation / recession for All Weather tilts."""
    regime_l = (regime or "").lower()
    oil_vol = _series_vol(data, ("USO", "XOM", "CPER"))
    eq_vol = _series_vol(data, ("SPY", "VTI"))

    oil_up = False
    gold_up = False
    if "USO" in data.columns and len(data) >= 6:
        s = data["USO"].dropna()
        if len(s) >= 6:
            oil_up = float(s.iloc[-1] / s.iloc[-6] - 1.0) > 0.04
    if "GLD" in data.columns and len(data) >= 6:
        g = data["GLD"].dropna()
        if len(g) >= 6:
            gold_up = float(g.iloc[-1] / g.iloc[-6] - 1.0) > 0.03

    if macro_stress or "bear" in regime_l or "panic" in regime_l:
        return "recession"
    if oil_up and (gold_up or oil_vol > eq_vol * 1.2):
        return "inflation"
    if "bull" in regime_l and vol == "Low" and not macro_stress:
        return "growth"
    return "balanced"


def risk_parity_allocation(
    economic_regime: str,
    data: pd.DataFrame,
    *,
    lookback: int = 20,
) -> dict[str, float]:
    """Inverse-vol weights blended with regime priors."""
    prior = dict(_REGIME_PRIORS.get(economic_regime, _REGIME_PRIORS["balanced"]))
    inv: dict[str, float] = {}
    for bucket, symbols in _ASSET_COLS.items():
        vol = _series_vol(data, symbols, lookback=lookback)
        inv[bucket] = 1.0 / vol
    inv_total = sum(inv.values()) or 1.0
    rp = {k: v / inv_total for k, v in inv.items()}
    rp["cash"] = prior.get("cash", 0.15)
    rp_total = sum(rp.values())
    rp = {k: v / rp_total for k, v in rp.items()}

    blend = 0.55
    out: dict[str, float] = {}
    keys = ("equities", "bonds", "gold", "commodities", "cash")
    for key in keys:
        out[key] = round(blend * rp.get(key, 0) + (1 - blend) * prior.get(key, 0), 4)
    total = sum(out.values()) or 1.0
    return {k: round(v / total, 4) for k, v in out.items()}


def _allocation_to_cap_targets(allocation: dict[str, float]) -> dict[str, float]:
    eq = float(allocation.get("equities", 0.25))
    bonds = float(allocation.get("bonds", 0.20))
    gold = float(allocation.get("gold", 0.15))
    comm = float(allocation.get("commodities", 0.15))
    cash = float(allocation.get("cash", 0.15))
    return {
        "vti_core": eq * 0.58,
        "spy": eq * 0.22,
        "nyse": eq * 0.08 + comm * 0.55,
        "crypto": eq * 0.12,
        "metal": gold,
        "cash_buffer": bonds + cash * 0.65,
    }


def merge_risk_parity_caps(
    base_caps: dict[str, float],
    allocation: dict[str, float],
    *,
    max_shift: float | None = None,
) -> tuple[dict[str, float], dict[str, float]]:
    """Shift sleeve caps toward risk-parity targets (bounded per sleeve)."""
    ms = max_shift if max_shift is not None else config.RISK_PARITY_MAX_CAP_SHIFT
    base = {k: float(base_caps.get(k, 0.0)) for k in _CAP_KEYS}
    targets = _allocation_to_cap_targets(allocation)
    merged = dict(base)
    shifts: dict[str, float] = {}
    for key in _CAP_KEYS:
        delta = (targets.get(key, base[key]) - base[key]) * 0.60
        delta = max(-ms, min(ms, delta))
        merged[key] = round(max(0.0, base[key] + delta), 6)
        shifts[key] = round(delta, 6)
    non_cash = sum(merged[k] for k in _CAP_KEYS if k != "cash_buffer")
    merged["cash_buffer"] = round(max(0.0, 1.0 - non_cash), 6)
    return merged, shifts


def _pod_limits() -> dict[str, float]:
    return dict(config.POD_MAX_DRAWDOWN_PCT)


def load_pod_state() -> dict:
    return read_json_file(POD_STATE_FILE)


def save_pod_state(state: dict) -> None:
    write_json_file(POD_STATE_FILE, state)


def _executor_pod_values(
    executor,
    *,
    pair_value: float | None = None,
    vol_value: float | None = None,
    options_value: float | None = None,
) -> dict[str, float]:
    values = {
        "spy": float(getattr(executor, "spy_sleeve_value", lambda: 0.0)()),
        "crypto": float(getattr(executor, "crypto_sleeve_value", lambda: 0.0)()),
        "nyse": float(getattr(executor, "nyse_sleeve_value", lambda: 0.0)()),
        "stat_arb": float(pair_value if pair_value is not None else getattr(executor, "pair_sleeve_value", lambda: 0.0)()),
        "vol": float(vol_value or 0.0),
        "options": float(options_value or 0.0),
    }
    return {k: max(0.0, v) for k, v in values.items()}


def update_pod_state(
    state: dict,
    pod_values: dict[str, float],
) -> dict[str, float]:
    """Update peak/trough tracking; return drawdown pct per pod."""
    peaks = state.setdefault("peaks", {})
    drawdowns: dict[str, float] = {}
    for pod, val in pod_values.items():
        peak = max(float(peaks.get(pod, val)), val)
        peaks[pod] = peak
        if peak > 1e-6:
            drawdowns[pod] = max(0.0, (peak - val) / peak)
        else:
            drawdowns[pod] = 0.0
    state["drawdowns"] = {k: round(v, 4) for k, v in drawdowns.items()}
    state["values"] = {k: round(v, 2) for k, v in pod_values.items()}
    return drawdowns


def pod_scales_from_drawdowns(drawdowns: dict[str, float]) -> dict[str, float]:
    """Millennium-style: reduce at 75% of limit, pause at limit."""
    limits = _pod_limits()
    scales: dict[str, float] = {}
    for pod, limit in limits.items():
        dd = float(drawdowns.get(pod, 0.0))
        if dd >= limit:
            scales[pod] = config.POD_PAUSE_SCALE
        elif dd >= limit * 0.75:
            scales[pod] = config.POD_REDUCE_SCALE
        else:
            scales[pod] = 1.0
    return scales


def evaluate_pod_risk(
    executor,
    state: dict | None = None,
    *,
    pair_value: float | None = None,
    vol_value: float | None = None,
    options_value: float | None = None,
    persist: bool = True,
) -> tuple[dict[str, float], dict[str, Any]]:
    pod_state = dict(state or load_pod_state())
    values = _executor_pod_values(
        executor,
        pair_value=pair_value,
        vol_value=vol_value,
        options_value=options_value,
    )
    drawdowns = update_pod_state(pod_state, values)
    scales = pod_scales_from_drawdowns(drawdowns)
    if persist:
        save_pod_state(pod_state)
    meta = {
        "drawdowns": drawdowns,
        "scales": scales,
        "values": values,
        "paused": [p for p, s in scales.items() if s <= config.POD_PAUSE_SCALE + 1e-9],
        "reduced": [
            p for p, s in scales.items()
            if config.POD_PAUSE_SCALE < s < 0.999
        ],
    }
    if meta.get("paused"):
        logger.warning("pod risk: paused pods", extra={"paused": meta.get("paused")})
    if meta.get("reduced"):
        logger.info("pod risk: reduced pods", extra={"reduced": meta.get("reduced")})
    return scales, meta


def format_risk_parity_log(economic_regime: str, allocation: dict[str, float]) -> str:
    parts = [
        f"{k} {allocation[k]:.0%}"
        for k in ("equities", "bonds", "gold", "commodities", "cash")
        if k in allocation
    ]
    return f"Risk Parity ({economic_regime}): " + ", ".join(parts)


def format_pod_risk_log(meta: dict[str, Any]) -> str:
    paused = meta.get("paused") or []
    reduced = meta.get("reduced") or []
    if paused:
        return f"Pod risk: PAUSE {', '.join(paused)}"
    if reduced:
        return f"Pod risk: reduce {', '.join(reduced)}"
    return ""


def apply_risk_parity_cycle(
    data: pd.DataFrame,
    regime: str,
    vol: str,
    executor,
    *,
    macro_stress: bool = False,
    equity: float | None = None,
    base_caps: dict[str, float] | None = None,
    pair_value: float | None = None,
    vol_value: float | None = None,
    options_value: float | None = None,
    persist_pod: bool = True,
) -> tuple[dict[str, float], dict[str, float], dict[str, Any], dict[str, Any]]:
    """Merge All Weather caps and evaluate pod drawdown limits."""
    if equity is not None and equity < config.SMALL_ACCOUNT_EQUITY_THRESHOLD:
        return base_caps or config.fund_allocation_pct(), {}, {}, {}

    caps = dict(base_caps or config.fund_allocation_pct())
    rp_meta: dict[str, Any] = {}
    if config.effective_risk_parity_enabled():
        econ = detect_economic_regime(data, regime, vol, macro_stress=macro_stress)
        allocation = risk_parity_allocation(econ, data)
        caps, shifts = merge_risk_parity_caps(caps, allocation)
        rp_meta = {
            "economic_regime": econ,
            "allocation": allocation,
            "shifts": shifts,
        }
        logger.info("apply_risk_parity_cycle applied", extra={"economic_regime": econ, "shifts": shifts})

    pod_scales, pod_meta = evaluate_pod_risk(
        executor,
        pair_value=pair_value,
        vol_value=vol_value,
        options_value=options_value,
        persist=persist_pod,
    )
    return caps, pod_scales, rp_meta, pod_meta


def pod_entries_allowed(executor, pod: str) -> bool:
    if not config.effective_risk_parity_enabled():
        return True
    fn = getattr(executor, "pod_risk_scale", None)
    if not callable(fn):
        return True
    return float(fn(pod)) > config.POD_PAUSE_SCALE + 0.05
