"""Causal training scores. Open positions are marked and are not completed trades."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ABC = HERE.parent / "nyse_cash_abc"
WFS = HERE.parent / "walk_forward_select"
sys.path.insert(0, str(ABC))
sys.path.insert(0, str(WFS))

from engine import INITIAL_CASH, usable  # noqa: E402
from market import assert_member_isolated, slice_market  # noqa: E402
from plan import WARMUP  # noqa: E402
from run_rotation import action_hits, material_block  # noqa: E402
from select_rule import completed_trades  # noqa: E402
from stitch import simulate_continuous  # noqa: E402


def score_candidate(market: dict, fold: dict, spec: dict, slip: float) -> dict:
    dates = list(fold["train_dates"])
    assert_member_isolated(dates, fold["cutoff"])
    bars, member, spy, actions = slice_market(market, dates)
    if any(day > fold["cutoff"] for day in actions["renames"]):
        raise RuntimeError("rename after cutoff")
    for dist in actions["distributions"]:
        if dist.session > fold["cutoff"]:
            raise RuntimeError("distribution after cutoff")
    schedule = [None if i < WARMUP else dict(spec) for i in range(len(dates))]
    out = simulate_continuous(
        bars,
        member,
        dates,
        spy,
        schedule,
        decision_start=WARMUP,
        dividends=actions["dividends"],
        distributions=actions["distributions"],
        renames=actions["renames"],
        slip=slip,
        initial_cash=INITIAL_CASH,
        liquidate_terminal=False,
        open_book=True,
    )
    if out["end"] != fold["cutoff"] or out["sessions"] != len(dates) - WARMUP:
        raise RuntimeError("training window length drifted")
    if out["sessions"] != int(market["plan"]["train_sessions"]):
        raise RuntimeError("warmup sessions were scored")
    if any(row["reason"] in {"final", "final_stale"} for row in out["trade_log"]):
        raise RuntimeError("training counted a synthetic terminal liquidation")
    row = classify(out, bars, market["actions"], dates, market["lookup"])
    row.update(
        {
            "fold": fold["fold"],
            "order": int(spec["order"]),
            "ma_win": int(spec["ma_win"]),
            "max_hold": int(spec["max_hold"]),
            "atr_mult": float(spec["atr_mult"]),
            "train_start": fold["train_start"],
            "train_end": fold["train_end"],
            "warmup_excluded_sessions": WARMUP,
        }
    )
    return row


def classify(out: dict, bars: dict, actions: list[dict], dates: list[str], lookup: dict) -> dict:
    trades = list(out["trade_log"])
    done = completed_trades(trades)
    positions = list(out.get("positions") or [])
    open_cost = sum(float(row["cost"]) for row in positions)
    trade_pnl = sum(float(row["pnl"]) for row in trades)
    div_received = float(out.get("dividend_received") or 0.0)
    cash_gap = float(out["ending_cash"]) - (INITIAL_CASH + trade_pnl + div_received - open_cost)
    stale_open = []
    marked = 0.0
    for row in positions:
        bar = bars.get(row["symbol"], [None])[-1]
        if not usable(bar):
            stale_open.append(row["symbol"])
            continue
        marked += float(row["shares"]) * float(bar.close)
    unpaid = float(out.get("dividend_unpaid") or 0.0)
    equity_gap = float(out["ending_equity"]) - (float(out["ending_cash"]) + unpaid + marked)
    fresh = not stale_open
    cash_ok = abs(cash_gap) < 0.05
    equity_ok = (not fresh) or abs(equity_gap) < 0.05
    pseudo = []
    for row in positions:
        bar = bars.get(row["symbol"], [None])[-1]
        mark = float(bar.close) if usable(bar) else float(row["entry_fill"])
        pseudo.append(
            {
                "symbol": row["symbol"],
                "entry_date": row["entry_session"],
                "exit_date": dates[-1],
                "exit_fill": mark,
                "shares": row["shares"],
            }
        )
    hits = action_hits(trades + pseudo, actions, dates, lookup)
    block = material_block(hits)
    reasons = {}
    for row in trades:
        reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
    wins = [row for row in done if float(row["pnl"]) > 0]
    losses = [row for row in done if float(row["pnl"]) < 0]
    return {
        "net_return": float(out["net_return"]),
        "ending_equity": float(out["ending_equity"]),
        "ending_cash": float(out["ending_cash"]),
        "max_drawdown": float(out["max_drawdown"]),
        "turnover": float(out["turnover"]),
        "cost_dollars": float(out["cost_dollars"]),
        "avg_exposure": float(out["avg_exposure"]),
        "dividend_dollars": float(out["dividend_dollars"]),
        "completed_round_trips": len(done),
        "open_positions": len(positions),
        "open_cost": open_cost,
        "unrealized_pnl": None if stale_open else marked - open_cost,
        "trade_reasons": reasons,
        "wins": len(wins),
        "losses": len(losses),
        "reconciled": bool(cash_ok and equity_ok and not out.get("valuation_inconclusive")),
        "cash_identity_ok": cash_ok,
        "equity_identity_ok": equity_ok,
        "cash_gap": cash_gap,
        "distribution_unpriced": int(out["distribution_unpriced"]),
        "unresolved_holdings": int(out["unresolved_holdings"]),
        "stale_terminals": int(out["stale_terminals"]),
        "stale_open_marks": len(stale_open),
        "stale_open_symbols": stale_open,
        "valuation_inconclusive": bool(out.get("valuation_inconclusive")),
        "held_action": bool(block["blocked"]),
        "held_action_hits": len(block["hits"]),
        "held_action_priced_total": block["priced_total"],
    }
