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
import sqlite3
import time
from pathlib import Path

import numpy as np
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


def yf_symbol(ticker: str) -> str:
    return str(ticker).strip().upper().replace(".", "-")


def ticker_aliases(ticker: str) -> set[str]:
    t = str(ticker).strip().upper()
    return {t, t.replace(".", "-"), t.replace("-", "."), t.replace(".", "_")}


def resolve_column(ticker: str, columns) -> str | None:
    """Membership ticker → price column, allowing BRK.B / BRK-B / BRK_B."""
    cols = {str(c).upper(): c for c in columns}
    for alias in ticker_aliases(ticker):
        if alias in cols:
            return cols[alias]
    return None


def ffill_interior_only(data: pd.DataFrame) -> pd.DataFrame:
    """Fill holiday gaps, but do not extend a name past its last real print."""
    if data is None or data.empty:
        return data
    last = {c: data[c].last_valid_index() for c in data.columns}
    filled = data.sort_index().ffill()
    for col, ts in last.items():
        if ts is None:
            filled[col] = np.nan
            continue
        filled.loc[filled.index > ts, col] = np.nan
    return filled


def disjoint_session_windows(data: pd.DataFrame, *, days: int, ma_win: int, pad: int = 40):
    """Two non-overlapping walk frames. Recent first, then the prior year.

    Each frame is ``days + ma_win + pad`` session bars. The prior frame ends at
    the first walk bar of the recent frame, so the evaluated dates do not overlap.
    """
    need = int(days) + int(ma_win) + int(pad)
    start_i = int(ma_win) + 15
    if len(data) < need:
        return [(f"{days}d_recent", data.tail(need))]
    recent = data.iloc[-need:]
    prior_end = len(data) - need + start_i
    if prior_end < need:
        return [(f"{days}d_recent", recent)]
    prior = data.iloc[prior_end - need : prior_end]
    return [(f"{days}d_recent", recent), (f"{days}d_prior", prior)]


def prefetch_missing_members(
    members: list[str],
    have_columns,
    *,
    chunk: int = 40,
    pause_sec: float = 2.0,
) -> dict:
    """Download max daily history for PIT members with no sqlite table."""
    from modules.data_loader import clear_close_matrix_cache, daily_table_name

    have = {str(c).upper() for c in have_columns}
    missing = [m for m in members if not any(a in have for a in ticker_aliases(m))]
    report = {"wanted": len(members), "already": len(members) - len(missing), "fetched": 0, "failed": []}
    if not missing:
        return report
    try:
        import yfinance as yf
    except ImportError:
        report["failed"] = missing
        return report
    from fetch_data import _normalize_df

    conn = sqlite3.connect(config.resolve_db_path())
    for i in range(0, len(missing), chunk):
        batch = missing[i : i + chunk]
        yf_map = {yf_symbol(t): t for t in batch}
        try:
            raw = yf.download(
                list(yf_map),
                period="max",
                interval="1d",
                auto_adjust=True,
                progress=False,
                group_by="ticker",
                threads=True,
            )
        except Exception:
            report["failed"].extend(batch)
            time.sleep(pause_sec)
            continue
        if raw is None or raw.empty:
            report["failed"].extend(batch)
            time.sleep(pause_sec)
            continue
        for yf_sym, local in yf_map.items():
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    if yf_sym not in raw.columns.get_level_values(0):
                        report["failed"].append(local)
                        continue
                    sub = raw[yf_sym]
                else:
                    sub = raw if len(batch) == 1 else None
                if sub is None or sub.empty:
                    report["failed"].append(local)
                    continue
                df = _normalize_df(sub)
                if df.empty or "Close" not in df.columns or int(df["Close"].notna().sum()) < 5:
                    report["failed"].append(local)
                    continue
                df.to_sql(daily_table_name(local), conn, if_exists="replace", index=False)
                report["fetched"] += 1
            except Exception:
                report["failed"].append(local)
        conn.commit()
        time.sleep(pause_sec)
    conn.close()
    clear_close_matrix_cache()
    return report


def session_bars(data: pd.DataFrame) -> pd.DataFrame:
    """Mon–Fri rows only. Crypto ffill otherwise leaves weekend stock closes."""
    if data is None or data.empty:
        return data
    idx = pd.DatetimeIndex(data.index)
    return data.loc[idx.weekday < 5].copy()


def load_research_closes(membership: pd.DataFrame, extra_tickers=(), *, prefetch: bool = True):
    """Session-bar close matrix for PIT walks, with interior-only ffill.

    Prefetches missing S&P members (max yfinance history) so coverage is a
    real number. Two passes if the first fetch extends the date range.
    """
    from modules.data_loader import load_close_matrix

    wanted = sorted(set(membership["ticker"].astype(str)) | {str(t) for t in extra_tickers})
    data = load_close_matrix(interval="1d", tickers=wanted, ffill=False, force_refresh=True)
    report = {"wanted": len(wanted), "already": 0, "fetched": 0, "failed": []}
    if prefetch and data is not None and not data.empty:
        in_window = sp500_tickers_in_window(membership, data.index[0], data.index[-1])
        report = prefetch_missing_members(in_window, data.columns)
        data = load_close_matrix(interval="1d", tickers=wanted, ffill=False, force_refresh=True)
        if data is not None and not data.empty:
            again = sp500_tickers_in_window(membership, data.index[0], data.index[-1])
            extra = [t for t in again if resolve_column(t, data.columns) is None]
            if extra:
                more = prefetch_missing_members(extra, data.columns)
                report["fetched"] += more["fetched"]
                report["failed"] = sorted(set(report["failed"]) | set(more["failed"]))
                data = load_close_matrix(interval="1d", tickers=wanted, ffill=False, force_refresh=True)
    if data is None or data.empty:
        return data, report
    data = data.where(data > 0)
    data = data.dropna(axis=1, how="all")
    data = ffill_interior_only(data)
    data = session_bars(data)
    return data, report


def coverage_report(
    eligible: pd.DataFrame,
    data_columns,
    *,
    members_in_window=None,
) -> dict:
    """How much of the PIT universe has price data (the remaining survivorship gap).

    Pass ``members_in_window`` *before* filtering to ``data.columns``. If you
    build ``eligible`` only from names that already have prices, coverage is
    always 100% and the gap is hidden.
    """
    have = {str(c).upper() for c in data_columns}
    if members_in_window is not None:
        members = [str(m).upper() for m in dict.fromkeys(members_in_window) if m]
    else:
        members = [c for c in eligible.columns if bool(eligible[c].any())]
    missing = sorted(m for m in members if resolve_column(m, data_columns) is None)
    per_day = eligible.sum(axis=1)
    covered_cols = [c for c in eligible.columns if str(c).upper() in have]
    covered = eligible[covered_cols].sum(axis=1) if covered_cols else per_day * 0
    return {
        "members_in_window": len(members),
        "members_with_data": len(members) - len(missing),
        "members_missing_data": len(missing),
        "coverage_pct": round(100.0 * (len(members) - len(missing)) / len(members), 1) if members else 0.0,
        "avg_members_per_day": round(float(per_day.mean()), 1) if len(per_day) else 0.0,
        "avg_covered_per_day": round(float(covered.mean()), 1) if len(covered) else 0.0,
        "missing_sample": missing[:25],
    }
