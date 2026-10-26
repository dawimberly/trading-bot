"""Research accounting: end-of-bar equity and closed-trade metrics.

Synthetic prices and orders only. No broker, no market data.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from backtester import BacktestPortfolio, record_end_of_bar_equity
from modules.backtester_core import (
    RUN_OPTIONS,
    compute_performance_metrics,
    compute_regime_breakdown,
)


@pytest.fixture
def zero_equity_costs():
    saved = (RUN_OPTIONS.equity_slippage_bps, RUN_OPTIONS.equity_commission_bps)
    RUN_OPTIONS.equity_slippage_bps = 0.0
    RUN_OPTIONS.equity_commission_bps = 0.0
    yield
    RUN_OPTIONS.equity_slippage_bps, RUN_OPTIONS.equity_commission_bps = saved


def test_last_bar_fill_is_in_the_reported_mark(zero_equity_costs):
    portfolio = BacktestPortfolio(10_000.0)
    prices = pd.Series({"AAA": 10.0})
    decision = [portfolio.equity(prices)]
    reported: list[float] = []
    portfolio.trade("AAA", "buy", 10.0, tx_cost=0.001, notional=1_000.0)
    record_end_of_bar_equity(portfolio, prices, reported)
    assert reported[-1] == pytest.approx(portfolio.equity(prices))
    assert reported[-1] < decision[-1]
    assert len(portfolio.closed_trades) == 0


def test_long_round_trip_realized_pnl_matches_cash(zero_equity_costs):
    portfolio = BacktestPortfolio(10_000.0)
    portfolio.trade("AAA", "buy", 10.0, tx_cost=0.0, notional=1_000.0)
    portfolio.trade("AAA", "sell", 12.0, tx_cost=0.0, notional=1_200.0)
    assert portfolio.cash == pytest.approx(10_200.0)
    assert portfolio.positions == {}
    trade = portfolio.closed_trades[0]
    assert trade["position_side"] == "long"
    assert trade["realized_pnl"] == pytest.approx(200.0)
    assert trade["return_pct"] == pytest.approx(20.0)
    assert trade["entry_notional"] == pytest.approx(1_000.0)


def test_long_round_trip_includes_fee(zero_equity_costs):
    portfolio = BacktestPortfolio(10_000.0)
    portfolio.trade("AAA", "buy", 10.0, tx_cost=0.001, notional=1_000.0)
    portfolio.trade("AAA", "sell", 10.0, tx_cost=0.001, notional=1_000.0)
    trade = portfolio.closed_trades[0]
    assert trade["realized_pnl"] == pytest.approx(999.0 - 1_001.0)
    assert portfolio.cash == pytest.approx(10_000.0 - 1_001.0 + 999.0)


def test_partial_long_close_uses_allocated_cost(zero_equity_costs):
    portfolio = BacktestPortfolio(10_000.0)
    portfolio.trade("AAA", "buy", 10.0, tx_cost=0.0, notional=1_000.0)
    portfolio.trade("AAA", "sell", 12.0, tx_cost=0.0, notional=400.0)
    trade = portfolio.closed_trades[0]
    assert trade["return_pct"] == pytest.approx(20.0)
    assert trade["realized_pnl"] == pytest.approx(400.0 - 1_000.0 * (400.0 / 12.0) / 100.0)
    assert portfolio.positions["AAA"] == pytest.approx(100.0 - 400.0 / 12.0)
    assert len(portfolio.closed_trades) == 1


def test_short_cover_realized_pnl(zero_equity_costs):
    portfolio = BacktestPortfolio(10_000.0)
    opened = portfolio.trade(
        "AAA", "sell", 10.0, tx_cost=0.0, notional=1_000.0, allow_naked_short=True
    )
    assert opened is not None
    assert portfolio.closed_trades == []
    portfolio.trade("AAA", "buy", 8.0, tx_cost=0.0, notional=800.0)
    assert portfolio.positions == {}
    assert portfolio.cash == pytest.approx(10_200.0)
    trade = portfolio.closed_trades[0]
    assert trade["position_side"] == "short"
    assert trade["realized_pnl"] == pytest.approx(200.0)
    assert trade["return_pct"] == pytest.approx(20.0)


def test_partial_short_cover(zero_equity_costs):
    portfolio = BacktestPortfolio(10_000.0)
    portfolio.trade(
        "AAA", "sell", 10.0, tx_cost=0.0, notional=1_000.0, allow_naked_short=True
    )
    portfolio.trade("AAA", "buy", 8.0, tx_cost=0.0, notional=400.0)
    trade = portfolio.closed_trades[0]
    assert trade["realized_pnl"] == pytest.approx(100.0)
    assert trade["return_pct"] == pytest.approx(20.0)
    assert portfolio.positions["AAA"] == pytest.approx(-50.0)


def test_avg_trade_is_not_return_divided_by_orders():
    marks = [110.0, 121.0, 99.0]
    trades = [
        {"realized_pnl": 200.0, "return_pct": 20.0},
        {"realized_pnl": -100.0, "return_pct": -10.0},
    ]
    perf = compute_performance_metrics(
        marks,
        initial_capital=100.0,
        total_orders=9,
        closed_trades=trades,
    )
    assert perf["avg_trade_return_pct"] == pytest.approx(5.0)
    assert perf["return_per_order_pct"] != pytest.approx(perf["avg_trade_return_pct"])
    assert perf["return_per_order_pct"] == pytest.approx(round(perf["total_return_pct"] / 9, 3))
    assert perf["closed_trade_win_rate_pct"] == pytest.approx(50.0)
    assert perf["trade_profit_factor"] == pytest.approx(2.0)
    assert perf["win_rate_pct"] != perf["closed_trade_win_rate_pct"]


def test_omitted_closed_trades_keep_per_order_average():
    perf = compute_performance_metrics(
        [110.0],
        initial_capital=100.0,
        total_orders=4,
    )
    assert perf["avg_trade_return_pct"] == perf["return_per_order_pct"]
    assert perf["avg_trade_return_basis"] == "return_per_order"
    assert perf["closed_trade_count"] is None


def test_sortino_uses_full_sample_downside_deviation():
    rets = [0.10, -0.05, -0.05, 0.02]
    path = [100.0]
    for ret in rets:
        path.append(path[-1] * (1.0 + ret))
    mean = sum(rets) / len(rets)
    downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / len(rets))
    expected = mean / downside * math.sqrt(252.0)
    perf = compute_performance_metrics(path[1:], initial_capital=100.0, total_orders=0)
    assert perf["sortino"] == pytest.approx(round(expected, 2))
    assert perf["sortino"] > 0
    assert perf["sortino_negative_subset"] == 0.0


def test_drawdown_includes_a_drop_from_initial_capital():
    perf = compute_performance_metrics([80.0, 90.0], initial_capital=100.0, total_orders=0)
    assert perf["max_drawdown_pct"] == pytest.approx(-20.0)
    assert min(perf["drawdown_series"]) == pytest.approx(-20.0)
    assert perf["return_over_max_dd"] == pytest.approx(-0.5)
    assert perf["win_rate_pct"] == pytest.approx(50.0)


def test_calmar_is_cagr_over_drawdown():
    perf = compute_performance_metrics([80.0, 90.0], initial_capital=100.0, total_orders=0)
    cagr = (90.0 / 100.0) ** (252.0 / 2.0) - 1.0
    assert perf["calmar"] == pytest.approx(round(cagr / 0.2, 2))
    assert perf["calmar"] != perf["return_over_max_dd"]


def test_trade_profit_factor_is_undefined_without_losses():
    perf = compute_performance_metrics(
        [110.0],
        initial_capital=100.0,
        total_orders=1,
        closed_trades=[{"realized_pnl": 50.0, "return_pct": 5.0}],
    )
    assert perf["trade_profit_factor"] is None
    assert perf["profit_factor"] > 0


def test_regime_breakdown_includes_the_first_bar():
    rows = compute_regime_breakdown(
        [110.0, 90.0],
        [
            "RHYME_C: Steady_Bullish_Growth",
            "RHYME_E: Steady_Bearish_Decline",
        ],
        initial_capital=100.0,
    )
    by_short = {row["short"]: row for row in rows}
    assert by_short["RHYME_C"]["pnl_usd"] == pytest.approx(10.0)
    assert by_short["RHYME_E"]["pnl_usd"] == pytest.approx(-20.0)
    assert by_short["RHYME_C"]["days"] == 1
    assert sum(row["days"] for row in rows) == 2


def test_end_of_bar_series_keeps_one_mark_per_bar(zero_equity_costs):
    portfolio = BacktestPortfolio(10_000.0)
    reported: list[float] = []
    bar1 = pd.Series({"AAA": 10.0})
    portfolio.trade("AAA", "buy", 10.0, tx_cost=0.0, notional=1_000.0)
    record_end_of_bar_equity(portfolio, bar1, reported)
    bar2 = pd.Series({"AAA": 12.0})
    before_exit = portfolio.equity(bar2)
    portfolio.trade("AAA", "sell", 12.0, tx_cost=0.001, notional=1_200.0)
    record_end_of_bar_equity(portfolio, bar2, reported)
    assert len(reported) == 2
    assert reported[0] == pytest.approx(10_000.0)
    assert reported[1] == pytest.approx(portfolio.equity(bar2))
    assert reported[1] < before_exit
    assert np.isfinite(reported[1])


def _known_pnl(portfolio: BacktestPortfolio) -> float:
    return sum(t["realized_pnl"] for t in portfolio.closed_trades if t["realized_pnl"] is not None)


def _cash_basis_identity(portfolio: BacktestPortfolio, initial: float = 10_000.0) -> float:
    remaining = sum(float(v) for v in portfolio.entry_cost.values())
    return (portfolio.cash - initial) + remaining


def test_flat_trim_then_remainder_realizes_zero(zero_equity_costs):
    from modules.risk_management import trim_long_sleeves_to_cash_target

    portfolio = BacktestPortfolio(10_000.0)
    portfolio.trade("AAA", "buy", 10.0, tx_cost=0.0, notional=8_000.0)
    prices = pd.Series({"AAA": 10.0})
    assert trim_long_sleeves_to_cash_target(portfolio, prices, 0.50, 0.0) == 1
    assert portfolio.positions["AAA"] == pytest.approx(500.0)
    assert portfolio.entry_cost["AAA"] == pytest.approx(5_000.0)
    assert _known_pnl(portfolio) == pytest.approx(0.0)
    assert _known_pnl(portfolio) == pytest.approx(_cash_basis_identity(portfolio))
    portfolio.trade("AAA", "sell", 10.0, tx_cost=0.0, notional=1_000_000.0)
    assert portfolio.positions == {}
    assert "AAA" not in portfolio.entry_cost
    assert portfolio.cash == pytest.approx(10_000.0)
    assert _known_pnl(portfolio) == pytest.approx(0.0)


def test_profitable_and_losing_trims(zero_equity_costs):
    from modules.risk_management import trim_long_sleeves_to_cash_target

    winner = BacktestPortfolio(10_000.0)
    winner.trade("AAA", "buy", 10.0, tx_cost=0.0, notional=1_000.0)
    assert trim_long_sleeves_to_cash_target(winner, pd.Series({"AAA": 12.0}), 0.95, 0.0) == 1
    assert winner.closed_trades[0]["realized_pnl"] > 0
    assert _known_pnl(winner) == pytest.approx(_cash_basis_identity(winner))

    loser = BacktestPortfolio(10_000.0)
    loser.trade("AAA", "buy", 10.0, tx_cost=0.0, notional=1_000.0)
    assert trim_long_sleeves_to_cash_target(loser, pd.Series({"AAA": 8.0}), 0.95, 0.0) == 1
    assert loser.closed_trades[0]["realized_pnl"] < 0
    assert _known_pnl(loser) == pytest.approx(_cash_basis_identity(loser))


def test_full_trim_removes_cost_basis(zero_equity_costs):
    from modules.risk_management import trim_long_sleeves_to_cash_target

    portfolio = BacktestPortfolio(10_000.0)
    portfolio.trade("AAA", "buy", 10.0, tx_cost=0.0, notional=1_000.0)
    assert trim_long_sleeves_to_cash_target(portfolio, pd.Series({"AAA": 12.0}), 1.0, 0.0) == 1
    assert portfolio.positions == {}
    assert "AAA" not in portfolio.entry_cost
    assert portfolio.closed_trades[0]["realized_pnl"] == pytest.approx(200.0)
    assert portfolio.cash == pytest.approx(10_200.0)
    assert _known_pnl(portfolio) == pytest.approx(_cash_basis_identity(portfolio))


def test_trim_without_research_ledger_keeps_cash_and_quantity():
    from modules.risk_management import trim_long_sleeves_to_cash_target

    class Bare:
        def __init__(self):
            self.cash = 2_000.0
            self.positions = {"AAA": 800.0}

        def equity(self, prices):
            return self.cash + sum(
                float(qty) * float(prices[sym]) for sym, qty in self.positions.items()
            )

    bare = Bare()
    assert trim_long_sleeves_to_cash_target(bare, pd.Series({"AAA": 10.0}), 0.50, 0.0) == 1
    assert bare.cash == pytest.approx(5_000.0)
    assert bare.positions["AAA"] == pytest.approx(500.0)


def test_unknown_basis_is_excluded_from_trade_stats(zero_equity_costs):
    long_book = BacktestPortfolio(10_000.0)
    long_book.positions["AAA"] = 10.0
    before = long_book.cash
    long_book.trade("AAA", "sell", 10.0, tx_cost=0.0, notional=100.0)
    assert long_book.cash == pytest.approx(before + 100.0)
    assert long_book.closed_trades[0]["realized_pnl"] is None
    assert long_book.closed_trades[0]["return_pct"] is None
    assert long_book.closed_trades[0]["basis_known"] is False

    short_book = BacktestPortfolio(10_000.0)
    short_book.positions["AAA"] = -10.0
    before_short = short_book.cash
    short_book.trade("AAA", "buy", 10.0, tx_cost=0.0, notional=100.0)
    assert short_book.cash == pytest.approx(before_short - 100.0)
    assert short_book.positions == {}
    assert short_book.closed_trades[0]["realized_pnl"] is None
    assert short_book.closed_trades[0]["basis_known"] is False

    mixed = long_book.closed_trades + [
        {"realized_pnl": 20.0, "return_pct": 10.0},
        {"realized_pnl": -10.0, "return_pct": -5.0},
    ]
    perf = compute_performance_metrics(
        [100.0],
        initial_capital=100.0,
        total_orders=3,
        closed_trades=mixed,
    )
    assert perf["closed_trade_count"] == 2
    assert perf["closed_trade_unknown_count"] == 1
    assert perf["closed_trade_win_rate_pct"] == pytest.approx(50.0)
    assert perf["trade_profit_factor"] == pytest.approx(2.0)
    assert perf["avg_trade_return_pct"] == pytest.approx(2.5)
    assert perf["avg_trade_return_basis"] == "closed_trade"


def test_calmar_labels_match_across_runners():
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    for name in ("backtester.py", "_bt_exp_365_runner.py"):
        text = (root / name).read_text(encoding="utf-8")
        ratio_at = text.index('print(f"Calmar Ratio:')
        cagr_at = text.index('print(f"Calmar (CAGR):')
        ratio_line = text[ratio_at : text.index("\n", ratio_at)]
        cagr_line = text[cagr_at : text.index("\n", cagr_at)]
        assert "return_over_max_dd" in ratio_line
        assert "result.get('calmar'" in cagr_line or 'result.get("calmar"' in cagr_line
    runner = (root / "_bt_exp_365_runner.py").read_text(encoding="utf-8")
    assert "Avg trade return:" not in runner
    assert "Return per order:" in runner
    review = (root / "scripts" / "analysis" / "weekly_review.py").read_text(encoding="utf-8")
    assert r"Calmar Ratio:\s*" in review
