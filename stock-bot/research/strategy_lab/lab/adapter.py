"""Calls the frozen cash engine and payable-date stitch. Does not modify them."""

from __future__ import annotations

import json
import math
import sys

from lab.bundle_market import load_frozen_market, load_sim_market, resolve_snapshot
from lab.paths import ABC, ABC_RESULTS, PAYABLE_EVAL, PAYABLE_SELECTIONS, WALK
from lab.snapshots import connect_readonly
from lab.specs import SpecError, candidates, resolve_windows

for path in (str(ABC), str(WALK)):
    if path not in sys.path:
        sys.path.insert(0, path)

_MARKETS: dict[str, dict] = {}


def market_for(snapshot_id: str | None = None) -> dict:
    """Load the requested bundle. The default remains the frozen reproduction tape."""
    kind, resolved, root = resolve_snapshot(snapshot_id)
    cached = _MARKETS.get(resolved)
    if cached is not None:
        return cached
    market = load_frozen_market() if kind == "frozen" else load_sim_market(root)
    _MARKETS[resolved] = market
    return market


def _market():
    return market_for(None)


def calendar() -> list[str]:
    return list(_market()["calendar"])


def data_health() -> dict:
    market = _market()
    problems = market["lot_problems"]
    missing = [row for row in problems if row.get("problem") == "missing_payable"]
    inverted = [row for row in problems if row.get("problem") == "payable_before_ex"]
    days = market["calendar"]
    return {
        "snapshot_id": market["snapshot"]["snapshot_id"],
        "provenance": market["snapshot"]["provenance"],
        "first_session": days[0],
        "last_session": days[-1],
        "sessions": len(days),
        "symbols": len(market["lookup"]),
        "missing_payable": missing,
        "payable_before_ex": inverted,
        "coverage_block": bool(missing),
        "coverage_note": (
            "A dividend without a payable date blocks an experiment that includes its ex-date. "
            "Dates are not dropped to avoid the gap."
            if missing
            else "Every stored cash dividend in this snapshot has a payable date or the recorded ALL exception."
        ),
    }


def _ann_vol(equity: list[float]) -> float:
    rets = [cur / prev - 1.0 for prev, cur in zip(equity, equity[1:]) if prev]
    if len(rets) < 2:
        return float("nan")
    mean = sum(rets) / len(rets)
    var = sum((item - mean) ** 2 for item in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252.0)


def public_metrics(result: dict, *, phase: str, comparable: bool, folds_completed: int, folds_required: int) -> dict:
    equity = list(result.get("equity_path") or [])
    vol = result.get("ann_vol")
    if vol is None or (isinstance(vol, float) and math.isnan(vol)):
        vol = _ann_vol(equity)
    unpaid = result.get("dividend_unpaid", result.get("dividend_unpaid", 0.0))
    return {
        "phase": phase,
        "comparable": comparable,
        "folds_completed": folds_completed,
        "folds_required": folds_required,
        "start": result.get("start"),
        "end": result.get("end"),
        "sessions": result.get("sessions"),
        "net_return": result.get("net_return"),
        "ending_equity": result.get("ending_equity"),
        "ending_cash": result.get("ending_cash"),
        "max_drawdown": result.get("max_drawdown"),
        "ann_vol": vol,
        "avg_exposure": result.get("avg_exposure"),
        "turnover": result.get("turnover"),
        "cost_dollars": result.get("cost_dollars"),
        "trades": result.get("trades"),
        "dividend_received": result.get("dividend_received", result.get("dividend_cash")),
        "dividend_unpaid": unpaid,
        "reconciled": result.get("reconciled"),
        "equity_dates": list(result.get("equity_dates") or []),
        "equity_path": equity,
    }


def _selected_actions(market: dict, dates: list[str]) -> dict:
    from data_access import actions_for
    from engine import Distribution
    from stitch import Grant, Settlement

    if market.get("kind") != "sim":
        return actions_for(dates, market["divs"], market["splits"])
    allowed = set(dates)
    accounting = market["accounting"]
    renames: dict[str, list[tuple[str, str]]] = {}
    for row in accounting.get("renames", []):
        if row["session"] in allowed:
            renames.setdefault(row["session"], []).append((row["old"], row["new"]))
    settlements: dict[str, list] = {}
    for row in accounting.get("settlements", []):
        if row["session"] not in allowed:
            continue
        settlements.setdefault(row["session"], []).append(
            Settlement(
                symbol=row["symbol"],
                cash_per_share=float(row["cash_per_share"]),
                acquirer=row.get("acquirer"),
                share_ratio=float(row["share_ratio"]),
                residual_name=row.get("residual_name"),
                residual_per_share=float(row.get("residual_per_share") or 0.0),
                residual_low=row.get("residual_low"),
                residual_high=row.get("residual_high"),
                residual_terminal=row.get("residual_terminal") or "bounded",
            )
        )
    distributions = [
        Distribution(row["session"], row["parent"], row["child"], float(row["ratio"]))
        for row in accounting.get("spins", [])
        if row["session"] in allowed
    ]
    grants = [
        Grant(
            session=row["session"],
            parent=row["parent"],
            name=row["name"],
            ratio=float(row["ratio"]),
            value_low=row.get("grant_value_low"),
            value_high=row.get("grant_value_high"),
            terminal=row.get("terminal") or "unpriced_path",
        )
        for row in accounting.get("grants", [])
        if row["session"] in allowed
    ]
    return {"distributions": distributions, "renames": renames, "settlements": settlements, "grants": grants}


