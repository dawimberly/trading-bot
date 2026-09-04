"""HTTP 403 / 40310000 'not fractionable' is an order reject, not auth."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import TimeInForce

from modules.alpaca_client import (
    AlpacaAuthError,
    AlpacaValidationError,
    call_with_retry,
    is_auth_alpaca_error,
    is_not_fractionable_error,
    is_order_forbidden_not_auth,
    is_skippable_order_error,
)
from modules.alpaca_executor import AlpacaExecutor
from modules import alpaca_executor as executor_module


def _api_error(body: str, status: int = 403) -> APIError:
    http = SimpleNamespace(response=SimpleNamespace(status_code=status))
    return APIError(body, http)


PS_BODY = '{"code":40310000,"message":"asset \\"PS\\" is not fractionable"}'


def test_ps_not_fractionable_is_not_auth():
    exc = _api_error(PS_BODY)
    assert exc.status_code == 403
    assert is_not_fractionable_error(exc)
    assert is_order_forbidden_not_auth(exc)
    assert is_auth_alpaca_error(exc) is False
    assert is_skippable_order_error(exc) is True


def test_real_unauthorized_403_still_auth():
    exc = _api_error('{"code":40310000,"message":"unauthorized"}')
    assert is_auth_alpaca_error(exc) is True
    assert is_skippable_order_error(exc) is False


def test_call_with_retry_raises_validation_not_auth_no_sys_exit():
    def boom():
        raise _api_error(PS_BODY)

    with pytest.raises(AlpacaValidationError) as caught:
        call_with_retry(boom, op_name="submit_order")
    assert not isinstance(caught.value, AlpacaAuthError)
    assert is_not_fractionable_error(caught.value)


def test_call_with_retry_auth_403_still_auth():
    def boom():
        raise _api_error('{"code":40310000,"message":"unauthorized"}')

    with pytest.raises(AlpacaAuthError):
        call_with_retry(boom, op_name="submit_order")


def test_whole_share_qty_floors_notional(monkeypatch):
    executor = AlpacaExecutor.__new__(AlpacaExecutor)
    executor._sizing_data = pd.DataFrame({"PS": [80.0, 82.0]})
    monkeypatch.setattr(
        "modules.real_time_data.get_latest_price", lambda _sym: None, raising=False
    )
    assert executor._whole_share_qty_for_notional("PS", 824.75) == "10"


def test_execute_order_retries_not_fractionable_as_whole_shares(monkeypatch):
    executor_module._NON_FRACTIONABLE_ASSETS.discard("PS")
    ex = AlpacaExecutor.__new__(AlpacaExecutor)
    ex._sizing_data = pd.DataFrame({"PS": [80.0]})
    ex._equity_trading_allowed = lambda _s: True
    ex._is_unknown_asset = lambda _s: False
    ex.get_order_params = lambda _s: ("PS", TimeInForce.DAY, False)
    ex._cancel_open_orders_for = lambda _s: None
    ex._atr_adjust_notional = lambda _s, n: n
    ex._skip_if_notional_invalid = lambda n, **_k: n
    ex._blocks_new_active_ticker = lambda _s: False
    ex._apply_concentration_cap = lambda _s, n: n
    ex._min_notional = lambda: 1.0
    monkeypatch.setattr(
        "modules.real_time_data.get_latest_price", lambda _sym: None, raising=False
    )
    submitted = []

    def fake_submit(order, **_kwargs):
        submitted.append(order)
        if getattr(order, "notional", None) is not None:
            raise AlpacaValidationError('asset "PS" is not fractionable') from _api_error(
                PS_BODY
            )
        return SimpleNamespace(id="qty-retry")

    ex._submit_order = fake_submit
    try:
        result = ex.execute_order("PS", "buy", notional=824.75)
        assert result is not None
        assert result.id == "qty-retry"
        assert len(submitted) == 2
        assert submitted[0].notional == pytest.approx(824.75)
        assert float(submitted[1].qty) == 10
        assert getattr(submitted[1], "notional", None) in (None, 0)
    finally:
        executor_module._NON_FRACTIONABLE_ASSETS.discard("PS")


def test_mark_not_fractionable_session_cache():
    executor_module._NON_FRACTIONABLE_ASSETS.discard("PS")
    try:
        AlpacaExecutor._mark_not_fractionable("PS")
        ex = AlpacaExecutor.__new__(AlpacaExecutor)
        assert ex._is_not_fractionable("PS")
    finally:
        executor_module._NON_FRACTIONABLE_ASSETS.discard("PS")
