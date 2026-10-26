"""Run the frozen walk-forward selection experiment. Research only."""

from __future__ import annotations

import json
import math
import sys
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ABC = HERE.parent / "nyse_cash_abc"
sys.path.insert(0, str(ABC))
sys.path.insert(0, str(HERE))

from engine import INITIAL_CASH, SLIP, simulate  # noqa: E402
from data_access import actions_for, assert_training_isolated, load_market, panel_for, spy_dividend_map  # noqa: E402
from metrics import fold_metrics, spy_total_return  # noqa: E402
from select import candidates, select_winner  # noqa: E402
from stitch import simulate_continuous  # noqa: E402

RESULTS = HERE / "results"
MARKET = None


def _init_worker() -> None:
    global MARKET
    MARKET = load_market()


def _score(task: tuple) -> dict:
    fold_i, spec = task
    market = MARKET
    fold = market["plan"]["folds"][fold_i]
    assert_training_isolated(fold)
    dates = list(fold["train_dates"])
    if dates[-1] != fold["cutoff"] or any(day > fold["cutoff"] for day in dates):
        raise RuntimeError("training panel is not cut off")
    bars, member, spy = panel_for(dates, market["lookup"], market["membership"])
    actions = actions_for(dates, market["divs"], market["splits"])
    if any(day > fold["cutoff"] for day in actions["renames"]):
        raise RuntimeError("rename after cutoff")
    for dist in actions["distributions"]:
        if dist.session > fold["cutoff"]:
            raise RuntimeError("distribution after cutoff")
    warmup = int(market["config"]["warmup_sessions"])
    out = simulate(
        bars,
        member,
        dates,
        spy,
        arm="A",
        decision_start=warmup,
        dividends=actions["dividends"],
        distributions=actions["distributions"],
        renames=actions["renames"],
        ma_win=int(spec["ma_win"]),
        max_hold=int(spec["max_hold"]),
        atr_mult=float(spec["atr_mult"]),
        atr_period=14,
        max_names=15,
        sleeve=0.95,
        slip=SLIP,
        initial_cash=INITIAL_CASH,
    )
    if out["end"] != fold["cutoff"]:
        raise RuntimeError("terminal valuation is not the cutoff")
    if out["sessions"] != int(market["config"]["train_sessions"]):
        raise RuntimeError("warmup sessions were scored")
    return {
        "fold": fold["fold"],
        "order": spec["order"],
        "ma_win": spec["ma_win"],
        "max_hold": spec["max_hold"],
        "atr_mult": spec["atr_mult"],
        "net_return": out["net_return"],
        "ending_equity": out["ending_equity"],
        "max_drawdown": out["max_drawdown"],
        "turnover": out["turnover"],
        "trades": out["trades"],
        "cost_dollars": out["cost_dollars"],
        "avg_exposure": out["avg_exposure"],
        "dividend_dollars": out["dividend_dollars"],
        "reconciled": out["reconciled"],
        "distribution_unpriced": out["distribution_unpriced"],
        "warmup_excluded_sessions": warmup,
        "train_start": out["start"],
        "train_end": out["end"],
    }


def _book(market: dict, schedule: list, slip: float) -> dict:
    dates = market["plan"]["eval_history"]
    bars, member, spy = panel_for(dates, market["lookup"], market["membership"])
    actions = actions_for(dates, market["divs"], market["splits"])
    warmup = int(market["config"]["warmup_sessions"])
    return simulate_continuous(
        bars,
        member,
        dates,
        spy,
        schedule,
        decision_start=warmup,
        dividends=actions["dividends"],
        distributions=actions["distributions"],
        renames=actions["renames"],
        atr_period=14,
        max_names=15,
        sleeve=0.95,
        slip=slip,
        initial_cash=INITIAL_CASH,
    )


