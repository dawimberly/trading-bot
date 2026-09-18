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
    assert config.paper_lab_max_names() == 8
    assert config.paper_lab_max_hold_days() == 30
    assert config.paper_lab_half_gain_pct() == 0.20
    assert config.paper_lab_trail_arm_pct() == 0.10
    assert config.paper_lab_disaster_pct() == 0.10


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
    assert lab.get("MAX_ACTIVE_TICKERS") == "8"
    assert lab.get("PAPER_LAB_MAX_NAMES") == "8"
    assert lab.get("PAPER_LAB_MAX_HOLD_DAYS") == "30"
    assert lab.get("PAPER_LAB_HALF_GAIN_PCT") == "0.20"
    assert lab.get("PAPER_LAB_TRAIL_ARM_PCT") == "0.10"
    assert lab.get("PAPER_LAB_ADD_POLICY") == "idle"
    assert lab.get("PAPER_LAB_ADD_MAX_MULT") == "2.0"
    assert lab.get("PAPER_MAX_POSITION_PCT") == "0.15"
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


def test_live_vti_off_effective_nyse_cap_not_frozen_10pct(monkeypatch):
    """VTI-off Live must not keep Live Conservative's frozen ~10% NYSE baseline."""
    monkeypatch.setattr(config, "PAPER_TRADING", False)
    monkeypatch.setattr(config, "LIVE_CONSERVATIVE_ENABLED", True)
    monkeypatch.setattr(config, "VTI_CORE_ENABLED", False)
    monkeypatch.setattr(config, "LIVE_VTI_CORE_PCT", 0.0)
    monkeypatch.setattr(config, "VTI_CORE_PCT", 0.0)
    monkeypatch.setattr(config, "SMALL_ACCOUNT_VTI_CORE_PCT", 0.0)
    monkeypatch.setattr(config, "NYSE_SLEEVE_CAP_PCT", 0.95)
    monkeypatch.setattr(config, "SPY_SLEEVE_CAP_PCT", 0.0)
    monkeypatch.setattr(config, "CRYPTO_SLEEVE_CAP_PCT", 0.0)
    monkeypatch.setattr(config, "STAT_ARB_SLEEVE_CAP_ENABLED", False)
    monkeypatch.setattr(config, "LIVE_ACTIVE_SLEEVE_CHOICE", "nyse")
    monkeypatch.setattr(config, "LIVE_SMALL_ACTIVE_SLEEVE_PCT", 0.95)
    monkeypatch.setattr(config, "paper_aggressive_context", lambda: False)
    monkeypatch.setattr(config, "paper_only_sleeves_active", lambda: False)
    monkeypatch.setattr(config, "is_realistic_research_active", lambda: False)
    monkeypatch.setattr(config, "backtest_paper_sleeves_context", lambda: False)
    monkeypatch.setattr(config, "backtest_live_conservative_context", lambda: False)
    monkeypatch.setattr(config, "is_small_account", lambda equity=None: True)
    monkeypatch.setattr(config, "metal_sleeve_enabled", lambda: False)
    monkeypatch.setattr(config, "paper_sleeve_hard_cap_pct", lambda sleeve: None)

    assert config.live_conservative_lock_active() is False
    assert config.live_conservative_profile_active() is False
    cap = config.effective_sleeve_cap(config.NYSE_SLEEVE_CAP_PCT, sleeve="nyse")
    assert cap >= 0.90, f"expected ~0.95 NYSE room, got frozen-style {cap}"
    # effective_nyse_sleeve_cap_pct is paper-only expansion; Live uses fund_scaled.
    assert config.effective_nyse_sleeve_cap_pct() >= 0.90


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
        "PAPER_LAB_MAX_NAMES",
        "PAPER_LAB_TRAIL_ARM_PCT",
        "PAPER_LAB_MAX_HOLD_DAYS",
        "PAPER_LAB_ADD_POLICY",
        "PAPER_LAB_ADD_MAX_MULT",
    ):
        assert f'"{key}"' in src


def test_lab_add_policy_defaults_idle_on_lab(monkeypatch):
    monkeypatch.setenv("TRADING_BOOK_ID", "alpaca_paper")
    monkeypatch.setenv("PAPER_LAB_CONCENTRATED", "true")
    monkeypatch.delenv("PAPER_LAB_ADD_POLICY", raising=False)
    assert config.paper_lab_add_policy() == "idle"
    monkeypatch.setenv("PAPER_LAB_ADD_POLICY", "fresh")
    assert config.paper_lab_add_policy() == "fresh"
    monkeypatch.setenv("TRADING_BOOK_ID", "alpaca_paper_v2")
    monkeypatch.setenv("PAPER_MEDIUM_STRATEGY", "true")
    monkeypatch.delenv("PAPER_LAB_CONCENTRATED", raising=False)
    assert config.paper_lab_add_policy() == "room"


def test_lab_fresh_buy_room_is_full_ticket_until_2x(monkeypatch):
    monkeypatch.setenv("TRADING_BOOK_ID", "alpaca_paper")
    monkeypatch.setenv("PAPER_LAB_CONCENTRATED", "true")
    monkeypatch.setenv("PAPER_LAB_ADD_POLICY", "fresh")
    monkeypatch.setenv("PAPER_MAX_POSITION_PCT", "0.15")
    monkeypatch.setenv("PAPER_LAB_ADD_MAX_MULT", "2.0")
    equity = 100_000.0
    # Held at the 15% name cap: leftover-room would be 0; fresh allows another 15k.
    room = config.concentration_buy_room(
        current_val=15_000.0, equity=equity, has_position=True
    )
    assert abs(room - 15_000.0) < 0.02
    hard = config.concentration_buy_room(
        current_val=30_000.0, equity=equity, has_position=True
    )
    assert hard == 0.0
    # New name is still 15%, not 30%.
    fresh_new = config.concentration_buy_room(
        current_val=0.0, equity=equity, has_position=False
    )
    assert abs(fresh_new - 15_000.0) < 0.02
    banner = config.format_paper_lab_banner() or ""
    assert "add=fresh" in banner


def test_lab_idle_add_only_when_book_full(monkeypatch):
    monkeypatch.setenv("TRADING_BOOK_ID", "alpaca_paper")
    monkeypatch.setenv("PAPER_LAB_CONCENTRATED", "true")
    monkeypatch.setenv("PAPER_LAB_ADD_POLICY", "idle")
    monkeypatch.setenv("PAPER_MAX_POSITION_PCT", "0.15")
    equity = 100_000.0
    leftover = config.concentration_buy_room(
        current_val=8_000.0,
        equity=equity,
        has_position=True,
        slots_full=False,
    )
    assert abs(leftover - 7_000.0) < 0.02
    no_extra = config.concentration_buy_room(
        current_val=15_000.0,
        equity=equity,
        has_position=True,
        slots_full=False,
    )
    assert no_extra == 0.0
    extra = config.concentration_buy_room(
        current_val=15_000.0,
        equity=equity,
        has_position=True,
        slots_full=True,
        extra_ok=True,
    )
    assert abs(extra - 15_000.0) < 0.02
    chasing = config.concentration_buy_room(
        current_val=15_000.0,
        equity=equity,
        has_position=True,
        slots_full=True,
        extra_ok=False,
    )
    assert chasing == 0.0
    assert config.paper_lab_extra_add_ok(gain_pct=0.12) is False
    assert config.paper_lab_extra_add_ok(age_days=23) is False
    assert config.paper_lab_extra_add_ok(gain_pct=0.04, age_days=10) is True
    banner = config.format_paper_lab_banner() or ""
    assert "idle-cash add" in banner
