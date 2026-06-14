"""Cloud bot backtests — parent backtester with best-paper profile + saved results."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from cloud_bot.config.env_loader import apply_runtime_env, build_runtime_env, load_cloud_dotenv
from cloud_bot.config.profile import BEST_PAPER_ENV, apply_to_config_module, final_paper_backtest_kwargs
from cloud_bot.config.settings import REPO_ROOT, load_settings
from cloud_bot.modules.stack import STACK_FEATURES, STACK_SAFETY_GUARDS

RESULTS_DIR = Path(__file__).resolve().parents[1] / "data" / "backtest_results"


def _ensure_repo_path() -> None:
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def prepare_backtest_env(*, fast_mode: bool = False) -> Path | None:
    """Load cloud_bot/.env, apply best-paper profile, sync config module."""
    env_path = load_cloud_dotenv()
    settings = load_settings()
    runtime_env = build_runtime_env(settings)
    apply_runtime_env(runtime_env)
    apply_to_config_module()

    if fast_mode:
        from modules.backtester_core import (
            RUN_OPTIONS,
            apply_default_execution_costs,
            apply_run_options_to_config,
        )

        RUN_OPTIONS.fast_mode = True
        apply_run_options_to_config()
        apply_default_execution_costs()

    return env_path


def _results_dir() -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    return RESULTS_DIR


def _save_window_result(window: dict, *, env_path: Path | None) -> Path:
    out_dir = _results_dir()
    label = window["window"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"compare_{label}_{stamp}.json"
    md_path = out_dir / f"compare_{label}_{stamp}.md"
    latest_json = out_dir / f"compare_{label}_latest.json"
    latest_md = out_dir / f"compare_{label}_latest.md"

    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "window": label,
        "start": window.get("start"),
        "end": window.get("end"),
        "sim_bars": window.get("sim_bars"),
        "vti_benchmark_pct": window.get("vti_benchmark_pct"),
        "env_file": str(env_path) if env_path else None,
        "stack": list(STACK_FEATURES),
        "safety": list(STACK_SAFETY_GUARDS),
        "best_paper_env": dict(BEST_PAPER_ENV),
        "rows": window.get("rows", []),
        "final": window.get("final"),
        "legacy": window.get("legacy"),
    }
    text = json.dumps(payload, indent=2, default=str)
    json_path.write_text(text, encoding="utf-8")
    latest_json.write_text(text, encoding="utf-8")

    from backtester import _format_final_table

    md_lines = [
        f"# Cloud Bot Backtest — {label}",
        "",
        f"Generated: {payload['generated_utc']}",
        f"Window: {window.get('start')} -> {window.get('end')} ({window.get('sim_bars')} bars)",
        f"Env: {payload['env_file'] or '(defaults)'}",
        "",
        "## Stack",
        "",
    ]
    for feat in STACK_FEATURES:
        md_lines.append(f"- {feat}")
    md_lines.extend(["", "## Results", "", "```"])
    md_lines.append(_format_final_table(window["rows"]))
    md_lines.append("```")
    final = window.get("final") or {}
    bench = window.get("vti_benchmark_pct")
    md_lines.extend(
        [
            "",
            f"**Best Paper:** {final.get('total_return_pct', 0):+.2f}% | "
            f"Sharpe {final.get('sharpe', 0):.2f} | "
            f"MaxDD {final.get('max_drawdown_pct', 0):.2f}% | "
            f"Pairs {final.get('pairs_traded', 0)}",
        ]
    )
    if bench is not None:
        md_lines.append(
            f"**VTI:** {bench:+.2f}% | "
            f"vs VTI {(final.get('total_return_pct', 0) - bench):+.2f} pp"
        )
    md_body = "\n".join(md_lines) + "\n"
    md_path.write_text(md_body, encoding="utf-8")
    latest_md.write_text(md_body, encoding="utf-8")
    return json_path


def run_compare(
    *,
    days: int | None = 365,
    use_max: bool = False,
    refresh: bool = False,
    save: bool = True,
    fast_mode: bool = False,
) -> int:
    """Run final-style comparison for one window (best paper vs legacy vs VTI)."""
    _ensure_repo_path()
    env_path = prepare_backtest_env(fast_mode=fast_mode)

    from backtester import MIN_HISTORY, _ensure_daily_data, _format_final_table, _run_final_window

    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
        label = "max"
    else:
        d = days or 365
        data = _ensure_daily_data(d, refresh=refresh, use_max=False)
        label = f"{d}d"

    if len(data) < MIN_HISTORY:
        print(f"Need {MIN_HISTORY} bars; got {len(data)}.")
        return 1

    tag = " (fast-mode)" if fast_mode else ""
    print(f"--- CLOUD BOT BACKTEST ({label}){tag} — Best Paper v2.1 ---")
    if env_path:
        print(f"--- Env: {env_path} ---")
    window = _run_final_window(data, window_label=label)
    print(_format_final_table(window["rows"]))

    bench = window["vti_benchmark_pct"]
    final = window["final"]
    print(
        f"\nCloud profile: {final['total_return_pct']:+.2f}% | "
        f"Sharpe {final['sharpe']:.2f} | MaxDD {final['max_drawdown_pct']:.2f}% | "
        f"Pairs {final.get('pairs_traded', 0)}"
    )
    if bench is not None:
        print(f"VTI benchmark: {bench:+.2f}% ({final['total_return_pct'] - bench:+.2f} pp)")

    if save:
        path = _save_window_result(window, env_path=env_path)
        print(f"Saved: {path}")

    return 0


def run_single(
    *,
    days: int | None = 365,
    use_max: bool = False,
    refresh: bool = False,
    fast_mode: bool = False,
) -> int:
    """Run best-paper backtest only (no compare table)."""
    _ensure_repo_path()
    prepare_backtest_env(fast_mode=fast_mode)

    from backtester import MIN_HISTORY, _ensure_daily_data, run_backtest

    kwargs = final_paper_backtest_kwargs()

    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        data = _ensure_daily_data(days or 365, refresh=refresh, use_max=False)

    if len(data) < MIN_HISTORY:
        print(f"Need {MIN_HISTORY} bars; got {len(data)}.")
        return 1

    tag = " [fast-mode]" if fast_mode else ""
    print(f"--- CLOUD BOT SINGLE BACKTEST{tag} — Best Paper v2.1 ---")

    result = run_backtest(
        data, track_metrics=True, track_active_exposure=True, **kwargs
    )
    print(
        f"Return {result['total_return_pct']:+.2f}% | "
        f"Sharpe {result['sharpe']:.2f} | "
        f"MaxDD {result['max_drawdown_pct']:.2f}% | "
        f"Pairs {result.get('pairs_traded', 0)} | "
        f"Orders {result.get('total_orders', 0)}"
    )
    if result.get("execution_cost_pct") is not None:
        print(f"Execution costs: {result['execution_cost_pct']:.3f}% of initial capital")
    return 0
