"""Point-in-time candidate universes for daily research walks.

The default research universe (`config.nyse_momentum_universe`) is whatever
the screener and sector pools contain *today*. A momentum walk over history
on names picked for having momentum today is look-ahead. Two honest
alternatives:

* ``frozen``  — the hand-written static lists only (``config.UNIVERSE`` +
  sector pools). No screener-only names. Still hindsight-picked, but not
  momentum-ranked at the end of the window.
* ``sp500``   — S&P 500 membership by date (``data/sp500_ticker_start_end.csv``,
  from github.com/fja05680/sp500). A name is a candidate on date *t* only if it
  was in the index on *t*. Names that left the index stop being candidates on
  their exit date; names with no price history are reported as missing so the
  remaining survivorship gap is a number, not a guess.

Research only. Nothing here touches the runtime universe.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd

import config

ROOT = Path(__file__).resolve().parents[1]
SP500_MEMBERSHIP_PATH = ROOT / "data" / "sp500_ticker_start_end.csv"
SP500_MEMBERSHIP_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/master/sp500_ticker_start_end.csv"
)
MODES = ("current", "frozen", "sp500")


def _static_pool_names() -> set[str]:
    """Hand-written lists only (no screener JSON)."""
    out: set[str] = set(config.get_nyse_universe_fixed())
    try:
        from modules.dynamic_universe import EQUITY_SECTOR_MAP
        from modules.pipeline_strategies import NYSE_SECTOR_MAP
        from modules.sector_screener import LIQUID_NYSE_FALLBACK, SECTOR_EXTRA_BY_TAG

        out.update(EQUITY_SECTOR_MAP)
        out.update(NYSE_SECTOR_MAP)
        out.update(LIQUID_NYSE_FALLBACK)
        for extras in SECTOR_EXTRA_BY_TAG.values():
            out.update(extras)
    except ImportError:
        pass
    return {str(s).strip().upper() for s in out if s}


def frozen_universe(columns) -> list[str]:
    """Columns that come from static lists, excluding screener-only names."""
    static = _static_pool_names()
    return [c for c in columns if str(c).upper() in static]


def ensure_sp500_membership(path: Path = SP500_MEMBERSHIP_PATH, *, timeout: float = 30.0) -> Path:
    """Download the membership CSV once if it is missing."""
    if path.is_file():
        return path
    import requests

    resp = requests.get(SP500_MEMBERSHIP_URL, timeout=timeout)
    resp.raise_for_status()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(resp.text, encoding="utf-8", newline="")
    return path


def load_sp500_membership(path: Path = SP500_MEMBERSHIP_PATH) -> pd.DataFrame:
    """Rows of ``ticker, start, end`` (end NaT = still a member)."""
    rows: list[dict] = []
    with open(path, encoding="utf-8", newline="") as fh:
        for rec in csv.DictReader(fh):
            tick = str(rec.get("ticker") or "").strip().upper()
            if not tick:
                continue
            rows.append(
                {
                    "ticker": tick,
                    "start": pd.Timestamp(rec.get("start_date")),
                    "end": pd.Timestamp(rec["end_date"]) if rec.get("end_date") else pd.NaT,
                }
            )
    if not rows:
        return pd.DataFrame(columns=["ticker", "start", "end"])
    return pd.DataFrame(rows)


def membership_matrix(
    index: pd.DatetimeIndex, membership: pd.DataFrame, *, tickers=None
) -> pd.DataFrame:
    """Bool frame (dates x tickers): ``True`` when the name was in the index that day."""
    idx = pd.DatetimeIndex(index).normalize()
    if tickers is None:
        tickers = sorted(membership["ticker"].unique())
    tickers = [str(t).upper() for t in tickers]
    out = pd.DataFrame(False, index=idx, columns=tickers)
    for rec in membership.itertuples(index=False):
        if rec.ticker not in out.columns:
            continue
        start = pd.Timestamp(rec.start).normalize()
        end = pd.Timestamp(rec.end).normalize() if pd.notna(rec.end) else None
        mask = idx >= start
        if end is not None:
            mask &= idx < end
        if mask.any():
            out.loc[mask, rec.ticker] = True
    return out


def sp500_tickers_in_window(
    membership: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp
) -> list[str]:
    """Every name that was a member at any point in ``[start, end]``."""
    start = pd.Timestamp(start).normalize()
    end = pd.Timestamp(end).normalize()
    ends = membership["end"].fillna(pd.Timestamp.max)
    mask = (membership["start"] <= end) & (ends > start)
    return sorted(membership.loc[mask, "ticker"].unique())


def coverage_report(eligible: pd.DataFrame, data_columns) -> dict:
    """How much of the PIT universe has price data (the remaining survivorship gap)."""
    have = {str(c).upper() for c in data_columns}
    members = [c for c in eligible.columns if bool(eligible[c].any())]
    missing = sorted(m for m in members if m not in have)
    per_day = eligible.sum(axis=1)
    covered = eligible[[c for c in eligible.columns if c in have]].sum(axis=1)
    return {
        "members_in_window": len(members),
        "members_with_data": len(members) - len(missing),
        "members_missing_data": len(missing),
        "coverage_pct": round(100.0 * (len(members) - len(missing)) / len(members), 1) if members else 0.0,
        "avg_members_per_day": round(float(per_day.mean()), 1) if len(per_day) else 0.0,
        "avg_covered_per_day": round(float(covered.mean()), 1) if len(covered) else 0.0,
        "missing_sample": missing[:25],
    }
