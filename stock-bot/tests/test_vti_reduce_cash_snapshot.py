"""Pure helpers for VTI reduce cash snapshot (no Alpaca, no orders)."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from vti_reduce_cash_snapshot import (  # noqa: E402
    anchor_inventory,
    asof_cycle,
    classify_cash,
    heuristic_label,
    walk_vti_inventory,
    would_skip_cash_need_rule,
)


def test_classify_cash_unused_vs_tight():
    assert classify_cash(30000, 29660) == "cash_unused"
    assert classify_cash(10000, 29660) == "cash_tight"
    assert classify_cash(None, 29660) == "cash_unknown"
    assert classify_cash(float("nan"), 29660) == "cash_unknown"
    assert classify_cash(100, 0) == "cash_unknown"


def test_heuristic_maps_unknown_to_tight():
    assert heuristic_label("cash_unknown") == "cash_tight"
    assert heuristic_label("cash_unused") == "cash_unused"
    assert heuristic_label("cash_tight") == "cash_tight"


def test_cash_need_skip_requires_unused_and_floor():
    assert would_skip_cash_need_rule(kind="cash_unused", vti_pct=54.0) is True
    assert would_skip_cash_need_rule(kind="cash_unused", vti_pct=80.0) is True
    assert would_skip_cash_need_rule(kind="cash_unused", vti_pct=39.9) is False
    assert would_skip_cash_need_rule(kind="cash_tight", vti_pct=80.0) is False
    assert would_skip_cash_need_rule(kind="cash_unused", vti_pct=None) is False


def test_walk_qty_before_sell():
    fills = [
        {"ts": pd.Timestamp("2026-06-16 14:00:00"), "side": "buy", "qty": 214.433},
        {"ts": pd.Timestamp("2026-06-16 14:13:09"), "side": "sell", "qty": 81.049},
        {"ts": pd.Timestamp("2026-06-16 14:13:40"), "side": "buy", "qty": 80.0},
        {"ts": pd.Timestamp("2026-06-16 14:14:27"), "side": "sell", "qty": 80.036},
    ]
    walked = walk_vti_inventory(fills)
    assert math.isclose(walked[1]["qty_before"], 214.433, rel_tol=0, abs_tol=1e-6)
    assert math.isclose(walked[1]["qty_after"], 214.433 - 81.049, rel_tol=0, abs_tol=1e-6)
    assert math.isclose(walked[3]["qty_before"], 214.433 - 81.049 + 80.0, rel_tol=0, abs_tol=1e-6)


def test_anchor_shifts_walk_to_broker_qty():
    walked = walk_vti_inventory(
        [
            {"ts": pd.Timestamp("2026-05-29"), "side": "buy", "qty": 100.0},
            {"ts": pd.Timestamp("2026-06-01"), "side": "sell", "qty": 10.0},
        ]
    )
    anchored, delta = anchor_inventory(walked, 97.0)
    assert math.isclose(delta, 7.0)
    assert math.isclose(anchored[-1]["qty_after"], 97.0)
    assert math.isclose(anchored[0]["qty_before"], 7.0)


def test_asof_cycle_uses_last_row_at_or_before():
    cycles = pd.DataFrame(
        {
            "ts": pd.to_datetime(
                ["2026-08-05 08:30:00-05:00", "2026-08-05 09:00:00-05:00"]
            ),
            "equity": [97500.0, 97400.0],
            "cash": [22600.0, 11000.0],
        }
    )
    hit = asof_cycle(cycles, pd.Timestamp("2026-08-05 08:45:00-05:00"))
    assert hit["equity"] == 97500.0
    assert hit["cash"] == 22600.0
    assert hit["cycle_stale"] is False
    miss = asof_cycle(cycles, pd.Timestamp("2026-08-01 08:00:00-05:00"))
    assert miss["cash"] is None
    assert miss["cycle_stale"] is True
