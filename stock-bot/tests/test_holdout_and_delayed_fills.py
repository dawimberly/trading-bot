"""Chronological holdout scoring and opt-in next-close fills.

Synthetic equity and orders only. No broker and no market data.
"""

from __future__ import annotations

import pandas as pd
import pytest

import config
from backtester import (
    BacktestExecutor,
    BacktestPortfolio,
    _queue_research_intent,
    flush_delayed_intents,
    record_end_of_bar_equity,
)
from modules.backtester_core import (
    RUN_OPTIONS,
    compute_performance_metrics,
    format_walk_forward_table,
    score_chronological_holdout,
    walk_forward_purged,
)
from modules.operating_layer import _execute_backtest_core_rebalance
from modules.pipeline_strategies import (
    _apply_when_filled,
    _count_if_filled,
    _defer_pair_cooldown,
    _execute_market_neutral_legs,
    _mark_nyse_entered_today,
    _nyse_mom_entries_by_day,
    execute_atomic_pair_entry,
)
from modules.stat_arb_sleeve import _close_position, _execute_entry, _open_book
from backtester import reject_terminal_delayed_intents
from _bt_exp_365_runner import _append_runner_holdout_mark


@pytest.fixture
def zero_equity_costs():
    saved = (
        RUN_OPTIONS.equity_slippage_bps,
        RUN_OPTIONS.equity_commission_bps,
        RUN_OPTIONS.fill_timing,
    )
    RUN_OPTIONS.equity_slippage_bps = 0.0
    RUN_OPTIONS.equity_commission_bps = 0.0
    RUN_OPTIONS.fill_timing = "same_close"
    yield
    (
        RUN_OPTIONS.equity_slippage_bps,
        RUN_OPTIONS.equity_commission_bps,
        RUN_OPTIONS.fill_timing,
    ) = saved


def _rising_then_falling_curve():
    # Bars 0-2 are the prefix. Bars 3-4 are the test window. Bar 5 is past it.
    bars = [0, 1, 2, 3, 4, 5]
    equity = [100.0, 150.0, 200.0, 180.0, 160.0, 500.0]
    return equity, bars


def test_training_gains_do_not_inflate_a_losing_test_window():
    equity, bars = _rising_then_falling_curve()
    holdout = score_chronological_holdout(
        equity, bars, test_begin=3, test_end=5, initial_capital=100.0
    )
    full = compute_performance_metrics(equity[:5], initial_capital=100.0, total_orders=0)
    assert holdout["start_equity"] == pytest.approx(200.0)
    assert holdout["final_equity"] == pytest.approx(160.0)
    assert holdout["total_return_pct"] == pytest.approx(-20.0)
    assert holdout["total_return_pct"] < 0
    assert full["total_return_pct"] > 0
    assert holdout["sharpe"] < 0
    assert holdout["max_drawdown_pct"] < 0


def test_test_window_boundaries_exclude_the_end_bar():
    equity, bars = _rising_then_falling_curve()
    holdout = score_chronological_holdout(
        equity, bars, test_begin=3, test_end=5, initial_capital=100.0
    )
    assert holdout["test_bar_first"] == 3
    assert holdout["test_bar_last"] == 4
    assert holdout["test_marks"] == 2
    assert holdout["final_equity"] != pytest.approx(500.0)


def test_preexisting_position_contributes_only_test_window_pnl():
    equity, bars = _rising_then_falling_curve()
    holdout = score_chronological_holdout(
        equity, bars, test_begin=3, test_end=5, initial_capital=100.0
    )
    assert holdout["start_equity"] > 100.0
    assert holdout["total_return_pct"] == pytest.approx(
        (160.0 / holdout["start_equity"] - 1.0) * 100.0
    )


def test_embargo_bars_are_not_passed_to_selection(zero_equity_costs):
    frame = pd.DataFrame({"close": range(200)})
    seen = []

    def select_fn(train):
        seen.append(set(int(i) for i in train.index))

    def run_fn(prefix, test_begin, test_end):
        equity = []
        level = 100.0
        for i in range(len(prefix)):
            level *= 1.01 if i < test_begin else 0.99
            equity.append(level)
        return {
            "equity_values": equity,
            "equity_bar_index": list(range(len(prefix))),
            "initial_capital": 100.0,
            "total_return_pct": 50.0,
            "sharpe": 2.0,
            "sortino": 2.0,
            "max_drawdown_pct": -1.0,
        }

    rows = walk_forward_purged(
        frame,
        min_history=30,
        n_folds=2,
        run_fn=run_fn,
        embargo_bars=5,
        select_fn=select_fn,
    )
    assert len(rows) == 2
    assert len(seen) == 2
    later = rows[1]
    embargo = set(range(later["train_end"], later["test_begin"]))
    assert later["train_end"] == later["test_begin"] - later["embargo_bars"]
    assert embargo
    assert embargo.isdisjoint(seen[1])
    assert all(i < later["test_begin"] for i in seen[1])
    assert "No parameter fit" in later["embargo_note"]
    assert later["scoring"] == "chronological_holdout"
    assert later["return_pct"] < 0
    assert later["legacy_return_pct"] == pytest.approx(50.0)
    assert later["test_bar_first"] == later["test_begin"]
    assert later["test_bar_last"] == later["test_end"] - 1
    table = format_walk_forward_table(rows, purged=True)
    assert "Chronological holdout" in table
    assert "nothing is fitted" in table
    assert "optimized" not in table.lower()


def test_signal_on_bar_i_does_not_fill_until_next_bar(zero_equity_costs):
    RUN_OPTIONS.fill_timing = "next_close"
    portfolio = BacktestPortfolio(10_000.0)
    signal_prices = pd.Series({"AAA": 10.0})
    executor = BacktestExecutor(portfolio, signal_prices)
    executor._bar_index = 4
    queued = executor.execute_order("AAA", "buy", notional=400.0)
    assert queued["queued"] is True
    assert queued["signal_bar"] == 4
    assert portfolio.positions == {}
    assert portfolio.cash == pytest.approx(10_000.0)
    assert executor.orders == []
    marks: list[float] = []
    record_end_of_bar_equity(portfolio, signal_prices, marks, 4, [])
    assert marks[-1] == pytest.approx(10_000.0)
    next_prices = pd.Series({"AAA": 11.0})
    fills = flush_delayed_intents(portfolio, next_prices)
    assert len(fills) == 1
    assert fills[0]["signal_bar"] == 4
    assert portfolio.positions["AAA"] > 0
    reported: list[float] = []
    bar_index: list[int] = []
    record_end_of_bar_equity(portfolio, next_prices, reported, 5, bar_index)
    assert bar_index == [5]
    assert reported[-1] == pytest.approx(portfolio.equity(next_prices))
    assert portfolio.cash < 10_000.0
    assert reported[-1] == pytest.approx(
        portfolio.cash + portfolio.positions["AAA"] * 11.0
    )


def test_final_bar_intents_do_not_execute(zero_equity_costs):
    portfolio = BacktestPortfolio(10_000.0)
    _queue_research_intent(
        portfolio,
        symbol="AAA",
        side="buy",
        notional=1_000.0,
        tx_cost=0.0,
        allow_naked_short=False,
        reduce_only=False,
        signal_bar=9,
        source="strategy",
    )
    assert portfolio.cash == pytest.approx(10_000.0)
    assert portfolio.positions == {}
    dropped = len(portfolio._pending_intents)
    portfolio._pending_intents = []
    assert dropped == 1
    assert portfolio.cash == pytest.approx(10_000.0)


