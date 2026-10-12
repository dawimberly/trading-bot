"""Read-only inventory of local files for an NYSE ranking comparison.

Does not fetch data, open a broker, start a bot, or print secret values.
Weekend rows are counted separately and are not treated as sessions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPO = ROOT.parent
OUT = Path(__file__).with_suffix(".json")
CACHE = ROOT / "data" / "cache" / "backtest" / "days_266.pkl"
SLEEVE = {
    "SPY",
    "QQQ",
    "IWM",
    "VTI",
    "GLD",
    "SLV",
    "CPER",
    "URA",
    "PPLT",
    "DBB",
    "GDX",
}


def _env_key_names(path: Path) -> dict:
    """Return key names that have a non-placeholder value. Never return values."""
    if not path.is_file():
        return {"present": False, "nonplaceholder_keys": []}
    keys: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, _, value = raw.partition("=")
        val = value.strip().strip('"').strip("'")
        low = val.lower()
        placeholder = (
            not val
            or low.startswith("your_")
            or "placeholder" in low
            or low in {"changeme", "xxx", "todo"}
        )
        if not placeholder:
            keys.append(key.strip())
    return {"present": True, "bytes": path.stat().st_size, "nonplaceholder_keys": keys}


def _cache() -> dict:
    if not CACHE.is_file():
        return {"present": False, "path": str(CACHE)}
    import pandas as pd

    frame = pd.read_pickle(CACHE)
    idx = pd.DatetimeIndex(pd.to_datetime(frame.index))
    weekend = idx.dayofweek.isin([5, 6])
    sessions = frame.loc[~weekend]
    equity = [c for c in frame.columns if "-USD" not in str(c) and str(c) not in SLEEVE]
    session_idx = sessions.index
    dup_all = int(idx.duplicated().sum())
    dup_sessions = int(session_idx.duplicated().sum())

    def unchanged_share(col: str) -> float | None:
        if col not in sessions.columns or len(sessions) < 2:
            return None
        s = sessions[col]
        prev = s.shift(1)
        ok = s.notna() & prev.notna()
        if int(ok.sum()) == 0:
            return None
        return float((s[ok] == prev[ok]).mean())

    sparse = {
        str(k): int(v)
        for k, v in frame.notna().sum().items()
        if int(v) < len(frame)
    }
    session_sparse = {
        str(k): int(v)
        for k, v in sessions.notna().sum().items()
        if int(v) < len(sessions)
    }
    first = idx.min()
    after = sessions.loc[sessions.index >= first + pd.DateOffset(months=12)]
    # 252 prior weekday rows is the session lag used only as a coverage count.
    lag_ok = 0
    if len(sessions) > 252:
        lag_ok = int(len(sessions) - 252)
    return {
        "present": True,
        "path": str(CACHE.relative_to(ROOT)),
        "rows_including_weekends": int(len(frame)),
        "columns": int(frame.shape[1]),
        "fields": ["close_only"],
        "ohlc_present": False,
        "start": str(first.date()),
        "end": str(idx.max().date()),
        "median_step_days_raw_index": float(idx.to_series().diff().dt.days.median()),
        "weekend_rows_excluded_from_sessions": int(weekend.sum()),
        "weekday_rows_treated_as_sessions": int((~weekend).sum()),
        "duplicate_timestamps_raw": dup_all,
        "duplicate_weekday_timestamps": dup_sessions,
        "column_names": [str(c) for c in frame.columns],
        "equity_names": equity,
        "equity_count": len(equity),
        "sparse_columns_including_weekends": sparse,
        "sparse_weekday_columns": session_sparse,
        "aapl_weekday_unchanged_vs_prior_weekday": unchanged_share("AAPL"),
        "spy_weekday_unchanged_vs_prior_weekday": unchanged_share("SPY"),
        "weekday_rows_on_or_after_12_calendar_months": int(len(after)),
        "first_weekday_on_or_after_12_calendar_months": str(after.index.min().date())
        if len(after)
        else None,
        "weekday_rows_after_252_prior_weekdays": lag_ok,
        "adjustment_status": "unverifiable_from_close_only_pickle",
        "membership_column": False,
        "delist_column": False,
    }


def _files() -> dict:
    interesting = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(ROOT))
        if rel.startswith("sentiment/sources"):
            continue
        name = path.name.lower()
        if path.suffix.lower() in {".db", ".parquet", ".pkl", ".feather", ".h5"} or any(
            token in name for token in ("sp500", "constituent", "delist", "intraday")
        ):
            interesting.append({"path": rel, "bytes": path.stat().st_size})
    return {
        "market_data_db": (ROOT / "market_data.db").is_file()
        or (ROOT / "data" / "market_data.db").is_file(),
        "intraday_script": (ROOT / "scripts" / "research" / "backtest_intraday.py").is_file(),
        "intraday_cache": (ROOT / "data" / "intraday_cache").is_dir(),
        "core_allocator": (ROOT / "modules" / "core_allocator.py").is_file(),
        "price_or_membership_files": interesting,
    }


def main() -> int:
    report = {
        "checkout": str(REPO),
        "secrets_printed": False,
        "broker_contacted": False,
        "cache": _cache(),
        "files": _files(),
        "credential_files": {
            "repo_env": _env_key_names(REPO / ".env"),
            "stock_bot_env": _env_key_names(ROOT / ".env"),
            "note": "Only key names are listed. Values are never written.",
        },
    }
    OUT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    print(
        json.dumps(
            {
                "weekday_sessions": report["cache"].get("weekday_rows_treated_as_sessions"),
                "weekend_rows": report["cache"].get("weekend_rows_excluded_from_sessions"),
                "equity_count": report["cache"].get("equity_count"),
                "ohlc": report["cache"].get("ohlc_present"),
                "market_db": report["files"]["market_data_db"],
                "stock_bot_env_present": report["credential_files"]["stock_bot_env"]["present"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
