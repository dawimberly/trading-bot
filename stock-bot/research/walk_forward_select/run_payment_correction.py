"""Rerun the frozen selector with payable-date cash. Does not overwrite the ex-date results."""

from __future__ import annotations

import json
import shutil
import sys
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ABC = HERE.parent / "nyse_cash_abc"
sys.path.insert(0, str(ABC))
sys.path.insert(0, str(HERE))

from engine import INITIAL_CASH, SLIP  # noqa: E402
from cash_lots import dividend_lots, lots_on, spy_lots  # noqa: E402
from data_access import actions_for, load_market, panel_for  # noqa: E402
from metrics import fold_metrics, spy_total_return_paid  # noqa: E402
from run_walkforward import _constant_schedule, _match, _public, _selector_schedule  # noqa: E402
from select import candidates, select_winner  # noqa: E402
from slippage_trace import decompose  # noqa: E402
from stitch import simulate_continuous  # noqa: E402

OUT = HERE / "results" / "payment_timing"
SUPER = HERE / "results" / "superseded" / "ex_date_cash"
MARKET = None


def _init_worker() -> None:
    global MARKET
    market = load_market()
    lots, problems = dividend_lots(market["divs"], market["splits"], market["calendar"])
    market["lots"] = lots
    market["lot_problems"] = problems
    MARKET = market


def _constant(dates: list[str], spec: dict, decision_start: int) -> list:
    return [dict(spec) if i >= decision_start else None for i in range(len(dates))]


def _run(market, dates, schedule, slip, decision_start) -> dict:
    bars, member, spy = panel_for(dates, market["lookup"], market["membership"])
    actions = actions_for(dates, market["divs"], market["splits"])
    return simulate_continuous(
        bars,
        member,
        dates,
        spy,
        schedule,
        decision_start=decision_start,
        distributions=actions["distributions"],
        renames=actions["renames"],
        dividend_lots=lots_on(dates, market["lots"]),
        atr_period=14,
        max_names=15,
        sleeve=0.95,
        slip=slip,
        initial_cash=INITIAL_CASH,
    )


def _score(task: tuple) -> dict:
    fold_i, spec = task
    market = MARKET
    fold = market["plan"]["folds"][fold_i]
    dates = list(fold["train_dates"])
    warmup = int(market["config"]["warmup_sessions"])
    if dates[-1] != fold["cutoff"] or any(day > fold["cutoff"] for day in dates):
        raise RuntimeError("training dates pass the cutoff")
    out = _run(market, dates, _constant(dates, spec, warmup), SLIP, warmup)
    if out["end"] != fold["cutoff"] or out["sessions"] != int(market["config"]["train_sessions"]):
        raise RuntimeError("training score includes warm-up or a later session")
    if not out["reconciled"] or out["distribution_unpriced"]:
        raise RuntimeError(f"unreconciled fold {fold['fold']} {spec}")
    return {
        "fold": fold["fold"],
        "order": spec["order"],
        "ma_win": spec["ma_win"],
        "max_hold": spec["max_hold"],
        "atr_mult": spec["atr_mult"],
        "net_return": out["net_return"],
        "ending_equity": out["ending_equity"],
        "ending_cash": out["ending_cash"],
        "max_drawdown": out["max_drawdown"],
        "turnover": out["turnover"],
        "trades": out["trades"],
        "cost_dollars": out["cost_dollars"],
        "avg_exposure": out["avg_exposure"],
        "dividend_dollars": out["dividend_dollars"],
        "dividend_received": out["dividend_received"],
        "dividend_unpaid": out["dividend_unpaid"],
        "reconciled": out["reconciled"],
        "distribution_unpriced": out["distribution_unpriced"],
        "train_start": out["start"],
        "train_end": out["end"],
    }


def _preserve_originals() -> None:
    SUPER.mkdir(parents=True, exist_ok=True)
    for name in ("report.md", "evaluation.json", "selections.json", "training_scores.json", "verification.json", "feasibility.json"):
        src = HERE / "results" / name
        dest = SUPER / name
        if src.is_file() and not dest.is_file():
            shutil.copy2(src, dest)


