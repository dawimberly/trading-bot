"""
Split-score vol path — SHADOW ONLY.

DO NOT import into market_context.py / crypto_vol_gate.py yet.

Design (matches capital that actually uses each signal):
  - equity_vol_score  -> drives RHYME A/B (non-*-USD columns)
  - crypto_vol_score  -> drives crypto_vol_gate only (*-USD columns)
  - blended FIXED     -> kept for comparison (what dropna-fix alone does)

Also keeps BUGGY / FIXED blended helpers for prior shadow diffs.

Research sidecar. Freeze unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config  # noqa: E402


def _nan_to_zero(vol: float) -> float:
    try:
        v = float(vol)
    except (TypeError, ValueError):
        return 0.0
    return v if v == v else 0.0


def _is_crypto_col(col: object) -> bool:
    try:
        return bool(config.is_crypto(config.normalize_symbol(str(col))))
    except Exception:
        s = str(col).upper()
        return s.endswith("-USD") or s.endswith("/USD") or (
            s.endswith("USD") and ("-" in s or "/" in s)
        )


def split_columns(data: pd.DataFrame) -> tuple[list, list]:
    """Return (equity_cols, crypto_cols)."""
    if data is None or data.empty:
        return [], []
    crypto = [c for c in data.columns if _is_crypto_col(c)]
    equity = [c for c in data.columns if c not in crypto]
    return equity, crypto


def _panel_std_mean(data: pd.DataFrame) -> float:
    """Per-column std(skipna=True).mean() — no row-level dropna."""
    if data is None or data.empty or len(data) < 2 or data.shape[1] == 0:
        return 0.0
    return _nan_to_zero(data.pct_change().std(skipna=True).mean())


def cross_asset_vol_score_BUGGY(data: pd.DataFrame) -> float:
    """Production-equivalent buggy score (dropna how=any)."""
    if data is None or data.empty or len(data) < 2:
        return 0.0
    return _nan_to_zero(data.pct_change().dropna().std().mean())


def cross_asset_vol_score_FIXED(data: pd.DataFrame) -> float:
    """Blended FIXED — all columns (crypto + equity). Comparison baseline."""
    return _panel_std_mean(data)


def equity_vol_score(data: pd.DataFrame) -> float:
    """
    Equity / non-crypto panel only — proposed driver for RHYME A/B.
    """
    equity_cols, _ = split_columns(data)
    if not equity_cols:
        return 0.0
    return _panel_std_mean(data[equity_cols])


def crypto_vol_score(data: pd.DataFrame) -> float:
    """
    Crypto *-USD panel only — proposed driver for crypto_vol_gate.
    """
    _, crypto_cols = split_columns(data)
    if not crypto_cols:
        return 0.0
    return _panel_std_mean(data[crypto_cols])


def composition_snapshot(data: pd.DataFrame, *, threshold: float | None = None) -> dict:
    """One-shot crypto vs equity vs blended stats for a frame."""
    thr = float(
        threshold
        if threshold is not None
        else getattr(config, "REGIME_VOL_THRESHOLD_DAILY", 0.02)
    )
    equity_cols, crypto_cols = split_columns(data)
    out = {
        "n_equity": len(equity_cols),
        "n_crypto": len(crypto_cols),
        "equity_std": equity_vol_score(data),
        "crypto_std": crypto_vol_score(data),
        "blended_fixed": cross_asset_vol_score_FIXED(data),
        "blended_buggy": cross_asset_vol_score_BUGGY(data),
        "threshold": thr,
    }
    for key in ("equity_std", "crypto_std", "blended_fixed", "blended_buggy"):
        v = out[key]
        out[f"{key}_vs_thresh"] = (v / thr) if thr else float("nan")
    return out
