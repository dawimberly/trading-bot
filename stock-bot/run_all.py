"""24/7 live trading loop: refresh data, detect regime, run crypto and equity strategies.

Run: python run_all.py
Preflight: python scripts/account/preflight.py
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import sys
import time
import traceback
import warnings

from modules.logging_utils import setup_logging, log_event, log_subsystem_warning

import config
from modules.safe_io import install_safe_stdout, write_json_atomic, fatal_startup
from modules.alpaca_client import AlpacaAuthError, AlpacaCriticalError, AlpacaValidationError
from modules.alpaca_executor import AlpacaExecutor
from modules.real_time_data import load_live_close_matrix, start_realtime_feed, format_status_line
from modules.data_refresh import RefreshScheduler
from modules.market_hours import is_equity_market_open
from modules.wisdom_sentiment import resolve_wisdom_regime
from modules.pipeline_strategies import (
    run_crypto_strategy,
    run_equity_strategy,
    run_equity_pairs_strategy,
    run_international_strategy,
    run_bond_strategy,
    run_spy_exits,
    run_spy_strategy,
    resolve_cycle_deploy,
    summarize_entry_skip_reason,
)
from modules.portfolio_manager import PortfolioManager
from modules.holdings_reconcile import reconcile
from modules.holdings_rebalance import rebalance_to_targets
from modules.crypto_vol_gate import crypto_trading_allowed
from modules.vti_core import rebalance_vti_core, vti_core_value
from modules.position_exits import run_position_exits
from modules.risk_management import RiskManager
from modules import trade_journal
from modules import alerts
from modules import wisdom_journal
from modules.game_plan import run_game_plan_cycle
from modules.macro_signals import ensure_macro_daily, evaluate, load_daily_matrix
from modules.macro_calendar import macro_event_context
from modules.cost_basis import compute_sleeve_pnl, format_sleeve_pnl_line
from modules.wisdom_evaluator import maybe_run_daily_evaluation, maybe_run_monthly_rollup
from modules.scan_schedule import (
    cycle_sleep_seconds,
    equity_scan_state,
    format_scan_schedule_line,
)
from modules.market_context import cross_asset_vol_score, get_volatility

# yfinance logs "$BTC-USD: possibly delisted; ..." for crypto pairs (false positives).
_YF_CRYPTO_DELISTED = re.compile(r"possibly delisted", re.IGNORECASE)
_YF_CRYPTO_TICKER = re.compile(r"(-USD|/USD)")


class _YfinanceCryptoDelistedFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if _YF_CRYPTO_DELISTED.search(msg) and _YF_CRYPTO_TICKER.search(msg):
            return False
        return True


_yf_logger = logging.getLogger("yfinance")
if not any(isinstance(f, _YfinanceCryptoDelistedFilter) for f in _yf_logger.filters):
    _yf_logger.addFilter(_YfinanceCryptoDelistedFilter())

warnings.filterwarnings("ignore", message=r".*possibly delisted.*")

pair_cooldown = {}

logger = logging.getLogger(__name__)


def _warn_nonfatal(context: str, exc: BaseException) -> None:
    log_subsystem_warning("run_all", context, exc)


def _social_alpaca_credentials():
    from modules.social_sleeve import get_social_alpaca_credentials

    return get_social_alpaca_credentials()


def _executor_cache_key() -> tuple:
    if (
        config.paper_chase_mode_enabled()
        and os.getenv("PAPER_CHASE_USE_RESEARCH_KEYS", "").lower() in ("1", "true", "yes")
    ):
        creds = _social_alpaca_credentials()
        if creds:
            return ("research", creds[0][-8], True)
    return ("main", config.PAPER_TRADING)


_executor_singleton: AlpacaExecutor | None = None
_executor_singleton_key: tuple | None = None


def _make_executor() -> AlpacaExecutor:
    """Paper chase can use isolated PAPER_APCA_* research book when configured."""
    global _executor_singleton, _executor_singleton_key
    key = _executor_cache_key()
    if _executor_singleton is not None and _executor_singleton_key == key:
        return _executor_singleton
    if key[0] == "research":
        creds = _social_alpaca_credentials()
        _executor_singleton = AlpacaExecutor(paper=True, credentials_fn=lambda: creds)
    else:
        _executor_singleton = AlpacaExecutor()
    _executor_singleton_key = key
    return _executor_singleton
refresh_scheduler = RefreshScheduler()
risk_manager = RiskManager(max_drawdown_pct=config.MAX_DRAWDOWN_PCT)
portfolio_manager = PortfolioManager(ledger_file=config.LEDGER_PATH)
_startup_reconciled = False
_main_cycle_count = 0
_startup_rebalanced = False
_macro_daily_bootstrapped = False
_last_cycle_schedule = None
_live_startup_confirmed = False
_strategic_rebalancer = None


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
    if (
        not config.PAPER_TRADING
        and config.ALLOW_LIVE_TRADING
        and _main_cycle_count < 2
    ):
        if _main_cycle_count == 1:
            logging.getLogger(__name__).info("Live: startup rebalance deferred until cycle 2", extra={"phase": _main_cycle_count})
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
            logging.getLogger(__name__).info("Rebalance on startup: orders submitted", extra={"orders": n})
            for a in result["actions"]:
                if a.get("phase") in ("buy", "sell"):
                    logging.getLogger(__name__).info(
                        "rebalance action",
                        extra={
                            "action": a.get('phase'),
                            "symbol": a.get('symbol'),
                            "notional": a.get('notional'),
                            "sleeve": a.get('sleeve'),
                        },
                    )
    except Exception as exc:
        _warn_nonfatal("Rebalance error", exc)


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
        min_n = config.effective_min_notional(result["before"]["equity"])
        if any(v >= min_n for v in over.values()):
            logging.getLogger(__name__).info("Holdings reconcile (startup)", extra={"over_cap_before": over})
            if result.get("trim_actions"):
                logging.getLogger(__name__).info("Holdings reconcile: trim orders", extra={"trim_orders": len(result['trim_actions'])})
            after = result["after"]["over_cap"]
            logging.getLogger(__name__).info("Holdings reconcile (after)", extra={"over_cap_after": after})
        if result.get("ledger"):
            logging.getLogger(__name__).info("Holdings reconcile ledger rebuilt", extra={"open_positions": result['ledger']['open_positions']})
        from modules.stat_arb_sleeve import reconcile_stat_arb_book

        stat = reconcile_stat_arb_book(executor)
        log = logging.getLogger(__name__)
        if stat.get("removed"):
            log.info(
                "Stat-arb book reconcile",
                extra={
                    "kept": len(stat.get("kept", [])),
                    "removed": len(stat["removed"]),
                },
            )
        if stat.get("resolved"):
            log.info(
                "Stat-arb orphans auto-resolved",
                extra={"resolved": stat["resolved"][:8]},
            )
        ignored = stat.get("ignored") or {}
        if ignored:
            log.info(
                "Stat-arb reconcile: non-pair holdings ignored",
                extra={
                    "count": sum(len(v) for v in ignored.values()),
                    "reasons": {k: len(v) for k, v in ignored.items()},
                },
            )
        if stat.get("informational"):
            log.info(
                "Stat-arb untracked legs (informational)",
                extra={"symbols": stat["informational"][:8]},
            )
    except Exception as exc:
        _warn_nonfatal("Holdings reconcile error", exc)


def log_trade(symbol, side, regime):
    with open(config.TRADE_HISTORY_LOG, "a", encoding="utf-8") as f:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"{ts} | {side.upper()} | {symbol} | Regime: {regime}\n")


def _crypto_log(symbol, side, regime, pair_key, z, notional="", pair_msg=None):
    logger = logging.getLogger(__name__)
    if pair_msg:
        logger.info(pair_msg, extra={"symbol": symbol, "pair": pair_key, "action": side, "regime": regime, "notional": notional})
    else:
        cap = config.CRYPTO_SLEEVE_CAP_PCT
        logger.info("CRYPTO SLEEVE", extra={"pair": pair_key, "z": round(z,2), "side": side.upper(), "notional": notional, "cap": cap, "regime": regime})
    log_trade(symbol, side, regime)
    trade_journal.log_signal(symbol, side, regime, pair_key, z, _last_equity, notional)


def _equity_pair_log(symbol, side, regime, pair_key, z, notional="", pair_msg=None):
    logger = logging.getLogger(__name__)
    if pair_msg:
        logger.info(pair_msg, extra={"symbol": symbol, "pair": pair_key, "action": side, "regime": regime, "notional": notional})
    else:
        logger.info("NYSE PAIR", extra={"pair": pair_key, "side": side.upper(), "notional": notional, "regime": regime})
    log_trade(symbol, side, regime)
    trade_journal.log_signal(symbol, side, regime, pair_key, z, _last_equity, notional)


def _equity_log(symbol, side, regime, pair_key, _z, notional=""):
    logging.getLogger(__name__).info(
        "NYSE SLEEVE entry",
        extra={"symbol": symbol, "notional": notional, "cap": config.NYSE_SLEEVE_CAP_PCT, "regime": regime},
    )
    log_trade(symbol, side, regime)
    trade_journal.log_signal(symbol, side, regime, pair_key, 0.0, _last_equity, notional)


def _spy_log(symbol, side, regime, pair_key, momentum, notional=""):
    if side == "buy":
        logging.getLogger(__name__).info("SPY SLEEVE buy", extra={"symbol": symbol, "notional": notional, "ma_window": config.effective_spy_ma_window(), "cap": config.SPY_SLEEVE_CAP_PCT, "regime": regime})
    else:
        logging.getLogger(__name__).info("SPY SLEEVE sell", extra={"symbol": symbol, "notional": notional, "ma_window": config.effective_spy_ma_window(), "cap": config.SPY_SLEEVE_CAP_PCT, "regime": regime})
    log_trade(symbol, side, regime)
    trade_journal.log_signal(
        symbol, side, regime, pair_key, momentum, _last_equity, notional
    )


_last_equity = 0.0


def _record_cycle_error(error: str) -> None:
    """Persist last cycle failure on heartbeat for dashboard/status (non-trading metadata)."""
    from modules.safe_io import read_json_file, write_json_atomic

    path = config.ensure_heartbeat_path_writable()
    payload = read_json_file(path) or {}
    payload["last_cycle_error"] = str(error)[:500]
    payload["last_cycle_error_at"] = datetime.datetime.now().isoformat()
    write_json_atomic(path, payload)


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
    macro_event=None,
    sleeve_pnl=None,
    scan_schedule=None,
    social_sleeve=None,
    vti_core=None,
    sleeve_caps=None,
    dynamic_vol_score=None,
    thinking_engine=None,
    risk_parity=None,
    entry_skip_reason=None,
    entry_skip_daily=None,
    dynamic_vti=None,
    heartbeat_data=None,
    heartbeat_regime=None,
    insider_state=None,
):
    macro_stress = bool(
        wisdom
        and (wisdom.get("dynamic_stress") or wisdom.get("governor_stress"))
    )
    payload = {
        "timestamp": datetime.datetime.now().isoformat(),
        "regime": regime,
        "equity": equity,
        "cash": cash,
        "cash_pct": round(float(cash) / float(equity), 6) if equity and equity > 0 else 0.0,
        "crypto_trades_last_cycle": crypto_trades,
        "equity_trades_last_cycle": equity_trades,
        "spy_trades_last_cycle": spy_trades,
        "sleeve_caps": sleeve_caps
        or {
            "vti_core": config.effective_vti_core_pct(
                equity,
                vol_score=dynamic_vol_score,
                macro_stress=macro_stress,
                regime=heartbeat_regime or regime,
                data=heartbeat_data,
                insider_state=insider_state,
            ),
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
        "last_cycle_error": None,
        "last_cycle_error_at": None,
    }
    if dynamic_vol_score is not None:
        payload["dynamic_vol_score"] = round(float(dynamic_vol_score), 6)
    if dynamic_vti:
        payload["dynamic_vti"] = dynamic_vti
    try:
        from modules.markov_regime import heartbeat_hmm_payload

        hmm_hb = heartbeat_hmm_payload()
        if hmm_hb is not None:
            payload["markov_hmm"] = hmm_hb
    except Exception:
        pass
    try:
        from modules.time_of_day import heartbeat_tod_payload

        tod_hb = heartbeat_tod_payload()
        if tod_hb is not None:
            payload["time_of_day"] = tod_hb
    except Exception:
        pass
    try:
        from modules.daily_profit_banking import heartbeat_daily_bank_payload

        bank_hb = heartbeat_daily_bank_payload()
        if bank_hb is not None:
            payload["daily_bank"] = bank_hb
    except Exception:
        pass
    if thinking_engine:
        payload["thinking_engine"] = {
            "model": thinking_engine.get("model"),
            "confidence": thinking_engine.get("confidence"),
            "suggested_tilt": thinking_engine.get("suggested_tilt"),
            "applied_deltas": thinking_engine.get("applied_deltas"),
            "apply_log": thinking_engine.get("apply_log"),
            "regime_narrative": thinking_engine.get("regime_narrative"),
            "narrative": thinking_engine.get("narrative"),
            "asymmetry": thinking_engine.get("asymmetry"),
            "reasoning_preview": (thinking_engine.get("reasoning") or "")[:400],
        }
    if risk_parity:
        payload["risk_parity"] = risk_parity
    if entry_skip_reason is not None:
        payload["entry_skip_reason"] = entry_skip_reason
    if entry_skip_daily:
        payload["entry_skip_daily"] = entry_skip_daily
    if scan_schedule:
        payload["scan_schedule"] = scan_schedule
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
            "headline_web_sentiment": wisdom.get("headline_web_sentiment"),
            "felix_sentiment": wisdom.get("felix_sentiment"),
            "felix_video_id": wisdom.get("felix_video_id"),
            "felix_video_title": wisdom.get("felix_video_title"),
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
    if macro_event:
        payload["macro_event"] = macro_event
    if sleeve_pnl:
        payload["sleeve_pnl"] = sleeve_pnl
    if vti_core and vti_core.get("enabled"):
        payload["vti_core"] = {
            "target_pct": vti_core.get("target_pct"),
            "target_value": vti_core.get("target_value"),
            "current_value": vti_core.get("current_value"),
            "drift_pct": vti_core.get("drift_pct"),
            "last_action": vti_core.get("action"),
        }
    if social_sleeve and social_sleeve.get("enabled"):
        payload["social_sleeve"] = {
            "score": social_sleeve.get("score"),
            "target": social_sleeve.get("target"),
            "cap_pct": social_sleeve.get("cap_pct"),
            "paper_equity": social_sleeve.get("paper_equity"),
            "paper_ok": social_sleeve.get("paper_ok"),
            "felix_video_id": social_sleeve.get("felix_video_id"),
        }
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
    write_json_atomic(config.ensure_heartbeat_path_writable(), payload)


def main():
    global _last_equity, _last_cycle_schedule, _main_cycle_count
    _main_cycle_count += 1
    now_ts = datetime.datetime.now()
    executor = _make_executor()
    schedule = equity_scan_state(executor.client, now_ts)
    _last_cycle_schedule = schedule
    market_open = refresh_scheduler.sync(
        executor.client, now_ts, equity_prep=schedule.get("equity_prep", False)
    )
    schedule["market_open"] = market_open
    equity_scans = schedule.get("equity_scans", market_open)
    executor.equity_session_open = market_open
    executor.refresh_cache()

    account = executor._get_account()
    equity = float(account.equity)
    cash = float(account.cash)
    config.configure_account_profile(equity, cash=cash)
    _last_equity = equity

    if config.paper_aggressive_context():
        try:
            from modules.deployment_monitor import excess_cash_warning, record_cash_snapshot

            record_cash_snapshot(equity, cash)
            cash_warn = excess_cash_warning()
            if cash_warn:
                print(f"--- {cash_warn} ---")
        except Exception as exc:
            _warn_nonfatal("deployment cash monitor", exc)

    from modules.trading_safety import (
        daily_loss_circuit_tripped,
        refresh_daily_loss_session,
        set_entry_block_for_cycle,
    )

    paper_book = bool(config.PAPER_TRADING)
    if _main_cycle_count == 1:
        refresh_daily_loss_session(equity, paper=paper_book, startup=True)

    dl_tripped, dl_reason, _ = daily_loss_circuit_tripped(
        equity,
        paper=paper_book,
    )
    set_entry_block_for_cycle(dl_reason if dl_tripped else None)

    if config.effective_daily_bank_enabled():
        try:
            from modules.daily_profit_banking import (
                format_daily_bank_banner,
                update_daily_bank,
            )

            update_daily_bank(equity)
            bank_banner = format_daily_bank_banner()
            if bank_banner and _main_cycle_count <= 2:
                print(f"--- {bank_banner} ---")
        except Exception as exc:
            _warn_nonfatal("Daily profit banking", exc)

    if config.effective_garch_vol_enabled():
        try:
            from modules.garch_vol import format_garch_vol_banner, update_garch_vol
            from modules.pipeline_strategies import load_pipeline_data

            _gdata = load_pipeline_data()
            update_garch_vol(_gdata)
            garch_banner = format_garch_vol_banner()
            if garch_banner and _main_cycle_count <= 2:
                print(f"--- {garch_banner} ---")
        except Exception as exc:
            _warn_nonfatal("GARCH vol forecast", exc)

    if config.effective_arima_enabled():
        try:
            from modules.arima_forecast import (
                format_arima_forecast_banner,
                update_arima_forecast,
            )
            from modules.pipeline_strategies import load_pipeline_data

            _adata = load_pipeline_data()
            update_arima_forecast(_adata)
            arima_banner = format_arima_forecast_banner()
            if arima_banner and _main_cycle_count <= 2:
                print(f"--- {arima_banner} ---")
        except Exception as exc:
            _warn_nonfatal("ARIMA mean forecast", exc)

    if dl_tripped:
        logger.warning(
            "DAILY LOSS CIRCUIT: %s - no new entries or thinking tilts today",
            dl_reason,
        )
        log_event("daily_loss_circuit", reason=dl_reason, equity=equity)

    prev_halted = risk_manager.halted
    risk_manager.record_equity(equity)
    can_trade = risk_manager.check_drawdown(equity)
    if not can_trade:
        if risk_manager.should_liquidate_on_breach() and market_open:
            from modules.game_plan import _trim_long_sleeves_for_cash

            target = equity * config.HALT_TARGET_CASH_PCT
            if cash < target:
                trim_actions = _trim_long_sleeves_for_cash(executor, target - cash)
                if trim_actions:
                    logger.warning(
                        "Halt liquidation: %d trim(s) toward %.0f%% cash",
                        len(trim_actions),
                        config.HALT_TARGET_CASH_PCT * 100,
                    )
                    account = executor._get_account()
                    equity = float(account.equity)
                    cash = float(account.cash)
        peak = risk_manager.peak_equity or equity
        dd = risk_manager.current_drawdown(equity)
        if not prev_halted:
            log_event("risk_halt", equity=equity, peak=peak, drawdown=dd)
            logger.warning("RISK HALT: Max drawdown reached. Skipping cycle.")
        trade_journal.log_event("halt", equity=equity, cash=cash, notes="drawdown limit")
        alerts.notify_halt(equity, peak, dd)
        try:
            alerts.maybe_daily_summary(equity, cash, "HALTED", True)
        except Exception as exc:
            _warn_nonfatal("Alert error", exc)
        _write_heartbeat(
            "HALTED", equity, cash, 0, 0, 0, True, market_open, None, scan_schedule=schedule
        )
        return

    if prev_halted and not risk_manager.halted:
        dd_resume = risk_manager.current_drawdown(equity)
        log_event("risk_resume", equity=equity, drawdown=dd_resume)
        logger.info(
            "RISK RESUME: drawdown %.1f%% below %.0f%%",
            dd_resume * 100,
            config.HALT_RESUME_DRAWDOWN_PCT * 100,
        )
        try:
            alerts.notify_resume(equity, dd_resume)
        except Exception as exc:
            _warn_nonfatal("Resume alert error", exc)
    alerts.clear_halt_flag()
    peak = risk_manager.peak_equity or equity
    dd = risk_manager.current_drawdown(equity)
    try:
        alerts.maybe_major_drawdown_alert(equity, peak, dd)
    except Exception as exc:
        _warn_nonfatal("Drawdown alert error", exc)
    _maybe_reconcile_startup(executor)

    if not market_open:
        canceled = executor.cancel_open_equity_orders()
        if canceled:
            log_event("equity_orders_canceled", count=canceled, reason="session_closed")
            print(f"--- Canceled {canceled} stale equity order(s) (session closed) ---")

    log_event("cycle_start", timestamp=str(datetime.datetime.now()))
    print("--- Pipeline Cycle: " + str(datetime.datetime.now()) + " ---")
    print(f"--- {format_scan_schedule_line(schedule)} ---")
    data = load_live_close_matrix()
    if config.effective_dynamic_core_enabled():
        from modules.core_allocator import maybe_refresh_core_allocation

        maybe_refresh_core_allocation(data)
    if data.empty or len(data) < 20:
        db = config.resolve_db_path()
        db_size = db.stat().st_size if db.is_file() else 0
        log_event("cycle_skip", reason="insufficient_data", equity=equity)
        logger.warning(
            "Insufficient market data. Skipping cycle. (db=%s size=%s rows=%s)",
            db,
            db_size,
            len(data),
        )
        trade_journal.log_event("skip", equity=equity, notes="empty or short data")
        return

    felix_sync = None
    if config.FELIX_SYNC_ENABLED:
        from modules.felix_sentiment import maybe_sync_felix_transcripts

        felix_sync = maybe_sync_felix_transcripts()
    if felix_sync:
        if felix_sync.get("ok"):
            print(
                f"--- Felix transcript sync: +{felix_sync.get('added', 0)} videos "
                f"(skipped {felix_sync.get('skipped', 0)}) ---"
            )
        elif felix_sync.get("error"):
            print(f"--- Felix transcript sync skipped: {felix_sync['error']} ---")

    wisdom = config.apply_paper_wisdom_floor(resolve_wisdom_regime(data))
    display_regime = wisdom["regime"]
    regime = wisdom.get("entries_regime", display_regime)
    vol = wisdom["volatility"]
    if wisdom.get("felix_video_id"):
        print(
            f"--- Felix overlay: {wisdom.get('felix_video_title', '')[:50]} | "
            f"felix {wisdom.get('felix_sentiment')} headline "
            f"{wisdom.get('headline_web_sentiment')} -> web {wisdom.get('web_sentiment')} ---"
        )

    macro_ctx = macro_event_context()
    if macro_ctx.get("active"):
        wisdom["sizing_multiplier"] = round(
            float(wisdom.get("sizing_multiplier", 1.0)) * macro_ctx["sizing_scale"],
            3,
        )
        wisdom["macro_event_guard"] = macro_ctx.get("event")

    sleeve_pnl = None
    if config.COST_BASIS_AWARE_ENABLED:
        sleeve_pnl = compute_sleeve_pnl(executor)
        executor.set_sleeve_pnl(sleeve_pnl)
    elif hasattr(executor, "set_sleeve_pnl"):
        executor.set_sleeve_pnl(None)

    if hasattr(executor, "set_wisdom_sizing_multiplier"):
        from modules.regime_sizing import effective_regime_sizing_multiplier

        sizing = float(wisdom.get("sizing_multiplier", 1.0))
        regime_mult = effective_regime_sizing_multiplier(
            display_regime, wisdom_paused=bool(wisdom.get("wisdom_paused"))
        )
        if config.effective_regime_dynamic_sizing():
            if wisdom.get("wisdom_mode") == "dynamic":
                sizing = round(sizing * regime_mult, 3)
            else:
                sizing = regime_mult
        else:
            from modules.pipeline_strategies import regime_soft_pause_sizing_multiplier

            soft_mult = regime_soft_pause_sizing_multiplier(
                regime, wisdom_paused=bool(wisdom.get("wisdom_paused"))
            )
            if soft_mult < 0.999:
                sizing = round(sizing * soft_mult, 3)
        executor.set_wisdom_sizing_multiplier(sizing)
    if hasattr(executor, "set_current_regime"):
        executor.set_current_regime(display_regime)

    vol_label = get_volatility(data) if "get_volatility" in globals() else "Unknown"
    try:
        vol_score = cross_asset_vol_score(data)
    except NameError:
        vol_score = 0.0
    sleeve_cap_pcts = None
    if config.DYNAMIC_SLEEVE_CAPS_ENABLED:
        from modules.fund_config import get_dynamic_sleeve_caps

        sleeve_cap_pcts = get_dynamic_sleeve_caps(vol_score, equity)
        executor.set_dynamic_sleeve_caps(sleeve_cap_pcts)
        if vol_score > float(os.getenv("DYNAMIC_SLEEVE_VOL_ELEVATED", "0.018")):
            print(
                f"--- Dynamic sleeve caps: vol={vol_score:.4f} "
                f"cash={sleeve_cap_pcts.get('cash_buffer', 0):.1%} ---"
            )
    elif hasattr(executor, "set_dynamic_sleeve_caps"):
        executor.set_dynamic_sleeve_caps(None)

    macro_regime_result = None
    if config.effective_macro_regime_adaptor_enabled():
        from modules.macro_regime_adaptor import (
            apply_yield_gate_boost,
            evaluate_macro_regime,
            log_regime_messages,
            merge_regime_sleeve_caps,
        )
        try:
            macro_daily = load_daily_matrix(days=120)
        except Exception as exc:
            logger.debug("macro daily matrix (120d) load failed, using intraday only: %s", exc)
            macro_daily = None
        macro_regime = evaluate_macro_regime(
            data, daily_macro=macro_daily, wisdom=wisdom
        )
        macro_regime_result = macro_regime
        if macro_regime.get("active"):
            log_regime_messages(macro_regime)
            base_caps = sleeve_cap_pcts or config.fund_allocation_pct()
            merged = merge_regime_sleeve_caps(base_caps, macro_regime)
            executor.set_dynamic_sleeve_caps(merged)
            sleeve_cap_pcts = merged

    if schedule.get("crypto_only"):
        gp_signals = {"ok": True, "stress": False, "yield_gate": False}
    else:
        gp_signals = _game_plan_signals(regime)
    yield_gated = bool(gp_signals.get("yield_gate"))
    if macro_regime_result:
        yield_gated = apply_yield_gate_boost(yield_gated, macro_regime_result)
    raw_yield_gated = yield_gated
    yield_gated = config.effective_yield_gate(yield_gated, regime=regime)
    if (
        config.PAPER_YIELD_GATE_OVERRIDE
        and (config.paper_aggressive_context() or config.is_realistic_research_active())
    ):
        logger.info(
            "Paper yield gate override active - allowing more deployment"
            + (
                f" (raw={raw_yield_gated} -> effective={yield_gated})"
                if raw_yield_gated != yield_gated or raw_yield_gated
                else ""
            )
        )
        print(
            "--- Paper yield gate override active - allowing more deployment ---"
        )
    try:
        alerts.maybe_yield_gate_alert(yield_gated)
    except Exception as exc:
        _warn_nonfatal("Yield gate alert error", exc)

    macro_stress_flag = bool(
        wisdom.get("dynamic_stress")
        or wisdom.get("governor_stress")
        or gp_signals.get("stress")
    )
    executor.set_dynamic_risk_context(
        vol_score=vol_score,
        regime=regime,
        macro_stress=macro_stress_flag,
    )
    dd = risk_manager.current_drawdown(equity)
    config.set_dynamic_risk_context(
        drawdown=dd,
        recovery_mode=risk_manager.recovery_mode,
        equity_history=risk_manager.recent_equity_history(),
    )
    if config.effective_paper_profit_protect_enabled():
        from modules.dynamic_risk import update_profit_protect_context

        update_profit_protect_context(equity=equity)
    if config.paper_aggressive_context() and config.PAPER_DYNAMIC_RISK_ENABLED:
        dyn_risk = config.effective_risk_per_trade(equity)
        print(
            f"--- Dynamic risk: {dyn_risk:.1%} per trade "
            f"(vol={vol_score:.4f}, stress={macro_stress_flag}) ---"
        )
    _maybe_rebalance_startup(
        executor, data, regime, vol, equity_scans, yield_gated=yield_gated
    )
    web = wisdom.get("web_sentiment")
    gap = wisdom.get("sentiment_gap")
    web_s = f"{web:+.2f}" if web is not None else "n/a"
    gap_s = f"{gap:+.2f}" if gap is not None else "n/a"
    pause_s = ""
    if wisdom.get("wisdom_paused"):
        tier = wisdom.get("gap_tier") or wisdom.get("wisdom_mode")
        pause_s = f" | DYNAMIC PAUSE ({tier})"
        if regime != display_regime:
            print(
                f"--- Wisdom pause: entries gated as {regime} "
                f"(classified {display_regime}) ---"
            )
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
    macro_s = ""
    if macro_ctx.get("active"):
        ev = macro_ctx.get("event") or {}
        macro_s = (
            f" | MACRO GUARD: {ev.get('name', '?')} "
            f"x{macro_ctx.get('sizing_scale', 1):.2f}"
        )
    elif macro_ctx.get("next"):
        nxt = macro_ctx["next"]
        macro_s = f" | Next macro: {nxt.get('name')} {nxt.get('date')}"
    pnl_s = ""
    if sleeve_pnl:
        pnl_line = format_sleeve_pnl_line(sleeve_pnl)
        if pnl_line != "flat":
            pnl_s = f" | P&L: {pnl_line}"
    regime_sz = ""
    if config.effective_regime_dynamic_sizing():
        from modules.regime_sizing import effective_regime_sizing_multiplier

        rm = effective_regime_sizing_multiplier(
            display_regime, wisdom_paused=bool(wisdom.get("wisdom_paused"))
        )
        regime_sz = f" | sizing x{rm:.2f}"
        from modules.regime_sizing import regime_sleeve_exposure_ceiling

        ceil = regime_sleeve_exposure_ceiling(display_regime)
        if ceil is not None:
            regime_sz += f" | sleeve cap {ceil:.0%}"
    if config.effective_daily_bank_enabled():
        try:
            from modules.daily_profit_banking import (
                format_daily_bank_banner,
                is_banked,
                update_daily_bank,
            )

            update_daily_bank(equity)
            if is_banked():
                bb = format_daily_bank_banner()
                if bb:
                    print(f"--- {bb} ---")
        except Exception as exc:
            _warn_nonfatal("Daily profit banking refresh", exc)

    print(
        f"--- Regime: {display_regime} | Vol: {vol} | "
        f"Wisdom: {wisdom['wisdom_mode']} | web {web_s} | gap {gap_s}{pause_s}{regime_sz}{gp_s}{macro_s}{pnl_s} | "
        f"Equity session: {'OPEN' if market_open else 'CLOSED'} | "
        f"phase: {schedule.get('phase', '?')} ---"
    )

    spacex_snapshot = None
    if config.SPACEX_IPO_MONITOR_ENABLED and (
        not schedule.get("crypto_only") or config.SPACEX_IPO_CRYPTO_OVERRIDE
    ):
        from modules.spacex_ipo_monitor import get_spacex_ipo_monitor

        spacex_snapshot = get_spacex_ipo_monitor()
    spacex_heartbeat = None
    crypto_gate = crypto_trading_allowed(vol, regime, spacex_snapshot=spacex_snapshot)
    if spacex_snapshot:
        from modules.spacex_ipo_monitor import format_monitor_line

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
            _warn_nonfatal("SpaceX IPO alert error", exc)

    risk_parity_meta = None
    pod_risk_meta = None
    thinking_result = None
    insider_boost = None
    if config.effective_insider_signal_boost_enabled():
        try:
            from modules.insider_signal_handler import apply_insider_signals_to_strategies

            bubble_100 = None
            try:
                from modules.bubble_risk import compute_bubble_risk

                bubble_100 = float(compute_bubble_risk(data, regime).get("score_100") or 0.0)
            except Exception as exc:
                logger.debug("bubble risk score unavailable for insider gating: %s", exc)
            insider_boost = apply_insider_signals_to_strategies(
                bubble_score_100=bubble_100,
                regime=regime,
            )
            summary = insider_boost.get("summary") or ""
            if summary and summary != "insider signal boost off":
                print(f"--- Insider signal boost: {summary} ---")
        except Exception as exc:
            _warn_nonfatal("Insider signal boost error", exc)

    if config.effective_markov_hmm_enabled():
        try:
            from modules.markov_regime import format_markov_hmm_banner, update_markov_hmm

            hmm_bubble = None
            if insider_boost is not None:
                hmm_bubble = insider_boost.get("bubble_score_100")
            if hmm_bubble is None:
                try:
                    from modules.bubble_risk import compute_bubble_risk

                    hmm_bubble = float(
                        compute_bubble_risk(data, display_regime).get("score_100") or 0.0
                    )
                except Exception:
                    hmm_bubble = None
            hmm_pred = update_markov_hmm(
                data,
                regime=display_regime,
                bubble_score_100=hmm_bubble,
                insider_state=insider_boost,
                sentiment=wisdom.get("web_sentiment") or wisdom.get("price_sentiment"),
            )
            if config.effective_markov_hmm_primary_regime() and hmm_pred.get("ok"):
                from modules.markov_regime import apply_hmm_primary_regime

                display_regime = apply_hmm_primary_regime(display_regime)
                regime = display_regime
            hmm_banner = format_markov_hmm_banner()
            if hmm_banner and hmm_pred.get("ok"):
                print(f"--- {hmm_banner} ---")
            if config.effective_markov_hmm_primary_regime():
                print(
                    f"--- Markov HMM primary regime: "
                    f"{'ON' if hmm_pred.get('ok') else 'fallback RHYME'} ---"
                )
        except Exception as exc:
            _warn_nonfatal("Markov HMM", exc)

    dynamic_vti_meta = None
    if config.paper_aggressive_context() and config.PAPER_DYNAMIC_VTI_ENABLED:
        try:
            from modules.dynamic_vti_allocator import (
                build_vti_allocator_context,
                compute_smart_vti_core_pct,
                format_dynamic_vti_banner,
            )

            vti_ctx = build_vti_allocator_context(
                data=data,
                regime=display_regime,
                vol_score=vol_score,
                volatility=vol_label,
                macro_stress=macro_stress_flag,
                insider_state=insider_boost,
            )
            vti_decision = compute_smart_vti_core_pct(equity, vti_ctx)
            dynamic_vti_meta = {
                "pct": vti_decision.pct,
                "base_pct": vti_decision.base_pct,
                "adjustment_pp": vti_decision.adjustment_pp,
                "drivers": list(vti_decision.drivers),
                "detail": dict(vti_decision.detail),
                "banner": format_dynamic_vti_banner(
                    vti_decision.pct, vti_decision.drivers
                ),
            }
            print(f"--- {dynamic_vti_meta['banner']} ---")
        except Exception as exc:
            _warn_nonfatal("Dynamic VTI allocator", exc)

    portfolio_decision_meta = None
    if config.effective_portfolio_constructor_enabled():
        try:
            from modules.portfolio_constructor import (
                build_portfolio_context,
                compute_portfolio_decision,
                format_portfolio_constructor_banner,
                merge_portfolio_sleeve_caps,
            )

            pc_ctx = build_portfolio_context(
                data=data,
                regime=display_regime,
                bubble_score_100=(dynamic_vti_meta or {}).get("detail", {}).get(
                    "bubble_score_100"
                ),
                insider_state=insider_boost,
            )
            pc_decision = compute_portfolio_decision(pc_ctx)
            base_caps = sleeve_cap_pcts or config.fund_allocation_pct()
            sleeve_cap_pcts = merge_portfolio_sleeve_caps(base_caps, pc_decision)
            executor.set_dynamic_sleeve_caps(sleeve_cap_pcts)
            portfolio_decision_meta = {
                "active_sleeve_mult": pc_decision.active_sleeve_mult,
                "stat_arb_mult": pc_decision.stat_arb_mult,
                "short_willingness_mult": pc_decision.short_willingness_mult,
                "drivers": list(pc_decision.drivers),
                "detail": dict(pc_decision.detail),
                "banner": format_portfolio_constructor_banner(pc_decision),
            }
            print(f"--- {portfolio_decision_meta['banner']} ---")
        except Exception as exc:
            _warn_nonfatal("Portfolio constructor error", exc)

    if config.effective_thinking_engine_enabled():
        try:
            from modules.thinking_engine import (
                log_thinking_result,
                maybe_apply_thinking_caps,
                maybe_run_thinking,
            )
            from modules.thinking_news import maybe_run_scheduled_news_thinking

            news_slot = maybe_run_scheduled_news_thinking(data, regime, vol, wisdom)
            if news_slot:
                print(f"--- Thinking news scheduled run started ({news_slot}) ---")

            thinking_result = maybe_run_thinking(
                data,
                regime,
                vol,
                wisdom,
                top_headline=(spacex_heartbeat or {}).get("top_headline"),
            )
            if thinking_result and not thinking_result.get("apply_log"):
                log_thinking_result(thinking_result)
            base_caps = sleeve_cap_pcts or config.fund_allocation_pct()
            sleeve_cap_pcts, thinking_result = maybe_apply_thinking_caps(
                base_caps,
                thinking_result,
                equity=equity,
            )
            if sleeve_cap_pcts:
                executor.set_dynamic_sleeve_caps(sleeve_cap_pcts)
        except Exception as exc:
            _warn_nonfatal("Thinking engine (deploy continues)", exc)

    if config.effective_vol_position_sizing_enabled() or config.effective_loss_cutting_enabled():
        from modules.vol_position_sizing import set_top1_sizing_context

        set_top1_sizing_context(executor, thinking_result)

    if config.effective_risk_parity_enabled():
        from modules.risk_parity_sleeve import (
            apply_risk_parity_cycle,
            format_pod_risk_log,
            format_risk_parity_log,
        )

        sleeve_cap_pcts, pod_scales, risk_parity_meta, pod_risk_meta = apply_risk_parity_cycle(
            data,
            regime,
            vol,
            executor,
            macro_stress=macro_stress_flag,
            equity=equity,
            base_caps=sleeve_cap_pcts or config.fund_allocation_pct(),
        )
        executor.set_dynamic_sleeve_caps(sleeve_cap_pcts)
        executor.set_pod_risk_scales(pod_scales)
        if risk_parity_meta.get("allocation"):
            print(format_risk_parity_log(
                risk_parity_meta["economic_regime"],
                risk_parity_meta["allocation"],
            ))
        pod_log = format_pod_risk_log(pod_risk_meta)
        if pod_log:
            print(f"--- {pod_log} ---")

    listing_snapshot = None
    if config.SPACEX_IPO_LISTING_MONITOR_ENABLED and not schedule.get("crypto_only"):
        from modules.spacex_ipo_listing_monitor import get_spacex_ipo_listing_status

        listing_snapshot = get_spacex_ipo_listing_status(executor=executor)
    spacex_listing_heartbeat = None
    ipo_buy_result = None
    if listing_snapshot:
        from modules.spacex_ipo_listing_monitor import format_listing_line

        print(f"--- {format_listing_line(listing_snapshot)} ---")
        if listing_snapshot.get("ready_to_buy_alpaca"):
            print(f"!!! {config.SPACEX_IPO_TICKER} TRADABLE ON ALPACA - IPO listing live !!!")
        if listing_snapshot.get("ready_to_buy_kraken"):
            k = listing_snapshot.get("kraken") or {}
            print(
                f"!!! {config.SPACEX_IPO_TICKER} TRADABLE ON KRAKEN "
                f"({k.get('wsname') or k.get('pair')}) - buy on Kraken Pro !!!"
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
            _warn_nonfatal("SpaceX listing alert error", exc)
        if listing_snapshot.get("ready_to_buy_kraken"):
            from modules.kraken_ipo_buy import maybe_buy_kraken_spcx

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

    vti_result = None
    if config.REBALANCE_ENABLED and market_open:
        global _strategic_rebalancer
        from modules.operating_layer import run_operating_cycle_live
        from modules.rebalancer import StrategicRebalancer

        if _strategic_rebalancer is None:
            _strategic_rebalancer = StrategicRebalancer()
        op_result = run_operating_cycle_live(
            executor,
            regime=display_regime,
            vol=vol,
            macro_stress=macro_stress_flag,
            vol_score=vol_score,
            bar_date=datetime.date.today(),
            market_open=market_open,
            rebalancer=_strategic_rebalancer,
        )
        if op_result:
            wisdom_rec = op_result.get("wisdom") or {}
            core_tgt = op_result.get("core_target", config.REBALANCE_CORE_TARGET)
            if not op_result.get("skipped"):
                print(
                    f"--- Operating Layer rebalance -> core {core_tgt:.0%} "
                    f"(drift {op_result.get('drift', {}).get('max_drift', 0):.1%}) "
                    f"| wisdom {wisdom_rec.get('action', 'hold')} "
                    f"conv {wisdom_rec.get('conviction', 0):.2f} ---"
                )
            elif wisdom_rec.get("accepted"):
                print(
                    f"--- Operating Layer: wisdom {wisdom_rec.get('action')} "
                    f"conv {wisdom_rec.get('conviction', 0):.2f} (no rebalance trigger) ---"
                )
    elif config.vti_core_enabled() and market_open:
        vti_result = rebalance_vti_core(
            executor,
            market_open=market_open,
            vol_score=vol_score,
            macro_stress=macro_stress_flag,
            volatility=vol_label,
        )
        if config.paper_aggressive_context() and vti_result.get("enabled"):
            print(
                f"--- Dynamic VTI: {vti_result.get('target_pct', 0):.0%} target "
                f"(vol={vol_label}/{vol_score:.4f}, stress={macro_stress_flag}) ---"
            )
        if vti_result.get("action"):
            print(
                f"--- VTI core: {vti_result['action']} {vti_result.get('notional', 0):,.2f} "
                f"-> {vti_result.get('current_value', 0):,.2f} / "
                f"{vti_result.get('target_value', 0):,.2f} "
                f"({vti_result.get('target_pct', config.VTI_CORE_PCT):.0%} target) ---"
            )
        elif vti_result.get("enabled") and not vti_result.get("skipped"):
            print(
                f"--- VTI core: {vti_result.get('current_value', 0):,.2f} / "
                f"{vti_result.get('target_value', 0):,.2f} ---"
            )

    options_result = None
    if config.effective_options_sleeve_enabled() and market_open:
        from modules.options_sleeve import current_vix_level, run_options_sleeve_cycle

        options_result = run_options_sleeve_cycle(
            executor,
            volatility=vol_label,
            vix=current_vix_level(),
            market_open=market_open,
        )

    if config.effective_vol_trading_enabled() and market_open:
        from modules.options_sleeve import current_vix_level
        from modules.volatility_sleeve import run_volatility_sleeve_cycle

        run_volatility_sleeve_cycle(
            executor,
            volatility=vol_label,
            vol_score=vol_score,
            vix=current_vix_level(),
            market_open=market_open,
        )

    social_result = None
    bubble_for_social = None
    try:
        if dynamic_vti_meta and isinstance(dynamic_vti_meta.get("detail"), dict):
            bubble_for_social = dynamic_vti_meta["detail"].get("bubble_score_100")
        elif insider_boost is not None:
            bubble_for_social = insider_boost.get("bubble_score_100")
    except Exception:
        bubble_for_social = None
    if bubble_for_social is None:
        try:
            from modules.bubble_risk import compute_bubble_risk

            bubble_for_social = float(
                compute_bubble_risk(data, display_regime).get("score_100") or 0.0
            )
        except Exception:
            bubble_for_social = None
    social_dynamic_ctx = (
        config.effective_felix_social_dynamic_enabled()
        or config.felix_social_manual_override()
        or config.PAPER_SOCIAL_SLEEVE_ENABLED
    )
    if social_dynamic_ctx:
        from modules.social_sleeve import apply_dynamic_social_gate

        apply_dynamic_social_gate(
            display_regime, bubble_for_social, log=True
        )
    if (
        config.effective_social_sleeve_enabled() or social_dynamic_ctx
    ) and market_open:
        from modules.social_sleeve import run_social_sleeve_cycle

        social_result = run_social_sleeve_cycle(wisdom, executor, market_open=market_open)
        if social_result.get("enabled") is not False or social_result.get("paper_actions"):
            tgt = social_result.get("target") or "cash"
            score = social_result.get("score")
            if config.effective_social_sleeve_enabled():
                print(
                    f"--- Social sleeve: score {score} -> {tgt} "
                    f"(cap {config.effective_social_sleeve_cap_pct():.0%} paper"
                    f"{'' if social_result.get('paper_ok') else ', paper keys missing'}) ---"
                )
            for act in social_result.get("paper_actions") or []:
                print(
                    f"  social paper {act['action']} {act['symbol']} "
                    f"${act.get('notional', 0):,.2f}"
                )
            for act in social_result.get("live_mirror_actions") or []:
                print(
                    f"  social live mirror {act['action']} {act['symbol']} "
                    f"${act.get('notional', 0):,.2f}"
                )

    orb_mom_result = None
    if config.effective_orb_momentum_enabled() and market_open:
        try:
            from modules.orb_momentum_sleeve import run_orb_momentum_cycle

            live_book = bool(
                not config.PAPER_TRADING and config.orb_momentum_live_sleeve_enabled()
            )
            orb_mom_result = run_orb_momentum_cycle(
                data,
                executor,
                regime=display_regime,
                market_open=market_open,
                live=live_book,
                journal=trade_journal,
                yield_gated=yield_gated,
            )
            if orb_mom_result.get("enabled"):
                n_sig = len(orb_mom_result.get("signals") or [])
                n_in = len(orb_mom_result.get("entries") or [])
                n_out = len(orb_mom_result.get("exits") or [])
                skip = orb_mom_result.get("skipped") or ""
                print(
                    f"--- ORB momentum: {n_sig} signal(s), {n_in} entry(ies), "
                    f"{n_out} exit(s)"
                    f"{f' ({skip})' if skip else ''} ---"
                )
                for act in orb_mom_result.get("entries") or []:
                    if act.get("ok"):
                        print(
                            f"  ORB buy {act['symbol']} ${act.get('notional', 0):,.2f} "
                            f"stop={act.get('stop')} tgt={act.get('target')}"
                        )
        except Exception as exc:
            _warn_nonfatal("ORB momentum sleeve", exc)

    sector_rot_result = None
    if config.effective_sector_rotation_enabled() and market_open:
        try:
            from modules.sector_rotation import run_sector_rotation_cycle

            live_book = bool(
                not config.PAPER_TRADING
                and getattr(config, "SECTOR_ROTATION_LIVE_SLEEVE", False)
            )
            sector_rot_result = run_sector_rotation_cycle(
                data,
                executor,
                regime=display_regime,
                market_open=market_open,
                live=live_book,
                journal=trade_journal,
                yield_gated=yield_gated,
            )
            if sector_rot_result.get("enabled"):
                reason = sector_rot_result.get("rebalance_reason") or ""
                skip = sector_rot_result.get("skipped") or ""
                tops = ",".join((sector_rot_result.get("targets") or {}).keys()) or "-"
                print(
                    f"--- Sector rotation: {reason or skip} | "
                    f"cap {float(sector_rot_result.get('cap_pct') or 0):.0%} | "
                    f"targets {tops} ---"
                )
                for act in sector_rot_result.get("actions") or []:
                    if act.get("ok"):
                        print(
                            f"  sector {act['action']} {act['symbol']} "
                            f"${act.get('notional', 0):,.2f}"
                        )
        except Exception as exc:
            _warn_nonfatal("Sector rotation sleeve", exc)

    vol_bo_result = None
    if config.effective_vol_breakout_enabled() and market_open and config.PAPER_TRADING:
        try:
            from modules.vol_breakout_sleeve import run_vol_breakout_cycle

            vol_bo_result = run_vol_breakout_cycle(
                data,
                executor,
                regime=display_regime,
                market_open=market_open,
                live=False,
                journal=trade_journal,
                yield_gated=yield_gated,
            )
            if vol_bo_result.get("enabled"):
                n_sig = len(vol_bo_result.get("signals") or [])
                n_in = len(vol_bo_result.get("entries") or [])
                n_out = len(vol_bo_result.get("exits") or [])
                skip = vol_bo_result.get("skipped") or ""
                print(
                    f"--- Vol breakout: {n_sig} signal(s), {n_in} entry(ies), "
                    f"{n_out} exit(s)"
                    f"{f' ({skip})' if skip else ''} ---"
                )
                for act in vol_bo_result.get("entries") or []:
                    if act.get("ok"):
                        print(
                            f"  vol-bo buy {act['symbol']} ${act.get('notional', 0):,.2f} "
                            f"ATRx{act.get('atr_expand')} stop={act.get('stop')} "
                            f"tgt={act.get('target')}"
                        )
        except Exception as exc:
            _warn_nonfatal("Vol breakout sleeve", exc)

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
        market_open=equity_scans,
    )
    if config.effective_crypto_enabled():
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
    else:
        c = 0
    s = 0
    nyse_trades = 0
    gp_result = {"enabled": False, "signals": gp_signals, "actions": []}

    market_open = is_equity_market_open(executor.client)
    executor.equity_session_open = market_open
    if equity_scans:
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
        if config.effective_equity_pairs_enabled():
            nyse_trades = run_equity_pairs_strategy(
                data,
                executor,
                regime,
                now,
                pair_cooldown,
                log_fn=_equity_pair_log,
                portfolio_manager=portfolio_manager,
                yield_gated=yield_gated,
            )
        else:
            # IPO trim helper is optional; older stacks may not include it.
            try:
                from modules.pipeline_strategies import (  # type: ignore
                    run_ipo_safety_trims as run_ipo_safety_trims,
                )
            except Exception as exc:
                logger.debug("IPO safety trims unavailable: %s", exc)

                def run_ipo_safety_trims(_data, _executor, **_kw):  # type: ignore
                    return 0

            from modules.pipeline_strategies import run_nyse_momentum_and_stat_arb

            run_ipo_safety_trims(data, executor)
            nyse_trades = run_nyse_momentum_and_stat_arb(
                data,
                executor,
                regime,
                now,
                pair_cooldown,
                log_fn=_equity_log,
                portfolio_manager=portfolio_manager,
                yield_gated=yield_gated,
                full_data=data,
            )
            if config.effective_international_sleeve_enabled():
                nyse_trades += run_international_strategy(
                    data,
                    executor,
                    regime,
                    now,
                    pair_cooldown,
                    log_fn=_equity_log,
                    portfolio_manager=portfolio_manager,
                    yield_gated=yield_gated,
                    full_data=data,
                )
            if config.effective_bond_sleeve_enabled():
                from modules.macro_signals import evaluate, load_daily_matrix
                from modules.options_sleeve import current_vix_level

                try:
                    macro_daily = load_daily_matrix(days=450)
                    macro_window = macro_daily
                    macro_eval = evaluate(macro_daily, regime)
                    macro_stress_flag = bool(macro_eval.get("stress"))
                except Exception as exc:
                    logger.debug("macro daily matrix (450d) load failed, using intraday window: %s", exc)
                    macro_window = data
                    macro_stress_flag = False
                nyse_trades += run_bond_strategy(
                    data,
                    executor,
                    regime,
                    now,
                    pair_cooldown,
                    log_fn=_equity_log,
                    volatility=vol,
                    vix=current_vix_level(),
                    macro_stress=macro_stress_flag,
                    macro_window=macro_window,
                )
        if config.effective_opportunistic_short_enabled():
            try:
                from modules.opportunistic_short_sleeve import run_opportunistic_short_strategy

                run_opportunistic_short_strategy(
                    data,
                    executor,
                    regime,
                    now,
                    pair_cooldown,
                    log_fn=_equity_log,
                    volatility=vol,
                )
            except Exception as exc:
                _warn_nonfatal("Opportunistic short strategy", exc)
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
    elif schedule.get("equity_prep"):
        print(
            f"--- Open prep: refreshing regime; SPY/NYSE scans start "
            f"{config.EQUITY_SCAN_AFTER_OPEN_MIN}m after the bell ---"
        )
        if config.game_plan_active():
            gp_result = run_game_plan_cycle(
                executor,
                regime,
                market_open=False,
                signals=gp_signals,
            )
    else:
        if config.effective_crypto_enabled():
            print("--- Overnight: crypto only (SPY/NYSE scans off) ---")
        else:
            print("--- Overnight: equity scans off (crypto sleeve disabled) ---")
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
            from modules.kraken_autopilot import format_autopilot_line, run_kraken_autopilot

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
            log_subsystem_warning("kraken_autopilot", "Autopilot cycle failed", exc)
            print(f"--- Kraken autopilot error (non-fatal): {exc} ---")

    sleeves = executor.sleeve_snapshot()
    metal_line = ""
    if config.metal_sleeve_enabled() and "metal_value" in sleeves:
        metal_line = (
            f" | Metal ${round(sleeves['metal_value'], 2)}/"
            f"${round(sleeves['metal_cap'], 2)}"
        )
    total_entries = int(c) + int(s) + int(nyse_trades)
    if total_entries > 0:
        entry_skip_reason = "traded"
    else:
        entry_skip_reason = summarize_entry_skip_reason(
            data,
            executor,
            regime,
            now,
            pair_cooldown,
            yield_gated=yield_gated,
            market_open=equity_scans,
            volatility=vol,
            wisdom_paused=bool(wisdom.get("wisdom_paused")),
        )
    print(f"--- Entry gates: {entry_skip_reason} ---")
    from modules.entry_skip_tracker import (
        maybe_emit_daily_summary,
        record_cycle,
    )

    entry_skip_daily = record_cycle(entry_skip_reason)
    maybe_emit_daily_summary()
    print(
        f"=== CYCLE STATUS: spy={s} nyse={nyse_trades} crypto={c} | "
        f"gates={entry_skip_reason} | today skipped={entry_skip_daily.get('skipped_cycles', 0)} "
        f"({entry_skip_daily.get('top_skip', '-')}) ==="
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
            f"game_plan stress={sig.get('stress')} "
            f"gate_raw={sig.get('yield_gate')} gate={yield_gated} "
            f"metal=${gp_result.get('metal_value', 0)}"
        )
    extra_notes = []
    if macro_ctx.get("active"):
        ev = macro_ctx.get("event") or {}
        extra_notes.append(
            f"macro_guard={ev.get('name')} x{macro_ctx.get('sizing_scale', 1):.2f}"
        )
    if sleeve_pnl:
        extra_notes.append(f"sleeve_pnl={format_sleeve_pnl_line(sleeve_pnl)}")
    if extra_notes:
        gp_notes = f"{gp_notes}; {'; '.join(extra_notes)}".strip("; ")
    trade_journal.log_cycle(
        regime,
        equity,
        cash,
        c,
        nyse_trades,
        notes=(
            f"spy={s} crypto_cap={config.CRYPTO_SLEEVE_CAP_PCT:.2%} "
            f"nyse_cap={config.effective_nyse_sleeve_cap_pct():.2%}; "
            f"entry_gates={entry_skip_reason}; {gp_notes}"
        ),
    )
    try:
        alerts.maybe_daily_summary(equity, cash, regime, False)
    except Exception as exc:
        _warn_nonfatal("Alert error", exc)
    try:
        from modules.periodic_summary import send_periodic_summary

        # Paper research: compact 3h pulse. Live uses send_daily_live_summary instead.
        if config.PAPER_TRADING or config.paper_chase_mode_enabled():
            send_periodic_summary(
                float(getattr(config, "TELEGRAM_PERIODIC_SUMMARY_HOURS", 3.0) or 3.0),
                equity=equity,
                cash=cash,
                regime=display_regime,
                sleeves=sleeves,
            )
    except Exception as exc:
        _warn_nonfatal("Periodic Telegram summary", exc)
    try:
        from modules.periodic_summary import send_daily_live_summary

        if not config.PAPER_TRADING and not market_open:
            send_daily_live_summary(
                equity=equity,
                cash=cash,
                regime=display_regime,
                sleeves=sleeves,
                market_open=market_open,
            )
    except Exception as exc:
        _warn_nonfatal("Live daily Telegram summary", exc)
    try:
        from modules.sharpe_history import update_sharpe_history

        # EOD Sharpe snapshot (once per ET day; skips while equity session open).
        update_sharpe_history(equity, market_open=market_open)
    except Exception as exc:
        _warn_nonfatal("Sharpe history update", exc)
    try:
        if config.telegram_weekly_summary_enabled():
            from modules.weekly_telegram_summary import send_weekly_telegram_summary

            send_weekly_telegram_summary(
                equity=equity,
                cash=cash,
                regime=display_regime,
                wisdom=wisdom,
                sleeves=sleeves,
                market_open=market_open,
            )
    except Exception as exc:
        _warn_nonfatal("Weekly Telegram summary", exc)
    try:
        from modules.telegram_commands import maybe_poll_telegram_commands

        n = maybe_poll_telegram_commands(
            equity=equity,
            cash=cash,
            regime=display_regime,
        )
        if n:
            print(f"--- Telegram commands handled: {n} ---")
    except Exception as exc:
        _warn_nonfatal("Telegram commands", exc)
    try:
        from modules.weekly_report import maybe_generate_weekly_report

        maybe_generate_weekly_report(
            equity=equity,
            cash=cash,
            regime=display_regime,
            wisdom=wisdom,
            sleeves=sleeves,
            market_open=market_open,
        )
    except Exception as exc:
        _warn_nonfatal("Weekly report", exc)
    _write_heartbeat(
        display_regime,
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
        macro_event=macro_ctx,
        sleeve_pnl=sleeve_pnl,
        scan_schedule=schedule,
        social_sleeve=social_result,
        vti_core=vti_result,
        sleeve_caps=sleeve_cap_pcts,
        dynamic_vol_score=vol_score
        if config.DYNAMIC_SLEEVE_CAPS_ENABLED or config.paper_aggressive_context()
        else None,
        thinking_engine=thinking_result,
        risk_parity={
            **(risk_parity_meta or {}),
            "pod": pod_risk_meta,
        }
        if (risk_parity_meta or pod_risk_meta)
        else None,
        entry_skip_reason=entry_skip_reason,
        entry_skip_daily=entry_skip_daily,
        dynamic_vti=dynamic_vti_meta,
        heartbeat_data=data,
        heartbeat_regime=display_regime,
        insider_state=insider_boost,
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
            _warn_nonfatal("Monthly wisdom alert error", exc)


def _print_kraken_banner():
    if not config.KRAKEN_AUTOPILOT_ENABLED:
        return
    from modules.kraken_capabilities import probe_kraken_capabilities
    from modules.kraken_spot import autopilot_enabled, trading_allowed

    if not autopilot_enabled():
        print("--- Kraken autopilot: enabled but API keys missing ---")
        return
    mode = "DRY-RUN" if config.KRAKEN_DRY_RUN else (
        "LIVE" if trading_allowed() else "BLOCKED (set ALLOW_KRAKEN_TRADING=yes)"
    )
    print(
        f"--- Kraken autopilot: {mode} | max ${config.KRAKEN_MAX_ORDER_USD:.0f}/order | "
        f"cycle buy budget ${config.KRAKEN_CYCLE_BUDGET_USD:.0f} ---"
    )
    cap = probe_kraken_capabilities()
    if not cap.get("crypto_ok"):
        print("!!! Kraken crypto API failed - run scripts/account/preflight_kraken.py !!!")
    if not cap.get("xstock_ok"):
        print(
            "!!! Kraken xStocks API off - SPY/NYSE will not auto-trade "
            "(enable tokenized permission on API key) !!!"
        )


def _alpaca_startup_hint(exc: Exception | None = None) -> str:
    if config.PAPER_TRADING:
        return (
            "Use paper Alpaca keys with PAPER_TRADING=true "
            "(endpoint paper-api.alpaca.markets). "
            "Live keys need PAPER_TRADING=false."
        )
    return (
        "Use live Alpaca keys with PAPER_TRADING=false and ALLOW_LIVE_TRADING=yes "
        "(endpoint api.alpaca.markets). Paper keys return 401 on the live endpoint."
    )


def _print_account_startup_summary(equity: float, cash: float | None = None) -> None:
    mode = "PAPER" if config.PAPER_TRADING else "LIVE"
    config.configure_account_profile(equity, cash=cash)
    vti = config.effective_vti_core_pct(equity=equity)
    cash_line = ""
    if cash is not None and equity > 0:
        cash_pct = float(cash) / float(equity)
        cash_line = f" | cash ${cash:,.2f} ({cash_pct:.0%})"
    print(
        f"--- Account summary: ${equity:,.2f} equity | {mode} | "
        f"core target {vti:.0%}{cash_line} | "
        f"risk {config.effective_risk_per_trade():.0%}/trade | "
        f"min order ${config.effective_min_notional(equity):.2f} ---"
    )
    if config.paper_aggressive_context():
        try:
            from modules.deployment_monitor import excess_cash_warning, record_cash_snapshot

            if cash is not None:
                record_cash_snapshot(equity, cash)
            warn = excess_cash_warning(equity, cash)
            if warn:
                print(f"!!! {warn} !!!")
            deploy_line = config.format_high_cash_deploy_banner(equity=equity, cash=cash)
            if deploy_line:
                print(f">>> {deploy_line} <<<")
        except Exception as exc:
            _warn_nonfatal("deployment cash startup warning", exc)
    if config.is_small_account(equity):
        if config.live_conservative_profile_active():
            print(f"--- {config.format_live_conservative_banner()} ---")
        else:
            print(
                f"--- Small account profile (<${config.SMALL_ACCOUNT_EQUITY_THRESHOLD:,.0f}): "
                f"max order ${config.effective_max_notional_per_order(equity):,.2f} | "
                f"VTI {vti:.0%} ---"
            )


def _print_startup_banner(startup_equity: float | None = None):
    mode = "PAPER" if config.PAPER_TRADING else "LIVE"
    endpoint = (
        "paper-api.alpaca.markets"
        if config.PAPER_TRADING
        else "api.alpaca.markets (LIVE)"
    )
    if config.PAPER_TRADING:
        print("=" * 60)
        print("=== BOT STARTING IN PAPER MODE (simulated money) ===")
        print("=" * 60)
    else:
        print("=" * 60)
        print("=== BOT STARTING IN LIVE MODE (REAL MONEY) ===")
        print("=" * 60)
    print("--- Starting 24/7 Weinstein-Iteration Engine ---")
    print(f"--- Alpaca: {mode} ({endpoint}) ---")
    print(config.format_paper_live_profile_line())
    if config.is_realistic_research_active():
        for line in config.format_realistic_research_startup_lines():
            print(line)
    if config.paper_aggressive_context():
        vti_mode = (
            f"fixed {config.PAPER_VTI_CORE_PCT:.0%} VTI"
            if not config.PAPER_DYNAMIC_VTI_ENABLED
            else f"Smart Dynamic VTI {config.DYNAMIC_VTI_PAPER_FLOOR:.0%}-{config.DYNAMIC_VTI_PAPER_CEILING:.0%}"
        )
        soft = (
            f" | regime sizing A={config.PAPER_REGIME_A_SIZING_MULT:.1f} "
            f"C={config.PAPER_REGIME_C_SIZING_MULT:.1f} D={config.PAPER_REGIME_D_SIZING_MULT:.1f} "
            f"E={config.PAPER_REGIME_E_SIZING_MULT:.1f} B={config.PAPER_REGIME_B_SIZING_MULT:.1f}"
            if config.effective_regime_dynamic_sizing()
            else (
                f" | soft-pause {config.PAPER_SOFT_PAUSE_SIZING_MULT:.0%}"
                if config.effective_paper_soft_pause()
                else ""
            )
        )
        print(
            f"--- Paper SHARPE CHASE: {vti_mode} | "
            f"active boost x{config.PAPER_ACTIVE_SLEEVE_BOOST} | "
            f"wisdom floor x{config.PAPER_WISDOM_SIZING_FLOOR}{soft} | "
            f"crypto vol-only={config.effective_crypto_vol_only()} | "
            f"cycle {config.CYCLE_INTERVAL_SEC}s | refresh {config.REFRESH_INTERVAL}s ---"
        )
        for line in config.paper_frequency_mode_lines():
            print(line)
        research_line = config.format_research_mode_banner()
        if research_line:
            print(f"--- {research_line} ---")
        try:
            from modules.pipeline_strategies import load_pipeline_data
            from modules.market_context import current_regime_from_data
            from modules.regime_sizing import format_regime_sizing_line

            _banner_data = load_pipeline_data()
            _banner_regime = current_regime_from_data(_banner_data)
            if _banner_regime:
                print(f"--- {format_regime_sizing_line(_banner_regime)} ---")
        except Exception as exc:
            _warn_nonfatal("regime sizing startup banner", exc)
        try:
            from modules.positioning_overlay import format_positioning_banner

            _cot_line = format_positioning_banner()
            if _cot_line:
                print(f"--- {_cot_line} ---")
        except Exception as exc:
            _warn_nonfatal("positioning overlay startup banner", exc)
        try:
            from modules.core_allocator import format_core_allocator_banner

            _core_line = format_core_allocator_banner()
            if _core_line and not (
                config.paper_aggressive_context() and config.PAPER_DYNAMIC_VTI_ENABLED
            ):
                print(f"--- {_core_line} ---")
        except Exception as exc:
            _warn_nonfatal("core allocator startup banner", exc)
        if config.paper_aggressive_context() and config.PAPER_DYNAMIC_VTI_ENABLED:
            try:
                from modules.dynamic_vti_allocator import format_startup_smart_vti_banner
                from modules.pipeline_strategies import load_pipeline_data
                from modules.market_context import (
                    cross_asset_vol_score,
                    current_regime_from_data,
                    get_volatility,
                )

                _vti_data = load_pipeline_data()
                _vti_regime = current_regime_from_data(_vti_data)
                _vti_vol = get_volatility(_vti_data) if _vti_data is not None else "Low"
                _vti_line = format_startup_smart_vti_banner(
                    data=_vti_data,
                    equity=startup_equity,
                    regime=_vti_regime,
                    vol_score=cross_asset_vol_score(_vti_data)
                    if _vti_data is not None and not getattr(_vti_data, "empty", True)
                    else None,
                    volatility=_vti_vol,
                )
                if _vti_line:
                    print(f"--- {_vti_line} ---")
                print(f"--- {config.format_smart_dynamic_vti_lock_banner()} ---")
            except Exception as exc:
                _warn_nonfatal("Smart Dynamic VTI startup banner", exc)
        try:
            from modules.operating_layer import format_operating_layer_banner

            _op_line = format_operating_layer_banner()
            if _op_line:
                print(f"--- {_op_line} ---")
        except Exception as exc:
            _warn_nonfatal("operating layer startup banner", exc)
    ws_line = format_status_line()
    if ws_line:
        print(f"--- {ws_line} ---")
    try:
        from modules.kimi_client import format_kimi_deep_thinker_banner

        kimi_line = format_kimi_deep_thinker_banner()
        if kimi_line and config.effective_kimi_deep_thinker_enabled():
            print(kimi_line)
    except ImportError:
        pass
    try:
        from modules.insider_monitor import format_insider_monitor_banner

        insider_line = format_insider_monitor_banner()
        if insider_line and config.effective_insider_monitor_enabled():
            print(insider_line)
        if config.effective_insider_signal_boost_enabled():
            from modules.insider_signal_handler import format_insider_boost_startup_banner

            boost_line = format_insider_boost_startup_banner()
            if boost_line:
                print(f">>> {boost_line} <<<")
    except ImportError:
        pass
    _print_kraken_banner()
    alloc = config.fund_allocation_pct()
    if config.vti_core_enabled():
        print(
            f"--- Fund: {alloc['vti_core']:.0%} {config.VTI_CORE_SYMBOL} core | "
            f"active SPY {alloc['spy']:.0%} | crypto {alloc['crypto']:.0%} | "
            f"NYSE {alloc['nyse']:.0%} | cash {alloc['cash_buffer']:.0%} ---"
        )
    else:
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
    risk_pct = config.effective_risk_per_trade()
    spy_ma = config.effective_spy_ma_window()
    nyse_ma = config.effective_nyse_ma_window()
    print(
        f"--- SPY MA{spy_ma} | crypto Z-pairs | "
        f"NYSE MA{nyse_ma} | "
        f"{risk_pct:.1%}/trade within sleeve ---"
    )
    if config.is_small_account():
        if config.live_conservative_profile_active():
            print(f"--- {config.format_live_conservative_banner()} ---")
        else:
            print(
                f"--- Small account safety: max ${config.effective_max_notional_per_order():,.2f}/order | "
                f"VTI {config.vti_core_allocation_pct():.0%} core ---"
            )
    print(
        f"--- Order sizing: scales with equity (ref ${config.REFERENCE_EQUITY:,.0f} -> "
        f"min ${config.MIN_NOTIONAL:.0f}; $100 account -> min "
        f"${config.effective_min_notional(100):.2f}) ---"
    )
    if config.ALPACA_CRYPTO_FEE_AWARE:
        print(
            f"--- Alpaca fees: equities $0 | crypto taker "
            f"{config.ALPACA_CRYPTO_TAKER_FEE_PCT:.2%}/leg reserved in sizing ---"
        )
    print(f"--- Sentiment: {config.SENTIMENT_SOURCE} (RHYME regimes) ---")
    if config.FELIX_SYNC_ENABLED or config.FELIX_SENTIMENT_ENABLED:
        print(
            f"--- Felix channel: sync={'on' if config.FELIX_SYNC_ENABLED else 'off'} "
            f"every {config.FELIX_SYNC_INTERVAL_HOURS}h | "
            f"blend={'on' if config.FELIX_SENTIMENT_ENABLED else 'off'} "
            f"({config.FELIX_SENTIMENT_BLEND_WEIGHT:.0%} weight) -> "
            f"{config.FELIX_MANIFEST_FILE} ---"
        )
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
    if config.MACRO_EVENT_GUARD_ENABLED:
        print(
            f"--- Macro event guard: {config.MACRO_EVENT_HOURS_BEFORE}h window | "
            f"sizing x{config.MACRO_EVENT_SIZING_SCALE} (NFP/CPI/FOMC/PPI/GDP) ---"
        )
    if config.COST_BASIS_AWARE_ENABLED:
        print(
            f"--- Cost basis aware: underwater buys x{config.UNDERWATER_SIZING_SCALE} | "
            f"block discretionary sells below cost: {config.DISCRETIONARY_SELL_BELOW_COST} ---"
        )
    if config.SCAN_SCHEDULE_ENABLED:
        print(
            f"--- Scan schedule: crypto overnight every "
            f"{config.CRYPTO_ONLY_CYCLE_INTERVAL_SEC // 60}m | equity prep "
            f"{config.EQUITY_SCAN_BEFORE_OPEN_MIN}m before open | "
            f"SPY/NYSE {config.EQUITY_SCAN_AFTER_OPEN_MIN}m after open -> close "
            f"(cycle {config.CYCLE_INTERVAL_SEC}s) ---"
        )
    else:
        print("--- Scan schedule: off (legacy: equity scans when session open) ---")
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
    if config.SOCIAL_SLEEVE_ENABLED:
        from modules.social_sleeve import social_paper_available

        paper_note = "yes" if social_paper_available() else "need PAPER_APCA_* or SOCIAL_APCA_*"
        print(
            f"--- Social sleeve: {config.SOCIAL_SLEEVE_CAP_PCT:.0%} on paper ({paper_note}) | "
            f"live mirror {config.SOCIAL_MIRROR_TO_LIVE_PCT:.0%} of social cap | "
            f"GLD/XLE/SPY (no IPOs) ---"
        )
    if config.SPACEX_IPO_LISTING_MONITOR_ENABLED:
        print(
            f"--- SpaceX IPO listing: SEC + Alpaca scan for {config.SPACEX_IPO_TICKER} "
            f"(expected {config.SPACEX_IPO_EXPECTED_DATE}) -> "
            f"{config.SPACEX_IPO_LISTING_CACHE_FILE} ---"
        )
        if config.KRAKEN_SPCX_BUY_ENABLED:
            print(
                f"--- Kraken SPCX live buy: ${config.KRAKEN_SPCX_BUY_USD:,.0f} "
                f"when SPCX/SPCXx appears on Kraken Pro API ---"
            )
    print(f"--- Journal: {config.PAPER_JOURNAL_CSV} | Heartbeat: {config.HEARTBEAT_FILE} ---")
    if alerts.alerts_configured():
        print(f"--- Alerts: on - {config.telegram_alert_policy_summary()} ---")
        if config.telegram_weekly_summary_enabled():
            print(
                f"--- Weekly Telegram: Fridays after {config.TELEGRAM_WEEKLY_SUMMARY_TIME} ET ---"
            )
    else:
        print("--- Alerts: off (set TELEGRAM_* or SMTP_* in .env) ---")
    if not config.PAPER_TRADING and config.ALLOW_LIVE_TRADING:
        print("!!! WARNING: Live trading enabled (ALLOW_LIVE_TRADING=yes) !!!")


def _confirm_live_trading_startup(equity: float) -> None:
    """One-time loud warning and 10s abort window before the live main loop."""
    global _live_startup_confirmed
    if config.PAPER_TRADING or _live_startup_confirmed or not config.ALLOW_LIVE_TRADING:
        return
    if os.getenv("PORTAL_MANAGED_BOT", "").strip().lower() in ("1", "true", "yes"):
        _live_startup_confirmed = True
        print(
            f"--- Live loop starting (portal-managed, ~${equity:,.2f} account) ---\n",
            flush=True,
        )
        return
    profile = config.configure_account_profile(equity)
    print("")
    print("=" * 60)
    print(
        f"=== LIVE TRADING ENABLED ON REAL MONEY ACCOUNT === "
        f"Equity: ${equity:,.2f}"
    )
    print("=" * 60)
    if profile.get("small_account"):
        if profile.get("live_conservative"):
            print(f"--- {config.format_live_conservative_banner()} ---")
        else:
            print(
                f"--- Small account safety (<${config.SMALL_ACCOUNT_EQUITY_THRESHOLD:,.0f}): "
                f"{profile['risk_per_trade']:.0%} risk | "
                f"max ${profile['max_notional_per_order']:,.2f}/order | "
                f"VTI {profile['vti_core_pct']:.0%} ---"
            )
    print("--- Press Ctrl+C within 10 seconds to abort ---")
    for remaining in range(10, 0, -1):
        print(f"Starting live trading loop in {remaining}s...")
        time.sleep(1)
    _live_startup_confirmed = True
    print("--- Live loop starting ---\n")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="24/7 integrated fund trading loop")
    parser.add_argument(
        "--cycles",
        type=int,
        default=0,
        help="Exit after N main-loop cycles (0 = run forever)",
    )
    cli_args = parser.parse_args()

    install_safe_stdout()
    from pathlib import Path

    if getattr(sys, "frozen", False):
        from modules.runtime_paths import resolve_data_root

        try:
            os.chdir(resolve_data_root())
        except OSError:
            pass

    config.configure_heartbeat_path()
    db_path = config.ensure_market_db()
    setup_logging(log_dir=Path("logs"))
    config.ensure_sentiment_dirs()
    try:
        config.validate_alpaca_config()
    except ValueError as exc:
        fatal_startup(
            f"{exc}\n\nCopy .env.example to dist\\.env and add your Alpaca keys, "
            "or run from stock-bot with .env in the project root."
        )
    config.apply_best_paper_config_if_enabled()
    chase_extras = config.init_paper_chase_if_enabled()
    if chase_extras:
        print(f"--- Paper chase extras: {', '.join(chase_extras)} ---")
    startup_equity = None
    startup_cash = None
    try:
        _startup_executor = _make_executor()
        _startup_acct = _startup_executor._get_account()
        startup_equity = float(_startup_acct.equity)
        startup_cash = float(_startup_acct.cash)
        config.configure_account_profile(startup_equity, cash=startup_cash)
    except AlpacaAuthError as exc:
        fatal_startup(
            f"Alpaca authentication failed at startup: {exc}\n\n{_alpaca_startup_hint(exc)}"
        )
    except Exception as exc:
        print(f"[WARN] Could not load Alpaca account at startup: {exc}")
        print(f"       {_alpaca_startup_hint(exc)}")
    if config.effective_real_time_websocket_enabled():
        start_realtime_feed()
        time.sleep(1.5)
    _print_startup_banner(startup_equity)
    if startup_equity is not None:
        _print_account_startup_summary(startup_equity, cash=startup_cash)
    if startup_equity is not None:
        _confirm_live_trading_startup(startup_equity)
    from modules.dashboard_launcher import maybe_launch_dashboard

    maybe_launch_dashboard()
    trade_journal.log_event("startup", notes="run_all.py started")
    cycle_count = 0

    import signal

    def _handle_shutdown(signum, _frame):
        logger.info("Shutdown signal %s - exiting", signum)
        trade_journal.log_event("shutdown", notes=f"signal {signum}")
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_shutdown)

    from modules.heartbeat_watchdog import (
        CycleWatchdog,
        watchdog_enabled,
        watchdog_timeout_sec,
    )

    watchdog = None
    if watchdog_enabled():
        watchdog = CycleWatchdog(timeout_sec=watchdog_timeout_sec())
        watchdog.start()
        print(
            f"--- Heartbeat watchdog: ON (auto-restart if a cycle stalls "
            f">{watchdog.timeout_sec:.0f}s) ---"
        )

    while True:
        if watchdog is not None:
            watchdog.begin_cycle("main")
        try:
            main()
        except AlpacaAuthError as e:
            log_event("alpaca_auth_failure", error=str(e))
            logger.critical(
                "Alpaca authentication failed: %s - verify APCA_API_KEY_ID / "
                "APCA_API_SECRET_KEY in .env (paper keys when PAPER_TRADING=true)",
                e,
            )
            trade_journal.log_event("error", notes=f"Alpaca auth failure: {e}")
            sys.exit(1)
        except AlpacaCriticalError as e:
            log_event("alpaca_critical", error=str(e))
            _record_cycle_error(str(e))
            logger.warning(
                "Alpaca API failure after retries (skipping cycle, bot continues): %s",
                e,
            )
            trade_journal.log_event("error", notes=f"Alpaca API (transient): {e}")
        except AlpacaValidationError as e:
            log_event("alpaca_validation", error=str(e))
            logger.info("Alpaca order validation skipped (cycle continues): %s", e)
            trade_journal.log_event("error", notes=f"Alpaca validation skip: {e}")
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt - shutting down")
            trade_journal.log_event("shutdown", notes="keyboard interrupt")
            break
        except Exception as e:
            tb = traceback.format_exc()
            _record_cycle_error(str(e))
            log_event("cycle_error", error=str(e), exception_type=type(e).__name__)
            logger.exception("Cycle error: %s", e)
            notes = str(e)
            if tb.strip():
                notes = f"{notes}\n{tb[-1500:]}"
            trade_journal.log_event("error", notes=notes)
        finally:
            if watchdog is not None:
                watchdog.end_cycle()
        cycle_count += 1
        if cli_args.cycles and cycle_count >= cli_args.cycles:
            logger.info("Completed %s cycle(s); exiting (--cycles)", cli_args.cycles)
            break
        time.sleep(cycle_sleep_seconds(_last_cycle_schedule))