def _slim_trace(row: dict) -> dict:
    keep = (
        "ending_equity_5bps",
        "ending_equity_10bps",
        "equity_delta_10_minus_5",
        "return_delta",
        "cost_dollars_5bps",
        "cost_dollars_10bps",
        "cost_delta_not_added_again",
        "buckets",
        "counts",
        "examples",
        "dividend_received_delta",
        "dividend_unpaid_delta",
        "explained_minus_equity_delta",
        "earliest_divergence",
        "trades_5bps",
        "trades_10bps",
    )
    return {key: row[key] for key in keep}


def main() -> int:
    _preserve_originals()
    OUT.mkdir(parents=True, exist_ok=True)
    market = load_market()
    lots, problems = dividend_lots(market["divs"], market["splits"], market["calendar"])
    missing = [row for row in problems if row["problem"] == "missing_payable"]
    if missing:
        print("BLOCKED missing payable", len(missing), flush=True)
        return 1
    market["lots"] = lots
    market["lot_problems"] = problems
    spy, spy_notes = spy_lots(market["calendar"])
    cfg = market["config"]
    grid = candidates(cfg)
    # The ex-date cash engine match stays a check of the unchanged code path.
    from run_walkforward import _baseline, _book

    base_engine = _baseline(market, SLIP)
    base_stitch = _book(market, _constant_schedule(market, cfg["baseline_A"]), SLIP)
    engine_match = _match(base_engine, base_stitch)
    if not engine_match["matches_engine"]:
        (OUT / "evaluation.json").write_text(json.dumps({"status": "BLOCKED", "reason": "ex-date stitch drifted", "match": engine_match}, indent=2), encoding="utf-8")
        print("BLOCKED stitch drift", flush=True)
        return 1
    tasks = [(i, spec) for i in range(4) for spec in grid]
    scores = []
    print(f"score {len(tasks)} with payable-date cash", flush=True)
    try:
        with ProcessPoolExecutor(max_workers=min(8, len(tasks)), initializer=_init_worker) as pool:
            futures = [pool.submit(_score, task) for task in tasks]
            for done, future in enumerate(as_completed(futures), start=1):
                scores.append(future.result())
                if done % 9 == 0 or done == len(tasks):
                    print(f"scored {done}/{len(tasks)}", flush=True)
    except Exception as exc:
        (OUT / "evaluation.json").write_text(json.dumps({"status": "BLOCKED", "reason": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()}, indent=2), encoding="utf-8")
        print(type(exc).__name__, exc, flush=True)
        return 1
    scores.sort(key=lambda row: (row["fold"], row["order"]))
    (OUT / "training_scores.json").write_text(json.dumps(scores, indent=2), encoding="utf-8")
    original = json.loads((HERE / "results" / "selections.json").read_text(encoding="utf-8"))
    decisions = []
    for fold in market["plan"]["folds"]:
        rows = [row for row in scores if row["fold"] == fold["fold"]]
        decision = select_winner(rows, cfg)
        decision["fold"] = fold["fold"]
        decision["cutoff"] = fold["cutoff"]
        decision["eval_start"] = fold["eval"][0]
        decision["eval_end"] = fold["eval"][-1]
        decision["eligible_count"] = sum(1 for row in decision["candidates"] if row["eligible"])
        prior = next(row for row in original if row["fold"] == fold["fold"])
        decision["prior_ex_date_selection"] = prior["selected"]
        chosen = decision["selected"]
        old = prior["selected"]
        decision["selection_changed"] = chosen is None or (
            chosen["ma_win"],
            chosen["max_hold"],
            chosen["atr_mult"],
        ) != (old["ma_win"], old["max_hold"], old["atr_mult"])
        decisions.append(decision)
    (OUT / "selections.json").write_text(json.dumps(decisions, indent=2), encoding="utf-8")
    warmup = int(cfg["warmup_sessions"])
    history = market["plan"]["eval_history"]
    selector = _run(market, history, _selector_schedule(market, decisions), SLIP, warmup)
    baseline = _run(market, history, _constant_schedule(market, cfg["baseline_A"]), SLIP, warmup)
    slip2 = 10.0 / 10_000.0
    selector_10 = _run(market, history, _selector_schedule(market, decisions), slip2, warmup)
    baseline_10 = _run(market, history, _constant_schedule(market, cfg["baseline_A"]), slip2, warmup)
    # Original reported sensitivity uses the ex-date selections and ex-date cash.
    original_schedule = _selector_schedule(market, original)
    old_selector = _book(market, original_schedule, SLIP)
    old_selector_10 = _book(market, original_schedule, slip2)
    old_base = base_stitch
    old_base_10 = _book(market, _constant_schedule(market, cfg["baseline_A"]), slip2)
    eval_dates = market["plan"]["eval_dates"]
    spy_bars = {day: market["lookup"]["SPY"][day] for day in eval_dates}
    spy_eval = lots_on(eval_dates, spy)
    if not spy_eval:
        print("BLOCKED no SPY dividends", flush=True)
        return 1
    spy = spy_total_return_paid(eval_dates, spy_bars, spy_eval, SLIP, INITIAL_CASH)
    spy_10 = spy_total_return_paid(eval_dates, spy_bars, spy_eval, slip2, INITIAL_CASH)
    choice = {row["fold"]: row for row in decisions}
    folds = {
        "selector": [],
        "baseline_A": [],
        "spy_total_return": [],
    }
    for fold in market["plan"]["folds"]:
        row = fold_metrics(selector, fold["eval"], INITIAL_CASH)
        row["fold"] = fold["fold"]
        selected = choice[fold["fold"]]["selected"]
        row["selected"] = None if selected is None else {k: selected[k] for k in ("ma_win", "max_hold", "atr_mult")}
        folds["selector"].append(row)
        folds["baseline_A"].append(fold_metrics(baseline, fold["eval"], INITIAL_CASH) | {"fold": fold["fold"]})
        folds["spy_total_return"].append(fold_metrics(spy, fold["eval"], INITIAL_CASH) | {"fold": fold["fold"]})
    payload = {
        "status": "COMPLETED",
        "cash_timing": "payable date; receivable until paid; unpaid included in terminal equity",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "selection_algorithm": "unchanged frozen rule",
        "selection_changed": [row["fold"] for row in decisions if row["selection_changed"]],
        "lot_problems": problems,
        "spy_notes": spy_notes,
        "engine_ex_date_match": engine_match,
        "aggregate": {
            "selector": _public(selector),
            "baseline_A": _public(baseline),
            "spy_total_return": _public(spy),
        },
        "folds": folds,
        "doubled_slippage": {
            "reselection": False,
            "selector": _public(selector_10),
            "baseline_A": _public(baseline_10),
            "spy_total_return": _public(spy_10),
        },
    }
    (OUT / "evaluation.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    trace = {
        "ex_date_cash_original_schedule": {
            "selector": _slim_trace(decompose(old_selector, old_selector_10)),
            "baseline_A": _slim_trace(decompose(old_base, old_base_10)),
        },
        "payable_date_corrected_schedule": {
            "selector": _slim_trace(decompose(selector, selector_10)),
            "baseline_A": _slim_trace(decompose(baseline, baseline_10)),
        },
        "spy_payable": {
            "equity_5bps": spy["ending_equity"],
            "equity_10bps": spy_10["ending_equity"],
            "unpaid_5bps": spy["dividend_unpaid"],
            "received_5bps": spy["dividend_cash"],
            "reinvest_sessions_5bps": [row["session"] for row in spy["dividends"]],
        },
    }
    (HERE / "results" / "slippage_trace.json").write_text(json.dumps(trace, indent=2), encoding="utf-8")
    print(
        "COMPLETED",
        "changed",
        payload["selection_changed"],
        selector["net_return"],
        baseline["net_return"],
        spy["net_return"],
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
