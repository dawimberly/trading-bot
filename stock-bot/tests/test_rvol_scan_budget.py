"""Shared RVOL scan budget, unknown-RVOL ranking, and bounded gap/A2B1 fetches."""

from __future__ import annotations

import pandas as pd
import pytest

from modules.position_exits import _a2b1_prior_return, a2b1_should_flatten
from modules.volume_analysis import (
    apply_rvol_universe_boost,
    calculate_rvol,
    filter_symbols_by_rvol,
    prioritize_symbols_by_rvol,
)


def _enable_rvol(monkeypatch):
    monkeypatch.setattr(
        "modules.volume_analysis.config.effective_rvol_scanner_enabled",
        lambda: True,
    )


def test_large_universe_stops_at_a_spent_budget(monkeypatch):
    _enable_rvol(monkeypatch)
    calls: list[str] = []

    def _rvol(_data, sym):
        calls.append(sym)
        return 9.0

    monkeypatch.setattr("modules.volume_analysis.calculate_rvol", _rvol)
    symbols = [f"S{i:03d}" for i in range(400)]
    out = apply_rvol_universe_boost(symbols, None, deadline_sec=0)
    assert calls == []
    assert out == symbols
    assert out[0] != "S399"


def test_prioritize_does_not_rank_unscanned_names_as_high_rvol(monkeypatch):
    calls: list[str] = []

    def _rvol(_data, sym):
        calls.append(sym)
        return {"AAA": 2.0, "BBB": 1.2}.get(sym, 9.0)

    monkeypatch.setattr("modules.volume_analysis.calculate_rvol", _rvol)
    seen = {"n": 0}

    def _reached(_deadline):
        seen["n"] += 1
        return seen["n"] > 2

    monkeypatch.setattr("modules.volume_analysis._deadline_reached", _reached)
    out = prioritize_symbols_by_rvol(
        ["AAA", "BBB", "ZZZ", "CCC"], None, deadline_sec=45
    )
    assert calls == ["AAA", "BBB"]
    assert out[:2] == ["AAA", "BBB"]
    assert out[2:] == ["ZZZ", "CCC"]


def test_boost_uses_one_budget_for_filter_and_rank(monkeypatch):
    _enable_rvol(monkeypatch)
    clock = {"t": 1000.0}
    monkeypatch.setattr(
        "modules.volume_analysis.time.monotonic",
        lambda: clock["t"],
    )
    calls: list[str] = []

    def _rvol(_data, sym):
        calls.append(sym)
        clock["t"] += 20.0
        return 4.0

    monkeypatch.setattr("modules.volume_analysis.calculate_rvol", _rvol)
    symbols = ["A", "B", "C", "D", "E", "F", "G", "H"]
    out = apply_rvol_universe_boost(symbols, None, deadline_sec=45)
    assert calls == ["A", "B", "C"]
    assert out == symbols


def test_unknown_rvol_stays_eligible_and_unranked(monkeypatch):
    _enable_rvol(monkeypatch)

    def _rvol(_data, sym):
        return {"PASS": 3.0, "FAIL": 0.2, "UNK": None, "DELIST": None}[sym]

    monkeypatch.setattr("modules.volume_analysis.calculate_rvol", _rvol)
    kept = filter_symbols_by_rvol(
        ["PASS", "FAIL", "UNK", "DELIST"], None, min_rvol=1.0, deadline_sec=45
    )
    assert kept == ["PASS", "UNK", "DELIST"]
    ranked = prioritize_symbols_by_rvol(kept, None, deadline_sec=45)
    assert ranked == ["PASS", "UNK", "DELIST"]


def test_timeout_and_delisted_history_are_unknown_rvol(monkeypatch):
    monkeypatch.setattr("modules.volume_analysis._cache_get", lambda _symbol: None)

    def _hist(symbol, **_kwargs):
        if symbol == "HES":
            raise TimeoutError("Yahoo history exceeded 8s")
        raise RuntimeError(
            '$PXD: possibly delisted; no price data found  (period=30d)'
        )

    monkeypatch.setattr("modules.volume_analysis.yahoo_history", _hist)
    assert calculate_rvol(None, "HES") is None
    assert calculate_rvol(None, "PXD") is None

    _enable_rvol(monkeypatch)

    def _rvol(_data, sym):
        if sym == "GOOD":
            return 4.0
        return calculate_rvol(_data, sym)

    monkeypatch.setattr("modules.volume_analysis.calculate_rvol", _rvol)
    ranked = prioritize_symbols_by_rvol(
        ["HES", "GOOD", "PXD"], None, deadline_sec=45
    )
    assert ranked == ["GOOD", "HES", "PXD"]


