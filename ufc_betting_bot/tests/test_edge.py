import pandas as pd

from ufc_betting_bot.modules.edge import has_valid_odds, market_probs


def test_no_edge_without_odds():
    row = pd.Series({"implied_prob_f1": 0.55, "f1_odds": float("nan"), "f2_odds": float("nan")})
    assert not has_valid_odds(row)
    assert market_probs(row) is None


def test_american_odds_implied():
    row = pd.Series({"f1_odds": 200.0, "f2_odds": -245.0})
    m = market_probs(row)
    assert m is not None
    assert abs(m[0] + m[1] - 1.0) < 1e-6
