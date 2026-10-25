"""local_expectancy only. The other overnight arms stay rejected."""

from __future__ import annotations

import math
from datetime import datetime

NEIGHBORS = 15


def _scale(rows: list[dict]) -> tuple[list[float], list[float]]:
    n = len(rows)
    means = [sum(r["x"][i] for r in rows) / n for i in range(3)]
    scales = [
        max(1e-4, math.sqrt(sum((r["x"][i] - means[i]) ** 2 for r in rows) / n))
        for i in range(3)
    ]
    return means, scales


def _transform(values: list[float], means: list[float], scales: list[float]) -> list[float]:
    return [max(-5.0, min(5.0, (v - m) / s)) for v, m, s in zip(values, means, scales)]


def fit(rows: list[dict]) -> dict:
    means, scales = _scale(rows)
    return {
        "kind": "local_expectancy",
        "means": means,
        "scales": scales,
        "neighbors": [
            {"x": _transform(r["x"], means, scales), "y": r["y"]} for r in rows
        ],
        "n": len(rows),
    }


def predict(model: dict, values: list[float]) -> float:
    point = _transform(values, model["means"], model["scales"])
    nearest = sorted(
        model["neighbors"],
        key=lambda row: sum((a - b) ** 2 for a, b in zip(point, row["x"])),
    )[:NEIGHBORS]
    return sum(row["y"] for row in nearest) / len(nearest)


def learn(rows: list[dict]) -> tuple[dict | None, dict]:
    """Purged exit-date split. Acceptance is MAE against the training mean, not P&L."""
    info = {
        "status": "insufficient_sample",
        "objective": "local_expectancy",
        "samples": len(rows),
        "source": "matured baseline outcomes only",
        "portfolio_promotion": False,
    }
    days = sorted({r["exit_session"] for r in rows})
    if len(rows) < 30 or len(days) < 10:
        return None, info
    cut = days[max(1, int(len(days) * 0.6)) - 1]
    valid = [r for r in rows if r["exit_session"] > cut]
    if not valid:
        return None, info
    earliest = min(valid, key=lambda row: datetime.fromisoformat(row["entry_time"]))["entry_time"]
    train = [
        r
        for r in rows
        if r["exit_session"] <= cut and datetime.fromisoformat(r["exit_time"]) < datetime.fromisoformat(earliest)
    ]
    info.update(
        cutoff=cut,
        train=len(train),
        validation=len(valid),
        purged_validation_entry_cutoff=earliest,
    )
    if len(train) < 20 or len(valid) < 10:
        info["status"] = "insufficient_purged_split"
        return None, info
    trained = fit(train)
    constant = sum(r["y"] for r in train) / len(train)
    metric = sum(abs(predict(trained, r["x"]) - r["y"]) for r in valid) / len(valid)
    null = sum(abs(constant - r["y"]) for r in valid) / len(valid)
    accepted = metric < null
    info.update(
        validation_mae=metric,
        constant_mae=null,
        status="accepted_prediction_model" if accepted else "rejected_prediction_model",
    )
    if not accepted:
        return None, info
    return fit(rows), info
