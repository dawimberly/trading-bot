"""24/7 live trading loop: refresh data, detect regime, run crypto and equity strategies.

Run: python run_all.py
Preflight: python scripts/account/preflight.py
"""

import datetime
import time
import traceback

import config
from modules.safe_io import install_safe_stdout, write_json_atomic
from modules.alpaca_executor import AlpacaExecutor
from modules.data_loader import load_close_matrix
from modules.data_refresh import RefreshScheduler
from modules.market_hours import is_equity_market_open
from modules.wisdom_sentiment import resolve_wisdom_regime
from modules.pipeline_strategies import (
    run_crypto_strategy,
    run_equity_strategy,
    run_spy_exits,
    run_spy_strategy,
    resolve_cycle_deploy,
)
from modules.portfolio_manager import PortfolioManager
from modules.holdings_reconcile import reconcile
from modules.holdings_rebalance import rebalance_to_targets
from modules.crypto_vol_gate import crypto_trading_allowed
from modules.spacex_ipo_monitor import format_monitor_line, get_spacex_ipo_monitor
from modules.spacex_ipo_listing_monitor import (
    format_listing_line,
    get_spacex_ipo_listing_status,
)
from modules.spacex_ipo_buy import maybe_buy_spacex_ipo
from modules.kraken_ipo_buy import maybe_buy_kraken_spcx
from modules.kraken_autopilot import format_autopilot_line, run_kraken_autopilot
from modules.position_exits import run_position_exits
from modules.risk_management import RiskManager
from modules import trade_journal
from modules import alerts
from modules import wisdom_journal
from modules.game_plan import run_game_plan_cycle
from modules.macro_signals import ensure_macro_daily, evaluate, load_daily_matrix
from modules.wisdom_evaluator import maybe_run_daily_evaluation, maybe_run_monthly_rollup

pair_cooldown = {}
refresh_scheduler = RefreshScheduler()
risk_manager = RiskManager(max_drawdown_pct=config.MAX_DRAWDOWN_PCT)
portfolio_manager = PortfolioManager(ledger_file=config.LEDGER_PATH)
_startup_reconciled = False
_startup_rebalanced = False
_macro_daily_bootstrapped = False


def _gap_wide(gap) -> bool:
    if gap is None:
        return False
    try:
        return abs(float(gap)) >= config.WISDOM_GAP_THRESHOLD
    except (TypeError, ValueError):
        return False


def _game_plan_signals(regime: str) -> dict:
    global _macro_daily_bootstrapped
    if not config.game_plan_active():
        return {"ok": True, "stress": False, "yield_gate": False}
    if not _macro_daily_bootstrapped:
        ensure_macro_daily(refresh=True)
        _macro_daily_bootstrapped = True
    else:
        ensure_macro_daily(refresh=False)
    daily = load_daily_matrix(days=450)
    return evaluate(daily, regime)


def _maybe_rebalance_startup(executor, data, regime, vol, market_open, yield_gated=False):
    global _startup_rebalanced
    if _startup_rebalanced or not config.REBALANCE_ON_STARTUP:
        return
    _startup_rebalanced = True
    try:
        result = rebalance_to_targets(
            executor,
            data,
            regime=regime,
            volatility=vol,
            market_open=market_open,
            portfolio_manager=portfolio_manager,
            dry_run=False,
            yield_gated=yield_gated,
        )
        n = len([a for a in result.get("actions", []) if a.get("phase") in ("buy", "sell")])
        if n:
            print(f"--- Rebalance on startup: {n} order(s) ---")
            for a in result["actions"]:
                if a.get("phase") in ("buy", "sell"):
                    print(
                        f"  {a['phase'].upper()} {a.get('symbol', '')} "
                        f"${a.get('notional', 0):,.0f} ({a.get('sleeve', '')})"
                    )
    except Exception as exc:
        print(f"Rebalance error (non-fatal): {exc}")


