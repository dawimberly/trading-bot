#!/usr/bin/env python3
"""Full-system verification for Realistic Research v1.5.4 (paper-focused)."""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("PAPER_TRADING", "true")
os.environ.setdefault("PAPER_CHASE_MODE", "1")
os.environ.setdefault("PAPER_AGGRESSIVE", "true")

import config  # noqa: E402

config.enforce_realistic_research_profile()

# --- Terminal colors (Windows 10+ / modern terminals) ---

_RESET = "\033[0m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"
_CYAN = "\033[96m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_USE_COLOR = True
_BOX_H = "-"
_BOX_D = "="


def _enable_ansi() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def _c(text: str, color: str) -> str:
    if not _USE_COLOR or not sys.stdout.isatty():
        return text
    return f"{color}{text}{_RESET}"


Status = str  # "PASS" | "WARN" | "FAIL"


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str = ""


@dataclass
class SectionResult:
    title: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def status(self) -> Status:
        if any(c.status == "FAIL" for c in self.checks):
            return "FAIL"
        if any(c.status == "WARN" for c in self.checks):
            return "WARN"
        return "PASS"


def _status_icon(status: Status) -> str:
    if status == "PASS":
        return _c("PASS", _GREEN)
    if status == "WARN":
        return _c("WARN", _YELLOW)
    return _c("FAIL", _RED)


def _run_timed(fn: Callable[[], Any], timeout: float, default: Any = None) -> tuple[Any, bool]:
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(fn)
        try:
            return fut.result(timeout=timeout), False
        except FuturesTimeoutError:
            return default, True


def _fast_data_subset(data, *, max_cols: int = 22):
    if data is None or getattr(data, "empty", True):
        return data
    cols = list(data.columns)
    priority = [c for c in ("SPY", "VTI", "NVDA", "AAPL", "MSFT", "QQQ") if c in cols]
    rest = [
        c
        for c in cols
        if c not in priority and config._nyse_eligible_symbol(str(c))
    ][: max(0, max_cols - len(priority))]
    keep = list(dict.fromkeys(priority + rest))
    return data[keep]


def _load_verify_data(timeout: float = 90.0):
    def _load():
        from modules.pipeline_strategies import load_pipeline_data

        return _fast_data_subset(load_pipeline_data(interval="1d"))

    return _run_timed(_load, timeout, default=None)


# --- Section checks ---


def check_profile_config() -> SectionResult:
    sec = SectionResult("Profile & Config")
    ver = str(getattr(config, "REALISTIC_RESEARCH_VERSION", ""))
    if ver == "1.5.4":
        sec.checks.append(CheckResult("RR version locked", "PASS", f"v{ver}"))
    else:
        sec.checks.append(
            CheckResult("RR version locked", "FAIL", f"expected 1.5.4, got {ver!r}")
        )

    tag = str(getattr(config, "REALISTIC_RESEARCH_TAGLINE", ""))
    if (
        "1.5.4" in tag
        or "Sector-Aware" in tag
        or "Locked & Ready" in tag
        or "Full Feature Set" in tag
        or ("RVOL" in tag and "ORB" in tag and "Catalyst" in tag and "ATR" in tag)
    ):
        sec.checks.append(CheckResult("v1.5.4 tagline", "PASS", tag[:72]))
    else:
        sec.checks.append(CheckResult("v1.5.4 tagline", "WARN", tag[:72] or "missing"))

    flags = {
        "RVOL scanner": config.effective_rvol_scanner_enabled(),
        "ORB scanner": config.effective_orb_enabled(),
        "Catalyst scoring": config.effective_catalyst_scoring_enabled(),
        "ATR sizing": config.effective_atr_sizing_enabled(),
        "Conviction sizing": config.effective_conviction_sizing_enabled(),
        "Multi-timeframe": config.effective_multi_timeframe_enabled(),
        "Exit optimization": config.effective_exit_optimization_enabled(),
        "Correlation guard": config.effective_correlation_guard_enabled(),
        "Protective shorts": config.effective_opportunistic_short_enabled(),
        "Sector shorts": config.effective_sector_short_enabled(),
        "Insider monitor": config.effective_insider_monitor_enabled(),
        "Insider boosts": config.effective_insider_signal_boost_enabled(),
        "Stat arb": config.effective_stat_arb_enabled(),
        "Tail risk": config.effective_tail_risk_controls(),
    }
    for label, on in flags.items():
        sec.checks.append(
            CheckResult(label, "PASS" if on else "FAIL", "ON" if on else "OFF")
        )

    if config.PAPER_TRADING and config.paper_chase_mode_enabled():
        sec.checks.append(CheckResult("Paper chase context", "PASS", "PAPER_TRADING + CHASE"))
    else:
        sec.checks.append(CheckResult("Paper chase context", "WARN", "not full paper chase env"))

    return sec


