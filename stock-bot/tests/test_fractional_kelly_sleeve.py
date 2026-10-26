"""Half-Kelly sleeve stays at the cap until the sample exists, then scales."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parents[1] / "scripts" / "analysis" / "one_r_hit_backtest.py"
    spec = importlib.util.spec_from_file_location("one_r_hit_backtest", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_warmup_stays_at_cap():
    mod = _load()
    got = mod.fractional_kelly_sleeve([0.02, 0.01], fraction=0.5, floor=0.0, cap=0.67, min_n=40)
    assert got == 0.67


def test_thin_edge_cuts_the_sleeve():
    mod = _load()
    # mean +0.2%, dispersion about 8 points → half-Kelly well under the 0.67 cap
    returns = [0.082] * 20 + [-0.078] * 20
    got = mod.fractional_kelly_sleeve(returns, fraction=0.5, floor=0.0, cap=0.67, min_n=40)
    assert 0.10 < got < 0.25


def test_negative_edge_hits_the_floor():
    mod = _load()
    returns = [-0.02] * 30 + [0.005] * 10
    got = mod.fractional_kelly_sleeve(returns, fraction=0.5, floor=0.0, cap=0.67, min_n=40)
    assert got == 0.0
