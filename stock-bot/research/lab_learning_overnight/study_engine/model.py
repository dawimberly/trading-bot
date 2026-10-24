"""Causal ranking model. It scores eligible entries. It does not change exits or risk."""
from __future__ import annotations

import hashlib
import math

FEATURES = ("ma50_extension", "vol50", "rsi14")
FEATURE_DEF = (
    "51 contiguous completed regular-session five-minute CLOSES. "
    "ma50_extension = last/mean(prior 50 closes)-1. "
    "vol50 = sample standard deviation of the 50 simple close-to-close returns. "
    "rsi14 = simple 14-close RSI/100. No intrabar high or low."
)
FEATURE_HASH = hashlib.sha256(FEATURE_DEF.encode()).hexdigest()


def closes_of(history) -> list[float]:
    if not history:
        return []
    if isinstance(history[0], (int, float)):
        return [float(v) for v in history]
    return [float(item[1]) for item in history]


def features(history) -> list[float] | None:
    prices = closes_of(history)
    if len(prices) < 51:
        return None
    window = prices[-51:]
    changes = [b / a - 1 for a, b in zip(window, window[1:])]
    avg = sum(window[-50:]) / 50
    mu = sum(changes) / 50
    vol = math.sqrt(sum((r - mu) ** 2 for r in changes) / 49)
    diffs = [b - a for a, b in zip(window[-15:], window[-14:])]
    gain = sum(max(x, 0) for x in diffs) / 14
    loss = sum(max(-x, 0) for x in diffs) / 14
    if gain == loss == 0:
        rsi = 50.0
    elif loss == 0:
        rsi = 100.0
    else:
        rsi = 100 - 100 / (1 + gain / loss)
    return [window[-1] / avg - 1, vol, rsi / 100]


def fit(rows: list[dict]) -> dict:
    n = len(rows)
    means = [sum(r["x"][k] for r in rows) / n for k in range(3)]
    scales = [
        max(1e-4, math.sqrt(sum((r["x"][k] - means[k]) ** 2 for r in rows) / n))
        for k in range(3)
    ]
    scaled = [
        [max(-5, min(5, (r["x"][k] - means[k]) / scales[k])) for k in range(3)]
        for r in rows
    ]
    bias = sum(r["y"] for r in rows) / n
    weights = [0.0, 0.0, 0.0]
    for _ in range(300):
        err = [
            bias + sum(w * v for w, v in zip(weights, x)) - r["y"]
            for x, r in zip(scaled, rows)
        ]
        bias -= 0.02 * sum(err) / n
        weights = [
            w - 0.02 * (sum(e * x[k] for e, x in zip(err, scaled)) / n + 0.1 * w)
            for k, w in enumerate(weights)
        ]
    return {
        "means": means,
        "scales": scales,
        "weights": weights,
        "bias": bias,
        "n": n,
        "features": list(FEATURES),
        "feature_hash": FEATURE_HASH,
    }


def predict(model: dict, values: list[float]) -> float:
    scaled = [
        max(-5, min(5, (v - m) / s))
        for v, m, s in zip(values, model["means"], model["scales"])
    ]
    return model["bias"] + sum(w * v for w, v in zip(model["weights"], scaled))


def learn(rows: list[dict]) -> tuple[dict | None, dict]:
    """Chronological exit-date split. Acceptance is prediction MAE only, not P&L."""
    days = sorted({r["exit_session"] for r in rows})
    info = {
        "status": "insufficient_sample",
        "samples": len(rows),
        "exit_sessions": len(days),
        "feature_hash": FEATURE_HASH,
        "feature_def": FEATURE_DEF,
        "completed_trade": (
            "baseline virtual round trip whose exit timestamp is already known; "
            "label is net return after entry and exit slippage"
        ),
    }
    if len(rows) < 30 or len(days) < 10:
        return None, info
    cut = days[max(1, int(len(days) * 0.6)) - 1]
    train = [r for r in rows if r["exit_session"] <= cut]
    valid = [r for r in rows if r["exit_session"] > cut]
    valid_days = sorted({r["exit_session"] for r in valid})
    info.update(
        cutoff=cut,
        train=len(train),
        validation=len(valid),
        train_start=train[0]["exit_session"] if train else None,
        train_end=train[-1]["exit_session"] if train else None,
        validation_start=valid_days[0] if valid_days else None,
        validation_end=valid_days[-1] if valid_days else None,
        validation_dates=len(valid_days),
    )
    if len(train) < 20 or len(valid) < 10 or len(valid_days) < 2:
        info["status"] = "insufficient_split"
        return None, info
    trained = fit(train)
    constant = sum(r["y"] for r in train) / len(train)
    mae = sum(abs(predict(trained, r["x"]) - r["y"]) for r in valid) / len(valid)
    null = sum(abs(constant - r["y"]) for r in valid) / len(valid)
    accepted = mae < null
    info.update(
        status="accepted_prediction_model" if accepted else "rejected_prediction_model",
        mae=mae,
        constant_mae=null,
        constant_prediction=constant,
    )
    if not accepted:
        return None, info
    full = fit(rows)
    info["parameters"] = {
        "weights": full["weights"],
        "bias": full["bias"],
        "means": full["means"],
        "scales": full["scales"],
        "n": full["n"],
    }
    return full, info