def test_pending_orders_execute_in_sequence_and_a_round_trip_ends_flat(zero_equity_costs):
    portfolio = BacktestPortfolio(10_000.0)
    prices = pd.Series({"AAA": 10.0, "BBB": 20.0})
    _queue_research_intent(
        portfolio,
        symbol="BBB",
        side="buy",
        notional=400.0,
        tx_cost=0.0,
        allow_naked_short=False,
        reduce_only=False,
        signal_bar=1,
        source="strategy",
    )
    _queue_research_intent(
        portfolio,
        symbol="AAA",
        side="buy",
        notional=400.0,
        tx_cost=0.0,
        allow_naked_short=False,
        reduce_only=False,
        signal_bar=1,
        source="strategy",
    )
    _queue_research_intent(
        portfolio,
        symbol="AAA",
        side="sell",
        notional=400.0,
        tx_cost=0.0,
        allow_naked_short=False,
        reduce_only=False,
        signal_bar=1,
        source="strategy",
    )
    fills = flush_delayed_intents(portfolio, prices)
    assert [item["symbol"] for item in fills] == ["BBB", "AAA", "AAA"]
    assert [item["side"] for item in fills] == ["buy", "buy", "sell"]
    assert "AAA" not in portfolio.positions
    assert portfolio.positions["BBB"] == pytest.approx(20.0)
    assert portfolio.cash == pytest.approx(9_600.0)


def test_conflicting_sells_do_not_reverse_a_long(zero_equity_costs):
    portfolio = BacktestPortfolio(10_000.0)
    portfolio.trade("AAA", "buy", 10.0, tx_cost=0.0, notional=1_000.0)
    _queue_research_intent(
        portfolio,
        symbol="AAA",
        side="sell",
        notional=5_000.0,
        tx_cost=0.0,
        allow_naked_short=False,
        reduce_only=False,
        signal_bar=2,
        source="strategy",
    )
    _queue_research_intent(
        portfolio,
        symbol="AAA",
        side="sell",
        notional=5_000.0,
        tx_cost=0.0,
        allow_naked_short=False,
        reduce_only=False,
        signal_bar=2,
        source="strategy",
    )
    fills = flush_delayed_intents(portfolio, pd.Series({"AAA": 10.0}))
    assert len(fills) == 1
    assert portfolio.positions == {}
    assert portfolio.cash == pytest.approx(10_000.0)


def test_delayed_sell_does_not_open_a_short_unless_permitted(zero_equity_costs):
    blocked = BacktestPortfolio(10_000.0)
    _queue_research_intent(
        blocked,
        symbol="AAA",
        side="sell",
        notional=1_000.0,
        tx_cost=0.0,
        allow_naked_short=False,
        reduce_only=False,
        signal_bar=1,
        source="strategy",
    )
    assert flush_delayed_intents(blocked, pd.Series({"AAA": 10.0})) == []
    assert blocked.positions == {}
    assert blocked.cash == pytest.approx(10_000.0)

    permitted = BacktestPortfolio(10_000.0)
    _queue_research_intent(
        permitted,
        symbol="AAA",
        side="sell",
        notional=1_000.0,
        tx_cost=0.0,
        allow_naked_short=True,
        reduce_only=False,
        signal_bar=1,
        source="strategy",
    )
    fills = flush_delayed_intents(permitted, pd.Series({"AAA": 10.0}))
    assert len(fills) == 1
    assert permitted.positions["AAA"] < 0


def test_cash_positions_and_equity_reconcile_after_delayed_fills(zero_equity_costs):
    portfolio = BacktestPortfolio(10_000.0)
    flush_delayed_intents(
        portfolio,
        pd.Series({"AAA": 10.0}),
    )
    _queue_research_intent(
        portfolio,
        symbol="AAA",
        side="buy",
        notional=400.0,
        tx_cost=0.0,
        allow_naked_short=False,
        reduce_only=False,
        signal_bar=1,
        source="strategy",
    )
    flush_delayed_intents(portfolio, pd.Series({"AAA": 10.0}))
    marked = pd.Series({"AAA": 12.0})
    _queue_research_intent(
        portfolio,
        symbol="AAA",
        side="sell",
        notional=10_000.0,
        tx_cost=0.0,
        allow_naked_short=False,
        reduce_only=False,
        signal_bar=2,
        source="strategy",
    )
    flush_delayed_intents(portfolio, marked)
    position_value = sum(
        qty * float(marked[symbol]) for symbol, qty in portfolio.positions.items()
    )
    assert portfolio.equity(marked) == pytest.approx(portfolio.cash + position_value)
    assert portfolio.positions == {}
    realized = sum(trade["realized_pnl"] for trade in portfolio.closed_trades)
    assert portfolio.cash - 10_000.0 == pytest.approx(realized)
    assert realized == pytest.approx(80.0)


def test_slippage_and_fees_are_applied_once(zero_equity_costs):
    RUN_OPTIONS.equity_slippage_bps = 5.0
    delayed = BacktestPortfolio(10_000.0)
    direct = BacktestPortfolio(10_000.0)
    _queue_research_intent(
        delayed,
        symbol="AAA",
        side="buy",
        notional=400.0,
        tx_cost=0.001,
        allow_naked_short=False,
        reduce_only=False,
        signal_bar=1,
        source="strategy",
    )
    flush_delayed_intents(delayed, pd.Series({"AAA": 10.0}))
    direct.trade("AAA", "buy", 10.0, tx_cost=0.001, notional=400.0)
    assert delayed.execution_cost_usd > 0
    assert delayed.execution_cost_usd == pytest.approx(direct.execution_cost_usd)
    assert delayed.cash == pytest.approx(direct.cash)
    assert delayed.positions["AAA"] == pytest.approx(direct.positions["AAA"])


def test_same_close_still_fills_on_the_signal_bar(zero_equity_costs):
    RUN_OPTIONS.fill_timing = "same_close"
    portfolio = BacktestPortfolio(10_000.0)
    prices = pd.Series({"AAA": 10.0})
    executor = BacktestExecutor(portfolio, prices)
    executor._bar_index = 3
    order = executor.execute_order("AAA", "buy", notional=400.0)
    assert order["side"] == "buy"
    assert "queued" not in order
    assert portfolio.positions["AAA"] == pytest.approx(40.0)
    assert getattr(portfolio, "_pending_intents", []) == []
    again = BacktestPortfolio(10_000.0)
    again.trade("AAA", "buy", 10.0, tx_cost=0.0, notional=400.0)
    assert portfolio.cash == pytest.approx(again.cash)
    assert portfolio.positions["AAA"] == pytest.approx(again.positions["AAA"])
    assert executor.order_filled(order) is True
    assert _count_if_filled(executor, order) == 1


def test_default_core_rebalance_waits_until_the_next_bar(zero_equity_costs):
    prices = pd.Series({config.VTI_CORE_SYMBOL: 100.0})
    RUN_OPTIONS.fill_timing = "next_close"
    portfolio = BacktestPortfolio(10_000.0)
    assert _execute_backtest_core_rebalance(portfolio, prices, 0.5) is False
    assert portfolio.positions == {}
    assert portfolio.cash == pytest.approx(10_000.0)
    assert len(portfolio._pending_intents) == 1
    assert _execute_backtest_core_rebalance(portfolio, prices, 0.5) is False
    assert len(portfolio._pending_intents) == 1
    portfolio.cash = 1.0
    assert flush_delayed_intents(portfolio, prices, bar_index=2) == []
    assert portfolio.positions == {}

    funded = BacktestPortfolio(10_000.0)
    assert _execute_backtest_core_rebalance(funded, prices, 0.5) is False
    fills = flush_delayed_intents(funded, prices, bar_index=2)
    assert len(fills) == 1
    assert funded.positions[config.VTI_CORE_SYMBOL] > 0
    assert funded.cash < 10_000.0

    RUN_OPTIONS.fill_timing = "same_close"
    immediate = BacktestPortfolio(10_000.0)
    assert _execute_backtest_core_rebalance(immediate, prices, 0.5) is True
    assert immediate.positions[config.VTI_CORE_SYMBOL] > 0
    assert getattr(immediate, "_pending_intents", []) == []