def _maybe_reconcile_startup(executor):
    global _startup_reconciled
    if _startup_reconciled or not config.RECONCILE_ON_STARTUP:
        return
    _startup_reconciled = True
    try:
        result = reconcile(
            executor,
            portfolio_manager,
            rebuild=True,
            trim=config.TRIM_OVER_CAP_ON_STARTUP,
        )
        over = result["before"]["over_cap"]
        if any(v >= config.MIN_NOTIONAL for v in over.values()):
            print("--- Holdings reconcile (startup) ---")
            print(f"  Over-cap before: SPY ${over['spy']:,.0f} | crypto ${over['crypto']:,.0f} | NYSE ${over['nyse']:,.0f}")
            if result.get("trim_actions"):
                print(f"  Trim orders: {len(result['trim_actions'])}")
            after = result["after"]["over_cap"]
            print(f"  Over-cap after:  SPY ${after['spy']:,.0f} | crypto ${after['crypto']:,.0f} | NYSE ${after['nyse']:,.0f}")
        if result.get("ledger"):
            print(f"  Ledger rebuilt: {result['ledger']['open_positions']} Alpaca positions")
    except Exception as exc:
        print(f"Holdings reconcile error (non-fatal): {exc}")


def log_trade(symbol, side, regime):
    with open(config.TRADE_HISTORY_LOG, "a", encoding="utf-8") as f:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"{ts} | {side.upper()} | {symbol} | Regime: {regime}\n")


def _crypto_log(symbol, side, regime, pair_key, z, notional=""):
    cap = config.CRYPTO_SLEEVE_CAP_PCT
    print(
        f"!!! CRYPTO SLEEVE: {pair_key} | Z={round(z, 2)} | "
        f"{side.upper()} ${notional} | cap {cap:.2%} | Regime: {regime}"
    )
    log_trade(symbol, side, regime)
    trade_journal.log_signal(symbol, side, regime, pair_key, z, _last_equity, notional)


def _equity_log(symbol, side, regime, pair_key, _z, notional=""):
    print(
        f"!!! NYSE SLEEVE: {symbol} above MA50 | BUY ${notional} | "
        f"cap {config.NYSE_SLEEVE_CAP_PCT:.2%} | Regime: {regime}"
    )
    log_trade(symbol, side, regime)
    trade_journal.log_signal(symbol, side, regime, pair_key, 0.0, _last_equity, notional)


def _spy_log(symbol, side, regime, pair_key, momentum, notional=""):
    if side == "buy":
        print(
            f"!!! SPY SLEEVE: {symbol} above MA{config.SPY_MA_WINDOW} | "
            f"BUY ${notional} | cap {config.SPY_SLEEVE_CAP_PCT:.2%} | Regime: {regime}"
        )
    else:
        print(
            f"!!! SPY SLEEVE: {symbol} below MA{config.SPY_MA_WINDOW} | "
            f"SELL ${notional} | Regime: {regime}"
        )
    log_trade(symbol, side, regime)
    trade_journal.log_signal(
        symbol, side, regime, pair_key, momentum, _last_equity, notional
    )


_last_equity = 0.0


