"""Experiment specifications. Invalid windows are rejected, never clipped."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

FROZEN_GRID = {"ma_win": [50, 70, 100], "max_hold": [15, 30, 45], "atr_mult": [1.5, 2.0, 2.5]}
BASELINE_A = {"ma_win": 70, "max_hold": 30, "atr_mult": 2.0}
STRATEGIES = ("baseline_a", "frozen_selector", "research_grid")

# Dates already used to inspect or tune the cash-accounted research. Not an untouched holdout.
DEVELOPMENT_SPANS = (
    {"start": "2023-11-15", "end": "2025-05-01", "name": "nyse_cash_abc prior window"},
    {"start": "2025-05-02", "end": "2026-10-14", "name": "nyse_cash_abc recent window"},
    {"start": "2025-10-14", "end": "2026-10-14", "name": "walk-forward evaluation"},
)
UNTOUCHED_RANGE = None
UNTOUCHED_REASON = (
    "No genuinely untouched range exists in the research snapshot. "
    "The prior window, the recent window, and the walk-forward folds all sit inside "
    "2023-11-15 through 2026-10-14, and earlier sessions were used as indicator warm-up."
)


class SpecError(ValueError):
    """A concrete reason an experiment cannot run."""


def canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def spec_hash(spec: dict) -> str:
    return hashlib.sha256(canonical(spec).encode("utf-8")).hexdigest()


def default_research_spec(name: str = "research-grid") -> dict:
    """A new experiment id's starting point. Editing it does not change the frozen files."""
    return {
        "name": name,
        "strategy": "research_grid",
        "accounting": "payable_date",
        "grid": {"ma_win": [70], "max_hold": [30], "atr_mult": [2.0]},
        "schedule": {
            "anchor": "2024-12-31",
            "warmup_sessions": 85,
            "train_sessions": 40,
            "eval_sessions": 15,
            "folds": 1,
        },
        "costs": {"slip_bps_per_side": 5, "cash_interest": 0},
        "risk": {"max_drawdown_at_most": 0.25, "min_closed_trades": 1},
        "benchmark": "spy_total_return",
        "budget_hours": 24,
        "seed": 1,
        "locked_validation": None,
        "include_locked_validation": False,
        "comparable_to_frozen": False,
    }


def frozen_selector_spec() -> dict:
    return {
        "name": "frozen-walk-forward-selector",
        "strategy": "frozen_selector",
        "accounting": "payable_date",
        "grid": deepcopy(FROZEN_GRID),
        "schedule": {
            "anchor": "2026-10-14",
            "warmup_sessions": 115,
            "train_sessions": 504,
            "eval_sessions": 63,
            "folds": 4,
        },
        "costs": {"slip_bps_per_side": 5, "cash_interest": 0},
        "risk": {"max_drawdown_at_most": 0.25, "min_closed_trades": 100},
        "benchmark": "spy_total_return",
        "budget_hours": 24,
        "seed": 1,
        "locked_validation": None,
        "include_locked_validation": False,
        "comparable_to_frozen": True,
        "note": "Replays the frozen payable-date evaluation. It does not retune the saved selections.",
    }


def baseline_spec() -> dict:
    spec = default_research_spec("fixed-baseline-a")
    spec["strategy"] = "baseline_a"
    spec["grid"] = {"ma_win": [70], "max_hold": [30], "atr_mult": [2.0]}
    spec["schedule"] = {
        "anchor": "2026-10-14",
        "warmup_sessions": 85,
        "train_sessions": 0,
        "eval_sessions": 365,
        "folds": 1,
    }
    return spec


def _positive_int(spec: dict, group: str, key: str) -> int:
    try:
        value = int(spec[group][key])
    except (KeyError, TypeError, ValueError) as exc:
        raise SpecError(f"{group}.{key} must be an integer") from exc
    if value < 0:
        raise SpecError(f"{group}.{key} cannot be negative")
    return value


