"""Match a proposed search to results that were already stored. Read-only."""

from __future__ import annotations

import json

from lab.paths import PAYABLE_EVAL, PAYABLE_SELECTIONS
from lab.specs import FROZEN_GRID, resolve_windows

# The one-fold grid suggested after the payable-date correction. It is not a new search.
PROPOSED_ONE_FOLD = {
    "strategy": "research_grid",
    "accounting": "payable_date",
    "benchmark": "spy_total_return",
    "grid": FROZEN_GRID,
    "schedule": {
        "anchor": "2026-10-14",
        "warmup_sessions": 115,
        "train_sessions": 504,
        "eval_sessions": 63,
        "folds": 1,
    },
    "costs": {"slip_bps_per_side": 5, "cash_interest": 0},
    "risk": {"max_drawdown_at_most": 0.25, "min_closed_trades": 100},
}


class DuplicateSearch(ValueError):
    """The proposal repeats a completed training search."""

    def __init__(self, report: dict):
        self.report = report
        super().__init__(report["message"])


def proposed_one_fold_spec(name: str = "frozen-grid-one-fold") -> dict:
    spec = json.loads(json.dumps(PROPOSED_ONE_FOLD))
    spec["name"] = name
    spec["budget_hours"] = 24
    spec["seed"] = 1
    spec["locked_validation"] = None
    spec["include_locked_validation"] = False
    spec["comparable_to_frozen"] = False
    return spec


def _grid_key(grid: dict) -> tuple:
    return (
        tuple(int(v) for v in grid["ma_win"]),
        tuple(int(v) for v in grid["max_hold"]),
        tuple(float(v) for v in grid["atr_mult"]),
    )


def _same_rules(spec: dict) -> bool:
    costs = spec.get("costs") or {}
    risk = spec.get("risk") or {}
    return (
        spec.get("strategy") == "research_grid"
        and spec.get("accounting") == "payable_date"
        and spec.get("benchmark") == "spy_total_return"
        and _grid_key(spec.get("grid") or {}) == _grid_key(FROZEN_GRID)
        and costs.get("slip_bps_per_side") == 5
        and costs.get("cash_interest") in (0, 0.0)
        and float(risk.get("max_drawdown_at_most")) == 0.25
        and int(risk.get("min_closed_trades")) == 100
    )


def load_completed_folds() -> dict:
    """Payable-date walk-forward results. The files are not rewritten."""
    choices = json.loads(PAYABLE_SELECTIONS.read_text(encoding="utf-8"))
    evaluation = json.loads(PAYABLE_EVAL.read_text(encoding="utf-8"))
    folds = []
    for block in choices:
        rows = block["candidates"]
        fold_no = int(rows[0]["fold"])
        selected = block["selected"]
        segment = next(item for item in evaluation["folds"]["selector"] if item["fold"] == fold_no)
        baseline = next(item for item in evaluation["folds"]["baseline_A"] if item["fold"] == fold_no)
        spy = next(item for item in evaluation["folds"]["spy_total_return"] if item["fold"] == fold_no)
        folds.append(
            {
                "fold": fold_no,
                "train_start": rows[0]["train_start"],
                "train_end": rows[0]["train_end"],
                "eval_start": segment["start"],
                "eval_end": segment["end"],
                "selected": {
                    "ma_win": selected["ma_win"],
                    "max_hold": selected["max_hold"],
                    "atr_mult": selected["atr_mult"],
                    "net_return": selected.get("net_return"),
                    "max_drawdown": selected.get("max_drawdown"),
                    "trades": selected.get("trades"),
                },
                "training": [
                    {
                        "order": row["order"],
                        "ma_win": row["ma_win"],
                        "max_hold": row["max_hold"],
                        "atr_mult": row["atr_mult"],
                        "net_return": row["net_return"],
                        "max_drawdown": row["max_drawdown"],
                        "trades": row["trades"],
                        "eligible": row["eligible"],
                        "selected": (
                            row["ma_win"] == selected["ma_win"]
                            and row["max_hold"] == selected["max_hold"]
                            and row["atr_mult"] == selected["atr_mult"]
                        ),
                    }
                    for row in rows
                ],
                "stitched_segment": {
                    "entering_equity": segment["entering_equity"],
                    "ending_equity": segment["ending_equity"],
                    "net_return": segment["net_return"],
                    "baseline_a_net_return": baseline["net_return"],
                    "spy_net_return": spy["net_return"],
                    "fresh_book": segment["entering_equity"] == 100000.0,
                },
            }
        )
    return {"folds": folds, "aggregate": evaluation["aggregate"]}


def find_duplicate(spec: dict, calendar: list[str]) -> dict | None:
    """Return the completed fold this spec would rescore, or None."""
    if not _same_rules(spec):
        return None
    if int(spec["schedule"]["folds"]) != 1:
        return None
    windows = resolve_windows(spec, calendar)
    fold = windows["folds"][0]
    train = fold["train_scored"]
    evaluation = fold["eval"]
    for stored in load_completed_folds()["folds"]:
        if train[0] == stored["train_start"] and train[-1] == stored["train_end"] and evaluation[0] == stored["eval_start"] and evaluation[-1] == stored["eval_end"]:
            message = (
                f"This one-fold grid repeats fold {stored['fold']} of the completed payable-date "
                f"walk-forward. Training {stored['train_start']} through {stored['train_end']} and "
                f"evaluation {stored['eval_start']} through {stored['eval_end']} are already stored. "
                "The search was not queued."
            )
            return {
                "duplicates": True,
                "fold": stored,
                "message": message,
                "source": str(PAYABLE_SELECTIONS),
            }
    return None