def test_reduce_only_orders_cannot_reverse_exposure(zero_equity_costs):
    prices = pd.Series({"AAA": 10.0})
    long_book = BacktestPortfolio(10_000.0)
    long_book.trade("AAA", "buy", 10.0, tx_cost=0.0, notional=400.0)
    for _ in range(2):
        _queue_research_intent(
            long_book,
            symbol="AAA",
            side="sell",
            notional=5_000.0,
            tx_cost=0.0,
            allow_naked_short=False,
            reduce_only=True,
            signal_bar=1,
            source="strategy",
        )
    fills = flush_delayed_intents(long_book, prices)
    assert len(fills) == 1
    assert long_book.positions == {}

    short_book = BacktestPortfolio(10_000.0)
    short_book.positions["AAA"] = -10.0
    for _ in range(2):
        _queue_research_intent(
            short_book,
            symbol="AAA",
            side="buy",
            notional=5_000.0,
            tx_cost=0.0,
            allow_naked_short=False,
            reduce_only=True,
            signal_bar=1,
            source="strategy",
        )
    fills = flush_delayed_intents(short_book, prices)
    assert len(fills) == 1
    assert short_book.positions.get("AAA", 0.0) == pytest.approx(0.0)

    liquidated = BacktestPortfolio(10_000.0)
    liquidated.trade("AAA", "buy", 10.0, tx_cost=0.0, notional=400.0)
    _queue_research_intent(
        liquidated,
        symbol="AAA",
        side="sell",
        notional=5_000.0,
        tx_cost=0.0,
        allow_naked_short=True,
        reduce_only=True,
        signal_bar=2,
        source="strategy",
    )
    liquidated.trade("AAA", "sell", 10.0, tx_cost=0.0, notional=5_000.0)
    cash_after_liquidation = liquidated.cash
    assert flush_delayed_intents(liquidated, prices) == []
    assert liquidated.positions == {}
    assert liquidated.cash == pytest.approx(cash_after_liquidation)


def test_runner_end_of_bar_holdout_ignores_a_pre_test_gain(zero_equity_costs):
    portfolio = BacktestPortfolio(100.0)
    marks: list[float] = []
    bars: list[int] = []
    prices = pd.Series({"AAA": 10.0})
    _append_runner_holdout_mark(portfolio, prices, 0, marks, bars)
    portfolio.cash = 150.0
    _append_runner_holdout_mark(portfolio, prices, 1, marks, bars)
    _append_runner_holdout_mark(portfolio, prices, 2, marks, bars)
    _append_runner_holdout_mark(portfolio, prices, 3, marks, bars)
    holdout = score_chronological_holdout(
        marks, bars, test_begin=2, test_end=4, initial_capital=100.0
    )
    assert holdout["start_equity"] == pytest.approx(150.0)
    assert holdout["total_return_pct"] == pytest.approx(0.0)

    frame = pd.DataFrame({"close": range(200)})

    def run_fn(prefix, test_begin, _test_end):
        pre = []
        end = []
        level = 100.0
        for i in range(len(prefix)):
            if i == test_begin:
                level *= 1.5
            pre.append(level)
        level = 100.0
        for i in range(len(prefix)):
            if i == test_begin - 1:
                level *= 1.5
            end.append(level)
        return {
            "equity_values": pre,
            "equity_bar_index": list(range(len(prefix))),
            "holdout_equity_values": end,
            "holdout_equity_bar_index": list(range(len(prefix))),
            "initial_capital": 100.0,
            "total_return_pct": 50.0,
            "sharpe": 1.0,
            "sortino": 1.0,
            "max_drawdown_pct": 0.0,
        }

    rows = walk_forward_purged(
        frame, min_history=30, n_folds=2, run_fn=run_fn, embargo_bars=5
    )
    assert rows[0]["return_pct"] == pytest.approx(0.0)
    assert rows[0]["legacy_return_pct"] == pytest.approx(50.0)


class _FillLog:
    def __init__(self):
        self.events = []

    def on_fill(self, order, prices, portfolio, **meta):
        self.events.append({"side": order.get("side"), "meta": meta})


def test_delayed_fill_bookkeeping_uses_the_fill_bar(zero_equity_costs):
    saved = (
        config._paper_aggressive_ctx,
        config.PAPER_AGGRESSIVE_ENABLED,
        config.PAPER_REGIME_DD_RISK_ENABLED,
        config.PAPER_SMART_STOPS,
        config.PAPER_POSITION_MAX_HOLD_BARS,
    )
    config._paper_aggressive_ctx = True
    config.PAPER_AGGRESSIVE_ENABLED = True
    config.PAPER_REGIME_DD_RISK_ENABLED = True
    config.PAPER_SMART_STOPS = False
    config.PAPER_POSITION_MAX_HOLD_BARS = 2
    RUN_OPTIONS.fill_timing = "next_close"
    try:
        portfolio = BacktestPortfolio(10_000.0)
        prices = pd.Series({"AAA": 10.0})
        executor = BacktestExecutor(portfolio, prices)
        executor._bar_index = 4
        executor._attribution = _FillLog()
        queued = executor.execute_order(
            "AAA", "buy", notional=400.0, sleeve="NYSE", strategy="momentum"
        )
        assert executor.order_filled(queued) is False
        assert _count_if_filled(executor, queued) == 0
        assert executor._attribution.events == []
        assert portfolio.position_meta == {}
        fills = flush_delayed_intents(portfolio, prices, bar_index=5)
        assert len(fills) == 1
        assert executor.order_filled(fills[0]) is True
        assert _count_if_filled(executor, fills[0]) == 1
        assert executor._attribution.events[0]["meta"]["strategy"] == "momentum"
        meta = portfolio.position_meta["AAA"]
        assert meta["entry_bar"] == 5
        assert meta["entry_price"] == pytest.approx(10.0)
        assert meta["peak_price"] == pytest.approx(10.0)
        from modules.paper_risk_controls import run_paper_position_exits

        exit_executor = BacktestExecutor(portfolio, prices)
        run_paper_position_exits(portfolio, prices, 6, exit_executor)
        assert portfolio._pending_intents == []
        run_paper_position_exits(portfolio, prices, 7, exit_executor)
        assert len(portfolio._pending_intents) == 1
        assert portfolio._pending_intents[0]["side"] == "sell"
        assert portfolio.positions["AAA"] > 0
        assert portfolio.position_meta["AAA"]["entry_bar"] == 5
    finally:
        (
            config._paper_aggressive_ctx,
            config.PAPER_AGGRESSIVE_ENABLED,
            config.PAPER_REGIME_DD_RISK_ENABLED,
            config.PAPER_SMART_STOPS,
            config.PAPER_POSITION_MAX_HOLD_BARS,
        ) = saved


def test_queued_rejected_and_dropped_intents_are_not_fills(zero_equity_costs):
    RUN_OPTIONS.fill_timing = "next_close"
    portfolio = BacktestPortfolio(10_000.0)
    prices = pd.Series({"AAA": 10.0})
    executor = BacktestExecutor(portfolio, prices)
    executor._attribution = _FillLog()
    queued = executor.execute_order("AAA", "buy", notional=400.0, strategy="momentum")
    assert executor.order_filled(queued) is False
    assert _count_if_filled(executor, queued) == 0
    portfolio._pending_intents = []
    assert portfolio.positions == {}
    assert executor._attribution.events == []
    assert portfolio.closed_trades == []

    rejected = BacktestPortfolio(10_000.0)
    _queue_research_intent(
        rejected,
        symbol="AAA",
        side="sell",
        notional=400.0,
        tx_cost=0.0,
        allow_naked_short=False,
        reduce_only=True,
        signal_bar=3,
        source="strategy",
    )
    assert flush_delayed_intents(rejected, prices, bar_index=4) == []
    assert rejected.cash == pytest.approx(10_000.0)
    assert rejected.closed_trades == []
    assert executor.order_filled(None) is False


def _pair_book(cash=10_000.0):
    portfolio = BacktestPortfolio(cash)
    prices = pd.Series({"AAA": 10.0, "BBB": 10.0, "CCC": 10.0})
    executor = BacktestExecutor(portfolio, prices)
    return portfolio, prices, executor


