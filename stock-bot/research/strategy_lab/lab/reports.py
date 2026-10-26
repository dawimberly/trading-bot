"""CSV, JSON, and a short Markdown report. Training rows are not called best."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from lab.jobs import add_artifact, job_detail
from lab.paths import EXPORTS


def result_tables(job_id: str) -> dict:
    detail = job_detail(job_id)
    if detail is None:
        raise KeyError(job_id)

    def frame(rows: list[dict]):
        import pandas as pd

        flat = []
        for row in rows:
            metrics = json.loads(row["metrics_json"]) if row["metrics_json"] else {}
            flat.append(
                {
                    "phase": row["phase"],
                    "ordinal": row["ordinal"],
                    "status": row["status"],
                    "comparable": bool(row["comparable"]),
                    "net_return": metrics.get("net_return"),
                    "ending_equity": metrics.get("ending_equity"),
                    "max_drawdown": metrics.get("max_drawdown"),
                    "ann_vol": metrics.get("ann_vol"),
                    "avg_exposure": metrics.get("avg_exposure"),
                    "turnover": metrics.get("turnover"),
                    "cost_dollars": metrics.get("cost_dollars"),
                    "trades": metrics.get("trades"),
                    "excess_return_vs_spy": metrics.get("excess_return_vs_spy"),
                    "dividend_unpaid": metrics.get("dividend_unpaid"),
                    "error": row["error"],
                }
            )
        return pd.DataFrame(flat)

    evaluation = frame([row for row in detail["candidates"] if row["phase"] == "evaluation" and row["comparable"]])
    training = frame([row for row in detail["candidates"] if row["phase"] == "training" and row["status"] == "complete"])
    other = frame([row for row in detail["candidates"] if row["status"] != "complete" or (row["phase"] == "evaluation" and not row["comparable"])])
    return {"evaluation": evaluation, "training": training, "other": other, "equity": None}


def write_exports(job_id: str) -> dict:
    detail = job_detail(job_id)
    if detail is None:
        raise KeyError(job_id)
    folder = EXPORTS / job_id
    folder.mkdir(parents=True, exist_ok=True)
    json_path = folder / "results.json"
    csv_path = folder / "results.csv"
    md_path = folder / "report.md"
    payload = {
        "job": detail["job"],
        "experiment": detail["experiment"],
        "candidates": detail["candidates"],
        "ranking_note": "Training rows are not walk-forward results. Incomplete candidates are not comparable.",
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["phase", "ordinal", "status", "comparable", "folds_completed", "folds_required", "net_return", "ending_equity", "max_drawdown", "error"],
        )
        writer.writeheader()
        for row in detail["candidates"]:
            metrics = json.loads(row["metrics_json"]) if row["metrics_json"] else {}
            writer.writerow(
                {
                    "phase": row["phase"],
                    "ordinal": row["ordinal"],
                    "status": row["status"],
                    "comparable": row["comparable"],
                    "folds_completed": row["folds_completed"],
                    "folds_required": row["folds_required"],
                    "net_return": metrics.get("net_return"),
                    "ending_equity": metrics.get("ending_equity"),
                    "max_drawdown": metrics.get("max_drawdown"),
                    "error": row["error"],
                }
            )
    md_path.write_text(_markdown(detail), encoding="utf-8")
    add_artifact(job_id, "json", str(json_path))
    add_artifact(job_id, "csv", str(csv_path))
    add_artifact(job_id, "markdown", str(md_path))
    return {"json": str(json_path), "csv": str(csv_path), "markdown": str(md_path)}


def _markdown(detail: dict) -> str:
    spec = json.loads(detail["experiment"]["spec_json"])
    job = detail["job"]
    lines = [
        f"# Research report {job['id'][:8]}",
        "",
        f"Strategy `{spec['strategy']}` on development data. This is not a production approval and not a best-strategy label.",
        "",
        spec.get("untouched_reason", ""),
        "",
        f"Status: {job['status']}. Completed candidates: {job['candidates_done']} of {job['candidates_total']}.",
        "",
        "## Walk-forward evaluation",
        "",
    ]
    eval_rows = [row for row in detail["candidates"] if row["phase"] == "evaluation" and row["comparable"]]
    if not eval_rows:
        lines.append("No fully evaluated walk-forward result.")
    for row in eval_rows:
        metrics = json.loads(row["metrics_json"])
        lines.append(
            f"Return {metrics.get('net_return')}, equity {metrics.get('ending_equity')}, "
            f"drawdown {metrics.get('max_drawdown')}, trades {metrics.get('trades')}, "
            f"excess versus SPY {metrics.get('excess_return_vs_spy')}."
        )
    lines.extend(["", "## Training rankings", "", "These scores chose parameters. They are not the evaluation.", ""])
    for row in detail["candidates"]:
        if row["phase"] != "training":
            continue
        metrics = json.loads(row["metrics_json"]) if row["metrics_json"] else {}
        lines.append(f"- order {row['ordinal']} status {row['status']} return {metrics.get('net_return')} eligible {metrics.get('eligible')}")
    incomplete = [row for row in detail["candidates"] if row["status"] == "incomplete"]
    if incomplete:
        lines.extend(["", "## Incomplete", "", "Not ranked with finished evaluations.", ""])
        for row in incomplete:
            lines.append(f"- {row['phase']} {row['ordinal']}: {row['error']}")
    lines.append("")
    return "\n".join(lines)