def _write_heartbeat(
    regime,
    equity,
    cash,
    crypto_trades,
    equity_trades,
    spy_trades,
    halted,
    market_open,
    sleeves=None,
    wisdom=None,
    spacex_ipo=None,
    spacex_listing=None,
    game_plan=None,
):
    payload = {
        "timestamp": datetime.datetime.now().isoformat(),
        "regime": regime,
        "equity": equity,
        "cash": cash,
        "crypto_trades_last_cycle": crypto_trades,
        "equity_trades_last_cycle": equity_trades,
        "spy_trades_last_cycle": spy_trades,
        "sleeve_caps": {
            "spy": config.effective_sleeve_cap(config.SPY_SLEEVE_CAP_PCT),
            "crypto": config.effective_sleeve_cap(config.CRYPTO_SLEEVE_CAP_PCT),
            "nyse": config.effective_sleeve_cap(config.NYSE_SLEEVE_CAP_PCT),
            "metal": config.METAL_SLEEVE_CAP_PCT if config.metal_sleeve_enabled() else 0.0,
            "cash_buffer": config.effective_cash_buffer_pct(),
        },
        "crypto_vol_only": config.CRYPTO_VOL_ONLY,
        "equity_session_open": market_open,
        "halted": halted,
        "paper": config.PAPER_TRADING,
    }
    if sleeves:
        payload["sleeve_exposure"] = sleeves
    if wisdom:
        payload["wisdom"] = {
            "mode": wisdom.get("wisdom_mode"),
            "web_sentiment": wisdom.get("web_sentiment"),
            "price_sentiment": wisdom.get("price_sentiment"),
            "gap": wisdom.get("sentiment_gap"),
            "paused": wisdom.get("wisdom_paused"),
            "governor_stress": wisdom.get("governor_stress"),
            "gap_tier": wisdom.get("gap_tier"),
            "sizing_multiplier": wisdom.get("sizing_multiplier"),
        }
    if spacex_ipo:
        payload["spacex_ipo"] = spacex_ipo
    if spacex_listing:
        payload["spacex_ipo_listing"] = {
            "stage": spacex_listing.get("stage"),
            "days_until_expected": spacex_listing.get("days_until_expected"),
            "ready_to_buy": spacex_listing.get("ready_to_buy"),
            "ready_to_buy_alpaca": spacex_listing.get("ready_to_buy_alpaca"),
            "ready_to_buy_kraken": spacex_listing.get("ready_to_buy_kraken"),
            "expected_listing_date": spacex_listing.get("expected_listing_date"),
            "alpaca_tradable": (spacex_listing.get("alpaca") or {}).get("tradable"),
            "kraken_tradable": (spacex_listing.get("kraken") or {}).get("tradable"),
            "kraken_pair": (spacex_listing.get("kraken") or {}).get("wsname"),
        }
    if game_plan:
        payload["game_plan_state"] = game_plan
    if config.game_plan_active():
        payload["game_plan"] = {
            "enabled": True,
            "yield_gate_only": config.GAME_PLAN_YIELD_GATE_ONLY,
            "yield_gate_enabled": config.YIELD_GATE_ENABLED,
            **(
                {
                    "metal_blend": config.metal_blend_weights(),
                    "metal_cap_pct": config.METAL_SLEEVE_CAP_PCT,
                    "stress_cash_pct": config.STRESS_CASH_PCT,
                }
                if config.metal_sleeve_enabled()
                else {}
            ),
        }
    write_json_atomic(config.HEARTBEAT_FILE, payload)


