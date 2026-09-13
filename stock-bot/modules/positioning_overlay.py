"""COT / positioning overlay — contrarian tilt on large-spec net positioning.

Loads latest Commitments of Traders snapshot from a local JSON file (default
``reference/cot_es.json``). Optional API fetch is stubbed for future wiring.

Contrarian rules (large speculators):
  - Heavily net short → bullish risk tilt (crowded bear → fade)
  - Heavily net long  → bearish risk tilt (crowded bull → fade)
  - Extreme readings use stronger multipliers

Multiplies with regime / drawdown risk in ``config.effective_risk_per_trade``
and ``effective_regime_sizing_multiplier``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger(__name__)

_CACHE: dict[str, Any] = {"path": None, "mtime": None, "snapshot": None}


def effective_positioning_overlay_enabled() -> bool:
    return config.effective_positioning_overlay_enabled()


def _cot_path() -> Path:
    raw = config.COT_DATA_FILE
    p = Path(raw)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / raw
    return p


def fetch_cot_from_api() -> dict[str, Any] | None:
    """API stub — wire to CFTC / third-party feed when ready."""
    if not config.COT_API_ENABLED:
        return None
    logger.debug("COT API fetch not implemented (stub)")
    return None


def load_cot_snapshot(*, force: bool = False) -> dict[str, Any] | None:
    """Load COT JSON from disk; optional API fallback when file missing."""
    path = _cot_path()
    try:
        mtime = path.stat().st_mtime if path.is_file() else None
    except OSError:
        mtime = None

    if (
        not force
        and _CACHE.get("snapshot") is not None
        and _CACHE.get("path") == str(path)
        and _CACHE.get("mtime") == mtime
    ):
        return dict(_CACHE["snapshot"])

    snapshot: dict[str, Any] | None = None
    if path.is_file():
        try:
            with open(path, encoding="utf-8") as f:
                snapshot = json.load(f)
            snapshot.setdefault("source", "file")
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("COT file read failed (%s): %s", path, exc)

    if snapshot is None:
        snapshot = fetch_cot_from_api()

    _CACHE["path"] = str(path)
    _CACHE["mtime"] = mtime
    _CACHE["snapshot"] = snapshot
    return dict(snapshot) if snapshot else None


def _net_pct_oi(snapshot: dict[str, Any]) -> float | None:
    if "large_specs_net_pct_oi" in snapshot:
        return float(snapshot["large_specs_net_pct_oi"])
    oi = float(snapshot.get("open_interest") or 0)
    net = snapshot.get("large_specs_net")
    if oi > 0 and net is not None:
        return float(net) / oi
    return None


def classify_cot_signal(snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return signal label, net % OI, and risk multiplier."""
    snap = snapshot if snapshot is not None else load_cot_snapshot()
    neutral = {
        "active": False,
        "label": "neutral",
        "net_pct_oi": None,
        "multiplier": 1.0,
        "as_of": None,
        "contract": config.COT_CONTRACT_LABEL,
    }
    if not effective_positioning_overlay_enabled():
        return neutral
    if not snap:
        neutral["label"] = "no_data"
        return neutral

    net_pct = _net_pct_oi(snap)
    if net_pct is None:
        neutral["label"] = "no_data"
        neutral["as_of"] = snap.get("as_of")
        return neutral

    short_thresh = config.COT_NET_SHORT_THRESH
    long_thresh = config.COT_NET_LONG_THRESH
    extreme_short = config.COT_EXTREME_SHORT_THRESH
    extreme_long = config.COT_EXTREME_LONG_THRESH

    mult = 1.0
    label = "neutral"
    if net_pct <= extreme_short:
        mult = config.COT_BULLISH_EXTREME_MULT
        label = "extreme_short_bullish"
    elif net_pct <= short_thresh:
        mult = config.COT_BULLISH_MULT
        label = "short_bullish"
    elif net_pct >= extreme_long:
        mult = config.COT_BEARISH_EXTREME_MULT
        label = "extreme_long_bearish"
    elif net_pct >= long_thresh:
        mult = config.COT_BEARISH_MULT
        label = "long_bearish"

    return {
        "active": label != "neutral",
        "label": label,
        "net_pct_oi": round(net_pct, 4),
        "multiplier": round(max(0.05, min(2.0, float(mult))), 4),
        "as_of": snap.get("as_of"),
        "contract": snap.get("contract") or config.COT_CONTRACT_LABEL,
    }


def positioning_risk_multiplier(*, refresh: bool = False) -> float:
    """Risk / sizing multiplier from latest COT snapshot (1.0 when inactive)."""
    if not effective_positioning_overlay_enabled():
        return 1.0
    sig = classify_cot_signal(load_cot_snapshot(force=refresh) if refresh else None)
    return float(sig.get("multiplier") or 1.0)


def format_positioning_banner() -> str | None:
    """One-line COT overlay for startup / cycle banners."""
    if not effective_positioning_overlay_enabled():
        return None
    sig = classify_cot_signal()
    if sig["label"] == "no_data":
        return (
            f">>> POSITIONING OVERLAY (COT) — no data "
            f"(set {config.COT_DATA_FILE}) | mult x1.00"
        )
    net_s = f"{sig['net_pct_oi']:+.1%}" if sig["net_pct_oi"] is not None else "n/a"
    as_of = sig.get("as_of") or "?"
    tilt = sig["label"].replace("_", " ")
    return (
        f">>> POSITIONING OVERLAY (COT) — {sig.get('contract', 'ES')} "
        f"large-spec {net_s} | {tilt} | mult x{sig['multiplier']:.2f} "
        f"(as of {as_of})"
    )