def test_next_close_pair_fills_both_legs_or_neither(zero_equity_costs, monkeypatch):
    RUN_OPTIONS.fill_timing = "next_close"
    monkeypatch.setattr(config, "effective_stat_arb_enabled", lambda: True)
    monkeypatch.setattr(config, "effective_equity_pairs_enabled", lambda: False)
    portfolio, prices, executor = _pair_book()
    cooldown = {}
    ok, long_n, short_n = execute_atomic_pair_entry(
        executor, "AAA", "BBB", 400.0, pair_key="AAA/BBB", strategy="stat_arb"
    )
    assert ok is False
    assert long_n is None and short_n is None
    assert portfolio.positions == {}
    assert portfolio.cash == pytest.approx(10_000.0)
    _defer_pair_cooldown(executor, cooldown, "AAA/BBB", 4)
    assert cooldown == {}

    fills = flush_delayed_intents(portfolio, prices, bar_index=5)
    assert [row["symbol"] for row in fills] == ["AAA", "BBB"]
    assert fills[0]["notional"] == pytest.approx(400.0)
    assert fills[1]["notional"] == pytest.approx(400.0)
    assert portfolio.positions["AAA"] == pytest.approx(40.0)
    assert portfolio.positions["BBB"] == pytest.approx(-40.0)
    assert portfolio.cash == pytest.approx(10_000.0)
    assert cooldown["AAA/BBB"] == 4
    assert flush_delayed_intents(portfolio, prices, bar_index=6) == []
    assert cooldown["AAA/BBB"] == 4
    assert getattr(portfolio, "pair_rejections", []) == []


def test_next_close_pair_rejects_when_either_leg_cannot_fill(zero_equity_costs, monkeypatch):
    RUN_OPTIONS.fill_timing = "next_close"
    monkeypatch.setattr(config, "effective_stat_arb_enabled", lambda: True)
    portfolio, prices, executor = _pair_book()
    execute_atomic_pair_entry(
        executor, "AAA", "BBB", 400.0, pair_key="AAA/BBB", strategy="stat_arb"
    )
    missing = prices.drop(labels=["BBB"])
    assert flush_delayed_intents(portfolio, missing, bar_index=5) == []
    assert portfolio.positions == {}
    assert portfolio.cash == pytest.approx(10_000.0)
    assert portfolio.pair_rejections[-1]["reason"] == "missing_price"
    assert portfolio.pair_rejections[-1]["symbols"] == ["AAA", "BBB"]


def test_next_close_pair_rejects_on_insufficient_cash(zero_equity_costs, monkeypatch):
    RUN_OPTIONS.fill_timing = "next_close"
    monkeypatch.setattr(config, "effective_stat_arb_enabled", lambda: True)
    portfolio, prices, executor = _pair_book()
    execute_atomic_pair_entry(
        executor, "AAA", "BBB", 400.0, pair_key="AAA/BBB", strategy="stat_arb"
    )
    portfolio.positions["CCC"] = 1_000.0
    portfolio.cash = 50.0
    assert flush_delayed_intents(portfolio, prices, bar_index=5) == []
    assert portfolio.positions.get("AAA", 0.0) == 0.0
    assert portfolio.positions.get("BBB", 0.0) == 0.0
    assert portfolio.positions["CCC"] == pytest.approx(1_000.0)
    assert portfolio.cash == pytest.approx(50.0)
    assert portfolio.pair_rejections[-1]["reason"] == "insufficient_cash"


def test_next_close_pair_rejects_when_short_permission_is_denied(zero_equity_costs, monkeypatch):
    RUN_OPTIONS.fill_timing = "next_close"
    monkeypatch.setattr(config, "effective_stat_arb_enabled", lambda: False)
    monkeypatch.setattr(config, "effective_equity_pairs_enabled", lambda: False)
    portfolio, prices, executor = _pair_book()
    execute_atomic_pair_entry(
        executor, "AAA", "BBB", 400.0, pair_key="AAA/BBB", strategy="stat_arb"
    )
    _queue_research_intent(
        portfolio,
        symbol="CCC",
        side="buy",
        notional=200.0,
        tx_cost=0.0,
        allow_naked_short=False,
        reduce_only=False,
        signal_bar=4,
        source="strategy",
    )
    fills = flush_delayed_intents(portfolio, prices, bar_index=5)
    assert [row["symbol"] for row in fills] == ["CCC"]
    assert portfolio.positions.get("AAA", 0.0) == 0.0
    assert portfolio.positions.get("BBB", 0.0) == 0.0
    assert portfolio.positions["CCC"] == pytest.approx(20.0)
    assert portfolio.pair_rejections[-1]["reason"] == "short_permission_denied"
    assert portfolio.cash == pytest.approx(9_800.0)


def test_terminal_bar_cancels_a_queued_pair(zero_equity_costs, monkeypatch):
    RUN_OPTIONS.fill_timing = "next_close"
    monkeypatch.setattr(config, "effective_stat_arb_enabled", lambda: True)
    portfolio, _prices, executor = _pair_book()
    stamps = {"n": 0}
    execute_atomic_pair_entry(
        executor, "AAA", "BBB", 400.0, pair_key="AAA/BBB", strategy="stat_arb"
    )
    _defer_pair_cooldown(executor, stamps, "AAA/BBB", 9)
    dropped = reject_terminal_delayed_intents(portfolio)
    assert dropped == 2
    assert portfolio.positions == {}
    assert portfolio.cash == pytest.approx(10_000.0)
    assert portfolio.pair_rejections[-1]["reason"] == "terminal_bar"
    assert stamps == {"n": 0}
    assert getattr(portfolio, "_pair_fill_callbacks", {}) == {}


def test_market_neutral_book_opens_once_after_both_legs(zero_equity_costs, monkeypatch):
    RUN_OPTIONS.fill_timing = "next_close"
    monkeypatch.setattr(config, "effective_stat_arb_enabled", lambda: True)
    portfolio, prices, executor = _pair_book()

    class _Book:
        def __init__(self):
            self.rows = []

        def add_position(self, key, z, qty):
            self.rows.append((key, z, qty))

    book = _Book()
    intent = {
        "long_symbol": "AAA",
        "short_symbol": "BBB",
        "z_score": 2.0,
        "pair_key": "AAA/BBB",
        "notional": 800.0,
    }
    count, ok = _execute_market_neutral_legs(
        executor, intent, portfolio_manager=book
    )
    assert count == 0 and ok is False
    assert book.rows == []
    assert portfolio.positions == {}
    cooldown = {}
    _defer_pair_cooldown(executor, cooldown, "AAA/BBB", 4)
    flush_delayed_intents(portfolio, prices, bar_index=5)
    assert book.rows == [("AAA/BBB", 2.0, 0)]
    assert cooldown["AAA/BBB"] == 4
    assert portfolio.positions["AAA"] == pytest.approx(40.0)
    assert portfolio.positions["BBB"] == pytest.approx(-40.0)
    flush_delayed_intents(portfolio, prices, bar_index=6)
    assert book.rows == [("AAA/BBB", 2.0, 0)]

    denied, denied_prices, denied_executor = _pair_book()
    monkeypatch.setattr(config, "effective_stat_arb_enabled", lambda: False)
    monkeypatch.setattr(config, "effective_equity_pairs_enabled", lambda: False)
    denied_book = _Book()
    _execute_market_neutral_legs(denied_executor, intent, portfolio_manager=denied_book)
    assert flush_delayed_intents(denied, denied_prices, bar_index=5) == []
    assert denied_book.rows == []
    assert denied.positions == {}