def main():
    global _last_equity
    now_ts = datetime.datetime.now()
    executor = AlpacaExecutor()
    market_open = refresh_scheduler.sync(executor.client, now_ts)
    executor.equity_session_open = market_open
    executor.refresh_cache()

    account = executor._get_account()
    equity = float(account.equity)
    cash = float(account.cash)
    _last_equity = equity

    prev_halted = risk_manager.halted
    can_trade = risk_manager.check_drawdown(equity)
    if not can_trade:
        if risk_manager.should_liquidate_on_breach() and market_open:
            from modules.game_plan import _trim_long_sleeves_for_cash

            target = equity * config.HALT_TARGET_CASH_PCT
            if cash < target:
                trim_actions = _trim_long_sleeves_for_cash(executor, target - cash)
                if trim_actions:
                    print(
                        f"--- Halt liquidation: {len(trim_actions)} trim(s) "
                        f"toward {config.HALT_TARGET_CASH_PCT:.0%} cash ---"
                    )
                    account = executor._get_account()
                    equity = float(account.equity)
                    cash = float(account.cash)
        peak = risk_manager.peak_equity or equity
        dd = risk_manager.current_drawdown(equity)
        if not prev_halted:
            print("!!! RISK HALT: Max drawdown reached. Skipping cycle. !!!")
        trade_journal.log_event("halt", equity=equity, cash=cash, notes="drawdown limit")
        alerts.notify_halt(equity, peak, dd)
        try:
            alerts.maybe_daily_summary(equity, cash, "HALTED", True)
        except Exception as exc:
            print(f"Alert error (non-fatal): {exc}")
        _write_heartbeat("HALTED", equity, cash, 0, 0, 0, True, market_open, None)
        return

    if prev_halted and not risk_manager.halted:
        print(
            f"--- RISK RESUME: drawdown {risk_manager.current_drawdown(equity):.1%} "
            f"below {config.HALT_RESUME_DRAWDOWN_PCT:.0%} ---"
        )
    alerts.clear_halt_flag()
    _maybe_reconcile_startup(executor)

    if not market_open:
        canceled = executor.cancel_open_equity_orders()
        if canceled:
            print(f"--- Canceled {canceled} stale equity order(s) (session closed) ---")

    print("--- Pipeline Cycle: " + str(datetime.datetime.now()) + " ---")
    data = load_close_matrix()
    if data.empty or len(data) < 20:
        print("Insufficient market data. Skipping cycle.")
        trade_journal.log_event("skip", equity=equity, notes="empty or short data")
        return

    wisdom = resolve_wisdom_regime(data)
    regime = wisdom["regime"]
    vol = wisdom["volatility"]
    if hasattr(executor, "set_wisdom_sizing_multiplier"):
        executor.set_wisdom_sizing_multiplier(wisdom.get("sizing_multiplier", 1.0))
    gp_signals = _game_plan_signals(regime)
    yield_gated = bool(gp_signals.get("yield_gate"))
    _maybe_rebalance_startup(executor, data, regime, vol, market_open, yield_gated=yield_gated)
    web = wisdom.get("web_sentiment")
    gap = wisdom.get("sentiment_gap")
    web_s = f"{web:+.2f}" if web is not None else "n/a"
    gap_s = f"{gap:+.2f}" if gap is not None else "n/a"
    pause_s = ""
    if wisdom.get("wisdom_paused"):
        tier = wisdom.get("gap_tier") or wisdom.get("wisdom_mode")
        pause_s = f" | DYNAMIC PAUSE ({tier})"
    elif wisdom.get("wisdom_mode") == "dynamic":
        tier = wisdom.get("gap_tier")
        mult = wisdom.get("sizing_multiplier", 1.0)
        if tier and tier != "no_web":
            pause_s = f" | dynamic: {tier} x{mult:.2f}"
        elif mult and mult < 0.999:
            pause_s = f" | dynamic: sizing x{mult:.2f}"
    elif wisdom.get("wisdom_mode") == "governor" and _gap_wide(gap):
        stress = wisdom.get("governor_stress")
        if stress is False:
            pause_s = " | governor: gap wide, calm (trust price)"
    gp_s = ""
    if config.game_plan_active():
        gate = "GATE" if yield_gated else "open"
        if config.GAME_PLAN_YIELD_GATE_ONLY:
            gp_s = f" | GamePlan: yield-only | SPY {gate}"
        else:
            stress = "STRESS" if gp_signals.get("stress") else "calm"
            gp_s = f" | GamePlan: {stress} | SPY {gate}"
    print(
        f"--- Regime: {regime} | Vol: {vol} | "
        f"Wisdom: {wisdom['wisdom_mode']} | web {web_s} | gap {gap_s}{pause_s}{gp_s} | "
        f"Equity session: {'OPEN' if market_open else 'CLOSED'} ---"
    )

    spacex_snapshot = get_spacex_ipo_monitor()
    spacex_heartbeat = None
    crypto_gate = crypto_trading_allowed(vol, regime, spacex_snapshot=spacex_snapshot)
    if spacex_snapshot:
        print(f"--- {format_monitor_line(spacex_snapshot)} ---")
        s = spacex_snapshot.get("summary", {})
        spacex_heartbeat = {
            "narrative": s.get("narrative"),
            "headline_count": s.get("headline_count"),
            "btc_linked_count": s.get("btc_linked_count"),
            "spcx_perp_count": s.get("spcx_perp_count"),
            "avg_sentiment": s.get("avg_sentiment"),
            "alert": spacex_snapshot.get("alert"),
            "top_headline": (s.get("top_headlines") or [{}])[0].get("title"),
            "top_spcx_perp": (s.get("top_spcx_perp") or [{}])[0].get("title"),
        }
        spacex_heartbeat["crypto_override"] = crypto_gate.get("spacex_override", False)
        spacex_heartbeat["crypto_allowed"] = crypto_gate.get("allowed", False)
        if crypto_gate.get("spacex_override"):
            print(
                f"--- Crypto vol OVERRIDE: {crypto_gate.get('reason')} "
                f"(5m vol {vol}; SpaceX narrative opens BTC pairs) ---"
            )
        try:
            alerts.maybe_spacex_ipo_alert(spacex_snapshot)
        except Exception as exc:
            print(f"SpaceX IPO alert error (non-fatal): {exc}")

    listing_snapshot = get_spacex_ipo_listing_status(executor=executor)
    spacex_listing_heartbeat = None
    ipo_buy_result = None
    if listing_snapshot:
        print(f"--- {format_listing_line(listing_snapshot)} ---")
        if listing_snapshot.get("ready_to_buy_alpaca"):
            print(f"!!! {config.SPACEX_IPO_TICKER} TRADABLE ON ALPACA — IPO listing live !!!")
        if listing_snapshot.get("ready_to_buy_kraken"):
            k = listing_snapshot.get("kraken") or {}
            print(
                f"!!! {config.SPACEX_IPO_TICKER} TRADABLE ON KRAKEN "
                f"({k.get('wsname') or k.get('pair')}) — buy on Kraken Pro !!!"
            )
        spacex_listing_heartbeat = {
            "stage": listing_snapshot.get("stage"),
            "days_until_expected": listing_snapshot.get("days_until_expected"),
            "ready_to_buy": listing_snapshot.get("ready_to_buy"),
            "ready_to_buy_alpaca": listing_snapshot.get("ready_to_buy_alpaca"),
            "ready_to_buy_kraken": listing_snapshot.get("ready_to_buy_kraken"),
            "sec_stage": (listing_snapshot.get("sec") or {}).get("sec_stage"),
            "kraken_pair": (listing_snapshot.get("kraken") or {}).get("wsname"),
        }
        try:
            alerts.maybe_spacex_listing_alert(listing_snapshot)
            alerts.maybe_spacex_ipo_countdown_alert(listing_snapshot)
        except Exception as exc:
            print(f"SpaceX listing alert error (non-fatal): {exc}")
        if market_open and listing_snapshot.get("ready_to_buy_alpaca"):
            ipo_buy_result = maybe_buy_spacex_ipo(executor, listing_snapshot)
            if ipo_buy_result:
                print(
                    f"--- SpaceX IPO paper buy {config.SPACEX_IPO_TICKER}: "
                    f"${ipo_buy_result.get('notional', 0):,.0f} "
                    f"({'ok' if ipo_buy_result.get('ok') else 'failed'}) ---"
                )
        if listing_snapshot.get("ready_to_buy_kraken"):
            kraken_buy = maybe_buy_kraken_spcx(listing_snapshot)
            if kraken_buy:
                if kraken_buy.get("ok"):
                    print(
                        f"--- Kraken SPCX buy {kraken_buy.get('pair')}: "
                        f"${kraken_buy.get('usd', 0):,.0f} "
                        f"vol {kraken_buy.get('volume')} ---"
                    )
                elif kraken_buy.get("error"):
                    print(f"--- Kraken SPCX buy skipped/failed: {kraken_buy['error']} ---")

    exits = run_position_exits(
        executor, risk_manager, trade_journal, equity_session_open=market_open
    )
    if exits:
        print(f"--- Stop-loss exits: {exits} ---")

    now = datetime.datetime.now()
    resolve_cycle_deploy(
        data,
        executor,
        regime,
        now,
        pair_cooldown,
        volatility=vol,
        spacex_snapshot=spacex_snapshot,
        yield_gated=yield_gated,
        market_open=market_open,
    )
    c = run_crypto_strategy(
        data,
        executor,
        regime,
        now,
        pair_cooldown,
        log_fn=_crypto_log,
        portfolio_manager=portfolio_manager,
        volatility=vol,
        spacex_snapshot=spacex_snapshot,
    )
    s = 0
    nyse_trades = 0
    gp_result = {"enabled": False, "signals": gp_signals, "actions": []}

    market_open = is_equity_market_open(executor.client)
    executor.equity_session_open = market_open
    if market_open:
        s += run_spy_exits(data, executor, regime, log_fn=_spy_log)
        s += run_spy_strategy(
            data,
            executor,
            regime,
            now,
            pair_cooldown,
            log_fn=_spy_log,
            portfolio_manager=portfolio_manager,
            yield_gated=yield_gated,
        )
        nyse_trades = run_equity_strategy(
            data,
            executor,
            regime,
            now,
            pair_cooldown,
            log_fn=_equity_log,
            portfolio_manager=portfolio_manager,
            yield_gated=yield_gated,
        )
        gp_result = run_game_plan_cycle(
            executor,
            regime,
            market_open=True,
            signals=gp_signals,
        )
        if gp_result.get("actions"):
            for a in gp_result["actions"]:
                phase = a.get("phase", "action")
                sym = a.get("symbol", "")
                notional = a.get("notional", "")
                print(f"--- Game plan {phase}: {sym} ${notional} ---")
                side = "sell" if phase in ("sell", "exit_metal") else "buy"
                if sym:
                    log_trade(sym, side, regime)
                    trade_journal.log_event(
                        "game_plan",
                        symbol=sym,
                        side=side,
                        regime=regime,
                        equity=equity,
                        cash=cash,
                        notional=notional,
                        notes=phase,
                    )
    else:
        print("--- Equity session closed; skipping SPY and equity scans ---")
        if config.game_plan_active():
            gp_result = run_game_plan_cycle(
                executor,
                regime,
                market_open=False,
                signals=gp_signals,
            )
    print(f"--- Crypto: {c} | SPY: {s} | NYSE: {nyse_trades} ---")

    kraken_autopilot_result = None
    if config.KRAKEN_AUTOPILOT_ENABLED:
        try:
            kraken_autopilot_result = run_kraken_autopilot(
                wisdom=wisdom,
                gp_signals=gp_signals,
                gp_result=gp_result,
                crypto_gate=crypto_gate,
                data=data,
                regime=regime,
                now=now,
                pair_cooldown=pair_cooldown,
                market_open=market_open,
            )
            print(f"--- {format_autopilot_line(kraken_autopilot_result)} ---")
            rb = kraken_autopilot_result.get("rebalance") or {}
            if rb.get("profile"):
                cap = rb.get("capabilities") or {}
                print(
                    f"--- Kraken rebalance {rb.get('profile')}: "
                    f"${rb.get('total_usd', 0):.0f} | "
                    f"API fills: crypto={cap.get('crypto_ok')} xstock={cap.get('xstock_ok')} "
                    f"| stocks not on API: {len(rb.get('needs_app') or [])} ---"
                )
            for bucket in ("cleanup", "crypto_mirror", "paper_mirror"):
                for item in kraken_autopilot_result.get(bucket) or []:
                    if not item.get("ok"):
                        continue
                    intent = item.get("intent") or item.get("trade") or {}
                    sym = intent.get("symbol") or item.get("pair", "?")
                    phase = intent.get("phase", bucket)
                    dry = " (dry-run)" if item.get("dry_run") else ""
                    print(f"--- Kraken {phase}: {sym}{dry} ---")
            for item in rb.get("executed") or []:
                if not item.get("ok"):
                    continue
                tr = item.get("trade") or {}
                sym = tr.get("symbol", "?")
                dry = " (dry-run)" if item.get("dry_run") else ""
                print(f"--- Kraken rebalance: {tr.get('side')} {sym}{dry} ---")
        except Exception as exc:
            print(f"--- Kraken autopilot error (non-fatal): {exc} ---")

    sleeves = executor.sleeve_snapshot()
    metal_line = ""
    if config.metal_sleeve_enabled() and "metal_value" in sleeves:
        metal_line = (
            f" | Metal ${round(sleeves['metal_value'], 2)}/"
            f"${round(sleeves['metal_cap'], 2)}"
        )
    print(
        f"--- Exposure: SPY ${round(sleeves['spy_value'], 2)}/${round(sleeves['spy_cap'], 2)} | "
        f"Crypto ${round(sleeves['crypto_value'], 2)}/${round(sleeves['crypto_cap'], 2)} | "
        f"NYSE ${round(sleeves['nyse_value'], 2)}/${round(sleeves['nyse_cap'], 2)}{metal_line} ---"
    )
    gp_notes = ""
    if config.game_plan_active() and gp_result.get("enabled"):
        sig = gp_result.get("signals") or {}
        gp_notes = (
            f"game_plan stress={sig.get('stress')} gate={sig.get('yield_gate')} "
            f"metal=${gp_result.get('metal_value', 0)}"
        )
    trade_journal.log_cycle(
        regime,
        equity,
        cash,
        c,
        nyse_trades,
        notes=(
            f"spy={s} crypto_cap={config.CRYPTO_SLEEVE_CAP_PCT:.2%} "
            f"nyse_cap={config.effective_sleeve_cap(config.NYSE_SLEEVE_CAP_PCT):.2%}; "
            f"{gp_notes}"
        ),
    )
    try:
        alerts.maybe_daily_summary(equity, cash, regime, False)
    except Exception as exc:
        print(f"Alert error (non-fatal): {exc}")
    _write_heartbeat(
        regime,
        equity,
        cash,
        c,
        nyse_trades,
        s,
        False,
        market_open,
        sleeves,
        wisdom,
        spacex_heartbeat,
        spacex_listing_heartbeat,
        gp_result if config.game_plan_active() else None,
    )

    wisdom_journal.log_cycle(
        data,
        datetime.datetime.now(),
        wisdom,
        equity=equity,
        cash=cash,
        crypto_trades=c,
        spy_trades=s,
        nyse_trades=nyse_trades,
        spacex_ipo=spacex_snapshot,
        crypto_gate=crypto_gate,
    )
    scorecard = maybe_run_daily_evaluation()
    if scorecard:
        print(f"--- Wisdom scorecard: {scorecard.get('recommendation', '')} ---")

    rollup = maybe_run_monthly_rollup()
    if rollup:
        print(
            f"--- Wisdom monthly {rollup.get('month')}: "
            f"{rollup.get('recommendation', '')} ---"
        )
        try:
            alerts.maybe_monthly_wisdom_summary(rollup)
        except Exception as exc:
            print(f"Monthly wisdom alert error (non-fatal): {exc}")


