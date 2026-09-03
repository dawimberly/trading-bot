"""Assert / banner for alpaca_paper_v2 locked strategy (33% VTI / 67% NYSE)."""

from __future__ import annotations

import os

import config


def _is_portal_paper_v2() -> bool:
    """Only the alpaca_paper_v2 book — never VFIFX / other portal books."""
    if not config.PAPER_TRADING:
        return False
    book = (os.getenv("PYTHONTRADING_ENV_FILE") or "").replace("\\", "/")
    return "alpaca_paper_v2" in book


def paper_v2_strategy_snapshot() -> dict:
    """Current effective knobs that must match the 33/67 lock."""
    nyse_cap = float(config.effective_nyse_sleeve_cap_pct())
    return {
        "vti_enabled": bool(config.vti_core_enabled()),
        "vti_pct": float(config.effective_vti_core_pct()),
        "paper_vti_pct": float(config.PAPER_VTI_CORE_PCT),
        "dynamic_vti": bool(config.PAPER_DYNAMIC_VTI_ENABLED),
        "nyse_cap": nyse_cap,
        "nyse_max_exposure": float(config.PAPER_NYSE_MAX_EXPOSURE_PCT),
        "max_active": int(config.effective_max_active_tickers()),
        "rebalance": bool(config.REBALANCE_ENABLED),
        "yield_gate_override": bool(config.PAPER_YIELD_GATE_OVERRIDE),
        "momentum_quality": bool(config.effective_paper_momentum_quality_fixes()),
        "spy_cap": float(config.SPY_SLEEVE_CAP_PCT),
        "stat_arb": bool(config.effective_stat_arb_enabled()),
        "crypto": bool(config.effective_crypto_enabled()),
        "options": bool(config.effective_options_sleeve_enabled()),
        "vol_trading": bool(config.effective_vol_trading_enabled()),
        "per_name": float(config.PAPER_NYSE_PER_NAME_MAX_PCT),
        # Diverting Realistic Research sleeves (must stay OFF for pure 33/67)
        "orb": bool(config.effective_orb_momentum_enabled()),
        "sector_rotation": bool(config.effective_sector_rotation_enabled()),
        "vol_breakout": bool(config.effective_vol_breakout_enabled()),
        "felix_social": bool(config.effective_felix_social_dynamic_enabled()),
        "social_sleeve": bool(config.PAPER_SOCIAL_SLEEVE_ENABLED),
        "protective_short": bool(config.effective_protective_short_enabled()),
        "dynamic_core": bool(config.effective_dynamic_core_enabled()),
        "international": bool(config.effective_international_sleeve_enabled()),
        "active_boost": float(config.PAPER_ACTIVE_SLEEVE_BOOST),
    }


def validate_paper_v2_strategy() -> list[str]:
    """Return human-readable violations (empty = OK)."""
    if not _is_portal_paper_v2():
        return []
    s = paper_v2_strategy_snapshot()
    errs: list[str] = []
    if not s["vti_enabled"]:
        errs.append("VTI core disabled")
    if abs(s["vti_pct"] - 0.33) > 0.005:
        errs.append(f"VTI target {s['vti_pct']:.0%} != 33%")
    if s["dynamic_vti"]:
        errs.append("Dynamic VTI ON (must be fixed 33%)")
    if s["nyse_cap"] < 0.65 or s["nyse_cap"] > 0.68:
        errs.append(f"NYSE cap {s['nyse_cap']:.0%} not ~67%")
    if s["max_active"] != 10:
        errs.append(f"MAX_ACTIVE_TICKERS={s['max_active']} != 10")
    if s["rebalance"]:
        errs.append("REBALANCE_ENABLED=true (want VTI drift only)")
    if not s["yield_gate_override"]:
        errs.append("PAPER_YIELD_GATE_OVERRIDE=false")
    if not s["momentum_quality"]:
        errs.append("PAPER_MOMENTUM_QUALITY_FIXES=false")
    if s["spy_cap"] > 1e-9:
        errs.append(f"SPY sleeve cap {s['spy_cap']:.0%} (want 0)")
    if s["stat_arb"]:
        errs.append("stat arb ON")
    if s["crypto"]:
        errs.append("crypto sleeve ON")
    if s["options"]:
        errs.append("options sleeve ON")
    if s["vol_trading"]:
        errs.append("vol trading sleeve ON")
    if abs(s["per_name"] - 0.10) > 0.015:
        errs.append(f"per-name max {s['per_name']:.0%} != 10%")
    if s["orb"]:
        errs.append("ORB momentum ON (steals NYSE slots)")
    if s["sector_rotation"]:
        errs.append("sector rotation ON (steals NYSE cash/slots)")
    if s["vol_breakout"]:
        errs.append("vol breakout ON (steals NYSE cash/slots)")
    if s["felix_social"]:
        errs.append("Felix social-dynamic ON")
    if s["social_sleeve"]:
        errs.append("social sleeve ON")
    if s["protective_short"]:
        errs.append("protective shorts ON")
    if s["dynamic_core"]:
        errs.append("DYNAMIC_CORE ON (must stay fixed VTI 33%)")
    if s["international"]:
        errs.append("international sleeve ON")
    if abs(s["active_boost"] - 1.0) > 0.05:
        errs.append(f"PAPER_ACTIVE_SLEEVE_BOOST={s['active_boost']:.2f} != 1.0")
    return errs


def print_paper_v2_strategy_lock() -> None:
    """Startup banner + hard fail if portal v2 knobs drift."""
    if not _is_portal_paper_v2():
        return
    s = paper_v2_strategy_snapshot()
    print(
        f"--- STRATEGY LOCK (alpaca_paper_v2): "
        f"VTI {s['vti_pct']:.0%} fixed | NYSE {s['nyse_cap']:.0%} | "
        f"{s['max_active']} names | pure momentum "
        f"(SPY/crypto/stat-arb/options/vol/ORB/sector/vol-BO/social/shorts OFF) | "
        f"quality fixes ON | rebalance OFF ---"
    )
    errs = validate_paper_v2_strategy()
    if not errs:
        print("--- STRATEGY LOCK: OK ---")
        return
    msg = "; ".join(errs)
    print(f"--- STRATEGY LOCK FAILED: {msg} ---")
    raise RuntimeError(f"alpaca_paper_v2 strategy lock failed: {msg}")