def test_failed_rvol_is_dropped_and_empty_sleeve_fallback_remains(monkeypatch):
    _enable_rvol(monkeypatch)

    def _rvol(_data, sym):
        return 0.1

    monkeypatch.setattr("modules.volume_analysis.calculate_rvol", _rvol)
    assert filter_symbols_by_rvol(["BBB", "AAA"], None, min_rvol=1.0) == []
    out = apply_rvol_universe_boost(["BBB", "AAA"], None, deadline_sec=45)
    assert out == ["BBB", "AAA"]


def test_overnight_gap_timeout_uses_matrix_and_spent_budget_skips_yahoo(monkeypatch):
    from modules.pipeline_strategies import (
        _nyse_momentum_quality_skip,
        _overnight_gap_pct,
    )

    calls: list[str] = []

    def _hist(symbol, **kwargs):
        calls.append(symbol)
        assert kwargs["period"] == "10d"
        raise TimeoutError("Yahoo history exceeded 8s")

    monkeypatch.setattr("modules.volume_analysis.yahoo_history", _hist)
    monkeypatch.setattr(
        "modules.pipeline_strategies.config.backtest_paper_sleeves_context",
        lambda: False,
    )
    data = pd.DataFrame({"AAA": [100.0, 110.0]})
    assert _overnight_gap_pct("AAA", data) == pytest.approx(0.10)
    assert calls == ["AAA"]

    calls.clear()
    from modules.volume_analysis import _deadline_at

    assert _overnight_gap_pct("AAA", None, deadline=_deadline_at(0)) is None
    assert calls == []

    monkeypatch.setattr(
        "modules.pipeline_strategies.config.effective_paper_momentum_quality_fixes",
        lambda: False,
    )
    assert _nyse_momentum_quality_skip("AAA", data) is None
    assert calls == []


def test_measured_overnight_gap_still_blocks_a_large_open(monkeypatch):
    from modules.pipeline_strategies import _nyse_momentum_quality_skip

    hist = pd.DataFrame({"Open": [10.0, 12.0], "Close": [11.0, 12.5]})

    def _hist(_symbol, **_kwargs):
        return hist

    monkeypatch.setattr("modules.volume_analysis.yahoo_history", _hist)
    monkeypatch.setattr(
        "modules.pipeline_strategies.config.backtest_paper_sleeves_context",
        lambda: False,
    )
    monkeypatch.setattr(
        "modules.pipeline_strategies.config.effective_paper_momentum_quality_fixes",
        lambda: True,
    )
    monkeypatch.setattr(
        "modules.pipeline_strategies._nyse_open_cooldown_active",
        lambda now=None: False,
    )
    monkeypatch.setattr(
        "modules.pipeline_strategies._nyse_journal_entered_today",
        lambda *args, **kwargs: False,
    )
    skip = _nyse_momentum_quality_skip("AAA", pd.DataFrame({"AAA": [1.0, 1.0]}))
    assert skip is not None
    assert "overnight gap" in skip


def test_a2b1_timeout_is_unknown_and_does_not_flatten(monkeypatch):
    calls: list[str] = []

    def _hist(symbol, **kwargs):
        calls.append(symbol)
        assert kwargs["period"] == "15d"
        raise TimeoutError("Yahoo history exceeded 8s")

    monkeypatch.setattr("modules.volume_analysis.yahoo_history", _hist)

    class _Exec:
        pass

    executor = _Exec()
    assert _a2b1_prior_return("ARM", executor=executor) is None
    assert _a2b1_prior_return("ARM", executor=executor) is None
    assert calls == ["ARM"]
    assert a2b1_should_flatten(1, None) is False

    class _Injected:
        _a2b1_prior_returns = {"ARM": 0.09}

    assert _a2b1_prior_return("ARM", executor=_Injected()) == 0.09
    assert calls == ["ARM"]


def test_a2b1_spent_scan_budget_does_not_fetch_or_cache(monkeypatch):
    def _hist(*_args, **_kwargs):
        raise AssertionError("yahoo should not run")

    monkeypatch.setattr("modules.volume_analysis.yahoo_history", _hist)

    class _Exec:
        pass

    executor = _Exec()
    from modules.volume_analysis import _deadline_at

    assert (
        _a2b1_prior_return("KTOS", executor=executor, deadline=_deadline_at(0))
        is None
    )
    assert not hasattr(executor, "_a2b1_ret_cache") or executor._a2b1_ret_cache == {}
