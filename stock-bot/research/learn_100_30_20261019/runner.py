"""Offline walk-forward learner. One worker. No broker imports. No scheduled task."""

from __future__ import annotations

import ctypes
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from books import (  # noqa: E402
    engine_matches_stitch,
    eval_path,
    fixed_schedule,
    fold_rows,
    learner_schedule,
    public_book,
    run_continuous,
    run_engine_baseline,
    slice_return,
    spec_dict,
    spy_book,
    trade_stats,
    year_slices,
)
from io_util import dump, load, sha256_file  # noqa: E402
from market import load_market  # noqa: E402
from plan import INITIAL_CASH, baseline_spec, candidates  # noqa: E402
from report import write_report  # noqa: E402
from score import score_candidate  # noqa: E402
from select_rule import select_winner  # noqa: E402

SLIP5 = 5.0 / 10_000.0
SLIP10 = 10.0 / 10_000.0
DEADLINE_S = 6 * 3600
FOLDS = HERE / "folds"
PROGRESS = HERE / "progress.json"
SPEC = HERE / "preregistration.json"
HASHES = HERE / "source_hashes.json"
LOG = HERE / "run.log"
BANNED = ("alpaca_trade_api", "modules.alpaca_client", "modules.alpaca_executor", "modules.portal_bot")
RULES = {
    "max_drawdown": 0.25,
    "min_completed_round_trips": 30,
    "baseline_id": "ma70_hold30_atr2.0",
}


