"""Shared Alpaca REST client factory, caching, and resilient API wrappers."""

from __future__ import annotations

import logging
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
TRANSIENT_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})
AUTH_HTTP_STATUS = frozenset({401, 403})

_client_cache: dict[tuple[str, str, bool, str], TradingClient] = {}
_cache_lock = threading.Lock()


class AlpacaAuthError(RuntimeError):
    """Non-retryable authentication/authorization failure."""


class AlpacaCriticalError(RuntimeError):
    """Non-recoverable Alpaca API failure after retries."""


class AlpacaValidationError(RuntimeError):
    """Order rejected by Alpaca (422/403 validation) — skip order, keep bot running."""


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
) -> TradingClient:
    """Return a cached TradingClient using config credentials."""
    cred_fn = credentials_fn or config.get_alpaca_credentials
    api_key, secret_key = cred_fn()
    use_paper = config.PAPER_TRADING if paper is None else bool(paper)
    if not use_paper and not config.ALLOW_LIVE_TRADING:
        raise RuntimeError(
            "Live trading is disabled. Use Alpaca paper keys with PAPER_TRADING=true, "
            "or set ALLOW_LIVE_TRADING=yes to acknowledge live risk."
        )
    return build_trading_client(api_key, secret_key, paper=use_paper)


def is_transient_alpaca_error(exc: BaseException) -> bool:
    if isinstance(exc, APIError):
        status = getattr(exc, "status_code", None)
        return status in TRANSIENT_HTTP_STATUS
    return isinstance(exc, (TimeoutError, OSError, ConnectionError))


def is_auth_alpaca_error(exc: BaseException) -> bool:
    if not isinstance(exc, APIError):
        return False
    status = getattr(exc, "status_code", None)
    if status == 403 and "insufficient qty" in str(exc).lower():
        return False
    return status in AUTH_HTTP_STATUS


def is_skippable_order_error(exc: BaseException) -> bool:
    """422 notional/qty validation or insufficient-qty 403 — do not crash the cycle."""
    if not isinstance(exc, APIError):
        return False
    status = getattr(exc, "status_code", None)
    msg = str(exc).lower()
    if status == 422:
        return True
    if status == 403 and "insufficient qty" in msg:
        return True
    return False


def call_with_retry(
    func: Callable[..., T],
    /,
    *args: Any,
    op_name: str = "alpaca_api",
    max_attempts: int = MAX_API_ATTEMPTS,
    **kwargs: Any,
) -> T:
    """Call an Alpaca SDK method with exponential backoff on transient failures."""
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)
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
        except (TimeoutError, OSError, ConnectionError) as exc:
            last_exc = exc
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
            logger.error("Alpaca %s network failure after retries: %s", op_name, exc)
            raise AlpacaCriticalError(str(exc)) from exc
    if last_exc is not None:
        raise AlpacaCriticalError(str(last_exc)) from last_exc
    raise AlpacaCriticalError(f"{op_name} failed with no exception captured")