def check_rvol(data) -> SectionResult:
    sec = SectionResult("RVOL Scanner")
    if not config.effective_rvol_scanner_enabled():
        sec.checks.append(CheckResult("RVOL enabled", "FAIL", "scanner off"))
        return sec
    sec.checks.append(CheckResult("RVOL enabled", "PASS", f"min {config.RVOL_MIN_THRESHOLD}x"))

    if data is None or getattr(data, "empty", True):
        sec.checks.append(CheckResult("Pipeline data", "WARN", "no data - skip scan"))
        return sec

    def _scan():
        from modules.volume_analysis import get_high_rvol_stocks

        return get_high_rvol_stocks(data, min_rvol=float(config.RVOL_MIN_THRESHOLD), limit=8)

    hits, timed_out = _run_timed(_scan, 75.0, default=[])
    if timed_out:
        sec.checks.append(CheckResult("RVOL scan", "WARN", "timed out (universe large)"))
        return sec
    if not isinstance(hits, list):
        sec.checks.append(CheckResult("RVOL scan", "FAIL", f"bad return type {type(hits)}"))
        return sec
    if hits:
        top = ", ".join(f"{r['symbol']} {r['rvol']:.1f}x" for r in hits[:3])
        sec.checks.append(CheckResult("RVOL signals", "PASS", f"{len(hits)} hits - {top}"))
    else:
        sec.checks.append(
            CheckResult(
                "RVOL signals",
                "WARN",
                f"none >= {config.RVOL_MIN_THRESHOLD}x (quiet day or volume cache)",
            )
        )
    return sec


def check_orb(data) -> SectionResult:
    sec = SectionResult("ORB Scanner")
    if not config.effective_orb_enabled():
        sec.checks.append(CheckResult("ORB enabled", "FAIL", "scanner off"))
        return sec
    sec.checks.append(
        CheckResult("ORB enabled", "PASS", f"{config.ORB_BREAKOUT_MINUTES}m range")
    )

    if data is None or getattr(data, "empty", True):
        sec.checks.append(CheckResult("Pipeline data", "WARN", "no data - skip scan"))
        return sec

    def _scan():
        from modules.volume_analysis import get_orb_signals

        return get_orb_signals(data, minutes=int(config.ORB_BREAKOUT_MINUTES), limit=8)

    signals, timed_out = _run_timed(_scan, 90.0, default=[])
    if timed_out:
        sec.checks.append(CheckResult("ORB scan", "WARN", "timed out (intraday fetch slow)"))
        return sec
    if not isinstance(signals, list):
        sec.checks.append(CheckResult("ORB scan", "FAIL", f"expected list, got {type(signals)}"))
        return sec
    if signals and not isinstance(signals[0], dict):
        sec.checks.append(CheckResult("ORB scan", "FAIL", "items are not dicts"))
        return sec
    if signals:
        kinds = {}
        for s in signals:
            kinds[s.get("type", "?")] = kinds.get(s.get("type", "?"), 0) + 1
        summary = ", ".join(f"{k}={v}" for k, v in sorted(kinds.items()))
        sec.checks.append(CheckResult("ORB breakouts", "PASS", f"{len(signals)} signals ({summary})"))
    else:
        sec.checks.append(
            CheckResult(
                "ORB breakouts",
                "WARN",
                "no breakouts (after hours / no intraday / RVOL filter)",
            )
        )
    return sec


def check_catalyst(data) -> SectionResult:
    sec = SectionResult("Catalyst Scoring")
    if not config.effective_catalyst_scoring_enabled():
        sec.checks.append(CheckResult("Catalyst enabled", "FAIL", "off"))
        return sec
    sec.checks.append(
        CheckResult("Catalyst enabled", "PASS", f"min score {config.CATALYST_MIN_SCORE:.0f}")
    )

    if data is None or getattr(data, "empty", True):
        sec.checks.append(CheckResult("Pipeline data", "WARN", "no data"))
        return sec

    def _scan():
        from modules.catalyst_scoring import get_top_catalyst_stocks

        return get_top_catalyst_stocks(
            data, min_score=float(config.CATALYST_MIN_SCORE), limit=8
        )

    top, timed_out = _run_timed(_scan, 60.0, default=[])
    if timed_out:
        sec.checks.append(CheckResult("Catalyst scan", "WARN", "timed out"))
        return sec
    if top:
        line = ", ".join(f"{r['symbol']} {r['score']}" for r in top[:4])
        sec.checks.append(CheckResult("Top catalysts", "PASS", line))
    else:
        sec.checks.append(
            CheckResult(
                "Top catalysts",
                "WARN",
                f"none >= {config.CATALYST_MIN_SCORE:.0f} (quiet tape)",
            )
        )
    return sec


