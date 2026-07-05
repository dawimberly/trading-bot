#!/usr/bin/env python3
"""Verify insider monitor, boosts, risk guard, and dashboard integration."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402


def _ok(label: str, detail: str = "") -> None:
    suffix = f" — {detail}" if detail else ""
    print(f"  [OK] {label}{suffix}")


def _fail(label: str, detail: str = "") -> None:
    suffix = f" — {detail}" if detail else ""
    print(f"  [FAIL] {label}{suffix}")


def _warn(label: str, detail: str = "") -> None:
    suffix = f" — {detail}" if detail else ""
    print(f"  [WARN] {label}{suffix}")


def main() -> int:
    print("=== Insider Integration Verification ===\n")
    failures = 0

    # Config / monitor
    print("Monitor")
    if config.effective_insider_monitor_enabled():
        _ok("Insider monitor enabled (paper/research)")
    else:
        _fail("Insider monitor disabled", "expected on for paper chase")
        failures += 1

    from modules.insider_monitor import (  # noqa: E402
        _sig_type,
        get_recent_insider_signals,
        format_insider_monitor_banner,
    )

    signals = get_recent_insider_signals(days=7, min_score=60)
    if signals:
        _ok(f"Signals (min_score=60)", f"{len(signals)} returned")
    else:
        _warn("No signals with min_score=60", "SEC cache may be empty or filtered")

    print("\nTop signals (up to 5)")
    if not signals:
        print("  (none)")
    for sig in signals[:5]:
        tk = sig.get("ticker") or sig.get("company") or "?"
        print(
            f"  {tk:6} {_sig_type(sig):16} score={sig.get('score', 0):3}  "
            f"{str(sig.get('description') or '')[:60]}"
        )

    # Boosts
    print("\nBoosts")
    if config.effective_insider_signal_boost_enabled():
        _ok("Insider signal boost enabled")
    else:
        _fail("Insider signal boost disabled")
        failures += 1

    from modules.insider_signal_handler import (  # noqa: E402
        apply_insider_signals_to_strategies,
        get_boost_snapshot,
    )

    state = apply_insider_signals_to_strategies()
    snap = get_boost_snapshot()
    mom = snap.get("momentum_boosts") or {}
    sa = snap.get("stat_arb_boosts") or {}
    shorts = snap.get("short_boosts") or {}
    if snap.get("enabled"):
        _ok("apply_insider_signals_to_strategies()", snap.get("summary", "")[:80])
    else:
        _fail("Boost handler returned disabled")
        failures += 1

    print("\nApplied boosts")
    if mom:
        for sym, val in sorted(mom.items(), key=lambda x: -x[1])[:5]:
            print(f"  momentum  {sym}: +{val:.3f}")
    else:
        print("  momentum: (none active)")
    if sa:
        for sym, val in sorted(sa.items(), key=lambda x: -x[1])[:5]:
            if val > 1.0:
                print(f"  stat_arb  {sym}: x{val:.3f}")
    else:
        print("  stat_arb: (none active)")
    if shorts:
        for sym, meta in shorts.items():
            print(f"  short     {sym}: base={meta.get('base', 0):.3f} role={meta.get('role')}")
    else:
        print("  short: (none active)")

    # Risk guard
    print("\nRisk guard")
    if config.effective_insider_risk_guard_enabled():
        _ok("Risk guard enabled")
    else:
        _warn("Risk guard disabled")
    guard_notes = snap.get("risk_guard_notes") or []
    bubble = snap.get("bubble_score_100")
    if bubble is not None:
        print(f"  bubble_score_100: {bubble}")
    if guard_notes:
        for note in guard_notes:
            print(f"  guard: {note}")
    else:
        print("  guard: no activations this cycle (normal if bubble <= 85)")

    test_guard = apply_insider_signals_to_strategies(
        bubble_score_100=90.0,
        regime="RHYME_B: Panic_Volatility",
    )
    if test_guard.get("risk_guard_notes"):
        _ok("Risk guard fires on bubble>85", "; ".join(test_guard["risk_guard_notes"][:2]))
    else:
        _fail("Risk guard test (bubble=90) did not activate")
        failures += 1
    apply_insider_signals_to_strategies()

    # Dashboard
    print("\nDashboard panel")
    dash = ROOT / "dashboard_app.py"
    if dash.is_file():
        text = dash.read_text(encoding="utf-8", errors="replace")
        checks = [
            ("_insider_section", "Insider section widget"),
            ("_insider_table", "Insider DataTable"),
            ("_fetch_insider_signals_snapshot", "Refresh snapshot hook"),
            ("_fill_insider_signals", "UI fill handler"),
            ("REFRESH_SECONDS = 45", "45s auto-refresh"),
        ]
        for needle, label in checks:
            if needle in text:
                _ok(label)
            else:
                _fail(label, f"missing {needle}")
                failures += 1
    else:
        _fail("dashboard_app.py not found")
        failures += 1

    banner = format_insider_monitor_banner()
    if banner:
        print(f"\nBanner: {banner}")

    print("\n=== Summary ===")
    if failures:
        print(f"FAILED — {failures} check(s) need attention")
        return 1
    print("All checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
