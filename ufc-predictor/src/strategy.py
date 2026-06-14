"""Betting strategy: fractional Kelly, card risk caps, same-card parlays."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

import numpy as np
import pandas as pd

from ufc_betting_bot.modules.edge import fight_decimal_odds, market_probs, raw_kelly_fraction


@dataclass
class StrategyConfig:
    """Conservative defaults: quarter-Kelly, capped per bet and per card."""

    kelly_fraction: float = 0.25
    max_bet_fraction: float = 0.02
    min_bet_fraction: float = 0.005
    max_card_risk_fraction: float = 0.08
    min_edge: float = 0.05
    flat_stake: float = 10.0
    parlay_min_edge: float = 0.07
    parlay_min_combined_prob: float = 0.35
    parlay_max_legs: int = 3
    unrealistic_roi_threshold_pct: float = 500.0


@dataclass
class BetCandidate:
    fight_id: str
    event_key: str
    bet_side: str
    prob: float
    decimal_odds: float
    edge: float
    kelly_full: float
    expected_value: float
    fighter1_name: str = ""
    fighter2_name: str = ""
    pick_name: str = ""
    winner_name: str = ""
    market_type: str = "moneyline"
    prop_key: str = ""
    display_label: str = ""
    odds_source: str = "synthetic"


@dataclass
class ParlayCandidate:
    legs: list[BetCandidate]
    combined_prob: float
    combined_odds: float
    expected_value: float
    min_leg_edge: float


def kelly_stake(
    bankroll: float,
    *,
    prob: float,
    decimal_odds: float,
    edge: float,
    config: StrategyConfig,
) -> float:
    """Fractional Kelly stake with per-bet cap."""
    if edge < config.min_edge or bankroll <= 0:
        return 0.0
    kelly = raw_kelly_fraction(prob, decimal_odds) * config.kelly_fraction
    kelly = min(kelly, config.max_bet_fraction)
    if kelly < config.min_bet_fraction:
        return 0.0
    return float(min(bankroll * kelly, bankroll * config.max_bet_fraction))


def effective_card_risk_cap(
    config: StrategyConfig,
    mc_card_risk: dict[str, Any] | None = None,
) -> tuple[float, list[str]]:
    """Resolve per-card risk cap, optionally adjusted by Monte Carlo card assessment."""
    if not mc_card_risk:
        return config.max_card_risk_fraction, []
    try:
        from src.risk_manager import recommended_card_risk_fraction

        return recommended_card_risk_fraction(mc_card_risk, config.max_card_risk_fraction)
    except ImportError:
        return config.max_card_risk_fraction, []


def apply_card_risk_cap(
    stakes: list[float],
    bankroll: float,
    *,
    max_card_fraction: float,
    mc_card_risk: dict[str, Any] | None = None,
) -> tuple[list[float], float, list[str]]:
    """
    Scale down stakes so total card exposure <= max_card_fraction * bankroll.

    When ``mc_card_risk`` is provided, may lower the cap via Monte Carlo guidance.
    Returns (capped_stakes, effective_cap_fraction, warnings).
    """
    warnings: list[str] = []
    cap_fraction = max_card_fraction
    if mc_card_risk:
        try:
            from src.risk_manager import recommended_card_risk_fraction

            cap_fraction, cap_warnings = recommended_card_risk_fraction(mc_card_risk, max_card_fraction)
            warnings.extend(cap_warnings)
        except ImportError:
            pass

    if not stakes or bankroll <= 0:
        return stakes, cap_fraction, warnings
    total = sum(stakes)
    cap = bankroll * cap_fraction
    if total <= cap or total <= 0:
        return stakes, cap_fraction, warnings
    scale = cap / total
    return [s * scale for s in stakes], cap_fraction, warnings


def bet_expected_value(prob: float, decimal_odds: float) -> float:
    """EV per $1 staked."""
    if decimal_odds <= 1 or not np.isfinite(prob):
        return 0.0
    return prob * (decimal_odds - 1.0) - (1.0 - prob)


def extract_bet_candidates(
    row: pd.Series,
    *,
    config: StrategyConfig,
) -> BetCandidate | None:
    """Single-fight value bet candidate when odds and edge exist."""
    market = market_probs(row)
    decimal = fight_decimal_odds(row)
    if market is None or decimal is None:
        return None

    m1, m2 = market
    p1 = float(row.get("prob_f1_win", 0.5))
    p2 = float(row.get("prob_f2_win", 1.0 - p1))
    edge_f1 = p1 - m1
    edge_f2 = p2 - m2

    if edge_f1 >= edge_f2 and edge_f1 >= config.min_edge:
        side, prob, odds, edge = "f1", p1, decimal[0], edge_f1
    elif edge_f2 > edge_f1 and edge_f2 >= config.min_edge:
        side, prob, odds, edge = "f2", p2, decimal[1], edge_f2
    else:
        return None

    f1 = str(row.get("fighter_1", row.get("fighter1_name", row.get("fighter1", "")))).strip()
    f2 = str(row.get("fighter_2", row.get("fighter2_name", row.get("fighter2", "")))).strip()
    pick_name = f1 if side == "f1" else f2

    return BetCandidate(
        fight_id=str(row.get("fight_id", "")),
        event_key=str(row.get("event_name", row.get("event", ""))),
        bet_side=side,
        prob=prob,
        decimal_odds=odds,
        edge=edge,
        kelly_full=raw_kelly_fraction(prob, odds),
        expected_value=bet_expected_value(prob, odds),
        fighter1_name=f1,
        fighter2_name=f2,
        pick_name=pick_name,
        winner_name=pick_name,
    )


def build_parlay_candidates(
    card_rows: pd.DataFrame,
    *,
    config: StrategyConfig,
) -> list[ParlayCandidate]:
    """
    Same-card parlays only: 2–max_legs legs, min edge 7%, combined prob > 35%.
    Sorted by expected value descending.
    """
    legs: list[BetCandidate] = []
    for _, row in card_rows.iterrows():
        cand = extract_bet_candidates(row, config=config)
        if cand is None or cand.edge < config.parlay_min_edge:
            continue
        legs.append(cand)

    if len(legs) < 2:
        return []

    parlays: list[ParlayCandidate] = []
    max_legs = min(config.parlay_max_legs, len(legs))
    for n in range(2, max_legs + 1):
        for combo in combinations(legs, n):
            combined_prob = float(np.prod([c.prob for c in combo]))
            if combined_prob < config.parlay_min_combined_prob:
                continue
            combined_odds = float(np.prod([c.decimal_odds for c in combo]))
            ev = combined_prob * (combined_odds - 1.0) - (1.0 - combined_prob)
            parlays.append(
                ParlayCandidate(
                    legs=list(combo),
                    combined_prob=combined_prob,
                    combined_odds=combined_odds,
                    expected_value=ev,
                    min_leg_edge=min(c.edge for c in combo),
                )
            )

    parlays.sort(key=lambda p: p.expected_value, reverse=True)
    return parlays


def strategy_from_profile(
    *,
    min_edge: float | None = None,
    bankroll: float | None = None,
    recent_win_rate: float | None = None,
    model_confidence: float | None = None,
    hours_to_event: float | None = None,
    use_dynamic_thresholds: bool | None = None,
) -> StrategyConfig:
    """Build StrategyConfig from active UFC_PROFILE thresholds (optionally dynamic)."""
    import config as _cfg

    enabled = (
        _cfg.DYNAMIC_THRESHOLDS_ENABLED if use_dynamic_thresholds is None else use_dynamic_thresholds
    )
    if enabled and bankroll is not None:
        from ufc_betting_bot.modules.dynamic_thresholds import get_profile_thresholds

        thresholds = get_profile_thresholds(
            bankroll,
            recent_win_rate,
            model_confidence,
            hours_to_event=hours_to_event,
            profile=_cfg.UFC_PROFILE,
        )
        edge = thresholds.alert_min_edge if min_edge is None else min_edge
        return StrategyConfig(
            kelly_fraction=_cfg.profile_value("kelly_fraction"),
            max_bet_fraction=_cfg.profile_value("max_bet_fraction"),
            max_card_risk_fraction=_cfg.profile_value("max_card_risk_fraction"),
            min_edge=edge,
            parlay_min_edge=thresholds.parlay_min_edge,
            parlay_min_combined_prob=thresholds.parlay_min_combined_prob,
            parlay_max_legs=_cfg.ALERT_PARLAY_MAX_LEGS,
            flat_stake=_cfg.FLAT_STAKE,
        )

    ps = _cfg.profile_settings()
    edge = ps["alert_min_edge"] if min_edge is None else min_edge
    return StrategyConfig(
        kelly_fraction=ps["kelly_fraction"],
        max_bet_fraction=ps["max_bet_fraction"],
        max_card_risk_fraction=ps["max_card_risk_fraction"],
        min_edge=edge,
        parlay_min_edge=ps["parlay_min_edge"],
        parlay_min_combined_prob=ps["parlay_min_combined_prob"],
        parlay_max_legs=_cfg.ALERT_PARLAY_MAX_LEGS,
        flat_stake=_cfg.FLAT_STAKE,
    )


def _pick_model_prob(row: pd.Series) -> tuple[str, float, str]:
    """Return (pick, model_prob, fight_label) for a prediction row."""
    f1 = str(row.get("fighter_1", row.get("fighter1", "")))
    f2 = str(row.get("fighter_2", row.get("fighter2", "")))
    pick = str(row.get("predicted_winner", ""))
    prob = row.get("predicted_prob", row.get("prob_f1_win"))
    if pd.isna(prob):
        if pick == f2 and pd.notna(row.get("prob_f2_win")):
            prob = float(row["prob_f2_win"])
        elif pick == f1 and pd.notna(row.get("prob_f1_win")):
            prob = float(row["prob_f1_win"])
        else:
            p1 = float(row.get("prob_f1_win", 0.5))
            prob = p1 if pick == f1 else 1.0 - p1
    return pick, float(prob), f"{f1} vs {f2}"


def build_model_only_parlay_candidates(
    card_rows: pd.DataFrame,
    *,
    min_pick_prob: float = 0.52,
    parlay_max_legs: int = 3,
    parlay_min_combined_prob: float = 0.25,
) -> list[dict[str, Any]]:
    """
    Research helper: rank same-card parlays by model probability only (no odds).

    Uses highest-confidence picks per fight; does not imply +EV without market lines.
    """
    legs: list[dict[str, Any]] = []
    for _, row in card_rows.iterrows():
        pick, prob, fight = _pick_model_prob(row)
        if prob < min_pick_prob:
            continue
        legs.append(
            {
                "fight": fight,
                "pick": pick,
                "prob": prob,
                "fight_id": str(row.get("fight_id", fight)),
            }
        )
    if len(legs) < 2:
        return []

    out: list[dict[str, Any]] = []
    max_legs = min(parlay_max_legs, len(legs))
    for n in range(2, max_legs + 1):
        for combo in combinations(legs, n):
            combined_prob = float(np.prod([leg["prob"] for leg in combo]))
            if combined_prob < parlay_min_combined_prob:
                continue
            picks_txt = " + ".join(f"{leg['pick']} ({leg['prob']:.0%})" for leg in combo)
            out.append(
                {
                    "n_legs": n,
                    "legs": list(combo),
                    "combined_prob": combined_prob,
                    "picks": picks_txt,
                    "model_only": True,
                }
            )
    out.sort(key=lambda x: x["combined_prob"], reverse=True)
    return out[:5]


def compute_equity_metrics(equity: pd.Series) -> dict[str, float]:
    """Max drawdown % and longest win streak from chronological equity curve."""
    if equity.empty:
        return {"max_drawdown_pct": 0.0, "max_win_streak": 0.0}

    eq = equity.astype(float).reset_index(drop=True)
    peak = eq.cummax()
    drawdown = np.where(peak > 0, (peak - eq) / peak, 0.0)
    max_dd = float(np.max(drawdown) * 100.0) if len(drawdown) else 0.0

    return {"max_drawdown_pct": max_dd, "max_win_streak": 0.0}


def compute_trade_streaks(won: pd.Series) -> dict[str, float]:
    if won.empty:
        return {"max_win_streak": 0.0, "max_loss_streak": 0.0}
    best_win = best_loss = cur_win = cur_loss = 0
    for w in won.astype(int):
        if w:
            cur_win += 1
            cur_loss = 0
        else:
            cur_loss += 1
            cur_win = 0
        best_win = max(best_win, cur_win)
        best_loss = max(best_loss, cur_loss)
    return {"max_win_streak": float(best_win), "max_loss_streak": float(best_loss)}


def warn_unrealistic_roi(roi_pct: float, *, threshold: float = 500.0) -> str | None:
    if not np.isfinite(roi_pct) or roi_pct <= threshold:
        return None
    return (
        f"ROI {roi_pct:.1f}% exceeds {threshold:.0f}% — likely overfitting, odds leakage, "
        "or compounding artifacts. Treat as diagnostic only."
    )


def enrich_summary_with_risk_metrics(
    trades: pd.DataFrame,
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Add drawdown / streak stats to a backtest summary dict."""
    if trades.empty:
        summary.update(max_drawdown_pct=0.0, max_win_streak=0.0, max_loss_streak=0.0)
        return summary
    if "equity" in trades.columns:
        summary.update(compute_equity_metrics(trades["equity"]))
    if "won" in trades.columns:
        summary.update(compute_trade_streaks(trades["won"]))
    warning = warn_unrealistic_roi(float(summary.get("roi_pct", 0)))
    if warning:
        summary["roi_warning"] = warning
    return summary