def say(message: str) -> None:
    print(message, flush=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(message + "\n")


def set_below_normal() -> None:
    kernel = ctypes.windll.kernel32
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.SetPriorityClass.restype = ctypes.c_int
    handle = kernel.GetCurrentProcess()
    if not kernel.SetPriorityClass(handle, 0x00004000):
        raise RuntimeError(f"could not set below-normal priority ({kernel.GetLastError()})")


def tripwire() -> None:
    bad = [name for name in sys.modules if name in BANNED or name.startswith("alpaca.")]
    if bad:
        raise RuntimeError("broker client loaded: " + ", ".join(bad))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def elapsed_seconds(start: str) -> float:
    began = datetime.fromisoformat(start)
    return (datetime.now(timezone.utc) - began).total_seconds()


def verify_hashes() -> dict:
    spec = load(SPEC)
    recorded = load(HASHES)
    for path_text, expected in spec["files"].items():
        got = sha256_file(Path(path_text))
        if got != expected:
            raise RuntimeError(f"hash mismatch for {path_text}")
    spec_hash = sha256_file(SPEC)
    if recorded.get("preregistration_sha256") != spec_hash:
        raise RuntimeError("preregistration hash does not match source_hashes.json")
    return spec


def check_plan(spec: dict, market: dict) -> None:
    got = market["plan"]
    want = spec["plan"]
    keys = ("folds", "evaluation_sessions", "first_eval", "last_eval", "first_train", "calendar_sessions")
    for key in keys:
        if got[key] != want[key]:
            raise RuntimeError(f"plan {key} drifted")
    if got["fold_bounds"][0]["eval_dates"] != want["fold_bounds"][0]["eval_dates"]:
        raise RuntimeError("fold dates drifted")
    if got["fold_bounds"][-1]["eval_end"] != want["fold_bounds"][-1]["eval_end"]:
        raise RuntimeError("last fold drifted")
    if got["eval_history"] != want["eval_history"]:
        raise RuntimeError("evaluation history drifted")


def write_progress(start: str, spec_hash: str, **fields) -> None:
    payload = {
        "pid": os.getpid(),
        "start_time": start,
        "elapsed_seconds": round(elapsed_seconds(start), 3),
        "spec_sha256": spec_hash,
        "credentials_printed": False,
        "last_error": None,
    }
    payload.update(fields)
    dump(PROGRESS, payload)


def selection_path(fold: int) -> Path:
    return FOLDS / f"fold_{fold:02d}_selection.json"


def training_path(fold: int) -> Path:
    return FOLDS / f"fold_{fold:02d}_training.json"


def prefix_path(fold: int) -> Path:
    return FOLDS / f"fold_{fold:02d}_prefix.json"


def train_one(market: dict, fold: dict, spec_hash: str) -> None:
    if selection_path(fold["fold"]).is_file():
        prior = load(selection_path(fold["fold"]))
        if prior.get("spec_sha256") != spec_hash:
            raise RuntimeError("saved selection hash does not match this specification")
        return
    rows = []
    base = None
    for spec in candidates():
        row = score_candidate(market, fold, spec, SLIP5)
        rows.append(row)
        if row["order"] == baseline_spec()["order"]:
            base = row
        say(
            f"fold {fold['fold']} scored {spec['candidate_id']} "
            f"return={row['net_return']:.4f} trades={row['completed_round_trips']} dd={row['max_drawdown']:.3f}"
        )
    if base is None or not base["cash_identity_ok"]:
        raise RuntimeError("baseline training cash identity failed; refusing a silent fallback")
    decision = select_winner(rows, RULES)
    dump(
        training_path(fold["fold"]),
        {
            "spec_sha256": spec_hash,
            "fold": fold["fold"],
            "cutoff": fold["cutoff"],
            "train_start": fold["train_start"],
            "train_end": fold["train_end"],
            "rows": decision["candidates"],
        },
    )
    dump(
        selection_path(fold["fold"]),
        {
            "spec_sha256": spec_hash,
            "fold": fold["fold"],
            "cutoff": fold["cutoff"],
            "choice": decision["choice"],
            "reason": decision["reason"],
            "selected": decision["selected"],
            "eligible_count": decision["eligible_count"],
            "candidate_count": decision["candidate_count"],
            "saved_before_evaluation": True,
            "saved_at": now(),
        },
    )
    say(
        f"fold {fold['fold']} selected {decision['selected']['candidate_id']} "
        f"via {decision['choice']} eligible {decision['eligible_count']}/27"
    )


def load_choices(folds: list[dict], spec_hash: str) -> dict[int, dict]:
    choices = {}
    for fold in folds:
        payload = load(selection_path(fold["fold"]))
        if payload.get("spec_sha256") != spec_hash:
            raise RuntimeError("selection hash mismatch")
        if not payload.get("saved_before_evaluation"):
            raise RuntimeError("selection was not saved before evaluation")
        selected = payload["selected"]
        choices[fold["fold"]] = {
            **spec_dict(selected),
            "candidate_id": selected["candidate_id"],
            "choice": payload["choice"],
            "completed_round_trips": selected["completed_round_trips"],
            "reason": payload["reason"],
        }
    return choices


def save_prefix(market: dict, folds: list[dict], choices: dict, fold: dict, spec_hash: str) -> None:
    path = prefix_path(fold["fold"])
    if path.is_file() and load(path).get("spec_sha256") == spec_hash:
        return
    history = market["plan"]["eval_history"]
    dates = history[: history.index(fold["eval_end"]) + 1]
    used = [row for row in folds if row["fold"] <= fold["fold"]]
    used_choices = {row["fold"]: choices[row["fold"]] for row in used}
    schedule = learner_schedule(dates, used, used_choices)
    last = fold["fold"] == folds[-1]["fold"]
    out = run_continuous(market, dates, schedule, SLIP5, liquidate=last)
    dump(
        path,
        {
            "spec_sha256": spec_hash,
            "fold": fold["fold"],
            "end": fold["eval_end"],
            "liquidate": last,
            "ending_equity": out["ending_equity"],
            "reconciled": out["reconciled"],
        },
    )


def equity_on(result: dict, day: str) -> float:
    return float(result["equity_path"][result["equity_dates"].index(day)])


def defect_list(book: dict) -> list[str]:
    found = []
    if not book.get("reconciled"):
        found.append("unreconciled accounting")
    if book.get("distribution_unpriced"):
        found.append("unpriced distribution")
    if book.get("unresolved_holdings"):
        found.append("unresolved holding")
    if book.get("stale_terminals"):
        found.append("stale terminal price")
    if book.get("valuation_inconclusive"):
        found.append("inconclusive valuation")
    if book.get("action_blocked"):
        found.append("held corporate action outside the priced tolerance")
    return found


def summarize_spy(result: dict, eval_dates: list[str]) -> dict:
    from books import path_stats

    stats = path_stats(eval_path(result, eval_dates), eval_dates)
    stats["turnover"] = result["turnover"]
    stats["cost_dollars"] = result["cost_dollars"]
    stats["avg_exposure"] = result["avg_exposure"]
    stats["dividend_dollars"] = result.get("dividend_cash")
    return stats


def finish_report(payload: dict) -> None:
    dump(HERE / "results.json", payload["results"])
    text = write_report(payload["report"])
    (HERE / "MORNING_REPORT.md").write_text(text, encoding="utf-8")


def main() -> int:
    set_below_normal()
    tripwire()
    spec = verify_hashes()
    spec_hash = sha256_file(SPEC)
    if PROGRESS.is_file():
        prior = load(PROGRESS)
        if prior.get("spec_sha256") not in (None, spec_hash):
            raise RuntimeError("progress.json belongs to a different specification")
        if prior.get("status") == "BLOCKED":
            say("blocked run will not resume")
            return 2
        if prior.get("status") == "COMPLETED":
            say("already completed")
            return 0
        start = prior["start_time"]
    else:
        start = now()
    market = load_market()
    check_plan(spec, market)
    folds = market["plan"]["fold_bounds"]
    history = market["plan"]["eval_history"]
    eval_dates = [day for fold in folds for day in fold["eval_dates"]]
    write_progress(start, spec_hash, status="RUNNING", current_fold=0, completed_folds=0)
    try:
        return _run(spec, spec_hash, start, market, folds, history, eval_dates)
    except Exception as exc:
        write_progress(start, spec_hash, status="BLOCKED", current_fold=None, completed_folds=None, last_error=str(exc))
        say(traceback.format_exc())
        blocked = {
            "status": "BLOCKED",
            "error": str(exc),
            "credentials_printed": False,
            "production_changed": False,
        }
        dump(HERE / "results.json", blocked)
        report = _shell_report("BLOCKED", f"The run stopped: {exc}")
        (HERE / "MORNING_REPORT.md").write_text(write_report(report), encoding="utf-8")
        return 1


def _run(spec, spec_hash, start, market, folds, history, eval_dates) -> int:
    fidelity_path = HERE / "fidelity.json"
    if not fidelity_path.is_file():
        say("checking stitch against the frozen engine")
        base = baseline_spec()
        schedule = fixed_schedule(history, folds, base)
        stitched = run_continuous(market, history, schedule, SLIP5, liquidate=True)
        engine = run_engine_baseline(market, history, SLIP5)
        match = engine_matches_stitch(engine, stitched)
        dump(fidelity_path, match)
        if not match["matches"]:
            raise RuntimeError("continuous harness does not reproduce the frozen engine")
        say("engine and continuous baseline match")
    for fold in folds:
        if elapsed_seconds(start) > DEADLINE_S:
            return _incomplete(spec, spec_hash, start, "six-hour limit before training finished")
        write_progress(
            start,
            spec_hash,
            status="RUNNING",
            current_fold=fold["fold"],
            completed_folds=fold["fold"] - 1,
        )
        train_one(market, fold, spec_hash)
        choices = load_choices(folds[: fold["fold"]], spec_hash)
        save_prefix(market, folds, choices, fold, spec_hash)
        write_progress(
            start,
            spec_hash,
            status="RUNNING",
            current_fold=fold["fold"],
            completed_folds=fold["fold"],
        )
        say(f"fold {fold['fold']} checkpoint saved")
    if elapsed_seconds(start) > DEADLINE_S:
        return _incomplete(spec, spec_hash, start, "six-hour limit before the stitched evaluation")
    choices = load_choices(folds, spec_hash)
    say("running continuous books")
    learner = run_continuous(market, history, learner_schedule(history, folds, choices), SLIP5, liquidate=True)
    for fold in folds:
        prefix = load(prefix_path(fold["fold"]))
        got = equity_on(learner, fold["eval_end"]) if not prefix["liquidate"] else float(learner["ending_equity"])
        if abs(got - float(prefix["ending_equity"])) > 1e-4:
            raise RuntimeError(f"fold {fold['fold']} prefix equity does not match the full book")
    baseline = run_continuous(market, history, fixed_schedule(history, folds, baseline_spec()), SLIP5, liquidate=True)
    first = choices[1]
    first_book = run_continuous(market, history, fixed_schedule(history, folds, first), SLIP5, liquidate=True)
    spy = spy_book(market, eval_dates, SLIP5)
    say("running 10 bps stress without reselection")
    stress_learner = run_continuous(market, history, learner_schedule(history, folds, choices), SLIP10, liquidate=True)
    stress_base = run_continuous(market, history, fixed_schedule(history, folds, baseline_spec()), SLIP10, liquidate=True)
    stress_spy = spy_book(market, eval_dates, SLIP10)
    hindsight = None
    if elapsed_seconds(start) + 1200 < DEADLINE_S:
        hindsight = _hindsight(market, history, folds, eval_dates, start)
    payload = _payload(
        spec,
        folds,
        eval_dates,
        choices,
        learner,
        baseline,
        first_book,
        spy,
        stress_learner,
        stress_base,
        stress_spy,
        hindsight,
    )
    _write_outputs(payload)
    write_progress(start, spec_hash, status="COMPLETED", current_fold=len(folds), completed_folds=len(folds))
    say(f"completed verdict {payload['report']['verdict']}")
    return 0


def _hindsight(market, history, folds, eval_dates, start) -> dict:
    rows = []
    for spec in candidates():
        if elapsed_seconds(start) > DEADLINE_S - 60:
            return {"complete": False, "rows": rows, "note": "Stopped before every constant book finished. Not a ceiling."}
        out = run_continuous(market, history, fixed_schedule(history, folds, spec), SLIP5, liquidate=True)
        summary = public_book(out, eval_dates)
        rows.append(
            {
                "candidate_id": spec["candidate_id"],
                "net_return": summary["net_return"],
                "max_drawdown": summary["max_drawdown"],
                "action_blocked": summary["action_blocked"],
            }
        )
        say(f"hindsight {spec['candidate_id']} {summary['net_return']:.4f}")
    best = max(rows, key=lambda row: (row["net_return"], -row["max_drawdown"], row["candidate_id"]))
    return {
        "complete": True,
        "best": best,
        "rows": rows,
        "note": "Unattainable. Chosen after all evaluation results. It did not affect the learner.",
    }


def _payload(spec, folds, eval_dates, choices, learner, baseline, first_book, spy, stress_l, stress_b, stress_s, hindsight):
    learn = public_book(learner, eval_dates)
    base = public_book(baseline, eval_dates)
    first = public_book(first_book, eval_dates)
    spy_summary = summarize_spy(spy, eval_dates)
    stress = {
        "learner": public_book(stress_l, eval_dates),
        "baseline": public_book(stress_b, eval_dates),
        "spy": summarize_spy(stress_s, eval_dates),
    }
    mid = len(folds) // 2
    first_dates = [day for fold in folds[:mid] for day in fold["eval_dates"]]
    second_dates = [day for fold in folds[mid:] for day in fold["eval_dates"]]
    halves = {
        "learner_first": slice_return(learner, first_dates),
        "learner_second": slice_return(learner, second_dates),
        "baseline_first": slice_return(baseline, first_dates),
        "baseline_second": slice_return(baseline, second_dates),
    }
    fold_table = []
    for row in fold_rows(learner, folds, choices):
        chosen = choices[row["fold"]]
        row["candidate_id"] = chosen["candidate_id"]
        row["fallback"] = chosen["choice"] == "baseline_fallback"
        row["train_completed"] = chosen["completed_round_trips"]
        fold_table.append(row)
    base_folds = {row["fold"]: row for row in fold_rows(baseline, folds, None)}
    for row in fold_table:
        row["baseline_return"] = base_folds[row["fold"]]["net_return"]
    years = []
    for left, right in zip(year_slices(learner, eval_dates), year_slices(baseline, eval_dates)):
        spy_year = year_slices(spy, eval_dates)
        match = next(item for item in spy_year if item["year"] == left["year"])
        years.append(
            {
                "year": left["year"],
                "learner_sessions": left["sessions"],
                "learner_return": left["net_return"],
                "baseline_return": right["net_return"],
                "spy_return": match["net_return"],
            }
        )
    defects = sorted(set(defect_list(learn) + defect_list(base) + defect_list(first)))
    failures = []
    if learn["net_return"] <= base["net_return"]:
        failures.append("stitched net return did not beat the fixed baseline")
    if learn["max_drawdown"] > base["max_drawdown"] + 0.02 + 1e-12:
        failures.append("stitched max drawdown was more than 2 percentage points worse")
    if not (halves["learner_first"] > halves["baseline_first"] and halves["learner_second"] > halves["baseline_second"]):
        failures.append("did not beat the baseline in both chronological halves")
    if len(folds) < 3:
        failures.append("fewer than three evaluation folds")
    if stress["learner"]["net_return"] <= stress["baseline"]["net_return"]:
        failures.append("not ahead of the baseline at 10 bps per side")
    if defects:
        failures.append("unresolved accounting, price, or corporate-action defects")
    if defects:
        verdict = "INCONCLUSIVE"
    elif failures:
        verdict = "REJECT"
    else:
        verdict = "PASS_RESEARCH_ONLY"
    worst = min(fold_table, key=lambda row: row["net_return"])
    worst_text = (
        f"fold {worst['fold']} {worst['start']} → {worst['end']}, {worst['candidate_id']}, "
        f"return {worst['net_return']:.2%}, max drawdown {worst['max_drawdown']:.2%}"
    )
    conc = learn["trades"]
    share = conc.get("top_symbol_share_of_positive_pnl")
    top = conc.get("top_symbol")
    if share is not None and share >= 0.5 and top:
        concentration = (
            f"Completed strategy-trade profits are concentrated: {top['symbol']} contributed "
            f"{100 * share:.1f}% of positive completed-trade PnL. "
        )
    else:
        concentration = "No single completed-trade symbol accounted for half of positive completed-trade PnL. "
    positive_folds = [row["net_return"] for row in fold_table if row["net_return"] > 0]
    if positive_folds and max(row["net_return"] for row in fold_table) > 0:
        best_fold = max(fold_table, key=lambda row: row["net_return"])
        concentration += (
            f"The strongest fold was {best_fold['fold']} at {best_fold['net_return']:.2%}. "
            f"Halves were {halves['learner_first']:.2%} and {halves['learner_second']:.2%}."
        )
    answer_learning = (
        f"The learned selector returned {learn['net_return']:.2%} versus the fixed baseline {base['net_return']:.2%} "
        f"({(learn['net_return'] - base['net_return']) * 100:.2f} percentage points) on the same {len(eval_dates)} sessions. "
        + (
            "That is higher than the baseline, and the frozen research gate passed. It is still not a deployment result."
            if verdict == "PASS_RESEARCH_ONLY"
            else "The frozen research gate did not pass. " + "; ".join(failures) + "."
        )
    )
    spy_gap = learn["net_return"] - spy_summary["net_return"]
    answer_market = (
        f"On these dates the selector {'finished ahead of' if spy_gap > 0 else 'did not beat'} SPY total return "
        f"({spy_summary['net_return']:.2%}; gap {spy_gap * 100:.2f} percentage points). "
        "VTI is unavailable: the frozen tape has no VTI bars and no VTI dividends. An unavailable benchmark is not zero."
    )
    unknowns = [
        "The evaluation dates sit inside history already used by earlier daily studies. This is not a pristine holdout.",
        "The tape has 949 usable sessions, so the aligned study is 720 evaluation sessions, not 1,000.",
        "Daily MA70/grid research does not reproduce the employed five-minute MA50 books.",
        "The Lab +5% arm / 2% observed-peak / 30-minute reentry rule was not tested.",
        "Training replays start from $100,000 cash. The reported strategy is the continuous evaluation ledger.",
        "Dividends are credited on the ex-date, matching the trusted baseline. Payable-date receivables were not used.",
    ]
    if defects:
        unknowns.append("Defects: " + "; ".join(defects) + ".")
    if hindsight is None:
        unknowns.append("The hindsight ceiling was not computed.")
    answer_next = {
        "PASS_RESEARCH_ONLY": "The smallest next experiment is a pre-registered prospective paper shadow on sessions after 2026-10-14, with no orders and no grid change. This 100/30 study does not replace the protocol's disjoint-window confirmation.",
        "REJECT": "Do not retune this grid and do not revive rejected families on these dates. Leave the selector undeployed. A new family needs its own queue entry and a later window.",
        "INCONCLUSIVE": "Do not rank a winner from books with unresolved defects. The next experiment is to price the reported held-action or stale-price events, then rerun this same frozen rule on a clean tape.",
    }[verdict]
    results = {
        "status": "COMPLETED",
        "verdict": verdict,
        "failures": failures,
        "defects": defects,
        "evaluation_sessions": len(eval_dates),
        "is_1000_session_study": False,
        "decision_dates": len(folds),
        "learner": learn,
        "baseline": base,
        "first_window_fixed": first,
        "spy": spy_summary,
        "vti": None,
        "stress_10bps": stress,
        "halves": halves,
        "folds": fold_table,
        "years": years,
        "hindsight": hindsight,
        "credentials_printed": False,
        "production_changed": False,
        "previously_exposed": True,
    }
    report = {
        "status": "COMPLETED",
        "verdict": verdict,
        "answer_learning": answer_learning,
        "answer_market": answer_market,
        "answer_unknown": " ".join(unknowns),
        "answer_next": answer_next,
        "scope": spec["scope"],
        "design": spec["design_summary"],
        "learner": learn,
        "baseline": base,
        "first_fixed": first,
        "spy": spy_summary,
        "stress": stress,
        "learner_minus_baseline_pp": (learn["net_return"] - base["net_return"]) * 100,
        "half_fold_counts": [mid, len(folds) - mid],
        "halves": halves,
        "worst_fold": worst_text,
        "concentration": concentration,
        "decision_dates": f"{len(folds)} cutoffs from {folds[0]['cutoff']} through {folds[-1]['cutoff']}",
        "folds": fold_table,
        "years": years,
        "hindsight": None
        if not hindsight
        else (
            f"{hindsight['note']} Best constant candidate: {hindsight['best']['candidate_id']} at "
            f"{hindsight['best']['net_return']:.2%}."
            if hindsight.get("complete")
            else hindsight["note"]
        ),
        "defects_text": "No unresolved accounting, stale-price, or held-action defects in the three 5 bps books."
        if not defects
        else "Defects: " + "; ".join(defects) + ".",
        "exposure_text": spec["previously_exposed"],
    }
    return {
        "results": results,
        "report": report,
        "learner_raw_trades": learner["trade_log"],
        "baseline_raw_trades": baseline["trade_log"],
        "first_raw_trades": first_book["trade_log"],
        "learner_equity": eval_path(learner, eval_dates),
        "baseline_equity": eval_path(baseline, eval_dates),
    }


def _write_outputs(payload: dict) -> None:
    def slim(trades: list[dict]) -> list[dict]:
        keys = ("symbol", "entry_date", "exit_date", "reason", "pnl", "shares", "entry_ma_win", "entry_max_hold", "entry_atr_mult")
        return [{key: row.get(key) for key in keys} for row in trades]

    dump(
        HERE / "continuous_ledger.json",
        {
            "learner_trades": slim(payload["learner_raw_trades"]),
            "baseline_trades": slim(payload["baseline_raw_trades"]),
            "first_window_trades": slim(payload["first_raw_trades"]),
            "learner_equity": payload["learner_equity"],
            "baseline_equity": payload["baseline_equity"],
            "trade_stats_learner": trade_stats(payload["learner_raw_trades"]),
        },
    )
    dump(HERE / "benchmark_results.json", {"spy": payload["results"]["spy"], "vti": None})
    dump(HERE / "cost_stress.json", payload["results"]["stress_10bps"])
    if payload["results"].get("hindsight"):
        dump(HERE / "hindsight.json", payload["results"]["hindsight"])
    finish_report(payload)


def _incomplete(spec, spec_hash, start, reason: str) -> int:
    write_progress(start, spec_hash, status="INCOMPLETE", current_fold=None, completed_folds=None, last_error=reason)
    dump(HERE / "results.json", {"status": "INCOMPLETE", "reason": reason, "production_changed": False})
    text = write_report(_shell_report("INCOMPLETE", reason))
    (HERE / "MORNING_REPORT.md").write_text(text, encoding="utf-8")
    say(reason)
    return 0


def _shell_report(status: str, reason: str) -> dict:
    return {
        "status": status,
        "verdict": status,
        "answer_learning": "Unknown. No completed stitched comparison is available.",
        "answer_market": "Unknown versus SPY. VTI is unavailable in the frozen tape.",
        "answer_unknown": reason,
        "answer_next": "Finish or repair this same frozen specification. Do not widen the candidate set.",
        "scope": "Research only. No deployment.",
        "design": reason,
        "exposure_text": "Previously exposed daily history. Not a pristine holdout.",
        "defects_text": reason,
    }


if __name__ == "__main__":
    raise SystemExit(main())
