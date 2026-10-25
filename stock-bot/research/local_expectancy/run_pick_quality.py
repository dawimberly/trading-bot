"""Pick quality versus random, the eligible list, and VTI. Research only. No orders."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from pick_quality import (
    BOOTSTRAP_DRAWS,
    CLIP,
    HORIZONS,
    MAX_NAMES,
    NAME_CAP,
    PRIMARY_HORIZON,
    SLEEVE,
    daily_selections,
    decision_rows,
    portfolio_book,
    same_close_bias,
    summarize_horizon,
    trade_exits,
)
from run_study import ROOT, bars_of, load_panel

RULES_PATH = ROOT / "preregistration_pick_quality.json"
PANEL_PATH = ROOT / "panel.json"
OUT_PATH = ROOT / "results_pick_quality.json"


def _round(value):
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {key: _round(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_round(item) for item in value]
    return value


def _books(sessions, bars, decisions, bounds):
    common = dict(
        window_start=bounds[0],
        window_end=bounds[1],
    )
    specs = {
        "full_one_name": dict(clip=1.0, name_cap=1.0, sleeve=1.0, max_names=1, use_stop=False, use_halt=False, slip_bps=0),
        "clip": dict(clip=CLIP, name_cap=NAME_CAP, sleeve=SLEEVE, max_names=MAX_NAMES, use_stop=False, use_halt=False, slip_bps=0),
        "clip_stop": dict(clip=CLIP, name_cap=NAME_CAP, sleeve=SLEEVE, max_names=MAX_NAMES, use_stop=True, use_halt=False, slip_bps=0),
        "clip_stop_halt": dict(clip=CLIP, name_cap=NAME_CAP, sleeve=SLEEVE, max_names=MAX_NAMES, use_stop=True, use_halt=True, slip_bps=0),
        "clip_stop_halt_5bps": dict(clip=CLIP, name_cap=NAME_CAP, sleeve=SLEEVE, max_names=MAX_NAMES, use_stop=True, use_halt=True, slip_bps=5),
        "clip_stop_halt_10bps": dict(clip=CLIP, name_cap=NAME_CAP, sleeve=SLEEVE, max_names=MAX_NAMES, use_stop=True, use_halt=True, slip_bps=10),
    }
    out = {}
    for name, spec in specs.items():
        out[name] = portfolio_book(sessions, bars, decisions, **common, **spec)
    full = out["full_one_name"]["return"]
    clip = out["clip"]["return"]
    stopped = out["clip_stop"]["return"]
    halted = out["clip_stop_halt"]["return"]
    cost5 = out["clip_stop_halt_5bps"]["return"]
    cost10 = out["clip_stop_halt_10bps"]["return"]
    out["signed_gaps"] = {
        "full_minus_clip": full - clip,
        "clip_minus_stop": clip - stopped,
        "stop_minus_halt": stopped - halted,
        "halt_minus_5bps": halted - cost5,
        "bps5_minus_bps10": cost5 - cost10,
    }
    return out


def _clears(pooled: dict, windows: dict) -> bool:
    if pooled.get("days", 0) == 0:
        return False
    if not pooled["excess_vs_random_mean"] > 0:
        return False
    if not pooled["p_value_vs_random"] < 0.05:
        return False
    for name in ("prior_365", "recent_365"):
        block = windows[name][str(PRIMARY_HORIZON)]
        if not block["excess_vs_random_mean"] > 0:
            return False
    return True


def main(argv: list[str]) -> dict:
    rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    digest = hashlib.sha256(PANEL_PATH.read_bytes()).hexdigest()
    if digest != rules["panel_sha256"]:
        raise SystemExit("panel hash does not match the frozen preregistration")
    panel = load_panel(False, rules)
    bars = bars_of(panel)
    included = list(json.loads((ROOT / "results.json").read_text(encoding="utf-8"))["symbols_included"])
    sessions = sorted(set(bars["SPY"]) & set(bars["VTI"]))
    public = {}
    pooled_rows = {horizon: [] for horizon in HORIZONS}
    for name in ("prior_365", "recent_365"):
        bounds = rules["windows"][name]
        start = [day for day in sessions if bounds["after"] < day <= bounds["through"]]
        window_start, window_end = start[0], start[-1]
        public[name] = {"window_start": window_start, "window_end": window_end, "sessions": len(start), "horizons": {}}
        for horizon in HORIZONS:
            rows = decision_rows(
                sessions,
                bars,
                included,
                window_start=window_start,
                window_end=window_end,
                horizon=horizon,
            )
            pooled_rows[horizon].extend(rows)
            summary = summarize_horizon(rows, bars, sessions, horizon, draws=BOOTSTRAP_DRAWS)
            if horizon == PRIMARY_HORIZON:
                summary["same_close_fill_bias"] = same_close_bias(sessions, bars, rows, horizon)
                summary["exits"] = trade_exits(
                    sessions, bars, rows, last_index=sessions.index(window_end)
                )
                selections = daily_selections(
                    sessions, bars, included, window_start=window_start, window_end=window_end
                )
                decisions = {row["decision_index"]: row["symbol"] for row in selections}
                summary["portfolio"] = _books(sessions, bars, decisions, (window_start, window_end))
                summary["picks"] = [
                    {"decision": row["decision"], "symbol": row["symbol"], "eligible": len(row["eligible"])}
                    for row in rows
                ]
            public[name]["horizons"][str(horizon)] = _round(summary)
    pooled = {}
    for horizon in HORIZONS:
        summary = summarize_horizon(pooled_rows[horizon], bars, sessions, horizon, draws=BOOTSTRAP_DRAWS)
        if horizon == PRIMARY_HORIZON:
            summary["same_close_fill_bias"] = same_close_bias(sessions, bars, pooled_rows[horizon], horizon)
        pooled[str(horizon)] = _round(summary)
    primary = pooled[str(PRIMARY_HORIZON)]
    window_primary = {
        name: public[name]["horizons"][str(PRIMARY_HORIZON)] for name in ("prior_365", "recent_365")
    }
    # The clear test uses the unrounded pooled and window summaries. Recompute flags from rounded
    # public numbers so the file is the record. Rounding cannot flip a result that is not on the line.
    cleared = _clears(primary, {name: {"20": block} for name, block in window_primary.items()})
    payload = {
        "hypothesis_id": rules["hypothesis_id"],
        "primary_clears": cleared,
        "promotion": False,
        "proves_an_edge": False,
        "reason_not_proof": "June 2026 survivor list, and both windows were already used.",
        "actual_picks": {
            "status": "unavailable",
            "reason": "No NYSE entry journal is in this checkout. Another sleeve's journal was not used.",
        },
        "clean_evaluation": rules["spent_holdouts"]["clean_evaluation"],
        "alpaca_free_sip": rules["alpaca_free_sip"],
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "panel_sha256": digest,
        "symbols_included": included,
        "bootstrap_draws": BOOTSTRAP_DRAWS,
        "windows": public,
        "pooled": pooled,
        "entry_quote_max_age_seconds": 420,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "primary_clears": cleared,
                "promotion": False,
                "proves_an_edge": False,
                "pooled_20": {
                    "days": primary.get("days"),
                    "excess_vs_random_mean": primary.get("excess_vs_random_mean"),
                    "p_value_vs_random": primary.get("p_value_vs_random"),
                    "excess_vs_eligible_mean": primary.get("excess_vs_eligible_mean"),
                    "excess_vs_vti_mean": primary.get("excess_vs_vti_mean"),
                    "path_hit_vs_random": primary.get("path_hit_vs_random"),
                },
            },
            indent=2,
        )
    )
    return payload


if __name__ == "__main__":
    main(sys.argv[1:])
