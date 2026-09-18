"""Daily ATR stops must not fall back to the 5-minute sizing matrix."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from modules.exit_management import resolve_symbol_atr_and_conviction, reset_daily_atr_cache
from modules.smart_atr_stops import compute_stop_price, ensure_initial_stop, evaluate_smart_stop


def test_resolve_atr_ignores_five_minute_matrix(monkeypatch):
    reset_daily_atr_cache()
    monkeypatch.setattr(
        "modules.exit_management._daily_bar_atr",
        lambda symbol: None,
    )
    # Tiny 5-minute range that used to stamp a 0.1% stop.
    data = pd.DataFrame({"PFE": [25.00, 25.02, 25.01, 25.03, 25.00] * 8})
    pos = SimpleNamespace(avg_entry_price=25.0, current_price=25.0)
    executor = SimpleNamespace(
        _sizing_data=data,
        _find_position=lambda symbol: pos,
    )
    atr, _conv = resolve_symbol_atr_and_conviction(executor, "PFE")
    assert atr == 0.5  # 2% of $25 entry, not ~$0.02 5-minute ATR


def test_ensure_initial_stop_restamps_hair_trigger():
    entry = 25.0
    noisy = ensure_initial_stop({}, entry=entry, atr=0.02, side="long")
    tight = float(noisy["smart_stop_price"])
    assert abs(entry - tight) < 0.10
    daily = ensure_initial_stop(noisy, entry=entry, atr=0.80, side="long")
    wide = float(daily["smart_stop_price"])
    assert wide < tight
    assert abs(entry - wide) > 1.0


def test_evaluate_does_not_stop_on_quote_noise_after_daily_atr():
    entry = 25.0
    current = 24.95  # −0.2% — would hit a 5-minute 2× ATR stop
    decision = evaluate_smart_stop(
        symbol="PFE",
        entry=entry,
        current=current,
        atr=0.80,
        meta={},
        qty=10,
        side="long",
    )
    stop = compute_stop_price(entry, 0.80, multiplier=2.0)
    assert current > stop
    assert decision.get("action") is None
