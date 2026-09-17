"""Lab vs Medium book gates (no Alpaca, no .env secrets)."""

from __future__ import annotations

import os
from types import SimpleNamespace

import config
from modules import alpaca_executor as ae
from modules.alpaca_executor import AlpacaExecutor
from modules.pipeline_strategies import regime_entries_paused
from modules.position_exits import _max_hold_bars


def test_medium_enabled_only_on_v2(monkeypatch):
    monkeypatch.setenv("TRADING_BOOK_ID", "alpaca_paper_v2")
    monkeypatch.setenv("PAPER_MEDIUM_STRATEGY", "true")
    monkeypatch.setenv("MAX_ACTIVE_TICKERS", "15")
    monkeypatch.delenv("PAPER_LAB_CONCENTRATED", raising=False)
    assert config.paper_medium_strategy_enabled() is True
    assert config.paper_lab_concentrated_enabled() is False
    assert config.effective_max_active_tickers() == 15
    assert config.effective_exit_optimization_enabled() is False
    assert _max_hold_bars() == int(config.PAPER_POSITION_MAX_HOLD_BARS)


def test_lab_not_medium(monkeypatch):
    monkeypatch.setenv("TRADING_BOOK_ID", "alpaca_paper")
    monkeypatch.setenv("PAPER_LAB_CONCENTRATED", "true")
    monkeypatch.delenv("PAPER_MEDIUM_STRATEGY", raising=False)
    assert config.paper_lab_concentrated_enabled() is True
    assert config.paper_medium_strategy_enabled() is False
    assert config.effective_max_active_tickers() == config.paper_lab_max_names()


def test_live_book_gets_neither(monkeypatch):
    monkeypatch.setenv("TRADING_BOOK_ID", "alpaca_live")
    monkeypatch.setenv("PAPER_MEDIUM_STRATEGY", "true")
    monkeypatch.setenv("PAPER_LAB_CONCENTRATED", "true")
    assert config.paper_medium_strategy_enabled() is False
    assert config.paper_lab_concentrated_enabled() is False


def test_lab_sold_today_roundtrip(tmp_path, monkeypatch):
    journal = tmp_path / "paper_journal.csv"
    journal.write_text("x", encoding="utf-8")
    monkeypatch.setenv("PAPER_JOURNAL_CSV", str(journal))
    monkeypatch.setenv("TRADING_BOOK_ID", "alpaca_paper")
    assert ae._lab_sold_today("AAPL") is False
    ae._record_lab_sold_today("AAPL")
    assert ae._lab_sold_today("AAPL") is True
    assert ae._lab_sold_today("MSFT") is False
    payload = (tmp_path / "lab_sold_today.json").read_text(encoding="utf-8")
    assert "AAPL" in payload


def test_medium_trims_smallest_over_cap(monkeypatch):
    monkeypatch.setenv("TRADING_BOOK_ID", "alpaca_paper_v2")
    monkeypatch.setenv("PAPER_MEDIUM_STRATEGY", "true")
    monkeypatch.setenv("MAX_ACTIVE_TICKERS", "15")
    positions = []
    for i in range(16):
        qty = 1.0
        px = 10.0 + i
        mv = qty * px
        positions.append(
            SimpleNamespace(
                symbol=f"T{i:02d}",
                qty=qty,
                current_price=px,
                avg_entry_price=px,
                market_value=mv,
            )
        )
    positions.append(
        SimpleNamespace(
            symbol="VTI",
            qty=10,
            current_price=200,
            avg_entry_price=200,
            market_value=2000,
        )
    )
    ex = AlpacaExecutor.__new__(AlpacaExecutor)
    ex.dry_run = True
    ex.equity_session_open = True
    ex._positions = positions
    ex._get_positions = lambda: list(ex._positions)
    ex._normalize_pos_symbol = AlpacaExecutor._normalize_pos_symbol
    ex._is_core_exempt = lambda sym: config.normalize_symbol(sym) == "VTI"
    ex._position_market_value = lambda pos: float(pos.market_value)
    actions = ex.trim_over_active_tickers(dry_run=True)
    assert len(actions) == 1
    assert actions[0]["symbol"] == "T00"
    assert actions[0]["reason"] == "medium_name_cap"


