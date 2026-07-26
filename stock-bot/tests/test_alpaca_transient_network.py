"""Regression: DNS/getaddrinfo failures are TRANSIENT_NETWORK, not auth."""

from __future__ import annotations

import socket

import pytest

from modules.alpaca_client import (
    AlpacaAuthError,
    AlpacaTransientNetworkError,
    call_with_retry,
    clear_network_backoff,
    is_transient_network_error,
    note_network_failure,
)
from modules import error_watcher


def test_getaddrinfo_classified_as_transient_network():
    exc = OSError(11001, "getaddrinfo failed")
    try:
        exc.winerror = 11001  # type: ignore[attr-defined]
    except Exception:
        pass
    assert is_transient_network_error(exc) is True
    wrapped = Exception(
        "HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): "
        "Max retries exceeded with url: /v2/clock "
        "(Caused by NameResolutionError(\"Failed to resolve "
        "'paper-api.alpaca.markets' ([Errno 11001] getaddrinfo failed)\"))"
    )
    wrapped.__cause__ = exc
    assert is_transient_network_error(wrapped) is True


def test_call_with_retry_raises_transient_not_auth(monkeypatch):
    clear_network_backoff()
    monkeypatch.setattr("modules.alpaca_client.MAX_API_ATTEMPTS", 2)
    monkeypatch.setattr("modules.alpaca_client.RETRY_BASE_DELAY_SEC", 0.01)
    monkeypatch.setattr(
        "modules.alpaca_client.note_network_failure",
        lambda **_: 5.0,
    )

    def boom():
        raise socket.gaierror(11001, "getaddrinfo failed")

    with pytest.raises(AlpacaTransientNetworkError):
        call_with_retry(boom, op_name="get_clock")

    with pytest.raises(AlpacaTransientNetworkError):
        call_with_retry(boom, op_name="get_clock")
    # Must never look like auth.
    assert not isinstance(AlpacaTransientNetworkError("x"), AlpacaAuthError)


def test_error_watcher_classifies_and_labels_network(tmp_path, monkeypatch):
    monkeypatch.setattr(error_watcher, "_enabled", lambda: True)
    monkeypatch.setattr(error_watcher, "_ERRORS_PATH", tmp_path / "bot_errors.jsonl")
    monkeypatch.setattr(error_watcher, "_ACTIONS_PATH", tmp_path / "bot_actions.jsonl")
    monkeypatch.setattr(error_watcher, "_CURSOR_QUEUE", tmp_path / "cursor_fix_queue.md")
    monkeypatch.setattr(error_watcher, "_LOG_DIR", tmp_path)
    monkeypatch.setattr(error_watcher, "_last_network_log_at", 0.0)
    monkeypatch.setattr(error_watcher, "_last_tg_at", 0.0)

    msg = (
        "HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): "
        "Max retries exceeded (NameResolutionError getaddrinfo failed)"
    )
    assert error_watcher.classify_error_class(msg) == "transient_network"
    eid = error_watcher.log_failed_order(
        symbol="CYCLE",
        side="n/a",
        reason="cycle_error",
        error=msg,
    )
    assert eid
    rows = (tmp_path / "bot_errors.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 1
    import json

    row = json.loads(rows[0])
    assert row["error_class"] == "transient_network"
    assert error_watcher.error_label(row) == "transient_network"
    queue = (tmp_path / "cursor_fix_queue.md").read_text(encoding="utf-8")
    assert "transient_network" in queue
    assert "not a strategy error" in queue.lower() or "DNS/network" in queue


def test_note_network_failure_sets_backoff():
    clear_network_backoff()
    delay = note_network_failure(backoff_sec=7.0)
    assert 5.0 <= delay <= 30.0
    clear_network_backoff()
