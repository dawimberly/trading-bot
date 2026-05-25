"""24/7 SPY bot: buy when the S&P 500 ETF is above its moving average (market-up bet).

Run: python run_spy.py
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
from modules.pipeline_strategies import run_spy_exits, run_spy_strategy
from modules.portfolio_manager import PortfolioManager
from modules.position_exits import run_position_exits
from modules.risk_management import RiskManager
from modules import trade_journal
from modules import alerts

pair_cooldown = {}
refresh_scheduler = RefreshScheduler(
    refresh_crypto=False,
    equity_tickers=[config.SPY_BOT_SYMBOL],
)
risk_manager = RiskManager(max_drawdown_pct=config.MAX_DRAWDOWN_PCT)
portfolio_manager = PortfolioManager(ledger_file=config.LEDGER_PATH)
_last_equity = 0.0


def log_trade(symbol, side, regime):
    with open(config.TRADE_HISTORY_LOG, "a", encoding="utf-8") as f:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"{ts} | SPY-BOT | {side.upper()} | {symbol} | Regime: {regime}\n")


def _spy_log(symbol, side, regime, pair_key, momentum, notional=""):
    if side == "buy":
        print(
            f"!!! SPY SIGNAL: {symbol} above MA{config.SPY_MA_WINDOW} | "
            f"momentum={round(momentum * 100, 2)}% | BUY | ${notional} | Regime: {regime}"
        )
    else:
        print(
            f"!!! SPY EXIT: {symbol} below MA{config.SPY_MA_WINDOW} | "
            f"SELL | ${notional} | Regime: {regime}"
        )
    log_trade(symbol, side, regime)
    trade_journal.log_signal(
        symbol, side, regime, pair_key, momentum, _last_equity, notional
    )


def _write_heartbeat(regime, equity, cash, spy_trades, halted, holding_spy, market_open):
    payload = {
        "timestamp": datetime.datetime.now().isoformat(),
        "bot": "spy",
        "symbol": config.SPY_BOT_SYMBOL,
        "regime": regime,
        "equity": equity,
        "cash": cash,
        "spy_trades_last_cycle": spy_trades,
        "holding_spy": holding_spy,
        "equity_session_open": market_open,
        "halted": halted,
        "paper": config.PAPER_TRADING,
    }
    with open(config.SPY_HEARTBEAT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _holding_spy(executor):
    try:
        return any(
            p.symbol == config.SPY_BOT_SYMBOL
            for p in executor.client.get_all_positions()
        )
    except Exception:
        return False


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
        trade_journal.log_event("halt", equity=equity, cash=cash, notes="spy bot drawdown")
        alerts.notify_halt(equity, peak, dd)
        try:
            alerts.maybe_daily_summary(equity, cash, "HALTED", True)
        except Exception as e:
            print(f"Alert error (non-fatal): {e}")
        _write_heartbeat("HALTED", equity, cash, 0, True, _holding_spy(executor), market_open)
        return

    alerts.clear_halt_flag()

    print("--- SPY Bot Cycle: " + str(datetime.datetime.now()) + " ---")
    data = load_close_matrix()
    min_bars = max(20, config.SPY_MA_WINDOW)
    if data.empty or len(data) < min_bars:
        print("Insufficient market data. Skipping cycle.")
        trade_journal.log_event("skip", equity=equity, notes="spy bot: short data")
        return
    if config.SPY_BOT_SYMBOL not in data.columns:
        print(f"No {config.SPY_BOT_SYMBOL} data in database. Skipping cycle.")
        trade_journal.log_event(
            "skip", equity=equity, notes=f"missing {config.SPY_BOT_SYMBOL}"
        )
        return

    regime = get_market_regime(get_sentiment(data), get_volatility(data))
    print(f"--- Regime: {regime} | Equity session: {'OPEN' if market_open else 'CLOSED'} ---")

    if not market_open:
        holding = _holding_spy(executor)
        print("--- Equity session closed; skipping SPY scan ---")
        trade_journal.log_cycle(regime, equity, cash, 0, 0)
        _write_heartbeat(regime, equity, cash, 0, False, holding, market_open)
        return

    exits = run_position_exits(
        executor, risk_manager, trade_journal, equity_session_open=market_open
    )
    if exits:
        print(f"--- Stop-loss exits: {exits} ---")

    now = datetime.datetime.now()
    ma_exits = run_spy_exits(
        data,
        executor,
        regime,
        log_fn=_spy_log,
    )
    if ma_exits:
        print(f"--- MA-break exits: {ma_exits} ---")

    trades = run_spy_strategy(
        data,
        executor,
        regime,
        now,
        pair_cooldown,
        log_fn=_spy_log,
        portfolio_manager=portfolio_manager,
    )
    holding = _holding_spy(executor)
    print(f"--- SPY trades: {trades} | MA exits: {ma_exits} | Holding SPY: {holding} ---")
    trade_journal.log_cycle(regime, equity, cash, 0, trades)
    try:
        alerts.maybe_daily_summary(equity, cash, regime, False)
    except Exception as e:
        print(f"Alert error (non-fatal): {e}")
    _write_heartbeat(regime, equity, cash, trades, False, holding, market_open)


def _print_startup_banner():
    mode = "PAPER" if config.PAPER_TRADING else "LIVE"
    print("--- Starting SPY Market-Up Bot ---")
    print(
        f"--- Symbol: {config.SPY_BOT_SYMBOL} | Signal: price > MA{config.SPY_MA_WINDOW} | "
        f"Exit below MA: {config.SPY_EXIT_ON_MA_BREAK} ---"
    )
    print(f"--- Alpaca mode: {mode} ---")
    print(
        f"--- Allocation: {config.SPY_RISK_PER_TRADE:.0%}/entry, stop {config.STOP_LOSS_PCT:.0%}, "
        f"max DD {config.MAX_DRAWDOWN_PCT:.0%} ---"
    )
    print(f"--- Journal: {config.PAPER_JOURNAL_CSV} | Heartbeat: {config.SPY_HEARTBEAT_FILE} ---")
    if alerts.alerts_configured():
        print("--- Alerts: enabled (Telegram and/or email) ---")
    else:
        print("--- Alerts: off (set TELEGRAM_* or SMTP_* in .env) ---")
    if not config.PAPER_TRADING:
        print("!!! WARNING: Live trading enabled !!!")


if __name__ == "__main__":
    _print_startup_banner()
    trade_journal.log_event("startup", notes="run_spy.py started")
    while True:
        try:
            main()
        except Exception as e:
            print("Cycle Error: " + str(e))
            trade_journal.log_event("error", notes=f"spy bot: {e}")
        time.sleep(60)
