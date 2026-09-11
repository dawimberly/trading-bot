"""Regression: Alpaca {"message":"Internal Server Error"} is transient, not a CYCLE order.

Run: python -m pytest tests/test_alpaca_internal_server_error.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.alpaca_client import (
    AlpacaCriticalError,
    alpaca_http_status,
    call_with_retry,
    is_skippable_order_error,
    is_transient_alpaca_error,
)
from modules import error_watcher


class _FakeAPIError(Exception):
    """Stand-in for alpaca.common.exceptions.APIError with optional status_code."""

    def __init__(self, message: str, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def test_json_500_without_status_code_is_transient():
    exc = _FakeAPIError('{"message":"Internal Server Error"}')
    assert alpaca_http_status(exc) == 500
    assert is_transient_alpaca_error(exc) is True


def test_status_code_500_is_transient():
    exc = _FakeAPIError("boom", status_code=500)
    assert alpaca_http_status(exc) == 500
    assert is_transient_alpaca_error(exc) is True


def test_critical_error_cause_chain_keeps_500():
    cause = _FakeAPIError('{"message":"Internal Server Error"}')
    wrapped = AlpacaCriticalError(str(cause))
    wrapped.__cause__ = cause
    assert alpaca_http_status(wrapped) == 500
    assert is_transient_alpaca_error(wrapped) is True


def test_unknown_asset_still_skippable():
    class _NotFound(_FakeAPIError):
        pass

    exc = _NotFound("asset not found")
    assert is_skippable_order_error(exc) is True


def test_call_with_retry_retries_json_500(monkeypatch):
    monkeypatch.setattr("modules.alpaca_client.RETRY_BASE_DELAY_SEC", 0.0)
    from alpaca.common.exceptions import APIError

    attempts = {"n": 0}

    def boom():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise APIError('{"message": "Internal Server Error"}')
        return "ok"

    assert call_with_retry(boom, op_name="submit_order") == "ok"
    assert attempts["n"] == 3


def test_call_with_retry_gives_up_after_max_500s(monkeypatch):
    monkeypatch.setattr("modules.alpaca_client.RETRY_BASE_DELAY_SEC", 0.0)
    from alpaca.common.exceptions import APIError

    def boom():
        raise APIError('{"message": "Internal Server Error"}')

    try:
        call_with_retry(boom, op_name="get_account")
        raise AssertionError("expected AlpacaCriticalError")
    except AlpacaCriticalError as exc:
        assert "Internal Server Error" in str(exc)


def test_classify_internal_server_error_string():
    msg = '{"message":"Internal Server Error"}'
    assert error_watcher.classify_error_class(msg) == "transient_api"


def test_log_failed_order_cycle_is_not_an_order(tmp_path, monkeypatch):
    monkeypatch.setattr(error_watcher, "_enabled", lambda: True)
    monkeypatch.setattr(error_watcher, "_ERRORS_PATH", tmp_path / "bot_errors.jsonl")
    monkeypatch.setattr(error_watcher, "_ACTIONS_PATH", tmp_path / "bot_actions.jsonl")
    monkeypatch.setattr(error_watcher, "_CURSOR_QUEUE", tmp_path / "cursor_fix_queue.md")
    monkeypatch.setattr(error_watcher, "_LOG_DIR", tmp_path)
    monkeypatch.setattr(error_watcher, "_last_network_log_at", 0.0)
    monkeypatch.setattr(error_watcher, "_last_tg_at", 0.0)
    monkeypatch.setattr(error_watcher, "_maybe_telegram_error", lambda *a, **k: None)

    eid = error_watcher.log_failed_order(
        symbol="CYCLE",
        side="n/a",
        reason="cycle_error",
        error='{"message":"Internal Server Error"}',
    )
    assert eid
    rows = (tmp_path / "bot_errors.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 1
    row = json.loads(rows[0])
    assert row["event"] == "transient_api"
    assert row["error_class"] == "transient_api"
    assert row.get("symbol") != "CYCLE" or row["event"] != "order_failed"
    queue = (tmp_path / "cursor_fix_queue.md").read_text(encoding="utf-8")
    assert "Order failed" not in queue
    assert "transient_api" in queue
    assert "not a strategy error" in queue.lower() or "Internal Server Error" in queue