def validate_spec(spec: dict, calendar: list[str] | None = None) -> dict:
    """Return a normalized spec or raise SpecError. Does not change dates to make it fit."""
    if not isinstance(spec, dict):
        raise SpecError("specification must be an object")
    strategy = spec.get("strategy")
    if strategy not in STRATEGIES:
        if strategy in ("cross_sectional_momentum", "absolute_trend"):
            raise SpecError(
                f"{strategy} is a prepared specification. It is not queued. "
                "The data-expansion gates for a new market regime are not met, and the book has not been scored."
            )
        raise SpecError(f"strategy must be one of {', '.join(STRATEGIES)}")
    if spec.get("accounting") != "payable_date":
        raise SpecError("new experiments use payable-date cash. The saved baseline artifact is a separate read-only reproduction.")
    if spec.get("benchmark") != "spy_total_return":
        raise SpecError("benchmark must be spy_total_return")
    costs = spec.get("costs") or {}
    slip = costs.get("slip_bps_per_side")
    if slip not in (5, 10):
        raise SpecError("costs.slip_bps_per_side must be 5 or 10")
    if costs.get("cash_interest") not in (0, 0.0):
        raise SpecError("cash interest is fixed at zero in this engine")
    budget = spec.get("budget_hours")
    if budget not in (24, 48) and not (isinstance(budget, (int, float)) and 0 < float(budget) <= 48):
        raise SpecError("budget_hours must be a positive number up to 48")
    if strategy == "frozen_selector":
        frozen = frozen_selector_spec()
        for key in ("grid", "schedule", "risk", "accounting", "costs"):
            if spec.get(key) != frozen[key]:
                raise SpecError(
                    f"frozen_selector.{key} does not match the frozen specification. "
                    "Tweaks require strategy research_grid and a new experiment id."
                )
    if strategy == "baseline_a":
        grid = spec.get("grid") or {}
        if grid.get("ma_win") != [70] or grid.get("max_hold") != [30] or grid.get("atr_mult") != [2.0]:
            raise SpecError("baseline_a is fixed at 70/30/2.0. A different triple is a research_grid experiment.")
    grid = spec.get("grid") or {}
    for key in ("ma_win", "max_hold", "atr_mult"):
        values = grid.get(key)
        if not isinstance(values, list) or not values:
            raise SpecError(f"grid.{key} must be a non-empty list")
    schedule = spec.get("schedule") or {}
    warmup = _positive_int(spec, "schedule", "warmup_sessions")
    train = _positive_int(spec, "schedule", "train_sessions")
    step = _positive_int(spec, "schedule", "eval_sessions")
    folds = _positive_int(spec, "schedule", "folds")
    if step < 1 or folds < 1:
        raise SpecError("evaluation needs at least one session and one fold")
    if strategy == "research_grid" and train < 1:
        raise SpecError("research_grid training must contain at least one session")
    anchor = schedule.get("anchor")
    if not isinstance(anchor, str):
        raise SpecError("schedule.anchor must be a session date")
    longest_ma = max(int(v) for v in grid["ma_win"])
    if warmup < longest_ma + 15:
        raise SpecError(
            f"warmup_sessions {warmup} is shorter than the longest moving average ({longest_ma}) plus 15. "
            "The window was not shortened to fit."
        )
    lock = spec.get("locked_validation")
    if lock is not None:
        if not isinstance(lock, dict) or "start" not in lock or "end" not in lock:
            raise SpecError("locked_validation needs start and end")
        if lock["start"] > lock["end"]:
            raise SpecError("locked_validation start is after its end")
    if calendar is not None:
        resolve_windows(spec, calendar)
    normalized = deepcopy(spec)
    normalized["data_role"] = "development"
    normalized["untouched_range"] = UNTOUCHED_RANGE
    normalized["untouched_reason"] = UNTOUCHED_REASON
    normalized["development_spans"] = list(DEVELOPMENT_SPANS)
    return normalized


def resolve_windows(spec: dict, calendar: list[str]) -> dict:
    """Concrete session lists ending on the declared anchor. Missing history is an error."""
    if not calendar:
        raise SpecError("the snapshot calendar is empty")
    anchor = spec["schedule"]["anchor"]
    if anchor not in calendar:
        raise SpecError(
            f"anchor {anchor} is not a session in the snapshot. "
            f"The snapshot runs from {calendar[0]} through {calendar[-1]}. No substitute date was used."
        )
    history = [day for day in calendar if day <= anchor]
    warmup = int(spec["schedule"]["warmup_sessions"])
    train = int(spec["schedule"]["train_sessions"])
    step = int(spec["schedule"]["eval_sessions"])
    folds_n = int(spec["schedule"]["folds"])
    need = warmup + train + folds_n * step
    if len(history) < need:
        raise SpecError(
            f"need {need} sessions through {anchor} ({warmup} warm-up, {train} train, {folds_n} fold(s) of {step}). "
            f"The snapshot has {len(history)}. The anchor was not moved and the fold was not shortened."
        )
    span = history[-need:]
    eval_start = warmup + train
    folds = []
    for i in range(folds_n):
        ev = span[eval_start + i * step : eval_start + (i + 1) * step]
        origin = history.index(ev[0])
        scored = history[origin - train : origin] if train else []
        hist = history[origin - train - warmup : origin]
        if ev[0] <= hist[-1]:
            raise SpecError("training dates overlap the evaluation fold")
        folds.append({"fold": i + 1, "eval": ev, "train_scored": scored, "train_dates": hist, "cutoff": hist[-1]})
    lock = spec.get("locked_validation")
    eval_dates = [day for fold in folds for day in fold["eval"]]
    if lock and not spec.get("include_locked_validation"):
        hit = [day for day in eval_dates if lock["start"] <= day <= lock["end"]]
        if hit:
            raise SpecError(
                f"evaluation overlaps the locked validation range {lock['start']} to {lock['end']} "
                f"on {hit[0]}. Those dates stay out of parameter search."
            )
    return {
        "span": span,
        "folds": folds,
        "eval_dates": eval_dates,
        "eval_history": history[history.index(folds[0]["eval"][0]) - warmup : history.index(folds[-1]["eval"][-1]) + 1],
    }


def planned_rows(spec: dict) -> int:
    """Training rows plus one walk-forward evaluation. The frozen selector is one replay."""
    if spec["strategy"] == "frozen_selector":
        return 1
    return len(candidates(spec)) * int(spec["schedule"]["folds"]) + 1


def candidates(spec: dict) -> list[dict]:
    rows = []
    order = 0
    for ma in spec["grid"]["ma_win"]:
        for hold in spec["grid"]["max_hold"]:
            for mult in spec["grid"]["atr_mult"]:
                rows.append({"order": order, "ma_win": int(ma), "max_hold": int(hold), "atr_mult": float(mult)})
                order += 1
    return rows
