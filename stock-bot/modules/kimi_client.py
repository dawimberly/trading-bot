"""Optional Kimi deep reasoning via OpenAI-compatible APIs (Moonshot or NVIDIA NIM).

Providers:
  - Moonshot direct: KIMI_API_BASE_URL=https://api.moonshot.ai/v1, model=kimi-latest
  - NVIDIA Integrate: KIMI_API_BASE_URL=https://integrate.api.nvidia.com/v1,
                      model=moonshotai/kimi-k2.6, key=nvapi-...

Cost/latency: cloud billed per token; typical daily call ~3-15s+ round-trip.
Do NOT invoke from the 5m trading loop — only thinking_engine daily refresh.
"""

from __future__ import annotations

import datetime
import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import config
from modules.safe_io import read_json_file, write_json_file

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]


def _state_path() -> Path:
    raw = getattr(config, "KIMI_STATE_FILE", "data/kimi_daily_state.json")
    p = Path(raw)
    if not p.is_absolute():
        p = ROOT / p
    return p


def kimi_configured() -> bool:
    return bool(config.KIMI_API_ENABLED and (config.KIMI_API_KEY or "").strip())


def should_run_kimi_daily() -> bool:
    """True when daily Kimi deep-think has not run yet today."""
    if not config.KIMI_DAILY_THINK or not kimi_configured():
        return False
    state = read_json_file(_state_path()) or {}
    today = datetime.date.today().isoformat()
    return str(state.get("last_date") or "") != today


def record_kimi_daily_run(*, regime: str = "", model: str = "", source: str = "kimi") -> None:
    now = datetime.datetime.now().isoformat()
    write_json_file(
        _state_path(),
        {
            "last_date": datetime.date.today().isoformat(),
            "last_run_at": now,
            "regime": regime,
            "model": model or config.KIMI_MODEL,
            "source": source,
        },
    )


def kimi_configured() -> bool:
    return bool(config.KIMI_API_ENABLED and (config.KIMI_API_KEY or "").strip())


def _chat_completions_url() -> str:
    base = str(config.KIMI_API_BASE_URL).rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _kimi_provider_label() -> str:
    base = str(config.KIMI_API_BASE_URL).lower()
    if "nvidia.com" in base:
        return "NVIDIA NIM"
    if "moonshot" in base:
        return "Moonshot"
    return "OpenAI-compatible"


def format_kimi_deep_thinker_banner() -> str | None:
    if config.effective_kimi_deep_thinker_enabled():
        daily = "daily" if config.KIMI_DAILY_THINK else "on-demand"
        provider = _kimi_provider_label()
        return (
            f">>> Kimi Deep Thinker: ENABLED "
            f"({provider}, model={config.KIMI_MODEL}, {daily}, hybrid w/ Ollama)"
        )
    if config.KIMI_API_ENABLED and not (config.KIMI_API_KEY or "").strip():
        return ">>> Kimi Deep Thinker: OFF (set KIMI_API_KEY or NVIDIA_API_KEY in .env)"
    return ">>> Kimi Deep Thinker: OFF"


def deep_think(prompt: str, *, system: str | None = None) -> str:
    """Single chat completion (Moonshot direct or NVIDIA integrate.api.nvidia.com)."""
    if not kimi_configured():
        raise RuntimeError("Kimi API disabled or KIMI_API_KEY missing")

    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    body: dict[str, Any] = {
        "model": config.KIMI_MODEL,
        "messages": messages,
        "temperature": float(config.KIMI_TEMPERATURE),
        "max_tokens": int(config.KIMI_MAX_TOKENS),
        "stream": False,
    }
    top_p_raw = getattr(config, "KIMI_TOP_P", None)
    if top_p_raw not in (None, ""):
        try:
            body["top_p"] = float(top_p_raw)
        except (TypeError, ValueError):
            pass

    payload = json.dumps(body).encode("utf-8")
    url = _chat_completions_url()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {config.KIMI_API_KEY.strip()}",
    }

    max_retries = max(1, int(getattr(config, "KIMI_MAX_RETRIES", 3)))
    timeout = float(getattr(config, "KIMI_TIMEOUT_SEC", 120))
    last_err: Exception | None = None

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError("Kimi API returned no choices")
            message = choices[0].get("message") or {}
            content = str(message.get("content") or "").strip()
            if not content:
                raise RuntimeError("Kimi API returned empty content")
            return content
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, RuntimeError) as exc:
            last_err = exc
            if attempt + 1 >= max_retries:
                break
            delay = 0.5 * (2**attempt)
            logger.warning(
                "Kimi deep_think attempt %d/%d failed: %s (retry in %.1fs)",
                attempt + 1,
                max_retries,
                exc,
                delay,
            )
            time.sleep(delay)

    raise RuntimeError(f"Kimi deep_think failed after {max_retries} attempts: {last_err}")
