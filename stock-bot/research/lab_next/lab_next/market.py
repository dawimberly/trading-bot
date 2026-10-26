"""Read-only five-minute cache inspector.

This module does not import an order executor, does not read trading credentials,
and does not write the production market database or the intraday cache.
Split-adjusted cache rows are not raw tradable bars. Missing corporate-action
coverage blocks a virtual fill feed instead of assuming there were no dividends,
mergers, renames, or spin-offs.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

from .calendar import ET, close_verified, is_session

CACHE = Path(__file__).resolve().parents[3] / "data" / "intraday_cache"


def blockers(provenance: dict, *, has_action_ledger: bool, universe: str) -> list[str]:
    found = []
    if provenance.get("adjustment") != "raw":
        found.append(
            f"prices are {provenance.get('adjustment') or 'unmarked'}, not raw; "
            "the engine will not invent a raw series from split-adjusted bars"
        )
    if not has_action_ledger:
        found.append(
            "no independent corporate-action ledger for splits, cash dividends, "
            "mergers, renames, and spin-offs; a caller boolean would not be an audit"
        )
    if universe != "explicit_frozen_asof_universe":
        found.append(
            "universe is a saved cache snapshot, not a point-in-time index; "
            "this is not a historical S&P study"
        )
    if not provenance.get("spy_total_return") or not provenance.get("vti_total_return"):
        found.append("SPY/VTI total return is unavailable; an absent benchmark is not zero")
    return found


def _meta(path: Path) -> dict:
    sidecar = path.with_suffix(".meta.json")
    if not sidecar.is_file():
        return {"symbol": path.stem, "meta": False}
    return json.loads(sidecar.read_text(encoding="utf-8"))


def inspect(cache: Path = CACHE, *, as_of: datetime | None = None) -> dict:
    moment = as_of or datetime.now(ET)
    files = sorted(cache.glob("*.pkl")) if cache.is_dir() else []
    metas = [_meta(path) for path in files]
    adjustments = sorted({item.get("adjustment") for item in metas if item.get("adjustment")})
    ends = [item.get("end") for item in metas if item.get("end")]
    sample = _sample_quality(files[:3])
    provenance = {
        "adjustment": adjustments[0] if len(adjustments) == 1 else None,
        "adjustments": adjustments,
        "spy_total_return": (cache / "SPY.pkl").is_file(),
        "vti_total_return": (cache / "VTI.pkl").is_file(),
        "feed": sorted({item.get("feed") for item in metas if item.get("feed")}),
        "bar_interval": sorted({item.get("bar_interval") for item in metas if item.get("bar_interval")}),
    }
    reasons = blockers(provenance, has_action_ledger=False, universe="intraday_cache_snapshot")
    if sample["duplicate_timestamps"]:
        reasons.append(f"duplicate timestamps in sample: {sample['duplicate_timestamps']}")
    if ends:
        newest = max(ends)
        newest_dt = datetime.fromisoformat(newest)
        if newest_dt.tzinfo is None:
            newest_dt = newest_dt.replace(tzinfo=ET)
        if moment - newest_dt > timedelta(days=3):
            reasons.append(f"cache is stale for forward use; newest stored bar is {newest}")
    early = close_verified(moment.date().isoformat()) if is_session(moment.date().isoformat()) else False
    return {
        "reader": "local five-minute pickle cache",
        "cache": str(cache),
        "symbols": len(files),
        "connected": cache.is_dir() and bool(files),
        "tradable_feed": False,
        "running": False,
        "pid": None,
        "as_of": moment.isoformat(),
        "newest_bar": max(ends) if ends else None,
        "close_verified_for_as_of_date": early,
        "sample": sample,
        "blockers": reasons,
        "historical_replay": "blocked",
        "forward_observation": "blocked",
    }


def _sample_quality(paths: list[Path]) -> dict:
    duplicates = {}
    unreadable = []
    try:
        import pandas as pd
    except ImportError:
        return {"duplicate_timestamps": {}, "unreadable": ["pandas_unavailable"], "rows": 0}
    rows = 0
    for path in paths:
        try:
            frame = pd.read_pickle(path)
        except Exception as exc:
            unreadable.append(f"{path.stem}:{type(exc).__name__}")
            continue
        if "timestamp" not in getattr(frame, "columns", []):
            unreadable.append(f"{path.stem}:no_timestamp")
            continue
        rows += len(frame)
        dupes = int(frame["timestamp"].duplicated().sum())
        if dupes:
            duplicates[path.stem] = dupes
    return {"duplicate_timestamps": duplicates, "unreadable": unreadable, "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--forward-check", action="store_true")
    parser.add_argument("--cache", type=Path, default=CACHE)
    args = parser.parse_args()
    if not args.inspect and not args.forward_check:
        parser.error("--inspect or --forward-check required")
    payload = inspect(args.cache)
    payload["command"] = "forward-check" if args.forward_check else "inspect"
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
