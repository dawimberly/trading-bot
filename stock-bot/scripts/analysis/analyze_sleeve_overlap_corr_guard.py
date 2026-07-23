"""Sleeve overlap + correlation-guard analysis for Realistic Research v1.5.4.

Parses thorough backtest logs (attribution / co-fire proxies), exit-event JSON,
and correlation-guard snapshots. Optionally runs a lightweight SPY/NYSE signal
co-fire proxy on daily bars (does not re-run the full executor backtest).

Run from stock-bot root:
  ..\\.venv\\Scripts\\python.exe scripts/analysis/analyze_sleeve_overlap_corr_guard.py
  ..\\.venv\\Scripts\\python.exe scripts/analysis/analyze_sleeve_overlap_corr_guard.py \\
      --log backtest_v154_thorough_1000.txt --signal-proxy
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DEFAULT_LOG = ROOT / "backtest_v154_thorough_1000.txt"
DEFAULT_EXIT = ROOT / "data" / "exit_events.json"
DEFAULT_CORR = ROOT / "data" / "correlation_guard.json"
DEFAULT_DB = ROOT / "data" / "strategy_metrics.db"
DEFAULT_OUT_TXT = Path(__file__).resolve().parent / "sleeve_overlap_corr_guard_v154.txt"
DEFAULT_OUT_JSON = Path(__file__).resolve().parent / "sleeve_overlap_corr_guard_v154.json"


def _f(pat: str, text: str, cast=float, default=None):
    m = re.search(pat, text, re.M)
    if not m:
        return default
    try:
        return cast(m.group(1))
    except (TypeError, ValueError):
        return default


def _line(pat: str, text: str, default: str | None = None) -> str | None:
    m = re.search(pat, text, re.M)
    if not m:
        return default
    if m.lastindex:
        return m.group(1).strip()
    return m.group(0).strip()


def parse_backtest_log(text: str) -> dict[str, Any]:
    """Extract overlap / sleeve / skip metrics from a thorough fund-report log."""
    out: dict[str, Any] = {
        "source": "backtest_log",
        "simulation": _line(r"^Simulation:\s*(.+)$", text),
        "total_return_pct": _f(r"^Total Return:\s*([+-]?[\d.]+)%", text),
        "vti_bh_pct": _f(r"^VTI Buy & Hold:\s*([+-]?[\d.]+)%", text),
        "sharpe": _f(r"^Sharpe Ratio:\s*([+-]?[\d.]+)", text),
        "max_dd_pct": _f(r"^Max Drawdown:\s*([+-]?[\d.]+)%", text),
        "spy_signals": _f(r"^SPY signals:\s*(\d+)", text, int),
        "nyse_signals": _f(r"^NYSE signals:\s*(\d+)", text, int),
        "crypto_signals": _f(r"^Crypto signals:\s*(\d+)", text, int),
        "total_orders": _f(r"^Total orders:\s*(\d+)", text, int),
        "stack_line": _line(r"stack ON:\s*(.+)$", text),
    }

    sim_m = re.search(
        r"~\s*(\d+)\s*days,\s*(\d+)\s+daily bars", text, re.I
    )
    if sim_m:
        out["sim_calendar_days"] = int(sim_m.group(1))
        out["sim_bars"] = int(sim_m.group(2))

    skip_m = re.search(
        r"Skip breakdown:\s*cycles=(\d+)\s+traded=(\d+)\s+skipped=(\d+)", text
    )
    if skip_m:
        out["skip"] = {
            "cycles": int(skip_m.group(1)),
            "traded": int(skip_m.group(2)),
            "skipped": int(skip_m.group(3)),
        }

    blockers = _line(r"TOP BLOCKERS:\s*(.+)$", text)
    tokens = _line(r"TOP TOKENS:\s*(.+)$", text)
    if blockers or tokens:
        out["skip_detail"] = {"blockers": blockers, "tokens": tokens}

    overlap_m = re.search(
        r"Overlap:\s*(\d+)\s+bars with MA50\+stat arb exposure\s*\|\s*"
        r"(\d+)\s+bars same-symbol overlap",
        text,
    )
    if overlap_m:
        bars = int(overlap_m.group(1))
        same = int(overlap_m.group(2))
        sim_bars = out.get("sim_bars") or 0
        out["stat_arb_ma50_overlap"] = {
            "overlap_bars": bars,
            "same_symbol_overlap_bars": same,
            "overlap_pct_of_sim_bars": round(100.0 * bars / sim_bars, 1)
            if sim_bars
            else None,
            "same_symbol_pct_of_sim_bars": round(100.0 * same / sim_bars, 1)
            if sim_bars
            else None,
        }

    fills_m = re.search(
        r"MA50 signals=(\d+)\s+fills=(\d+)\s*\|\s*SPY entry_fills=(\d+)", text
    )
    if fills_m:
        out["entry_fills"] = {
            "ma50_signals": int(fills_m.group(1)),
            "ma50_fills": int(fills_m.group(2)),
            "spy_entry_fills": int(fills_m.group(3)),
        }

    # Prefer funnel block universe (full scan) over summary banner (may differ).
    sa: dict[str, Any] = {}
    funnel_univ = re.search(
        r"Stat arb funnel:.*?Stat Arb universe:\s*(\d+)",
        text,
        re.S | re.I,
    )
    if funnel_univ:
        sa["universe"] = int(funnel_univ.group(1))
    else:
        v = _f(r"Stat Arb universe:\s*(\d+)", text, int)
        if v is not None:
            sa["universe"] = v
    contrib = _f(r"Stat arb contribution:\s*([+-]?[\d.]+)%", text, float)
    if contrib is not None:
        sa["contribution_pct"] = contrib
    funnel = re.search(
        r"scan_signals=(\d+)\s+intents=(\d+)\s+pairs_opened=(\d+)\s+"
        r"pairs_closed=(\d+)\s+leg_fills=(\d+)\s+fill_rate=([\d.]+)%",
        text,
    )
    if funnel:
        sa.update(
            {
                "scan_signals": int(funnel.group(1)),
                "intents": int(funnel.group(2)),
                "pairs_opened": int(funnel.group(3)),
                "pairs_closed": int(funnel.group(4)),
                "leg_fills": int(funnel.group(5)),
                "fill_rate_pct": float(funnel.group(6)),
            }
        )
    rejects = _line(r"Stat arb rejects:\s*(.+)$", text)
    if rejects:
        sa["rejects"] = rejects
    exits = _line(r"Stat arb exits by reason:\s*(.+)$", text)
    if exits:
        sa["exits_by_reason"] = exits
    pairs = _line(r"Stat arb per-pair \(top\):\s*(.+)$", text)
    if pairs:
        sa["per_pair_top"] = pairs
    if sa:
        out["stat_arb"] = sa

    # Sleeve PnL table rows (first attribution block)
    sleeve_rows = {}
    for label, key in (
        ("SPY MA200", "spy"),
        ("NYSE MA50 momentum", "ma50_momentum"),
        ("Stat arb pairs \\(NYSE\\)", "stat_arb"),
        ("Crypto sleeve", "crypto"),
        ("Opportunistic shorts", "opportunistic_short"),
    ):
        m = re.search(
            rf"^{label}\s+"
            rf"([+-]?[\d.]+)\s+([+-]?[\d.]+)\s+([+-]?[\d.]+)\s+"
            rf"(\d+)\s+([\d.]+)%\s+([+-]?[\d.]+)%\s+([\d.]+)",
            text,
            re.M,
        )
        if m:
            sleeve_rows[key] = {
                "pnl_usd": float(m.group(1)),
                "realized_usd": float(m.group(2)),
                "unrealized_usd": float(m.group(3)),
                "trips": int(m.group(4)),
                "win_pct": float(m.group(5)),
                "avg_ret_pct": float(m.group(6)),
                "profit_factor": float(m.group(7)),
            }
    if sleeve_rows:
        out["sleeves"] = sleeve_rows

    short = _line(r"^Protective Shorts:[^\n]*", text) or _line(
        r"Protective Shorts:[^\n]*", text
    )
    if short:
        out["shorts_banner"] = short[:240]

    # Co-fire: fund report does not print cofire_pct; derive bounds from signals.
    spy_n = out.get("spy_signals")
    nyse_n = out.get("nyse_signals")
    bars = out.get("sim_bars")
    traded = (out.get("skip") or {}).get("traded")
    if spy_n is not None and nyse_n is not None and bars:
        # Upper bound if every overlapping signal day co-fired (impossible to
        # recover exact spy_nyse_cofire_days from the printed report).
        out["spy_nyse_cofire_estimate"] = {
            "note": (
                "Fund report does not print cofire_pct / spy_nyse_cofire_days "
                "(computed in backtester.py but omitted from print). "
                "Bounds below use signal counts only."
            ),
            "spy_signal_rate_pct": round(100.0 * spy_n / bars, 1),
            "nyse_signal_rate_pct": round(100.0 * nyse_n / bars, 1),
            "naive_independent_joint_pct": round(
                100.0 * (spy_n / bars) * (nyse_n / bars), 2
            ),
            "max_possible_cofire_days": min(spy_n, nyse_n),
            "max_possible_cofire_pct_of_bars": round(
                100.0 * min(spy_n, nyse_n) / bars, 1
            ),
            "traded_cycles": traded,
            "traded_cycle_pct_of_skip_cycles": (
                round(100.0 * traded / out["skip"]["cycles"], 1)
                if traded is not None and out.get("skip", {}).get("cycles")
                else None
            ),
        }

    # Corr-guard log hits inside the backtest transcript
    corr_hits = len(
        re.findall(r"Correlation guard\s+(active|blocked)", text, re.I)
    )
    corr_any = len(re.findall(r"Correlation guard", text, re.I))
    out["corr_guard_in_log"] = {
        "active_or_blocked_lines": corr_hits,
        "any_mention_lines": corr_any,
    }

    regimes = {}
    for m in re.finditer(r"^\s+(RHYME_[A-E]:[^:]+):\s*(\d+)\s*$", text, re.M):
        regimes[m.group(1).strip()] = int(m.group(2))
    if regimes:
        out["regime_counts"] = regimes

    return out


def analyze_exit_events(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"available": False, "path": str(path)}
    payload = json.loads(path.read_text(encoding="utf-8"))
    events = list(payload.get("events") or [])
    by_sleeve = Counter((e.get("sleeve") or "(none)") for e in events)
    by_reason = Counter((e.get("reason") or "?") for e in events)
    by_combo = Counter(
        f"{e.get('sleeve') or '(none)'}|{e.get('reason') or '?'}" for e in events
    )
    symbols = Counter((e.get("symbol") or "(empty)") for e in events)
    ts_list = [e.get("ts") for e in events if e.get("ts")]
    return {
        "available": True,
        "path": str(path),
        "updated_at": payload.get("updated_at"),
        "n_events": len(events),
        "note": (
            "exit_events.json retains last 400 wall-clock events; timestamps are "
            "run-time (not simulation bar dates). Thorough 1000d run overwrote "
            "this file during attribution exits."
        ),
        "ts_first": ts_list[0] if ts_list else None,
        "ts_last": ts_list[-1] if ts_list else None,
        "by_sleeve": dict(by_sleeve),
        "by_reason": dict(by_reason),
        "by_sleeve_reason": dict(by_combo.most_common(20)),
        "top_symbols": dict(symbols.most_common(12)),
    }


def analyze_corr_guard(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"available": False, "path": str(path)}
    payload = json.loads(path.read_text(encoding="utf-8"))
    last_corr = payload.get("last_corr")
    mult = payload.get("last_multiplier")
    try:
        import config
        from modules.risk_management import get_correlation_guard_multiplier

        max_allowed = float(getattr(config, "MAX_PORTFOLIO_CORR", 0.65))
        enabled = bool(config.effective_correlation_guard_enabled())
        derived_mult = (
            get_correlation_guard_multiplier(float(last_corr), max_allowed)
            if last_corr is not None
            else None
        )
    except Exception as exc:
        max_allowed = 0.65
        enabled = None
        derived_mult = None
        config_err = str(exc)
    else:
        config_err = None

    status = "n/a"
    if last_corr is None:
        status = "no_portfolio_corr_snapshot"
    elif float(mult or 1.0) < 0.999:
        status = "reducing_size"
    else:
        status = "ok_full_size"

    return {
        "available": True,
        "path": str(path),
        "enabled": enabled,
        "max_portfolio_corr": max_allowed,
        "last_corr": last_corr,
        "last_multiplier": mult,
        "derived_multiplier": derived_mult,
        "status": status,
        "updated_at": payload.get("updated_at"),
        "config_error": config_err,
        "gap": (
            None
            if last_corr is not None
            else (
                "Snapshot has last_corr=null — backtest rarely persists a live "
                "portfolio corr, and the thorough log has no 'Correlation guard "
                "active/blocked' lines. Hit/reject counts are unavailable."
            )
        ),
    }


def analyze_strategy_db(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"available": False, "path": str(path)}
    try:
        con = sqlite3.connect(str(path))
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        tables = {
            r[0]
            for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        out: dict[str, Any] = {"available": True, "path": str(path), "tables": sorted(tables)}
        if "closed_trades" in tables:
            rows = cur.execute(
                "SELECT strategy_id, COUNT(*) AS n, ROUND(SUM(pnl), 2) AS pnl "
                "FROM closed_trades GROUP BY strategy_id ORDER BY n DESC"
            ).fetchall()
            out["closed_trades_by_strategy"] = [
                {"strategy_id": r["strategy_id"], "n": r["n"], "pnl": r["pnl"]}
                for r in rows
            ]
            out["closed_trades_total"] = sum(r["n"] for r in rows)
        con.close()
        return out
    except Exception as exc:
        return {"available": False, "path": str(path), "error": str(exc)}


def signal_proxy_spy_nyse(*, days: int | None = 1000) -> dict[str, Any]:
    """Fast vectorized SPY/NYSE co-fire proxy (not full executor path).

    SPY fire ≈ close > MA(SPY_MA_WINDOW); NYSE fire ≈ any capped-universe name
    above its MA50. Regime pause is approximated as SPY below MA (bearish proxy)
    so we avoid per-bar sentiment (too slow on ~1000d).
    """
    try:
        import numpy as np
        import pandas as pd

        import config
        from modules.data_loader import load_close_matrix
    except Exception as exc:
        return {"available": False, "error": str(exc)}

    min_hist = max(50, int(getattr(config, "SPY_MA_WINDOW", 200)))
    ma_spy = int(getattr(config, "SPY_MA_WINDOW", 200))
    data = load_close_matrix(interval="1d", days=days)
    if len(data) < min_hist + 10:
        return {
            "available": False,
            "error": f"insufficient daily bars ({len(data)})",
        }

    spy_sym = config.SPY_BOT_SYMBOL
    if spy_sym not in data.columns:
        return {"available": False, "error": f"{spy_sym} missing from close matrix"}

    try:
        cfg_univ = list(config.nyse_momentum_universe(data.columns) or [])
    except Exception:
        cfg_univ = []
    if not cfg_univ:
        cfg_univ = [
            c
            for c in data.columns
            if not config.is_crypto(c)
            and c != spy_sym
            and not config.is_metal_symbol(c)
        ]
    equity_univ = [c for c in cfg_univ if c in data.columns][:40]
    if not equity_univ:
        return {"available": False, "error": "no equity columns for NYSE proxy"}

    spy = data[spy_sym].astype(float)
    spy_ma = spy.rolling(ma_spy, min_periods=ma_spy).mean()
    spy_bull = spy > spy_ma

    above_ma50 = pd.DataFrame(
        {
            c: data[c].astype(float) > data[c].astype(float).rolling(50, min_periods=50).mean()
            for c in equity_univ
        }
    )
    nyse_any = above_ma50.any(axis=1)

    # Align to post-warmup bars (same window thorough log uses conceptually)
    mask = np.arange(len(data)) >= min_hist
    spy_f_s = spy_bull & mask
    nyse_f_s = nyse_any & mask & spy_bull  # NYSE typically gated by non-paused/bull regimes
    # Also report ungated NYSE-above-MA50 for context
    nyse_raw = nyse_any & mask
    both = spy_f_s & nyse_raw
    n = int(mask.sum())
    if n <= 0:
        return {"available": False, "error": "no bars after warmup"}

    spy_f = int(spy_f_s.sum())
    nyse_f = int(nyse_raw.sum())
    both_f = int(both.sum())
    # Co-want ≈ both above thresholds on same day
    both_w = both_f

    return {
        "available": True,
        "note": (
            "Vectorized proxy: SPY>MA200 fire; NYSE=any of "
            f"{len(equity_univ)} names >MA50. No per-bar RHYME pause "
            "(avoids slow sentiment loop). Not executor cofire_budget path."
        ),
        "equity_universe_n": len(equity_univ),
        "window": {
            "start": str(data.index[min_hist].date()),
            "end": str(data.index[-1].date()),
            "bars": n,
            "days_arg": days,
        },
        "spy_fire_days": spy_f,
        "nyse_fire_days": nyse_f,
        "spy_nyse_cofire_days": both_f,
        "spy_nyse_cowant_days": both_w,
        "pct_bars_spy_fire": round(100.0 * spy_f / n, 1),
        "pct_bars_nyse_fire": round(100.0 * nyse_f / n, 1),
        "pct_bars_spy_nyse_cofire": round(100.0 * both_f / n, 1),
        "pct_bars_spy_nyse_cowant": round(100.0 * both_w / n, 1),
        "pct_spy_fires_that_cofire_nyse": round(100.0 * both_f / spy_f, 1)
        if spy_f
        else None,
        "pct_nyse_fires_that_cofire_spy": round(100.0 * both_f / nyse_f, 1)
        if nyse_f
        else None,
        "pct_bars_either_fire": round(
            100.0 * int((spy_f_s | nyse_raw).sum()) / n, 1
        ),
        "nyse_gated_by_spy_bull_cofire_days": int((spy_f_s & nyse_raw).sum()),
    }


def build_report(
    *,
    log_path: Path,
    exit_path: Path,
    corr_path: Path,
    db_path: Path,
    signal_proxy: bool,
    proxy_days: int | None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "profile": "Realistic Research v1.5.4",
        "inputs": {
            "log": str(log_path) if log_path else None,
            "exit_events": str(exit_path),
            "correlation_guard": str(corr_path),
            "strategy_metrics_db": str(db_path),
        },
    }

    if log_path and log_path.exists():
        text = log_path.read_text(encoding="utf-8", errors="replace")
        report["backtest"] = parse_backtest_log(text)
        report["backtest"]["log_bytes"] = log_path.stat().st_size
        report["backtest"]["log_mtime"] = datetime.fromtimestamp(
            log_path.stat().st_mtime
        ).isoformat(timespec="seconds")
    else:
        report["backtest"] = {
            "available": False,
            "path": str(log_path) if log_path else None,
        }

    report["exit_events"] = analyze_exit_events(exit_path)
    report["correlation_guard"] = analyze_corr_guard(corr_path)
    report["strategy_metrics"] = analyze_strategy_db(db_path)

    if signal_proxy:
        report["signal_proxy_spy_nyse"] = signal_proxy_spy_nyse(days=proxy_days)
    else:
        report["signal_proxy_spy_nyse"] = {"available": False, "skipped": True}

    gaps = []
    bt = report.get("backtest") or {}
    if not bt.get("stat_arb_ma50_overlap"):
        gaps.append("MA50+stat-arb overlap line missing from log")
    if bt.get("corr_guard_in_log", {}).get("active_or_blocked_lines", 0) == 0:
        gaps.append(
            "No correlation-guard active/blocked lines in thorough log "
            "(hit/reject counts unavailable)"
        )
    if (report.get("correlation_guard") or {}).get("last_corr") is None:
        gaps.append("correlation_guard.json last_corr is null")
    if bt.get("spy_nyse_cofire_estimate"):
        gaps.append(
            "Exact spy_nyse_cofire_pct not printed in fund report "
            "(use signal_proxy or re-run backtester with print)"
        )
    report["gaps"] = gaps
    return report


def format_summary(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("SLEEVE OVERLAP + CORRELATION GUARD - v1.5.4")
    lines.append(f"Generated: {report.get('generated_at')}")
    lines.append("=" * 72)

    bt = report.get("backtest") or {}
    if bt.get("simulation"):
        lines.append("")
        lines.append("--- Thorough backtest (parsed log) ---")
        lines.append(f"  Simulation:     {bt.get('simulation')}")
        lines.append(
            f"  Return / Sharpe / MaxDD: "
            f"{bt.get('total_return_pct')}% / {bt.get('sharpe')} / {bt.get('max_dd_pct')}%"
        )
        lines.append(
            f"  Signals:        SPY={bt.get('spy_signals')}  "
            f"NYSE={bt.get('nyse_signals')}  Crypto={bt.get('crypto_signals')}  "
            f"orders={bt.get('total_orders')}"
        )
        if bt.get("stack_line"):
            lines.append(f"  Stack:          {bt['stack_line'][:100]}")

        ov = bt.get("stat_arb_ma50_overlap") or {}
        if ov:
            lines.append("")
            lines.append("  Stat arb x NYSE (MA50) exposure overlap:")
            lines.append(
                f"    Overlap bars:          {ov.get('overlap_bars')} "
                f"({ov.get('overlap_pct_of_sim_bars')}% of {bt.get('sim_bars')} bars)"
            )
            lines.append(
                f"    Same-symbol overlap:   {ov.get('same_symbol_overlap_bars')} "
                f"({ov.get('same_symbol_pct_of_sim_bars')}%)"
            )

        fills = bt.get("entry_fills") or {}
        if fills:
            lines.append(
                f"    MA50 fills / SPY fills: {fills.get('ma50_fills')} / "
                f"{fills.get('spy_entry_fills')}"
            )

        sa = bt.get("stat_arb") or {}
        if sa:
            lines.append("")
            lines.append("  Stat arb funnel:")
            lines.append(
                f"    universe={sa.get('universe')}  "
                f"signals={sa.get('scan_signals')}  intents={sa.get('intents')}  "
                f"opened={sa.get('pairs_opened')}  fill={sa.get('fill_rate_pct')}%"
            )
            if sa.get("contribution_pct") is not None:
                lines.append(
                    f"    contribution of NYSE+stat PnL: {sa.get('contribution_pct')}%"
                )
            if sa.get("rejects"):
                lines.append(f"    rejects: {sa.get('rejects')}")

        est = bt.get("spy_nyse_cofire_estimate") or {}
        if est:
            lines.append("")
            lines.append("  SPY x NYSE co-fire (log-derived bounds):")
            lines.append(
                f"    Signal rates: SPY {est.get('spy_signal_rate_pct')}% / "
                f"NYSE {est.get('nyse_signal_rate_pct')}% of bars"
            )
            lines.append(
                f"    Naive independent joint: {est.get('naive_independent_joint_pct')}%"
            )
            lines.append(
                f"    Max possible cofire: {est.get('max_possible_cofire_days')} days "
                f"({est.get('max_possible_cofire_pct_of_bars')}% of bars)"
            )
            if est.get("traded_cycles") is not None:
                lines.append(
                    f"    Traded cycles: {est.get('traded_cycles')} "
                    f"({est.get('traded_cycle_pct_of_skip_cycles')}% of skip cycles)"
                )
            lines.append(f"    Note: {est.get('note')}")

        cg_log = bt.get("corr_guard_in_log") or {}
        lines.append("")
        lines.append(
            f"  Corr-guard mentions in log: "
            f"active/blocked={cg_log.get('active_or_blocked_lines', 0)}  "
            f"any={cg_log.get('any_mention_lines', 0)}"
        )

        sleeves = bt.get("sleeves") or {}
        if sleeves:
            lines.append("")
            lines.append("  Sleeve PnL (attribution):")
            for k, row in sleeves.items():
                lines.append(
                    f"    {k:<22} PnL ${row.get('pnl_usd'):>9.2f}  "
                    f"trips={row.get('trips')}  win={row.get('win_pct')}%"
                )

        skip = bt.get("skip") or {}
        if skip:
            lines.append(
                f"  Skip: cycles={skip.get('cycles')} traded={skip.get('traded')} "
                f"skipped={skip.get('skipped')}"
            )
            detail = bt.get("skip_detail") or {}
            if detail.get("blockers"):
                lines.append(f"    Blockers: {detail['blockers']}")
            if detail.get("tokens"):
                lines.append(f"    Tokens:   {detail['tokens']}")

    ex = report.get("exit_events") or {}
    lines.append("")
    lines.append("--- Exit events JSON ---")
    if ex.get("available"):
        lines.append(
            f"  n={ex.get('n_events')}  updated={ex.get('updated_at')}  "
            f"range={ex.get('ts_first')} -> {ex.get('ts_last')}"
        )
        lines.append(f"  by sleeve: {ex.get('by_sleeve')}")
        lines.append(f"  by reason: {ex.get('by_reason')}")
        lines.append(f"  Note: {ex.get('note')}")
    else:
        lines.append("  (unavailable)")

    cg = report.get("correlation_guard") or {}
    lines.append("")
    lines.append("--- Correlation guard snapshot ---")
    if cg.get("available"):
        lines.append(
            f"  enabled={cg.get('enabled')}  max_corr={cg.get('max_portfolio_corr')}  "
            f"status={cg.get('status')}"
        )
        lines.append(
            f"  last_corr={cg.get('last_corr')}  mult={cg.get('last_multiplier')}  "
            f"updated={cg.get('updated_at')}"
        )
        if cg.get("gap"):
            lines.append(f"  Gap: {cg['gap']}")
    else:
        lines.append("  (unavailable)")

    proxy = report.get("signal_proxy_spy_nyse") or {}
    lines.append("")
    lines.append("--- SPY/NYSE signal co-fire proxy (loose intent, not entry fills) ---")
    if proxy.get("available"):
        w = proxy.get("window") or {}
        lines.append(
            f"  Window: {w.get('start')} -> {w.get('end')} ({w.get('bars')} bars)"
        )
        lines.append(
            f"  Co-fire days: {proxy.get('spy_nyse_cofire_days')} "
            f"({proxy.get('pct_bars_spy_nyse_cofire')}% of bars)"
        )
        lines.append(
            f"  Of SPY>MA days with any NYSE>MA50: "
            f"{proxy.get('pct_spy_fires_that_cofire_nyse')}%  |  "
            f"Of NYSE>MA50 days with SPY>MA: "
            f"{proxy.get('pct_nyse_fires_that_cofire_spy')}%"
        )
        lines.append(
            f"  Contrast vs thorough fills: SPY fills={((report.get('backtest') or {}).get('entry_fills') or {}).get('spy_entry_fills')} "
            f"MA50 fills={((report.get('backtest') or {}).get('entry_fills') or {}).get('ma50_fills')} "
            f"(proxy is much looser than cooldown/room-gated entries)"
        )
        lines.append(f"  Note: {proxy.get('note')}")
    elif proxy.get("skipped"):
        lines.append("  (not run; pass --signal-proxy to enable)")
    else:
        lines.append(f"  (unavailable: {proxy.get('error')})")

    db = report.get("strategy_metrics") or {}
    if db.get("available") and db.get("closed_trades_by_strategy"):
        lines.append("")
        lines.append("--- Strategy metrics DB (closed_trades) ---")
        lines.append(f"  total rows: {db.get('closed_trades_total')}")
        for row in (db.get("closed_trades_by_strategy") or [])[:10]:
            lines.append(
                f"    {row['strategy_id']:<22} n={row['n']:<5} pnl=${row['pnl']}"
            )

    gaps = report.get("gaps") or []
    if gaps:
        lines.append("")
        lines.append("--- Gaps ---")
        for g in gaps:
            lines.append(f"  - {g}")

    lines.append("")
    lines.append("=" * 72)
    return "\n".join(lines)


def _resolve_log(explicit: Path | None) -> Path:
    if explicit:
        return explicit if explicit.is_absolute() else ROOT / explicit
    candidates = sorted(
        ROOT.glob("backtest_v154_thorough*.txt"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    # Prefer the main thorough log (not .err / .watcher)
    for p in candidates:
        if p.name.endswith(".err.txt") or "watcher" in p.name or "status" in p.name:
            continue
        if "thorough_1000" in p.name and p.suffix == ".txt":
            return p
    if DEFAULT_LOG.exists():
        return DEFAULT_LOG
    # Fallback: newest backtest_*.txt with SLEEVE ATTRIBUTION
    for p in sorted(ROOT.glob("backtest*.txt"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            if "SLEEVE ATTRIBUTION" in p.read_text(encoding="utf-8", errors="replace")[:200000]:
                # check end of file too
                pass
            text = p.read_text(encoding="utf-8", errors="replace")
            if "SLEEVE ATTRIBUTION" in text and "Total Return:" in text:
                return p
        except OSError:
            continue
    return DEFAULT_LOG


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sleeve overlap + correlation guard analysis (v1.5.4)"
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=None,
        help="Backtest log path (default: latest backtest_v154_thorough_1000.txt)",
    )
    parser.add_argument(
        "--exit-events",
        type=Path,
        default=DEFAULT_EXIT,
        help="exit_events.json path",
    )
    parser.add_argument(
        "--corr-guard",
        type=Path,
        default=DEFAULT_CORR,
        help="correlation_guard.json path",
    )
    parser.add_argument(
        "--strategy-db",
        type=Path,
        default=DEFAULT_DB,
        help="strategy_metrics.db path",
    )
    parser.add_argument(
        "--signal-proxy",
        action="store_true",
        default=True,
        help="Run vectorized SPY/NYSE co-fire proxy (default on)",
    )
    parser.add_argument(
        "--no-signal-proxy",
        action="store_true",
        help="Skip SPY/NYSE signal co-fire proxy",
    )
    parser.add_argument(
        "--proxy-days",
        type=int,
        default=1000,
        help="Days of daily history for signal proxy (default 1000)",
    )
    parser.add_argument(
        "--out-txt",
        type=Path,
        default=DEFAULT_OUT_TXT,
        help="Write text summary here",
    )
    parser.add_argument(
        "--out-json",
        type=Path,
        default=DEFAULT_OUT_JSON,
        help="Write JSON report here",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Print JSON to stdout instead of text summary",
    )
    args = parser.parse_args()

    log_path = _resolve_log(args.log)
    exit_path = args.exit_events if args.exit_events.is_absolute() else ROOT / args.exit_events
    corr_path = args.corr_guard if args.corr_guard.is_absolute() else ROOT / args.corr_guard
    db_path = args.strategy_db if args.strategy_db.is_absolute() else ROOT / args.strategy_db

    report = build_report(
        log_path=log_path,
        exit_path=exit_path,
        corr_path=corr_path,
        db_path=db_path,
        signal_proxy=not bool(args.no_signal_proxy),
        proxy_days=args.proxy_days,
    )
    report["inputs"]["log"] = str(log_path)

    summary = format_summary(report)
    out_txt = args.out_txt if args.out_txt.is_absolute() else Path(__file__).resolve().parent / args.out_txt.name
    out_json = args.out_json if args.out_json.is_absolute() else Path(__file__).resolve().parent / args.out_json.name
    out_txt.write_text(summary + "\n", encoding="utf-8")
    out_json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    if args.json_only:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(summary)
        print(f"\nWrote: {out_txt}")
        print(f"Wrote: {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
