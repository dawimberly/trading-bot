"""
Kraken autopilot: cleanup (A), crypto mirror (B), paper-bot mirror (C).
Uses wisdom regime, game-plan stress/yield gate, and crypto vol gate.
"""

from __future__ import annotations

from typing import Any

import config
from modules import kraken_budget
from modules.kraken_cleanup import build_cleanup_intents, execute_cleanup_intents
from modules.kraken_rebalance import run_kraken_rebalance
from modules.kraken_pairs import kraken_pair_for_symbol
from modules.kraken_advisor import fetch_kraken_balances
from modules.kraken_execute import execute_kraken_trade
from modules.kraken_spot import kraken_configured, trading_allowed
from modules.pipeline_strategies import (
    PAUSED_REGIMES,
    crypto_trade_intents,
    nyse_mirror_intent,
    spy_mirror_intent,
)

# Re-export balance helper — defined below in this file if missing from kraken_spot


def _entries_blocked(wisdom: dict, regime: str) -> bool:
    if wisdom.get("wisdom_paused"):
        return True
    return regime in PAUSED_REGIMES


def _wisdom_stress(wisdom: dict, gp_signals: dict) -> bool:
    if gp_signals.get("stress"):
        return True
    if wisdom.get("governor_stress"):
        return True
    regime = wisdom.get("regime", "")
    return regime in PAUSED_REGIMES


def _mirror_notional(default: float) -> float:
    return min(float(default), float(config.KRAKEN_MAX_ORDER_USD))


def fetch_kraken_balances_for_sell(symbol: str) -> dict:
    """Return {volume} for base asset on Kraken."""
    bal = fetch_kraken_balances()
    if not bal.get("ok"):
        return {"volume": None, "api_ok": False}
    target = symbol.upper().replace("-USD", "")
    for b in bal.get("balances") or []:
        disp = b.get("display", "").upper().replace(".EQ", "")
        if disp == target or disp == target.replace("XBT", "BTC"):
            return {"volume": float(b.get("amount", 0))}
    return {"volume": None}


def _execute_buy_intent(intent: dict) -> dict:
    usd = _mirror_notional(intent.get("notional") or config.KRAKEN_CRYPTO_NOTIONAL)
    trade = {
        "symbol": intent.get("symbol", ""),
        "side": "buy",
        "usd": usd,
        "volume": None,
    }
    out = execute_kraken_trade(trade)
    out["intent"] = intent
    return out


def _execute_sell_intent(intent: dict) -> dict:
    sym = intent.get("symbol", "")
    vol = intent.get("volume")
    if vol is None:
        bal = fetch_kraken_balances_for_sell(sym)
        vol = bal.get("volume")
    trade = {
        "symbol": sym,
        "side": "sell",
        "usd": intent.get("notional") or config.KRAKEN_MAX_ORDER_USD,
        "volume": vol,
    }
    out = execute_kraken_trade(trade)
    out["intent"] = intent
    return out


def mirror_game_plan_actions(
    gp_result: dict,
    *,
    yield_gated: bool,
    entries_blocked: bool,
) -> list[dict]:
    """Mirror Alpaca game-plan actions (metals, stress trim) where Kraken has a pair."""
    results = []
    for action in gp_result.get("actions") or []:
        sym = action.get("symbol", "")
        phase = action.get("phase", "")
        if not sym:
            continue
        if kraken_pair_for_symbol(sym) is None:
            results.append(
                {"ok": False, "skipped": sym, "reason": "no Kraken pair", "phase": phase}
            )
            continue
        if phase in ("sell", "exit_metal"):
            if entries_blocked and phase != "exit_metal":
                results.append({"ok": False, "skipped": sym, "reason": "entries blocked"})
                continue
            intent = {
                "symbol": sym,
                "side": "sell",
                "phase": f"mirror_{phase}",
                "notional": action.get("notional"),
            }
            results.append(_execute_sell_intent(intent))
        elif phase == "buy_metal":
            if entries_blocked or yield_gated:
                results.append({"ok": False, "skipped": sym, "reason": "gate"})
                continue
            notional = action.get("notional") or config.KRAKEN_MAX_ORDER_USD
            intent = {
                "symbol": sym,
                "side": "buy",
                "phase": "mirror_buy_metal",
                "notional": _mirror_notional(notional),
            }
            results.append(_execute_buy_intent(intent))
    return results


