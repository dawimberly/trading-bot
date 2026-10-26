"""Write the immutable specification before any evaluation result."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ABC = HERE.parent / "nyse_cash_abc"
WFS = HERE.parent / "walk_forward_select"
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

from io_util import dump, sha256_file  # noqa: E402
from market import load_market  # noqa: E402
from plan import INITIAL_CASH, MAX_DRAWDOWN, MIN_COMPLETED, baseline_spec, candidates  # noqa: E402

FILES = [
    ABC / "data" / "bars.sqlite",
    ABC / "data" / "spy_distributions.json",
    ABC / "data" / "unapplied_actions.json",
    ABC / "data" / "manifest.json",
    ABC / "data" / "calendar.json",
    ABC / "engine.py",
    ABC / "membership.py",
    ABC / "run_abc.py",
    ABC / "run_rotation.py",
    WFS / "stitch.py",
    WFS / "data_access.py",
    WFS / "metrics.py",
    ROOT / "data" / "sp500_ticker_start_end.csv",
    ROOT / "modules" / "pit_universe.py",
    HERE / "plan.py",
    HERE / "select_rule.py",
    HERE / "market.py",
    HERE / "score.py",
    HERE / "books.py",
    HERE / "report.py",
    HERE / "runner.py",
    HERE / "io_util.py",
    HERE / "test_learning.py",
    HERE / "data_audit.json",
    HERE / "REPRODUCE.md",
]


def main() -> int:
    if (HERE / "preregistration.json").is_file():
        raise SystemExit("preregistration.json already exists")
    audit_path = HERE / "data_audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["clarifications_before_results"] = {
        "calendar": "SQLite has 949 usable SPY weekdays from 2023-01-03 through 2026-10-14. manifest calendar.json starts 2023-07-18 and has 815 sessions. Same snapshot and split adjustment. Coverage on the 949 sessions is 99.924 percent with zero interior holes and zero invalid OHLC rows.",
        "not_1000": "The longest complete alignment is 24 folds, 720 evaluation sessions. This is not a 1,000-session study.",
        "para": "PARA rows in this sqlite are the stored ChartExchange correction from 2024-04-24 through 2025-08-06. PARA is not dropped. sim_2021_2026_v2 stays blocked and is not used.",
        "vti": "No VTI bars and no VTI dividends. The benchmark is unavailable.",
        "intraday": "The intraday cache is a separate limited universe and is not combined with this daily study.",
    }
    audit_path.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    market = load_market()
    plan = market["plan"]
    if plan["evaluation_sessions"] != 720 or plan["calendar_sessions"] != 949:
        raise SystemExit(f"unexpected plan {plan['evaluation_sessions']} {plan['calendar_sessions']}")
    if plan["first_eval"] != "2023-11-30" or plan["last_eval"] != "2026-10-14":
        raise SystemExit("unexpected evaluation bounds")
    files = {str(path): sha256_file(path) for path in FILES}
    spec = {
        "experiment": "learn_walk_100_30",
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "immutable": True,
        "no_deployment": True,
        "is_1000_session_study": False,
        "hypothesis": "A causal rule that, at each cutoff, freezes the eligible member of the existing 27-candidate MA/hold/ATR grid with the highest open-book net return on the preceding 100 sessions has a higher continuous stitched net return on the following non-overlapping 30-session windows than holding baseline A fixed.",
        "experimental_change": "One change: the parameter-selection policy over the existing 27-candidate grid. Changing moving average, hold, and ATR together is not a one-knob production promotion.",
        "dataset": {
            "identity": "research/nyse_cash_abc/data/bars.sqlite",
            "sha256": files[str(ABC / "data" / "bars.sqlite")],
            "adjustment": "Alpaca SIP Adjustment.SPLIT. Prices are not dividend-adjusted.",
            "session_cutoff": "2026-10-14",
            "usable_sessions": 949,
            "calendar_start": "2023-01-03",
            "calendar_end": "2026-10-14",
        },
        "simulator": {
            "training": "walk_forward_select/stitch.py simulate_continuous with liquidate_terminal false",
            "evaluation": "the same function with liquidate_terminal true, constant or scheduled parameters",
            "fidelity": "A constant baseline schedule must match nyse_cash_abc/engine.py simulate before any fold is scored.",
            "stitch_sha256": files[str(WFS / "stitch.py")],
            "engine_sha256": files[str(ABC / "engine.py")],
        },
        "baseline": {
            **baseline_spec(),
            "atr_period": 14,
            "atr_definition": "mean absolute close-to-close change over 14 sessions ending at the decision close",
            "rank": "close / moving average - 1, highest first, then ticker",
            "eligibility": "close above that moving average",
            "names": 15,
            "sleeve": 0.95,
            "initial_cash": INITIAL_CASH,
            "entries": "next session open after the decision close",
            "stop": "entry fill minus ATR multiplier times ATR, floor 0.01",
            "hold": "sell at the close when the session index is at least max_hold sessions after the entry session",
            "long_only": True,
            "leverage": False,
            "cash_interest": 0,
        },
        "candidates": candidates(),
        "rejected_not_in_this_run": [
            "profit target",
            "close trail armed at +5% / 3% under the peak close",
            "volatility entry filter",
            "stall rotation",
            "risk-adjusted MA rank",
            "12% volatility target",
            "fat-loser open drip",
            "Friday flatten and other killed timing rules",
            "spherical or euclidean adaptive risk-off",
            "20-session return, 60-session return, and 60-session near-high rankers",
            "Lab +5% arm / 2% observed-peak / 30-minute reentry, which is path-dependent and is not represented by a daily-close proxy",
        ],
        "parameters_fixed": {
            "max_names": 15,
            "sleeve": 0.95,
            "atr_period": 14,
            "initial_cash": INITIAL_CASH,
            "slip_bps_per_side": 5,
            "stress_slip_bps_per_side": 10,
            "stress_reselection": False,
        },
        "training_and_evaluation": {
            "warmup_sessions": 115,
            "warmup_rule": "Longest grid moving average, 100, plus the validated engine's 15-session buffer. Warmup is strictly before the 100 scored training sessions.",
            "train_sessions": 100,
            "eval_sessions": 30,
            "folds": 24,
            "evaluation_sessions": 720,
            "roll_sessions": 30,
            "training_capital": "Each training replay starts at $100,000 with no carried positions. It is the selection score, not the reported strategy.",
            "training_mark": "Open positions are marked at the cutoff close. That unrealized result is in the training return. Synthetic terminal liquidations are not created and are not counted as completed trades. Distribution fractions are not completed strategy round trips.",
            "evaluation_ledger": "One continuous cash and position ledger. At a boundary, cash, holdings, entry basis, and the entry-time stop and hold remain. New entries, including an order queued at the cutoff close, use the newly selected policy.",
            "halves": "First 12 folds and last 12 folds of the continuous book.",
        },
        "plan": plan,
        "selection": {
            "rule": "Highest training net return among eligible candidates.",
            "eligibility": {
                "reconciled": True,
                "no_unresolved_held_action": True,
                "max_drawdown_at_most": MAX_DRAWDOWN,
                "min_completed_round_trips": MIN_COMPLETED,
            },
            "ties": "lower max drawdown, then lower turnover, then lower candidate order",
            "fallback": "If none is eligible, keep baseline A and record baseline_fallback.",
            "sample_rule_frozen": "The 30-trade and 25 percent drawdown gates are not reduced after results.",
            "material_action_limits": {"event": 1000.0, "total": 2000.0, "source": "nyse_cash_abc/run_rotation.py"},
        },
        "costs": {
            "primary_bps_per_side": 5,
            "round_trip_bps": 10,
            "stress_bps_per_side": 10,
            "fee": "Slippage is on the execution price only. It is not also a fee.",
            "fold_transition_and_terminal": "Charged when those trades happen. The final evaluation close is liquidated. Training does not liquidate.",
        },
        "dividends": "Ex-date cash, split-adjusted onto the stored share count, matching the trusted baseline. Payable-date receivables are supported by the stitch engine and are not used, because using them would break reproduction of the saved baseline. Later split factors already embedded in the stored bars scale dividend rates. The training date array still excludes later sessions.",
        "corporate_actions": "GE to GEV 0.25 on 2024-04-02 and the FI to FISV rename on 2025-11-11 are applied. Other stored actions are not invented. A held name that crosses one beyond the existing materiality limits makes that candidate ineligible and, on the evaluation book, fails the study.",
        "pass_rule": [
            "stitched net return after 5 bps per side beats the fixed baseline",
            "stitched max drawdown is not more than 2 percentage points worse",
            "beats the baseline in both chronological halves",
            "at least three completed evaluation folds",
            "remains ahead of the baseline at 10 bps per side without reselection",
            "no unresolved accounting, stale-price, lookahead, or held-action defects",
        ],
        "kill_rule": "Any failed required condition is REJECT if the books are clean and INCONCLUSIVE if a defect makes the comparison untrustworthy. No post-result retune. Beating the baseline is not beating SPY or VTI. A research pass does not deploy.",
        "comparisons": [
            "continuous fixed baseline",
            "rolling learned selector",
            "candidate chosen on the first training window and then held fixed",
            "SPY buy-and-hold total return from stored SPY distributions",
            "VTI only if verified bars and dividends exist; otherwise unavailable, not zero",
            "hindsight best constant candidate, labeled unattainable, after selections are saved, and forbidden from affecting them",
        ],
        "compute_budget": {
            "max_wall_clock_hours": 6,
            "workers": 1,
            "priority": "below normal",
            "scheduled_task": False,
            "candidate_cap": 27,
        },
        "previously_exposed": "Every evaluation session from 2023-11-30 through 2026-10-14 falls inside the previously scored daily windows 2023-11-15 through 2026-10-14. Those windows were used by the cash A/B/C study, the 504/63 walk-forward, the 2026-10-19 profit screen, and the spherical-state study. This is previously exposed history, not a pristine holdout. The future decision-tape study's unopened validation outcomes were not read. Rolling 100/30 folds do not replace the protocol's disjoint-window confirmation or forward paper evidence.",
        "employed_books_not_tested": "Portal launch overlays, read and not modified: Lab alpaca_paper is concentrated 8-name five-minute MA50 with the +5 percent arm / 2 percent observed-peak / 30-minute reentry experiment. Medium alpaca_paper_v2 is 15 names, 2x ATR, 30-bar hold, VTI off, 95 percent sleeve. This daily grid does not reproduce either book.",
        "scope": "Research only, inside this directory. Live, Medium, Lab, their supervisors, scheduled tasks, broker orders, .env files, portal overlays, desktop launchers, and production strategy modules are out of scope.",
        "design_summary": "Frozen daily cash tape, 949 usable sessions, 24 non-overlapping 30-session evaluation folds (720 sessions) after a 100-session causal training window and 115 sessions of indicator warmup. Not a 1,000-session study. VTI is unavailable. sim_2021_2026_v2 was not used.",
        "files": files,
        "credentials_printed": False,
    }
    dump(HERE / "preregistration.json", spec)
    digest = sha256_file(HERE / "preregistration.json")
    dump(
        HERE / "source_hashes.json",
        {"preregistration_sha256": digest, "files": files, "frozen_at": spec["frozen_at"]},
    )
    dump(
        HERE / "candidate_manifest.json",
        {
            "count": 27,
            "baseline_id": "ma70_hold30_atr2.0",
            "candidates": candidates(),
            "one_experimental_change": "selection over this grid",
            "excluded": spec["rejected_not_in_this_run"],
        },
    )
    print(digest)
    print(plan["folds"], plan["evaluation_sessions"], plan["first_eval"], plan["last_eval"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
