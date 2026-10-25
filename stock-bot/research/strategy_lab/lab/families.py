"""Specifications for two families. They are not scored and cannot be queued yet."""

from __future__ import annotations

# Steady-state time of one MA training score on this snapshot: 619 sessions, measured locally.
PILOT_SECONDS = 23.85
PILOT_NOTE = (
    "The second consecutive MA training score took 23.85 seconds "
    "(619 sessions, 115 of them warm-up). These new signals have not been scored. "
    "The estimate multiplies that measurement by the candidate count because each candidate "
    "scans the same point-in-time panel once per session. The evaluation window is shorter, "
    "so the total is a conservative upper bound."
)

BOOK = {
    "accounting": "payable_date",
    "entries": "next open after the decision close",
    "max_names": 15,
    "sleeve": 0.95,
    "initial_cash": 100_000.0,
    "cash_interest": 0,
    "slip_bps_per_side": 5,
    "atr_period": 14,
    "atr_mult": 2.0,
    "benchmark": "spy_total_return",
    "selection": (
        "Eligible when max drawdown is at most 25 percent and closed trades are at least 100. "
        "The winner is the highest training net return. Ties break to lower drawdown, then lower turnover, "
        "then earlier parameter order. If none is eligible, the evaluation fold queues no new entries."
    ),
    "schedule": {
        "anchor": "2026-10-14",
        "train_sessions": 504,
        "eval_sessions": 63,
        "folds": 1,
    },
}


def _family(name: str, signal: str, grid: dict, warmup: int, warmup_rule: str, distinct: str) -> dict:
    count = 1
    for values in grid.values():
        count *= len(values)
    need = warmup + BOOK["schedule"]["train_sessions"] + BOOK["schedule"]["folds"] * BOOK["schedule"]["eval_sessions"]
    four_fold = warmup + BOOK["schedule"]["train_sessions"] + 4 * BOOK["schedule"]["eval_sessions"]
    return {
        "family": name,
        "status": "specification_only",
        "queue": False,
        "signal": signal,
        "grid": grid,
        "candidates": count,
        "warmup_sessions": warmup,
        "warmup_rule": warmup_rule,
        "distinct": distinct,
        "book": BOOK,
        "sessions_for_one_fold": need,
        "sessions_for_four_folds": four_fold,
        "planned_rows": count + 1,
        "estimated_seconds": round((count + 1) * PILOT_SECONDS, 1),
    }


def specifications() -> list[dict]:
    momentum = _family(
        "cross_sectional_momentum",
        "Trailing close-to-close return over L sessions, skipping the most recent 21. "
        "At a rebalance, buy up to 15 point-in-time members with a positive trailing return, highest first, then ticker. "
        "The ATR stop is the existing execution stop. Rebalance is every 21 sessions.",
        {"lookback": [63, 126, 252]},
        273,
        "Longest lookback (252) plus the fixed 21-session skip. The lookback is not shortened when history is short.",
        "Ranking is trailing return, not distance to a moving average. The 21-session skip and the positive-return entry are the only signal rules.",
    )
    trend = _family(
        "absolute_trend",
        "Enter when the close is above its simple moving average. Exit when the close is back at or below that average, "
        "or when the ATR stop is touched. There is no holding-period clock. Rank eligible names by close divided by the average, minus one.",
        {"trend_window": [50, 100, 200]},
        215,
        "Longest window (200) plus the 15-session buffer the cash engine already uses. The window is not shortened.",
        "Baseline A exits on a holding limit or the ATR stop. It does not exit because the close fell through the average. This family does.",
    )
    return [momentum, trend]


def feasibility(sessions: int, first_session: str, last_session: str) -> list[dict]:
    rows = []
    for spec in specifications():
        one = sessions >= spec["sessions_for_one_fold"]
        four = sessions >= spec["sessions_for_four_folds"]
        if one:
            fit = (
                f"One fold fits in {sessions} sessions ({first_session} to {last_session}). "
                "That span is development data already used by the cash studies."
            )
        else:
            fit = (
                f"Need {spec['sessions_for_one_fold']} sessions and the snapshot has {sessions}. "
                "The window was not shortened."
            )
        rows.append(
            {
                "family": spec["family"],
                "candidates": spec["candidates"],
                "warmup_sessions": spec["warmup_sessions"],
                "one_fold": "fits" if one else "blocked",
                "four_folds": "fits" if four else "blocked",
                "sessions_for_one_fold": spec["sessions_for_one_fold"],
                "sessions_for_four_folds": spec["sessions_for_four_folds"],
                "estimated_seconds": spec["estimated_seconds"],
                "data": fit,
                "regimes": "No added regime. History before the snapshot is absent.",
                "queue": "no",
            }
        )
    return rows
