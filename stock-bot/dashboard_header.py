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


def _paper_research_tape(vti_label: str | None = None) -> str:
    parts = [
        f"PAPER RESEARCH v{_research_version()}",
        "NYSE SLEEVE",
        "THINKING ON",
        "JOURNAL = FILL",
    ]
    label = (vti_label or "").strip()
    if label:
        parts.append(label)
    return " · ".join(parts)


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
    nyse = _float(exp.get("nyse_value"))
    crypto = _float(exp.get("crypto_value"))
    metal = _float(exp.get("metal_value"))
    invested = vti + nyse + crypto + metal
    if invested <= 0:
        return False
    if vti > 0:
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
    vti_target_frac: float | None = None,
    override_vti_target: bool = False,
) -> list[dict[str, Any]]:
    """Display-layout sleeve mix: actual vs target for VTI, NYSE, cash.

    When ``override_vti_target`` is set, the VTI target is the book's
    effective config (``None`` means unknown). Heartbeat ``sleeve_caps``
    are not used for that target.
    """
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
    if override_vti_target:
        vti_tgt = None if vti_target_frac is None else float(vti_target_frac) * 100.0
    else:
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


def _book_strategy_tape(
    *,
    paper: bool,
    book_id: str | None = None,
    vti_label: str | None = None,
) -> str:
    """One strategy line for the cyan bar. VTI text is the effective label."""
    bid = (book_id or "").strip().lower()
    vti = (vti_label or "").strip()

    def _line(parts: list[str]) -> str:
        if vti:
            parts.append(vti)
        return " · ".join(parts)

    if not paper:
        return _line(["LIVE PROFILE A", "NYSE ACTIVE", "REAL MONEY"])
    try:
        import config

        if bid == "alpaca_paper_v2" or (
            not bid and config.paper_medium_strategy_enabled()
        ):
            return _line(
                [
                    "MEDIUM SoT (alpaca_paper_v2)",
                    "15 NAMES · 30D HOLD · ATR 2.0×",
                    f"RESEARCH v{_research_version()}",
                ]
            )
        if bid == "alpaca_paper":
            return _line(
                [
                    "PAPER LAB (alpaca_paper)",
                    "8 NAMES ≤15%",
                    "IDLE-CASH ADD ≤30% IF FULL",
                    "30D HOLD · TRAIL HIGH @ +10%",
                    "HALF @ +20%",
                    f"RESEARCH v{_research_version()}",
                ]
            )
    except Exception:
        pass
    return _paper_research_tape(vti)


def header_tape_text(
    *,
    paper: bool,
    heartbeat: dict[str, Any] | None = None,
    book_id: str | None = None,
    vti_label: str | None = None,
) -> str:
    """One cyan-bar line: selected book and its strategy.

    ``heartbeat`` is ignored. VTI is included only when ``vti_label`` is the
    book's effective config, so a stale sleeve cap cannot contradict it.
    """
    _ = heartbeat
    tape = _book_strategy_tape(paper=paper, book_id=book_id, vti_label=vti_label)
    if _NYSE_100_RE.search(tape):
        return _book_strategy_tape(paper=paper, book_id=None, vti_label=vti_label)
    return tape