def run_kraken_autopilot(
    *,
    wisdom: dict,
    gp_signals: dict,
    gp_result: dict,
    crypto_gate: dict,
    data,
    regime: str,
    now,
    pair_cooldown: dict,
    market_open: bool,
) -> dict[str, Any]:
    """
    Run enabled autopilot modes. Returns summary for logging / heartbeat.
    """
    summary: dict[str, Any] = {
        "enabled": config.KRAKEN_AUTOPILOT_ENABLED,
        "dry_run": config.KRAKEN_DRY_RUN,
        "live": trading_allowed(),
        "rebalance": {},
        "cleanup": [],
        "crypto_mirror": [],
        "paper_mirror": [],
        "skipped": [],
    }

    kraken_budget.reset_cycle_budget()

    if not config.KRAKEN_AUTOPILOT_ENABLED:
        summary["skipped"].append("KRAKEN_AUTOPILOT_ENABLED=false")
        return summary
    if not kraken_configured():
        summary["skipped"].append("Kraken API keys missing")
        return summary
    if not config.ALLOW_KRAKEN_TRADING and not config.KRAKEN_DRY_RUN:
        summary["skipped"].append("Set ALLOW_KRAKEN_TRADING=yes or KRAKEN_DRY_RUN=true")
        return summary

    entries_blocked = _entries_blocked(wisdom, regime)
    stress = _wisdom_stress(wisdom, gp_signals)
    yield_gated = bool(gp_signals.get("yield_gate"))
    wisdom_mode = wisdom.get("wisdom_mode", "baseline")

    # --- Rebalance: target weights from reference/kraken_targets.json ---
    if config.KRAKEN_REBALANCE_ENABLED:
        summary["rebalance"] = run_kraken_rebalance(
            stress=stress,
            crypto_allowed=bool(crypto_gate.get("allowed")),
            entries_blocked=entries_blocked,
        )
    else:
        summary["skipped"].append("rebalance off")

    # --- A: Cleanup (playbook rules; independent of rebalance) ---
    if config.KRAKEN_AUTOPILOT_CLEANUP:
        intents = build_cleanup_intents(
            max_actions=config.KRAKEN_CLEANUP_MAX_ACTIONS,
            wisdom_stress=stress,
        )
        summary["cleanup"] = execute_cleanup_intents(intents)
    else:
        summary["skipped"].append("cleanup off")

    # --- B: Crypto mirror (same Z-score intents as paper bot) ---
    if config.KRAKEN_AUTOPILOT_CRYPTO_MIRROR:
        if entries_blocked:
            summary["skipped"].append("crypto mirror: wisdom/regime pause")
        elif not crypto_gate.get("allowed"):
            summary["skipped"].append(
                f"crypto mirror: {crypto_gate.get('reason', 'blocked')}"
            )
        else:
            notional = _mirror_notional(config.KRAKEN_CRYPTO_NOTIONAL)
            intents = crypto_trade_intents(
                data,
                regime,
                now,
                pair_cooldown,
                volatility=wisdom.get("volatility"),
                notional=notional,
            )
            for intent in intents:
                if intent["side"] == "buy":
                    summary["crypto_mirror"].append(_execute_buy_intent(intent))
                else:
                    summary["crypto_mirror"].append(_execute_sell_intent(intent))
    else:
        summary["skipped"].append("crypto mirror off")

    # --- C: Mirror paper bot (game plan + SPY + NYSE when session open) ---
    if config.KRAKEN_AUTOPILOT_MIRROR:
        if config.GAME_PLAN_ENABLED and gp_result.get("enabled"):
            summary["paper_mirror"].extend(
                mirror_game_plan_actions(
                    gp_result,
                    yield_gated=yield_gated,
                    entries_blocked=entries_blocked,
                )
            )

        if market_open and not entries_blocked:
            spy_sym = config.SPY_BOT_SYMBOL
            if yield_gated and spy_sym in ("SPY", "QQQ", "VOO"):
                summary["skipped"].append("spy mirror: yield gate")
            else:
                spy_intent = spy_mirror_intent(
                    data,
                    regime,
                    now,
                    pair_cooldown,
                    yield_gated=yield_gated,
                )
                if spy_intent and kraken_pair_for_symbol(spy_intent["symbol"]):
                    spy_intent["notional"] = _mirror_notional(config.KRAKEN_MAX_ORDER_USD)
                    summary["paper_mirror"].append(_execute_buy_intent(spy_intent))

            nyse_intent = nyse_mirror_intent(
                data, regime, now, pair_cooldown, yield_gated=yield_gated
            )
            if nyse_intent and kraken_pair_for_symbol(nyse_intent["symbol"]):
                nyse_intent["notional"] = _mirror_notional(config.KRAKEN_MAX_ORDER_USD)
                summary["paper_mirror"].append(_execute_buy_intent(nyse_intent))
        elif entries_blocked:
            summary["skipped"].append("paper mirror buys: wisdom pause")
    else:
        summary["skipped"].append("paper mirror off")

    summary["wisdom_mode"] = wisdom_mode
    summary["wisdom_paused"] = wisdom.get("wisdom_paused")
    summary["regime"] = regime
    summary["stress"] = stress
    if kraken_budget.cycle_budget_usd() > 0:
        summary["cycle_buy_budget_usd"] = kraken_budget.cycle_budget_usd()
        summary["cycle_buy_spent_usd"] = kraken_budget.cycle_buy_spent()
    return summary


def format_autopilot_line(summary: dict) -> str:
    rb = summary.get("rebalance") or {}
    n_rb = len([x for x in rb.get("executed", []) if x.get("ok")])
    n_rb_plan = rb.get("plan_trades", 0)
    profile = rb.get("profile", "")
    n_clean = len([x for x in summary.get("cleanup", []) if x.get("ok")])
    n_manual = len([x for x in summary.get("cleanup", []) if x.get("manual")])
    n_app = len(rb.get("needs_app") or [])
    n_crypto = len([x for x in summary.get("crypto_mirror", []) if x.get("ok")])
    n_mirror = len([x for x in summary.get("paper_mirror", []) if x.get("ok")])
    mode = "DRY-RUN" if summary.get("dry_run") else ("LIVE" if summary.get("live") else "off")
    pause = " PAUSED" if summary.get("wisdom_paused") else ""
    rb_s = f" rebalance={n_rb}/{n_rb_plan}({profile})" if rb else ""
    budget_s = ""
    if summary.get("cycle_buy_budget_usd"):
        budget_s = (
            f" buy_budget=${summary.get('cycle_buy_spent_usd', 0):.0f}/"
            f"${summary['cycle_buy_budget_usd']:.0f}"
        )
    return (
        f"Kraken autopilot [{mode}]:{rb_s} cleanup={n_clean} stocks_pending={n_app} "
        f"crypto={n_crypto} mirror={n_mirror}{budget_s} "
        f"wisdom={summary.get('wisdom_mode')}{pause}"
    )
