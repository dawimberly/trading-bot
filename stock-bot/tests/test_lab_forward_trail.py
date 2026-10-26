"""Lab forward trail. Isolated fixtures and mocked broker responses only."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import config
from modules import lab_forward_trail as trail
from modules import purchase_plans
from modules.alpaca_executor import AlpacaExecutor, note_lab_sell
from modules.pipeline_strategies import run_equity_strategy
from modules.position_exits import run_position_exits

T0 = datetime(2026, 10, 19, 14, 0, tzinfo=timezone.utc)


def _lab(monkeypatch, tmp_path, book="alpaca_paper"):
    folder = tmp_path / "users" / "owner" / "books" / book
    folder.mkdir(parents=True)
    heartbeat = folder / "bot_heartbeat.json"
    heartbeat.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("TRADING_BOOK_ID", book)
    monkeypatch.setenv("HEARTBEAT_FILE", str(heartbeat))
    monkeypatch.setenv("PAPER_JOURNAL_CSV", str(folder / "paper_journal.csv"))
    if book == "alpaca_paper":
        monkeypatch.setenv("PAPER_LAB_CONCENTRATED", "true")
    else:
        monkeypatch.delenv("PAPER_LAB_CONCENTRATED", raising=False)
    monkeypatch.delenv("PAPER_NYSE_A2B1_ENABLED", raising=False)
    monkeypatch.setattr(trail, "_TEST_NOW", T0)
    return folder


def _pos(symbol="AAPL", qty=1, avg=100, price=100):
    return SimpleNamespace(
        symbol=symbol,
        qty=qty,
        avg_entry_price=avg,
        current_price=price,
        created_at=T0,
    )


class _Risk:
    def _log_event(self, _msg):
        return None


class _Exec:
    def __init__(self):
        self.positions = []
        self.orders = []
        self.exit_calls = []
        self.filled = False
        self.fresh = None
        self.fail_submit = False
        self.orders_error = False

    def _get_positions(self):
        return list(self.positions)

    def _get_account(self):
        return SimpleNamespace(equity=100000)

    def open_orders_for_symbol(self, _symbol):
        if self.orders_error:
            raise RuntimeError("broker orders unavailable")
        return list(self.orders)

    def execute_full_exit(self, symbol, reason="exit", sleeve=None):
        self.exit_calls.append((symbol, reason, sleeve))
        if self.fail_submit:
            return None
        return SimpleNamespace(id="order-1", status="new")

    def order_filled(self, _order):
        return self.filled

    def order_fill_details(self, _order):
        return {
            "filled": self.filled,
            "partial": not self.filled,
            "status": "filled" if self.filled else "new",
            "confirmed_fill": False,
        }

    def _fresh_position(self, _symbol):
        return self.fresh


def test_arm_thresholds():
    assert trail.arms("104.99", "100") is False
    assert trail.arms("105", "100") is True
    assert trail.arms("105.01", "100") is True
    assert trail.arms(Decimal("105"), Decimal("100")) is True


def test_breach_is_exact_two_percent():
    assert trail.breaches("98.01", "100") is False
    assert trail.breaches("98", "100") is True
    assert trail.breaches("97.99", "100") is True


def test_higher_peak_and_rebound_do_not_exit(monkeypatch, tmp_path):
    _lab(monkeypatch, tmp_path)
    state = trail._empty()
    trail.observe(state, "AAPL", "100", "105", T0, initialization="first_observed_mark")
    trail.observe(state, "AAPL", "100", "110", T0, initialization="first_observed_mark")
    assert state["positions"]["AAPL"]["peak"] == "110"
    rebound = trail.observe(
        state, "AAPL", "100", "107.81", T0, initialization="first_observed_mark"
    )
    assert state["positions"]["AAPL"]["exit_pending"] is False
    assert rebound == []
    # 110 * 0.98 = 107.80 exactly.
    hit = trail.observe(state, "AAPL", "100", "107.80", T0, initialization="first_observed_mark")
    assert state["positions"]["AAPL"]["exit_pending"] is True
    assert hit[-1]["event"] == "breach"
    bounced = trail.observe(
        state, "AAPL", "130", "120", T0, initialization="first_observed_mark"
    )
    assert bounced == []
    assert state["positions"]["AAPL"]["exit_pending"] is True
    assert state["positions"]["AAPL"]["peak"] == "110"


def test_adds_before_and_after_arming(monkeypatch, tmp_path):
    _lab(monkeypatch, tmp_path)
    state = trail._empty()
    trail.observe(state, "AAPL", "100", "104", T0, initialization="first_observed_mark")
    assert state["positions"]["AAPL"]["armed"] is False
    # Updated average after an add. 94.50 is 5% above 90, and was not 5% above 100.
    trail.observe(state, "AAPL", "90", "94.50", T0, initialization="first_observed_mark")
    assert state["positions"]["AAPL"]["armed"] is True
    assert state["positions"]["AAPL"]["peak"] == "94.50"
    trail.observe(state, "AAPL", "94.50", "100", T0, initialization="first_observed_mark")
    assert state["positions"]["AAPL"]["peak"] == "100"
    # A later add changes the average and does not disarm or reset the peak.
    trail.observe(state, "AAPL", "120", "99", T0, initialization="first_observed_mark")
    episode = state["positions"]["AAPL"]
    assert episode["armed"] is True
    assert episode["peak"] == "100"
    assert episode["exit_pending"] is False


def test_pending_exit_blocks_adds_and_keeps_peak(monkeypatch, tmp_path):
    folder = _lab(monkeypatch, tmp_path)
    state = trail._empty()
    trail.observe(state, "AAPL", "100", "105", T0, initialization="first_observed_mark")
    trail.observe(state, "AAPL", "100", "98", T0, initialization="first_observed_mark")
    trail.save_state(state)
    assert trail.buy_block_reason("AAPL", T0) == "lab_forward_exit_pending"
    assert trail.buy_block_reason("MSFT", T0) is None
    again = trail.observe(state, "AAPL", "80", "110", T0, initialization="first_observed_mark")
    assert again == []
    assert state["positions"]["AAPL"]["peak"] == "105"
    events = (folder / trail.EVENT_NAME).read_text(encoding="utf-8")
    assert "add_blocked_exit_pending" in events


def test_exit_attempts_partial_reconcile_and_flat(monkeypatch, tmp_path):
    folder = _lab(monkeypatch, tmp_path)
    executor = _Exec()
    executor.positions = [_pos(price=105)]
    run_position_exits(executor, _Risk())
    assert executor.exit_calls == []
    stored = json.loads((folder / trail.STATE_NAME).read_text(encoding="utf-8"))
    assert stored["positions"]["AAPL"]["armed"] is True
    assert Decimal(stored["positions"]["AAPL"]["peak"]) == Decimal("105")
    assert "historical_peak" not in stored["positions"]["AAPL"]

    executor.positions = [_pos(price=102.9)]
    executor.fail_submit = True
    run_position_exits(executor, _Risk())
    assert executor.exit_calls == [("AAPL", trail.EXIT_REASON, "NYSE")]
    stored = json.loads((folder / trail.STATE_NAME).read_text(encoding="utf-8"))
    assert stored["positions"]["AAPL"]["exit_pending"] is True
    assert "AAPL" not in stored["cooldowns"]

    executor.fail_submit = False
    executor.orders = [{"id": "working", "side": "sell", "status": "new", "qty": "1"}]
    executor.positions = [_pos(price=110)]
    run_position_exits(executor, _Risk())
    assert len(executor.exit_calls) == 1
    stored = json.loads((folder / trail.STATE_NAME).read_text(encoding="utf-8"))
    assert Decimal(stored["positions"]["AAPL"]["peak"]) == Decimal("105")

    executor.orders = []
    executor.filled = False
    executor.fresh = _pos(qty="0.4", price=102)
    run_position_exits(executor, _Risk())
    assert len(executor.exit_calls) == 2
    stored = json.loads((folder / trail.STATE_NAME).read_text(encoding="utf-8"))
    assert stored["positions"]["AAPL"]["exit_pending"] is True
    assert "AAPL" not in stored["cooldowns"]

    executor.filled = True
    executor.fresh = None
    run_position_exits(executor, _Risk())
    stored = json.loads((folder / trail.STATE_NAME).read_text(encoding="utf-8"))
    assert "AAPL" not in stored["positions"]
    assert stored["cooldowns"]["AAPL"]["flat_at"] == T0.isoformat()
    text = (folder / trail.EVENT_NAME).read_text(encoding="utf-8")
    assert "exit_attempt" in text
    assert "exit_wait_open_order" in text
    assert '"confirmed_fill":false' in text.replace(" ", "")
    assert '"confirmed_fill":true' in text.replace(" ", "") or '"confirmed_fill": true' in text


def test_unreadable_open_orders_do_not_duplicate(monkeypatch, tmp_path):
    _lab(monkeypatch, tmp_path)
    executor = _Exec()
    executor.positions = [_pos(price=105)]
    run_position_exits(executor, _Risk())
    executor.positions = [_pos(price=98)]
    executor.orders_error = True
    run_position_exits(executor, _Risk())
    assert executor.exit_calls == []
    executor.orders_error = False
    executor.orders = []
    run_position_exits(executor, _Risk())
    assert executor.exit_calls == [("AAPL", trail.EXIT_REASON, "NYSE")]


def test_cooldown_starts_at_flat_and_expires_at_thirty_minutes(monkeypatch, tmp_path):
    _lab(monkeypatch, tmp_path)
    state = trail._empty()
    trail.observe(state, "AAPL", "100", "105", T0, initialization="first_observed_mark")
    trail.observe(state, "AAPL", "100", "98", T0, initialization="first_observed_mark")
    assert trail.cooldown_blocks(state, "AAPL", T0) is False
    event = trail.confirm_flat(state, "AAPL", T0)
    assert event["cooldown_expires_at"] == (T0 + timedelta(minutes=30)).isoformat()
    assert "AAPL" not in state["positions"]
    just_before = T0 + timedelta(minutes=30) - timedelta(microseconds=1)
    exactly = T0 + timedelta(minutes=30)
    assert trail.cooldown_blocks(state, "AAPL", just_before) is True
    assert trail.cooldown_blocks(state, "AAPL", exactly) is False
    assert trail.cooldown_blocks(state, "MSFT", just_before) is False
    trail.save_state(state)
    assert trail.buy_block_reason("AAPL", just_before) == "lab_forward_cooldown"
    assert trail.buy_block_reason("MSFT", just_before) is None
    assert trail.buy_block_reason("AAPL", exactly) is None


def test_same_day_and_ma50_guards_on_the_entry_path(monkeypatch, tmp_path):
    folder = _lab(monkeypatch, tmp_path)
    note_lab_sell("AAPL", "lab_disaster")
    assert trail_sold(folder, "AAPL")
    note_lab_sell("MSFT", trail.EXIT_REASON)
    assert not trail_sold(folder, "MSFT")

    state = trail._empty()
    state["cooldowns"]["AAPL"] = {
        "episode_id": "ep",
        "flat_at": T0.isoformat(),
        "expires_at": (T0 + timedelta(minutes=30)).isoformat(),
        "expiry_logged": False,
        "reentry_consumed": False,
    }
    trail.save_state(state)
    executor = AlpacaExecutor.__new__(AlpacaExecutor)
    executor._equity_trading_allowed = lambda _symbol: True
    executor._is_unknown_asset = lambda _symbol: False

    def _cancel(_symbol):
        raise AssertionError("cooldown must return before canceling orders")

    executor._cancel_open_orders_for = _cancel
    assert executor.execute_order("AAPL", "buy", notional=100, reason="AAPL/MA50", sleeve="NYSE") is None

    calls = []

    class Entry:
        equity_session_open = True

        def execute_order(self, symbol, side, notional=None, reason="", sleeve=None):
            calls.append((symbol, side, reason))
            return SimpleNamespace(id="buy-1", status="filled")

        def order_filled(self, _order, max_wait=5):
            return True

        def _get_account(self):
            return SimpleNamespace(equity=100000)

        def compute_nyse_notional(self):
            return 1000

    import modules.pipeline_strategies as pipeline

    monkeypatch.setattr(pipeline, "regime_entries_paused", lambda *_a, **_k: False)
    monkeypatch.setattr(pipeline, "_nyse_equity_columns", lambda _data: ["AAPL"])
    monkeypatch.setattr(
        pipeline,
        "_equity_momentum_ranked",
        lambda *_a, **_k: ["AAPL"],
    )
    monkeypatch.setattr(pipeline, "_nyse_momentum_quality_skip", lambda *_a, **_k: None)
    monkeypatch.setattr(config, "effective_min_notional", lambda *_a, **_k: 1)
    monkeypatch.setattr(config, "NYSE_BETA_SCALING_ENABLED", False)
    monkeypatch.setattr(config, "effective_conviction_sizing_enabled", lambda: False)
    monkeypatch.setattr(config, "effective_correlation_guard_enabled", lambda: False)
    monkeypatch.setattr(config, "effective_insider_signal_boost_enabled", lambda: False)
    monkeypatch.setattr(pipeline, "_apply_ipo_buy_notional", lambda _s, notional, *_a, **_k: notional)

    # Inside the 30 minutes the MA50 exemption is off, and execute_order blocks.
    blocked = Entry()
    blocked.execute_order = executor.execute_order
    pair = {"AAPL/MA50": T0}
    run_equity_strategy(SimpleNamespace(), blocked, "RHYME_A", T0, pair)
    assert calls == []

    # At expiry, the same-day file would still block a disaster sell.
    # This name's policy exit did not write it. The hour-long MA50 stamp is exempt.
    state["cooldowns"]["AAPL"]["expires_at"] = T0.isoformat()
    trail.save_state(state)
    monkeypatch.setattr(trail, "_TEST_NOW", T0)
    run_equity_strategy(SimpleNamespace(), Entry(), "RHYME_A", T0, {"AAPL/MA50": T0})
    assert calls and calls[0][0] == "AAPL"
    assert trail.ma50_pair_cooldown_exempt("AAPL", T0) is False

    # A different name still honors the MA50 hour.
    calls.clear()
    monkeypatch.setattr(pipeline, "_nyse_equity_columns", lambda _data: ["MSFT"])
    monkeypatch.setattr(pipeline, "_equity_momentum_ranked", lambda *_a, **_k: ["MSFT"])
    run_equity_strategy(SimpleNamespace(), Entry(), "RHYME_A", T0, {"MSFT/MA50": T0})
    assert calls == []


def trail_sold(folder, symbol) -> bool:
    path = folder / "lab_sold_today.json"
    if not path.is_file():
        return False
    payload = json.loads(path.read_text(encoding="utf-8"))
    return symbol in payload.get("symbols", [])


def test_persistence_restart_corrupt_and_new_episode(monkeypatch, tmp_path):
    folder = _lab(monkeypatch, tmp_path)
    state = trail._empty()
    trail.observe(state, "AAPL", "100", "105", T0, initialization="first_observed_mark")
    trail.observe(state, "AAPL", "100", "98", T0, initialization="first_observed_mark")
    trail.save_state(state)
    temporary = folder / (trail.STATE_NAME + ".tmp")
    assert not temporary.exists()
    reloaded = trail.load_state()
    assert reloaded["positions"]["AAPL"]["exit_pending"] is True
    assert reloaded["book"] == "alpaca_paper"

    path = folder / trail.STATE_NAME
    path.write_text("{", encoding="utf-8")
    try:
        trail.load_state()
        raised = False
    except trail.CorruptState:
        raised = True
    assert raised
    assert path.read_text(encoding="utf-8") == "{"
    assert trail.on_position("AAPL", "100", "50", open_orders=[]) == "corrupt"
    assert path.read_text(encoding="utf-8") == "{"

    path.write_text(json.dumps(reloaded), encoding="utf-8")
    flat = trail.load_state()
    trail.confirm_flat(flat, "AAPL", T0)
    first = "gone"
    trail.save_state(flat)
    trail.observe(flat, "AAPL", "200", "210", T0, initialization="new_episode")
    assert flat["positions"]["AAPL"]["episode_id"] != reloaded["positions"]["AAPL"]["episode_id"]
    assert flat["positions"]["AAPL"]["peak"] == "210"
    assert flat["cooldowns"]["AAPL"]["expires_at"] == (T0 + timedelta(minutes=30)).isoformat()
    assert first


def test_existing_position_uses_current_mark_only(monkeypatch, tmp_path):
    folder = _lab(monkeypatch, tmp_path)
    executor = _Exec()
    executor.positions = [_pos(price=106)]
    run_position_exits(executor, _Risk())
    stored = json.loads((folder / trail.STATE_NAME).read_text(encoding="utf-8"))
    episode = stored["positions"]["AAPL"]
    assert episode["initialization"] == "first_observed_mark"
    assert episode["armed"] is True
    assert Decimal(episode["peak"]) == Decimal("106")
    assert Decimal(episode["average_entry_at_arm"]) == Decimal("100")
    text = (folder / trail.EVENT_NAME).read_text(encoding="utf-8")
    assert '"historical_peak_used":false' in text.replace(" ", "")
    assert "999" not in text


def test_half_and_old_trail_do_not_fire(monkeypatch, tmp_path):
    _lab(monkeypatch, tmp_path)
    executor = _Exec()
    executor.positions = [_pos(price=125)]
    run_position_exits(executor, _Risk())
    assert executor.exit_calls == []
    executor.positions = [_pos(avg=100, price=89)]
    run_position_exits(executor, _Risk())
    assert executor.exit_calls == [("AAPL", "lab_disaster", "NYSE")]


def test_live_and_medium_are_unchanged(monkeypatch, tmp_path):
    for book in ("alpaca_live", "alpaca_paper_v2"):
        folder = _lab(monkeypatch, tmp_path / book, book=book)
        if book == "alpaca_paper_v2":
            monkeypatch.setenv("PAPER_MEDIUM_STRATEGY", "true")
        monkeypatch.setattr(config, "effective_smart_stops_enabled", lambda: False)
        monkeypatch.setattr(config, "effective_exit_optimization_enabled", lambda: False)
        executor = _Exec()
        executor.positions = [_pos(price=130)]
        run_position_exits(executor, _Risk())
        assert executor.exit_calls == []
        assert not (folder / trail.STATE_NAME).exists()
    monkeypatch.setenv("TRADING_BOOK_ID", "alpaca_live")
    rules = purchase_plans.exit_rules(
        SimpleNamespace(
            paper_lab_concentrated_enabled=lambda: False,
            effective_smart_stops_enabled=lambda: False,
            effective_exit_optimization_enabled=lambda: False,
            STOP_LOSS_PCT=0.05,
        )
    )
    assert rules["kind"] == "fixed_entry_stop"


def test_lab_purchase_plan_describes_the_experiment():
    rules = purchase_plans.exit_rules(
        SimpleNamespace(
            paper_lab_concentrated_enabled=lambda: True,
            paper_lab_disaster_pct=lambda: 0.10,
            paper_lab_max_hold_days=lambda: 30,
            paper_nyse_a2b1_enabled=lambda: True,
        )
    )
    assert rules["kind"] == "lab_forward_peak_exit"
    assert rules["backtest_validated"] is False
    assert rules["paper_forward_experiment"] is True
    assert rules["arm_multiple"] == 1.05
    assert rules["peak_exit_multiple"] == 0.98
    assert rules["cooldown_minutes"] == 30
    assert rules["half_gain_fraction"] is None
    assert rules["trail_fraction"] is None
    assert rules["disaster_fraction"] == 0.10
    medium = purchase_plans.exit_rules(
        SimpleNamespace(
            paper_lab_concentrated_enabled=lambda: False,
            effective_smart_stops_enabled=lambda: True,
            paper_medium_strategy_enabled=lambda: True,
            ATR_STOP_MULTIPLIER=2,
            ATR_PERIOD=14,
            PAPER_POSITION_MAX_HOLD_BARS=30,
            STOP_LOSS_REEVAL_PCTS=[-0.5, -0.5],
        )
    )
    assert medium["kind"] == "dynamic_smart_atr"
    assert medium["max_hold"] == 30


def test_cross_book_path_is_refused(monkeypatch, tmp_path):
    lab = _lab(monkeypatch, tmp_path)
    other = tmp_path / "users" / "owner" / "books" / "alpaca_paper_v2"
    other.mkdir()
    planted = other / trail.STATE_NAME
    planted.write_text('{"book":"alpaca_paper_v2"}', encoding="utf-8")
    monkeypatch.setenv("HEARTBEAT_FILE", str(other / "bot_heartbeat.json"))
    monkeypatch.setenv("PAPER_JOURNAL_CSV", str(other / "paper_journal.csv"))
    assert trail.book_dir() is None
    assert planted.read_text(encoding="utf-8") == '{"book":"alpaca_paper_v2"}'
    assert not (lab / trail.STATE_NAME).exists()
