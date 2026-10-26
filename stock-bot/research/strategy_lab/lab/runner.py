"""Finite search execution. The budget stops scheduling; it does not discard finished rows."""

from __future__ import annotations

import json
import time

from lab.adapter import evaluate_schedule, prepare, score_training
from lab.specs import SpecError
from lab.jobs import finish, heartbeat, mark_incomplete, record_candidate
from lab.reports import write_exports
from lab.specs import candidates
from lab.store import connect


def _controls(job_id: str, owner: str) -> dict:
    row = heartbeat(job_id, owner)
    if row is not None:
        return row
    conn = connect()
    try:
        current = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    return dict(current) if current else {"cancel_requested": 1, "status": "cancelled"}


def _wait_if_paused(job_id: str, owner: str, deadline: float, sleep) -> str:
    while True:
        row = _controls(job_id, owner)
        if row.get("cancel_requested"):
            return "cancel"
        if row.get("status") != "paused":
            return "run"
        if time.time() >= deadline:
            return "budget"
        sleep(0.05)


def run_plan(job: dict, owner: str, work: list[dict], run_one, *, sleep=time.sleep, now=time.time) -> str:
    """Execute an already-built candidate list. ``run_one`` returns a metrics dict or raises."""
    return _run_plan(job, owner, work, run_one, _load_spec(job), sleep, now)


def _load_spec(job: dict) -> dict:
    conn = connect()
    try:
        row = conn.execute("SELECT spec_json FROM experiments WHERE id = ?", (job["experiment_id"],)).fetchone()
    finally:
        conn.close()
    return json.loads(row["spec_json"])


def _run_plan(job, owner, work, run_one, spec, sleep, now) -> str:
    deadline = now() + float(job["budget_seconds"])
    started = now()
    pilot = None
    required = int(spec["schedule"]["folds"])
    for index, item in enumerate(work):
        if now() >= deadline:
            _stop_rest(job["id"], work[index:], "budget expired before this candidate started", required)
            finish(job["id"], "completed", "budget expired; unfinished candidates are incomplete and are not ranked", pilot_seconds=pilot, eta_seconds=_eta(pilot, len(work)))
            return "completed"
        decision = _wait_if_paused(job["id"], owner, deadline, sleep)
        if decision == "cancel":
            _stop_rest(job["id"], work[index:], "cancelled", required)
            finish(job["id"], "cancelled", "cancelled between candidates", pilot_seconds=pilot)
            return "cancelled"
        if decision == "budget":
            _stop_rest(job["id"], work[index:], "budget expired while paused", required)
            finish(job["id"], "completed", "budget expired", pilot_seconds=pilot)
            return "completed"
        remaining_budget = deadline - now()
        if pilot is not None and (len(work) - index) * pilot > remaining_budget:
            _stop_rest(job["id"], work[index:], "pilot estimate exceeds the remaining budget", required)
            finish(job["id"], "completed", "stopped from the pilot estimate; partial candidates are not ranked", pilot_seconds=pilot, eta_seconds=_eta(pilot, len(work)))
            return "completed"
        t0 = now()
        try:
            metrics = run_one(item)
        except SpecError as exc:
            record_candidate(
                job["id"], item["ordinal"], item["phase"], item["params"], "failed", None, str(exc),
                comparable=False, folds_completed=0, folds_required=required,
            )
            _stop_rest(job["id"], work[index + 1 :], "blocked by the failed candidate's data error", required)
            finish(job["id"], "failed", str(exc), pilot_seconds=pilot)
            return "failed"
        except Exception as exc:
            record_candidate(
                job["id"], item["ordinal"], item["phase"], item["params"], "failed", None, str(exc),
                comparable=False, folds_completed=0, folds_required=required,
            )
            continue
        pilot = pilot if pilot is not None else max(now() - t0, 0.0)
        comparable = item["phase"] == "evaluation" and metrics.get("folds_completed") == metrics.get("folds_required")
        record_candidate(
            job["id"],
            item["ordinal"],
            item["phase"],
            item["params"],
            "complete",
            _stored(metrics),
            None,
            comparable=comparable,
            folds_completed=int(metrics.get("folds_completed") or 0),
            folds_required=int(metrics.get("folds_required") or required),
            pilot_seconds=pilot,
            eta_seconds=(len(work) - index - 1) * pilot,
        )
    finish(job["id"], "completed", None, pilot_seconds=pilot, eta_seconds=now() - started)
    return "completed"


