"""Shared Alpaca REST client factory, caching, and resilient API wrappers."""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Callable, TypeVar

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient

import config

try:
    from modules.ssl_certs import configure_ssl_certificates

    configure_ssl_certificates()
except ImportError:
    pass

logger = logging.getLogger(__name__)

T = TypeVar("T")

MAX_API_ATTEMPTS = 3
RETRY_BASE_DELAY_SEC = 0.5
# After DNS/connect failure, pause briefly before the next Alpaca call (no spam).
NETWORK_BACKOFF_MIN_SEC = 5.0
NETWORK_BACKOFF_MAX_SEC = 30.0
TRANSIENT_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})
AUTH_HTTP_STATUS = frozenset({401, 403})

_NETWORK_MARKERS = (
    "nameresolutionerror",
    "getaddrinfo",
    "failed to resolve",
    "name or service not known",
    "nodename nor servname",
    "temporary failure in name resolution",
    "max retries exceeded",
    "connection aborted",
    "connection reset",
    "connection refused",
    "network is unreachable",
    "timed out",
    "read timed out",
    "connect timeout",
    "winerror 11001",
    "winerror 10060",
    "winerror 10054",
    "errno 11001",
    "errno -2",
    "errno -3",
    "sslerror",
    "certificate verify failed",
    "proxyerror",
)

_client_cache: dict[tuple[str, str, bool, str], TradingClient] = {}
_cache_lock = threading.Lock()
_network_backoff_until: float = 0.0
_network_backoff_lock = threading.Lock()


class AlpacaAuthError(RuntimeError):
    """Non-retryable authentication/authorization failure."""


class AlpacaCriticalError(RuntimeError):
    """Non-recoverable Alpaca API failure after retries."""


class AlpacaValidationError(RuntimeError):
    """Order rejected by Alpaca (422/403 validation) — skip order, keep bot running."""


class AlpacaTransientNetworkError(RuntimeError):
    """DNS / connect / timeout — skip cycle orders, keep supervisor running."""

    error_class = "transient_network"


def _client_cache_key(
    api_key: str,
    secret_key: str,
    *,
    paper: bool,
    base_url: str,
) -> tuple[str, str, bool, str]:
    return (api_key, secret_key, bool(paper), base_url)


def reset_trading_client_cache() -> None:
    """Clear cached clients (e.g. after reload_from_env)."""
    with _cache_lock:
        _client_cache.clear()


def build_trading_client(
    api_key: str,
    secret_key: str,
    *,
    paper: bool,
    base_url: str | None = None,
) -> TradingClient:
    """Construct or reuse a TradingClient for the given credential set."""
    url = base_url or config.get_alpaca_base_url(paper=paper)
    key = _client_cache_key(api_key, secret_key, paper=paper, base_url=url)
    with _cache_lock:
        cached = _client_cache.get(key)
        if cached is not None:
            return cached
        client = TradingClient(
            api_key=api_key,
            secret_key=secret_key,
            paper=paper,
            url_override=url,
        )
        _client_cache[key] = client
        logger.debug(
            "TradingClient cached",
            extra={"paper": paper, "base_url": url, "key_suffix": api_key[-4:]},
        )
        return client


def get_trading_client(
    paper: bool | None = None,
    credentials_fn: Callable[[], tuple[str, str]] | None = None,
    *,
    allow_live: bool | None = None,
) -> TradingClient:
    """Return a cached TradingClient using config credentials."""
    use_paper = config.PAPER_TRADING if paper is None else bool(paper)
    if credentials_fn is not None:
        api_key, secret_key = credentials_fn()
    else:
        api_key, secret_key = config.get_alpaca_credentials(paper=use_paper)
    use_allow_live = config.ALLOW_LIVE_TRADING if allow_live is None else bool(allow_live)
    if not use_paper and not use_allow_live:
        raise RuntimeError(
            "Live trading is disabled. Use Alpaca paper keys with PAPER_TRADING=true, "
            "or set ALLOW_LIVE_TRADING=yes to acknowledge live risk."
        )
    return build_trading_client(api_key, secret_key, paper=use_paper)


