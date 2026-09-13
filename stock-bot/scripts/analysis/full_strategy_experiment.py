"""Full overnight strategy experiment — hierarchical feature ablation + MC + walk-forward.

Phases:
  1. Strong baseline (all recommended features ON)
  2. Ablation (flip one feature at a time)
  3. Key promising combinations
  4. Monte Carlo (50 runs) on top configs
  5. Walk-forward (4 folds) on top configs

Examples:
  python scripts/analysis/full_strategy_experiment.py --days 365 --paper-aggressive
  python scripts/analysis/full_strategy_experiment.py --days 365 --paper-aggressive --resume
  python scripts/analysis/full_strategy_experiment.py --phase ablation --resume
  python scripts/analysis/full_strategy_experiment.py --mc-runs 50 --top-n 6 --walk-forward 4
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from backtester import (
    MIN_HISTORY,
    _build_indicator_context,
    _ensure_daily_data,
    _trim_baseline_backtest_data,
    run_backtest,
)
from modules.backtester_core import (
    PURGE_EMBARGO_BARS,
    RUN_OPTIONS,
    apply_default_execution_costs,
    apply_run_options_to_config,
    release_backtest_memory,
    reset_caches,
    walk_forward_purged,
)
from modules.core_allocator import reset_core_allocator_state
from scripts.analysis.monte_carlo_backtest import perturb_market_data

DEFAULT_JSON = Path(__file__).with_name("full_experiment_last.json")
DEFAULT_MD = Path(__file__).with_name("full_experiment_report.md")
DEFAULT_STATE = Path(__file__).with_name("full_experiment_state.json")
DEFAULT_LOG = Path(__file__).with_name("full_experiment.log")

FEATURE_NAMES = (
    "dynamic_core",
    "deep_history_indicators",
    "regime_dynamic_sizing",
    "wisdom_thinking",
    "positioning_overlay",
    "stat_arb",
    "risk_tightened",
    "hybrid_rebalance",
)

PHASES = ("baseline", "ablation", "combos", "monte_carlo", "walk_forward")


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    dynamic_core: bool = True
    deep_history_indicators: bool = True
    regime_dynamic_sizing: bool = True
    wisdom_thinking: bool = True
    positioning_overlay: bool = True
    stat_arb: bool = True
    risk_tightened: bool = True
    hybrid_rebalance: bool = True
    tags: tuple[str, ...] = ()

    def config_id(self) -> str:
        payload = self.feature_dict()
        blob = json.dumps(payload, sort_keys=True)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]

    def feature_dict(self) -> dict[str, bool]:
        return {k: getattr(self, k) for k in FEATURE_NAMES}

    def label(self) -> str:
        on = [k for k in FEATURE_NAMES if getattr(self, k)]
        return f"{self.name} ({len(on)}/{len(FEATURE_NAMES)} on)"


def strong_baseline() -> StrategyConfig:
    """Minimal research profile — matches config.py paper-aggressive defaults."""
    return StrategyConfig(
        name="minimal_research_profile",
        dynamic_core=False,
        deep_history_indicators=True,
        regime_dynamic_sizing=True,
        wisdom_thinking=False,
        positioning_overlay=False,
        stat_arb=True,
        risk_tightened=True,
        hybrid_rebalance=False,
        tags=("baseline", "experiment_winner"),
    )


def ablation_configs(base: StrategyConfig) -> list[StrategyConfig]:
    rows: list[StrategyConfig] = []
    for feat in FEATURE_NAMES:
        flags = base.feature_dict()
        flags[feat] = not flags[feat]
        rows.append(
            StrategyConfig(
                name=f"ablate_no_{feat}",
                tags=("ablation", f"flip_{feat}"),
                **flags,
            )
        )
    return rows


def combo_configs(base: StrategyConfig) -> list[StrategyConfig]:
    """Promising combinations informed by prior deep-history / stack comparisons."""
    presets: list[tuple[str, dict[str, bool], tuple[str, ...]]] = [
        (
            "deep_history_fixed_core",
            {
                "dynamic_core": False,
                "deep_history_indicators": True,
            },
            ("combo", "deep_indicators_only"),
        ),
        (
            "all_on_no_hybrid_rebalance",
            {"hybrid_rebalance": False},
            ("combo",),
        ),
        (
            "all_on_no_thinking",
            {"wisdom_thinking": False},
            ("combo",),
        ),
        (
            "all_on_no_stat_arb",
            {"stat_arb": False},
            ("combo",),
        ),
        (
            "all_on_no_positioning",
            {"positioning_overlay": False},
            ("combo",),
        ),
        (
            "all_on_no_regime_sizing",
            {"regime_dynamic_sizing": False},
            ("combo",),
        ),
        (
            "all_on_basic_risk",
            {"risk_tightened": False},
            ("combo",),
        ),
        (
            "deep_history_no_thinking",
            {
                "dynamic_core": False,
                "deep_history_indicators": True,
                "wisdom_thinking": False,
            },
            ("combo",),
        ),
        (
            "core_stack_no_deep",
            {
                "deep_history_indicators": False,
                "dynamic_core": True,
            },
            ("combo",),
        ),
        (
            "minimal_features",
            {k: False for k in FEATURE_NAMES},
            ("combo", "minimal"),
        ),
        (
            "kitchen_sink",
            {k: True for k in FEATURE_NAMES},
            ("combo", "kitchen_sink"),
        ),
    ]
    out: list[StrategyConfig] = []
    for name, overrides, tags in presets:
        flags = base.feature_dict()
        flags.update(overrides)
        out.append(StrategyConfig(name=name, tags=tags, **flags))
    return out


@dataclass
class RunResult:
    phase: str
    config: StrategyConfig
    task_id: str
    return_pct: float
    sharpe: float
    sortino: float
    max_drawdown_pct: float
    composite_score: float
    p5_return: float | None = None
    mc_runs: int | None = None
    wf_folds: int | None = None
    wf_avg_sharpe: float | None = None
    wf_avg_return: float | None = None
    elapsed_sec: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "task_id": self.task_id,
            "config": asdict(self.config),
            "config_id": self.config.config_id(),
            "return_pct": round(self.return_pct, 4),
            "sharpe": round(self.sharpe, 4),
            "sortino": round(self.sortino, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "composite_score": round(self.composite_score, 6),
            "p5_return": round(self.p5_return, 4) if self.p5_return is not None else None,
            "mc_runs": self.mc_runs,
            "wf_folds": self.wf_folds,
            "wf_avg_sharpe": self.wf_avg_sharpe,
            "wf_avg_return": self.wf_avg_return,
            "elapsed_sec": round(self.elapsed_sec, 2),
            "extra": self.extra,
        }


def composite_score(
    sharpe: float,
    return_pct: float,
    *,
    p5_return: float | None = None,
) -> float:
    tail = p5_return if p5_return is not None else return_pct
    tail_term = 1.0 / (1.0 + abs(float(tail)))
    return float(sharpe) * 0.5 + float(return_pct) * 0.3 + tail_term * 0.2


def setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )


def log_progress(msg: str) -> None:
    logging.info(msg)


@dataclass
class _ConfigSnapshot:
    values: dict[str, Any]


def _capture_config() -> _ConfigSnapshot:
    keys = (
        "DYNAMIC_CORE_ENABLED",
        "PAPER_REGIME_DYNAMIC_SIZING_ENABLED",
        "POSITIONING_OVERLAY_ENABLED",
        "REBALANCE_ENABLED",
        "PAPER_HALT_RESUME_DRAWDOWN_PCT",
        "HALT_RESUME_DRAWDOWN_PCT",
        "HALT_LIQUIDATE_ON_BREACH",
        "DERIVED_BEAR_PAUSE_ENABLED",
        "REGIME_SENTIMENT_THRESHOLD",
        "WISDOM_LOG_FILE",
        "DEEP_HISTORY_ENABLED",
        "DEEP_HISTORY_INDICATORS_ONLY",
    )
    return _ConfigSnapshot({k: getattr(config, k) for k in keys})


def _restore_config(snap: _ConfigSnapshot) -> None:
    for k, v in snap.values.items():
        setattr(config, k, v)


@contextlib.contextmanager
def apply_strategy_config(cfg: StrategyConfig, *, wisdom_log: Path) -> Iterator[None]:
    snap = _capture_config()
    config.DYNAMIC_CORE_ENABLED = bool(cfg.dynamic_core)
    config.PAPER_REGIME_DYNAMIC_SIZING_ENABLED = bool(cfg.regime_dynamic_sizing)
    config.POSITIONING_OVERLAY_ENABLED = bool(cfg.positioning_overlay)
    config.REBALANCE_ENABLED = bool(cfg.hybrid_rebalance)
    config.WISDOM_LOG_FILE = str(wisdom_log)
    if cfg.risk_tightened:
        config.PAPER_HALT_RESUME_DRAWDOWN_PCT = 0.06
        config.HALT_RESUME_DRAWDOWN_PCT = 0.08
        config.HALT_LIQUIDATE_ON_BREACH = True
        config.DERIVED_BEAR_PAUSE_ENABLED = True
        config.REGIME_SENTIMENT_THRESHOLD = 0.10
    else:
        config.PAPER_HALT_RESUME_DRAWDOWN_PCT = 0.0
        config.HALT_RESUME_DRAWDOWN_PCT = 0.0
        config.HALT_LIQUIDATE_ON_BREACH = False
        config.DERIVED_BEAR_PAUSE_ENABLED = False
        config.REGIME_SENTIMENT_THRESHOLD = 0.15
    try:
        yield
    finally:
        _restore_config(snap)


def _target_sim_bars(days: int, available_bars: int) -> int:
    want = max(5, int(days * 0.80))
    warmup = min(MIN_HISTORY, max(0, available_bars - 5))
    return min(want, max(5, available_bars - warmup))


def _prepare_run_data(
    full_data,
    *,
    days: int,
    deep_indicators_only: bool,
    max_years: int,
    refresh: bool,
):
    if full_data is None or getattr(full_data, "empty", True):
        return full_data, None, None

    target = _target_sim_bars(days, len(full_data))
    if not deep_indicators_only:
        trade = _trim_baseline_backtest_data(full_data, target_sim_bars=target)
        return trade, None, None

    allocator_data = _trim_baseline_backtest_data(full_data, target_sim_bars=target)
    trade = (
        full_data.iloc[-target:].copy()
        if len(full_data) > target
        else full_data.copy()
    )
    indicator_context = _build_indicator_context(
        trade, max_years=max_years, refresh=refresh
    )
    if indicator_context is None or getattr(indicator_context, "empty", True):
        return allocator_data, None, None
    return trade, indicator_context, allocator_data


def _run_backtest_for_config(
    cfg: StrategyConfig,
    full_data,
    *,
    days: int,
    max_years: int,
    refresh: bool,
    bt_kwargs: dict[str, Any],
    wisdom_log: Path,
    data_override=None,
) -> dict[str, Any]:
    reset_core_allocator_state()
    source = data_override if data_override is not None else full_data
    with apply_strategy_config(cfg, wisdom_log=wisdom_log):
        trade, indicator_context, allocator_data = _prepare_run_data(
            source,
            days=days,
            deep_indicators_only=cfg.deep_history_indicators,
            max_years=max_years,
            refresh=refresh,
        )
        if trade is None or len(trade) < 20:
            return {}

        run_kwargs = {
            **bt_kwargs,
            "paper_stat_arb": cfg.stat_arb,
            "paper_thinking": cfg.wisdom_thinking,
            "wisdom_mode": "dynamic" if cfg.wisdom_thinking else None,
            "indicator_context": indicator_context,
            "allocator_data": allocator_data,
            "deep_history_indicators_only": cfg.deep_history_indicators,
            "max_years": max_years,
        }
        result = run_backtest(trade, verbose=False, track_metrics=True, **run_kwargs)
    release_backtest_memory(collect=False)
    return result


def apply_run_options_from_args(args: argparse.Namespace) -> None:
    RUN_OPTIONS.fast_mode = bool(args.fast_mode)
    RUN_OPTIONS.no_thinking = bool(args.no_thinking)
    RUN_OPTIONS.realistic_costs = not args.no_realistic_costs
    RUN_OPTIONS.full_accuracy = not RUN_OPTIONS.fast_mode
    apply_default_execution_costs()
    apply_run_options_to_config()


def build_backtest_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    if args.paper_aggressive:
        config.set_paper_aggressive_context(True)
        config.set_backtest_paper_sleeves_context(True)
        config.PAPER_CRYPTO_ENABLED = False
        config.PAPER_CRYPTO_V2_ENABLED = False
    return {
        "paper_aggressive": bool(args.paper_aggressive),
        "stat_arb_report": False,
        "paper_crypto_enabled": False if args.paper_aggressive else None,
    }


class ExperimentState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {
            "version": 1,
            "completed_tasks": [],
            "results": [],
        }
        if path.is_file():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass

    def is_done(self, task_id: str) -> bool:
        return task_id in set(self.data.get("completed_tasks", []))

    def add_result(self, task_id: str, result: RunResult) -> None:
        if task_id in self.data.get("completed_tasks", []):
            return
        self.data.setdefault("completed_tasks", []).append(task_id)
        self.data.setdefault("results", []).append(result.to_dict())
        self.data["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    def results(self) -> list[dict[str, Any]]:
        return list(self.data.get("results", []))


def _result_from_backtest(
    phase: str,
    cfg: StrategyConfig,
    task_id: str,
    result: dict[str, Any],
    *,
    elapsed_sec: float,
    p5_return: float | None = None,
    mc_runs: int | None = None,
    wf_folds: int | None = None,
    wf_avg_sharpe: float | None = None,
    wf_avg_return: float | None = None,
    extra: dict[str, Any] | None = None,
) -> RunResult:
    ret = float(result.get("total_return_pct", 0.0))
    sh = float(result.get("sharpe", 0.0))
    return RunResult(
        phase=phase,
        config=cfg,
        task_id=task_id,
        return_pct=ret,
        sharpe=sh,
        sortino=float(result.get("sortino", 0.0)),
        max_drawdown_pct=float(result.get("max_drawdown_pct", 0.0)),
        composite_score=composite_score(sh, ret, p5_return=p5_return),
        p5_return=p5_return,
        mc_runs=mc_runs,
        wf_folds=wf_folds,
        wf_avg_sharpe=wf_avg_sharpe,
        wf_avg_return=wf_avg_return,
        elapsed_sec=elapsed_sec,
        extra=extra or {},
    )


def run_full_window(
    cfg: StrategyConfig,
    *,
    phase: str,
    full_data,
    args: argparse.Namespace,
    bt_kwargs: dict[str, Any],
    state: ExperimentState,
    wisdom_log: Path,
) -> RunResult | None:
    task_id = f"{phase}:{cfg.config_id()}"
    if args.resume and state.is_done(task_id):
        log_progress(f"SKIP (done) {task_id} {cfg.name}")
        return None
    t0 = time.perf_counter()
    log_progress(f"RUN {phase} | {cfg.label()}")
    raw = _run_backtest_for_config(
        cfg,
        full_data,
        days=int(args.days),
        max_years=int(args.max_years),
        refresh=bool(args.refresh),
        bt_kwargs=bt_kwargs,
        wisdom_log=wisdom_log,
    )
    elapsed = time.perf_counter() - t0
    if not raw:
        log_progress(f"FAIL {task_id} — insufficient data")
        return None
    rr = _result_from_backtest(phase, cfg, task_id, raw, elapsed_sec=elapsed)
    state.add_result(task_id, rr)
    log_progress(
        f"DONE {task_id} | ret {rr.return_pct:+.2f}% sharpe {rr.sharpe:.2f} "
        f"score {rr.composite_score:.3f} ({elapsed:.1f}s)"
    )
    return rr


def run_monte_carlo_for_config(
    cfg: StrategyConfig,
    *,
    base_data,
    full_data,
    args: argparse.Namespace,
    bt_kwargs: dict[str, Any],
    state: ExperimentState,
    wisdom_log: Path,
    seed: int,
) -> RunResult | None:
    task_id = f"monte_carlo:{cfg.config_id()}"
    if args.resume and state.is_done(task_id):
        log_progress(f"SKIP (done) {task_id} {cfg.name}")
        return None

    mc_runs = int(args.mc_runs)
    rng = np.random.default_rng(seed + int(cfg.config_id(), 16) % 10_000)
    rets: list[float] = []
    sharpes: list[float] = []
    dds: list[float] = []
    t0 = time.perf_counter()
    log_progress(f"RUN monte_carlo x{mc_runs} | {cfg.label()}")

    for i in range(mc_runs):
        perturbed, _, _ = perturb_market_data(
            base_data,
            noise_level=float(args.noise_level),
            regime_noise=float(args.regime_noise),
            rng=rng,
        )
        raw = _run_backtest_for_config(
            cfg,
            full_data,
            days=int(args.days),
            max_years=int(args.max_years),
            refresh=False,
            bt_kwargs=bt_kwargs,
            wisdom_log=wisdom_log,
            data_override=perturbed,
        )
        if raw:
            rets.append(float(raw.get("total_return_pct", 0.0)))
            sharpes.append(float(raw.get("sharpe", 0.0)))
            dds.append(float(raw.get("max_drawdown_pct", 0.0)))
        if (i + 1) % max(1, mc_runs // 10) == 0:
            log_progress(f"  MC {cfg.name}: {i + 1}/{mc_runs}")

    elapsed = time.perf_counter() - t0
    if not rets:
        log_progress(f"FAIL {task_id} — no MC runs completed")
        return None

    ret_arr = np.asarray(rets, dtype=float)
    sh_arr = np.asarray(sharpes, dtype=float)
    p5 = float(np.percentile(ret_arr, 5))
    summary = {
        "return_mean": float(np.mean(ret_arr)),
        "return_median": float(np.median(ret_arr)),
        "return_p5": p5,
        "return_p95": float(np.percentile(ret_arr, 95)),
        "sharpe_mean": float(np.mean(sh_arr)),
        "sharpe_median": float(np.median(sh_arr)),
        "max_dd_mean": float(np.mean(dds)),
        "pct_profitable": float(np.mean(ret_arr > 0) * 100),
    }
    pseudo = {
        "total_return_pct": summary["return_median"],
        "sharpe": summary["sharpe_median"],
        "sortino": summary["sharpe_median"],
        "max_drawdown_pct": summary["max_dd_mean"],
    }
    rr = _result_from_backtest(
        "monte_carlo",
        cfg,
        task_id,
        pseudo,
        elapsed_sec=elapsed,
        p5_return=p5,
        mc_runs=mc_runs,
        extra=summary,
    )
    state.add_result(task_id, rr)
    log_progress(
        f"DONE {task_id} | med ret {summary['return_median']:+.2f}% "
        f"p5 {p5:+.2f}% med sharpe {summary['sharpe_median']:.2f} "
        f"score {rr.composite_score:.3f} ({elapsed:.1f}s)"
    )
    return rr


def run_walk_forward_for_config(
    cfg: StrategyConfig,
    *,
    base_data,
    full_data,
    args: argparse.Namespace,
    bt_kwargs: dict[str, Any],
    state: ExperimentState,
    wisdom_log: Path,
) -> RunResult | None:
    task_id = f"walk_forward:{cfg.config_id()}"
    if args.resume and state.is_done(task_id):
        log_progress(f"SKIP (done) {task_id} {cfg.name}")
        return None

    n_folds = int(args.walk_forward)
    t0 = time.perf_counter()
    log_progress(f"RUN walk_forward x{n_folds} | {cfg.label()}")

    def _wf_fn(data_slice, _test_begin: int, _test_end: int) -> dict[str, Any]:
        return _run_backtest_for_config(
            cfg,
            full_data,
            days=int(args.days),
            max_years=int(args.max_years),
            refresh=False,
            bt_kwargs=bt_kwargs,
            wisdom_log=wisdom_log,
            data_override=data_slice,
        )

    wf_rows = walk_forward_purged(
        base_data,
        min_history=MIN_HISTORY,
        n_folds=n_folds,
        run_fn=_wf_fn,
        embargo_bars=int(args.embargo_bars),
    )
    elapsed = time.perf_counter() - t0
    if not wf_rows:
        log_progress(f"FAIL {task_id} — insufficient bars for walk-forward")
        return None

    avg_ret = float(np.mean([r.get("return_pct") or 0.0 for r in wf_rows]))
    avg_sh = float(np.mean([r.get("sharpe") or 0.0 for r in wf_rows]))
    avg_dd = float(np.mean([r.get("max_dd_pct") or 0.0 for r in wf_rows]))
    pseudo = {
        "total_return_pct": avg_ret,
        "sharpe": avg_sh,
        "sortino": avg_sh,
        "max_drawdown_pct": avg_dd,
    }
    rr = _result_from_backtest(
        "walk_forward",
        cfg,
        task_id,
        pseudo,
        elapsed_sec=elapsed,
        wf_folds=n_folds,
        wf_avg_sharpe=avg_sh,
        wf_avg_return=avg_ret,
        extra={"folds": wf_rows},
    )
    state.add_result(task_id, rr)
    log_progress(
        f"DONE {task_id} | avg ret {avg_ret:+.2f}% avg sharpe {avg_sh:.2f} "
        f"score {rr.composite_score:.3f} ({elapsed:.1f}s)"
    )
    return rr


def pick_top_configs(
    results: list[dict[str, Any]],
    *,
    top_n: int,
    phases: tuple[str, ...] = ("baseline", "ablation", "combos"),
) -> list[StrategyConfig]:
    scored: dict[str, tuple[float, StrategyConfig]] = {}
    for row in results:
        if row.get("phase") not in phases:
            continue
        cfg_raw = row.get("config") or {}
        cfg = StrategyConfig(**{k: cfg_raw[k] for k in cfg_raw if k != "tags"})
        cid = cfg.config_id()
        score = float(row.get("composite_score", 0.0))
        if cid not in scored or score > scored[cid][0]:
            scored[cid] = (score, cfg)
    ranked = sorted(scored.values(), key=lambda x: x[0], reverse=True)
    return [cfg for _, cfg in ranked[:top_n]]


def compute_feature_importance(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    early = [r for r in results if r.get("phase") in ("baseline", "ablation", "combos")]
    if not early:
        return []

    baseline_rows = [
        r for r in early if r.get("config", {}).get("name") == "minimal_research_profile"
    ]
    baseline_score = (
        float(baseline_rows[0]["composite_score"]) if baseline_rows else None
    )

    importance: list[dict[str, Any]] = []
    for feat in FEATURE_NAMES:
        on_scores = []
        off_scores = []
        ablation_deltas = []
        for row in early:
            cfg = row.get("config") or {}
            score = float(row.get("composite_score", 0.0))
            if cfg.get(feat):
                on_scores.append(score)
            else:
                off_scores.append(score)
            if row.get("config", {}).get("name") == f"ablate_no_{feat}" and baseline_score is not None:
                ablation_deltas.append(score - baseline_score)

        on_mean = float(np.mean(on_scores)) if on_scores else 0.0
        off_mean = float(np.mean(off_scores)) if off_scores else 0.0
        importance.append(
            {
                "feature": feat,
                "on_mean_score": round(on_mean, 4),
                "off_mean_score": round(off_mean, 4),
                "on_minus_off": round(on_mean - off_mean, 4),
                "ablation_delta_from_baseline": round(
                    float(np.mean(ablation_deltas)) if ablation_deltas else 0.0,
                    4,
                ),
                "n_on": len(on_scores),
                "n_off": len(off_scores),
            }
        )
    importance.sort(key=lambda x: x["on_minus_off"], reverse=True)
    return importance


def render_markdown_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Full Strategy Experiment Report",
        "",
        f"Generated: {payload.get('saved_at', '')}",
        "",
        "## Run settings",
        "",
        f"- Days: {payload.get('days')}",
        f"- Paper aggressive: {payload.get('paper_aggressive')}",
        f"- Monte Carlo runs (top configs): {payload.get('mc_runs')}",
        f"- Walk-forward folds: {payload.get('walk_forward_folds')}",
        f"- Total elapsed: {payload.get('elapsed_sec')}s",
        "",
        "## Ranked configurations",
        "",
        "| Rank | Phase | Config | Return % | Sharpe | MaxDD % | Composite | p5 ret |",
        "|-----:|-------|--------|---------:|-------:|--------:|----------:|-------:|",
    ]
    for i, row in enumerate(payload.get("ranked", []), start=1):
        p5 = row.get("p5_return")
        p5_s = f"{p5:+.2f}" if p5 is not None else "—"
        lines.append(
            f"| {i} | {row.get('phase')} | {row.get('config', {}).get('name')} "
            f"| {row.get('return_pct', 0):+.2f} | {row.get('sharpe', 0):.2f} "
            f"| {row.get('max_drawdown_pct', 0):.2f} | {row.get('composite_score', 0):.3f} "
            f"| {p5_s} |"
        )

    lines.extend(["", "## Feature importance", ""])
    lines.append("| Feature | ON mean | OFF mean | ON-OFF | Ablation delta |")
    lines.append("|---------|--------:|---------:|-------:|---------------:|")
    for row in payload.get("feature_importance", []):
        lines.append(
            f"| {row['feature']} | {row['on_mean_score']:.3f} | {row['off_mean_score']:.3f} "
            f"| {row['on_minus_off']:+.3f} | {row['ablation_delta_from_baseline']:+.3f} |"
        )

    lines.extend(["", "## Notes", ""])
    lines.append(
        "- Composite = Sharpe×0.5 + Return×0.3 + (1/(1+|p5_return|))×0.2 "
        "(full-window runs use return as p5 proxy)."
    )
    lines.append(
        "- Ablation delta: score change vs baseline when that single feature is flipped OFF."
    )
    return "\n".join(lines) + "\n"


def save_outputs(
    *,
    json_path: Path,
    md_path: Path,
    meta: dict[str, Any],
    results: list[dict[str, Any]],
) -> None:
    ranked = sorted(results, key=lambda r: float(r.get("composite_score", 0.0)), reverse=True)
    feature_importance = compute_feature_importance(results)
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        **meta,
        "ranked": ranked,
        "feature_importance": feature_importance,
        "results": results,
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown_report(payload), encoding="utf-8")
    log_progress(f"Saved {json_path}")
    log_progress(f"Saved {md_path}")


def print_ranked_table(results: list[dict[str, Any]], *, limit: int = 25) -> None:
    ranked = sorted(results, key=lambda r: float(r.get("composite_score", 0.0)), reverse=True)
    print("\n=== Ranked configurations (composite score) ===")
    print(
        f"{'#':>3} {'Phase':<12} {'Config':<28} {'Return':>8} {'Sharpe':>7} "
        f"{'MaxDD':>7} {'Score':>7}"
    )
    print("-" * 82)
    for i, row in enumerate(ranked[:limit], start=1):
        name = (row.get("config") or {}).get("name", "?")
        print(
            f"{i:>3} {row.get('phase', ''):<12} {name:<28} "
            f"{row.get('return_pct', 0):+7.2f}% {row.get('sharpe', 0):6.2f} "
            f"{row.get('max_drawdown_pct', 0):6.2f}% {row.get('composite_score', 0):6.3f}"
        )


def print_feature_importance(results: list[dict[str, Any]]) -> None:
    rows = compute_feature_importance(results)
    if not rows:
        return
    print("\n=== Feature importance (ON mean - OFF mean composite) ===")
    print(f"{'Feature':<26} {'ON-OFF':>8} {'Ablation d':>10}")
    print("-" * 48)
    for row in rows:
        print(
            f"{row['feature']:<26} {row['on_minus_off']:+8.3f} "
            f"{row['ablation_delta_from_baseline']:+10.3f}"
        )


def run_experiment(args: argparse.Namespace) -> int:
    setup_logging(Path(args.log_file))
    apply_run_options_from_args(args)
    if args.refresh:
        reset_caches(disk=True)

    state = ExperimentState(Path(args.state_file))
    wisdom_log = Path(args.wisdom_log)
    wisdom_log.parent.mkdir(parents=True, exist_ok=True)

    days = int(args.days or config.BACKTEST_DAYS)
    bt_kwargs = build_backtest_kwargs(args)
    base_cfg = strong_baseline()
    phase_filter = {p.strip() for p in args.phase.split(",") if p.strip()} if args.phase != "all" else set(PHASES)

    log_progress("=== Full strategy experiment ===")
    log_progress(
        f"Days={days} paper_aggressive={args.paper_aggressive} "
        f"resume={args.resume} phases={args.phase}"
    )

    try:
        full_data = _ensure_daily_data(days, refresh=args.refresh, use_max=args.max)
    except Exception as exc:
        log_progress(f"Database error: {exc}")
        return 1
    if len(full_data) < 20:
        log_progress(f"Need at least 20 daily bars; got {len(full_data)}.")
        return 1

    target = _target_sim_bars(days, len(full_data))
    base_data = _trim_baseline_backtest_data(full_data, target_sim_bars=target)
    warmup = min(MIN_HISTORY, max(0, len(base_data) - 5))
    log_progress(
        f"Window {base_data.index[warmup].date()} -> {base_data.index[-1].date()} "
        f"({len(base_data) - warmup} sim bars)"
    )

    t0 = time.perf_counter()

    if "baseline" in phase_filter:
        run_full_window(
            base_cfg,
            phase="baseline",
            full_data=full_data,
            args=args,
            bt_kwargs=bt_kwargs,
            state=state,
            wisdom_log=wisdom_log,
        )

    if "ablation" in phase_filter:
        for cfg in ablation_configs(base_cfg):
            run_full_window(
                cfg,
                phase="ablation",
                full_data=full_data,
                args=args,
                bt_kwargs=bt_kwargs,
                state=state,
                wisdom_log=wisdom_log,
            )

    if "combos" in phase_filter:
        seen: set[str] = {base_cfg.config_id()}
        for cfg in combo_configs(base_cfg):
            if cfg.config_id() in seen:
                continue
            seen.add(cfg.config_id())
            run_full_window(
                cfg,
                phase="combos",
                full_data=full_data,
                args=args,
                bt_kwargs=bt_kwargs,
                state=state,
                wisdom_log=wisdom_log,
            )

    top_configs = pick_top_configs(state.results(), top_n=int(args.top_n))
    log_progress(f"Top {len(top_configs)} configs for MC/WF: {[c.name for c in top_configs]}")

    if "monte_carlo" in phase_filter:
        for cfg in top_configs:
            run_monte_carlo_for_config(
                cfg,
                base_data=base_data,
                full_data=full_data,
                args=args,
                bt_kwargs=bt_kwargs,
                state=state,
                wisdom_log=wisdom_log,
                seed=int(args.seed),
            )

    if "walk_forward" in phase_filter and int(args.walk_forward) >= 2:
        for cfg in top_configs:
            run_walk_forward_for_config(
                cfg,
                base_data=base_data,
                full_data=full_data,
                args=args,
                bt_kwargs=bt_kwargs,
                state=state,
                wisdom_log=wisdom_log,
            )

    elapsed = time.perf_counter() - t0
    results = state.results()
    print_ranked_table(results)
    print_feature_importance(results)

    meta = {
        "days": days,
        "paper_aggressive": bool(args.paper_aggressive),
        "fast_mode": bool(args.fast_mode),
        "mc_runs": int(args.mc_runs),
        "walk_forward_folds": int(args.walk_forward),
        "top_n": int(args.top_n),
        "window": {
            "start": str(base_data.index[warmup].date()),
            "end": str(base_data.index[-1].date()),
            "sim_bars": len(base_data) - warmup,
        },
        "elapsed_sec": round(elapsed, 1),
        "baseline_config": asdict(base_cfg),
    }
    save_outputs(
        json_path=Path(args.json_out),
        md_path=Path(args.md_out),
        meta=meta,
        results=results,
    )
    release_backtest_memory()
    log_progress(f"Experiment complete in {elapsed:.1f}s")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Overnight hierarchical strategy experiment (baseline → ablation → MC → WF)",
    )
    parser.add_argument("--days", type=int, default=config.BACKTEST_DAYS)
    parser.add_argument("--paper-aggressive", action="store_true")
    parser.add_argument("--fast-mode", action="store_true", help="Faster but less accurate sweeps")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--max", action="store_true")
    parser.add_argument("--max-years", type=int, default=20)
    parser.add_argument(
        "--phase",
        default="all",
        help="all | baseline | ablation | combos | monte_carlo | walk_forward | comma list",
    )
    parser.add_argument("--resume", action="store_true", help="Skip completed tasks in state file")
    parser.add_argument("--mc-runs", type=int, default=50, help="Monte Carlo runs per top config")
    parser.add_argument("--walk-forward", type=int, default=4, help="Walk-forward folds for top configs")
    parser.add_argument("--top-n", type=int, default=6, help="Top configs for MC/WF phases")
    parser.add_argument("--embargo-bars", type=int, default=PURGE_EMBARGO_BARS)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--noise-level", type=float, default=0.012)
    parser.add_argument("--regime-noise", type=float, default=0.10)
    parser.add_argument("--no-thinking", action="store_true")
    parser.add_argument("--no-realistic-costs", action="store_true")
    parser.add_argument("--json-out", default=str(DEFAULT_JSON))
    parser.add_argument("--md-out", default=str(DEFAULT_MD))
    parser.add_argument("--state-file", default=str(DEFAULT_STATE))
    parser.add_argument("--log-file", default=str(DEFAULT_LOG))
    parser.add_argument(
        "--wisdom-log",
        default=str(ROOT / "logs" / "full_experiment_wisdom.jsonl"),
        help="Wisdom layer log path during experiment",
    )
    return parser


def main() -> int:
    return run_experiment(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