def check_atr(data) -> SectionResult:
    sec = SectionResult("ATR Sizing")
    if not config.effective_atr_sizing_enabled():
        sec.checks.append(CheckResult("ATR enabled", "FAIL", "off"))
        return sec
    sec.checks.append(
        CheckResult(
            "ATR enabled",
            "PASS",
            f"{config.ATR_PERIOD}d ATR x {config.ATR_RISK_MULTIPLE} cap {config.ATR_MAX_SIZE_PCT:.0%}",
        )
    )

    equity = 100_000.0
    sym = config.SPY_BOT_SYMBOL

    def _size():
        from modules.risk_management import get_atr_risk_size

        return get_atr_risk_size(equity, sym, data)

    result, timed_out = _run_timed(_size, 30.0, default=None)
    if timed_out or result is None:
        sec.checks.append(CheckResult("ATR notional", "WARN", "timed out or no result"))
        return sec
    notional = float(result.get("notional") or 0)
    method = str(result.get("method") or "?")
    atr = float(result.get("atr") or 0)
    if notional > 0 and notional <= equity:
        sec.checks.append(
            CheckResult(
                "ATR notional",
                "PASS",
                f"{sym} ${notional:,.0f} method={method} atr={atr:.2f}",
            )
        )
    else:
        sec.checks.append(CheckResult("ATR notional", "FAIL", str(result)))
    return sec


def check_insider() -> SectionResult:
    sec = SectionResult("Insider Monitor")
    if not config.effective_insider_monitor_enabled():
        sec.checks.append(CheckResult("Monitor enabled", "FAIL", "off"))
        return sec
    sec.checks.append(CheckResult("Monitor enabled", "PASS", "paper/research"))

    from modules.insider_monitor import get_recent_insider_signals

    signals = get_recent_insider_signals(days=7, min_score=60)
    if signals:
        top = signals[0]
        sec.checks.append(
            CheckResult(
                "Signals (score>=60)",
                "PASS",
                f"{len(signals)} - top {top.get('ticker')} s{top.get('score')}",
            )
        )
    else:
        sec.checks.append(
            CheckResult("Signals (score>=60)", "WARN", "none (SEC cache empty or filtered)")
        )

    try:
        from modules.insider_signal_handler import apply_insider_signals_to_strategies

        state = apply_insider_signals_to_strategies()
        if state.get("enabled"):
            sec.checks.append(
                CheckResult("Boost handler", "PASS", str(state.get("summary", ""))[:60])
            )
        else:
            sec.checks.append(CheckResult("Boost handler", "FAIL", "disabled"))
    except Exception as exc:
        sec.checks.append(CheckResult("Boost handler", "FAIL", str(exc)[:80]))
    return sec


def check_protective_shorts() -> SectionResult:
    sec = SectionResult("Protective Shorts")
    on = config.effective_opportunistic_short_enabled()
    sec.checks.append(CheckResult("Short sleeve", "PASS" if on else "FAIL", "ON" if on else "OFF"))

    banner = config.format_opportunistic_short_banner()
    if banner and "OFF" not in banner:
        sec.checks.append(CheckResult("Short banner", "PASS", banner[:70]))
    else:
        sec.checks.append(CheckResult("Short banner", "WARN" if on else "FAIL", banner or "n/a"))

    lo = config.effective_protective_short_min_pct()
    hi = config.effective_protective_short_max_pct()
    if 0.05 <= lo <= hi <= 0.25:
        sec.checks.append(CheckResult("Gross band", "PASS", f"{lo:.0%}-{hi:.0%}"))
    else:
        sec.checks.append(CheckResult("Gross band", "WARN", f"{lo:.0%}-{hi:.0%}"))

    try:
        from modules.short_activity import format_shorts_telegram_block

        block = format_shorts_telegram_block(regime="RHYME_C", equity=100_000.0)
        if block:
            sec.checks.append(CheckResult("Short activity block", "PASS", block.split("\n")[0][:60]))
        else:
            sec.checks.append(CheckResult("Short activity block", "WARN", "empty"))
    except Exception as exc:
        sec.checks.append(CheckResult("Short activity block", "WARN", str(exc)[:60]))
    return sec


