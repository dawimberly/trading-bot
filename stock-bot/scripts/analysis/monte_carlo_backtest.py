"""Monte Carlo robustness test: many perturbed backtests on one historical window.

Runs the same profile as backtester.py on randomized price paths (noise,
volatility scaling, regime drift) to estimate the distribution of return,
Sharpe, and max drawdown — not just a single path.

Lightweight by default: quiet runs, no HTML, indicator cache cleared between
simulations so perturbations are not masked.

Examples:
  python scripts/analysis/monte_carlo_backtest.py --paper-aggressive --days 365 --mc-runs 200
  python scripts/analysis/monte_carlo_backtest.py --paper-aggressive --days 365 --mc-runs 500 --fast-mode
  python scripts/analysis/monte_carlo_backtest.py --small-account --days 180 --mc-runs 100 --noise-level 0.015
  python scripts/analysis/monte_carlo_backtest.py --paper-aggressive --days 365 --mc-runs 200 --export-dir runs/artifacts/mc
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from backtester import MIN_HISTORY, _ensure_daily_data, run_backtest
from modules.backtester_core import (
    RUN_OPTIONS,
    apply_default_execution_costs,
    apply_run_options_to_config,
    release_backtest_memory,
    reset_caches,
)

DEFAULT_EXPORT = Path(__file__).with_name("monte_carlo_last.json")


def _mc_run_to_dict(r: McRunResult) -> dict:
    row: dict = {
        "run": r.run,
        "total_return_pct": r.total_return_pct,
        "sharpe": r.sharpe,
        "max_drawdown_pct": r.max_drawdown_pct,
        "vol_mult": round(r.vol_mult, 4),
        "regime_drift": round(r.regime_drift, 4),
        "vti_min": r.vti_min,
        "vti_max": r.vti_max,
        "vti_avg": r.vti_avg,
        "vti_at_max_dd": r.vti_at_max_dd,
        "regime_at_max_dd": r.regime_at_max_dd,
        "regime_counts": r.regime_counts,
        "max_dd_bar": r.max_dd_bar,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    if r.trough_sleeve is not None:
        row["trough_sleeve"] = r.trough_sleeve
    return row


def _ensure_export_dir(export_dir: Path, *, meta: dict) -> Path:
    """Create export folder + README once. Safe to call repeatedly."""
    export_dir.mkdir(parents=True, exist_ok=True)
    readme = export_dir / "README.md"
    if not readme.is_file():
        lines = [
            "# Monte Carlo run folder",
            "",
            "Durable per-run results so a kill mid-campaign does not lose metrics.",
            "",
            "| File | Role |",
            "|------|------|",
            "| `runs.jsonl` | One JSON object per **completed** MC run (append-only, fsync) |",
            "| `summary.json` / `summary.md` | Rolling stats over completed runs |",
            "| `results.json` | Partial after each run; final when complete |",
            "| `progress.txt` / `in_progress.json` | Live progress + current run marker |",
            "| `meta.json` | Run config (seed, window, noise) |",
            "",
            "## How to read mid-flight",
            "",
            "```text",
            "Get-Content runs.jsonl -Tail 5",
            "Get-Content summary.md",
            "```",
            "",
            "Treat results as **partial** until `summary.json` has `\"complete\": true`.",
            "",
            "## This run",
            "",
            f"- Started (UTC): {meta.get('started_at', '')}",
            f"- Profile: `{meta.get('profile', '')}`",
            f"- Days: {meta.get('days', '')} | mc-runs: {meta.get('mc_runs', '')}",
            f"- Noise: {meta.get('noise_level', '')} | regime_noise: {meta.get('regime_noise', '')} | seed: {meta.get('seed', '')}",
            f"- Window: {meta.get('window_start', '')} → {meta.get('window_end', '')}",
            "",
        ]
        readme.write_text("\n".join(lines), encoding="utf-8")
    meta_path = export_dir / "meta.json"
    if not meta_path.is_file():
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return export_dir


def _append_run_jsonl(path: Path, row: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass


def _load_completed_mc_runs(export_dir: Path) -> dict[int, McRunResult]:
    """Load unique completed runs from runs.jsonl (last write wins per run id)."""
    path = export_dir / "runs.jsonl"
    out: dict[int, McRunResult] = {}
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            rid = int(o.get("run") or 0)
        except (TypeError, ValueError):
            continue
        if rid <= 0:
            continue
        out[rid] = McRunResult(
            run=rid,
            total_return_pct=_finite_metric(o.get("total_return_pct")),
            sharpe=_finite_metric(o.get("sharpe")),
            max_drawdown_pct=_finite_metric(o.get("max_drawdown_pct")),
            vol_mult=float(o.get("vol_mult") or 0.0),
            regime_drift=float(o.get("regime_drift") or 0.0),
            vti_min=o.get("vti_min"),
            vti_max=o.get("vti_max"),
            vti_avg=o.get("vti_avg"),
            vti_at_max_dd=o.get("vti_at_max_dd"),
            regime_at_max_dd=o.get("regime_at_max_dd"),
            regime_counts=o.get("regime_counts"),
            max_dd_bar=o.get("max_dd_bar"),
            trough_sleeve=o.get("trough_sleeve"),
        )
    return out


def _rewrite_deduped_jsonl(export_dir: Path, prior: dict[int, McRunResult]) -> None:
    """Rewrite runs.jsonl with one line per run id (sorted). Keeps resume SoT clean."""
    path = export_dir / "runs.jsonl"
    tmp = export_dir / "runs.jsonl.tmp"
    lines = [
        json.dumps(_mc_run_to_dict(prior[k]), separators=(",", ":"))
        for k in sorted(prior)
    ]
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    tmp.replace(path)


def _next_run_start_index(prior: dict[int, McRunResult], mc_runs: int) -> int:
    """0-based index of first missing run in 1..mc_runs (mc_runs if complete)."""
    for run_id in range(1, mc_runs + 1):
        if run_id not in prior:
            return run_id - 1
    return mc_runs


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except SystemError:
        return False
    return True


def _acquire_export_lock(export_dir: Path) -> Path:
    """Exclusive lock so duplicate MC workers cannot share an export-dir."""
    lock_path = export_dir / "mc_worker.lock"
    my_pid = os.getpid()
    if lock_path.is_file():
        try:
            raw = json.loads(lock_path.read_text(encoding="utf-8"))
            other = int(raw.get("pid") or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            other = 0
        if other and other != my_pid and _pid_alive(other):
            raise RuntimeError(
                f"Another MC worker holds {lock_path} (pid={other}). "
                "Stop the duplicate before resuming."
            )
    payload = {
        "pid": my_pid,
        "ppid": os.getppid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    lock_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return lock_path


def _release_export_lock(lock_path: Path | None) -> None:
    if lock_path is None:
        return
    try:
        if lock_path.is_file():
            raw = json.loads(lock_path.read_text(encoding="utf-8"))
            if int(raw.get("pid") or 0) == os.getpid():
                lock_path.unlink(missing_ok=True)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass


def _run_heartbeat(
    stop: threading.Event,
    export_dir: Path,
    *,
    completed: int,
    mc_runs: int,
    current_run: int,
    t0: float,
) -> None:
    """Refresh progress.txt while a long backtest is in-flight (avoids 'stuck at N')."""
    while not stop.wait(30.0):
        try:
            _write_progress_files(
                export_dir,
                completed=completed,
                mc_runs=mc_runs,
                current_run=current_run,
                elapsed_sec=time.perf_counter() - t0,
                status="backtesting",
            )
        except OSError:
            pass


def _write_progress_files(
    export_dir: Path,
    *,
    completed: int,
    mc_runs: int,
    current_run: int | None,
    elapsed_sec: float,
    status: str,
) -> None:
    """Durable mid-flight markers (survive crash better than console-only)."""
    now = datetime.now(timezone.utc).isoformat()
    sec_per = elapsed_sec / max(1, completed) if completed else 0.0
    (export_dir / "progress.txt").write_text(
        f"runs={completed}/{mc_runs}\n"
        f"current_run={current_run if current_run is not None else ''}\n"
        f"status={status}\n"
        f"complete={'true' if completed >= mc_runs and status == 'complete' else 'false'}\n"
        f"elapsed_sec={elapsed_sec:.1f}\n"
        f"sec_per_run={sec_per:.1f}\n"
        f"updated_at={now}\n",
        encoding="utf-8",
    )
    payload = {
        "updated_at": now,
        "status": status,
        "completed_runs": completed,
        "mc_runs": mc_runs,
        "current_run": current_run,
        "elapsed_sec": round(elapsed_sec, 1),
        "sec_per_run": round(sec_per, 2),
    }
    (export_dir / "in_progress.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def _write_rolling_summary(
    export_dir: Path,
    rows: list[McRunResult],
    *,
    mc_runs: int,
    complete: bool,
    elapsed_sec: float,
) -> None:
    rets = np.array([r.total_return_pct for r in rows], dtype=float)
    sharpes = np.array([r.sharpe for r in rows], dtype=float)
    dds = np.array([r.max_drawdown_pct for r in rows], dtype=float)
    n = len(rows)
    pct_profitable = float(np.mean(rets > 0) * 100) if rets.size else 0.0
    pct_sharpe_gt1 = float(np.mean(sharpes > 1.0) * 100) if sharpes.size else 0.0
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "complete": complete,
        "completed_runs": n,
        "mc_runs": mc_runs,
        "elapsed_sec": round(elapsed_sec, 1),
        "sec_per_run": round(elapsed_sec / max(1, n), 2),
        "summary": {
            "return_pct": _percentile_table(rets, "return_pct"),
            "sharpe": _percentile_table(sharpes, "sharpe"),
            "max_drawdown_pct": _percentile_table(dds, "max_drawdown_pct"),
            "p_return_positive": round(pct_profitable, 2),
            "p_sharpe_gt_1": round(pct_sharpe_gt1, 2),
        },
    }
    (export_dir / "summary.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8"
    )
    rt = payload["summary"]["return_pct"]
    sh = payload["summary"]["sharpe"]
    dd = payload["summary"]["max_drawdown_pct"]
    md = [
        f"# Monte Carlo summary ({'COMPLETE' if complete else 'PARTIAL'})",
        "",
        f"Updated: {payload['saved_at']}",
        f"Progress: **{n}/{mc_runs}** runs | {payload['sec_per_run']}s/run",
        "",
        "| Metric | mean | median | p5 | p95 |",
        "|--------|-----:|-------:|---:|----:|",
    ]
    for label, t in (("Return %", rt), ("Sharpe", sh), ("MaxDD %", dd)):
        if not t or not t.get("n"):
            continue
        md.append(
            f"| {label} | {t['mean']:.2f} | {t['median']:.2f} | {t['p5']:.2f} | {t['p95']:.2f} |"
        )
    md += [
        "",
        f"P(return > 0): {pct_profitable:.1f}% | P(Sharpe > 1): {pct_sharpe_gt1:.1f}%",
        "",
        "Source of truth for individual runs: `runs.jsonl`.",
        "",
    ]
    (export_dir / "summary.md").write_text("\n".join(md), encoding="utf-8")
    _write_progress_files(
        export_dir,
        completed=n,
        mc_runs=mc_runs,
        current_run=None if complete else n + 1,
        elapsed_sec=elapsed_sec,
        status="complete" if complete else "between_runs",
    )


@dataclass
class McRunResult:
    run: int
    total_return_pct: float
    sharpe: float
    max_drawdown_pct: float
    vol_mult: float
    regime_drift: float
    # Optional left-tail diagnostics (populated when backtest returns series).
    vti_min: float | None = None
    vti_max: float | None = None
    vti_avg: float | None = None
    vti_at_max_dd: float | None = None
    regime_at_max_dd: str | None = None
    regime_counts: dict[str, int] | None = None
    max_dd_bar: int | None = None
    trough_sleeve: dict[str, object] | None = None


def _max_dd_index(equity: list[float]) -> int | None:
    """Index of the equity trough that realizes max drawdown (peak-to-trough)."""
    if not equity:
        return None
    peak = float(equity[0])
    peak_i = 0
    worst_dd = 0.0
    trough_i = 0
    for i, raw in enumerate(equity):
        eq = float(raw)
        if eq > peak:
            peak = eq
            peak_i = i
        if peak > 0:
            dd = eq / peak - 1.0
            if dd < worst_dd:
                worst_dd = dd
                trough_i = i
    return trough_i


def _normalize_regime_short(label: str | None) -> str:
    raw = str(label or "").strip()
    if not raw:
        return "unknown"
    return raw.split(":", 1)[0].strip() or raw


def extract_run_diagnostics(result: dict) -> dict[str, object]:
    """VTI path stats + dominant regime at max-DD trough from a backtest result."""
    vti = [float(x) for x in (result.get("vti_core_series") or []) if x is not None]
    equity = [float(x) for x in (result.get("equity_values") or []) if x is not None]
    regimes = [str(x) for x in (result.get("regime_series") or [])]
    dd_i = _max_dd_index(equity)

    vti_at_dd = None
    regime_at_dd = None
    if dd_i is not None:
        if vti:
            vti_at_dd = float(vti[min(dd_i, len(vti) - 1)])
        if regimes:
            regime_at_dd = _normalize_regime_short(regimes[min(dd_i, len(regimes) - 1)])

    # Dominant regime over a window around the trough (±5 bars).
    dominant = regime_at_dd
    if dd_i is not None and regimes:
        lo = max(0, dd_i - 5)
        hi = min(len(regimes), dd_i + 6)
        window = regimes[lo:hi]
        if window:
            counts: dict[str, int] = {}
            for lab in window:
                key = _normalize_regime_short(lab)
                counts[key] = counts.get(key, 0) + 1
            dominant = max(counts.items(), key=lambda kv: kv[1])[0]

    regime_counts_raw = result.get("regime_counts") or {}
    regime_counts = {
        _normalize_regime_short(k): int(v) for k, v in regime_counts_raw.items()
    }

    return {
        "vti_min": round(min(vti), 4) if vti else None,
        "vti_max": round(max(vti), 4) if vti else None,
        "vti_avg": round(float(np.mean(vti)), 4) if vti else None,
        "vti_at_max_dd": round(vti_at_dd, 4) if vti_at_dd is not None else None,
        "regime_at_max_dd": dominant,
        "regime_counts": regime_counts,
        "max_dd_bar": int(dd_i) if dd_i is not None else None,
    }


_SLEEVE_USD_KEYS = (
    "spy",
    "nyse_momentum",
    "stat_arb",
    "opportunistic_short",
    "crypto",
    "metals",
    "other",
)


def _peak_before_trough(equity: list[float], trough_i: int) -> int:
    peak = float(equity[0])
    peak_i = 0
    for i in range(0, trough_i + 1):
        eq = float(equity[i])
        if eq >= peak:
            peak = eq
            peak_i = i
    return peak_i


def extract_trough_sleeve_attribution(result: dict) -> dict[str, object]:
    """Marked sleeve exposures at max-DD trough (+ peak→trough deltas when available)."""
    equity = [float(x) for x in (result.get("equity_values") or []) if x is not None]
    series = list(result.get("sleeve_path_series") or [])
    dates = list(result.get("equity_index") or [])
    dd_i = _max_dd_index(equity)
    if dd_i is None or not equity:
        return {"error": "no_equity"}

    trough_i = int(dd_i)
    peak_i = _peak_before_trough(equity, trough_i)
    # Prefer sparse peak/trough snapshots (fast path) when present.
    if result.get("sleeve_path_trough") is not None:
        snap = result.get("sleeve_path_trough")
        if result.get("sleeve_path_trough_bar") is not None:
            trough_i = int(result["sleeve_path_trough_bar"])
    else:
        snap = series[min(trough_i, len(series) - 1)] if series else None

    if result.get("sleeve_path_peak") is not None:
        peak_snap = result.get("sleeve_path_peak")
        if result.get("sleeve_path_peak_bar") is not None:
            peak_i = int(result["sleeve_path_peak_bar"])
    else:
        peak_snap = series[min(peak_i, len(series) - 1)] if series else None

    peak_eq = float(equity[min(peak_i, len(equity) - 1)])
    trough_eq = float(equity[min(trough_i, len(equity) - 1)])
    trough_dd = (trough_eq / peak_eq - 1.0) if peak_eq > 0 else 0.0

    sleeve_pct = {}
    sleeve_usd = {}
    if isinstance(snap, dict):
        for key in _SLEEVE_USD_KEYS:
            sleeve_usd[key] = float(snap.get(key) or 0.0)
            sleeve_pct[key] = float(snap.get(f"{key}_pct") or 0.0)

    # Peak→trough marked-value delta (proxy for contribution; rebalancing noise included).
    sleeve_delta_usd: dict[str, float] = {}
    if isinstance(snap, dict) and isinstance(peak_snap, dict):
        for key in _SLEEVE_USD_KEYS + ("vti_notional", "cash"):
            sleeve_delta_usd[key] = round(
                float(snap.get(key) or 0.0) - float(peak_snap.get(key) or 0.0), 2
            )

    bleeding = None
    if sleeve_delta_usd:
        # Most negative active sleeve delta (exclude cash; VTI reported separately).
        active_deltas = {
            k: v for k, v in sleeve_delta_usd.items() if k in _SLEEVE_USD_KEYS
        }
        if active_deltas:
            bleeding = min(active_deltas.items(), key=lambda kv: kv[1])[0]

    # Lifetime sleeve PnL from attribution finalize (end-of-run), if present.
    att = result.get("attribution") or {}
    lifetime_pnl: dict[str, float] = {}
    sleeves = att.get("sleeves") or {}
    key_map = {
        "spy": "spy",
        "nyse_momentum": "ma50_momentum",
        "stat_arb": "stat_arb",
        "opportunistic_short": "opportunistic_short",
        "crypto": "crypto",
    }
    for out_key, att_key in key_map.items():
        s = sleeves.get(att_key) or {}
        if "total_pnl_usd" in s:
            lifetime_pnl[out_key] = float(s.get("total_pnl_usd") or 0.0)

    date = None
    if dates:
        date = str(dates[min(trough_i, len(dates) - 1)])[:10]

    peak_vti = (peak_snap or {}).get("vti_pct") if isinstance(peak_snap, dict) else None
    peak_active = (peak_snap or {}).get("active_pct") if isinstance(peak_snap, dict) else None
    trough_vti = (snap or {}).get("vti_pct") if isinstance(snap, dict) else None
    trough_active = (snap or {}).get("active_pct") if isinstance(snap, dict) else None
    shift_bits: list[str] = []
    if peak_vti is not None and trough_vti is not None:
        shift_bits.append(f"VTI {float(peak_vti):.0f}→{float(trough_vti):.0f}%")
    if peak_active is not None and trough_active is not None:
        shift_bits.append(f"active {float(peak_active):.0f}→{float(trough_active):.0f}%")
    if bleeding and sleeve_delta_usd:
        shift_bits.append(f"Δ{bleeding} ${sleeve_delta_usd.get(bleeding, 0):+,.0f}")

    # Dominant marked sleeve at trough (largest active notional %), separate from bleed delta.
    dominant_at_trough = None
    if sleeve_pct:
        dominant_at_trough = max(sleeve_pct.items(), key=lambda kv: kv[1])[0]

    return {
        "max_dd_bar": trough_i,
        "peak_bar": peak_i,
        "trough_date": date,
        "peak_equity": round(peak_eq, 2),
        "trough_equity": round(trough_eq, 2),
        "trough_dd_pct": round(100.0 * trough_dd, 2),
        "total_return_pct": float(result.get("total_return_pct") or 0.0),
        "max_drawdown_pct": float(result.get("max_drawdown_pct") or 0.0),
        "sharpe": float(result.get("sharpe") or 0.0),
        "vti_target_pct": (snap or {}).get("vti_target_pct") if snap else None,
        "vti_pct": trough_vti,
        "cash_pct": (snap or {}).get("cash_pct") if snap else None,
        "active_pct": trough_active,
        "peak_vti_pct": peak_vti,
        "peak_active_pct": peak_active,
        "peak_vs_trough_shift": "; ".join(shift_bits) if shift_bits else "—",
        "sleeve_usd": {k: round(v, 2) for k, v in sleeve_usd.items()},
        "sleeve_pct": {k: round(v, 2) for k, v in sleeve_pct.items()},
        "sleeve_delta_peak_to_trough_usd": sleeve_delta_usd,
        "main_bleeding_sleeve": bleeding,
        "dominant_sleeve_at_trough": dominant_at_trough,
        "top_holdings": (snap or {}).get("top_holdings") if snap else [],
        "lifetime_sleeve_pnl_usd": {k: round(v, 2) for k, v in lifetime_pnl.items()},
        "has_sleeve_path": bool(series)
        or result.get("sleeve_path_trough") is not None,
    }


def _trim_data_window(data: pd.DataFrame, days: int | None) -> pd.DataFrame:
    sim_target_days = days or config.BACKTEST_DAYS
    target_sim_bars = max(5, int(sim_target_days * 0.80))
    n_bars = len(data)
    warmup = min(MIN_HISTORY, max(0, n_bars - 5))
    desired_total = warmup + target_sim_bars
    if len(data) > desired_total:
        return data.iloc[-desired_total:].copy()
    return data.copy()


def perturb_market_data(
    base: pd.DataFrame,
    *,
    noise_level: float,
    regime_noise: float,
    rng: np.random.Generator,
    regime_stress: bool = False,
) -> tuple[pd.DataFrame, float, float]:
    """Return perturbed close matrix + realized vol_mult and regime_drift.

    Perturbs daily simple returns: scale by vol_mult, add idiosyncratic noise,
    and a correlated regime shock. Daily moves are clipped to avoid path explosion.

    When ``regime_stress`` is True, widen vol/drift shocks and optionally inject a
    contiguous multi-week stress block so regime labels actually move across runs.
    """
    out = base.copy()
    rn = float(regime_noise)
    if regime_stress:
        rn = max(rn, 0.25)
        vol_mult = float(1.0 + rng.uniform(-rn * 2.0, rn * 2.0))
        regime_drift = float(rng.normal(0, rn * 0.05))
    else:
        vol_mult = float(1.0 + rng.uniform(-rn, rn))
        regime_drift = float(rng.normal(0, rn * 0.02))
    n = len(out)
    if n < 2:
        return out, vol_mult, regime_drift

    max_daily_move = min(0.20, max(0.05, noise_level * 8 + rn * 0.15))
    shock_scale = rn * (0.05 if regime_stress else 0.02)
    regime_shock = rng.normal(regime_drift, shock_scale, size=n - 1)
    if regime_stress and n > 40 and float(rng.random()) < 0.55:
        # Contiguous stress window (forces regime machinery off the baseline mix).
        span = int(rng.integers(15, min(45, n - 5)))
        start = int(rng.integers(0, max(1, n - 1 - span)))
        block = rng.normal(-abs(rn) * 0.04, rn * 0.06, size=span)
        regime_shock[start : start + span] = (
            regime_shock[start : start + span] + block
        )
    values = out.to_numpy(dtype=float, copy=True)

    for j in range(values.shape[1]):
        col = values[:, j]
        p0 = col[:-1]
        p1 = col[1:]
        mask = np.isfinite(p0) & np.isfinite(p1) & (p0 > 0) & (p1 > 0)
        if not mask.any():
            continue
        simple_rets = (p1[mask] / p0[mask]) - 1.0
        id_noise = rng.normal(0.0, noise_level, size=simple_rets.shape[0])
        shock = regime_shock[mask]
        new_rets = np.clip(
            simple_rets * vol_mult + id_noise + shock,
            -max_daily_move,
            max_daily_move,
        )
        idx = np.where(mask)[0]
        for k, i in enumerate(idx):
            col[i + 1] = col[i] * (1.0 + new_rets[k])
        values[:, j] = col

    out.iloc[:, :] = values
    return out, vol_mult, regime_drift


def _percentile_table(values: np.ndarray, label: str) -> dict[str, float]:
    clean = values[np.isfinite(values)]
    if clean.size == 0:
        return {"metric": label, "n": 0}
    return {
        "metric": label,
        "n": int(clean.size),
        "mean": round(float(np.mean(clean)), 4),
        "median": round(float(np.median(clean)), 4),
        "std": round(float(np.std(clean)), 4),
        "p5": round(float(np.percentile(clean, 5)), 4),
        "p25": round(float(np.percentile(clean, 25)), 4),
        "p75": round(float(np.percentile(clean, 75)), 4),
        "p95": round(float(np.percentile(clean, 95)), 4),
        "min": round(float(np.min(clean)), 4),
        "max": round(float(np.max(clean)), 4),
    }


def _histogram(values: np.ndarray, *, bins: int = 12) -> dict:
    clean = values[np.isfinite(values)]
    if clean.size == 0:
        return {"bins": [], "counts": []}
    counts, edges = np.histogram(clean, bins=bins)
    return {
        "bins": [round(float(x), 4) for x in edges.tolist()],
        "counts": [int(x) for x in counts.tolist()],
    }


def _print_summary_table(rows: list[McRunResult]) -> None:
    rets = np.array([r.total_return_pct for r in rows], dtype=float)
    sharpes = np.array([r.sharpe for r in rows], dtype=float)
    dds = np.array([r.max_drawdown_pct for r in rows], dtype=float)
    tables = [
        _percentile_table(rets, "Return %"),
        _percentile_table(sharpes, "Sharpe"),
        _percentile_table(dds, "MaxDD %"),
    ]
    print("\n=== Monte Carlo summary ===")
    print(f"{'Metric':<12} {'mean':>8} {'median':>8} {'p5':>8} {'p25':>8} {'p75':>8} {'p95':>8}")
    print("-" * 68)
    for t in tables:
        if t.get("n", 0) == 0:
            continue
        print(
            f"{t['metric']:<12} "
            f"{t['mean']:>8.2f} "
            f"{t['median']:>8.2f} "
            f"{t['p5']:>8.2f} "
            f"{t['p25']:>8.2f} "
            f"{t['p75']:>8.2f} "
            f"{t['p95']:>8.2f}"
        )


def _print_ascii_hist(values: np.ndarray, *, title: str, bins: int = 10) -> None:
    clean = values[np.isfinite(values)]
    if clean.size == 0:
        return
    counts, edges = np.histogram(clean, bins=bins)
    if counts.max() == 0:
        return
    width = 40
    print(f"\n{title}")
    for i, c in enumerate(counts):
        bar = "#" * int(round(c / counts.max() * width))
        lo, hi = edges[i], edges[i + 1]
        print(f"  {lo:>8.2f} .. {hi:<8.2f} | {bar} ({int(c)})")


def apply_run_options_from_args(args: argparse.Namespace) -> None:
    RUN_OPTIONS.fast_mode = bool(args.fast_mode)
    RUN_OPTIONS.no_thinking = bool(args.no_thinking)
    RUN_OPTIONS.strict_pit = bool(getattr(args, "strict_pit", False))
    RUN_OPTIONS.realistic_costs = not args.no_realistic_costs
    if args.equity_slippage_bps is not None:
        RUN_OPTIONS.equity_slippage_bps = max(0.0, float(args.equity_slippage_bps))
    if args.crypto_slippage_bps is not None:
        RUN_OPTIONS.crypto_slippage_bps = max(0.0, float(args.crypto_slippage_bps))
    RUN_OPTIONS.equity_commission_bps = max(0.0, float(args.equity_commission_bps))
    RUN_OPTIONS.crypto_commission_bps = max(0.0, float(args.crypto_commission_bps))
    RUN_OPTIONS.full_accuracy = not RUN_OPTIONS.fast_mode
    apply_default_execution_costs()
    apply_run_options_to_config()
    if RUN_OPTIONS.strict_pit or bool(getattr(config, "STRICT_PIT_BACKTEST", False)):
        config.STRICT_PIT_BACKTEST = True
        try:
            config.set_backtest_strict_pit_context(True)
        except Exception:
            pass
        config.apply_strict_pit_kill_switches()
        RUN_OPTIONS.no_thinking = True
        print("STRICT PIT: overlays/thinking kill-switches applied", flush=True)


def build_backtest_kwargs(args: argparse.Namespace) -> dict:
    vti_core = max(0.0, min(1.0, args.vti_core))
    if args.small_account and vti_core <= 0:
        vti_core = config.SMALL_ACCOUNT_VTI_CORE_PCT
    if args.no_nyse_conditional and args.paper_aggressive:
        config.PAPER_NYSE_CONDITIONAL_ON_SPY = False
    kwargs = {
        "track_spy_fill": False,
        "verbose": False,
        "vti_core_pct": vti_core,
        "paper_aggressive": bool(args.paper_aggressive),
        "small_account": bool(args.small_account),
        "stat_arb_report": False,
        "track_sleeve_path": bool(getattr(args, "track_sleeve_path", False)),
        "paper_crypto_enabled": (
            True if args.paper_crypto
            else False if args.paper_aggressive
            else None
        ),
    }
    if args.start_equity is not None:
        kwargs["live_thinking_start_equity"] = float(args.start_equity)
    return kwargs


def _finite_metric(value: object, default: float = 0.0) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return out if np.isfinite(out) else default


def run_monte_carlo(args: argparse.Namespace) -> int:
    apply_run_options_from_args(args)
    # MC must not spam [deploy] prints (config default / .env often leave this True).
    config.PAPER_DEPLOY_DEBUG = False
    if args.refresh:
        reset_caches(disk=True)

    days = args.days or config.BACKTEST_DAYS
    try:
        data = _ensure_daily_data(days, refresh=args.refresh, use_max=args.max)
    except Exception as exc:
        print(f"Database error: {exc}")
        return 1
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
        return 1

    use_deep = bool(args.paper_aggressive) and bool(
        getattr(config, "DEEP_HISTORY_ENABLED", False)
    ) and not bool(args.fast_mode)
    deep_indicators_only = bool(getattr(config, "DEEP_HISTORY_INDICATORS_ONLY", True))

    if use_deep and deep_indicators_only:
        # Locked v1.5.x paper path: sim window only; indicators from deep context.
        sim_target_days = days or config.BACKTEST_DAYS
        target_sim_bars = max(5, int(sim_target_days * 0.80))
        base = data.iloc[-target_sim_bars:].copy() if len(data) > target_sim_bars else data.copy()
        warmup = 0
    else:
        base = _trim_data_window(data, days)
        warmup = min(MIN_HISTORY, max(0, len(base) - 5))
    start_date = base.index[warmup]
    end_date = base.index[-1]
    bt_kwargs = build_backtest_kwargs(args)

    indicator_context = None
    if use_deep:
        from backtester import _build_indicator_context

        indicator_context = _build_indicator_context(
            base, max_years=20, refresh=bool(args.refresh)
        )
        if indicator_context is None or getattr(indicator_context, "empty", True):
            print("WARNING: deep indicator context unavailable; MC falls back to in-window warmup.")
            indicator_context = None
            use_deep = False
            if warmup == 0:
                base = _trim_data_window(data, days)
                warmup = min(MIN_HISTORY, max(0, len(base) - 5))
                start_date = base.index[warmup]
                end_date = base.index[-1]
        else:
            bt_kwargs["indicator_context"] = indicator_context
            bt_kwargs["deep_history_indicators_only"] = True
            print(
                f"Deep indicator context locked for MC "
                f"({len(indicator_context):,} bars, reused across runs)"
            )

    profile = []
    if args.paper_aggressive:
        profile.append("paper-aggressive")
    if getattr(args, "strict_pit", False):
        profile.append("strict-pit")
    if args.small_account:
        profile.append("small-account")
    if args.fast_mode:
        profile.append("fast-mode")
    if getattr(args, "regime_stress", False):
        profile.append("regime-stress")
    if use_deep:
        profile.append("deep-indicators")
    profile_label = ", ".join(profile) if profile else "default"

    print("=== Monte Carlo backtest ===")
    print(f"Profile: {profile_label}")
    print(
        f"Window: {start_date.date()} -> {end_date.date()} "
        f"({len(base) - warmup} sim bars, {warmup} warmup)"
    )
    print(
        f"Runs: {args.mc_runs} | noise={args.noise_level:.4f} | "
        f"regime_noise={args.regime_noise:.4f} | seed={args.seed}"
    )
    if RUN_OPTIONS.fast_mode:
        print("FAST MODE: smaller universe, thinking off (use full run for final MC)")

    rng = np.random.default_rng(args.seed)  # kept for meta; each run uses SeedSequence
    rows: list[McRunResult] = []
    t0 = time.perf_counter()
    lock_path: Path | None = None

    export_dir: Path | None = None
    raw_export = getattr(args, "export_dir", None) or os.environ.get("MC_EXPORT_DIR")
    if raw_export:
        export_dir = _ensure_export_dir(
            Path(str(raw_export)),
            meta={
                "started_at": datetime.now(timezone.utc).isoformat(),
                "profile": profile_label,
                "days": days,
                "mc_runs": args.mc_runs,
                "noise_level": args.noise_level,
                "regime_noise": args.regime_noise,
                "seed": args.seed,
                "window_start": str(start_date.date()),
                "window_end": str(end_date.date()),
                "sim_bars": len(base) - warmup,
                "fast_mode": RUN_OPTIONS.fast_mode,
            },
        )
        print(f"Export dir: {export_dir} (runs.jsonl + rolling summary)")
        if not args.export_json:
            args.export_json = str(export_dir / "results.json")
        # Seed markers immediately so a crash before run 1 still leaves a trail.
        try:
            (export_dir / "runs.jsonl").touch(exist_ok=True)
        except OSError as exc:
            print(f"WARNING: export-dir seed failed: {exc}")
        try:
            lock_path = _acquire_export_lock(export_dir)
            print(f"Export lock acquired (pid={os.getpid()})", flush=True)
        except RuntimeError as exc:
            print(f"ERROR: {exc}")
            return 2

    try:
        return _run_monte_carlo_loop(
            args=args,
            base=base,
            warmup=warmup,
            start_date=start_date,
            end_date=end_date,
            bt_kwargs=bt_kwargs,
            profile_label=profile_label,
            days=days,
            export_dir=export_dir,
            t0=t0,
        )
    finally:
        _release_export_lock(lock_path)


def _run_monte_carlo_loop(
    *,
    args: argparse.Namespace,
    base: pd.DataFrame,
    warmup: int,
    start_date,
    end_date,
    bt_kwargs: dict,
    profile_label: str,
    days: int | None,
    export_dir: Path | None,
    t0: float,
) -> int:
    rows: list[McRunResult] = []

    # Resume: reload completed runs from jsonl. Per-run seeds mean no RNG replay.
    start_i = 0
    if export_dir is not None:
        prior = _load_completed_mc_runs(export_dir)
        if prior:
            try:
                _rewrite_deduped_jsonl(export_dir, prior)
            except OSError as exc:
                print(f"WARNING: jsonl dedupe rewrite failed: {exc}")
            rows = [prior[k] for k in sorted(prior)]
            start_i = _next_run_start_index(prior, args.mc_runs)
            print(
                f"Resume: loaded {len(prior)} unique completed run(s) from jsonl; "
                f"continuing at {start_i + 1}/{args.mc_runs} "
                f"(per-run seeds; no RNG replay)",
                flush=True,
            )
            try:
                _write_progress_files(
                    export_dir,
                    completed=len(rows),
                    mc_runs=args.mc_runs,
                    current_run=start_i + 1 if start_i < args.mc_runs else args.mc_runs,
                    elapsed_sec=0.0,
                    status="resuming" if start_i < args.mc_runs else "complete",
                )
                _write_rolling_summary(
                    export_dir,
                    rows,
                    mc_runs=args.mc_runs,
                    complete=start_i >= args.mc_runs,
                    elapsed_sec=0.0,
                )
            except OSError as exc:
                print(f"WARNING: resume progress write failed: {exc}")
        else:
            try:
                _write_progress_files(
                    export_dir,
                    completed=0,
                    mc_runs=args.mc_runs,
                    current_run=1,
                    elapsed_sec=0.0,
                    status="starting",
                )
            except OSError as exc:
                print(f"WARNING: export-dir seed failed: {exc}")

    if start_i >= args.mc_runs:
        print(f"Resume: already complete ({start_i}/{args.mc_runs}) — nothing to do")
    for i in range(start_i, args.mc_runs):
        if export_dir is not None:
            try:
                _write_progress_files(
                    export_dir,
                    completed=len(rows),
                    mc_runs=args.mc_runs,
                    current_run=i + 1,
                    elapsed_sec=time.perf_counter() - t0,
                    status="running",
                )
            except OSError:
                pass
            print(
                f"  MC run {i + 1}/{args.mc_runs} starting "
                f"(export={export_dir})",
                flush=True,
            )
        # Deterministic per-run seed so resume skips completed runs without replaying RNG.
        run_rng = np.random.default_rng(np.random.SeedSequence([int(args.seed), i + 1]))
        perturbed, vol_mult, regime_drift = perturb_market_data(
            base,
            noise_level=args.noise_level,
            regime_noise=args.regime_noise,
            rng=run_rng,
            regime_stress=bool(getattr(args, "regime_stress", False)),
        )
        # Keep deep indicator cache warm across runs; only drop data frames.
        release_backtest_memory(collect=False, indicators=False)
        hb_stop: threading.Event | None = None
        hb_thread: threading.Thread | None = None
        if export_dir is not None:
            hb_stop = threading.Event()
            hb_thread = threading.Thread(
                target=_run_heartbeat,
                kwargs={
                    "stop": hb_stop,
                    "export_dir": export_dir,
                    "completed": len(rows),
                    "mc_runs": args.mc_runs,
                    "current_run": i + 1,
                    "t0": t0,
                },
                name=f"mc-heartbeat-{i + 1}",
                daemon=True,
            )
            hb_thread.start()
        try:
            result = run_backtest(perturbed, **bt_kwargs)
        finally:
            if hb_stop is not None:
                hb_stop.set()
            if hb_thread is not None:
                hb_thread.join(timeout=2.0)
        diag = extract_run_diagnostics(result if isinstance(result, dict) else {})
        trough = None
        if getattr(args, "track_sleeve_path", False):
            trough = extract_trough_sleeve_attribution(
                result if isinstance(result, dict) else {}
            )
            # Drop full bar series from result memory path (already extracted).
            if isinstance(result, dict):
                result.pop("sleeve_path_series", None)
        row = McRunResult(
            run=i + 1,
            total_return_pct=_finite_metric(result.get("total_return_pct")),
            sharpe=_finite_metric(result.get("sharpe")),
            max_drawdown_pct=_finite_metric(result.get("max_drawdown_pct")),
            vol_mult=vol_mult,
            regime_drift=regime_drift,
            vti_min=diag.get("vti_min"),  # type: ignore[arg-type]
            vti_max=diag.get("vti_max"),  # type: ignore[arg-type]
            vti_avg=diag.get("vti_avg"),  # type: ignore[arg-type]
            vti_at_max_dd=diag.get("vti_at_max_dd"),  # type: ignore[arg-type]
            regime_at_max_dd=diag.get("regime_at_max_dd"),  # type: ignore[arg-type]
            regime_counts=diag.get("regime_counts"),  # type: ignore[arg-type]
            max_dd_bar=diag.get("max_dd_bar"),  # type: ignore[arg-type]
            trough_sleeve=trough,  # type: ignore[arg-type]
        )
        rows.append(row)

        # Always-on durable per-run log when --export-dir is set.
        if export_dir is not None:
            try:
                _append_run_jsonl(export_dir / "runs.jsonl", _mc_run_to_dict(row))
                _write_rolling_summary(
                    export_dir,
                    rows,
                    mc_runs=args.mc_runs,
                    complete=False,
                    elapsed_sec=time.perf_counter() - t0,
                )
            except OSError as exc:
                print(f"WARNING: export-dir write failed: {exc}")

        # Incremental export after every completed run (crash-safe partial results).
        if args.export_json:
            try:
                partial = {
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                    "profile": profile_label,
                    "days": days,
                    "window": {
                        "start": str(start_date.date()),
                        "end": str(end_date.date()),
                        "sim_bars": len(base) - warmup,
                    },
                    "mc_runs": args.mc_runs,
                    "completed_runs": len(rows),
                    "partial": True,
                    "noise_level": args.noise_level,
                    "regime_noise": args.regime_noise,
                    "seed": args.seed,
                    "fast_mode": RUN_OPTIONS.fast_mode,
                    "runs": [_mc_run_to_dict(r) for r in rows],
                }
                Path(args.export_json).write_text(
                    json.dumps(partial, indent=2), encoding="utf-8"
                )
            except OSError:
                pass
        if args.progress and (
            (i + 1) % max(1, args.mc_runs // 20) == 0 or i + 1 == args.mc_runs
        ):
            elapsed = time.perf_counter() - t0
            rate = (i + 1 - start_i) / elapsed if elapsed > 0 else 0.0
            print(f"  ... {i + 1}/{args.mc_runs} runs ({rate:.2f} runs/s)")
        status_path = os.environ.get("MC_STATUS_PATH")
        if status_path:
            try:
                Path(status_path).write_text(
                    f"runs={i + 1}/{args.mc_runs}\n"
                    f"elapsed_sec={time.perf_counter() - t0:.1f}\n"
                    f"sec_per_run={(time.perf_counter() - t0) / max(1, (i + 1 - start_i)):.1f}\n",
                    encoding="utf-8",
                )
            except OSError:
                pass

    elapsed = time.perf_counter() - t0
    _print_summary_table(rows)

    rets = np.array([r.total_return_pct for r in rows], dtype=float)
    sharpes = np.array([r.sharpe for r in rows], dtype=float)
    dds = np.array([r.max_drawdown_pct for r in rows], dtype=float)
    _print_ascii_hist(rets, title="Return % distribution")
    _print_ascii_hist(sharpes, title="Sharpe distribution")
    _print_ascii_hist(dds, title="Max drawdown % distribution")

    pct_profitable = float(np.mean(rets > 0) * 100) if rets.size else 0.0
    pct_sharpe_gt1 = float(np.mean(sharpes > 1.0) * 100) if sharpes.size else 0.0
    print(
        f"\nP(return > 0): {pct_profitable:.1f}% | "
        f"P(Sharpe > 1): {pct_sharpe_gt1:.1f}% | "
        f"elapsed {elapsed:.1f}s ({elapsed / max(1, args.mc_runs):.2f}s/run)"
    )

    if export_dir is not None:
        try:
            _write_rolling_summary(
                export_dir,
                rows,
                mc_runs=args.mc_runs,
                complete=True,
                elapsed_sec=elapsed,
            )
        except OSError as exc:
            print(f"WARNING: final export-dir summary failed: {exc}")

    if args.export_json:
        out_path = Path(args.export_json)
        payload = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "profile": profile_label,
            "days": days,
            "window": {
                "start": str(start_date.date()),
                "end": str(end_date.date()),
                "sim_bars": len(base) - warmup,
            },
            "mc_runs": args.mc_runs,
            "noise_level": args.noise_level,
            "regime_noise": args.regime_noise,
            "seed": args.seed,
            "fast_mode": RUN_OPTIONS.fast_mode,
            "summary": {
                "return_pct": _percentile_table(rets, "return_pct"),
                "sharpe": _percentile_table(sharpes, "sharpe"),
                "max_drawdown_pct": _percentile_table(dds, "max_drawdown_pct"),
                "p_return_positive": round(pct_profitable, 2),
                "p_sharpe_gt_1": round(pct_sharpe_gt1, 2),
            },
            "histograms": {
                "return_pct": _histogram(rets, bins=args.hist_bins),
                "sharpe": _histogram(sharpes, bins=args.hist_bins),
                "max_drawdown_pct": _histogram(dds, bins=args.hist_bins),
            },
            "runs": [_mc_run_to_dict(r) for r in rows],
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nExported: {out_path}")

    release_backtest_memory()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monte Carlo robustness test (perturbed backtest paths)",
    )
    parser.add_argument(
        "--mc-runs",
        type=int,
        default=500,
        metavar="N",
        help="Number of randomized simulations (default: 500)",
    )
    parser.add_argument(
        "--noise-level",
        type=float,
        default=0.01,
        metavar="SIGMA",
        help="Gaussian noise on daily log returns (default: 0.01)",
    )
    parser.add_argument(
        "--regime-noise",
        type=float,
        default=0.1,
        metavar="SCALE",
        help="Volatility scaling + correlated regime drift (default: 0.1)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="RNG seed for reproducible paths (default: 42)",
    )
    parser.add_argument(
        "--hist-bins",
        type=int,
        default=12,
        help="Histogram bin count for export (default: 12)",
    )
    parser.add_argument(
        "--export-json",
        nargs="?",
        const=str(DEFAULT_EXPORT),
        default=None,
        metavar="PATH",
        help="Write full MC results + histogram bins to JSON",
    )
    parser.add_argument(
        "--export-dir",
        default=None,
        metavar="DIR",
        help=(
            "Durable folder: README + runs.jsonl (each completed run) + rolling "
            "summary.md/json. Also sets results.json unless --export-json is given. "
            "Env MC_EXPORT_DIR is an alternate."
        ),
    )
    parser.add_argument(
        "--track-sleeve-path",
        action="store_true",
        help="Record per-bar sleeve exposures and export trough attribution per run",
    )
    parser.add_argument(
        "--no-progress",
        dest="progress",
        action="store_false",
        help="Suppress per-run progress lines",
    )
    parser.set_defaults(progress=True)

    # Mirror core backtester.py profile flags
    parser.add_argument(
        "--days",
        type=int,
        default=config.BACKTEST_DAYS,
        help=f"Simulation length in calendar days (default: {config.BACKTEST_DAYS})",
    )
    parser.add_argument("--refresh", action="store_true", help="Re-download daily history")
    parser.add_argument("--max", action="store_true", help="Use max available daily history")
    parser.add_argument(
        "--vti-core",
        type=float,
        default=0.0,
        metavar="PCT",
        help="Passive VTI core fraction (e.g. 0.7)",
    )
    parser.add_argument(
        "--paper-aggressive",
        action="store_true",
        help="Paper research profile (Best Paper Bot stack)",
    )
    parser.add_argument(
        "--strict-pit",
        action="store_true",
        help="STRICT PIT MC leg: kill non-PIT overlays/thinking (honesty vs FULL stack)",
    )
    parser.add_argument(
        "--regime-stress",
        action="store_true",
        help="Stronger regime shocks + random multi-week stress blocks (moves regime_counts)",
    )
    parser.add_argument(
        "--paper-crypto",
        action="store_true",
        help="Enable PAPER_CRYPTO_ENABLED",
    )
    parser.add_argument(
        "--small-account",
        action="store_true",
        help="Live small-account profile ($100 start, 90%% VTI)",
    )
    parser.add_argument(
        "--no-nyse-conditional",
        action="store_true",
        help="Disable NYSE conditional-on-SPY (paper aggressive)",
    )
    parser.add_argument(
        "--fast-mode",
        action="store_true",
        help="Quick MC: smaller ticker universe, no thinking engine",
    )
    parser.add_argument(
        "--no-thinking",
        action="store_true",
        help="Disable thinking-engine tilts",
    )
    parser.add_argument(
        "--start-equity",
        type=float,
        default=None,
        metavar="USD",
        help="Starting equity for small-account sim",
    )
    parser.add_argument(
        "--equity-slippage-bps",
        type=float,
        default=None,
        metavar="BPS",
        help="Equity slippage bps override",
    )
    parser.add_argument(
        "--crypto-slippage-bps",
        type=float,
        default=None,
        metavar="BPS",
        help="Crypto slippage bps override",
    )
    parser.add_argument(
        "--equity-commission-bps",
        type=float,
        default=float(__import__("os").getenv("BACKTEST_EQUITY_COMMISSION_BPS", "0")),
        help="Extra equity commission bps",
    )
    parser.add_argument(
        "--crypto-commission-bps",
        type=float,
        default=float(__import__("os").getenv("BACKTEST_CRYPTO_COMMISSION_BPS", "0")),
        help="Extra crypto commission bps",
    )
    parser.add_argument(
        "--no-realistic-costs",
        action="store_true",
        help="Disable default slippage assumptions",
    )
    return parser


def main() -> int:
    from modules.logging_utils import setup_project_logging

    setup_project_logging()
    parser = build_parser()
    args = parser.parse_args()
    if args.mc_runs < 1:
        print("--mc-runs must be >= 1")
        return 1
    if args.noise_level < 0 or args.regime_noise < 0:
        print("--noise-level and --regime-noise must be >= 0")
        return 1
    return run_monte_carlo(args)


if __name__ == "__main__":
    raise SystemExit(main())
