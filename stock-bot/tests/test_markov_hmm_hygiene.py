"""HMM dependency fallback and backtest time-of-day lookahead guards."""

from __future__ import annotations

import builtins
import logging

import numpy as np
import pandas as pd

import config
from modules import markov_regime
from modules.markov_regime import (
    build_hmm_features,
    format_markov_hmm_banner,
    heartbeat_hmm_payload,
    hmm_sizing_multiplier,
    reset_markov_hmm_state,
    set_research_tod_neutral,
    update_markov_hmm,
)


def _spy_frame(n: int = 180) -> pd.DataFrame:
    idx = pd.bdate_range("2018-01-02", periods=n)
    rets = np.random.default_rng(3).normal(0.0003, 0.01, size=n)
    px = 100.0 * np.exp(np.cumsum(rets))
    return pd.DataFrame({"SPY": px}, index=idx)


def _block_hmmlearn(monkeypatch) -> None:
    real_import = builtins.__import__

    def blocked(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "hmmlearn" or str(name).startswith("hmmlearn."):
            raise ImportError("blocked for test")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", blocked)


def test_missing_hmmlearn_is_loud_and_sizes_one(monkeypatch, caplog):
    reset_markov_hmm_state()
    set_research_tod_neutral(False)
    _block_hmmlearn(monkeypatch)
    monkeypatch.setattr(config, "effective_markov_hmm_enabled", lambda: True)

    frame = _spy_frame()
    with caplog.at_level(logging.WARNING, logger="modules.markov_regime"):
        pred = update_markov_hmm(frame, neutralize_tod=True)

    assert pred["ok"] is False
    assert pred["reason"] == "hmmlearn_missing"
    assert pred["dependency_missing"] is True
    assert pred["sizing_mult"] == 1.0
    assert pred["fallback"] == "rhyme"
    assert hmm_sizing_multiplier() == 1.0
    assert "hmmlearn is not installed" in caplog.text
    assert "sizing stays 1.0" in caplog.text

    hb = heartbeat_hmm_payload()
    assert hb["enabled"] is True
    assert hb["ok"] is False
    assert hb["reason"] == "hmmlearn_missing"
    assert hb["dependency_missing"] is True
    assert hb["sizing_mult"] == 1.0
    banner = format_markov_hmm_banner() or ""
    assert "INACTIVE hmmlearn missing" in banner
    assert "sizing x1.00" in banner
    set_research_tod_neutral(False)
    reset_markov_hmm_state()


def test_live_gate_stays_off_and_heartbeat_says_so(monkeypatch):
    reset_markov_hmm_state()
    set_research_tod_neutral(False)
    monkeypatch.setattr(config, "MARKOV_HMM_ENABLED", True)
    monkeypatch.setattr(config, "MARKOV_HMM_LIVE_ENABLED", False)
    monkeypatch.setattr(config, "paper_only_sleeves_active", lambda: False)
    monkeypatch.setattr(config, "paper_aggressive_context", lambda: False)
    monkeypatch.setattr(config, "is_realistic_research_active", lambda: False)

    assert config.effective_markov_hmm_enabled() is False
    hb = heartbeat_hmm_payload()
    assert hb["enabled"] is False
    assert hb["reason"] == "gated_off"
    assert hb["live_opt_in"] is False
    assert hb["sizing_mult"] == 1.0
    assert hb["inactive_explicit"] is True

    pred = update_markov_hmm(_spy_frame(), neutralize_tod=True)
    assert pred["reason"] == "disabled"
    assert pred["sizing_mult"] == 1.0
    assert hmm_sizing_multiplier() == 1.0
    reset_markov_hmm_state()


def test_research_flag_neutralizes_tod_cache(monkeypatch):
    reset_markov_hmm_state()
    set_research_tod_neutral(True)
    calls = {"cache": 0, "clock": 0}

    def _cache():
        calls["cache"] += 1
        return {"recommendation": {"best_entry_bucket": "open"}}

    def _clock():
        calls["clock"] += 1
        return True

    monkeypatch.setattr("modules.time_of_day.load_tod_cache", _cache)
    monkeypatch.setattr("modules.time_of_day.get_last_tod_summary", lambda: None)
    monkeypatch.setattr("modules.time_of_day.effective_time_of_day_analysis", _clock)

    frame, _names = build_hmm_features(_spy_frame())
    assert frame is not None
    assert (frame["tod_bucket"] == 0.5).all()
    assert calls["cache"] == 0

    monkeypatch.setattr(config, "effective_markov_hmm_enabled", lambda: True)
    _block_hmmlearn(monkeypatch)
    pred = update_markov_hmm(_spy_frame())
    assert pred["tod_research"] == "neutralized"
    assert pred["tod_lookahead_blocked"] is True
    assert calls["cache"] == 0
    assert calls["clock"] == 0
    set_research_tod_neutral(False)
    reset_markov_hmm_state()


def test_live_clock_path_still_reads_cache(monkeypatch):
    reset_markov_hmm_state()
    set_research_tod_neutral(False)
    monkeypatch.setattr(config, "backtest_paper_sleeves_context", lambda: False)
    monkeypatch.setattr(config, "backtest_strict_pit_context", lambda: False)
    monkeypatch.setattr(config, "backtest_live_conservative_context", lambda: False)
    calls = {"cache": 0}

    def _cache():
        calls["cache"] += 1
        return {"recommendation": {"best_entry_bucket": "open"}}

    monkeypatch.setattr("modules.time_of_day.load_tod_cache", _cache)
    monkeypatch.setattr("modules.time_of_day.get_last_tod_summary", lambda: None)

    series = markov_regime._tod_feature_series(
        _spy_frame().index, neutralize=False
    )
    assert calls["cache"] == 1
    assert float(series.iloc[0]) == 0.0
    reset_markov_hmm_state()


def test_run_backtest_arms_and_clears_tod_neutral():
    from pathlib import Path

    src = Path(__file__).resolve().parents[1].joinpath("backtester.py").read_text(
        encoding="utf-8"
    )
    assert "set_research_tod_neutral(True)" in src
    assert "set_research_tod_neutral(False)" in src