def _exception_chain(exc: BaseException | None) -> list[BaseException]:
    out: list[BaseException] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        out.append(cur)
        cur = cur.__cause__ or cur.__context__
    return out


def _exception_text(exc: BaseException) -> str:
    parts = [f"{type(e).__name__}: {e}" for e in _exception_chain(exc)]
    return " | ".join(parts).lower()


def is_transient_network_error(exc: BaseException) -> bool:
    """True for DNS / connect / timeout failures (not auth, not order reject)."""
    if isinstance(exc, AlpacaTransientNetworkError):
        return True
    if isinstance(exc, AlpacaAuthError):
        return False

    for e in _exception_chain(exc):
        if isinstance(e, APIError):
            if is_auth_alpaca_error(e):
                return False
            status = getattr(e, "status_code", None)
            if status in TRANSIENT_HTTP_STATUS:
                continue
            if status is not None and int(status) < 500 and status not in (408, 429):
                # Definite application/API response — not a DNS blip.
                return False
        name = type(e).__name__.lower()
        if name in {
            "nameresolutionerror",
            "connecttimeout",
            "readtimeout",
            "timeout",
            "timeouterror",
            "connectionerror",
            "protocolerror",
            "newconnectionerror",
            "maxretryerror",
            "proxyerror",
            "sslerror",
        }:
            return True
        if isinstance(e, (TimeoutError, ConnectionError)):
            return True
        # OSError covers WinError 11001 getaddrinfo failed, etc.
        if isinstance(e, OSError):
            winerr = getattr(e, "winerror", None)
            errno = getattr(e, "errno", None)
            if winerr in (11001, 10060, 10054, 10061) or errno in (11001, -2, -3, 101, 111):
                return True

    text = _exception_text(exc)
    return any(marker in text for marker in _NETWORK_MARKERS)


def is_transient_alpaca_error(exc: BaseException) -> bool:
    if is_transient_network_error(exc):
        return True
    if isinstance(exc, APIError):
        status = getattr(exc, "status_code", None)
        return status in TRANSIENT_HTTP_STATUS
    return isinstance(exc, (TimeoutError, OSError, ConnectionError))


def is_auth_alpaca_error(exc: BaseException) -> bool:
    if not isinstance(exc, APIError):
        return False
    status = getattr(exc, "status_code", None)
    msg = str(exc).lower()
    # Insufficient qty / missing asset are order/asset issues, not credential auth.
    if status == 403 and "insufficient qty" in msg:
        return False
    if is_unknown_asset_error(exc):
        return False
    return status in AUTH_HTTP_STATUS


def is_unknown_asset_error(exc: BaseException) -> bool:
    """True when Alpaca rejects an unknown/untradable symbol (e.g. SKY-USD)."""
    if not isinstance(exc, APIError):
        text = str(exc).lower()
        return "asset" in text and "not found" in text
    status = getattr(exc, "status_code", None)
    msg = str(exc).lower()
    if "asset" in msg and "not found" in msg:
        return True
    if status == 404:
        return True
    return False


def is_skippable_order_error(exc: BaseException) -> bool:
    """422 notional/qty validation, unknown asset, or insufficient-qty 403 — do not crash the cycle."""
    if not isinstance(exc, APIError):
        return is_unknown_asset_error(exc)
    status = getattr(exc, "status_code", None)
    msg = str(exc).lower()
    if is_unknown_asset_error(exc):
        return True
    if status == 422:
        return True
    if status == 403 and "insufficient qty" in msg:
        return True
    return False


def note_network_failure(*, backoff_sec: float | None = None) -> float:
    """Schedule a short pause before the next Alpaca call. Returns sleep seconds chosen."""
    global _network_backoff_until
    delay = backoff_sec
    if delay is None:
        delay = random.uniform(NETWORK_BACKOFF_MIN_SEC, NETWORK_BACKOFF_MAX_SEC)
    delay = max(float(NETWORK_BACKOFF_MIN_SEC), min(float(NETWORK_BACKOFF_MAX_SEC), float(delay)))
    with _network_backoff_lock:
        _network_backoff_until = max(_network_backoff_until, time.time() + delay)
    return delay


