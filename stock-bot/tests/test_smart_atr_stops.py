"""Daily ATR for smart stops — 5m noise must not fire a 2.0× stop at −0.3%."""

from __future__ import annotations

import unittest

import pandas as pd

import config
from modules.risk_management import calculate_atr
from modules.smart_atr_stops import (
    atr_stop_min_pct,
    compute_stop_price,
    ensure_initial_stop,
    evaluate_smart_stop,
)


def _daily_walk(days: int = 40, start: float = 100.0, daily_move: float = 1.50) -> pd.DataFrame:
    idx = pd.bdate_range("2026-07-01", periods=days)
    closes = [start + i * 0.05 + ((-1) ** i) * daily_move * 0.5 for i in range(days)]
    return pd.DataFrame({"TEST": closes}, index=idx)


def _five_min_session(days: int = 30, start: float = 100.0, daily_move: float = 1.50) -> pd.DataFrame:
    """RTH 5m bars: ~2% daily range, ~0.05% typical 5m print-to-print."""
    rows = []
    px = start
    for d in range(days):
        day = pd.Timestamp("2026-07-01") + pd.Timedelta(days=d)
        if day.weekday() >= 5:
            continue
        session_open = day + pd.Timedelta(hours=13, minutes=30)  # 9:30 ET as UTC-naive
        day_dir = 1.0 if d % 2 == 0 else -1.0
        for b in range(78):
            ts = session_open + pd.Timedelta(minutes=5 * b)
            # Intraday noise plus a slow daily drift.
            px = px + day_dir * (daily_move / 78.0) + ((-1) ** b) * 0.04
            rows.append((ts, px))
    idx, vals = zip(*rows)
    return pd.DataFrame({"TEST": list(vals)}, index=pd.DatetimeIndex(idx))


class DailyAtrTests(unittest.TestCase):
    def test_daily_series_unchanged_scale(self):
        data = _daily_walk()
        atr = calculate_atr(data, "TEST", period=14)
        self.assertIsNotNone(atr)
        native = data["TEST"].diff().abs().rolling(14).mean().iloc[-1]
        self.assertAlmostEqual(float(atr), round(float(native), 4), places=4)

    def test_five_min_atr_matches_daily_not_bar_noise(self):
        data = _five_min_session()
        atr = calculate_atr(data, "TEST", period=14)
        self.assertIsNotNone(atr)
        native_5m = data["TEST"].diff().abs().rolling(14).mean().iloc[-1]
        daily = data["TEST"].resample("1D").last().dropna()
        native_d = daily.diff().abs().rolling(14).mean().iloc[-1]
        self.assertGreater(float(atr), float(native_5m) * 3)
        self.assertAlmostEqual(float(atr), round(float(native_d), 4), places=3)
        px = float(data["TEST"].iloc[-1])
        self.assertGreater(float(atr) / px, 0.005)


class SmartStopFloorTests(unittest.TestCase):
    def setUp(self):
        self._saved = getattr(config, "ATR_STOP_MIN_PCT", 0.01)
        config.ATR_STOP_MIN_PCT = 0.01

    def tearDown(self):
        config.ATR_STOP_MIN_PCT = self._saved

    def test_min_pct_floors_tiny_atr(self):
        stop = compute_stop_price(100.0, atr=0.10, multiplier=2.0)
        # 2×0.10 = 0.20 would be a 0.2% stop; floor at 1% → 99.00
        self.assertEqual(stop, 99.00)
        self.assertGreaterEqual(atr_stop_min_pct(), 0.01)

    def test_restamp_replaces_five_min_stop(self):
        meta = {"smart_stop_price": 99.70, "atr_stop_mult": 2.0}  # −0.3%
        out = ensure_initial_stop(meta, entry=100.0, atr=2.0, side="long")
        self.assertLessEqual(float(out["smart_stop_price"]), 98.00)

    def test_keep_valid_daily_stop(self):
        meta = {"smart_stop_price": 96.00, "atr_stop_mult": 2.0}
        out = ensure_initial_stop(meta, entry=100.0, atr=2.0, side="long")
        self.assertEqual(float(out["smart_stop_price"]), 96.00)

    def test_point_three_pct_dip_does_not_exit(self):
        data = _five_min_session()
        atr = calculate_atr(data, "TEST", period=14)
        self.assertIsNotNone(atr)
        entry = float(data["TEST"].iloc[-1])
        current = entry * 0.997  # −0.3%, matching today's paper fills
        decision = evaluate_smart_stop(
            symbol="TEST",
            entry=entry,
            current=current,
            atr=float(atr),
            meta={},
            qty=10,
            data=data,
        )
        self.assertIsNone(decision.get("action"))
        stop = float(decision["stop_price"])
        self.assertLess(stop, entry * 0.99)

    def test_real_atr_stop_still_fires(self):
        entry = 100.0
        atr = 2.0  # 2% of price; 2.0× → $4 / 4%
        decision = evaluate_smart_stop(
            symbol="TEST",
            entry=entry,
            current=95.50,
            atr=atr,
            meta={},
            qty=10,
        )
        self.assertEqual(decision.get("action"), "exit")
        self.assertEqual(decision.get("exit_code"), "smart_atr_stop")


if __name__ == "__main__":
    unittest.main()
