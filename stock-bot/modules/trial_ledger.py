"""Research trial ledger + deflated Sharpe (Bailey & López de Prado, 2014).

Every backtest run that could influence a promote decision appends one JSON
line to ``data/research_trial_ledger.jsonl``. The count and dispersion of
Sharpe ratios in a *family* of trials is what the Deflated Sharpe Ratio needs:
after ``N`` tries the best one is expected to look good by chance, and DSR is
the probability the observed Sharpe beats that expected maximum.

All Sharpe inputs here are **per bar** (daily), not annualised.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LEDGER_PATH = Path(os.getenv("RESEARCH_TRIAL_LEDGER", str(ROOT / "data" / "research_trial_ledger.jsonl")))
_EULER_GAMMA = 0.5772156649015329
_RESULT_KEYS = (
    "book_ret_pct",
    "max_dd_pct",
    "max_dd_booked_pct",
    "trades",
    "win_rate",
    "sharpe",
    "sharpe_daily",
    "n_days",
    "daily_skew",
    "daily_kurt",
    "symbols",
    "start",
    "end",
    "cost_rt_bps",
)


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def record_trial(
    *,
    script: str,
    family: str,
    label: str,
    window: str,
    params: dict | None,
    result: dict,
    note: str = "",
    path: Path = LEDGER_PATH,
) -> dict:
    """Append one trial. Returns the row written."""
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "script": script,
        "family": family,
        "label": label,
        "window": window,
        "params": {k: _jsonable(v) for k, v in (params or {}).items()},
        "note": note,
    }
    for key in _RESULT_KEYS:
        if key in result:
            row[key] = _jsonable(result[key])
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    return row


def load_trials(path: Path = LEDGER_PATH, *, family: str | None = None) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if family is None or row.get("family") == family:
                out.append(row)
    return out


def expected_max_sharpe(n_trials: int, sr_var: float) -> float:
    """E[max SR] across ``n_trials`` independent zero-skill trials (per-bar units)."""
    n = int(n_trials)
    if n <= 1 or not sr_var or sr_var <= 0:
        return 0.0
    nd = NormalDist()
    return math.sqrt(sr_var) * (
        (1.0 - _EULER_GAMMA) * nd.inv_cdf(1.0 - 1.0 / n)
        + _EULER_GAMMA * nd.inv_cdf(1.0 - 1.0 / (n * math.e))
    )


def probabilistic_sharpe(
    sr: float, *, sr_benchmark: float, n_obs: int, skew: float = 0.0, kurt: float = 3.0
) -> float:
    """P[true SR > benchmark] given the observed per-bar SR and its return moments."""
    if n_obs is None or n_obs < 3:
        return float("nan")
    inner = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr * sr
    if inner <= 0:
        return float("nan")
    z = (sr - sr_benchmark) * math.sqrt(n_obs - 1.0) / math.sqrt(inner)
    return NormalDist().cdf(z)


def deflated_sharpe(
    sr: float,
    *,
    n_trials: int,
    sr_var: float,
    n_obs: int,
    skew: float = 0.0,
    kurt: float = 3.0,
) -> dict:
    sr0 = expected_max_sharpe(n_trials, sr_var)
    dsr = probabilistic_sharpe(sr, sr_benchmark=sr0, n_obs=n_obs, skew=skew, kurt=kurt)
    return {
        "sharpe_daily": sr,
        "sharpe_annual": round(sr * math.sqrt(252.0), 3),
        "n_trials": int(n_trials),
        "sr0_daily": round(sr0, 5),
        "sr0_annual": round(sr0 * math.sqrt(252.0), 3),
        "n_obs": int(n_obs),
        "dsr": round(dsr, 4) if dsr == dsr else None,
        "psr_vs_zero": round(probabilistic_sharpe(sr, sr_benchmark=0.0, n_obs=n_obs, skew=skew, kurt=kurt), 4),
    }


def family_sharpe_dispersion(trials: list[dict]) -> tuple[int, float]:
    """(trial count, variance of per-bar Sharpe) over trials that reported one."""
    vals = [float(t["sharpe_daily"]) for t in trials if t.get("sharpe_daily") is not None]
    n = len(vals)
    if n < 2:
        return n, 0.0
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    return n, var


def deflate_trial(trial: dict, family_trials: list[dict]) -> dict | None:
    """DSR for one trial against every trial recorded in its family."""
    sr = trial.get("sharpe_daily")
    n_obs = trial.get("n_days")
    if sr is None or not n_obs:
        return None
    n, var = family_sharpe_dispersion(family_trials)
    return deflated_sharpe(
        float(sr),
        n_trials=max(1, n),
        sr_var=var,
        n_obs=int(n_obs),
        skew=float(trial.get("daily_skew") or 0.0),
        kurt=float(trial.get("daily_kurt") or 3.0),
    )
