"""One-shot Kraken advice (rebalance plan + cleanup + crypto intents). No orders."""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
from modules.crypto_vol_gate import crypto_trading_allowed
from modules.data_loader import load_close_matrix
from modules.kraken_cleanup import build_cleanup_intents
from modules.kraken_rebalance import build_rebalance_plan
from modules.macro_signals import evaluate, load_daily_matrix
from modules.pipeline_strategies import PAUSED_REGIMES, crypto_trade_intents
from modules.wisdom_sentiment import resolve_wisdom_regime


def main() -> None:
    data = load_close_matrix()
    wisdom = resolve_wisdom_regime(data)
    regime = wisdom["regime"]
    vol = wisdom["volatility"]
    daily = load_daily_matrix(days=450)
    gp = evaluate(daily, regime) if daily is not None and not daily.empty else {}
    crypto_gate = crypto_trading_allowed(vol, regime)
    stress = (
        bool(gp.get("stress"))
        or bool(wisdom.get("governor_stress"))
        or regime in PAUSED_REGIMES
    )
    entries_blocked = bool(wisdom.get("wisdom_paused")) or regime in PAUSED_REGIMES

    plan = build_rebalance_plan(
        stress=stress,
        crypto_allowed=bool(crypto_gate.get("allowed")),
        entries_blocked=entries_blocked,
    )
    cleanup = build_cleanup_intents(max_actions=10, wisdom_stress=stress)
    crypto_intents = []
    if crypto_gate.get("allowed") and not entries_blocked:
        crypto_intents = crypto_trade_intents(
            data,
            regime,
            datetime.datetime.now(),
            {},
            volatility=vol,
            notional=config.KRAKEN_CRYPTO_NOTIONAL,
        )

    out = {
        "regime": regime,
        "volatility": vol,
        "wisdom_mode": wisdom.get("wisdom_mode"),
        "wisdom_paused": wisdom.get("wisdom_paused"),
        "game_plan_stress": gp.get("stress"),
        "yield_gate": gp.get("yield_gate"),
        "crypto_gate": crypto_gate,
        "rebalance_profile": plan.get("profile"),
        "total_usd": plan.get("total_usd"),
        "target_weights_pct": plan.get("weights"),
        "current_usd": plan.get("current"),
        "rebalance_trades": plan.get("trades"),
        "cleanup_intents": cleanup,
        "crypto_mirror_intents": crypto_intents,
        "entries_blocked": entries_blocked,
        "plan_error": plan.get("error"),
    }
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
