"""Reconcile a 5 bps book with the same parameters at 10 bps."""

from __future__ import annotations


def _groups(trades: list[dict]) -> dict:
    out: dict[tuple, list] = {}
    for trade in trades:
        if trade["reason"] == "distribution_fraction":
            key = (trade["symbol"], trade["entry_date"], trade["exit_date"], "distribution_fraction")
        else:
            key = (trade["symbol"], trade["entry_date"], trade["signal_date"])
        out.setdefault(key, []).append(trade)
    return out


def decompose(low: dict, high: dict) -> dict:
    """Explain high-slippage ending equity minus the 5 bps ending equity.

    Slippage is already inside trade P&L. It is not subtracted again.
    """
    left = _groups(low["trade_log"])
    right = _groups(high["trade_log"])
    buckets = {
        "same_trade_price": 0.0,
        "same_entry_different_size": 0.0,
        "same_entry_different_exit": 0.0,
        "only_5bps": 0.0,
        "only_10bps": 0.0,
    }
    counts = {key: 0 for key in buckets}
    examples = {key: [] for key in buckets}
    for key in sorted(set(left) | set(right)):
        a = left.get(key, [])
        b = right.get(key, [])
        if len(a) == 1 and len(b) == 1:
            pnl = b[0]["pnl"] - a[0]["pnl"]
            same_exit = a[0]["exit_date"] == b[0]["exit_date"] and a[0]["reason"] == b[0]["reason"]
            budget_5 = a[0]["shares"] * a[0]["entry_fill"]
            budget_10 = b[0]["shares"] * b[0]["entry_fill"]
            same_budget = abs(budget_5 - budget_10) <= 1.0
            if same_exit and same_budget:
                name = "same_trade_price"
            elif same_exit:
                name = "same_entry_different_size"
            else:
                name = "same_entry_different_exit"
            row = a[0]
        elif a and not b:
            name = "only_5bps"
            pnl = -sum(t["pnl"] for t in a)
            row = a[0]
        elif b and not a:
            name = "only_10bps"
            pnl = sum(t["pnl"] for t in b)
            row = b[0]
        else:
            name = "only_10bps" if len(b) >= len(a) else "only_5bps"
            pnl = sum(t["pnl"] for t in b) - sum(t["pnl"] for t in a)
            row = (b or a)[0]
        buckets[name] += pnl
        counts[name] += 1
        if len(examples[name]) < 5:
            examples[name].append(
                {
                    "symbol": row["symbol"],
                    "entry": row["entry_date"],
                    "pnl_delta": pnl,
                    "reason_5": None if not a else a[0]["reason"],
                    "reason_10": None if not b else b[0]["reason"],
                }
            )
    div_received = high.get("dividend_received", high.get("dividend_dollars", 0.0)) - low.get(
        "dividend_received", low.get("dividend_dollars", 0.0)
    )
    div_unpaid = high.get("dividend_unpaid", 0.0) - low.get("dividend_unpaid", 0.0)
    equity_delta = high["ending_equity"] - low["ending_equity"]
    explained = sum(buckets.values()) + div_received + div_unpaid
    first = None
    for i, (a, b) in enumerate(zip(low["equity_path"], high["equity_path"])):
        if abs(a - b) > 0.01:
            first = {
                "session": low["equity_dates"][i],
                "equity_5bps": a,
                "equity_10bps": b,
                "gap": b - a,
            }
            break
    return {
        "ending_equity_5bps": low["ending_equity"],
        "ending_equity_10bps": high["ending_equity"],
        "equity_delta_10_minus_5": equity_delta,
        "return_delta": high["net_return"] - low["net_return"],
        "cost_dollars_5bps": low["cost_dollars"],
        "cost_dollars_10bps": high["cost_dollars"],
        "cost_delta_not_added_again": high["cost_dollars"] - low["cost_dollars"],
        "buckets": buckets,
        "counts": counts,
        "examples": examples,
        "dividend_received_delta": div_received,
        "dividend_unpaid_delta": div_unpaid,
        "explained_minus_equity_delta": explained - equity_delta,
        "earliest_divergence": first,
        "trades_5bps": low["trades"],
        "trades_10bps": high["trades"],
    }
