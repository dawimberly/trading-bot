"""Desktop monitor header chrome — look only.

Live stamp is the book name (LIVE), never a sleeve slogan such as NYSE 100%
or VTI 85%. Tape uses heartbeat holdings when present.
"""

from __future__ import annotations

from typing import Any


def header_kicker_text(*, paper: bool) -> str:
    """Cyan kicker above the wordmark — book, not a sleeve mix."""
    return "PYTHONTRADING  ·  PAPER" if paper else "PYTHONTRADING  ·  LIVE"


def header_stamp_text(*, paper: bool) -> str:
    """Book identity next to Stock-bot. Never a sleeve slogan."""
    return "  PAPER  " if paper else "  LIVE  "


def _research_version() -> str:
    try:
        import config

        return str(getattr(config, "REALISTIC_RESEARCH_VERSION", "1.5.4") or "1.5.4")
    except Exception:
        return "1.5.4"


def header_holdings_bits(heartbeat: dict[str, Any] | None) -> list[str]:
    """Actual sleeve weights from the last heartbeat, when equity is known."""
    hb = heartbeat or {}
    exp = hb.get("sleeve_exposure") if isinstance(hb.get("sleeve_exposure"), dict) else {}
    try:
        equity = float(exp.get("equity") or hb.get("equity") or 0)
    except (TypeError, ValueError):
        equity = 0.0
    if equity <= 0:
        return []
    bits: list[str] = []
    for label, key in (
        ("VTI", "vti_core_value"),
        ("SPY", "spy_value"),
        ("NYSE", "nyse_value"),
        ("Crypto", "crypto_value"),
        ("Metal", "metal_value"),
    ):
        try:
            val = float(exp.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if val > 0:
            bits.append(f"{label} {100.0 * val / equity:.0f}%")
    try:
        cash = float(hb.get("cash") or 0)
    except (TypeError, ValueError):
        cash = 0.0
    if cash > 0:
        bits.append(f"CASH {100.0 * cash / equity:.0f}%")
    return bits


def header_tape_text(*, paper: bool, heartbeat: dict[str, Any] | None = None) -> str:
    """Cyan tape under the header. Prefer live holdings; never claim a fake mix."""
    holdings = header_holdings_bits(heartbeat)
    if paper:
        head = f"PAPER RESEARCH v{_research_version()}"
        if holdings:
            return f"{head}   ·   " + "   ·   ".join(holdings)
        return (
            f"{head}   ·   SMART DYNAMIC VTI 40–75%   ·   "
            "NYSE MOMENTUM   ·   THINKING ON   ·   JOURNAL = FILL"
        )
    if holdings:
        return "LIVE HOLDINGS   ·   " + "   ·   ".join(holdings)
    return "LIVE   ·   REAL MONEY   ·   HOLDINGS FROM HEARTBEAT   ·   CRYPTO OFF"