def _baseline(market: dict, slip: float) -> dict:
    dates = market["plan"]["eval_history"]
    bars, member, spy = panel_for(dates, market["lookup"], market["membership"])
    actions = actions_for(dates, market["divs"], market["splits"])
    base = market["config"]["baseline_A"]
    return simulate(
        bars,
        member,
        dates,
        spy,
        arm="A",
        decision_start=int(market["config"]["warmup_sessions"]),
        dividends=actions["dividends"],
        distributions=actions["distributions"],
        renames=actions["renames"],
        ma_win=int(base["ma_win"]),
        max_hold=int(base["max_hold"]),
        atr_mult=float(base["atr_mult"]),
        atr_period=14,
        max_names=15,
        sleeve=0.95,
        slip=slip,
        initial_cash=INITIAL_CASH,
    )


def _constant_schedule(market: dict, spec: dict) -> list:
    dates = market["plan"]["eval_history"]
    first = market["plan"]["eval_dates"][0]
    return [dict(spec) if day >= first else None for day in dates]


def _selector_schedule(market: dict, choices: list[dict]) -> list:
    dates = market["plan"]["eval_history"]
    by_fold = {row["fold"]: row for row in choices}
    schedule = []
    for day in dates:
        chosen = None
        for fold in market["plan"]["folds"]:
            if day in fold["eval"]:
                chosen = by_fold[fold["fold"]]["selected"]
                break
        schedule.append(None if chosen is None else dict(chosen))
    return schedule


def _public(result: dict) -> dict:
    skip = {"equity_path", "exposure_path", "equity_dates", "executions", "trade_log"}
    return {key: value for key, value in result.items() if key not in skip}


def _match(left: dict, right: dict) -> dict:
    keys = ("ending_equity", "max_drawdown", "trades", "cost_dollars", "turnover", "dividend_dollars", "net_return")
    gaps = {}
    for key in keys:
        gaps[key] = float(left[key]) - float(right[key])
    ok = all(abs(value) < 1e-6 for value in gaps.values())
    return {"matches_engine": ok, "engine_minus_stitch": gaps}


def _fold_rows(result: dict, market: dict, choice_by_fold: dict | None) -> list[dict]:
    rows = []
    for fold in market["plan"]["folds"]:
        row = fold_metrics(result, fold["eval"], INITIAL_CASH)
        row["fold"] = fold["fold"]
        if choice_by_fold is not None:
            selected = choice_by_fold[fold["fold"]]["selected"]
            row["selected"] = selected if selected is None else {
                "ma_win": selected["ma_win"],
                "max_hold": selected["max_hold"],
                "atr_mult": selected["atr_mult"],
            }
        rows.append(row)
    return rows


