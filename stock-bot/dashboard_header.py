"""Desktop monitor header chrome — look only.

Stamp is the book name (LIVE / PAPER), never a sleeve slogan such as NYSE 100%
or VTI 85%. Tape may show real mixed holdings, but never the leftover
NYSE-only 100% experiment — even if heartbeat sleeves look NYSE-only.
"""

from __future__ import annotations

import re
from typing import Any

_NYSE_100_RE = re.compile(r"\bNYSE\s*100%?\b", re.IGNORECASE)

# Paper is Realistic Research with Smart Dynamic VTI 40–75%, not NYSE-only.
_NYSE_ONLY_SHARE = 0.90


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


def _paper_research_tape() -> str:
    return (
        f"PAPER RESEARCH v{_research_version()}   ·   "
        "SMART DYNAMIC VTI 40–75%   ·   "
        "NYSE SLEEVE   ·   THINKING ON   ·   JOURNAL = FILL"
    )


def _live_fallback_tape() -> str:
    return "LIVE   ·   REAL MONEY   ·   HOLDINGS FROM HEARTBEAT   ·   CRYPTO OFF"


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _exposure(heartbeat: dict[str, Any] | None) -> tuple[dict[str, Any], float]:
    hb = heartbeat or {}
    exp = hb.get("sleeve_exposure") if isinstance(hb.get("sleeve_exposure"), dict) else {}
    equity = _float(exp.get("equity") or hb.get("equity"))
    return exp, equity


def header_holdings_bits(heartbeat: dict[str, Any] | None) -> list[str]:
    """Actual sleeve weights from the last heartbeat, when equity is known."""
    exp, equity = _exposure(heartbeat)
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
        val = _float(exp.get(key))
        if val > 0:
            bits.append(f"{label} {100.0 * val / equity:.0f}%")
    cash = _float((heartbeat or {}).get("cash"))
    if cash > 0:
        bits.append(f"CASH {100.0 * cash / equity:.0f}%")
    return [bit for bit in bits if not _NYSE_100_RE.search(bit)]


def holdings_are_nyse_only_slogan(heartbeat: dict[str, Any] | None) -> bool:
    """True when reported sleeves are ~all NYSE — leftover chrome, not book identity."""
    exp, equity = _exposure(heartbeat)
    if equity <= 0:
        return False
    vti = _float(exp.get("vti_core_value"))
    spy = _float(exp.get("spy_value"))
    nyse = _float(exp.get("nyse_value"))
    crypto = _float(exp.get("crypto_value"))
    metal = _float(exp.get("metal_value"))
    invested = vti + spy + nyse + crypto + metal
    if invested <= 0:
        return False
    if vti > 0 or spy > 0:
        return False
    return (nyse / invested) >= _NYSE_ONLY_SHARE


def _usable_holdings_bits(heartbeat: dict[str, Any] | None) -> list[str]:
    if holdings_are_nyse_only_slogan(heartbeat):
        return []
    bits = header_holdings_bits(heartbeat)
    sleeve_bits = [bit for bit in bits if not bit.startswith("CASH ")]
    if not sleeve_bits:
        return []
    return bits


def sleeve_mix_rows(
    heartbeat: dict[str, Any] | None,
    *,
    equity: float,
    cash: float,
) -> list[dict[str, Any]]:
    """Display-layout sleeve mix: actual vs target for VTI, NYSE, cash."""
    exp, eq_hb = _exposure(heartbeat)
    eq = equity if equity > 0 else eq_hb
    caps = (heartbeat or {}).get("sleeve_caps") if isinstance(
        (heartbeat or {}).get("sleeve_caps"), dict
    ) else {}
    cash_val = cash if cash > 0 else _float((heartbeat or {}).get("cash"))
    if eq <= 0:
        return []

    def _pct(value: float) -> float:
        return 100.0 * value / eq

    vti_val = _float(exp.get("vti_core_value"))
    nyse_val = _float(exp.get("nyse_value"))
    vti_tgt = _float(caps.get("vti_core")) * 100.0
    nyse_tgt = _float(caps.get("nyse")) * 100.0
    cash_tgt = _float(caps.get("cash")) * 100.0
    return [
        {
            "key": "vti",
            "label": "VTI actual / target",
            "actual_pct": _pct(vti_val),
            "target_pct": vti_tgt,
            "value": vti_val,
        },
        {
            "key": "nyse",
            "label": "NYSE actual / target",
            "actual_pct": _pct(nyse_val),
            "target_pct": nyse_tgt,
            "value": nyse_val,
        },
        {
            "key": "cash",
            "label": "Cash",
            "actual_pct": _pct(cash_val),
            "target_pct": cash_tgt,
            "value": cash_val,
        },
    ]


def header_tape_text(*, paper: bool, heartbeat: dict[str, Any] | None = None) -> str:
    """Cyan tape under the header. Never claim the NYSE-only 100% experiment."""
    holdings = _usable_holdings_bits(heartbeat)
    if paper:
        if holdings:
            tape = f"PAPER RESEARCH v{_research_version()}   ·   " + "   ·   ".join(holdings)
        else:
            tape = _paper_research_tape()
    elif holdings:
        tape = "LIVE HOLDINGS   ·   " + "   ·   ".join(holdings)
    else:
        tape = _live_fallback_tape()
    if _NYSE_100_RE.search(tape):
        return _paper_research_tape() if paper else _live_fallback_tape()
    return tape
