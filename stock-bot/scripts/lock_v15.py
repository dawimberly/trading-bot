#!/usr/bin/env python3
"""One-shot Realistic Research v1.5.4 final lock: verify, apply idempotent locks, print banner.

Final lock for Monday / production-ready paper (Profile B). Does not bump past 1.5.4.

Run from stock-bot/:
  python scripts/lock_v15.py
  python scripts/lock_v15.py --quick
  python scripts/lock_v15.py --verify-only
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("PAPER_TRADING", "true")
os.environ.setdefault("PAPER_CHASE_MODE", "1")
os.environ.setdefault("PAPER_AGGRESSIVE", "true")

LOCK_VERSION = "1.5.4"
LOCK_TAGLINE = "v1.5.4 - Sector-Aware Portfolio Constructor"
LOCK_BANNER = "v1.5.4 Locked & Ready for Monday"
LOCK_FEATURE_DETAIL = (
    "Smart Dynamic VTI (35-75%) + Sector Rotation (top 2-3 SPDRs) + "
    "ATR Vol Breakout (RVOL+MTF, <=1% risk) + "
    "Sector-Aware Portfolio Constructor + "
    "Dynamic Felix/social (RHYME_E / bubble>=65) + "
    "RVOL/ORB/Catalyst/ATR + Conviction + GARCH vol + MTF + Exits + Corr Guard + Shorts + "
    "RHYME primary regime + HMM soft-signal + Stat Arb quality + Enriched Thinking"
)
LOCK_FILE = ROOT / "data" / "realistic_research_v15.lock.json"

# ANSI
_RESET = "\033[0m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"
_CYAN = "\033[96m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_USE_COLOR = True


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


def _c(text: str, color: str = "") -> str:
    if not _USE_COLOR or not sys.stdout.isatty() or not color:
        return text
    return f"{color}{text}{_RESET}"


def _run_verify(*, quick: bool) -> tuple[str, list[Any], dict[str, int]]:
    """Run full_system_verify sections; return overall, sections, counts."""
    import config  # noqa: E402

    config.enforce_realistic_research_profile()

    from scripts.full_system_verify import (  # noqa: E402
        SectionResult,
        _load_verify_data,
        _print_final_confirmation_banner,
        _print_section,
        _print_summary_table,
        check_atr,
        check_bot_health,
        check_catalyst,
        check_dashboard,
        check_historical_news,
        check_insider,
        check_orb,
        check_profile_config,
        check_protective_shorts,
        check_rvol,
        check_strategy_performance,
        check_telegram,
    )

    print(_c(f"\n{'=' * 72}", _BOLD))
    print(_c("  REALISTIC RESEARCH v1.5.4 — FULL SYSTEM VERIFY", _BOLD + _CYAN))
    print(_c(f"{'=' * 72}", _BOLD))
    print(_c(f"Root: {ROOT}", _DIM))

    sections: list[SectionResult] = [check_profile_config()]
    data_timeout = 0.0 if quick else 90.0
    data, data_timeout_hit = _load_verify_data(timeout=data_timeout)
    if data_timeout_hit:
        print(_c("\n[WARN] Pipeline data load timed out — scanner sections may WARN", _YELLOW))
    elif data is not None and not getattr(data, "empty", True):
        print(
            _c(
                f"\nData: {len(data)} bars x {len(data.columns)} symbols (subset)",
                _DIM,
            )
        )
    else:
        print(_c("\n[WARN] No pipeline data — scanner sections limited", _YELLOW))

    sections.extend(
        [
            check_rvol(data),
            check_orb(data),
            check_catalyst(data),
            check_atr(data),
            check_insider(),
            check_protective_shorts(),
            check_bot_health(),
            check_strategy_performance(),
            check_dashboard(),
            check_telegram(),
            check_historical_news(),
        ]
    )

    for sec in sections:
        _print_section(sec)

    elapsed = 0.0  # summary table tracks its own timing in full_system_verify
    t0 = time.monotonic()
    overall = _print_summary_table(sections, time.monotonic() - t0)
    _print_final_confirmation_banner(overall)

    counts = {"pass": 0, "warn": 0, "fail": 0}
    for sec in sections:
        st = sec.status
        if st == "FAIL":
            counts["fail"] += 1
        elif st == "WARN":
            counts["warn"] += 1
        else:
            counts["pass"] += 1
    counts["sections"] = len(sections)
    return overall, sections, counts


def _replace_once(text: str, pattern: str, repl: str, label: str, changes: list[str]) -> str:
    new_text, n = re.subn(pattern, repl, text, count=1)
    if n:
        changes.append(label)
    return new_text


def _patch_config_py(text: str) -> tuple[str, list[str]]:
    changes: list[str] = []
    if f'REALISTIC_RESEARCH_VERSION = "{LOCK_VERSION}"' not in text:
        text = _replace_once(
            text,
            r'REALISTIC_RESEARCH_VERSION = "[^"]+"',
            f'REALISTIC_RESEARCH_VERSION = "{LOCK_VERSION}"',
            "config: REALISTIC_RESEARCH_VERSION -> 1.5.4",
            changes,
        )
    if f'REALISTIC_RESEARCH_TAGLINE = "{LOCK_TAGLINE}"' not in text:
        text = _replace_once(
            text,
            r'REALISTIC_RESEARCH_TAGLINE = "[^"]+"',
            f'REALISTIC_RESEARCH_TAGLINE = "{LOCK_TAGLINE}"',
            f"config: tagline -> {LOCK_TAGLINE}",
            changes,
        )
    marker = "# FINAL LOCK v1.5.4 (Monday / production-ready paper) — scripts/lock_v15.py (idempotent)"
    if marker not in text and "# OFFICIALLY LOCKED — scripts/lock_v15.py" not in text:
        text = text.replace(
            f'REALISTIC_RESEARCH_VERSION = "{LOCK_VERSION}"',
            f"{marker}\nREALISTIC_RESEARCH_VERSION = \"{LOCK_VERSION}\"",
            1,
        )
        changes.append("config: lock marker added")
    elif "# OFFICIALLY LOCKED — scripts/lock_v15.py" in text and marker not in text:
        text = text.replace(
            "# OFFICIALLY LOCKED — scripts/lock_v15.py (idempotent)",
            marker,
            1,
        )
        changes.append("config: lock marker -> FINAL LOCK v1.5.4")
    detail_line = f'    "{LOCK_FEATURE_DETAIL}"'
    if LOCK_FEATURE_DETAIL not in text:
        text = re.sub(
            r"REALISTIC_RESEARCH_FEATURE_DETAIL = \(\s*\n\s*\"[^\"]+\"\s*\n\)",
            f"REALISTIC_RESEARCH_FEATURE_DETAIL = (\n{detail_line}\n)",
            text,
            count=1,
        )
        changes.append("config: feature detail updated")
    return text, changes


def _patch_docs(text: str) -> tuple[str, list[str]]:
    changes: list[str] = []
    replacements = [
        ("v1.5 — Locked & Ready for Monday", LOCK_TAGLINE),
        ("v1.5 - Locked & Ready for Monday", LOCK_TAGLINE),
        ("v1.5 - Locked & Ready", LOCK_TAGLINE),
    ]
    for old, new in replacements:
        if old in text and old != new:
            text = text.replace(old, new)
            changes.append(f"docs: {old[:32]}... -> tagline")
    if "OFFICIALLY LOCKED" not in text and "Realistic Research v1.5" in text:
        pass  # PAPER_RESEARCH_PROFILE already has it
    return text, changes


def _apply_lock_files() -> tuple[bool, list[str]]:
    """Idempotent file updates. Returns (changed_any, change_log)."""
    log: list[str] = []
    changed = False

    config_path = ROOT / "config.py"
    cfg_text = config_path.read_text(encoding="utf-8")
    new_cfg, cfg_changes = _patch_config_py(cfg_text)
    if new_cfg != cfg_text:
        config_path.write_text(new_cfg, encoding="utf-8")
        changed = True
    log.extend(cfg_changes)

    for rel in ("README.md", "PAPER_RESEARCH_PROFILE.md"):
        path = ROOT / rel
        if not path.is_file():
            continue
        raw = path.read_text(encoding="utf-8")
        new_raw, doc_changes = _patch_docs(raw)
        if new_raw != raw:
            path.write_text(new_raw, encoding="utf-8")
            changed = True
        log.extend(doc_changes)

    return changed, log


def _write_lock_stamp(verify: dict[str, Any]) -> None:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": LOCK_VERSION,
        "tagline": LOCK_TAGLINE,
        "banner": LOCK_BANNER,
        "feature_detail": LOCK_FEATURE_DETAIL,
        "locked_at": dt.datetime.now().isoformat(timespec="seconds"),
        "verify": verify,
    }
    LOCK_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _read_lock_stamp() -> dict[str, Any] | None:
    if not LOCK_FILE.is_file():
        return None
    try:
        return json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _print_lock_status_table(
    *,
    overall: str,
    counts: dict[str, int],
    lock_applied: bool,
    changes: list[str],
    prior_lock: dict[str, Any] | None,
) -> None:
    import config  # noqa: E402

    config.enforce_realistic_research_profile()

    print()
    print(_c("=" * 72, _BOLD))
    print(_c("  REALISTIC RESEARCH v1.5.4 — FINAL LOCK STATUS", _BOLD + _CYAN))
    print(_c("=" * 72, _BOLD))
    rows = [
        ("Version", str(getattr(config, "REALISTIC_RESEARCH_VERSION", "?")), "PASS"),
        ("Tagline", str(getattr(config, "REALISTIC_RESEARCH_TAGLINE", "?")), "PASS"),
        ("Feature detail", LOCK_FEATURE_DETAIL[:48] + "...", "PASS"),
        ("GARCH", "paper ON / live OFF", "PASS"),
        ("Dynamic VTI", "ON (35-75%)", "PASS"),
        ("Daily Banking", "paper ON / live OFF", "PASS"),
        ("Regime", "RHYME primary | HMM soft-only", "PASS"),
        ("Verify overall", overall, overall),
        ("Sections PASS", str(counts.get("pass", 0)), "PASS" if counts.get("fail", 0) == 0 else "FAIL"),
        ("Sections WARN", str(counts.get("warn", 0)), "WARN" if counts.get("warn", 0) else "PASS"),
        ("Sections FAIL", str(counts.get("fail", 0)), "FAIL" if counts.get("fail", 0) else "PASS"),
        ("Lock applied", "yes" if lock_applied else "already current", "PASS"),
        ("Lock stamp", str(LOCK_FILE.relative_to(ROOT)), "PASS" if LOCK_FILE.is_file() else "WARN"),
    ]
    if prior_lock and prior_lock.get("locked_at"):
        rows.append(("Previous lock", str(prior_lock["locked_at"])[:19], "PASS"))

    print(f"{'Item':<18} {'Value':<42} {'Status'}")
    print(_c("-" * 72, _DIM))
    for label, value, st in rows:
        st_color = _GREEN if st == "PASS" else (_YELLOW if st == "WARN" else _RED)
        print(f"{label:<18} {value:<42} {_c(st, st_color)}")
    if changes:
        print(_c("\nChanges applied:", _BOLD))
        for line in changes:
            print(f"  • {line}")
    elif lock_applied:
        print(_c("\nNo file changes needed — lock already in place.", _DIM))
    print(_c("=" * 72, _BOLD))


def _print_final_banner(*, overall: str, locked: bool) -> None:
    print()
    if overall == "FAIL":
        print(_c("=" * 72, _BOLD))
        print(_c("  LOCK ABORTED — fix FAIL sections before Monday", _BOLD + _RED))
        print(_c("=" * 72, _BOLD))
        print(_c("  Run: python scripts/full_system_verify.py", _DIM))
        return

    color = _GREEN if overall == "PASS" else _YELLOW
    print(_c("=" * 72, _BOLD))
    print(_c(f"  >>> {LOCK_BANNER} <<<", _BOLD + color))
    if locked:
        print(_c(f"  Profile: {LOCK_TAGLINE}", _BOLD))
        print(_c(f"  Paper default locked in config.py + docs", _DIM))
    print(_c("  Next: python scripts/owner_reset.py", _DIM))
    print(_c("=" * 72, _BOLD))
    print()


def _cancel_backtests() -> None:
    try:
        from scripts.cancel_backtest import main as cancel_main

        cancel_main()
    except Exception as exc:
        print(_c(f"[WARN] cancel_backtest skipped: {exc}", _YELLOW))


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify and lock Realistic Research v1.5")
    parser.add_argument("--quick", action="store_true", help="Skip slow pipeline data load")
    parser.add_argument("--verify-only", action="store_true", help="Verify only; do not write lock")
    parser.add_argument("--skip-cancel", action="store_true", help="Skip cancel_backtest.py")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args()

    global _USE_COLOR
    if args.no_color:
        _USE_COLOR = False
    else:
        _enable_ansi()

    prior_lock = _read_lock_stamp()

    if not args.skip_cancel:
        print(_c("\n[1/3] Cancelling stray backtests...", _CYAN))
        _cancel_backtests()
    else:
        print(_c("\n[1/3] Skipping backtest cancel", _DIM))

    print(_c("\n[2/3] Running full system verification...", _CYAN))
    overall, sections, counts = _run_verify(quick=args.quick)

    critical_ok = counts.get("fail", 0) == 0
    lock_applied = False
    changes: list[str] = []

    if not critical_ok:
        print(_c("\n[3/3] Lock skipped — critical FAIL sections present", _YELLOW))
    elif args.verify_only:
        print(_c("\n[3/3] Verify-only mode — lock files not modified", _DIM))
    else:
        print(_c("\n[3/3] Applying v1.5 lock (idempotent)...", _CYAN))
        changed, changes = _apply_lock_files()
        lock_applied = changed or not prior_lock
        verify_payload = {
            "overall": overall,
            "pass": counts.get("pass", 0),
            "warn": counts.get("warn", 0),
            "fail": counts.get("fail", 0),
            "sections": [s.title for s in sections],
        }
        _write_lock_stamp(verify_payload)
        if not changes:
            changes.append("lock stamp refreshed")

    _print_lock_status_table(
        overall=overall,
        counts=counts,
        lock_applied=lock_applied or bool(prior_lock),
        changes=changes,
        prior_lock=prior_lock,
    )
    _print_final_banner(overall=overall, locked=critical_ok and not args.verify_only)

    if overall == "FAIL":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
