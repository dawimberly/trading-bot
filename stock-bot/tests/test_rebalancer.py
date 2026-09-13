"""Tests for StrategicRebalancer."""

from __future__ import annotations

from datetime import date

import config
from modules.rebalancer import StrategicRebalancer


def test_check_drift_reports_core_gap():
    rb = StrategicRebalancer(core_target=0.80, band_width=0.08)
    snap = rb.snapshot_from_values(
        equity=10000,
        core_value=6000,
        tactical_value=3000,
        cash_value=1000,
    )
    drift = rb.check_drift(snap)
    assert drift["core_pct"] == 0.6
    assert drift["core_drift"] == 0.2
    assert drift["max_drift"] >= 0.08


def test_needs_rebalance_on_drift():
    rb = StrategicRebalancer(core_target=0.80, band_width=0.08)
    snap = rb.snapshot_from_values(
        equity=10000,
        core_value=6000,
        tactical_value=3000,
        cash_value=1000,
    )
    assert rb.needs_rebalance(snap, date(2025, 6, 15), rb.check_drift(snap))


def test_needs_rebalance_on_month_start():
    rb = StrategicRebalancer(core_target=0.80, band_width=0.08)
    snap = rb.snapshot_from_values(
        equity=10000,
        core_value=8000,
        tactical_value=1500,
        cash_value=500,
    )
    drift = rb.check_drift(snap)
    assert not rb.needs_rebalance(snap, date(2025, 6, 15), drift)
    assert rb.needs_rebalance(
        snap,
        date(2025, 7, 1),
        drift,
        prev_bar_date=date(2025, 6, 30),
    )


def test_generate_rebalance_orders_buy_core():
    rb = StrategicRebalancer(core_target=0.80, band_width=0.08)
    snap = rb.snapshot_from_values(
        equity=10000,
        core_value=6000,
        tactical_value=3000,
        cash_value=1000,
    )
    orders = rb.generate_rebalance_orders(snap, 0.80, min_notional=10.0)
    assert len(orders) == 1
    assert orders[0].action == "buy"
    assert orders[0].notional == 2000.0


def test_disabled_when_rebalance_off(monkeypatch):
    monkeypatch.setattr(config, "REBALANCE_ENABLED", False)
    rb = StrategicRebalancer()
    snap = rb.snapshot_from_values(
        equity=10000,
        core_value=1000,
        tactical_value=8000,
        cash_value=1000,
    )
    assert not rb.needs_rebalance(snap, date(2025, 6, 15), rb.check_drift(snap))
