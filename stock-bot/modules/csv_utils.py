"""Defensive CSV readers for mixed-schema journal and state files."""

from __future__ import annotations

import csv
import logging
from collections import deque
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

TRADE_JOURNAL_CANONICAL = [
    "timestamp",
    "event",
    "symbol",
    "side",
    "regime",
    "pair_key",
    "z_score",
    "equity",
    "cash",
    "notional",
    "notes",
]

_JOURNAL_COLUMN_ALIASES = {
    "ticker": "symbol",
}


def normalize_csv_row(row: list[str], width: int) -> list[str]:
    """Pad or truncate a CSV row so it matches the header width."""
    if len(row) == width:
        return row
    if len(row) < width:
        return row + [""] * (width - len(row))
    return row[:width]


def read_csv_rows_safe(
    path: Path,
    *,
    max_rows: int | None = None,
) -> tuple[list[str], list[list[str]]]:
    """Read CSV rows without raising on ragged column counts."""
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if not header:
                return [], []
            header = [str(c).strip() for c in header]
            if max_rows is not None and max_rows > 0:
                rows = list(deque(reader, maxlen=max_rows))
            else:
                rows = list(reader)
    except OSError as exc:
        logger.warning("Could not read CSV %s: %s", path, exc)
        return [], []

    width = len(header)
    normalized: list[list[str]] = []
    for row in rows:
        if not row or all(not str(c).strip() for c in row):
            continue
        if len(row) != width:
            logger.debug(
                "Normalizing ragged CSV row in %s (%s cols -> %s)",
                path.name,
                len(row),
                width,
            )
        normalized.append(normalize_csv_row(row, width))
    return header, normalized


def read_csv_tail(path: Path, max_rows: int) -> pd.DataFrame:
    """Read the last max_rows data rows without failing on mixed schemas."""
    header, rows = read_csv_rows_safe(path, max_rows=max_rows)
    if not header:
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame(columns=header)
    try:
        return pd.DataFrame(rows, columns=header)
    except ValueError as exc:
        logger.warning("CSV tail parse failed for %s: %s", path.name, exc)
        return pd.DataFrame(columns=header)


def read_csv_file(path: Path, *, tail_rows: int | None = None) -> pd.DataFrame:
    """Load a CSV file; large files use a safe tail reader."""
    if not path.is_file():
        return pd.DataFrame()
    try:
        if tail_rows is not None and tail_rows > 0 and path.stat().st_size > 256_000:
            return read_csv_tail(path, tail_rows)
        return pd.read_csv(path, encoding="utf-8", engine="python", on_bad_lines="warn")
    except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError, ValueError) as exc:
        logger.warning("CSV read failed for %s: %s", path.name, exc)
        if tail_rows is not None and tail_rows > 0:
            try:
                return read_csv_tail(path, tail_rows)
            except Exception:
                return pd.DataFrame()
        return pd.DataFrame()


def coerce_trade_journal_df(df: pd.DataFrame) -> pd.DataFrame:
    """Align legacy/extra journal columns to a stable schema for UI + metrics."""
    if df.empty:
        return df
    out = df.copy()
    out.columns = [str(c).strip() for c in out.columns]
    for alias, target in _JOURNAL_COLUMN_ALIASES.items():
        if alias in out.columns:
            if target not in out.columns:
                out[target] = out[alias]
            elif out[target].astype(str).str.strip().eq("").all():
                out[target] = out[alias]

    for col in TRADE_JOURNAL_CANONICAL:
        if col not in out.columns:
            out[col] = ""

    extras = [c for c in out.columns if c not in TRADE_JOURNAL_CANONICAL]
    return out[TRADE_JOURNAL_CANONICAL + extras]
