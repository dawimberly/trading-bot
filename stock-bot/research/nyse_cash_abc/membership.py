"""Research membership through 2026-10-14.

Reads the production point-in-time file and does not write it. The file's last
change is 2026-08-18. Later changes come from S&P Dow Jones Indices releases
and are applied only in this process.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from modules.pit_universe import load_sp500_membership  # noqa: E402

# Effective before the open. An end date is exclusive, matching pit_universe.
# Sources: S&P DJI release 2026-09-04 (effective 2026-09-21); S&P DJI release
# 2026-10-01 (VYLR effective 2026-10-01, CTVA / TWLO / WBD effective 2026-10-06).
# Everpure's ticker P is from its 2026-09-08 release (NYSE: P). No S&P 500
# change is listed after 2026-10-06 through the 2026-10-14 session cutoff.
ADDITIONS = (
    ("BE", "2026-09-21"),
    ("P", "2026-09-21"),
    ("ILMN", "2026-09-21"),
    ("VYLR", "2026-10-01"),
    ("TWLO", "2026-10-06"),
)
REMOVALS = {
    "TAP": "2026-09-21",
    "TTD": "2026-09-21",
    "BLDR": "2026-09-21",
    "CTVA": "2026-10-06",
    "WBD": "2026-10-06",
}


def research_membership() -> pd.DataFrame:
    frame = load_sp500_membership().copy()
    for ticker, end in REMOVALS.items():
        end_ts = pd.Timestamp(end)
        mask = (frame["ticker"] == ticker) & (frame["start"] < end_ts) & (frame["end"].isna() | (frame["end"] > end_ts))
        frame.loc[mask, "end"] = end_ts
    extra = pd.DataFrame(
        {"ticker": [t for t, _ in ADDITIONS], "start": [pd.Timestamp(d) for _, d in ADDITIONS], "end": pd.NaT}
    )
    return pd.concat([frame, extra], ignore_index=True)
