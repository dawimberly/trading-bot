from __future__ import annotations

import urllib.error

from modules import ollama_client


def test_ollama_json_returns_service_error_fallback(monkeypatch):
    def raise_error(*args, **kwargs):
        raise urllib.error.HTTPError("http://localhost:11434", 500, "Internal Server Error", {}, None)

    monkeypatch.setattr(ollama_client, "ollama_complete", raise_error)

    result = ollama_client.ollama_json("ping")

    assert result["parse_error"] is True
    assert result["service_error"] is True
    assert result["error_type"] == "HTTPError"
