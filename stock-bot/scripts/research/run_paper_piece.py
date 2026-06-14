"""Run one or more bot pieces on the Alpaca paper book (research / isolation).

Uses PAPER_APCA_* (social paper account) by default — does not touch live ~$100.

Examples:
  python scripts/research/run_paper_piece.py --piece status
  python scripts/research/run_paper_piece.py --piece wisdom --piece alloc
  python scripts/research/run_paper_piece.py --piece vti_core --apply
  python scripts/research/run_paper_piece.py --piece social --apply
  python scripts/research/run_paper_piece.py --piece spy --piece nyse --apply
  python scripts/research/run_paper_piece.py --piece felix
  python scripts/research/run_paper_piece.py --piece all-active --apply

Dry-run (default): prints signals and intents; no orders unless --apply.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config
from modules.alpaca_executor import AlpacaExecutor
from modules.data_loader import load_close_matrix
from modules.felix_sentiment import maybe_sync_felix_transcripts
from modules.market_hours import is_equity_market_open
from modules.pipeline_strategies import (
    COOLDOWN_SECONDS,
    crypto_trade_intents,
    resolve_cycle_deploy,
    run_crypto_strategy,
    run_equity_strategy,
    run_spy_exits,
    run_spy_strategy,
    _spy_buy_intent,
    _nyse_buy_intent,
    _crypto_buy_intent,
)
from modules.portfolio_manager import PortfolioManager
from modules.social_sleeve import run_social_sleeve_cycle, social_paper_available
from modules.vti_core import rebalance_vti_core, vti_core_value
from modules.wisdom_sentiment import resolve_wisdom_regime

PIECES = (
    "status",
    "wisdom",
    "alloc",
    "felix",
    "rebalance",
    "vti_core",
    "social",
    "spy",
    "crypto",
    "nyse",
    "all-active",
)


def _make_executor(account: str) -> AlpacaExecutor:
    if account == "social":
        from modules.social_sleeve import get_social_alpaca_credentials

        creds = get_social_alpaca_credentials()
        if not creds:
            raise SystemExit(
                "Paper research needs PAPER_APCA_API_KEY_ID / PAPER_APCA_API_SECRET_KEY in .env"
            )
        return AlpacaExecutor(paper=True, credentials_fn=lambda: creds)
    # main-paper: Alpaca paper endpoint with primary keys (only if keys are paper-type)
    return AlpacaExecutor(paper=True)


def _load_context(executor: AlpacaExecutor) -> dict:
    data = load_close_matrix()
    if data.empty or len(data) < 20:
        raise SystemExit("Need market data — run: python fetch_data.py")
    wisdom = config.apply_paper_wisdom_floor(resolve_wisdom_regime(data))
    try:
        market_open = is_equity_market_open(executor.client)
    except Exception:
        market_open = True
    executor.equity_session_open = market_open
    executor.refresh_cache()
    account = executor._get_account()
    return {
        "data": data,
        "wisdom": wisdom,
        "regime": wisdom["regime"],
        "vol": wisdom["volatility"],
        "market_open": market_open,
        "equity": float(account.equity),
        "cash": float(account.cash),
        "pair_cooldown": {},
        "now": datetime.now(),
    }


def _print_status(executor: AlpacaExecutor, ctx: dict) -> None:
    snap = executor.sleeve_snapshot()
    print("--- Paper research status ---")
    print(f"Account equity: ${ctx['equity']:,.2f} | cash ${ctx['cash']:,.2f}")
    print(f"Market open:    {ctx['market_open']}")
    print(f"VTI core:       ${snap.get('vti_core_value', 0):,.2f} / cap ${snap.get('vti_core_cap', 0):,.2f}")
    print(f"SPY sleeve:     ${snap['spy_value']:,.2f} / cap ${snap['spy_cap']:,.2f}")
    print(f"Crypto sleeve:  ${snap['crypto_value']:,.2f} / cap ${snap['crypto_cap']:,.2f}")
    print(f"NYSE sleeve:    ${snap['nyse_value']:,.2f} / cap ${snap['nyse_cap']:,.2f}")


def _print_wisdom(ctx: dict) -> None:
    w = ctx["wisdom"]
    print("--- Wisdom ---")
    print(f"Regime: {w.get('regime')} | vol {w.get('volatility')}")
    print(f"Web: {w.get('web_sentiment')} | gap {w.get('sentiment_gap')}")
    if w.get("felix_video_id"):
        print(
            f"Felix: {w.get('felix_sentiment')} | "
            f"{(w.get('felix_video_title') or '')[:60]}"
        )


def _print_alloc() -> None:
    alloc = config.fund_allocation_pct()
    tag = " (PAPER AGGRESSIVE)" if config.paper_aggressive_context() else ""
    print(f"--- Fund allocation{tag} ---")
    if config.vti_core_enabled():
        print(f"VTI core: {alloc['vti_core']:.1%} {config.VTI_CORE_SYMBOL}")
    print(
        f"SPY {alloc['spy']:.1%} | crypto {alloc['crypto']:.1%} | "
        f"NYSE {alloc['nyse']:.1%} | cash {alloc['cash_buffer']:.1%}"
    )
    if config.paper_aggressive_context():
        print(
            f"Boost: {config.PAPER_ACTIVE_SLEEVE_BOOST:.0%}x active | "
            f"social cap {config.effective_social_sleeve_cap_pct():.0%} | "
            f"crypto vol-only {config.effective_crypto_vol_only()}"
        )


def _dry_intents(executor: AlpacaExecutor, ctx: dict, piece: str) -> None:
    data, regime, vol = ctx["data"], ctx["regime"], ctx["vol"]
    now, cd = ctx["now"], ctx["pair_cooldown"]
    mo = ctx["market_open"]
    print(f"--- {piece} (dry-run intents) ---")
    if piece == "spy":
        print(f"buy_intent: {_spy_buy_intent(data, regime, now, cd)}")
        n = executor.compute_spy_notional()
        print(f"notional:   {n}")
    elif piece == "crypto":
        print(f"buy_intent: {_crypto_buy_intent(data, regime, now, cd, volatility=vol)}")
        intents = crypto_trade_intents(
            data, regime, now, cd, volatility=vol, notional=executor.compute_crypto_notional()
        )
        for i in intents[:5]:
            print(f"  {i}")
    elif piece == "nyse":
        print(f"buy_intent: {_nyse_buy_intent(data, regime, now, cd)}")
        n = executor.compute_nyse_notional()
        print(f"notional:   {n}")
    elif piece == "vti_core":
        eq = ctx["equity"]
        pct = config.vti_core_allocation_pct()
        target = round(eq * pct, 2) if pct > 0 else 0
        print(f"VTI now ${vti_core_value(executor):,.2f} -> target ${target:,.2f} ({pct:.0%})")
    elif piece == "social":
        from modules.social_sleeve import aggregate_social_score, target_symbol_for_score

        agg = aggregate_social_score(ctx["wisdom"])
        print(f"score {agg.get('score')} -> {target_symbol_for_score(agg.get('score'))}")


def _run_piece(executor: AlpacaExecutor, ctx: dict, piece: str, *, apply: bool) -> None:
    if piece == "status":
        _print_status(executor, ctx)
        return
    if piece == "wisdom":
        _print_wisdom(ctx)
        return
    if piece == "alloc":
        _print_alloc()
        return
    if piece == "felix":
        r = maybe_sync_felix_transcripts(force=True)
        print(json.dumps(r or {"skipped": "interval or disabled"}, indent=2))
        return
    if piece == "rebalance":
        from modules.holdings_rebalance import rebalance_to_targets

        pm = PortfolioManager()
        r = rebalance_to_targets(
            executor,
            ctx["data"],
            regime=ctx["regime"],
            volatility=ctx["vol"],
            market_open=ctx["market_open"],
            portfolio_manager=pm,
            dry_run=not apply,
        )
        print(json.dumps(r, indent=2, default=str))
        if not apply:
            print("(dry-run — pass --apply to trim/deploy sleeves)")
        return

    if not apply:
        _dry_intents(executor, ctx, piece)
        print("(dry-run — pass --apply to place orders)")
        return

    data = ctx["data"]
    regime, vol, mo = ctx["regime"], ctx["vol"], ctx["market_open"]
    now, cd = ctx["now"], ctx["pair_cooldown"]
    wisdom = ctx["wisdom"]

    if piece == "vti_core":
        r = rebalance_vti_core(executor, market_open=mo)
        print(json.dumps(r, indent=2))
        return
    if piece == "social":
        if not social_paper_available():
            raise SystemExit("social piece needs PAPER_APCA_* keys")
        r = run_social_sleeve_cycle(wisdom, executor, market_open=mo)
        print(json.dumps(r, indent=2, default=str))
        return

    if hasattr(executor, "set_wisdom_sizing_multiplier"):
        executor.set_wisdom_sizing_multiplier(wisdom.get("sizing_multiplier", 1.0))
    resolve_cycle_deploy(
        data, executor, regime, now, cd, volatility=vol, market_open=mo
    )

    if piece == "spy":
        run_spy_exits(data, executor, regime)
        n = run_spy_strategy(data, executor, regime, now, cd)
        print(f"SPY strategy trades: {n}")
        return
    if piece == "crypto":
        pm = PortfolioManager()
        n = run_crypto_strategy(
            data, executor, regime, now, cd, portfolio_manager=pm, volatility=vol
        )
        print(f"Crypto strategy trades: {n}")
        return
    if piece == "nyse":
        n = run_equity_strategy(data, executor, regime, now, cd)
        print(f"NYSE strategy trades: {n}")
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Run isolated paper bot pieces for research")
    parser.add_argument(
        "--piece",
        action="append",
        choices=PIECES,
        required=True,
        help="Component to run (repeat flag for multiple)",
    )
    parser.add_argument(
        "--account",
        choices=("social", "main-paper"),
        default="social",
        help="social=PAPER_APCA_* book (default); main-paper=primary keys on paper API",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Place orders (default: dry-run / print intents only)",
    )
    parser.add_argument(
        "--conservative",
        action="store_true",
        help="Disable paper aggressive profile (use live-like caps on paper)",
    )
    args = parser.parse_args()

    aggressive = config.PAPER_AGGRESSIVE_ENABLED and not args.conservative
    config.set_paper_aggressive_context(aggressive)

    pieces: list[str] = []
    for p in args.piece:
        if p == "all-active":
            pieces.extend(
                ["rebalance", "vti_core", "social", "spy", "crypto", "nyse"]
            )
        else:
            pieces.append(p)
    # dedupe preserve order
    seen: set[str] = set()
    ordered = []
    for p in pieces:
        if p not in seen:
            seen.add(p)
            ordered.append(p)

    executor = _make_executor(args.account)
    ctx = _load_context(executor)
    mode = "AGGRESSIVE" if aggressive else "conservative"
    print(
        f"Paper research | account={args.account} | mode={mode} | "
        f"apply={args.apply} | pieces={', '.join(ordered)}"
    )

    try:
        for piece in ordered:
            _run_piece(executor, ctx, piece, apply=args.apply)
            if piece not in ("felix",) and args.apply:
                executor.refresh_cache()
                ctx["equity"] = float(executor._get_account().equity)
                ctx["cash"] = float(executor._get_account().cash)
    finally:
        config.set_paper_aggressive_context(False)


if __name__ == "__main__":
    main()
