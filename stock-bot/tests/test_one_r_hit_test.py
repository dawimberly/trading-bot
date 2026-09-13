"""1R target plan math — measure-only, no orders."""

from __future__ import annotations

from modules.one_r_hit_test import annotate_notes, plan_from_entry
from modules.smart_atr_stops import compute_stop_price


def test_plan_is_one_to_one_when_rr_is_one():
    entry = 100.0
    atr = 2.0
    plan = plan_from_entry(entry, atr, rr=1.0)
    stop = compute_stop_price(entry, atr, side="long")
    assert plan["stop"] == stop
    assert plan["target"] == round(entry + (entry - stop), 2)
    # 2.0 ATR at 2x = $4 risk → 4% target
    assert plan["stop_pct"] == 0.04
    assert plan["target_pct"] == 0.04


def test_min_pct_floor_widens_target():
    entry = 100.0
    atr = 0.10  # 2x ATR = $0.20, below 1% floor
    plan = plan_from_entry(entry, atr, rr=1.0)
    assert plan["stop"] == 99.0
    assert plan["target"] == 101.0
    assert plan["target_pct"] == 0.01


def test_annotate_notes_is_one_line():
    note = annotate_notes(plan_from_entry(50.0, 1.0, rr=1.0))
    assert "1r_target=" in note
    assert "\n" not in note


def test_walk_path_take_1r_and_stop():
    import numpy as np

    from modules.one_r_hit_test import walk_path

    closes = np.array([100.0, 101.0, 104.0, 99.0], dtype=float)
    i, px, reason, touched = walk_path(closes, 0, stop=98.0, target=103.0, max_hold=5, take_1r=True)
    assert reason == "1r" and touched and i == 2
    i, px, reason, touched = walk_path(closes, 0, stop=98.0, target=103.0, max_hold=5, take_1r=False)
    assert reason == "time" and touched is True
    down = np.array([100.0, 97.0, 96.0], dtype=float)
    i, px, reason, touched = walk_path(down, 0, stop=98.0, target=103.0, max_hold=5, take_1r=False)
    assert reason == "stop" and touched is False
