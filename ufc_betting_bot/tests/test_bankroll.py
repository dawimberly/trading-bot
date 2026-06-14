from __future__ import annotations

import pandas as pd

from ufc_betting_bot.config.settings import BankrollSettings
from ufc_betting_bot.modules.bankroll import BankrollManager, simulate_bankroll_bets


def test_max_bet_fraction_cap():
    settings = BankrollSettings(
        initial_bankroll=1000,
        kelly_fraction=1.0,
        max_bet_fraction=0.02,
        min_edge=0.01,
    )
    mgr = BankrollManager(settings)
    stake = mgr.compute_stake(prob=0.7, decimal_odds=2.0, edge=0.15)
    assert stake <= 1000 * 0.02 + 1e-9


def test_daily_loss_limit_blocks_bets():
    settings = BankrollSettings(
        initial_bankroll=1000,
        daily_loss_limit_fraction=0.05,
        max_bet_fraction=0.02,
        min_edge=0.01,
    )
    mgr = BankrollManager(settings)
    mgr._sync_day("2025-06-01")
    mgr.record_bet(50, won=False, decimal_odds=2.0)
    assert mgr.state.daily_pnl == -50
    # Loss limit is 5% = $50 — should halt
    assert not mgr.can_bet("2025-06-01")


def test_simulate_skips_rows_without_odds():
    preds = pd.DataFrame(
        {
            "event_date": pd.to_datetime(["2025-01-01", "2025-01-02"]),
            "f1_win": [1, 0],
            "prob_f1_win": [0.65, 0.40],
            "prob_f2_win": [0.35, 0.60],
            "f1_odds": [1.8, float("nan")],
            "f2_odds": [2.1, float("nan")],
        }
    )
    settings = BankrollSettings(initial_bankroll=1000, min_edge=0.05)
    trades, summary = simulate_bankroll_bets(preds, settings)
    assert summary["trades"] <= 1