def clear_network_backoff() -> None:
    global _network_backoff_until
    with _network_backoff_lock:
        _network_backoff_until = 0.0


def _await_network_backoff(op_name: str) -> None:
    with _network_backoff_lock:
        until = _network_backoff_until
    now = time.time()
    if until <= now:
        return
    wait = until - now
    logger.warning(
        "Alpaca %s waiting %.1fs after prior DNS/network failure (backoff)",
        op_name,
        wait,
    )
    time.sleep(wait)


def call_with_retry(
    func: Callable[..., T],
    /,
    *args: Any,
    op_name: str = "alpaca_api",
    max_attempts: int = MAX_API_ATTEMPTS,
    **kwargs: Any,
) -> T:
    """Call an Alpaca SDK method with exponential backoff on transient failures."""
    _await_network_backoff(op_name)
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = func(*args, **kwargs)
            clear_network_backoff()
            return result
        except APIError as exc:
            last_exc = exc
            if is_auth_alpaca_error(exc):
                try:
                    st = config.alpaca_credentials_status()
                    logger.critical(
                        "Alpaca auth failed during %s (HTTP %s): %s | mode=%s "
                        "endpoint=%s key_source=%s key_suffix=…%s loaded_env=%s",
                        op_name,
                        getattr(exc, "status_code", "?"),
                        exc,
                        st.get("mode"),
                        st.get("base_url"),
                        st.get("key_source"),
                        st.get("key_suffix"),
                        st.get("loaded_env"),
                    )
                except Exception:
                    logger.critical(
                        "Alpaca auth failed during %s (HTTP %s): %s",
                        op_name,
                        getattr(exc, "status_code", "?"),
                        exc,
                    )
                raise AlpacaAuthError(str(exc)) from exc
            if is_transient_alpaca_error(exc) and attempt < max_attempts:
                delay = RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1))
                logger.warning(
                    "Alpaca %s transient HTTP %s — retry %s/%s in %.1fs",
                    op_name,
                    getattr(exc, "status_code", "?"),
                    attempt,
                    max_attempts,
                    delay,
                )
                time.sleep(delay)
                continue
            if is_skippable_order_error(exc):
                logger.info(
                    "Alpaca %s order rejected (HTTP %s): %s",
                    op_name,
                    getattr(exc, "status_code", "?"),
                    exc,
                )
                raise AlpacaValidationError(str(exc)) from exc
            logger.error(
                "Alpaca %s failed (HTTP %s): %s",
                op_name,
                getattr(exc, "status_code", "?"),
                exc,
            )
            raise AlpacaCriticalError(str(exc)) from exc
        except Exception as exc:
            last_exc = exc
            # Never promote DNS/network to auth.
            if is_auth_alpaca_error(exc):
                raise AlpacaAuthError(str(exc)) from exc
            if is_transient_network_error(exc) or isinstance(
                exc, (TimeoutError, OSError, ConnectionError)
            ):
                if attempt < max_attempts:
                    delay = RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1))
                    logger.warning(
                        "Alpaca %s network error — retry %s/%s in %.1fs: %s",
                        op_name,
                        attempt,
                        max_attempts,
                        delay,
                        exc,
                    )
                    time.sleep(delay)
                    continue
                backoff = note_network_failure()
                logger.error(
                    "Alpaca %s TRANSIENT_NETWORK after retries (backoff %.0fs): %s",
                    op_name,
                    backoff,
                    exc,
                )
                raise AlpacaTransientNetworkError(str(exc)) from exc
            raise
    if last_exc is not None:
        if is_transient_network_error(last_exc):
            note_network_failure()
            raise AlpacaTransientNetworkError(str(last_exc)) from last_exc
        raise AlpacaCriticalError(str(last_exc)) from last_exc
    raise AlpacaCriticalError(f"{op_name} failed with no exception captured")
