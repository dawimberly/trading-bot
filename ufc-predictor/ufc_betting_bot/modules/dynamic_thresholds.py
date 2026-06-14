"""Conservative dynamic betting thresholds (research vs live base profiles)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

REFERENCE_BANKROLL = 1000.0
RECENT_BET_WINDOW = 30

_PROFILE_BASES: dict[str, dict[str, float]] = {
    "research": {
        "alert_min_edge": 0.04,
        "parlay_min_edge": 0.03,
        "parlay_min_combined_prob": 0.25,
        "parlay_min_ev": 0.08,
    },
    "live": {
        "alert_min_edge": 0.08,
        "parlay_min_edge": 0.07,
        "parlay_min_combined_prob": 0.35,
        "parlay_min_ev": 0.15,
    },
}

_PROFILE_BOUNDS: dict[str, dict[str, tuple[float, float]]] = {
    "research": {
        "alert_min_edge": (0.03, 0.12),
        "parlay_min_edge": (0.025, 0.10),
        "parlay_min_combined_prob": (0.20, 0.45),
        "parlay_min_ev": (0.06, 0.20),
    },
    "live": {
        "alert_min_edge": (0.06, 0.18),
        "parlay_min_edge": (0.05, 0.14),
        "parlay_min_combined_prob": (0.30, 0.55),
        "parlay_min_ev": (0.10, 0.30),
    },
}


@dataclass
class ThresholdResult:
    alert_min_edge: float
    parlay_min_edge: float
    parlay_min_combined_prob: float
    parlay_min_ev: float
    profile: str
    adjustments: list[str] = field(default_factory=list)
    base: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "alert_min_edge": self.alert_min_edge,
            "parlay_min_edge": self.parlay_min_edge,
            "parlay_min_combined_prob": self.parlay_min_combined_prob,
            "parlay_min_ev": self.parlay_min_ev,
            "profile": self.profile,
            "adjustments": list(self.adjustments),
            "base": dict(self.base),
        }


def _active_profile(profile: str | None) -> str:
    if profile:
        return profile.strip().lower()
    try:
        import config as _cfg

        return _cfg.UFC_PROFILE.strip().lower()
    except ImportError:
        return "research"


def _clamp(value: float, low: float, high: float) -> float:
    return float(max(low, min(high, value)))


def model_confidence_from_prob(prob: float) -> float:
    """0–1 confidence from distance of win probability from 50/50."""
    if not np.isfinite(prob):
        return 0.5
    return float(_clamp(abs(prob - 0.5) * 2.0, 0.0, 1.0))


def model_confidence_from_row(row: pd.Series) -> float:
    """Blend model probability spread with optional SHAP strength."""
    p1 = row.get("prob_f1_win", row.get("predicted_prob"))
    if pd.isna(p1):
        p1 = 0.5
    conf = model_confidence_from_prob(float(p1))
    shap_strength = row.get("shap_strength")
    if pd.notna(shap_strength):
        conf = 0.7 * conf + 0.3 * float(_clamp(shap_strength, 0.0, 1.0))
    return conf


def model_confidence_from_predictions(predictions: pd.DataFrame) -> float:
    if predictions is None or predictions.empty:
        return 0.5
    values = [model_confidence_from_row(row) for _, row in predictions.iterrows()]
    return float(np.mean(values)) if values else 0.5


def recent_win_rate_from_trades(
    trades: list[bool] | pd.Series | None,
    *,
    window: int = RECENT_BET_WINDOW,
) -> float | None:
    if trades is None:
        return None
    if isinstance(trades, pd.Series):
        wins = trades.astype(int).tolist()
    else:
        wins = [int(w) for w in trades]
    if not wins:
        return None
    sample = wins[-window:]
    return float(sum(sample) / len(sample))


def hours_to_event_from_row(
    row: pd.Series,
    *,
    now: pd.Timestamp | None = None,
) -> float | None:
    for col in ("event_date", "date", "commence_time"):
        if col not in row or pd.isna(row[col]):
            continue
        event_dt = pd.Timestamp(row[col])
        if event_dt.tzinfo is None:
            event_dt = event_dt.tz_localize("UTC")
        ref = now or pd.Timestamp.now(tz="UTC")
        delta_h = (event_dt - ref).total_seconds() / 3600.0
        return float(max(delta_h, 0.0))
    return None


def get_profile_thresholds(
    bankroll: float,
    recent_win_rate: float | None,
    model_confidence: float | None,
    *,
    hours_to_event: float | None = None,
    profile: str | None = None,
) -> ThresholdResult:
    """
    Adjust RESEARCH_/LIVE_ base thresholds using bankroll, form, confidence, and time.

    Conservative: stricter on small bankrolls (limited capital) and large bankrolls
    (protect accumulated gains). Loosening is capped and rare.
    """
    prof = _active_profile(profile)
    if prof not in _PROFILE_BASES:
        prof = "research"
    base = dict(_PROFILE_BASES[prof])
    bounds = _PROFILE_BOUNDS[prof]
    adjustments: list[str] = []

    edge_adj = 0.0
    parlay_edge_adj = 0.0
    prob_adj = 0.0
    ev_adj = 0.0

    br = max(float(bankroll), 50.0)
    ratio = br / REFERENCE_BANKROLL
    if ratio < 0.5:
        edge_adj += 0.012
        prob_adj += 0.02
        adjustments.append(f"small bankroll ${br:,.0f} (+edge)")
    elif ratio < 0.8:
        edge_adj += 0.005
        adjustments.append(f"below-reference bankroll ${br:,.0f}")
    elif ratio > 5.0:
        edge_adj += 0.035
        parlay_edge_adj += 0.015
        prob_adj += 0.04
        ev_adj += 0.03
        adjustments.append(f"large bankroll ${br:,.0f} (capital protection)")
    elif ratio > 2.0:
        edge_adj += min(0.02, 0.006 * math.log2(ratio))
        parlay_edge_adj += min(0.01, 0.003 * math.log2(ratio))
        prob_adj += min(0.02, 0.008 * math.log2(ratio))
        adjustments.append(f"growing bankroll ${br:,.0f}")

    wr = 0.5 if recent_win_rate is None else float(recent_win_rate)
    if wr < 0.40:
        edge_adj += 0.015
        ev_adj += 0.02
        adjustments.append(f"cold streak win rate {wr:.0%}")
    elif wr < 0.48:
        edge_adj += 0.008
        adjustments.append(f"below-average win rate {wr:.0%}")
    elif wr > 0.58:
        edge_adj -= 0.003
        adjustments.append(f"warm streak win rate {wr:.0%} (slight loosen)")

    conf = 0.5 if model_confidence is None else float(model_confidence)
    if conf < 0.45:
        edge_adj += 0.01
        prob_adj += 0.03
        adjustments.append(f"low model confidence {conf:.0%}")
    elif conf > 0.65:
        edge_adj -= 0.005
        adjustments.append(f"high model confidence {conf:.0%}")

    if hours_to_event is not None:
        h = float(hours_to_event)
        if h < 24:
            edge_adj += 0.012
            parlay_edge_adj += 0.01
            prob_adj += 0.02
            adjustments.append(f"fight <24h ({h:.0f}h)")
        elif h < 72:
            edge_adj += 0.006
            parlay_edge_adj += 0.005
            adjustments.append(f"fight <72h ({h:.0f}h)")
        elif h > 168:
            edge_adj -= 0.002
            adjustments.append(f"fight >7d out ({h:.0f}h)")

    out = ThresholdResult(
        alert_min_edge=_clamp(base["alert_min_edge"] + edge_adj, *bounds["alert_min_edge"]),
        parlay_min_edge=_clamp(
            base["parlay_min_edge"] + parlay_edge_adj + edge_adj * 0.5,
            *bounds["parlay_min_edge"],
        ),
        parlay_min_combined_prob=_clamp(
            base["parlay_min_combined_prob"] + prob_adj,
            *bounds["parlay_min_combined_prob"],
        ),
        parlay_min_ev=_clamp(base["parlay_min_ev"] + ev_adj + edge_adj * 0.25, *bounds["parlay_min_ev"]),
        profile=prof,
        adjustments=adjustments,
        base=base,
    )
    return out


def example_threshold_table(
    *,
    profile: str | None = None,
    bankrolls: list[float] | None = None,
    model_confidence: float = 0.55,
    recent_win_rate: float = 0.50,
    hours_to_event: float = 48.0,
) -> pd.DataFrame:
    """Sample threshold adjustments for reporting."""
    bankrolls = bankrolls or [250.0, 500.0, 1000.0, 2500.0, 5000.0, 10000.0]
    rows: list[dict[str, Any]] = []
    for br in bankrolls:
        t = get_profile_thresholds(
            br,
            recent_win_rate,
            model_confidence,
            hours_to_event=hours_to_event,
            profile=profile,
        )
        rows.append(
            {
                "bankroll": br,
                "min_edge": t.alert_min_edge,
                "parlay_leg_edge": t.parlay_min_edge,
                "combined_prob": t.parlay_min_combined_prob,
                "min_ev": t.parlay_min_ev,
                "adjustments": "; ".join(t.adjustments) or "base",
            }
        )
    return pd.DataFrame(rows)


def print_threshold_comparison_report(
    static_summary: dict[str, float],
    dynamic_summary: dict[str, float],
    *,
    profile: str | None = None,
    target_year: int = 2025,
) -> None:
    """Console report: static vs dynamic threshold backtest."""
    static_roi = float(static_summary.get("roi_pct", 0.0))
    dynamic_roi = float(dynamic_summary.get("roi_pct", 0.0))
    roi_delta = dynamic_roi - static_roi

    print(f"\n  DYNAMIC THRESHOLDS — {target_year} COMPARISON")
    print("  " + "=" * 72)
    print(f"  {'Mode':<12} {'Bets':>6} {'Hit rate':>10} {'ROI':>10} {'Max DD':>10} {'Final $':>12}")
    print("  " + "-" * 72)
    for label, summary in (("static", static_summary), ("dynamic", dynamic_summary)):
        print(
            f"  {label:<12} "
            f"{int(summary.get('trades', 0)):>6} "
            f"{summary.get('hit_rate', 0):>9.1%} "
            f"{summary.get('roi_pct', 0):>9.1f}% "
            f"{summary.get('max_drawdown_pct', 0):>9.1f}% "
            f"${summary.get('final_equity', summary.get('final_bankroll', 0)):>10,.0f}"
        )
    print("  " + "-" * 72)
    sign = "+" if roi_delta >= 0 else ""
    print(f"  ROI change (dynamic vs static): {sign}{roi_delta:.1f} pp")
    bet_delta = int(dynamic_summary.get("trades", 0)) - int(static_summary.get("trades", 0))
    print(f"  Bet count change: {bet_delta:+d}")

    examples = example_threshold_table(profile=profile)
    print("\n  EXAMPLE THRESHOLDS BY BANKROLL (50% win rate, 55% confidence, 48h to event)")
    print("  " + "-" * 72)
    for _, row in examples.iterrows():
        print(
            f"  ${row['bankroll']:>7,.0f}  "
            f"edge {row['min_edge']:.1%}  "
            f"leg {row['parlay_leg_edge']:.1%}  "
            f"prob {row['combined_prob']:.0%}  "
            f"EV {row['min_ev']:.0%}  "
            f"({row['adjustments']})"
        )
