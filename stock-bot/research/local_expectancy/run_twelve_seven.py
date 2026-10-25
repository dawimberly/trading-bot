"""12-to-7 momentum versus VTI and extension. Research only. No orders."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from gates import DRAWDOWN_SLACK, _fail_drawdown
from momentum import twelve_seven_return
from run_study import ROOT, bars_of, load_panel

RULES_PATH = ROOT / "preregistration_12_7.json"
PANEL_PATH = ROOT / "panel.json"
OUT_PATH = ROOT / "results_12_7.json"


def _strip(block: dict) -> dict:
    kept = {key: value for key, value in block.items() if key != "daily_returns"}
    return _round(kept)


def _round(value):
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {key: _round(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_round(item) for item in value]
    return value


def evaluate(windows: dict) -> dict:
    cells = []
    failed = False
    for name in ("prior_365", "recent_365"):
        for cost in ("slip_5", "slip_10"):
            block = windows[name][cost]
            reasons = []
            if not block["learner"]["return"] > block["vti_hold"]["return"]:
                reasons.append("vti_return")
            if not block["learner"]["return"] > block["baseline"]["return"]:
                reasons.append("extension_return")
            if _fail_drawdown(block["learner"]["max_drawdown"], block["baseline"]["max_drawdown"]):
                reasons.append("extension_drawdown")
            cells.append({"window": name, "cost": cost, "failures": reasons, "passed": not reasons})
            failed = failed or bool(reasons)
    beats_vti = all("vti_return" not in cell["failures"] for cell in cells)
    return {
        "status": "reject" if failed else "pass",
        "beats_vti_both_windows_both_costs": beats_vti,
        "promotion": False,
        "drawdown_slack_pp": DRAWDOWN_SLACK * 100,
        "cells": cells,
    }


def main(argv: list[str]) -> dict:
    rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    digest = hashlib.sha256(PANEL_PATH.read_bytes()).hexdigest()
    if digest != rules["panel_sha256"]:
        raise SystemExit("panel hash does not match the frozen preregistration")
    panel = load_panel(False, rules)
    bars = bars_of(panel)
    prior = json.loads((ROOT / "results.json").read_text(encoding="utf-8"))
    included = list(prior["symbols_included"])
    sessions = sorted(set(bars["SPY"]) & set(bars["VTI"]))

    def momentum_at(symbol: str, day: str) -> float | None:
        return twelve_seven_return(sessions, bars[symbol], day)

    # run_cell does not forward momentum_at. Call simulate through a local wrapper.
    from engine import hold_nav, path_stats, simulate
    from gates import bootstrap_total_returns

    def run(bounds, slip_bps):
        start = [day for day in sessions if bounds["after"] < day <= bounds["through"]]
        sliced = [day for day in sessions if day <= bounds["through"]]
        result = simulate(
            sliced,
            bars,
            included,
            window_start=start[0],
            window_end=start[-1],
            slip_bps=slip_bps,
            quote_age_seconds=0,
            momentum_at=momentum_at,
        )
        result["learner"]["bootstrap"] = bootstrap_total_returns(result["learner"]["daily_returns"])
        lo = sliced.index(start[0])
        hi = sliced.index(start[-1])
        slip = slip_bps / 10000
        holds = {
            "equal_weight_hold": path_stats(hold_nav(sliced, bars, included, lo, hi, slip)),
            "vti_hold": path_stats(hold_nav(sliced, bars, ["VTI"], lo, hi, slip)),
            "spy_hold": path_stats(hold_nav(sliced, bars, ["SPY"], lo, hi, slip)),
        }
        result["holds"] = holds
        result["window_start"] = start[0]
        result["window_end"] = start[-1]
        result["window_sessions"] = len(start)
        return result

    public = {}
    gate_windows = {}
    for name in rules["windows"]["required"]:
        public[name] = {}
        gate_windows[name] = {}
        for slip, key in ((5, "slip_5"), (10, "slip_10")):
            cell = run(rules["windows"][name], slip)
            public[name][key] = {
                "window_start": cell["window_start"],
                "window_end": cell["window_end"],
                "sessions": cell["window_sessions"],
                "learner": _strip(cell["learner"]),
                "baseline": _strip(cell["baseline"]),
                "vti_hold": _round(
                    {
                        "return": cell["holds"]["vti_hold"]["return"],
                        "max_drawdown": cell["holds"]["vti_hold"]["max_drawdown"],
                        "sharpe": cell["holds"]["vti_hold"]["sharpe"],
                    }
                ),
                "spy_hold": _round(
                    {
                        "return": cell["holds"]["spy_hold"]["return"],
                        "max_drawdown": cell["holds"]["spy_hold"]["max_drawdown"],
                    }
                ),
                "equal_weight_hold": _round(
                    {
                        "return": cell["holds"]["equal_weight_hold"]["return"],
                        "max_drawdown": cell["holds"]["equal_weight_hold"]["max_drawdown"],
                    }
                ),
            }
            gate_windows[name][key] = {
                "learner": cell["learner"],
                "baseline": cell["baseline"],
                "vti_hold": cell["holds"]["vti_hold"],
            }

    verdict = evaluate(gate_windows)
    payload = {
        "hypothesis_id": rules["hypothesis_id"],
        "status": verdict["status"],
        "beats_vti_both_windows_both_costs": verdict["beats_vti_both_windows_both_costs"],
        "promotion": False,
        "annual_target_claim": False,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "panel_sha256": digest,
        "symbols_included": included,
        "windows": public,
        "cells": verdict["cells"],
        "entry_quote_max_age_seconds": 420,
        "drawdown_slack_pp": verdict["drawdown_slack_pp"],
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": payload["status"],
                "beats_vti_both_windows_both_costs": payload["beats_vti_both_windows_both_costs"],
                "promotion": False,
                "cells": payload["cells"],
            },
            indent=2,
        )
    )
    return payload


if __name__ == "__main__":
    main(sys.argv[1:])
