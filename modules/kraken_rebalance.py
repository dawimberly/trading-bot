"""Rebalance Kraken Pro toward reference/kraken_targets.json (wisdom + game plan)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import config
from modules.kraken_advisor import LEVERAGED_TICKERS
from modules.kraken_capabilities import probe_kraken_capabilities
from modules.kraken_execute import execute_kraken_trade
from modules.kraken_portfolio import build_portfolio_snapshot, holdings_by_ticker

TARGETS_FILE = Path(__file__).resolve().parents[1] / "reference" / "kraken_targets.json"
STATE_FILE = Path(__file__).resolve().parents[1] / "kraken_autopilot_state.json"
CRYPTO_SET = frozenset({"BTC", "ETH", "SOL", "RENDER", "ADA", "AVAX", "LINK", "USD"})


def _load_targets() -> dict:
    if not TARGETS_FILE.exists():
        return {}
    return json.loads(TARGETS_FILE.read_text(encoding="utf-8"))


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _target_weights(
    *,
    stress: bool,
    crypto_allowed: bool,
    targets_cfg: dict,
) -> dict[str, float]:
    """Ticker -> target fraction of total equity (0-1)."""
    profile_name = "stress" if stress else "calm"
    profile = (targets_cfg.get("profiles") or {}).get(profile_name) or {}
    weights: dict[str, float] = {}

    cash = float(profile.get("cash_pct", 0)) / 100.0
    weights["USD"] = cash

    if crypto_allowed:
        for sym, pct in (profile.get("crypto") or {}).items():
            weights[sym.upper()] = float(pct) / 100.0
    else:
        extra = sum(float(p) / 100.0 for p in (profile.get("crypto") or {}).values())
        weights["USD"] = weights.get("USD", 0) + extra

    for sym, pct in (profile.get("equities") or {}).items():
        weights[sym.upper()] = float(pct) / 100.0

    return weights


def build_rebalance_plan(
    *,
    stress: bool,
    crypto_allowed: bool,
    entries_blocked: bool,
) -> dict[str, Any]:
    """Return {ok, profile, total_usd, trades: [{symbol, side, usd, volume, phase, reason}]}."""
    targets_cfg = _load_targets()
    snap = build_portfolio_snapshot()
    if not snap.get("ok") or float(snap.get("total_usd") or 0) < config.MIN_NOTIONAL:
        return {"ok": False, "error": snap.get("error") or "portfolio too small", "trades": []}
    total = float(snap["total_usd"])
    current = holdings_by_ticker(snap)
    weights = _target_weights(
        stress=stress, crypto_allowed=crypto_allowed, targets_cfg=targets_cfg
    )
    banned = {t.upper() for t in targets_cfg.get("banned_tickers") or []} | LEVERAGED_TICKERS
    hold = {t.upper() for t in targets_cfg.get("hold_tickers") or []}

    trades: list[dict] = []
    min_usd = config.MIN_NOTIONAL

    # 1) Sell banned + not-in-target (crypto + equity)
    target_tickers = set(weights.keys())
    for ticker, pos in current.items():
        if ticker == "USD":
            continue
        usd = float(pos.get("usd") or 0)
        vol = pos.get("volume")
        if usd < min_usd and (vol is None or float(vol) < config.KRAKEN_DUST_VOLUME):
            continue
        if ticker in banned or ticker in LEVERAGED_TICKERS:
            trades.append(
                {
                    "symbol": ticker,
                    "side": "sell",
                    "usd": usd,
                    "volume": vol,
                    "phase": "rebalance_exit_banned",
                    "reason": "banned or leverage",
                }
            )
            continue
        if ticker not in target_tickers:
            if ticker in hold:
                continue
            trades.append(
                {
                    "symbol": ticker,
                    "side": "sell",
                    "usd": usd,
                    "volume": vol,
                    "phase": "rebalance_exit_unwanted",
                    "reason": f"not in {('stress' if stress else 'calm')} target",
                }
            )

    # 2) Trim overweight
    for ticker, wt in weights.items():
        if ticker == "USD":
            continue
        target_usd = total * wt
        pos = current.get(ticker) or {"usd": 0, "volume": None}
        cur_usd = float(pos.get("usd") or 0)
        excess = cur_usd - target_usd
        if excess >= min_usd:
            vol = pos.get("volume")
            sell_frac = min(1.0, excess / cur_usd) if cur_usd > 0 else 1.0
            sell_vol = float(vol) * sell_frac if vol is not None else None
            trades.append(
                {
                    "symbol": ticker,
                    "side": "sell",
                    "usd": round(excess, 2),
                    "volume": sell_vol,
                    "phase": "rebalance_trim",
                    "reason": f"over target ${target_usd:.0f}",
                }
            )

    # 3) Buys for underweight (if not entries blocked)
    if not entries_blocked:
        for ticker, wt in weights.items():
            if ticker == "USD":
                continue
            target_usd = total * wt
            pos = current.get(ticker) or {"usd": 0}
            cur_usd = float(pos.get("usd") or 0)
            need = target_usd - cur_usd
            if need >= min_usd:
                need = min(need, config.KRAKEN_MAX_ORDER_USD)
                trades.append(
                    {
                        "symbol": ticker,
                        "side": "buy",
                        "usd": round(need, 2),
                        "volume": None,
                        "phase": "rebalance_buy",
                        "reason": f"under target ${target_usd:.0f}",
                    }
                )

    def _trade_rank(t: dict) -> tuple:
        sym = t.get("symbol", "")
        is_crypto = sym in ("RENDER", "ETH", "BTC", "SOL", "ADA", "AVAX", "LINK")
        phase = t.get("phase", "")
        if is_crypto and t["side"] == "sell":
            return (0, -t.get("usd", 0))
        if phase == "rebalance_exit_banned":
            return (1, -t.get("usd", 0))
        if t["side"] == "sell":
            return (2, -t.get("usd", 0))
        return (3, -t.get("usd", 0))

    trades.sort(key=_trade_rank)

    profile = "stress" if stress else "calm"
    return {
        "ok": True,
        "profile": profile,
        "total_usd": total,
        "weights": {k: round(v * 100, 1) for k, v in weights.items()},
        "current": {k: round(float(v.get("usd") or 0), 2) for k, v in current.items()},
        "trades": trades[: config.KRAKEN_REBALANCE_MAX_TRADES],
    }


def execute_rebalance_plan(plan: dict) -> dict[str, Any]:
    """Execute all trades via API where permitted (crypto + xStocks). No manual Telegram."""
    if not plan.get("ok"):
        return {"executed": [], "needs_app": [], "error": plan.get("error")}

    state = _load_state()
    today = date.today().isoformat()
    cap = probe_kraken_capabilities()

    executed: list[dict] = []
    needs_app: list[dict] = []
    auto_n = 0
    max_n = config.KRAKEN_REBALANCE_MAX_TRADES

    for trade in plan.get("trades") or []:
        if auto_n >= max_n and not trade.get("side") == "sell":
            break
        out = execute_kraken_trade(trade, capabilities=cap)
        executed.append(out)
        if out.get("ok"):
            auto_n += 1
        elif out.get("needs_app"):
            needs_app.append(trade)

    state["last_rebalance_at"] = today
    state["last_rebalance_profile"] = plan.get("profile")
    state["last_rebalance_auto_count"] = auto_n
    state["capabilities"] = {
        "crypto_ok": cap.get("crypto_ok"),
        "xstock_ok": cap.get("xstock_ok"),
        "equity_spot_ok": cap.get("equity_spot_ok"),
    }
    _save_state(state)

    return {
        "executed": executed,
        "needs_app": needs_app,
        "profile": plan.get("profile"),
        "total_usd": plan.get("total_usd"),
        "weights": plan.get("weights"),
        "capabilities": state["capabilities"],
    }


def run_kraken_rebalance(
    *,
    stress: bool,
    crypto_allowed: bool,
    entries_blocked: bool,
) -> dict[str, Any]:
    plan = build_rebalance_plan(
        stress=stress,
        crypto_allowed=crypto_allowed,
        entries_blocked=entries_blocked,
    )
    result = execute_rebalance_plan(plan)
    result["plan_trades"] = len(plan.get("trades") or [])
    return result