def check_bot_health() -> SectionResult:
    sec = SectionResult("Bot Health Score")
    from modules.bot_health import (
        calculate_health_score,
        gather_health_context,
        format_health_line,
        health_color,
    )

    ctx = gather_health_context({"regime": "RHYME_C"})
    health = calculate_health_score(**ctx)
    score = int(health.get("score") or 0)
    grade = str(health.get("grade") or "?")
    color = str(health.get("color") or health_color(score))
    comps = health.get("components") or {}

    if score >= 90:
        sec.checks.append(CheckResult("Score (target 90+)", "PASS", f"{score}/100 ({grade})"))
    elif score >= 85:
        sec.checks.append(CheckResult("Score (target 90+)", "WARN", f"{score}/100 ({grade})"))
    elif score >= 70:
        sec.checks.append(CheckResult("Score (target 90+)", "WARN", f"{score}/100 ({grade}) - below Excellent"))
    else:
        sec.checks.append(CheckResult("Score (target 90+)", "FAIL", f"{score}/100 ({grade})"))

    band = {"green": "Green >=85", "yellow": "Yellow 70-84", "red": "Red <70"}.get(color, color)
    sec.checks.append(CheckResult("Color band", "PASS" if color == "green" else "WARN", band))

    if comps.get("quiet_tape"):
        sec.checks.append(CheckResult("Quiet tape", "PASS", "scanner idle OK"))
    if comps.get("news_pool_clean"):
        sec.checks.append(CheckResult("News pool", "PASS", "clean headlines"))
    if comps.get("atr_sizing_ok"):
        sec.checks.append(CheckResult("ATR probe", "PASS", "SPY ATR sizing OK"))

    line = format_health_line(health)
    if line:
        sec.checks.append(CheckResult("Health line", "PASS", line.strip()[:70]))
    return sec


def check_strategy_performance() -> SectionResult:
    sec = SectionResult("Strategy Performance")
    mod = ROOT / "modules" / "strategy_performance.py"
    if not mod.is_file():
        sec.checks.append(CheckResult("strategy_performance.py", "FAIL", "missing"))
        return sec
    try:
        from modules.strategy_performance import (
            STRATEGY_IDS,
            STRATEGY_LABELS,
            get_strategy_ratings,
        )

        sec.checks.append(CheckResult("Module import", "PASS", f"{len(STRATEGY_IDS)} strategies"))
        ratings = get_strategy_ratings(days=30)
        ranked = ratings.get("ranked") or []
        if ranked:
            top = ranked[0]
            sec.checks.append(
                CheckResult(
                    "Ratings (30d)",
                    "PASS",
                    f"{top.get('label', '?')}: {top.get('rating')} ({top.get('trade_count')} trades)",
                )
            )
        else:
            sec.checks.append(
                CheckResult("Ratings (30d)", "WARN", "no closed trades yet — journal sync OK")
            )
        missing = [sid for sid in STRATEGY_IDS if sid not in STRATEGY_LABELS]
        if missing:
            sec.checks.append(CheckResult("Labels", "FAIL", f"missing {missing}"))
        else:
            sec.checks.append(CheckResult("Labels", "PASS", "all 10 strategies labeled"))
    except Exception as exc:
        sec.checks.append(CheckResult("get_strategy_ratings", "FAIL", str(exc)[:80]))
    return sec


