"""Pass and kill rules copied from the frozen preregistration. No extra gates."""

from __future__ import annotations

import random

REQUIRED_WINDOWS = ("prior_365", "recent_365")
COST_KEYS = ("slip_5", "slip_10")
DRAWDOWN_SLACK = 0.02
SUSPECT_ABS_RETURN = 5.0


def bootstrap_total_returns(daily_returns: list[float], n: int = 200, seed: int = 0) -> dict:
    if not daily_returns:
        return {"paths": 0, "mean": None, "median": None, "p05": None}
    rng = random.Random(seed)
    width = len(daily_returns)
    totals = []
    for _ in range(n):
        acc = 1.0
        for _step in range(width):
            acc *= 1.0 + daily_returns[rng.randrange(width)]
        totals.append(acc - 1.0)
    totals.sort()
    return {
        "paths": n,
        "mean": totals and sum(totals) / n,
        "median": totals[n // 2],
        "p05": totals[int(n * 0.05)],
    }


def _fail_drawdown(learner: float, baseline: float) -> bool:
    worse_pp = round((learner - baseline) * 100, 4)
    return worse_pp > DRAWDOWN_SLACK * 100


def window_failures(block: dict) -> list[str]:
    """Failures for one window at one cost. Empty means that cell passed."""
    learner = block["learner"]
    baseline = block["baseline"]
    reasons: list[str] = []
    if abs(learner["return"]) > SUSPECT_ABS_RETURN or abs(baseline["return"]) > SUSPECT_ABS_RETURN:
        reasons.append("implementation_suspect")
    if not learner["return"] > baseline["return"]:
        reasons.append("return")
    if _fail_drawdown(learner["max_drawdown"], baseline["max_drawdown"]):
        reasons.append("drawdown")
    if not learner["first_half"] > baseline["first_half"]:
        reasons.append("first_half")
    if not learner["second_half"] > baseline["second_half"]:
        reasons.append("second_half")
    if learner["return"] < block["equal_weight_hold"]["return"]:
        reasons.append("equal_weight_hold")
    if learner["return"] < block["vti_hold"]["return"]:
        reasons.append("vti_hold")
    monday = block.get("monday")
    if (
        learner["return"] > baseline["return"]
        and monday is not None
        and not monday["return"] > baseline["return"]
    ):
        reasons.append("monday_entries")
    boot = learner.get("bootstrap") or {}
    median = boot.get("median")
    if median is not None and median < 0 and learner["return"] > baseline["return"]:
        reasons.append("bootstrap_median")
    return reasons


def study_status(windows: dict) -> dict:
    cells = []
    failed = False
    for name in REQUIRED_WINDOWS:
        for cost in COST_KEYS:
            reasons = window_failures(windows[name][cost])
            cells.append({"window": name, "cost": cost, "failures": reasons, "passed": not reasons})
            failed = failed or bool(reasons)
    return {
        "status": "reject" if failed else "pass",
        "promotion": False,
        "cells": cells,
    }
