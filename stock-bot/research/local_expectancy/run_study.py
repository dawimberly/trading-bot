"""Run the frozen local_expectancy study. Research only. No orders."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from engine import hold_nav, path_stats, simulate
from gates import bootstrap_total_returns, study_status
from yahoo import fetch_symbol

ROOT = Path(__file__).resolve().parent
PANEL_PATH = ROOT / "panel.json"
RESULTS_PATH = ROOT / "results.json"
PERIOD1 = int(datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp())
PERIOD2 = int(datetime(2026, 10, 25, tzinfo=timezone.utc).timestamp())

PUBLIC_KEYS = {
    "hypothesis_id",
    "status",
    "promotion",
    "generated_at",
    "panel_sha256",
    "symbols_included",
    "symbols_excluded",
    "windows",
    "cells",
    "limitations",
    "annual_target_claim",
    "entry_quote_max_age_seconds",
}


def load_rules() -> dict:
    return json.loads((ROOT / "preregistration.json").read_text(encoding="utf-8"))


def _round(value):
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {key: _round(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_round(item) for item in value]
    return value


def _strip_path(block: dict) -> dict:
    kept = {key: value for key, value in block.items() if key != "daily_returns"}
    return _round(kept)


def fetch_panel(rules: dict) -> dict:
    names = list(rules["universe"]["names"]) + list(rules["universe"]["benchmarks"])
    symbols = {}
    errors = {}
    for symbol in names:
        try:
            symbols[symbol] = [
                {"d": day, "o": opened, "c": close}
                for day, (opened, close) in sorted(fetch_symbol(symbol, PERIOD1, PERIOD2).items())
            ]
        except RuntimeError as exc:
            errors[symbol] = str(exc.__cause__ or exc)[:120]
    panel = {
        "source": "yahoo_chart_v8_daily_adjusted",
        "period1": PERIOD1,
        "period2": PERIOD2,
        "symbols": symbols,
        "download_errors": errors,
    }
    PANEL_PATH.write_text(json.dumps(panel), encoding="utf-8")
    return panel


def load_panel(refresh: bool, rules: dict) -> dict:
    if refresh or not PANEL_PATH.exists():
        return fetch_panel(rules)
    return json.loads(PANEL_PATH.read_text(encoding="utf-8"))


def bars_of(panel: dict) -> dict[str, dict[str, tuple[float, float]]]:
    out = {}
    for symbol, rows in panel["symbols"].items():
        out[symbol] = {row["d"]: (float(row["o"]), float(row["c"])) for row in rows}
    return out


def classify(symbol: str, sessions: list[str], bars: dict, rules: dict) -> str | None:
    limits = rules["universe"]["exclude_before_returns"]
    have = bars.get(symbol) or {}
    if not have:
        return "no_bars"
    through = [day for day in sessions if day <= limits["min_closes_on_or_before"] and day in have]
    if len(through) < limits["min_closes"]:
        return "short_history"
    first = min(have)
    span = [day for day in sessions if day >= first]
    missing = [day for day in span if day not in have]
    if len(missing) > limits["max_gaps_after_inception"]:
        return "gaps"
    closes = [have[day][1] for day in span if day in have]
    for prev, cur in zip(closes, closes[1:]):
        move = cur / prev - 1
        if move > limits["exclude_if_one_day_move_above"]:
            return "untrusted_up_move"
        if move < limits["exclude_if_one_day_move_below"]:
            return "untrusted_down_move"
    return None


def window_sessions(sessions: list[str], after: str, through: str) -> list[str]:
    return [day for day in sessions if after < day <= through]


def _hold_block(nav: list[float]) -> dict:
    stats = path_stats(nav)
    return {"return": stats["return"], "max_drawdown": stats["max_drawdown"], "sharpe": stats["sharpe"]}


def run_cell(sessions, bars, symbols, bounds, slip_bps, *, skip_monday: bool = False) -> dict:
    start = window_sessions(sessions, bounds["after"], bounds["through"])
    if len(start) < 40:
        raise RuntimeError("window has too few sessions")
    sliced = [day for day in sessions if day <= bounds["through"]]
    result = simulate(
        sliced,
        bars,
        symbols,
        window_start=start[0],
        window_end=start[-1],
        slip_bps=slip_bps,
        quote_age_seconds=0,
        learner_skip_weekdays={0} if skip_monday else None,
    )
    result["learner"]["bootstrap"] = bootstrap_total_returns(result["learner"]["daily_returns"])
    lo = sliced.index(start[0])
    hi = sliced.index(start[-1])
    holds = {}
    for name, basket in (
        ("equal_weight_hold", symbols),
        ("vti_hold", ["VTI"]),
        ("spy_hold", ["SPY"]),
    ):
        holds[name] = _hold_block(hold_nav(sliced, bars, basket, lo, hi, slip_bps / 10000))
    result["holds"] = holds
    result["window_start"] = start[0]
    result["window_end"] = start[-1]
    result["window_sessions"] = len(start)
    return result


def public_window(cell: dict, monday: dict | None) -> dict:
    return {
        "window_start": cell["window_start"],
        "window_end": cell["window_end"],
        "sessions": cell["window_sessions"],
        "learner": _strip_path(cell["learner"]),
        "baseline": _strip_path(cell["baseline"]),
        "equal_weight_hold": _round(cell["holds"]["equal_weight_hold"]),
        "vti_hold": _round(cell["holds"]["vti_hold"]),
        "spy_hold": _round(cell["holds"]["spy_hold"]),
        "monday_learner_return": None if monday is None else round(monday["learner"]["return"], 6),
        "accepted_refits": cell["accepted_refits"],
        "rejected_refits": cell["rejected_refits"],
        "model_alive_at_end": cell["model_alive_at_end"],
        "quote_age_seconds": cell["quote_age_seconds"],
    }


def gate_block(cell: dict, monday: dict | None) -> dict:
    monday_return = None if monday is None else monday["learner"]["return"]
    return {
        "learner": cell["learner"],
        "baseline": cell["baseline"],
        "equal_weight_hold": cell["holds"]["equal_weight_hold"],
        "vti_hold": cell["holds"]["vti_hold"],
        "monday": None if monday_return is None else {"return": monday_return},
    }


def main(argv: list[str]) -> dict:
    refresh = "--refresh" in argv
    rules = load_rules()
    panel = load_panel(refresh, rules)
    bars = bars_of(panel)
    if "SPY" not in bars or "VTI" not in bars:
        raise SystemExit("SPY and VTI are required and were not both downloaded")
    sessions = sorted(set(bars["SPY"]) & set(bars["VTI"]))
    excluded = dict(panel.get("download_errors") or {})
    included = []
    for symbol in rules["universe"]["names"]:
        if symbol in excluded:
            continue
        reason = classify(symbol, sessions, bars, rules)
        if reason:
            excluded[symbol] = reason
            continue
        included.append(symbol)
    if len(included) < 8:
        raise SystemExit(f"too few symbols after pre-registered exclusions: {len(included)}")

    windows_public = {}
    gate_windows = {}
    for name in ("prior_365", "recent_365", "long_1000_supplement"):
        bounds = rules["windows"][name]
        windows_public[name] = {}
        for slip, key in ((5, "slip_5"), (10, "slip_10")):
            cell = run_cell(sessions, bars, included, bounds, slip)
            monday = run_cell(sessions, bars, included, bounds, slip, skip_monday=True)
            windows_public[name][key] = public_window(cell, monday)
            if name != "long_1000_supplement":
                gate_windows.setdefault(name, {})[key] = gate_block(cell, monday)

    verdict = study_status(gate_windows)
    payload = {
        "hypothesis_id": rules["hypothesis_id"],
        "status": verdict["status"],
        "promotion": False,
        "annual_target_claim": False,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "panel_sha256": hashlib.sha256(PANEL_PATH.read_bytes()).hexdigest(),
        "symbols_included": included,
        "symbols_excluded": excluded,
        "windows": windows_public,
        "cells": verdict["cells"],
        "entry_quote_max_age_seconds": 420,
        "limitations": [
            "Daily adjusted Yahoo bars stand in for the frozen five-minute feature clock because free five-minute history does not cover the protocol windows.",
            "One entry per session, not one entry per five-minute bar.",
            "Historical opens are modeled at quote age 0. A delayed SIP quote still fails the 7-minute gate and was not used as an entry feed.",
            "The June 2026 screener list has survivorship. Corporate actions are not independently audited.",
            "15x30x2 was not rebuilt. This does not promote, deploy, or touch a live or paper book.",
        ],
    }
    unexpected = set(payload) - PUBLIC_KEYS
    if unexpected:
        raise SystemExit(f"refusing to write unexpected fields: {sorted(unexpected)}")
    RESULTS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps({"status": payload["status"], "promotion": False, "cells": payload["cells"]}, indent=2))
    return payload


if __name__ == "__main__":
    main(sys.argv[1:])