def test_same_close_pair_still_fills_immediately(zero_equity_costs, monkeypatch):
    RUN_OPTIONS.fill_timing = "same_close"
    monkeypatch.setattr(config, "effective_stat_arb_enabled", lambda: True)
    portfolio, _prices, executor = _pair_book()
    ok, long_n, short_n = execute_atomic_pair_entry(
        executor, "AAA", "BBB", 400.0, pair_key="AAA/BBB", strategy="stat_arb"
    )
    assert ok is True
    assert long_n == pytest.approx(400.0)
    assert short_n == pytest.approx(400.0)
    assert portfolio.positions["AAA"] == pytest.approx(40.0)
    assert portfolio.positions["BBB"] == pytest.approx(-40.0)
    assert getattr(portfolio, "_pending_intents", []) == []


def test_fill_bookkeeping_updates_once_after_execution(zero_equity_costs):
    RUN_OPTIONS.fill_timing = "next_close"
    portfolio = BacktestPortfolio(10_000.0)
    prices = pd.Series({"AAA": 10.0, "BBB": 10.0})
    executor = BacktestExecutor(portfolio, prices)
    order = executor.execute_order("AAA", "buy", notional=400.0, reason="AAA|MOM", sleeve="NYSE")
    cooldown = {}
    stamps = {"n": 0}

    def _book():
        stamps["n"] += 1
        _mark_nyse_entered_today("AAA", now=datetime_now())
        cooldown["AAA|MOM"] = 5

    assert _apply_when_filled(executor, order, _book) is False
    assert stamps["n"] == 0
    assert cooldown == {}
    entered = _nyse_entered("AAA")
    assert "AAA" not in entered

    fills = flush_delayed_intents(portfolio, prices, bar_index=5)
    assert len(fills) == 1
    assert stamps["n"] == 1
    assert cooldown["AAA|MOM"] == 5
    assert "AAA" in _nyse_entered("AAA")
    assert flush_delayed_intents(portfolio, prices, bar_index=6) == []
    assert stamps["n"] == 1

    rejected = executor.execute_order("BBB", "sell", notional=100.0, reduce_only=True)
    rejected_stamps = {"n": 0}
    assert _apply_when_filled(
        executor, rejected, lambda: rejected_stamps.__setitem__("n", 1)
    ) is False
    assert flush_delayed_intents(portfolio, prices, bar_index=6) == []
    assert rejected_stamps["n"] == 0

    dropped = executor.execute_order("AAA", "buy", notional=100.0)
    dropped_stamps = {"n": 0}
    _apply_when_filled(executor, dropped, lambda: dropped_stamps.__setitem__("n", 1))
    reject_terminal_delayed_intents(portfolio)
    assert dropped_stamps["n"] == 0

    RUN_OPTIONS.fill_timing = "same_close"
    immediate = BacktestPortfolio(10_000.0)
    immediate_executor = BacktestExecutor(immediate, prices)
    filled = immediate_executor.execute_order("AAA", "buy", notional=400.0)
    immediate_stamps = {"n": 0}
    assert _apply_when_filled(
        immediate_executor,
        filled,
        lambda: immediate_stamps.__setitem__("n", immediate_stamps["n"] + 1),
    )
    assert immediate_stamps["n"] == 1
    assert flush_delayed_intents(immediate, prices, bar_index=2) == []
    assert immediate_stamps["n"] == 1
    _nyse_mom_entries_by_day.clear()


def _held_pair(portfolio, executor):
    portfolio.positions["AAA"] = 40.0
    portfolio.positions["BBB"] = -40.0
    portfolio.entry_cost["AAA"] = 400.0
    portfolio._short_open_qty["BBB"] = 40.0
    portfolio._short_open_proceeds["BBB"] = 400.0
    _open_book(executor)["AAA/BBB"] = {
        "long_symbol": "AAA",
        "short_symbol": "BBB",
        "leg_notional": 400.0,
        "entry_bar": 4,
        "beta": 1.0,
        "entry_z": 2.0,
    }


def test_delayed_pair_exit_closes_both_legs_and_keeps_the_book_until_flush(
    zero_equity_costs, monkeypatch
):
    RUN_OPTIONS.fill_timing = "next_close"
    monkeypatch.setattr(config, "effective_stat_arb_enabled", lambda: True)
    portfolio, prices, executor = _pair_book()
    _held_pair(portfolio, executor)
    position = _open_book(executor)["AAA/BBB"]
    assert _close_position(executor, "AAA/BBB", position, now=5, force=False) == 0
    assert "AAA/BBB" in _open_book(executor)
    assert portfolio.positions["AAA"] == pytest.approx(40.0)
    assert portfolio.positions["BBB"] == pytest.approx(-40.0)
    pending = [(row["symbol"], row["side"]) for row in portfolio._pending_intents]
    assert pending == [("AAA", "sell"), ("BBB", "buy")]
    assert _close_position(executor, "AAA/BBB", position, now=5, force=True) == 0
    assert [(row["symbol"], row["side"]) for row in portfolio._pending_intents] == pending
    flush_delayed_intents(portfolio, prices, bar_index=6)
    assert portfolio.positions.get("AAA", 0.0) == pytest.approx(0.0)
    assert portfolio.positions.get("BBB", 0.0) == pytest.approx(0.0)
    assert "AAA/BBB" not in _open_book(executor)


def test_forced_delayed_pair_exit_queues_one_order_per_leg(zero_equity_costs, monkeypatch):
    RUN_OPTIONS.fill_timing = "next_close"
    monkeypatch.setattr(config, "effective_stat_arb_enabled", lambda: True)
    portfolio, _prices, executor = _pair_book()
    _held_pair(portfolio, executor)
    position = _open_book(executor)["AAA/BBB"]
    assert _close_position(executor, "AAA/BBB", position, now=5, force=True) == 0
    pending = [(row["symbol"], row["side"]) for row in portfolio._pending_intents]
    assert pending == [("AAA", "sell"), ("BBB", "buy")]
    assert "AAA/BBB" in _open_book(executor)


def test_rejected_pair_exit_leaves_positions_and_book(zero_equity_costs, monkeypatch):
    RUN_OPTIONS.fill_timing = "next_close"
    monkeypatch.setattr(config, "effective_stat_arb_enabled", lambda: True)
    portfolio, prices, executor = _pair_book()
    _held_pair(portfolio, executor)
    position = _open_book(executor)["AAA/BBB"]
    _close_position(executor, "AAA/BBB", position, now=5, force=False)
    missing = prices.drop(labels=["BBB"])
    assert flush_delayed_intents(portfolio, missing, bar_index=6) == []
    assert portfolio.positions["AAA"] == pytest.approx(40.0)
    assert portfolio.positions["BBB"] == pytest.approx(-40.0)
    assert _open_book(executor)["AAA/BBB"]["leg_notional"] == pytest.approx(400.0)
    assert portfolio.closed_trades == []


def test_trim_between_pair_exit_signal_and_fill_cannot_reverse(
    zero_equity_costs, monkeypatch
):
    RUN_OPTIONS.fill_timing = "next_close"
    monkeypatch.setattr(config, "effective_stat_arb_enabled", lambda: True)
    portfolio, prices, executor = _pair_book()
    _held_pair(portfolio, executor)
    position = _open_book(executor)["AAA/BBB"]
    _close_position(executor, "AAA/BBB", position, now=5, force=False)
    portfolio.positions.pop("AAA")
    portfolio.entry_cost.pop("AAA", None)
    portfolio.cash += 400.0
    flush_delayed_intents(portfolio, prices, bar_index=6)
    assert portfolio.positions.get("AAA", 0.0) >= 0
    assert portfolio.positions.get("BBB", 0.0) <= 0


def test_terminal_pair_exit_keeps_the_book_and_positions(zero_equity_costs, monkeypatch):
    RUN_OPTIONS.fill_timing = "next_close"
    monkeypatch.setattr(config, "effective_stat_arb_enabled", lambda: True)
    portfolio, _prices, executor = _pair_book()
    _held_pair(portfolio, executor)
    position = _open_book(executor)["AAA/BBB"]
    _close_position(executor, "AAA/BBB", position, now=5, force=False)
    reject_terminal_delayed_intents(portfolio)
    assert portfolio.positions["AAA"] == pytest.approx(40.0)
    assert portfolio.positions["BBB"] == pytest.approx(-40.0)
    assert "AAA/BBB" in _open_book(executor)
    assert portfolio.closed_trades == []