def _require_search(market: dict) -> None:
    if market.get("kind") != "sim":
        return
    if market.get("simulation_ready") and not market.get("search_blocked"):
        return
    reasons = market.get("search_block_reasons") or []
    if reasons:
        raise SpecError("Strategy search is blocked. " + " ".join(reasons))
    sample = (market.get("blockers") or [{"reason": "unresolved corporate action"}])[0]
    name = sample.get("source_symbol") or sample.get("acquiree_symbol") or sample.get("symbol") or "holding"
    raise SpecError(
        f"simulation bundle is not ready: {name} {sample.get('reason')}. Strategy search was not started."
    )


def _payable_book(dates: list[str], schedule: list, slip: float, decision_start: int, market: dict | None = None) -> dict:
    from cash_lots import lots_on
    from data_access import panel_for
    from stitch import simulate_continuous

    market = market or _market()
    _require_search(market)
    missing = [
        row for row in market["lot_problems"]
        if row.get("problem") == "missing_payable" and row.get("ex_date") in set(dates)
    ]
    if missing:
        sample = missing[0]
        raise SpecError(
            f"dividend payable date missing for {sample.get('symbol')} ex-date {sample.get('ex_date')}. "
            "The experiment is blocked. Cash was not credited on the ex-date instead."
        )
    bars, member, spy = panel_for(dates, market["lookup"], market["membership"])
    actions = _selected_actions(market, dates)
    for row in actions["distributions"]:
        if row.child not in bars and row.child in market["lookup"]:
            series = market["lookup"][row.child]
            bars[row.child] = [series.get(day) if series.get(day) is not None else None for day in dates]
            member[row.child] = [False] * len(dates)
    for deals in actions.get("settlements", {}).values():
        for deal in deals:
            if deal.acquirer and deal.acquirer not in bars and deal.acquirer in market["lookup"]:
                series = market["lookup"][deal.acquirer]
                bars[deal.acquirer] = [series.get(day) for day in dates]
                member[deal.acquirer] = [False] * len(dates)
    out = simulate_continuous(
        bars,
        member,
        dates,
        spy,
        schedule,
        decision_start=decision_start,
        distributions=actions["distributions"],
        renames=actions["renames"],
        settlements=actions.get("settlements"),
        grants=actions.get("grants"),
        strict_actions=market.get("kind") == "sim",
        dividend_lots=lots_on(dates, market["lots"]),
        atr_period=14,
        max_names=15,
        sleeve=0.95,
        slip=slip,
        initial_cash=100_000.0,
    )
    if market.get("kind") == "sim" and (out.get("unresolved_holdings") or out.get("distribution_unpriced")):
        raise SpecError(
            "A holding had no priced corporate action. It was not closed at the last print, and strategy search was not continued."
        )
    return out


def _spy(dates: list[str], slip: float, market: dict | None = None) -> dict:
    from metrics import spy_total_return_paid

    market = market or _market()
    bars = {}
    for day in dates:
        raw = market["lookup"].get("SPY", {}).get(day)
        if raw is None:
            raise SpecError(f"SPY bar missing on {day}. The benchmark date was not skipped.")
        bars[day] = raw
    lots = [lot for lot in market["spy_lot_list"] if lot["ex_session"] in set(dates)]
    return spy_total_return_paid(dates, bars, lots, slip, 100_000.0, require_lots=bool(lots))


def score_training(spec: dict, candidate: dict, fold: dict) -> dict:
    slip = float(spec["costs"]["slip_bps_per_side"]) / 10_000.0
    dates = list(fold["train_dates"])
    warmup = int(spec["schedule"]["warmup_sessions"])
    schedule = [dict(candidate) if i >= warmup else None for i in range(len(dates))]
    out = _payable_book(dates, schedule, slip, warmup, market_for(spec.get("snapshot_id")))
    if out["end"] != fold["cutoff"] or any(day > fold["cutoff"] for day in dates):
        raise SpecError("training score included a session after the cutoff")
    metrics = public_metrics(out, phase="training", comparable=False, folds_completed=1, folds_required=1)
    metrics["params"] = candidate
    metrics["fold"] = fold["fold"]
    from select import eligible

    row = {
        "order": candidate["order"],
        "ma_win": candidate["ma_win"],
        "max_hold": candidate["max_hold"],
        "atr_mult": candidate["atr_mult"],
        "net_return": out["net_return"],
        "max_drawdown": out["max_drawdown"],
        "turnover": out["turnover"],
        "trades": out["trades"],
        "reconciled": out["reconciled"],
        "distribution_unpriced": out["distribution_unpriced"],
    }
    cfg = {"eligibility": spec["risk"]}
    metrics["eligible"] = bool(out["reconciled"]) and not out["distribution_unpriced"] and eligible(row, cfg)
    metrics["selection_row"] = row
    return metrics


