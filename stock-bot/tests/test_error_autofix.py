"""Runtime auto-fix: Alpaca 500s retry soon; Ollama only for unknown errors."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.error_autofix import (
    AutofixPlan,
    consult_ollama,
    deterministic_plan,
    handle_cycle_error,
    propose,
)


def test_internal_server_error_retries_soon_without_ollama(monkeypatch):
    monkeypatch.setattr("modules.error_autofix.consult_ollama", lambda *a, **k: None)
    plan = propose('{"message":"Internal Server Error"}')
    assert plan.action == "retry_soon"
    assert plan.source == "rules"
    assert plan.error_class == "transient_api"
    assert plan.loop_sleep_sec(60) == 15.0


def test_dns_error_backs_off():
    plan = deterministic_plan("Failed to resolve paper-api.alpaca.markets getaddrinfo")
    assert plan.action == "backoff"
    assert plan.error_class == "transient_network"
    assert plan.loop_sleep_sec(60) == 60.0
    assert plan.loop_sleep_sec(10) == 30.0


def test_ollama_allowlist_only(monkeypatch):
    seed = deterministic_plan("weird boom")
    monkeypatch.setattr(
        "modules.error_autofix.consult_ollama",
        lambda error, seed: AutofixPlan(
            action="retry_soon",
            sleep_sec=8,
            source="ollama",
            reason="retry",
            error_class=seed.error_class,
        ),
    )
    plan = propose("weird boom")
    assert plan.source == "ollama"
    assert plan.action == "retry_soon"


def test_ollama_rejects_code_patch_action(monkeypatch):
    seed = AutofixPlan(
        action="skip_cycle",
        sleep_sec=60,
        source="rules",
        reason="unknown",
        error_class="other",
    )

    def fake_json(*args, **kwargs):
        return {"action": "edit_file", "path": "run_all.py", "reason": "patch it"}

    monkeypatch.setattr("modules.ollama_client.ollama_available", lambda: True)
    monkeypatch.setattr("modules.ollama_client.ollama_json", fake_json)
    monkeypatch.setattr(seed, "error_class", "other")
    out = consult_ollama("boom", seed)
    assert out is None


def test_handle_cycle_error_logs_action(tmp_path, monkeypatch):
    from modules import error_watcher

    monkeypatch.setattr("modules.error_autofix.enabled", lambda: True)
    monkeypatch.setattr("modules.error_autofix._maybe_telegram", lambda plan: None)
    monkeypatch.setattr(error_watcher, "_enabled", lambda: True)
    monkeypatch.setattr(error_watcher, "_ACTIONS_PATH", tmp_path / "bot_actions.jsonl")
    monkeypatch.setattr(error_watcher, "_ERRORS_PATH", tmp_path / "bot_errors.jsonl")
    monkeypatch.setattr(error_watcher, "_LOG_DIR", tmp_path)

    plan = handle_cycle_error('{"message":"Internal Server Error"}')
    assert plan.action == "retry_soon"
    text = (tmp_path / "bot_actions.jsonl").read_text(encoding="utf-8")
    assert "autofix" in text
    assert "retry_soon" in text
