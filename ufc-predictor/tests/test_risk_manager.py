"""Tests for Monte Carlo risk manager."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.risk_manager import (
    assess_upcoming_card_risk,
    build_bet_schedule,
    recommended_card_risk_fraction,
    run_monte_carlo,
)


def _synthetic_predictions(n: int = 40) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    rows = []
    for i in range(n):
        p1 = float(rng.uniform(0.52, 0.68))
        m1 = p1 - 0.08  # model edge on f1
        m2 = 1.0 - m1
        f1_odds = round(1.0 / m1, 2)
        f2_odds = round(1.0 / m2, 2)
        rows.append(
            {
                "fight_id": f"f{i}",
                "event_name": f"UFC Event {i // 5}",
                "event_date": pd.Timestamp("2025-01-01") + pd.Timedelta(days=(i // 5) * 14),
                "prob_f1_win": p1,
                "prob_f2_win": 1.0 - p1,
                "f1_odds": f1_odds,
                "f2_odds": f2_odds,
                "f1_win": int(rng.random() < p1),
            }
        )
    return pd.DataFrame(rows)


def test_build_bet_schedule_filters_edge():
    preds = _synthetic_predictions(20)
    bets = build_bet_schedule(preds, min_edge=0.05)
    assert not bets.empty
    assert (bets["edge"] >= 0.05).all()


def test_run_monte_carlo_produces_metrics():
    preds = _synthetic_predictions(50)
    mc = run_monte_carlo(preds, n_simulations=500, random_seed=1)
    assert mc.n_simulations == 500
    assert "quarter_kelly" in mc.staking_summaries
    qk = mc.staking_summaries["quarter_kelly"]
    assert "expected_max_drawdown_pct" in qk
    assert "var_max_drawdown_pct" in qk
    assert "ruin_probability" in qk
    assert len(mc.max_drawdown_distribution["flat"]) == 500


def test_assess_upcoming_card_risk():
    preds = _synthetic_predictions(8)
    out = assess_upcoming_card_risk(preds, bankroll=5000, simulations=300, random_seed=2)
    assert out["available"] is True
    assert out["suggested_max_risk_pct"] > 0
    assert "quarter_kelly" in out["staking_modes"]


def test_recommended_card_risk_fraction_reduces_on_high_ruin():
    cap, warnings = recommended_card_risk_fraction(
        {"prob_loss": 0.4, "p5_pnl": -100, "ruin_probability": 0.12, "bankroll": 10000},
        base_cap=0.08,
    )
    assert cap < 0.08
    assert warnings