def _write_report(payload: dict) -> None:
    lines = [
        "# Walk-forward selection",
        "",
        f"Status: **{payload['status']}**.",
        "",
        "The selection rule was frozen in `SPEC.md` and `config.json` before this run. These folds overlap the previously inspected windows. They are simulated out-of-sample tests, not untouched confirmation.",
        "",
        f"Candidates scored: {payload['candidates_scored']}. Folds: {payload['folds_searched']}. Ineligible rows are kept in `training_scores.json`.",
        "",
    ]
    if payload["status"] != "COMPLETED":
        lines.append(payload.get("reason", ""))
        (RESULTS / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return
    lines.append("## Aggregate")
    lines.append("")
    lines.append("| Book | Return | Max DD | Ann. vol | Turnover | Costs | Exposure | End equity |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for label in ("selector", "baseline_A", "spy_total_return"):
        row = payload["aggregate"][label]
        lines.append(
            f"| {label} | {100 * row['net_return']:.4f}% | {100 * row['max_drawdown']:.4f}% | {100 * row['ann_vol']:.4f}% | {row['turnover']:.4f}x | ${row['cost_dollars']:,.2f} | {100 * row['avg_exposure']:.2f}% | ${row['ending_equity']:,.2f} |"
        )
    sel = payload["aggregate"]["selector"]["net_return"]
    base = payload["aggregate"]["baseline_A"]["net_return"]
    spy = payload["aggregate"]["spy_total_return"]["net_return"]
    lines.extend(
        [
            "",
            f"Selector minus fixed A: {100 * (sel - base):+.4f} percentage points. Selector minus SPY total return: {100 * (sel - spy):+.4f} percentage points.",
            "",
            "## Folds",
            "",
        ]
    )
    lines.append("| Fold | Dates | Choice | Selector | A | SPY |")
    lines.append("|---:|---|---|---:|---:|---:|")
    for fold, a_row, spy_row in zip(payload["folds"]["selector"], payload["folds"]["baseline_A"], payload["folds"]["spy_total_return"]):
        choice = fold["selected"]
        label = "cash" if choice is None else f"{choice['ma_win']}/{choice['max_hold']}/{choice['atr_mult']}"
        lines.append(
            f"| {fold['fold']} | {fold['start']} → {fold['end']} | {label} | {100 * fold['net_return']:.4f}% | {100 * a_row['net_return']:.4f}% | {100 * spy_row['net_return']:.4f}% |"
        )
    sens = payload["doubled_slippage"]
    lines.extend(
        [
            "",
            "## Doubled slippage, no reselection",
            "",
            f"10 bps per side. Selector {100 * sens['selector']['net_return']:.4f}%, fixed A {100 * sens['baseline_A']['net_return']:.4f}%, SPY {100 * sens['spy_total_return']['net_return']:.4f}%.",
            "",
            "This result describes the frozen selection process on these folds. It does not change the grid or the thresholds, and it does not start a paper account.",
            "",
        ]
    )
    (RESULTS / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    market = load_market()
    cfg = market["config"]
    grid = candidates(cfg)
    tasks = [(i, spec) for i in range(len(market["plan"]["folds"])) for spec in grid]
    scores = []
    workers = min(8, len(tasks))
    print(f"score {len(tasks)} candidates on {workers} workers", flush=True)
    try:
        with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker) as pool:
            futures = [pool.submit(_score, task) for task in tasks]
            for done, future in enumerate(as_completed(futures), start=1):
                scores.append(future.result())
                if done % 9 == 0 or done == len(tasks):
                    print(f"scored {done}/{len(tasks)}", flush=True)
    except Exception as exc:
        payload = {"status": "BLOCKED", "reason": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()}
        (RESULTS / "evaluation.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(payload["reason"], flush=True)
        return 1
    scores.sort(key=lambda row: (row["fold"], row["order"]))
    (RESULTS / "training_scores.json").write_text(json.dumps(scores, indent=2), encoding="utf-8")
    broken = [row for row in scores if not row["reconciled"] or row["distribution_unpriced"]]
    if broken:
        payload = {
            "status": "BLOCKED",
            "reason": "a training ledger failed reconciliation or priced a distribution as missing",
            "failed": [{k: row[k] for k in ("fold", "ma_win", "max_hold", "atr_mult", "reconciled", "distribution_unpriced")} for row in broken],
        }
        (RESULTS / "evaluation.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _write_report({"status": "BLOCKED", "reason": payload["reason"], "candidates_scored": len(scores), "folds_searched": 4})
        return 1
    decisions = []
    for fold in market["plan"]["folds"]:
        rows = [row for row in scores if row["fold"] == fold["fold"]]
        decision = select_winner(rows, cfg)
        decision["fold"] = fold["fold"]
        decision["cutoff"] = fold["cutoff"]
        decision["eval_start"] = fold["eval"][0]
        decision["eval_end"] = fold["eval"][-1]
        decision["eligible_count"] = sum(1 for row in decision["candidates"] if row["eligible"])
        decisions.append(decision)
    (RESULTS / "selections.json").write_text(json.dumps(decisions, indent=2), encoding="utf-8")

    base_engine = _baseline(market, SLIP)
    base_stitch = _book(market, _constant_schedule(market, cfg["baseline_A"]), SLIP)
    verification = _match(base_engine, base_stitch)
    verification["warmup_mark_is_initial_cash"] = math.isclose(base_stitch["warmup_mark"], INITIAL_CASH, abs_tol=1e-6)
    verification["training_cutoffs"] = [
        {"fold": fold["fold"], "cutoff": fold["cutoff"], "eval_start": fold["eval"][0], "train_sessions": len(fold["train_scored"])}
        for fold in market["plan"]["folds"]
    ]
    (RESULTS / "verification.json").write_text(json.dumps(verification, indent=2), encoding="utf-8")
    if not verification["matches_engine"] or not verification["warmup_mark_is_initial_cash"]:
        payload = {"status": "BLOCKED", "reason": "continuous book does not match the validated engine"}
        (RESULTS / "evaluation.json").write_text(json.dumps({**payload, "verification": verification}, indent=2), encoding="utf-8")
        _write_report({"status": "BLOCKED", "reason": payload["reason"], "candidates_scored": len(scores), "folds_searched": 4})
        print("BLOCKED stitch mismatch", verification, flush=True)
        return 1

    selector = _book(market, _selector_schedule(market, decisions), SLIP)
    if not selector["reconciled"] or selector["distribution_unpriced"]:
        payload = {"status": "BLOCKED", "reason": "stitched book failed reconciliation"}
        (RESULTS / "evaluation.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return 1
    eval_dates = market["plan"]["eval_dates"]
    spy_div = spy_dividend_map(eval_dates)
    if not spy_div:
        payload = {"status": "BLOCKED", "reason": "SPY cash dividends are missing; not substituting a price-only total return"}
        (RESULTS / "evaluation.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return 1
    spy_bars = {day: market["lookup"]["SPY"][day] for day in eval_dates}
    spy = spy_total_return(eval_dates, spy_bars, spy_div, SLIP, INITIAL_CASH)
    slip2 = 10.0 / 10_000.0
    sens_selector = _book(market, _selector_schedule(market, decisions), slip2)
    sens_base = _baseline(market, slip2)
    sens_spy = spy_total_return(eval_dates, spy_bars, spy_div, slip2, INITIAL_CASH)
    choice_by_fold = {row["fold"]: row for row in decisions}
    # Engine baseline has no execution ledger. The matched stitch supplies the same fold slices.
    payload = {
        "status": "COMPLETED",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidates_scored": len(scores),
        "folds_searched": len(decisions),
        "ineligible": sum(1 for row in scores if not _eligible_flag(row, decisions)),
        "dates": {
            "eval_start": eval_dates[0],
            "eval_end": eval_dates[-1],
            "folds": verification["training_cutoffs"],
        },
        "aggregate": {
            "selector": _public(selector),
            "baseline_A": {k: base_engine[k] for k in (
                "net_return", "ending_equity", "max_drawdown", "avg_exposure", "turnover",
                "trades", "cost_dollars", "dividend_dollars", "reconciled", "start", "end",
            )},
            "spy_total_return": _public(spy),
        },
        "folds": {
            "selector": _fold_rows(selector, market, choice_by_fold),
            "baseline_A": _fold_rows(base_stitch, market, None),
            "spy_total_return": _fold_rows(spy, market, None),
        },
        "doubled_slippage": {
            "slip_bps_per_side": 10,
            "reselection": False,
            "selector": _public(sens_selector),
            "baseline_A": {k: sens_base[k] for k in ("net_return", "ending_equity", "max_drawdown", "avg_exposure", "turnover", "trades", "cost_dollars", "reconciled")},
            "spy_total_return": _public(sens_spy),
        },
        "limitations": [
            "Folds overlap the previously inspected 365-session windows.",
            "Stored prices are split-adjusted through the snapshot. Training does not read sessions after its cutoff.",
            "Cash earns zero interest.",
        ],
    }
    # Annualized volatility for the engine baseline is not in its return dict.
    payload["aggregate"]["baseline_A"]["ann_vol"] = base_stitch["ann_vol"]
    payload["doubled_slippage"]["baseline_A"]["ann_vol"] = _book(
        market, _constant_schedule(market, cfg["baseline_A"]), slip2
    )["ann_vol"]
    (RESULTS / "evaluation.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _write_report(payload)
    print(payload["status"], payload["aggregate"]["selector"]["net_return"], payload["aggregate"]["baseline_A"]["net_return"], flush=True)
    return 0


def _eligible_flag(row: dict, decisions: list[dict]) -> bool:
    for decision in decisions:
        if decision["fold"] != row["fold"]:
            continue
        for candidate in decision["candidates"]:
            if candidate["order"] == row["order"]:
                return bool(candidate["eligible"])
    return False


if __name__ == "__main__":
    raise SystemExit(main())
