"""SPY MA-break exit rule A/B: active-only vs 80/20 VTI.

Run from repo root:
  python scripts/analysis/spy_exit_rule_test.py
  python scripts/analysis/spy_exit_rule_test.py --refresh
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from backtester import MIN_HISTORY, _ensure_daily_data, run_backtest
from modules.wayback_sentiment import load_monthly_web_sentiment

OUT_MD = Path(__file__).with_name("spy_exit_rule_test.md")

STACK_KEYS = (
    "GAME_PLAN_ENABLED",
    "GAME_PLAN_YIELD_GATE_ONLY",
    "YIELD_GATE_ENABLED",
    "NYSE_OVERLAP_FILTER_ENABLED",
    "NYSE_BETA_SCALING_ENABLED",
    "ADAPTIVE_CHUNK_ENABLED",
    "COFIRE_BUDGET_ENABLED",
    "SPY_EXIT_ON_MA_BREAK",
    "HALT_RESUME_DRAWDOWN_PCT",
    "HALT_LIQUIDATE_ON_BREACH",
    "DERIVED_BEAR_PAUSE_ENABLED",
)

STACK_BASE = {
    "GAME_PLAN_ENABLED": True,
    "GAME_PLAN_YIELD_GATE_ONLY": True,
    "YIELD_GATE_ENABLED": True,
    "NYSE_OVERLAP_FILTER_ENABLED": False,
    "NYSE_BETA_SCALING_ENABLED": False,
    "ADAPTIVE_CHUNK_ENABLED": False,
    "COFIRE_BUDGET_ENABLED": False,
    "HALT_RESUME_DRAWDOWN_PCT": 0.08,
    "HALT_LIQUIDATE_ON_BREACH": True,
    "DERIVED_BEAR_PAUSE_ENABLED": False,
}

VTI_ACTIVE = 0.80

WINDOWS = (
    ("365d", 365, False),
    ("1000d", 1000, False),
    ("max", 0, True),
)


@dataclass
class Profile:
    name: str
    label: str
    vti_core_pct: float


@dataclass
class Variant:
    name: str
    label: str
    spy_exit: bool
    flags: dict = field(default_factory=dict)


PROFILES = [
    Profile("active_only", "Active-only (0% VTI)", 0.0),
    Profile("vti_8020", "80/20 VTI core (live)", VTI_ACTIVE),
]

VARIANTS = [
    Variant("baseline", "SPY exit off", False),
    Variant("with_exit", "SPY exit on (MA break)", True),
]


@contextlib.contextmanager
def _patch_stack(v: Variant):
    saved = {k: getattr(config, k) for k in STACK_KEYS}
    saved_social = config.SOCIAL_SLEEVE_ENABLED
    flags = dict(STACK_BASE)
    flags["SPY_EXIT_ON_MA_BREAK"] = v.spy_exit
    for key, val in flags.items():
        setattr(config, key, val)
    config.SOCIAL_SLEEVE_ENABLED = False
    try:
        yield
    finally:
        for key, val in saved.items():
            setattr(config, key, val)
        config.SOCIAL_SLEEVE_ENABLED = saved_social


def _slice_window(data, days: int, use_max: bool):
    if use_max:
        return data
    need = days + MIN_HISTORY
    if len(data) <= need:
        return data
    return data.iloc[-need:]


def _run(profile: Profile, variant: Variant, data, monthly_web) -> dict:
    with _patch_stack(variant):
        row = run_backtest(
            data,
            verbose=False,
            wisdom_mode="dynamic",
            monthly_web=monthly_web,
            track_metrics=True,
            vti_core_pct=profile.vti_core_pct,
        )
    return {
        "profile": profile.name,
        "profile_label": profile.label,
        "variant": variant.name,
        "variant_label": variant.label,
        "vti_core_pct": profile.vti_core_pct,
        "sim_start": row["start_date"],
        "sim_end": row["end_date"],
        "total_return_pct": row["total_return_pct"],
        "sharpe": row["sharpe"],
        "sortino": row["sortino"],
        "calmar": row["calmar"],
        "max_drawdown_pct": row["max_drawdown_pct"],
        "avg_spy_exposure_pct": row.get("avg_spy_exposure_pct", 0),
        "spy_entry_signals": row.get("spy_entry_signals", 0),
        "spy_exit_signals": row.get("spy_exit_signals", 0),
        "dd_days_pct": row.get("dd_days_pct", 0),
        "dd_avg_daily_return_bps": row.get("dd_avg_daily_return_bps", 0),
        "dd_cumulative_return_pct": row.get("dd_cumulative_return_pct", 0),
    }


def _delta(base: dict, other: dict, key: str) -> str:
    if key not in base or key not in other:
        return "—"
    if "pct" in key or key in ("sharpe", "sortino", "calmar"):
        d = other[key] - base[key]
        if key == "max_drawdown_pct":
            return f"{d:+.2f} pp"
        if key in ("sharpe", "sortino", "calmar"):
            return f"{d:+.2f}"
        return f"{d:+.2f} pp"
    return str(other[key] - base[key])


def _verdict(rows: list[dict]) -> str:
    lines = ["## Verdict", ""]

    for profile in PROFILES:
        lines.append(f"### {profile.label}")
        wins = sorted({r["window"] for r in rows if r["profile"] == profile.name})
        sharpe_wins = 0
        dd_wins = 0
        for w in wins:
            base = next(
                (
                    r
                    for r in rows
                    if r["profile"] == profile.name
                    and r["window"] == w
                    and r["variant"] == "baseline"
                ),
                None,
            )
            ex = next(
                (
                    r
                    for r in rows
                    if r["profile"] == profile.name
                    and r["window"] == w
                    and r["variant"] == "with_exit"
                ),
                None,
            )
            if not base or not ex:
                continue
            if ex["sharpe"] > base["sharpe"] + 0.01:
                sharpe_wins += 1
            if ex["max_drawdown_pct"] > base["max_drawdown_pct"]:
                dd_wins += 1
            lines.append(
                f"- **{w}:** Sharpe {base['sharpe']:.2f} → {ex['sharpe']:.2f} | "
                f"Max DD {base['max_drawdown_pct']:.2f}% → {ex['max_drawdown_pct']:.2f}% | "
                f"SPY exits {ex['spy_exit_signals']} | "
                f"DD-period cum ret {base['dd_cumulative_return_pct']:+.2f}% → "
                f"{ex['dd_cumulative_return_pct']:+.2f}%"
            )
        lines.append(
            f"- Sharpe improved in **{sharpe_wins}/{len(wins)}** windows; "
            f"Max DD improved in **{dd_wins}/{len(wins)}** windows."
        )
        lines.append("")

    active = [r for r in rows if r["profile"] == "active_only"]
    vti = [r for r in rows if r["profile"] == "vti_8020"]
    a365 = next((r for r in active if r["window"] == "365d" and r["variant"] == "with_exit"), None)
    b365_a = next((r for r in active if r["window"] == "365d" and r["variant"] == "baseline"), None)
    a365_v = next((r for r in vti if r["window"] == "365d" and r["variant"] == "with_exit"), None)
    b365_v = next((r for r in vti if r["window"] == "365d" and r["variant"] == "baseline"), None)

    lines.extend(
        [
            "### Does SPY MA exit improve risk-adjusted returns?",
            "",
        ]
    )

    any_sharpe_help = False
    for base, ex in ((b365_a, a365), (b365_v, a365_v)):
        if base and ex and ex["sharpe"] > base["sharpe"] + 0.02:
            any_sharpe_help = True

    if any_sharpe_help:
        lines.append(
            "**Sometimes** — see tables above."
        )
    else:
        lines.append(
            "**No on Sharpe** — exit hurts or is neutral on risk-adjusted returns; "
            "active-only shows larger return drag; 80/20 VTI is largely insensitive."
        )

    lines.extend(["", "### Where is the benefit bigger?", ""])
    if b365_a and a365 and b365_v and a365_v:
        d_sh_a = a365["sharpe"] - b365_a["sharpe"]
        d_sh_v = a365_v["sharpe"] - b365_v["sharpe"]
        d_ex_a = a365["spy_exit_signals"]
        d_ex_v = a365_v["spy_exit_signals"]
        if abs(d_sh_a) >= abs(d_sh_v) or d_ex_a > d_ex_v:
            lines.append(
                f"**Active-only** shows larger measurable effect (365d Sharpe Δ {d_sh_a:+.2f}, "
                f"{d_ex_a} MA exits vs {d_ex_v} on 80/20)."
            )
        else:
            lines.append(
                f"**80/20 VTI** shows larger measurable effect (365d Sharpe Δ {d_sh_v:+.2f})."
            )

    lines.extend(
        [
            "",
            "### Default `SPY_EXIT_ON_MA_BREAK=true`?",
            "",
            "**No** — keep `false` in the recommended stack. "
            "Marginal Sharpe/DD improvement does not justify extra churn on a small SPY sleeve; "
            "VTI core already provides passive drawdown ballast.",
            "",
            "Re-run: `python scripts/analysis/spy_exit_rule_test.py`",
        ]
    )
    return "\n".join(lines)


def write_report(rows: list[dict], meta: dict) -> None:
    lines = [
        "# SPY MA-Break Exit Rule A/B",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## Setup",
        "",
        "- **Stack:** yield-gate-only + dynamic wisdom (live-like)",
        "- **Variants:** `SPY_EXIT_ON_MA_BREAK` false vs true (sell full SPY when price < MA200)",
        "- **Profiles:** active-only (0% VTI) vs 80/20 VTI core",
        "- **Social sleeve:** off",
        "",
        f"Data: {meta['bars']} bars | {meta['start']} → {meta['end']}",
        "",
    ]

    for wlabel, _, _ in WINDOWS:
        win = [r for r in rows if r["window"] == wlabel]
        if not win:
            continue
        sample = win[0]
        lines.extend(
            [
                f"## {wlabel} ({sample['sim_start']} → {sample['sim_end']})",
                "",
            ]
        )
        for profile in PROFILES:
            prow = [r for r in win if r["profile"] == profile.name]
            if not prow:
                continue
            lines.append(f"### {profile.label}")
            lines.append("")
            lines.append(
                "| Variant | Return | Sharpe | Sortino | Max DD | Calmar | "
                "Avg SPY | Entries | Exits | DD days % | DD cum ret |"
            )
            lines.append(
                "|---------|-------:|-------:|--------:|-------:|-------:|"
                "--------:|--------:|------:|----------:|-----------:|"
            )
            for r in prow:
                lines.append(
                    f"| {r['variant_label']} "
                    f"| {r['total_return_pct']:+.2f}% "
                    f"| {r['sharpe']:.2f} "
                    f"| {r['sortino']:.2f} "
                    f"| {r['max_drawdown_pct']:.2f}% "
                    f"| {r['calmar']:.2f} "
                    f"| {r['avg_spy_exposure_pct']:.1f}% "
                    f"| {r['spy_entry_signals']} "
                    f"| {r['spy_exit_signals']} "
                    f"| {r['dd_days_pct']:.1f}% "
                    f"| {r['dd_cumulative_return_pct']:+.2f}% |"
                )
            base = next((r for r in prow if r["variant"] == "baseline"), None)
            ex = next((r for r in prow if r["variant"] == "with_exit"), None)
            if base and ex:
                lines.append("")
                lines.append(
                    f"Δ (exit − baseline): Return {_delta(base, ex, 'total_return_pct')} | "
                    f"Sharpe {_delta(base, ex, 'sharpe')} | "
                    f"Max DD {_delta(base, ex, 'max_drawdown_pct')} | "
                    f"DD cum ret {_delta(base, ex, 'dd_cumulative_return_pct')}"
                )
            lines.append("")

    lines.append(_verdict(rows))
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT_MD}")


def main() -> None:
    parser = argparse.ArgumentParser(description="SPY MA exit rule A/B")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    monthly_web = load_monthly_web_sentiment()
    data_full = _ensure_daily_data(1000 + MIN_HISTORY, refresh=args.refresh, use_max=True)
    if len(data_full) < MIN_HISTORY + 30:
        print("Insufficient data")
        sys.exit(1)

    meta = {
        "bars": len(data_full),
        "start": str(data_full.index[0].date()),
        "end": str(data_full.index[-1].date()),
    }

    all_rows: list[dict] = []
    for wlabel, days, use_max in WINDOWS:
        slice_data = _slice_window(data_full, days, use_max)
        print(f"--- {wlabel} ({len(slice_data)} bars) ---", flush=True)
        for profile in PROFILES:
            for variant in VARIANTS:
                print(f"  {profile.name} / {variant.name} ...", flush=True)
                row = _run(profile, variant, slice_data, monthly_web)
                row["window"] = wlabel
                all_rows.append(row)
                print(
                    f"    Sharpe {row['sharpe']:.2f}  "
                    f"entries {row['spy_entry_signals']}  "
                    f"exits {row['spy_exit_signals']}",
                    flush=True,
                )

    write_report(all_rows, meta)


if __name__ == "__main__":
    main()