def evaluate_schedule(spec: dict, windows: dict, schedule_specs: list[dict | None]) -> dict:
    slip = float(spec["costs"]["slip_bps_per_side"]) / 10_000.0
    dates = windows["eval_history"]
    if len(schedule_specs) != len(dates):
        raise SpecError(
            f"evaluation schedule has {len(schedule_specs)} sessions and the window has {len(dates)}. "
            "The dates were not padded or truncated."
        )
    market = market_for(spec.get("snapshot_id"))
    out = _payable_book(dates, schedule_specs, slip, int(spec["schedule"]["warmup_sessions"]), market)
    metrics = public_metrics(
        out,
        phase="evaluation",
        comparable=True,
        folds_completed=int(spec["schedule"]["folds"]),
        folds_required=int(spec["schedule"]["folds"]),
    )
    bench = _spy(windows["eval_dates"], slip, market)
    metrics["benchmark_return"] = bench["net_return"]
    metrics["benchmark_equity"] = bench["ending_equity"]
    metrics["benchmark_unpaid"] = bench["dividend_unpaid"]
    metrics["excess_return_vs_spy"] = metrics["net_return"] - bench["net_return"]
    return metrics


def reproduce_baseline_a(label: str = "recent") -> dict:
    """Arm A through the frozen engine, matching abc_results.json. Ex-date cash is that artifact's rule."""
    from engine import WARMUP, simulate
    from membership import research_membership
    from run_abc import build_windows, load_tables, membership_index, panel_for
    from run_abc import member_on
    from run_rotation import GEV_DISTRIBUTION, alias_fi

    saved = json.loads(ABC_RESULTS.read_text(encoding="utf-8"))["arms"][label]["A"]
    conn = connect_readonly()
    bars, divs, splits, _notes = load_tables(conn)
    conn.close()
    calendar_days = json.loads((ABC / "data" / "calendar.json").read_text(encoding="utf-8"))
    windows = build_windows(calendar_days)
    dates = windows[label]
    membership = membership_index(research_membership())
    lookup = {}
    for rec in bars.itertuples(index=False):
        from engine import Bar

        lookup.setdefault(rec.symbol, {})[rec.session] = Bar(rec.open, rec.high, rec.low, rec.close)
    alias_fi(lookup)
    from run_abc import dividend_map

    symbols = sorted(s for s in set(lookup) - {"SPY"} if any(member_on(membership, s, d) for d in dates[WARMUP:]))
    panel, member, spy = panel_for(dates, symbols, lookup, membership)
    div_all = dividend_map(divs, splits, windows["span"])
    div_window = {s: {d: v for d, v in days.items() if d in set(dates)} for s, days in div_all.items()}
    renames = {"2025-11-11": [("FI", "FISV")]} if "2025-11-11" in dates else {}
    out = simulate(
        panel,
        member,
        dates,
        spy,
        arm="A",
        decision_start=WARMUP,
        dividends=div_window,
        renames=renames,
        distributions=[GEV_DISTRIBUTION],
    )
    return {"saved": saved, "ending_equity": out["ending_equity"], "net_return": out["net_return"], "max_drawdown": out["max_drawdown"], "trades": out["trades"]}


def reproduce_payable_evaluation() -> dict:
    """Stitch the frozen payable-date schedule. Does not rescore or retune."""
    from run_walkforward import _selector_schedule

    market = _market()
    saved = json.loads(PAYABLE_EVAL.read_text(encoding="utf-8"))
    choices = json.loads(PAYABLE_SELECTIONS.read_text(encoding="utf-8"))
    for index, choice in enumerate(choices):
        choice["fold"] = index + 1
    slip = 5.0 / 10_000.0
    dates = market["plan"]["eval_history"]
    schedule = _selector_schedule(market, choices)
    out = _payable_book(dates, schedule, slip, int(market["config"]["warmup_sessions"]))
    base = market["config"]["baseline_A"]
    base_schedule = [dict(base) if day >= market["plan"]["eval_dates"][0] else None for day in dates]
    base_out = _payable_book(dates, base_schedule, slip, int(market["config"]["warmup_sessions"]))
    return {
        "selector_equity": out["ending_equity"],
        "selector_saved": saved["aggregate"]["selector"]["ending_equity"],
        "baseline_equity": base_out["ending_equity"],
        "baseline_saved": saved["aggregate"]["baseline_A"]["ending_equity"],
        "selector_unpaid": out["dividend_unpaid"],
        "baseline_unpaid": base_out["dividend_unpaid"],
    }


def prepare(spec: dict) -> dict:
    market = market_for(spec.get("snapshot_id"))
    _require_search(market)
    windows = resolve_windows(spec, market["calendar"])
    return {"windows": windows, "candidates": candidates(spec), "snapshot_id": market["snapshot"]["snapshot_id"]}
