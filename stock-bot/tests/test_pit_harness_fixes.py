"""PIT harness: session bars, honest coverage, gap alignment by date."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "analysis"))

from modules import pit_universe as pit  # noqa: E402
from one_r_hit_backtest import align_to_frame  # noqa: E402


def test_session_bars_drops_weekends():
    idx = pd.to_datetime(["2026-10-09", "2026-10-10", "2026-10-11", "2026-10-12"])  # Fri-Mon
    data = pd.DataFrame({"AAA": [1.0, 1.0, 1.0, 2.0]}, index=idx)
    out = pit.session_bars(data)
    assert list(out.index.strftime("%Y-%m-%d")) == ["2026-10-09", "2026-10-12"]


def test_coverage_counts_members_without_price_data():
    idx = pd.to_datetime(["2026-10-09"])
    elig = pd.DataFrame({"AAA": [True], "BBB": [True]}, index=idx)
    report = pit.coverage_report(elig, ["AAA"], members_in_window=["AAA", "BBB", "CCC"])
    assert report["members_in_window"] == 3
    assert report["members_with_data"] == 1
    assert report["members_missing_data"] == 2
    assert report["coverage_pct"] == 33.3
    assert "BBB" in report["missing_sample"]


def test_ffill_interior_does_not_extend_past_last_print():
    idx = pd.to_datetime(["2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"])
    data = pd.DataFrame({"DEAD": [10.0, 10.5, np.nan, np.nan], "LIVE": [1.0, np.nan, 1.2, 1.3]}, index=idx)
    out = pit.ffill_interior_only(data)
    assert list(out["DEAD"]) == [10.0, 10.5, np.nan, np.nan] or (
        out["DEAD"].iloc[0] == 10.0 and pd.isna(out["DEAD"].iloc[2])
    )
    assert out["LIVE"].iloc[1] == 1.0
    assert out["LIVE"].iloc[3] == 1.3


def test_disjoint_windows_do_not_overlap_walk_dates():
    idx = pd.bdate_range("2022-01-03", periods=900)
    data = pd.DataFrame({"AAA": range(len(idx))}, index=idx)
    windows = pit.disjoint_session_windows(data, days=365, ma_win=70, pad=40)
    assert len(windows) == 2
    recent = windows[0][1]
    prior = windows[1][1]
    start_i = 70 + 15
    assert prior.index[-1] < recent.index[start_i]


def test_align_to_frame_uses_date_not_row_number():
    frame_idx = pd.to_datetime(["2025-09-16", "2025-09-17", "2025-09-18"])
    frame = pd.DataFrame({"AAA": [10.0, 11.0, 12.0]}, index=frame_idx)
    long_idx = pd.to_datetime(["2022-03-18", "2022-03-21", "2025-09-16", "2025-09-17", "2025-09-18"])
    gaps = pd.DataFrame({"AAA": [9.0, 9.0, 1.01, 1.02, 1.03]}, index=long_idx)
    aligned = align_to_frame(gaps, frame)
    assert aligned is not None
    assert list(aligned["AAA"]) == [1.01, 1.02, 1.03]