def execute(job: dict, owner: str) -> str:
    spec = dict(_load_spec(job))
    spec["snapshot_id"] = job.get("data_snapshot_id")
    prepared = prepare(spec)
    windows = prepared["windows"]
    grid = candidates(spec)
    if spec["strategy"] == "frozen_selector":
        work = [{"ordinal": 0, "phase": "evaluation", "params": {"source": "frozen_selections"}}]

        def run_one(_item):
            from lab.adapter import reproduce_payable_evaluation

            stats = reproduce_payable_evaluation()
            return {
                "phase": "evaluation",
                "comparable": True,
                "folds_completed": 4,
                "folds_required": 4,
                "net_return": None,
                "ending_equity": stats["selector_equity"],
                "baseline_equity": stats["baseline_equity"],
                "dividend_unpaid": stats["selector_unpaid"],
                "note": "Frozen payable-date replay. Training rows stay in the saved selections file and are not a new ranking.",
            }
    else:
        work = [
            {"ordinal": item["order"], "phase": "training", "params": item, "fold": fold}
            for fold in windows["folds"]
            for item in grid
        ]

        def run_one(item):
            return score_training(spec, item["params"], item["fold"])

    status = _run_plan(job, owner, work, run_one, spec, time.sleep, time.time)
    if status == "completed" and spec["strategy"] != "frozen_selector":
        try:
            schedule = selection_schedule(job["id"], spec, windows, grid)
            metrics = evaluate_schedule(spec, windows, schedule)
            record_candidate(
                job["id"], 0, "evaluation", {"schedule": "declared_rule"}, "complete", _stored(metrics), None,
                comparable=True, folds_completed=int(spec["schedule"]["folds"]), folds_required=int(spec["schedule"]["folds"]),
            )
        except Exception as exc:
            record_candidate(
                job["id"], 0, "evaluation", {}, "failed", None, str(exc),
                comparable=False, folds_completed=0, folds_required=int(spec["schedule"]["folds"]),
            )
            finish(job["id"], "failed", str(exc))
            write_exports(job["id"])
            return "failed"
    write_exports(job["id"])
    return status


def _jsonable(value):
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "item") and not isinstance(value, (bytes, str)):
        try:
            return value.item()
        except Exception:
            return value
    return value


def _stored(metrics: dict) -> dict:
    keep = _jsonable(dict(metrics))
    keep.pop("equity_path", None)
    keep.pop("equity_dates", None)
    return keep


def _eta(pilot: float | None, count: int) -> float | None:
    if pilot is None:
        return None
    return pilot * count


def _stop_rest(job_id: str, items: list[dict], reason: str, folds_required: int) -> None:
    for item in items:
        mark_incomplete(job_id, item["ordinal"], item["phase"], item["params"], reason, folds_required)


def selection_schedule(job_id: str, spec: dict, windows: dict, grid: list[dict]) -> list:
    from select import select_winner

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT metrics_json FROM candidates WHERE job_id = ? AND phase = 'training' AND status = 'complete'",
            (job_id,),
        ).fetchall()
    finally:
        conn.close()
    by_fold: dict[int, list] = {}
    for row in rows:
        metrics = json.loads(row["metrics_json"])
        selection = dict(metrics.get("selection_row") or metrics.get("params") or {})
        by_fold.setdefault(int(metrics["fold"]), []).append(selection)
    dates = windows["eval_history"]
    schedule = []
    for day in dates:
        chosen = None
        for fold in windows["folds"]:
            if day in fold["eval"]:
                pool = by_fold.get(fold["fold"], [])
                winner = select_winner(pool, {"eligibility": spec["risk"]}) if pool else {"choice": "cash", "selected": None}
                chosen = None if winner["choice"] != "parameters" else winner["selected"]
                break
        schedule.append(None if chosen is None else dict(chosen))
    return schedule