def _print_startup_banner():
    mode = "PAPER" if config.PAPER_TRADING else "LIVE"
    print("--- Starting 24/7 Weinstein-Iteration Engine ---")
    print(f"--- Alpaca mode: {mode} (Kraken not used) ---")
    alloc = config.fund_allocation_pct()
    print(
        f"--- Fund: SPY {alloc['spy']:.0%} | "
        f"crypto {alloc['crypto']:.0%} (vol-only) | "
        f"NYSE {alloc['nyse']:.0%} | "
        f"cash buffer {alloc['cash_buffer']:.0%} ---"
    )
    if config.game_plan_active():
        if config.GAME_PLAN_YIELD_GATE_ONLY:
            print(
                f"--- Game plan: yield-gate-only | yield gate "
                f"{'ON' if config.YIELD_GATE_ENABLED else 'OFF'} ---"
            )
        else:
            blend = config.metal_blend_weights()
            print(
                f"--- Game plan ON: metal {alloc['metal']:.0%} "
                f"({blend['GLD']:.0%} GLD / {blend['SLV']:.0%} SLV / {blend['CPER']:.0%} CPER) | "
                f"stress cash {config.STRESS_CASH_PCT:.0%} | yield gate "
                f"{'ON' if config.YIELD_GATE_ENABLED else 'OFF'} ---"
            )
    config.print_recommended_stack_flags()
    print(
        f"--- SPY MA{config.SPY_MA_WINDOW} | crypto Z-pairs | NYSE MA50 | "
        f"{config.RISK_PER_TRADE:.0%}/trade within sleeve ---"
    )
    print(f"--- Sentiment: {config.SENTIMENT_SOURCE} (RHYME regimes) ---")
    if config.WISDOM_MODE == "dynamic":
        print(
            f"--- Wisdom: dynamic | gap agg<{config.SENTIMENT_GAP_THRESHOLD_AGGRESSIVE} "
            f"normal<{config.SENTIMENT_GAP_THRESHOLD_NORMAL} "
            f"def>{config.SENTIMENT_GAP_THRESHOLD_DEFENSIVE} | "
            f"sizing {config.DYNAMIC_SIZING_MULTIPLIER_MIN}-{config.DYNAMIC_SIZING_MULTIPLIER_MAX} ---"
        )
    else:
        print(
            f"--- Wisdom: {config.WISDOM_MODE} | gap threshold {config.WISDOM_GAP_THRESHOLD} ---"
        )
    if config.WISDOM_EVAL_ENABLED:
        print(
            f"--- Wisdom eval: every {config.WISDOM_EVAL_DAYS}d -> "
            f"{config.WISDOM_SCORECARD_FILE} (history: {config.WISDOM_EVAL_HISTORY_FILE}) ---"
        )
    if config.WISDOM_MONTHLY_ENABLED:
        print(
            f"--- Wisdom monthly: rollup + alert -> wisdom_monthly_YYYY-MM.json "
            f"(history: {config.WISDOM_MONTHLY_HISTORY_FILE}) ---"
        )
    if config.SPACEX_IPO_MONITOR_ENABLED:
        print(
            f"--- SpaceX IPO monitor: RSS headlines -> {config.SPACEX_IPO_CACHE_FILE} "
            f"(cache {config.SPACEX_IPO_CACHE_HOURS}h) ---"
        )
    if config.SPACEX_IPO_CRYPTO_OVERRIDE:
        print(
            "--- SpaceX crypto override: opens BTC pairs when IPO/BTC or SPCX-perp "
            "narrative hot (despite Low 5m vol) ---"
        )
    if config.SPACEX_IPO_LISTING_MONITOR_ENABLED:
        print(
            f"--- SpaceX IPO listing: SEC + Alpaca scan for {config.SPACEX_IPO_TICKER} "
            f"(expected {config.SPACEX_IPO_EXPECTED_DATE}) -> "
            f"{config.SPACEX_IPO_LISTING_CACHE_FILE} ---"
        )
        if config.SPACEX_IPO_AUTO_BUY and config.PAPER_TRADING:
            print(
                f"--- SpaceX IPO paper auto-buy: ${config.SPACEX_IPO_BUY_NOTIONAL:,.0f} "
                f"when {config.SPACEX_IPO_TICKER} tradable on Alpaca ---"
            )
        if config.KRAKEN_SPCX_BUY_ENABLED:
            print(
                f"--- Kraken SPCX live buy: ${config.KRAKEN_SPCX_BUY_USD:,.0f} "
                f"when SPCX/SPCXx appears on Kraken Pro API ---"
            )
    print(f"--- Journal: {config.PAPER_JOURNAL_CSV} | Heartbeat: {config.HEARTBEAT_FILE} ---")
    if alerts.alerts_configured():
        print("--- Alerts: enabled (Telegram and/or email) ---")
    else:
        print("--- Alerts: off (set TELEGRAM_* or SMTP_* in .env) ---")
    if not config.PAPER_TRADING:
        print("!!! WARNING: Live trading enabled !!!")


if __name__ == "__main__":
    install_safe_stdout()
    _print_startup_banner()
    trade_journal.log_event("startup", notes="run_all.py started")
    while True:
        try:
            main()
        except Exception as e:
            tb = traceback.format_exc()
            print("Cycle Error: " + str(e))
            if tb.strip() and tb.strip() != f"{type(e).__name__}: {e}":
                print(tb)
            notes = str(e)
            if tb.strip():
                notes = f"{notes}\n{tb[-1500:]}"
            trade_journal.log_event("error", notes=notes)
        time.sleep(60)
