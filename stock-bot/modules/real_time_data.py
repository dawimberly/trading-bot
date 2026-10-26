"""Alpaca WebSocket real-time quotes/trades with in-memory cache + optional DB flush.

Falls back to 5m yfinance bars when disabled, degraded, or disconnected.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, time as dt_time, timezone
from typing import Any, Callable

import pandas as pd

import config
from modules.data_loader import clear_close_matrix_cache, safe_sql_table

logger = logging.getLogger(__name__)

# Reconnection / health
BACKOFF_INITIAL_SEC = 1
BACKOFF_MAX_SEC = 30
MAX_RETRIES_PER_SESSION = int(os.getenv("REAL_TIME_MAX_RETRIES", "5"))
STALE_DATA_SEC = int(os.getenv("REAL_TIME_STALE_SEC", "30"))
CONNECT_GRACE_SEC = int(os.getenv("REAL_TIME_CONNECT_GRACE_SEC", "45"))
WATCHDOG_POLL_SEC = 5
# Quiet reconnect noise outside this ET window (regular session ± buffer).
_WS_WARN_START_ET = dt_time(9, 25)
_WS_WARN_END_ET = dt_time(16, 5)


def _outside_equity_session_et(now: datetime | None = None) -> bool:
    """True before 9:25 ET or after 16:05 ET (overnight / weekend noise window)."""
    try:
        from zoneinfo import ZoneInfo

        et = ZoneInfo("America/New_York")
    except Exception:
        try:
            import pytz

            et = pytz.timezone("America/New_York")
        except Exception:
            return False
    ts = now or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    local = ts.astimezone(et)
    t = local.timetz().replace(tzinfo=None)
    return t < _WS_WARN_START_ET or t > _WS_WARN_END_ET


def _ws_reconnect_warning(msg: str, *args: Any) -> None:
    """WARNING in-session; DEBUG outside RTH so overnight reconnect loops stay quiet."""
    if _outside_equity_session_et():
        logger.debug(msg, *args)
    else:
        logger.warning(msg, *args)

_lock = threading.Lock()
_latest: dict[str, dict[str, Any]] = {}
_state: dict[str, Any] = {
    "enabled": False,
    "connected": False,
    "degraded": False,
    "connecting": False,
    "stock_symbols": [],
    "crypto_symbols": [],
    "subscribed_stock": 0,
    "subscribed_crypto": 0,
    "reconnect_attempts": 0,
    "disconnect_streak": 0,
    "last_tick_at": None,
    "last_flush_at": None,
    "connected_at": None,
    "error": None,
}
_stop = threading.Event()
_threads: list[threading.Thread] = []

_SYMBOL_LIMIT_RE = re.compile(r"symbol\s*limit", re.I)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def _use_quotes() -> bool:
    return os.getenv("REAL_TIME_USE_QUOTES", "false").lower() in ("1", "true", "yes", "on")


def _symbol_batch_size() -> int:
    return max(1, _env_int("REAL_TIME_SYMBOL_BATCH_SIZE", 10))


def _mid_price(bid: float | None, ask: float | None) -> float | None:
    if bid and ask and bid > 0 and ask > 0:
        return (float(bid) + float(ask)) / 2.0
    if bid and bid > 0:
        return float(bid)
    if ask and ask > 0:
        return float(ask)
    return None


def _is_symbol_limit_error(exc: BaseException | str | None) -> bool:
    text = str(exc or "")
    return bool(_SYMBOL_LIMIT_RE.search(text)) or "405" in text


def _chunk_symbols(symbols: list[str], size: int) -> list[list[str]]:
    size = max(1, size)
    return [symbols[i : i + size] for i in range(0, len(symbols), size)]


def _set_state(**kwargs: Any) -> None:
    with _lock:
        _state.update(kwargs)


def _record_tick(symbol: str, price: float | None, *, source: str) -> None:
    if price is None or price <= 0:
        return
    sym = config.normalize_symbol(symbol)
    now = datetime.now(timezone.utc)
    with _lock:
        _latest[sym] = {
            "price": float(price),
            "source": source,
            "ts": now,
        }
        _state["last_tick_at"] = now


def _overlay_ok() -> bool:
    """True when live prices are fresh enough to patch the 5m matrix."""
    with _lock:
        if not _state.get("enabled"):
            return False
        if _state.get("degraded"):
            return False
        if not _state.get("connected"):
            return False
        if not _latest:
            return False
        last = _state.get("last_tick_at")
    if last is None:
        return False
    age = (datetime.now(timezone.utc) - last).total_seconds()
    return age <= max(STALE_DATA_SEC * 2, 60)


def resolve_subscription_symbols() -> tuple[list[str], list[str]]:
    """Build stock + optional crypto subscribe lists (lightweight caps)."""
    stocks: list[str] = []
    seen: set[str] = set()
    max_nyse = _env_int("REAL_TIME_MAX_NYSE_SYMBOLS", 12)

    def add_equity(sym: str) -> None:
        sym = config.normalize_symbol(sym)
        if not sym or sym in seen or config.is_crypto(sym):
            return
        if not config._nyse_eligible_symbol(sym) and sym not in (
            config.SPY_BOT_SYMBOL,
            config.VTI_CORE_SYMBOL,
        ):
            return
        seen.add(sym)
        stocks.append(sym)

    add_equity(config.SPY_BOT_SYMBOL)
    add_equity(config.VTI_CORE_SYMBOL)

    use_dyn = config.USE_DYNAMIC_UNIVERSE or config.effective_paper_dynamic_universe()
    screener = config.load_screener_universe_tickers()
    if use_dyn and screener:
        for ticker in screener:
            if len(stocks) >= 2 + max_nyse:
                break
            add_equity(ticker)
    else:
        for ticker in config.equity_universe():
            if len(stocks) >= 2 + max_nyse:
                break
            add_equity(ticker)

    crypto: list[str] = []
    if config.effective_real_time_crypto_ws():
        max_crypto = _env_int("REAL_TIME_MAX_CRYPTO_SYMBOLS", 8)
        for sym in config.base_crypto_universe()[:max_crypto]:
            crypto.append(config.normalize_symbol(sym))

    return stocks, crypto


def _to_alpaca_crypto(symbol: str) -> str:
    return config.normalize_symbol(symbol).replace("-", "/")


def _subscribe_batched(
    stream: Any,
    symbols: list[str],
    *,
    kind: str,
    on_trade: Callable,
    on_quote: Callable | None,
    subscribe_trades: Callable,
    subscribe_quotes: Callable | None = None,
) -> list[str]:
    """Subscribe in batches; trim gracefully when Alpaca symbol limit is hit."""
    if not symbols:
        return []
    batch_size = _symbol_batch_size()
    subscribed: list[str] = []
    use_quotes = _use_quotes() and on_quote is not None and subscribe_quotes is not None

    for batch in _chunk_symbols(symbols, batch_size):
        try:
            subscribe_trades(on_trade, *batch)
            subscribed.extend(batch)
            logger.debug("%s WebSocket: subscribed trades batch (%s)", kind, len(batch))
        except Exception as exc:
            if not _is_symbol_limit_error(exc):
                raise
            logger.warning(
                "%s WebSocket: symbol limit on batch of %s — subscribing one-by-one",
                kind,
                len(batch),
            )
            for sym in batch:
                try:
                    subscribe_trades(on_trade, sym)
                    subscribed.append(sym)
                except Exception as exc2:
                    if _is_symbol_limit_error(exc2):
                        logger.warning(
                            "%s WebSocket: symbol limit at %s symbols — using partial subscription",
                            kind,
                            len(subscribed),
                        )
                        return subscribed
                    raise

    if use_quotes and subscribed:
        quote_batch_size = max(1, batch_size // 2) if use_quotes else batch_size
        for batch in _chunk_symbols(subscribed, quote_batch_size):
            try:
                subscribe_quotes(on_quote, *batch)
            except Exception as exc:
                if _is_symbol_limit_error(exc):
                    logger.warning(
                        "%s WebSocket: quote symbol limit — trades-only for %s symbols",
                        kind,
                        len(subscribed),
                    )
                    break
                raise

    return subscribed


def _should_force_reconnect(connected_at: datetime | None, last_tick_at: datetime | None) -> bool:
    now = datetime.now(timezone.utc)
    if last_tick_at is not None:
        return (now - last_tick_at).total_seconds() > STALE_DATA_SEC
    if connected_at is not None:
        return (now - connected_at).total_seconds() > CONNECT_GRACE_SEC
    return False


def _stale_watchdog(stream_holder: dict[str, Any], kind: str) -> None:
    """Force reconnect when no ticks arrive within the stale window."""
    while not _stop.is_set() and not stream_holder.get("stop"):
        time.sleep(WATCHDOG_POLL_SEC)
        with _lock:
            if not _state.get("connected"):
                continue
            connected_at = _state.get("connected_at")
            last_tick = _state.get("last_tick_at")
        if not _should_force_reconnect(connected_at, last_tick):
            continue
        age = 0
        if last_tick:
            age = int((datetime.now(timezone.utc) - last_tick).total_seconds())
        _ws_reconnect_warning(
            "%s WebSocket: no data for %ss — forcing reconnect",
            kind,
            age or STALE_DATA_SEC,
        )
        stream = stream_holder.get("stream")
        if stream is not None:
            for method in ("stop", "stop_ws", "close"):
                fn = getattr(stream, method, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception as exc:
                        logger.debug("WebSocket stream %s() during shutdown failed: %s", method, exc)
                    break
        return


def _mark_connected(kind: str, subscribed: list[str], *, is_reconnect: bool, attempts: int) -> None:
    now = datetime.now(timezone.utc)
    count = len(subscribed)
    with _lock:
        _state["connected"] = True
        _state["connecting"] = False
        _state["error"] = None
        _state["connected_at"] = now
        _state["reconnect_attempts"] = 0
        _state["disconnect_streak"] = 0
        if kind.lower().startswith("stock"):
            _state["subscribed_stock"] = count
        else:
            _state["subscribed_crypto"] = count
    if is_reconnect and attempts > 1:
        logger.info(
            "WebSocket reconnected after %s attempts (%s, %s symbols subscribed)",
            attempts - 1,
            kind,
            count,
        )
    else:
        logger.info("WebSocket connected (%s, %s symbols subscribed)", kind, count)


def _mark_disconnected(kind: str, exc: BaseException | None = None) -> None:
    with _lock:
        _state["connected"] = False
        _state["connecting"] = False
        streak = int(_state.get("disconnect_streak") or 0) + 1
        _state["disconnect_streak"] = streak
        _state["reconnect_attempts"] = streak
        if exc is not None:
            _state["error"] = str(exc)
    if exc is not None:
        _ws_reconnect_warning("%s WebSocket disconnected: %s", kind, exc)
    else:
        _ws_reconnect_warning("%s WebSocket disconnected", kind)


def _run_stock_stream(
    api_key: str,
    secret_key: str,
    symbols: list[str],
    *,
    is_reconnect: bool = False,
    attempts: int = 1,
) -> None:
    if not symbols:
        return
    from alpaca.data.enums import DataFeed
    from alpaca.data.live import StockDataStream

    feed_raw = (os.getenv("REAL_TIME_DATA_FEED") or "iex").strip().lower()
    feed = DataFeed.SIP if feed_raw == "sip" else DataFeed.IEX
    stream = StockDataStream(api_key, secret_key, feed=feed)
    holder: dict[str, Any] = {"stream": stream, "stop": False}

    async def on_trade(trade) -> None:
        sym = getattr(trade, "symbol", None) or (trade.get("S") if isinstance(trade, dict) else None)
        px = getattr(trade, "price", None) or (trade.get("p") if isinstance(trade, dict) else None)
        if sym and px:
            _record_tick(str(sym), float(px), source="trade")

    async def on_quote(quote) -> None:
        sym = getattr(quote, "symbol", None) or (quote.get("S") if isinstance(quote, dict) else None)
        bid = getattr(quote, "bid_price", None) or (quote.get("bp") if isinstance(quote, dict) else None)
        ask = getattr(quote, "ask_price", None) or (quote.get("ap") if isinstance(quote, dict) else None)
        px = _mid_price(bid, ask)
        if sym and px:
            _record_tick(str(sym), px, source="quote")

    was_connected = False
    try:
        subscribed = _subscribe_batched(
            stream,
            symbols,
            kind="Stock",
            on_trade=on_trade,
            on_quote=on_quote,
            subscribe_trades=stream.subscribe_trades,
            subscribe_quotes=stream.subscribe_quotes,
        )
        if not subscribed:
            raise RuntimeError("Stock WebSocket: no symbols subscribed (symbol limit)")

        _mark_connected("Stock", subscribed, is_reconnect=is_reconnect, attempts=attempts)
        was_connected = True
        watchdog = threading.Thread(
            target=_stale_watchdog,
            args=(holder, "Stock"),
            name="alpaca-ws-stocks-watchdog",
            daemon=True,
        )
        watchdog.start()
        stream.run()
    except Exception as exc:
        if not was_connected:
            _mark_disconnected("Stock", exc)
        raise
    finally:
        holder["stop"] = True
        if was_connected:
            _mark_disconnected("Stock")


def _run_crypto_stream(
    api_key: str,
    secret_key: str,
    symbols: list[str],
    *,
    is_reconnect: bool = False,
    attempts: int = 1,
) -> None:
    if not symbols:
        return
    from alpaca.data.live import CryptoDataStream

    stream = CryptoDataStream(api_key, secret_key)
    holder: dict[str, Any] = {"stream": stream, "stop": False}
    alpaca_map = {s: _to_alpaca_crypto(s) for s in symbols}
    rev_map = {v: k for k, v in alpaca_map.items()}
    alpaca_syms = list(alpaca_map.values())

    async def on_trade(trade) -> None:
        raw = getattr(trade, "symbol", None) or (trade.get("S") if isinstance(trade, dict) else None)
        sym = rev_map.get(str(raw), str(raw))
        px = getattr(trade, "price", None) or (trade.get("p") if isinstance(trade, dict) else None)
        if sym and px:
            _record_tick(sym, float(px), source="crypto_trade")

    async def on_quote(quote) -> None:
        raw = getattr(quote, "symbol", None) or (quote.get("S") if isinstance(quote, dict) else None)
        sym = rev_map.get(str(raw), str(raw))
        bid = getattr(quote, "bid_price", None) or (quote.get("bp") if isinstance(quote, dict) else None)
        ask = getattr(quote, "ask_price", None) or (quote.get("ap") if isinstance(quote, dict) else None)
        px = _mid_price(bid, ask)
        if sym and px:
            _record_tick(sym, px, source="crypto_quote")

    was_connected = False
    try:
        subscribed_alpaca = _subscribe_batched(
            stream,
            alpaca_syms,
            kind="Crypto",
            on_trade=on_trade,
            on_quote=on_quote,
            subscribe_trades=stream.subscribe_trades,
            subscribe_quotes=stream.subscribe_quotes,
        )
        subscribed = [rev_map.get(s, s) for s in subscribed_alpaca]
        if not subscribed:
            raise RuntimeError("Crypto WebSocket: no symbols subscribed (symbol limit)")

        _mark_connected("Crypto", subscribed, is_reconnect=is_reconnect, attempts=attempts)
        was_connected = True
        watchdog = threading.Thread(
            target=_stale_watchdog,
            args=(holder, "Crypto"),
            name="alpaca-ws-crypto-watchdog",
            daemon=True,
        )
        watchdog.start()
        stream.run()
    except Exception as exc:
        if not was_connected:
            _mark_disconnected("Crypto", exc)
        raise
    finally:
        holder["stop"] = True
        if was_connected:
            _mark_disconnected("Crypto")


def _stream_supervisor(kind: str, runner: Callable, api_key: str, secret_key: str, symbols: list[str]) -> None:
    """Reconnect with exponential backoff; degrade to 5m bars after max retries."""
    sym_list = list(symbols)
    backoff = BACKOFF_INITIAL_SEC
    while not _stop.is_set():
        with _lock:
            attempt = int(_state.get("disconnect_streak") or 0) + 1
        _set_state(connecting=True, reconnect_attempts=attempt)
        is_reconnect = attempt > 1
        try:
            runner(
                api_key,
                secret_key,
                sym_list,
                is_reconnect=is_reconnect,
                attempts=attempt,
            )
        except Exception as exc:
            if _is_symbol_limit_error(exc) and len(sym_list) > 2:
                sym_list = sym_list[: max(2, len(sym_list) // 2)]
                logger.warning(
                    "%s WebSocket: reducing subscription to %s symbols after limit error",
                    kind,
                    len(sym_list),
                )

        with _lock:
            streak = int(_state.get("disconnect_streak") or 0)

        if streak >= MAX_RETRIES_PER_SESSION:
            _ws_reconnect_warning(
                "%s WebSocket: max %s retries per session — falling back to 5m bars",
                kind,
                MAX_RETRIES_PER_SESSION,
            )
            _set_state(degraded=True, connecting=False, connected=False)
            return

        if _outside_equity_session_et():
            logger.debug(
                "%s WebSocket: reconnect in %ss (attempt %s/%s)",
                kind,
                backoff,
                streak,
                MAX_RETRIES_PER_SESSION,
            )
        else:
            logger.info(
                "%s WebSocket: reconnect in %ss (attempt %s/%s)",
                kind,
                backoff,
                streak,
                MAX_RETRIES_PER_SESSION,
            )
        if _stop.wait(backoff):
            return
        backoff = min(backoff * 2, BACKOFF_MAX_SEC)


def _flush_prices_to_db() -> None:
    if not _overlay_ok() and not _latest:
        return
    path = str(config.resolve_db_path())
    with _lock:
        snapshot = {k: v.get("price") for k, v in _latest.items() if v.get("price")}
    if not snapshot:
        return
    try:
        conn = sqlite3.connect(path, timeout=5)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {row[0] for row in cursor.fetchall()}
        for symbol, price in snapshot.items():
            if symbol not in tables:
                continue
            try:
                table = safe_sql_table(symbol, allowed=tables)
            except ValueError:
                continue
            cursor.execute(
                f'UPDATE "{table}" SET Close = ? WHERE rowid = '
                f'(SELECT rowid FROM "{table}" ORDER BY Date DESC LIMIT 1)',
                (float(price),),
            )
        conn.commit()
        conn.close()
        with _lock:
            _state["last_flush_at"] = datetime.now(timezone.utc)
        clear_close_matrix_cache()
    except Exception as exc:
        logger.debug("WebSocket DB flush skipped: %s", exc)


def _flush_loop() -> None:
    interval = max(2, _env_int("REAL_TIME_FLUSH_SEC", 5))
    while not _stop.is_set():
        _flush_prices_to_db()
        if _stop.wait(interval):
            break


def start_realtime_feed() -> bool:
    """Start background WebSocket + DB flush threads. Idempotent."""
    if not config.effective_real_time_websocket_enabled():
        return False
    with _lock:
        if _state.get("enabled"):
            return True
        stocks, crypto = resolve_subscription_symbols()
        _state.update(
            {
                "enabled": True,
                "connected": False,
                "degraded": False,
                "connecting": True,
                "stock_symbols": stocks,
                "crypto_symbols": crypto,
                "subscribed_stock": 0,
                "subscribed_crypto": 0,
                "reconnect_attempts": 0,
                "error": None,
            }
        )
    api_key, secret_key = config.get_alpaca_credentials()
    _stop.clear()
    stock_thread = threading.Thread(
        target=_stream_supervisor,
        args=("Stock", _run_stock_stream, api_key, secret_key, stocks),
        name="alpaca-ws-stocks",
        daemon=True,
    )
    flush_thread = threading.Thread(target=_flush_loop, name="alpaca-ws-flush", daemon=True)
    _threads[:] = [stock_thread, flush_thread]
    stock_thread.start()
    flush_thread.start()
    if crypto:
        crypto_thread = threading.Thread(
            target=_stream_supervisor,
            args=("Crypto", _run_crypto_stream, api_key, secret_key, crypto),
            name="alpaca-ws-crypto",
            daemon=True,
        )
        _threads.append(crypto_thread)
        crypto_thread.start()
    total = len(stocks) + len(crypto)
    logger.info(
        "Real-time WebSocket feed starting (%s stock + %s crypto = %s symbols)",
        len(stocks),
        len(crypto),
        total,
    )
    return True


def stop_realtime_feed() -> None:
    _stop.set()
    with _lock:
        _state["enabled"] = False
        _state["connected"] = False
        _state["connecting"] = False


def get_latest_price(symbol: str) -> float | None:
    sym = config.normalize_symbol(symbol)
    with _lock:
        tick = _latest.get(sym)
    if not tick:
        return None
    return float(tick["price"])


def apply_live_overlay(data: pd.DataFrame) -> pd.DataFrame:
    """Patch last bar close with latest WebSocket prices (no-op when degraded/disconnected)."""
    if data is None or data.empty or not _overlay_ok():
        return data
    with _lock:
        snapshot = {k: v.get("price") for k, v in _latest.items()}
    out = data.copy()
    last_idx = out.index[-1]
    changed = False
    for sym, price in snapshot.items():
        if sym not in out.columns or price is None or price <= 0:
            continue
        out.loc[last_idx, sym] = float(price)
        changed = True
    return out if changed else data


def load_live_close_matrix(**kwargs) -> pd.DataFrame:
    """load_close_matrix + optional WebSocket overlay on the last bar."""
    from modules.data_loader import load_close_matrix

    data = load_close_matrix(**kwargs)
    if config.effective_real_time_websocket_enabled():
        data = apply_live_overlay(data)
    return data


def _symbol_summary(stocks: list[str]) -> str:
    if len(stocks) >= 2:
        others = max(0, len(stocks) - 2)
        tail = f" + {others} other{'s' if others != 1 else ''}" if others else ""
        return f"{stocks[0]}, {stocks[1]}{tail}"
    if stocks:
        return stocks[0]
    return "none"


def _tick_age_label() -> str:
    with _lock:
        last = _state.get("last_tick_at")
    if last is None:
        return "no ticks yet"
    age = int((datetime.now(timezone.utc) - last).total_seconds())
    if age < 60:
        return f"last tick {age}s ago"
    return f"last tick {age // 60}m ago"


def format_status_line() -> str | None:
    if not config.effective_real_time_websocket_enabled():
        return None
    with _lock:
        stocks = list(_state.get("stock_symbols") or [])
        crypto = list(_state.get("crypto_symbols") or [])
        connected = bool(_state.get("connected"))
        connecting = bool(_state.get("connecting"))
        degraded = bool(_state.get("degraded"))
        sub_stock = int(_state.get("subscribed_stock") or 0)
        sub_crypto = int(_state.get("subscribed_crypto") or 0)
        attempts = int(_state.get("reconnect_attempts") or 0)

    if not stocks:
        stocks, crypto = resolve_subscription_symbols()
    total_cfg = len(stocks) + len(crypto)
    total_sub = sub_stock + sub_crypto if (sub_stock or sub_crypto) else total_cfg
    summary = _symbol_summary(stocks)

    if degraded:
        status = "Degraded (5m bars)"
    elif connected:
        status = "Connected"
    elif connecting:
        status = f"Reconnecting {attempts}/{MAX_RETRIES_PER_SESSION}"
    else:
        status = "Connecting"

    tick = _tick_age_label()
    crypto_bit = f" + {sub_crypto} crypto" if crypto else ""
    return (
        f"WebSocket: {status} | {total_sub} symbols ({summary}){crypto_bit} | {tick}"
    )


def get_status() -> dict[str, Any]:
    with _lock:
        return dict(_state)