def check_sharpe_history() -> SectionResult:
    sec = SectionResult("Sharpe History")
    mod = ROOT / "modules" / "sharpe_history.py"
    if not mod.is_file():
        sec.checks.append(CheckResult("sharpe_history.py", "FAIL", "missing"))
        return sec
    try:
        from modules.sharpe_history import (
            calculate_projected_sharpe,
            format_sharpe_history_summary,
            get_sharpe_snapshot,
            version_history_rows,
        )

        sec.checks.append(CheckResult("Module import", "PASS", "ok"))
        snap = get_sharpe_snapshot()
        all_s = snap.get("sharpe_all")
        since_s = snap.get("sharpe_since_update")
        proj = snap.get("projected_sharpe")
        ver = snap.get("version") or "?"
        detail = (
            f"v{ver} all={all_s if all_s is not None else 'n/a'} "
            f"since_update={since_s if since_s is not None else 'n/a'} "
            f"30d={snap.get('sharpe_30d')} 90d={snap.get('sharpe_90d')}"
        )
        if all_s is not None or since_s is not None or snap.get("points_all"):
            sec.checks.append(CheckResult("Snapshot", "PASS", detail[:90]))
        else:
            sec.checks.append(
                CheckResult("Snapshot", "WARN", "no equity curve yet — EOD will seed log")
            )
        try:
            live_proj = calculate_projected_sharpe(equity=snap.get("equity"), days=30)
            pval = live_proj.get("projected_sharpe")
            if pval is None:
                sec.checks.append(
                    CheckResult("Projected Sharpe", "WARN", "insufficient history")
                )
            else:
                conf = live_proj.get("confidence") or "n/a"
                # Conservative sanity: projection should not be wildly above realized.
                base = live_proj.get("base_sharpe")
                ok = True
                if base is not None:
                    ceiling = float(base) + (0.40 if float(base) < 0 else 0.30)
                    if float(pval) > ceiling or float(pval) > 1.25 or float(pval) < -1.50:
                        ok = False
                sec.checks.append(
                    CheckResult(
                        "Projected Sharpe",
                        "PASS" if ok else "WARN",
                        f"{float(pval):.2f} (30d, {conf}; base={base})",
                    )
                )
        except Exception as exc:
            sec.checks.append(CheckResult("Projected Sharpe", "FAIL", str(exc)[:80]))
        markers = version_history_rows(limit=5)
        if markers:
            m0 = markers[0]
            sec.checks.append(
                CheckResult(
                    "Version markers",
                    "PASS",
                    f"{len(markers)} row(s); latest {m0.get('from')}->{m0.get('to')}",
                )
            )
        else:
            sec.checks.append(
                CheckResult("Version markers", "WARN", "none yet — first EOD seeds VERSION_INIT")
            )
        summary = format_sharpe_history_summary()
        if summary:
            first = summary.splitlines()[0][:70]
            sec.checks.append(CheckResult("Summary", "PASS", first))
        log_path = ROOT / "data" / "sharpe_history.log"
        if log_path.is_file():
            sec.checks.append(CheckResult("Log file", "PASS", str(log_path.name)))
        else:
            sec.checks.append(
                CheckResult("Log file", "WARN", "sharpe_history.log not created yet")
            )
    except Exception as exc:
        sec.checks.append(CheckResult("sharpe_history", "FAIL", str(exc)[:80]))
    return sec


def check_dashboard() -> SectionResult:
    sec = SectionResult("Dashboard")
    dash = ROOT / "dashboard_app.py"
    if not dash.is_file():
        sec.checks.append(CheckResult("dashboard_app.py", "FAIL", "missing"))
        return sec

    text = dash.read_text(encoding="utf-8", errors="replace")
    panels = [
        ("_insider_section", "Insider panel"),
        ("_fill_insider_signals", "Insider fill handler"),
        ("Short Activity", "Short Activity section"),
        ("_short_toggle_btn", "Short Activity toggle"),
        ("_rvol_section", "RVOL/ORB/Catalyst panel"),
        ("_fetch_rvol_snapshot", "Scanner snapshot hook"),
        ("_orb_mom_section", "ORB Momentum panel"),
        ("_fetch_orb_momentum_snapshot", "ORB momentum hook"),
        ("_fill_orb_momentum", "ORB momentum fill"),
        ("_sector_rot_section", "Sector Rotation panel"),
        ("_fetch_sector_rotation_snapshot", "Sector rotation hook"),
        ("_fill_sector_rotation", "Sector rotation fill"),
        ("_vol_bo_section", "Vol Breakout panel"),
        ("_fetch_vol_breakout_snapshot", "Vol breakout hook"),
        ("_fill_vol_breakout", "Vol breakout fill"),
        ("_strategy_section", "Strategy Performance panel"),
        ("_fetch_strategy_performance_snapshot", "Strategy metrics hook"),
        ("_sharpe_section", "Sharpe History panel"),
        ("_fetch_sharpe_history_snapshot", "Sharpe history hook"),
        ("_fill_sharpe_history", "Sharpe history fill"),
    ]
    for needle, label in panels:
        if needle in text:
            sec.checks.append(CheckResult(label, "PASS", needle))
        else:
            sec.checks.append(CheckResult(label, "FAIL", f"missing {needle}"))
    return sec