def test_medium_name_cap_skips_when_session_closed(monkeypatch):
    monkeypatch.setenv("TRADING_BOOK_ID", "alpaca_paper_v2")
    monkeypatch.setenv("PAPER_MEDIUM_STRATEGY", "true")
    ex = AlpacaExecutor.__new__(AlpacaExecutor)
    ex.equity_session_open = False
    assert ex.trim_over_active_tickers(dry_run=True) == []


def test_dynamic_sizing_does_not_hard_pause_rhyme_e(monkeypatch):
    monkeypatch.setattr(config, "effective_regime_dynamic_sizing", lambda: True)
    assert regime_entries_paused("RHYME_E: Steady_Bearish_Decline") is False
    assert regime_entries_paused("RHYME_B: Panic_Volatility") is False


def test_live_without_dynamic_sizing_still_pauses_rhyme_e(monkeypatch):
    monkeypatch.setattr(config, "effective_regime_dynamic_sizing", lambda: False)
    monkeypatch.setattr(config, "effective_paper_soft_pause", lambda: False)
    assert regime_entries_paused("RHYME_E: Steady_Bearish_Decline") is True


def test_medium_user_bot_env_vti_core_off():
    """Medium SoT keeps 15-name ATR profile; VTI core is off (no 33/67)."""
    from pathlib import Path

    from modules.portal_bot import user_bot_env
    from modules.portal_paths import bind_project_root

    bind_project_root(Path(__file__).resolve().parents[1])
    u = "dawimberly"
    v2 = user_bot_env(u, "alpaca_paper_v2")
    lab = user_bot_env(u, "alpaca_paper")
    assert v2.get("PAPER_MEDIUM_STRATEGY") == "true"
    assert v2.get("PAPER_DYNAMIC_VTI") == "false"
    assert v2.get("VTI_CORE_ENABLED") == "false"
    assert v2.get("PAPER_VTI_CORE_PCT") == "0"
    assert v2.get("NYSE_SLEEVE_CAP_PCT") == "0.95"
    assert v2.get("VTI_REBALANCE_CADENCE") == "drift"
    assert v2.get("ATR_STOP_MULTIPLIER") == "2.0"
    assert lab.get("PAPER_LAB_CONCENTRATED") == "true"
    assert lab.get("MAX_ACTIVE_TICKERS") == "4"
    assert lab.get("PAPER_DYNAMIC_VTI") == "false"
    assert lab.get("VTI_CORE_ENABLED") == "false"
    assert lab.get("PAPER_VTI_CORE_PCT") == "0"
    assert lab.get("NYSE_SLEEVE_CAP_PCT") == "0.95"


def test_live_user_bot_env_vti_core_off():
    from pathlib import Path

    from modules.portal_bot import user_bot_env
    from modules.portal_paths import bind_project_root

    bind_project_root(Path(__file__).resolve().parents[1])
    live = user_bot_env("dawimberly", "alpaca_live")
    assert live.get("VTI_CORE_ENABLED") == "false"
    assert live.get("LIVE_VTI_CORE_PCT") == "0"
    assert live.get("LIVE_SMALL_ACTIVE_SLEEVE_PCT") == "0.95"
    assert live.get("LIVE_ACTIVE_SLEEVE_CHOICE") == "nyse"
    assert live.get("VTI_REBALANCE_CADENCE") == "drift"
    assert live.get("NYSE_SLEEVE_CAP_PCT") == "0.95"


def test_dotenv_overlay_includes_medium_vti_keys():
    import inspect

    src = inspect.getsource(config._load_project_dotenv)
    for key in (
        "PAPER_DYNAMIC_VTI",
        "PAPER_VTI_CORE_PCT",
        "NYSE_SLEEVE_CAP_PCT",
        "PAPER_NYSE_MAX_EXPOSURE_PCT",
        "LIVE_VTI_CORE_PCT",
        "VTI_REBALANCE_CADENCE",
    ):
        assert f'"{key}"' in src
