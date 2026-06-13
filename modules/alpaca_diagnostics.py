"""Alpaca credential checks and user-facing error messages."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from alpaca.common.exceptions import APIError

import config
from modules.alpaca_client import get_trading_client, is_auth_alpaca_error

ENV_FILE = Path(".env")
MISSING = "-"


def _env_present(name: str) -> bool:
    val = os.getenv(name)
    return bool(val and str(val).strip())


def alpaca_env_status(*, paper: bool | None = None) -> dict:
    """Summarize which Alpaca env vars are set (values never returned)."""
    use_paper = config.PAPER_TRADING if paper is None else bool(paper)
    key_vars = ("APCA_API_KEY_ID", "ALPACA_API_KEY")
    secret_vars = ("APCA_API_SECRET_KEY", "ALPACA_SECRET_KEY")
    has_key = any(_env_present(v) for v in key_vars)
    has_secret = any(_env_present(v) for v in secret_vars)
    return {
        "paper_mode": use_paper,
        "env_file_exists": ENV_FILE.is_file(),
        "has_api_key": has_key,
        "has_api_secret": has_secret,
        "credentials_ready": has_key and has_secret,
        "base_url": config.get_alpaca_base_url(paper=use_paper),
    }


def format_missing_env_message(*, paper: bool | None = None) -> str:
    st = alpaca_env_status(paper=paper)
    book = "paper" if st["paper_mode"] else "live"
    lines = [
        f"Alpaca {book} credentials missing in .env.",
        f"Expected file: {ENV_FILE.resolve()}",
        "  exists:" if st["env_file_exists"] else "  missing:",
        "  APCA_API_KEY_ID=your_key_id",
        "  APCA_API_SECRET_KEY=your_secret_key",
    ]
    if st["paper_mode"]:
        lines.append("  PAPER_TRADING=true")
    else:
        lines.append("  PAPER_TRADING=false  (live keys required for live account)")
        lines.append("  ALLOW_LIVE_TRADING=yes  (if trading live)")
    lines.append(f"Base URL: {st['base_url']}")
    return "\n".join(lines)


def format_auth_failure_message(*, paper: bool, http_status: int | str = 401) -> str:
    st = alpaca_env_status(paper=paper)
    book = "paper" if paper else "live"
    lines = [
        f"Alpaca {book} auth failed (HTTP {http_status}).",
        f".env: {'found' if st['env_file_exists'] else 'NOT FOUND'} at {ENV_FILE.resolve()}",
        f"API key set: {'yes' if st['has_api_key'] else 'NO'} | "
        f"secret set: {'yes' if st['has_api_secret'] else 'NO'}",
        f"Endpoint: {st['base_url']}",
    ]
    if not paper and config.PAPER_TRADING:
        lines.append(
            "Hint: PAPER_TRADING=true in .env — status live equity needs live keys "
            "with PAPER_TRADING=false or a separate live credential set."
        )
    elif paper and not config.PAPER_TRADING:
        lines.append("Hint: use paper API keys from Alpaca dashboard (paper trading).")
    else:
        lines.append(
            "Hint: regenerate keys at alpaca.markets — ensure key matches paper vs live endpoint."
        )
    return " | ".join(lines)


def fetch_alpaca_account(
    *,
    paper: bool | None = None,
    credentials_fn: Callable[[], tuple[str, str]] | None = None,
):
    """Return (account, error_message). Avoids noisy retry logging for status scripts."""
    use_paper = config.PAPER_TRADING if paper is None else bool(paper)
    try:
        if credentials_fn is None:
            config.get_alpaca_credentials()
        client = get_trading_client(paper=use_paper, credentials_fn=credentials_fn)
        account = client.get_account()
        return account, None
    except ValueError as exc:
        return None, str(exc)
    except APIError as exc:
        status = getattr(exc, "status_code", "?")
        if is_auth_alpaca_error(exc):
            return None, format_auth_failure_message(paper=use_paper, http_status=status)
        return None, f"Alpaca API error (HTTP {status}): {exc}"
    except RuntimeError as exc:
        return None, str(exc)
    except Exception as exc:
        return None, f"Alpaca fetch failed: {exc}"


def fetch_alpaca_equity(
    *,
    paper: bool | None = None,
    credentials_fn: Callable[[], tuple[str, str]] | None = None,
) -> tuple[float | None, str | None]:
    account, err = fetch_alpaca_account(paper=paper, credentials_fn=credentials_fn)
    if err or account is None:
        return None, err
    try:
        return float(account.equity), None
    except (TypeError, ValueError):
        return None, "Alpaca account returned invalid equity"
