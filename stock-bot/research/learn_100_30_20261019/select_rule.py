"""Training selection. Frozen before evaluation results."""

from __future__ import annotations

from plan import candidate_id

NON_STRATEGY_REASONS = frozenset(
    {"distribution_fraction", "merger_fraction", "final", "final_stale"}
)


def completed_trades(trades: list[dict]) -> list[dict]:
    return [row for row in trades if row.get("reason") not in NON_STRATEGY_REASONS]


def reject_reason(row: dict, rules: dict) -> str | None:
    if not row.get("reconciled"):
        return "unreconciled"
    if row.get("distribution_unpriced"):
        return "unpriced_distribution"
    if row.get("unresolved_holdings"):
        return "unresolved_holding"
    if row.get("stale_terminals") or row.get("stale_open_marks"):
        return "stale_price"
    if row.get("held_action"):
        return "held_action"
    if row.get("valuation_inconclusive"):
        return "valuation_inconclusive"
    if float(row["max_drawdown"]) > float(rules["max_drawdown"]) + 1e-12:
        return "drawdown"
    if int(row["completed_round_trips"]) < int(rules["min_completed_round_trips"]):
        return "trade_count"
    return None


def select_winner(rows: list[dict], rules: dict) -> dict:
    """Highest training net return. Ties: lower drawdown, lower turnover, lower order.

    If nobody is eligible, keep the fixed baseline and record the fallback.
    `eval_return` is ignored when present.
    """
    annotated = []
    for row in rows:
        item = {key: value for key, value in row.items() if key != "eval_return"}
        reason = reject_reason(item, rules)
        item["eligible"] = reason is None
        item["reject_reason"] = reason
        item["candidate_id"] = candidate_id(item)
        annotated.append(item)
    pool = [row for row in annotated if row["eligible"]]
    pool.sort(
        key=lambda row: (
            -float(row["net_return"]),
            float(row["max_drawdown"]),
            float(row["turnover"]),
            int(row["order"]),
        )
    )
    base_id = rules["baseline_id"]
    base = next(row for row in annotated if row["candidate_id"] == base_id)
    if not pool:
        return {
            "choice": "baseline_fallback",
            "reason": "no_eligible_candidate",
            "selected": _public(base),
            "eligible_count": 0,
            "candidate_count": len(annotated),
            "candidates": annotated,
        }
    winner = pool[0]
    return {
        "choice": "parameters",
        "reason": None,
        "selected": _public(winner),
        "eligible_count": len(pool),
        "candidate_count": len(annotated),
        "candidates": annotated,
    }


def _public(row: dict) -> dict:
    keys = (
        "order",
        "candidate_id",
        "ma_win",
        "max_hold",
        "atr_mult",
        "net_return",
        "max_drawdown",
        "turnover",
        "completed_round_trips",
        "open_positions",
        "reject_reason",
    )
    return {key: row.get(key) for key in keys}
