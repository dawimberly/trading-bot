"""Tests for daily error summary + digest helpers (no Telegram/network)."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from modules import error_watcher

_ET = ZoneInfo("America/New_York")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def test_error_label_buckets_insufficient_and_import():
    assert (
        error_watcher.error_label(
            {
                "event": "order_failed",
                "error": '{"message":"insufficient qty available for order"}',
            }
        )
        == "insufficient qty"
    )
    assert (
        error_watcher.error_label(
            {"event": "exception", "error_type": "ImportError", "error": "cannot import"}
        )
        == "ImportError"
    )


def test_daily_summary_markdown_and_write(tmp_path, monkeypatch):
    day = date(2026, 7, 23)
    # 18:00 UTC = 14:00 ET on that day
    rows = [
        {
            "ts": "2026-07-23T18:00:00+00:00",
            "id": "aaa111",
            "event": "exception",
            "error_type": "ImportError",
            "error": "cannot import name X",
            "file_line": "run_all.py:1017",
            "fix_area": "run_all.py",
            "symbol": None,
        },
        {
            "ts": "2026-07-23T18:10:00+00:00",
            "id": "bbb222",
            "event": "exception",
            "error_type": "ImportError",
            "error": "cannot import name X again",
            "file_line": "run_all.py:1017",
            "fix_area": "run_all.py",
        },
        {
            "ts": "2026-07-23T18:20:00+00:00",
            "id": "ccc333",
            "event": "order_failed",
            "error_type": "OrderFailed",
            "error": "insufficient qty available for order (requested: 128)",
            "symbol": "XLE",
            "file_line": "alpaca_executor.py:254",
            "fix_area": "modules/alpaca_executor.py",
        },
        # Different ET day — ignored
        {
            "ts": "2026-07-22T18:00:00+00:00",
            "id": "old999",
            "event": "exception",
            "error_type": "ValueError",
            "error": "yesterday",
        },
    ]
    src = tmp_path / "bot_errors.jsonl"
    out = tmp_path / "daily_errors_2026-07-23.md"
    _write_jsonl(src, rows)

    monkeypatch.setattr(error_watcher, "_enabled", lambda: True)
    loaded = error_watcher.load_errors_for_et_date(day, path=src)
    assert len(loaded) == 3

    md = error_watcher.format_daily_errors_markdown(loaded, day=day)
    assert "**Count today:** 3" in md
    assert "`ImportError` × 2" in md
    assert "`insufficient qty` × 1" in md
    assert "aaa111" in md or "`aaa111`" in md
    assert "XLE" in md
    assert "Cursor prompts (top 3)" in md
    assert "Investigate and fix top issue **ImportError**" in md

    digest = error_watcher.format_daily_digest_message(loaded)
    assert digest.startswith("Today's errors: 3")
    assert "ImportError (x2)" in digest
    assert "insufficient qty (x1)" in digest

    written = error_watcher.write_daily_error_summary(
        day=day, errors_path=src, out_path=out
    )
    assert written == out
    assert out.is_file()
    assert "Count today:** 3" in out.read_text(encoding="utf-8")


def test_daily_digest_due_respects_flag_and_time(monkeypatch, tmp_path):
    import config

    monkeypatch.setattr(error_watcher, "_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(config, "TELEGRAM_DAILY_ERROR_DIGEST", True)
    monkeypatch.setattr(config, "TELEGRAM_DAILY_ERROR_DIGEST_TIME", "16:45")

    before = datetime(2026, 7, 23, 16, 0, tzinfo=_ET)
    assert error_watcher.daily_error_digest_due(now_et=before) is False

    after = datetime(2026, 7, 23, 16, 45, tzinfo=_ET)
    assert error_watcher.daily_error_digest_due(now_et=after) is True

    # Latch
    error_watcher._save_state({"last_daily_error_digest": "2026-07-23"})
    assert error_watcher.daily_error_digest_due(now_et=after) is False


def test_watcher_banner_mentions_daily_log():
    text = error_watcher.watcher_banner()
    assert "daily log" in text.lower() or "Error watcher" in text


def test_install_uncaught_exception_hooks_logs_unicode(monkeypatch):
    """Pre-loop crashes must hit error_watcher (not only stderr)."""
    seen: list[tuple] = []

    def _fake_log(exc, *, context="", extra=None):
        seen.append((type(exc).__name__, context, str(exc)))
        return "testid"

    monkeypatch.setattr(error_watcher, "log_exception", _fake_log)
    error_watcher._HOOKS_INSTALLED = False
    error_watcher.install_uncaught_exception_hooks()
    # Idempotent
    error_watcher.install_uncaught_exception_hooks()

    exc = UnicodeEncodeError("charmap", "≥", 0, 1, "ordinal not in range")
    sys.excepthook(type(exc), exc, None)
    assert seen, "excepthook should call log_exception"
    assert seen[0][0] == "UnicodeEncodeError"
    assert seen[0][1] == "uncaught"

    # SystemExit must not spam watcher
    seen.clear()
    sys.excepthook(SystemExit, SystemExit(0), None)
    assert seen == []


def test_suggested_fix_area_unicode():
    area = error_watcher._suggested_fix_area(
        UnicodeEncodeError("charmap", "x", 0, 1, "bad"),
        "run_all.py:2556",
    )
    assert "safe_io" in area


def test_format_telegram_automation_banner_error_watcher_line():
    import config

    line = config.format_telegram_automation_banner()
    assert "Thinking TG=" in line and "Regime TG=" in line
    assert "Errors ON" in line or "Errors OFF" in line
    assert "Fills ON" in line or "Fills OFF" in line


def test_thinking_telegram_gated_off_by_default(monkeypatch):
    import config
    from modules import thinking_engine as te

    monkeypatch.setattr(config, "TELEGRAM_ALERT_THINKING", False)
    sent = []

    def _fake_broadcast(*_a, **_k):
        sent.append(1)
        return True

    monkeypatch.setattr(
        "modules.alerts.broadcast",
        _fake_broadcast,
        raising=False,
    )
    # Patch import path used inside helper
    import modules.alerts as alerts_mod

    monkeypatch.setattr(alerts_mod, "broadcast", _fake_broadcast)
    te._send_thinking_telegram(
        regime="RHYME_C: Steady_Bullish_Growth",
        confidence=0.8,
        tilt={"vti": 0.5, "cash": 0.1},
        disposition="accepted",
    )
    assert sent == []


def test_regime_telegram_gated_off_by_default(monkeypatch):
    import config
    from modules import market_context as mc

    monkeypatch.setattr(config, "TELEGRAM_ALERT_REGIME", False)
    sent = []

    def _fake_broadcast(*_a, **_k):
        sent.append(1)
        return True

    import modules.alerts as alerts_mod

    monkeypatch.setattr(alerts_mod, "broadcast", _fake_broadcast)
    mc.reset_regime_hysteresis()
    mc.announce_regime_change("RHYME_C: Steady_Bullish_Growth")
    mc.announce_regime_change("RHYME_D: Range_Bound_Neutral")
    assert sent == []