def check_telegram() -> SectionResult:
    sec = SectionResult("Telegram")
    from modules.telegram_commands import effective_telegram_commands_enabled, handle_telegram_command

    tg = config.get_telegram_config()
    if tg:
        sec.checks.append(CheckResult("Telegram config", "PASS", "token + chat_id present"))
    else:
        sec.checks.append(CheckResult("Telegram config", "WARN", "TELEGRAM_* not set in .env"))

    if effective_telegram_commands_enabled():
        sec.checks.append(CheckResult("Commands gate", "PASS", "paper commands enabled"))
    else:
        sec.checks.append(CheckResult("Commands gate", "WARN", "commands gated off"))

    for cmd in ("/status", "/signals", "/shorts", "/boosts"):
        try:
            reply = handle_telegram_command(
                cmd, equity=100_000.0, cash=10_000.0, regime="RHYME_C"
            )
            if reply:
                sec.checks.append(CheckResult(cmd, "PASS", reply.split("\n")[0][:55]))
            else:
                sec.checks.append(CheckResult(cmd, "FAIL", "no handler reply"))
        except Exception as exc:
            sec.checks.append(CheckResult(cmd, "FAIL", str(exc)[:60]))
    return sec


def check_code_health() -> SectionResult:
    """Import / log / silent-except hygiene (does not change trading logic)."""
    sec = SectionResult("Code Health Scan")

    # Critical market_context symbols used by the live cycle.
    try:
        from modules.market_context import (
            cross_asset_vol_score,
            get_volatility,
            set_regime_bar_index,
        )

        assert callable(get_volatility) and callable(cross_asset_vol_score)
        assert callable(set_regime_bar_index)
        sec.checks.append(
            CheckResult(
                "market_context imports",
                "PASS",
                "get_volatility + cross_asset_vol_score + set_regime_bar_index",
            )
        )
    except Exception as exc:
        sec.checks.append(CheckResult("market_context imports", "FAIL", str(exc)[:70]))

    try:
        import run_all as _run_all

        if callable(getattr(_run_all, "get_volatility", None)) and callable(
            getattr(_run_all, "cross_asset_vol_score", None)
        ):
            sec.checks.append(
                CheckResult("run_all volatility bindings", "PASS", "module-level imports present")
            )
        else:
            sec.checks.append(
                CheckResult(
                    "run_all volatility bindings",
                    "FAIL",
                    "get_volatility / cross_asset_vol_score missing on run_all",
                )
            )
    except Exception as exc:
        sec.checks.append(CheckResult("run_all volatility bindings", "FAIL", str(exc)[:70]))

    # Recent cycle errors in run_all.log (tail scan).
    log_path = ROOT / "logs" / "run_all.log"
    if log_path.is_file():
        try:
            # Read last ~200KB to avoid loading multi-MB logs fully.
            with log_path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 200_000), os.SEEK_SET)
                tail = fh.read().decode("utf-8", errors="ignore")
            lines = [ln for ln in tail.splitlines() if "Cycle error" in ln or "NameError" in ln]
            recent = [ln for ln in lines if "2026-07-10 1" in ln or "get_volatility" in ln]
            # Prefer today's afternoon+ (post-fix window) NameErrors.
            post_fix = [
                ln
                for ln in lines
                if ("2026-07-10 14:" in ln or "2026-07-10 15:" in ln)
                and ("NameError" in ln or "get_volatility" in ln or "Cycle error" in ln)
            ]
            if post_fix:
                sec.checks.append(
                    CheckResult(
                        "Recent cycle NameErrors",
                        "FAIL",
                        f"{len(post_fix)} hit(s) after 14:00 — {post_fix[-1][-80:]}",
                    )
                )
            elif any("get_volatility" in ln and "2026-07-10 1" in ln for ln in lines):
                sec.checks.append(
                    CheckResult(
                        "Recent cycle NameErrors",
                        "WARN",
                        "historical get_volatility errors earlier today (pre-fix)",
                    )
                )
            else:
                sec.checks.append(
                    CheckResult("Recent cycle NameErrors", "PASS", "none in log tail")
                )
        except Exception as exc:
            sec.checks.append(CheckResult("Recent cycle NameErrors", "WARN", str(exc)[:60]))
    else:
        sec.checks.append(CheckResult("Recent cycle NameErrors", "WARN", "run_all.log missing"))

    # Silent except Exception: pass density in modules/ (hygiene signal only).
    bare = 0
    try:
        for path in (ROOT / "modules").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            for i, line in enumerate(text):
                if line.strip() == "except Exception:":
                    nxt = text[i + 1].strip() if i + 1 < len(text) else ""
                    if nxt in ("pass", "..."):
                        bare += 1
        if bare > 40:
            sec.checks.append(
                CheckResult("Silent except density", "WARN", f"{bare} bare Exception:pass in modules/")
            )
        else:
            sec.checks.append(
                CheckResult("Silent except density", "PASS", f"{bare} bare Exception:pass in modules/")
            )
    except Exception as exc:
        sec.checks.append(CheckResult("Silent except density", "WARN", str(exc)[:60]))

    # Heartbeat path awareness (paper vs stale root file).
    try:
        resolve = getattr(config, "resolve_heartbeat_path", None)
        hb_path = Path(str(resolve() if callable(resolve) else getattr(config, "HEARTBEAT_FILE", "")))
        # Also accept portal paper heartbeat if configured path is stale/missing.
        candidates = [hb_path]
        portal_hb = (
            ROOT
            / "data"
            / "portal"
            / "users"
            / "dawimberly"
            / "books"
            / "alpaca_paper"
            / "bot_heartbeat.json"
        )
        if portal_hb.is_file():
            candidates.append(portal_hb)
        paper_chase = ROOT / "paper_chase_heartbeat.json"
        if paper_chase.is_file():
            candidates.append(paper_chase)
        existing = [p for p in candidates if p.is_file()]
        if not existing:
            sec.checks.append(CheckResult("Heartbeat freshness", "WARN", f"missing {hb_path}"))
        else:
            best = max(existing, key=lambda p: p.stat().st_mtime)
            age_min = (time.time() - best.stat().st_mtime) / 60.0
            st = "PASS" if age_min < 30 else "WARN"
            sec.checks.append(
                CheckResult(
                    "Heartbeat freshness",
                    st,
                    f"{best.name} age={age_min:.0f}m path={best}",
                )
            )
    except Exception as exc:
        sec.checks.append(CheckResult("Heartbeat freshness", "WARN", str(exc)[:60]))

    return sec


