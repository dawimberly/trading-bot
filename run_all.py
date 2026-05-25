"""24/7 live trading loop: refresh data, detect regime, run crypto and equity strategies.

Run: python run_all.py
Preflight: python scripts/account/preflight.py
"""

import datetime
import json
import time

import config
from modules.alpaca_executor import AlpacaExecutor
from modules.data_loader import load_close_matrix
from modules.data_refresh import RefreshScheduler
from modules.market_context import get_market_regime, get_sentiment, get_volatility
from modules.pipeline_strategies import run_crypto_strategy, run_equity_strategy
from modules.portfolio_manager import PortfolioManager
from modules.position_exits import run_position_exits
from modules.risk_management import RiskManager
from modules import trade_journal
from modules import alerts

pair_cooldown = {}
refresh_scheduler = RefreshScheduler()
risk_manager = RiskManager(max_drawdown_pct=config.MAX_DRAWDOWN_PCT)
portfolio_manager = PortfolioManager(ledger_file=config.LEDGER_PATH)


def log_trade(symbol, side, regime):
    with open(config.TRADE_HISTORY_LOG, "a", encoding="utf-8") as f:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"{ts} | {side.upper()} | {symbol} | Regime: {regime}\n")


def _crypto_log(symbol, side, regime, pair_key, z, notional=""):
    print(
        f"!!! CRYPTO SIGNAL: {pair_key} | Z={round(z, 2)} | "
        f"{side.upper()} | ${notional} | Regime: {regime}"
    )
    log_trade(symbol, side, regime)
    trade_journal.log_signal(symbol, side, regime, pair_key, z, _last_equity, notional)


def _equity_log(symbol, side, regime, pair_key, _z, notional=""):
    print(
        f"!!! EQUITY SIGNAL: {symbol} above MA50 | BUY | ${notional} | Regime: {regime}"
    )
    log_trade(symbol, side, regime)
    trade_journal.log_signal(symbol, side, regime, pair_key, 0.0, _last_equity, notional)


_last_equity = 0.0


def _write_heartbeat(regime, equity, cash, crypto_trades, equity_trades, halted, market_open):
    payload = {
        "timestamp": datetime.datetime.now().isoformat(),
        "regime": regime,
        "equity": equity,
        "cash": cash,
        "crypto_trades_last_cycle": crypto_trades,
        "equity_trades_last_cycle": equity_trades,
        "equity_session_open": market_open,
        "halted": halted,
        "paper": config.PAPER_TRADING,
    }
    with open(config.HEARTBEAT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def main():
    global _last_equity
    now_ts = datetime.datetime.now()
    executor = AlpacaExecutor()
    market_open = refresh_scheduler.sync(executor.client, now_ts)

    account = executor.client.get_account()
    equity = float(account.equity)
    cash = float(account.cash)
    _last_equity = equity

    halted = not risk_manager.check_drawdown(equity)
    if halted:
        peak = risk_manager.peak_equity or equity
        dd = (peak - equity) / peak if peak else 0
        print("!!! RISK HALT: Max drawdown reached. Skipping cycle. !!!")
        trade_journal.log_event("halt", equity=equity, cash=cash, notes="drawdown limit")
        alerts.notify_halt(equity, peak, dd)
        try:
            alerts.maybe_daily_summary(equity, cash, "HALTED", True)
        except Exception as e:
            print(f"Alert error (non-fatal): {e}")
        _write_heartbeat("HALTED", equity, cash, 0, 0, True, market_open)
        return

    alerts.clear_halt_flag()

    print("--- Pipeline Cycle: " + str(datetime.datetime.now()) + " ---")
    data = load_close_matrix()
    if data.empty or len(data) < 20:
        print("Insufficient market data. Skipping cycle.")
        trade_journal.log_event("skip", equity=equity, notes="empty or short data")
        return

    regime = get_market_regime(get_sentiment(data), get_volatility(data))
    print(f"--- Regime: {regime} | Equity session: {'OPEN' if market_open else 'CLOSED'} ---")

    exits = run_position_exits(
        executor, risk_manager, trade_journal, equity_session_open=market_open
    )
    if exits:
        print(f"--- Stop-loss exits: {exits} ---")

    now = datetime.datetime.now()
    c = run_crypto_strategy(
        data,
        executor,
        regime,
        now,
        pair_cooldown,
        log_fn=_crypto_log,
        portfolio_manager=portfolio_manager,
    )
    e = 0
    if market_open:
        e = run_equity_strategy(
            data,
            executor,
            regime,
            now,
            pair_cooldown,
            log_fn=_equity_log,
            portfolio_manager=portfolio_manager,
        )
    else:
        print("--- Equity session closed; skipping equity scan ---")
    print(f"--- Crypto trades: {c} | Equity trades: {e} ---")
    trade_journal.log_cycle(regime, equity, cash, c, e)
    try:
        alerts.maybe_daily_summary(equity, cash, regime, False)
    except Exception as e:
        print(f"Alert error (non-fatal): {e}")
    _write_heartbeat(regime, equity, cash, c, e, False, market_open)


def _print_startup_banner():
    mode = "PAPER" if config.PAPER_TRADING else "LIVE"
    print("--- Starting 24/7 Weinstein-Iteration Engine ---")
    print(f"--- Alpaca mode: {mode} (Kraken not used) ---")
    print(f"--- Risk: {config.RISK_PER_TRADE:.0%}/trade, stop {config.STOP_LOSS_PCT:.0%}, max DD {config.MAX_DRAWDOWN_PCT:.0%} ---")
    print(f"--- Journal: {config.PAPER_JOURNAL_CSV} | Heartbeat: {config.HEARTBEAT_FILE} ---")
    if alerts.alerts_configured():
        print("--- Alerts: enabled (Telegram and/or email) ---")
    else:
        print("--- Alerts: off (set TELEGRAM_* or SMTP_* in .env) ---")
    if not config.PAPER_TRADING:
        print("!!! WARNING: Live trading enabled !!!")


if __name__ == "__main__":
    _print_startup_banner()
    trade_journal.log_event("startup", notes="run_all.py started")
    while True:
        try:
            main()
        except Exception as e:
            print("Cycle Error: " + str(e))
            trade_journal.log_event("error", notes=str(e))
        time.sleep(60)