def test_same_close_pair_exit_still_flattens_immediately(zero_equity_costs, monkeypatch):
    RUN_OPTIONS.fill_timing = "same_close"
    monkeypatch.setattr(config, "effective_stat_arb_enabled", lambda: True)
    portfolio, _prices, executor = _pair_book()
    _held_pair(portfolio, executor)
    position = _open_book(executor)["AAA/BBB"]
    assert _close_position(executor, "AAA/BBB", position, now=5, force=False) == 1
    assert portfolio.positions.get("AAA", 0.0) == pytest.approx(0.0)
    assert portfolio.positions.get("BBB", 0.0) == pytest.approx(0.0)
    assert "AAA/BBB" not in _open_book(executor)
    assert getattr(portfolio, "_pending_intents", []) == []


class _Attribution:
    def __init__(self):
        self.events = []

    def on_fill(self, order, prices, portfolio, **meta):
        self.events.append((order.get("symbol"), order.get("side")))


def test_failed_second_leg_restores_the_portfolio_without_an_unwind(
    zero_equity_costs, monkeypatch
):
    RUN_OPTIONS.fill_timing = "next_close"
    monkeypatch.setattr(config, "effective_stat_arb_enabled", lambda: True)
    portfolio, prices, executor = _pair_book()
    portfolio.position_meta = {"ZZZ": {"entry_bar": 1}}
    portfolio._research_attribution = _Attribution()
    executor._attribution = portfolio._research_attribution
    execute_atomic_pair_entry(
        executor, "AAA", "BBB", 400.0, pair_key="AAA/BBB", strategy="stat_arb"
    )
    portfolio._pending_intents[0]["on_fill"].append(lambda: stamps.__setitem__("n", 1))
    stamps = {"n": 0}
    _queue_research_intent(
        portfolio,
        symbol="CCC",
        side="buy",
        notional=200.0,
        tx_cost=0.0,
        allow_naked_short=False,
        reduce_only=False,
        signal_bar=4,
        source="strategy",
    )
    original = portfolio.trade
    calls = {"n": 0}

    def fail_second(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            return None
        return original(*args, **kwargs)

    portfolio.trade = fail_second
    fills = flush_delayed_intents(portfolio, prices, bar_index=5)
    assert [row["symbol"] for row in fills] == ["CCC"]
    assert portfolio.positions.get("AAA", 0.0) == pytest.approx(0.0)
    assert portfolio.positions.get("BBB", 0.0) == pytest.approx(0.0)
    assert portfolio.positions["CCC"] == pytest.approx(20.0)
    assert portfolio.cash == pytest.approx(9_800.0)
    assert portfolio.closed_trades == []
    assert portfolio.position_meta == {"ZZZ": {"entry_bar": 1}}
    assert portfolio._research_attribution.events == [("CCC", "buy")]
    assert stamps["n"] == 0
    assert portfolio.pair_rejections[-1]["reason"] == "execution_failed"


def test_pair_execution_exception_is_not_swallowed(zero_equity_costs, monkeypatch):
    RUN_OPTIONS.fill_timing = "next_close"
    monkeypatch.setattr(config, "effective_stat_arb_enabled", lambda: True)
    portfolio, prices, executor = _pair_book()
    execute_atomic_pair_entry(
        executor, "AAA", "BBB", 400.0, pair_key="AAA/BBB", strategy="stat_arb"
    )
    original = portfolio.trade
    calls = {"n": 0}

    def explode(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("pair leg failed")
        return original(*args, **kwargs)

    portfolio.trade = explode
    with pytest.raises(RuntimeError, match="pair leg failed"):
        flush_delayed_intents(portfolio, prices, bar_index=5)
    assert portfolio.positions == {}
    assert portfolio.cash == pytest.approx(10_000.0)
    assert portfolio.closed_trades == []


def test_stat_arb_fill_state_is_visible_to_the_next_executor(zero_equity_costs, monkeypatch):
    RUN_OPTIONS.fill_timing = "next_close"
    monkeypatch.setattr(config, "effective_stat_arb_enabled", lambda: True)
    portfolio, prices, executor = _pair_book()

    class _Manager:
        def __init__(self):
            self.rows = []

        def add_position(self, key, z, qty):
            self.rows.append((key, z, qty))

    manager = _Manager()
    cooldown = {}
    import modules.stat_arb_sleeve as stat_arb

    monkeypatch.setattr(stat_arb, "pair_leg_notional", lambda *args, **kwargs: (400.0, 400.0))
    intent = {
        "long_symbol": "AAA",
        "short_symbol": "BBB",
        "z_score": 2.0,
        "pair_key": "AAA/BBB",
        "notional": 800.0,
        "beta": 1.0,
        "entry_bar": 4,
    }
    assert _execute_entry(
        executor,
        intent,
        regime="bull",
        now=4,
        pair_cooldown=cooldown,
        portfolio_manager=manager,
    ) == 0
    assert _open_book(executor) == {}
    assert cooldown == {}
    assert manager.rows == []
    flush_delayed_intents(portfolio, prices, bar_index=6)
    nxt = BacktestExecutor(portfolio, prices)
    assert _open_book(nxt)["AAA/BBB"]["entry_bar"] == 6
    assert cooldown["AAA/BBB"] == 6
    assert manager.rows == [("AAA/BBB", 2.0, 0)]
    assert nxt._pair_symbols == {"AAA", "BBB"}


def test_rejected_stat_arb_entry_creates_no_owner_state(zero_equity_costs, monkeypatch):
    RUN_OPTIONS.fill_timing = "next_close"
    monkeypatch.setattr(config, "effective_stat_arb_enabled", lambda: False)
    monkeypatch.setattr(config, "effective_equity_pairs_enabled", lambda: False)
    portfolio, prices, executor = _pair_book()

    class _Manager:
        def __init__(self):
            self.rows = []

        def add_position(self, *args):
            self.rows.append(args)

    manager = _Manager()
    cooldown = {}
    import modules.stat_arb_sleeve as stat_arb

    monkeypatch.setattr(stat_arb, "pair_leg_notional", lambda *args, **kwargs: (400.0, 400.0))
    intent = {
        "long_symbol": "AAA",
        "short_symbol": "BBB",
        "z_score": 2.0,
        "pair_key": "AAA/BBB",
        "notional": 800.0,
        "beta": 1.0,
    }
    _execute_entry(
        executor,
        intent,
        now=4,
        pair_cooldown=cooldown,
        portfolio_manager=manager,
    )
    flush_delayed_intents(portfolio, prices, bar_index=5)
    assert _open_book(executor) == {}
    assert cooldown == {}
    assert manager.rows == []
    assert portfolio.positions == {}


def test_partial_reduction_updates_notional_only_after_fill(zero_equity_costs, monkeypatch):
    RUN_OPTIONS.fill_timing = "next_close"
    monkeypatch.setattr(config, "effective_exit_optimization_enabled", lambda: True)
    monkeypatch.setattr(
        "modules.exit_management.partial_exit_fraction", lambda: 0.5
    )
    portfolio, prices, executor = _pair_book()
    _held_pair(portfolio, executor)
    position = _open_book(executor)["AAA/BBB"]
    position["exit_reason"] = "partial_1r"
    assert _close_position(executor, "AAA/BBB", position, now=5, force=False) == 0
    assert position["leg_notional"] == pytest.approx(400.0)
    flush_delayed_intents(portfolio, prices, bar_index=6)
    assert position["leg_notional"] == pytest.approx(200.0)
    assert portfolio.positions["AAA"] == pytest.approx(20.0)
    assert portfolio.positions["BBB"] == pytest.approx(-20.0)

    denied, denied_prices, denied_executor = _pair_book()
    _held_pair(denied, denied_executor)
    denied_position = _open_book(denied_executor)["AAA/BBB"]
    denied_position["exit_reason"] = "partial_1r"
    _close_position(denied_executor, "AAA/BBB", denied_position, now=5, force=False)
    assert flush_delayed_intents(denied, denied_prices.drop(labels=["BBB"]), bar_index=6) == []
    assert denied_position["leg_notional"] == pytest.approx(400.0)
    assert denied.positions["AAA"] == pytest.approx(40.0)
    assert denied.positions["BBB"] == pytest.approx(-40.0)


def test_profit_and_ipo_counters_survive_the_next_executor(zero_equity_costs, monkeypatch):
    RUN_OPTIONS.fill_timing = "next_close"
    monkeypatch.setattr("modules.profit_target.profit_target_enabled", lambda: True)
    monkeypatch.setattr("modules.profit_target.is_eligible_symbol", lambda *args, **kwargs: True)
    monkeypatch.setattr("modules.dynamic_universe.ipo_safety_enabled", lambda: True)
    monkeypatch.setattr("modules.dynamic_universe.is_ipo_symbol", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        "modules.dynamic_universe.ipo_trim_reduce_notional", lambda *args, **kwargs: 100.0
    )
    portfolio = BacktestPortfolio(10_000.0)
    prices = pd.Series({"AAA": 11.0, "BBB": 10.0})
    executor = BacktestExecutor(portfolio, prices)
    portfolio.positions["AAA"] = 10.0
    portfolio.entry_cost["AAA"] = 100.0
    executor._profit_target_state["AAA"] = {"armed": True, "hwm": 13.0, "entry": 10.0}

    class _Journal:
        def __init__(self):
            self.rows = []

        def log_exit(self, *args, **kwargs):
            self.rows.append(args)

    journal = _Journal()
    from modules.profit_target import run_profit_target_exits

    assert run_profit_target_exits(executor, now=4, bar_idx=4, journal=journal) == 0
    assert executor.profit_target_stats["exits"] == 0
    assert journal.rows == []
    from modules.pipeline_strategies import run_ipo_safety_trims

    portfolio.positions["BBB"] = 20.0
    portfolio.entry_cost["BBB"] = 200.0
    assert run_ipo_safety_trims(None, executor, bar_idx=4) == 0
    assert executor.ipo_stats["trims"] == 0
    flush_delayed_intents(portfolio, prices, bar_index=5)
    nxt = BacktestExecutor(portfolio, prices)
    assert nxt.profit_target_stats["exits"] == 1
    assert nxt._profit_exit_cooldown["AAA"]["bar_idx"] == 5
    assert journal.rows
    assert nxt.ipo_stats["trims"] == 1
    assert nxt.ipo_stats["trim_notional"] == pytest.approx(100.0)
    flush_delayed_intents(portfolio, prices, bar_index=6)
    assert nxt.profit_target_stats["exits"] == 1

    rejected = BacktestPortfolio(10_000.0)
    rejected_prices = pd.Series({"AAA": 11.0})
    rejected_executor = BacktestExecutor(rejected, rejected_prices)
    rejected.positions["AAA"] = 10.0
    rejected.entry_cost["AAA"] = 100.0
    rejected_executor._profit_target_state["AAA"] = {"armed": True, "hwm": 13.0, "entry": 10.0}
    rejected_journal = _Journal()
    run_profit_target_exits(rejected_executor, now=4, bar_idx=4, journal=rejected_journal)
    rejected.positions.pop("AAA")
    flush_delayed_intents(rejected, rejected_prices, bar_index=5)
    assert rejected_executor.profit_target_stats["exits"] == 0
    assert rejected_journal.rows == []
    assert rejected_executor._profit_exit_cooldown == {}

    terminal = BacktestPortfolio(10_000.0)
    terminal_executor = BacktestExecutor(terminal, rejected_prices)
    terminal.positions["AAA"] = 10.0
    terminal.entry_cost["AAA"] = 100.0
    terminal_executor._profit_target_state["AAA"] = {"armed": True, "hwm": 13.0, "entry": 10.0}
    terminal_journal = _Journal()
    run_profit_target_exits(terminal_executor, now=4, bar_idx=4, journal=terminal_journal)
    reject_terminal_delayed_intents(terminal)
    assert terminal_executor.profit_target_stats["exits"] == 0
    assert terminal_journal.rows == []
    assert terminal_executor._profit_exit_cooldown == {}


def test_stat_arb_sleeve_gross_rejects_the_whole_pair(zero_equity_costs, monkeypatch):
    RUN_OPTIONS.fill_timing = "next_close"
    monkeypatch.setattr(config, "effective_stat_arb_enabled", lambda: True)
    monkeypatch.setattr(config, "effective_stat_arb_sleeve_cap_enabled", lambda: True)
    monkeypatch.setattr(config, "effective_stat_arb_cap", lambda: 0.05)
    portfolio, prices, executor = _pair_book()
    execute_atomic_pair_entry(
        executor, "AAA", "BBB", 400.0, pair_key="AAA/BBB", strategy="stat_arb"
    )
    assert flush_delayed_intents(portfolio, prices, bar_index=5) == []
    assert portfolio.positions == {}
    assert portfolio.cash == pytest.approx(10_000.0)
    assert portfolio.pair_rejections[-1]["reason"] == "sleeve_gross"


def test_exact_close_with_slippage_leaves_no_residual_shares(zero_equity_costs, monkeypatch):
    RUN_OPTIONS.fill_timing = "next_close"
    RUN_OPTIONS.equity_slippage_bps = 5.0
    monkeypatch.setattr(config, "effective_stat_arb_enabled", lambda: True)
    portfolio, prices, executor = _pair_book()
    portfolio.positions["BBB"] = -40.0
    portfolio._short_open_qty["BBB"] = 40.0
    portfolio._short_open_proceeds["BBB"] = 400.0
    executor.execute_full_exit("BBB")
    flush_delayed_intents(portfolio, prices, bar_index=6)
    assert "BBB" not in portfolio.positions
    assert portfolio.cash == pytest.approx(10_000.0 - 40.0 * 10.005)
    assert portfolio.execution_cost_usd == pytest.approx(0.2)

    portfolio, prices, executor = _pair_book()
    portfolio.positions["AAA"] = 40.0
    portfolio.entry_cost["AAA"] = 400.0
    executor.execute_full_exit("AAA")
    flush_delayed_intents(portfolio, prices, bar_index=6)
    assert "AAA" not in portfolio.positions
    assert portfolio.cash == pytest.approx(10_000.0 + 40.0 * 9.995)
    assert portfolio.execution_cost_usd == pytest.approx(0.2)


def test_slipped_pair_exit_removes_the_book_only_when_both_legs_are_flat(
    zero_equity_costs, monkeypatch
):
    RUN_OPTIONS.fill_timing = "next_close"
    RUN_OPTIONS.equity_slippage_bps = 5.0
    monkeypatch.setattr(config, "effective_stat_arb_enabled", lambda: True)
    portfolio, prices, executor = _pair_book()
    _held_pair(portfolio, executor)
    position = _open_book(executor)["AAA/BBB"]
    assert _close_position(executor, "AAA/BBB", position, now=5, force=False) == 0
    assert "AAA/BBB" in _open_book(executor)
    assert portfolio._pending_intents[0]["close_qty"] == pytest.approx(40.0)
    assert portfolio._pending_intents[1]["close_qty"] == pytest.approx(40.0)
    flush_delayed_intents(portfolio, prices, bar_index=6)
    assert "AAA" not in portfolio.positions
    assert "BBB" not in portfolio.positions
    assert "AAA/BBB" not in _open_book(executor)
    assert portfolio.cash == pytest.approx(10_000.0 - 0.4)
    assert portfolio.execution_cost_usd == pytest.approx(0.4)


def test_notional_orders_still_convert_at_the_slipped_price(zero_equity_costs, monkeypatch):
    RUN_OPTIONS.fill_timing = "next_close"
    RUN_OPTIONS.equity_slippage_bps = 5.0
    monkeypatch.setattr(config, "effective_stat_arb_enabled", lambda: True)
    portfolio, prices, executor = _pair_book()
    executor.execute_order("AAA", "buy", notional=400.0)
    flush_delayed_intents(portfolio, prices, bar_index=6)
    assert portfolio.positions["AAA"] == pytest.approx(400.0 / 10.005)


def test_partial_reduction_keeps_remaining_shares_under_slippage(
    zero_equity_costs, monkeypatch
):
    RUN_OPTIONS.fill_timing = "next_close"
    RUN_OPTIONS.equity_slippage_bps = 5.0
    monkeypatch.setattr(config, "effective_exit_optimization_enabled", lambda: True)
    monkeypatch.setattr("modules.exit_management.partial_exit_fraction", lambda: 0.5)
    portfolio, prices, executor = _pair_book()
    _held_pair(portfolio, executor)
    position = _open_book(executor)["AAA/BBB"]
    position["exit_reason"] = "partial_1r"
    _close_position(executor, "AAA/BBB", position, now=5, force=False)
    assert all(row.get("close_qty") is None for row in portfolio._pending_intents)
    flush_delayed_intents(portfolio, prices, bar_index=6)
    assert portfolio.positions["AAA"] > 15.0
    assert portfolio.positions["BBB"] < -15.0
    assert position["leg_notional"] == pytest.approx(200.0)
    assert "AAA/BBB" in _open_book(executor)


def test_overlapping_reduce_only_closes_cannot_reverse(zero_equity_costs):
    RUN_OPTIONS.fill_timing = "next_close"
    RUN_OPTIONS.equity_slippage_bps = 5.0
    portfolio, prices, executor = _pair_book()
    portfolio.positions["AAA"] = 40.0
    portfolio.entry_cost["AAA"] = 400.0
    portfolio.positions["BBB"] = -40.0
    portfolio._short_open_qty["BBB"] = 40.0
    portfolio._short_open_proceeds["BBB"] = 400.0
    executor.execute_full_exit("AAA")
    executor.execute_full_exit("AAA")
    executor.execute_full_exit("BBB")
    executor.execute_full_exit("BBB")
    flush_delayed_intents(portfolio, prices, bar_index=6)
    assert portfolio.positions.get("AAA", 0.0) == pytest.approx(0.0)
    assert portfolio.positions.get("BBB", 0.0) == pytest.approx(0.0)


def test_pair_exit_without_cover_cash_stays_open(zero_equity_costs, monkeypatch):
    RUN_OPTIONS.fill_timing = "next_close"
    RUN_OPTIONS.equity_slippage_bps = 5.0
    monkeypatch.setattr(config, "effective_stat_arb_enabled", lambda: True)
    portfolio, prices, executor = _pair_book()
    _held_pair(portfolio, executor)
    portfolio.cash = 0.0
    position = _open_book(executor)["AAA/BBB"]
    _close_position(executor, "AAA/BBB", position, now=5, force=False)
    assert flush_delayed_intents(portfolio, prices, bar_index=6) == []
    assert portfolio.positions["AAA"] == pytest.approx(40.0)
    assert portfolio.positions["BBB"] == pytest.approx(-40.0)
    assert "AAA/BBB" in _open_book(executor)
    assert portfolio.closed_trades == []
    assert portfolio.pair_rejections[-1]["reason"] == "insufficient_cash"


def test_residual_exposure_keeps_the_research_book(zero_equity_costs, monkeypatch):
    RUN_OPTIONS.fill_timing = "next_close"
    monkeypatch.setattr(config, "effective_stat_arb_enabled", lambda: True)
    from modules.stat_arb_sleeve import _finalize_pair_book_close

    portfolio, _prices, executor = _pair_book()
    _held_pair(portfolio, executor)
    portfolio._research_fill_bar = 6
    position = _open_book(executor)["AAA/BBB"]
    _finalize_pair_book_close(
        executor,
        "AAA/BBB",
        position,
        long_sym="AAA",
        short_sym="BBB",
        leg_n=400.0,
        regime="",
        now=5,
        force=False,
        att=None,
        mark_pnl=None,
        is_crypto=False,
        log_fn=None,
    )
    assert "AAA/BBB" in _open_book(executor)
    assert portfolio.positions["AAA"] == pytest.approx(40.0)


def test_pair_exception_restores_later_intents(zero_equity_costs, monkeypatch):
    RUN_OPTIONS.fill_timing = "next_close"
    monkeypatch.setattr(config, "effective_stat_arb_enabled", lambda: True)
    portfolio, prices, executor = _pair_book()
    execute_atomic_pair_entry(
        executor, "AAA", "BBB", 400.0, pair_key="AAA/BBB", strategy="stat_arb"
    )
    _queue_research_intent(
        portfolio,
        symbol="CCC",
        side="buy",
        notional=200.0,
        tx_cost=0.0,
        allow_naked_short=False,
        reduce_only=False,
        signal_bar=4,
        source="strategy",
    )
    original = portfolio.trade
    calls = {"n": 0}

    def explode(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("pair leg failed")
        return original(*args, **kwargs)

    portfolio.trade = explode
    with pytest.raises(RuntimeError, match="pair leg failed"):
        flush_delayed_intents(portfolio, prices, bar_index=5)
    assert portfolio.positions == {}
    assert portfolio.cash == pytest.approx(10_000.0)
    assert [row["symbol"] for row in portfolio._pending_intents] == ["CCC"]
    portfolio.trade = original
    flush_delayed_intents(portfolio, prices, bar_index=6)
    assert portfolio.positions["CCC"] == pytest.approx(20.0)
    assert "AAA" not in portfolio.positions


def test_partial_queue_failure_records_a_rejection(zero_equity_costs, monkeypatch):
    RUN_OPTIONS.fill_timing = "next_close"
    monkeypatch.setattr(config, "effective_exit_optimization_enabled", lambda: True)
    monkeypatch.setattr("modules.exit_management.partial_exit_fraction", lambda: 0.5)
    portfolio, prices, executor = _pair_book()
    _held_pair(portfolio, executor)
    executor.prices = prices.drop(labels=["BBB"])
    position = _open_book(executor)["AAA/BBB"]
    position["exit_reason"] = "partial_1r"
    assert _close_position(executor, "AAA/BBB", position, now=5, force=False) == 0
    assert portfolio.positions["AAA"] == pytest.approx(40.0)
    assert portfolio.positions["BBB"] == pytest.approx(-40.0)
    assert position["leg_notional"] == pytest.approx(400.0)
    assert portfolio._pending_intents == []
    assert portfolio.closed_trades == []
    assert portfolio.pair_rejections[-1]["reason"] == "leg_not_queued"


def test_same_close_profit_cooldown_stays_on_the_signal_bar(zero_equity_costs, monkeypatch):
    RUN_OPTIONS.fill_timing = "same_close"
    monkeypatch.setattr("modules.profit_target.profit_target_enabled", lambda: True)
    monkeypatch.setattr("modules.profit_target.is_eligible_symbol", lambda *args, **kwargs: True)
    portfolio = BacktestPortfolio(10_000.0)
    prices = pd.Series({"AAA": 11.0})
    executor = BacktestExecutor(portfolio, prices)
    portfolio.positions["AAA"] = 10.0
    portfolio.entry_cost["AAA"] = 100.0
    executor._profit_target_state["AAA"] = {"armed": True, "hwm": 13.0, "entry": 10.0}
    from modules.profit_target import run_profit_target_exits

    assert run_profit_target_exits(executor, now=4, bar_idx=4) == 1
    assert executor._profit_exit_cooldown["AAA"]["bar_idx"] == 4


def datetime_now():
    from datetime import datetime

    return datetime.now()


def _nyse_entered(symbol: str) -> set:
    from modules.pipeline_strategies import _calendar_day_key

    return set(_nyse_mom_entries_by_day.get(_calendar_day_key(datetime_now()), set()))
