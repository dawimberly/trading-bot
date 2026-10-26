"""Tests for Kimi deep thinker client."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import config
from modules.kimi_client import (
    deep_think,
    format_kimi_deep_thinker_banner,
    should_run_kimi_daily,
)


def test_should_run_kimi_daily_when_not_run_today(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "KIMI_DAILY_THINK", True)
    monkeypatch.setattr(config, "KIMI_API_ENABLED", True)
    monkeypatch.setattr(config, "KIMI_API_KEY", "test-key")
    state_file = tmp_path / "kimi_state.json"
    monkeypatch.setattr(config, "KIMI_STATE_FILE", str(state_file))
    assert should_run_kimi_daily() is True


def test_deep_think_parses_openai_response(monkeypatch):
    monkeypatch.setattr(config, "KIMI_API_ENABLED", True)
    monkeypatch.setattr(config, "KIMI_API_KEY", "sk-test")
    monkeypatch.setattr(config, "KIMI_MODEL", "kimi-latest")
    monkeypatch.setattr(config, "KIMI_TEMPERATURE", 0.3)
    monkeypatch.setattr(config, "KIMI_MAX_TOKENS", 512)
    monkeypatch.setattr(config, "KIMI_API_BASE_URL", "https://api.moonshot.ai/v1")
    monkeypatch.setattr(config, "KIMI_TIMEOUT_SEC", 30)
    monkeypatch.setattr(config, "KIMI_MAX_RETRIES", 1)

    payload = {
        "choices": [{"message": {"content": "Risk-on: favor cyclicals over defensives."}}]
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(payload).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        out = deep_think("Summarize market regime in one sentence.")
    assert "Risk-on" in out


def test_nvidia_chat_completions_url(monkeypatch):
    monkeypatch.setattr(
        config,
        "KIMI_API_BASE_URL",
        "https://integrate.api.nvidia.com/v1",
    )
    from modules.kimi_client import _chat_completions_url

    assert _chat_completions_url() == "https://integrate.api.nvidia.com/v1/chat/completions"


def test_banner_off_by_default():
    was = config.KIMI_API_ENABLED
    config.KIMI_API_ENABLED = False
    try:
        line = format_kimi_deep_thinker_banner()
        assert line is not None
        assert "OFF" in line
    finally:
        config.KIMI_API_ENABLED = was