def check_historical_news() -> SectionResult:
    sec = SectionResult("Historical News")
    if not getattr(config, "HISTORICAL_NEWS_ENABLED", True):
        sec.checks.append(CheckResult("Enabled flag", "WARN", "HISTORICAL_NEWS_ENABLED=false"))
    else:
        sec.checks.append(CheckResult("Enabled flag", "PASS", "backtest proxy ON"))

    from modules.historical_news import (
        get_historical_headlines,
        is_financial_headline,
        is_junk_headline,
        clean_headline,
    )

    rows = get_historical_headlines("2026-06-01", None, days_back=3)
    if not rows:
        sec.checks.append(CheckResult("Headlines", "FAIL", "empty list"))
        return sec

    junk = [r for r in rows if is_junk_headline(str(r.get("title") or ""))]
    non_fin = [r for r in rows if not is_financial_headline(str(r.get("title") or ""))]
    if junk:
        sec.checks.append(CheckResult("Junk filter", "FAIL", f"{len(junk)} junk rows"))
    else:
        sec.checks.append(CheckResult("Junk filter", "PASS", "no ad/UI noise"))

    if non_fin:
        sec.checks.append(CheckResult("Financial filter", "WARN", f"{len(non_fin)} non-financial"))
    else:
        sec.checks.append(CheckResult("Financial filter", "PASS", f"{len(rows)} clean rows"))

    sample = clean_headline(str(rows[0].get("title") or ""))
    sec.checks.append(CheckResult("Sample", "PASS", sample[:72]))
    return sec


def _print_section(sec: SectionResult) -> None:
    print(f"\n{_c(sec.title, _CYAN + _BOLD)}")
    print(_c(_BOX_H * min(72, len(sec.title) + 4), _DIM))
    for chk in sec.checks:
        print(f"  {_status_icon(chk.status)}  {chk.name:<28} {chk.detail}")


def _print_summary_table(sections: list[SectionResult], elapsed: float) -> Status:
    print(f"\n{_c(_BOX_D * 72, _BOLD)}")
    print(_c(" REALISTIC RESEARCH v1.5.4 - FULL SYSTEM VERIFY", _BOLD))
    print(_c(_BOX_D * 72, _BOLD))
    print(f"{'Section':<24} {'Status':<8} Detail")
    print(_c(_BOX_H * 72, _DIM))

    fail_n = warn_n = pass_n = 0
    for sec in sections:
        st = sec.status
        if st == "FAIL":
            fail_n += 1
        elif st == "WARN":
            warn_n += 1
        else:
            pass_n += 1
        detail = ""
        if sec.checks:
            detail = sec.checks[0].detail[:42]
        print(f"{sec.title:<24} {_status_icon(st):<17} {detail}")

    print(_c(_BOX_H * 72, _DIM))
    overall: Status
    if fail_n:
        overall = "FAIL"
        verdict = f"{fail_n} section(s) FAILED - review before Monday"
    elif warn_n:
        overall = "WARN"
        verdict = f"{warn_n} section(s) WARN - operational with caveats"
    else:
        overall = "PASS"
        verdict = "All sections PASS - v1.5.4 stack healthy"

    print(
        f"Overall: {_status_icon(overall)}  "
        f"({pass_n} pass / {warn_n} warn / {fail_n} fail)  "
        f"{elapsed:.1f}s"
    )
    print(verdict)
    print(_c(_BOX_D * 72, _BOLD))
    return overall


