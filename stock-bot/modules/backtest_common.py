"""Shared helpers for backtester.py hub and satellite research scripts."""

from __future__ import annotations

import argparse

import pandas as pd


def slice_data_by_year(data: pd.DataFrame, year_from: int, year_to: int) -> pd.DataFrame:
    start = pd.Timestamp(f"{year_from}-01-01")
    end = pd.Timestamp(f"{year_to}-12-31")
    if data.index.tz is not None:
        start = start.tz_localize(data.index.tz)
        end = end.tz_localize(data.index.tz)
    return data.loc[(data.index >= start) & (data.index <= end)]


def normalize_yfinance_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    close_col = next((c for c in df.columns if str(c).lower() == "close"), None)
    if close_col is None:
        return pd.DataFrame()
    out = df[[close_col]].copy()
    out.columns = ["Close"]
    out.index.name = "Date"
    return out.reset_index()


def add_year_range_args(parser: argparse.ArgumentParser, *, include_refresh: bool = True) -> None:
    parser.add_argument("--from", dest="year_from", type=int, default=2017)
    parser.add_argument("--to", dest="year_to", type=int, default=2023)
    if include_refresh:
        parser.add_argument("--refresh", action="store_true")