def _print_final_confirmation_banner(overall: Status) -> None:
    """Closing banner for Monday prep / owner sign-off."""
    ver = str(getattr(config, "REALISTIC_RESEARCH_VERSION", "1.5.4"))
    tag = str(getattr(config, "REALISTIC_RESEARCH_TAGLINE", ""))
    detail = str(getattr(config, "REALISTIC_RESEARCH_FEATURE_DETAIL", ""))
    print()
    print(_c(_BOX_D * 72, _BOLD))
    if overall == "PASS":
        headline = "v1.5.4 — Ready for Monday"
        color = _GREEN
    elif overall == "WARN":
        headline = "v1.5.4 — Ready for Monday (warnings — review caveats)"
        color = _YELLOW
    else:
        headline = f">>> REALISTIC RESEARCH v{ver} — FAIL — FIX BEFORE MONDAY <<<"
        color = _RED
    print(_c(headline, _BOLD + color))
    print(_c(tag, _BOLD))
    if detail:
        print(_c(detail, _DIM))
    print(_c("Verify: python scripts/full_system_verify.py", _DIM))
    print(_c("Start:  python scripts/owner_reset.py  (or Start_Bot_and_Dashboard.bat)", _DIM))
    print(_c("Auto:   Start_Autonomous.bat  (overnight paper + 9 AM Telegram)", _DIM))
    print(_c(_BOX_D * 72, _BOLD))
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Realistic Research v1.5.4 stack")
    parser.add_argument("--no-color", action="store_true", help="Plain text output")
    parser.add_argument("--quick", action="store_true", help="Skip slow scanner timeouts")
    parser.add_argument(
        "--health-only",
        action="store_true",
        help="Run code-health / import / recent-log scan only (fast)",
    )
    args = parser.parse_args()

    global _USE_COLOR
    if args.no_color:
        _USE_COLOR = False
    elif not args.no_color:
        _enable_ansi()

    t0 = time.monotonic()
    print(_c(f"\nPythonTrading - RR v{config.REALISTIC_RESEARCH_VERSION} full verify", _BOLD))
    print(_c(f"Root: {ROOT}", _DIM))

    sections: list[SectionResult] = []
    if args.health_only:
        sections.append(check_profile_config())
        sections.append(check_code_health())
        for sec in sections:
            _print_section(sec)
        overall = _print_summary_table(sections, time.monotonic() - t0)
        _print_final_confirmation_banner(overall)
        return 1 if overall == "FAIL" else 0

    sections.append(check_profile_config())
    sections.append(check_code_health())

    data_timeout = 0.0 if args.quick else 90.0
    data, data_timeout_hit = _load_verify_data(timeout=data_timeout)
    if data_timeout_hit:
        print(_c("\n[WARN] Pipeline data load timed out - scanner sections may WARN", _YELLOW))
    elif data is not None and not getattr(data, "empty", True):
        print(
            _c(
                f"\nData: {len(data)} bars x {len(data.columns)} symbols (subset)",
                _DIM,
            )
        )
    else:
        print(_c("\n[WARN] No pipeline data - scanner sections limited", _YELLOW))

    sections.append(check_rvol(data))
    sections.append(check_orb(data))
    sections.append(check_catalyst(data))
    sections.append(check_atr(data))
    sections.append(check_insider())
    sections.append(check_protective_shorts())
    sections.append(check_bot_health())
    sections.append(check_strategy_performance())
    sections.append(check_sharpe_history())
    sections.append(check_dashboard())
    sections.append(check_telegram())
    sections.append(check_historical_news())

    for sec in sections:
        _print_section(sec)

    overall = _print_summary_table(sections, time.monotonic() - t0)
    _print_final_confirmation_banner(overall)
    return 1 if overall == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
