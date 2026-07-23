"""Lightweight local Ollama client — chat + generate, model resolution, JSON output."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Any

import config

logger = logging.getLogger(__name__)

_RESPONSE_CACHE: dict[str, tuple[float, tuple[str, str]]] = {}
_CACHE_TTL_SEC = 1800.0
_CACHE_MAX = 8


def _host() -> str:
    return config.OLLAMA_HOST.rstrip("/")


def ollama_available() -> bool:
    try:
        req = urllib.request.Request(f"{_host()}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def ollama_version() -> str | None:
    try:
        req = urllib.request.Request(f"{_host()}/api/version", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        ver = str(body.get("version") or "").strip()
        return ver or None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None


def ollama_installed_models() -> set[str]:
    try:
        req = urllib.request.Request(f"{_host()}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return set()
    names: set[str] = set()
    for item in body.get("models") or []:
        name = str(item.get("name") or "").strip()
        if name:
            names.add(name)
    return names


def model_available(model: str, installed: set[str] | None = None) -> bool:
    installed = installed if installed is not None else ollama_installed_models()
    if not installed:
        return True
    if model in installed:
        return True
    if ":" not in model:
        return any(n.startswith(f"{model}:") for n in installed)
    base = model.split(":")[0]
    return any(n.startswith(f"{base}:") or n == base for n in installed)


def resolve_model(model: str, installed: set[str] | None = None) -> str | None:
    """Map a configured model name to an installed tag, or None if missing."""
    installed = installed if installed is not None else ollama_installed_models()
    if not installed:
        return model
    if model in installed:
        return model
    if ":" not in model:
        for name in sorted(installed):
            if name.startswith(f"{model}:"):
                return name
    base = model.split(":")[0]
    for name in sorted(installed):
        if name.startswith(f"{base}:"):
            return name
    return None


def resolve_model_chain(*, fast_only: bool = False) -> list[str]:
    """Primary + fallbacks, keeping only models that are installed (if known)."""
    fallbacks = [
        m.strip()
        for m in str(config.OLLAMA_FALLBACK_MODELS or "").split(",")
        if m.strip()
    ]
    if fast_only:
        candidates = fallbacks or [config.OLLAMA_MODEL]
    else:
        candidates = [config.OLLAMA_MODEL]
        for m in fallbacks:
            if m not in candidates:
                candidates.append(m)
    installed = ollama_installed_models()
    if not installed:
        return candidates
    resolved: list[str] = []
    for model in candidates:
        pick = resolve_model(model, installed)
        if pick and pick not in resolved:
            resolved.append(pick)
    return resolved or candidates


def _is_fast_model(model: str) -> bool:
    low = model.lower()
    return any(tag in low for tag in ("1.5b", "3b", "1b", "llama3.2"))


def _num_predict(model: str, *, json_mode: bool = False) -> int:
    if json_mode:
        return 600 if _is_fast_model(model) else 1200
    return 450 if _is_fast_model(model) else 900


def _cache_get(key: str) -> tuple[str, str] | None:
    row = _RESPONSE_CACHE.get(key)
    if row and time.monotonic() - row[0] < _CACHE_TTL_SEC:
        return row[1]
    return None


def _cache_put(key: str, value: tuple[str, str]) -> None:
    if len(_RESPONSE_CACHE) >= _CACHE_MAX:
        oldest = min(_RESPONSE_CACHE, key=lambda k: _RESPONSE_CACHE[k][0])
        del _RESPONSE_CACHE[oldest]
    _RESPONSE_CACHE[key] = (time.monotonic(), value)


def clear_ollama_cache() -> None:
    _RESPONSE_CACHE.clear()


def _max_attempts() -> int:
    """Cap at 3 so slow/unreachable Ollama fails over to heuristic quickly."""
    return max(1, min(3, int(getattr(config, "OLLAMA_RETRY_COUNT", 3))))


def _backoff_sec(attempt: int) -> float:
    """Exponential backoff: 1s, 2s, 4s (capped)."""
    return min(8.0, 1.0 * (2**attempt))


def _http_post(
    path: str,
    body: dict[str, Any],
    *,
    timeout: int,
    retries: int | None = None,
) -> dict[str, Any]:
    payload = json.dumps(body).encode("utf-8")
    model = str(body.get("model") or "?")
    prompt_len = 0
    if isinstance(body.get("prompt"), str):
        prompt_len = len(body["prompt"])
    elif isinstance(body.get("messages"), list):
        prompt_len = sum(len(str(m.get("content") or "")) for m in body["messages"])
    req = urllib.request.Request(
        f"{_host()}{path}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    attempts = _max_attempts() if retries is None else max(1, min(3, int(retries)))
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            last_exc = exc
            err_type = type(exc).__name__
            logger.warning(
                "Ollama POST %s failed attempt %s/%s model=%s prompt_len=%s "
                "timeout=%ss error_type=%s err=%s",
                path,
                attempt + 1,
                attempts,
                model,
                prompt_len,
                timeout,
                err_type,
                exc,
            )
            if attempt + 1 >= attempts:
                break
            time.sleep(_backoff_sec(attempt))
            # Rebuild request — urllib Request is single-use for some failures.
            req = urllib.request.Request(
                f"{_host()}{path}",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
    assert last_exc is not None
    raise last_exc


def _merge_thinking_response(body: dict[str, Any]) -> tuple[str, str]:
    """Return (full_text, parse_text) from Ollama response (incl. deepseek thinking)."""
    answer = str(body.get("response") or body.get("message", {}).get("content") or "").strip()
    thinking = str(body.get("thinking") or "").strip()
    if answer and thinking:
        full = f"{thinking}\n\n---\n{answer}"
        if re.search(r"NARRATIVE\s*:", answer, re.I):
            parse_text = answer
        elif re.search(r"NARRATIVE\s*:", thinking, re.I):
            parse_text = thinking
        else:
            parse_text = f"{thinking}\n{answer}"
    elif answer:
        full = answer
        parse_text = answer
    elif thinking:
        full = thinking
        parse_text = thinking
    else:
        raise RuntimeError("Ollama returned empty response")
    return full, parse_text


def ollama_complete(
    prompt: str,
    *,
    system: str | None = None,
    model: str | None = None,
    timeout_sec: int | None = None,
    json_mode: bool = False,
    temperature: float = 0.3,
    use_cache: bool = True,
    retries: int | None = None,
) -> tuple[str, str]:
    """Call Ollama and return (full_text, text_for_parsing). Prefers /api/chat."""
    model = model or config.OLLAMA_MODEL
    cache_payload = f"{model}\0{system or ''}\0{json_mode}\0{prompt}"
    cache_key = hashlib.sha256(cache_payload.encode()).hexdigest()[:24]
    if use_cache:
        cached = _cache_get(cache_key)
        if cached:
            return cached

    timeout = int(timeout_sec if timeout_sec is not None else config.OLLAMA_TIMEOUT_SEC)
    prompt_len = len(prompt or "") + len(system or "")
    options = {"temperature": temperature, "num_predict": _num_predict(model, json_mode=json_mode)}

    use_chat = bool(getattr(config, "OLLAMA_USE_CHAT_API", True))
    body: dict[str, Any] | None = None
    if use_chat:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        chat_body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": options,
        }
        if json_mode:
            chat_body["format"] = "json"
        try:
            body = _http_post("/api/chat", chat_body, timeout=timeout, retries=retries)
            body = {
                "response": (body.get("message") or {}).get("content", ""),
                "thinking": body.get("thinking", ""),
            }
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            OSError,
            json.JSONDecodeError,
        ) as exc:
            logger.warning(
                "Ollama chat API failed model=%s prompt_len=%s error_type=%s err=%s "
                "— falling back to /api/generate",
                model,
                prompt_len,
                type(exc).__name__,
                exc,
            )

    if body is None:
        gen_body: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": options,
        }
        if system:
            gen_body["system"] = system
        if json_mode:
            gen_body["format"] = "json"
        try:
            body = _http_post("/api/generate", gen_body, timeout=timeout, retries=retries)
        except Exception as exc:
            logger.error(
                "Ollama generate failed model=%s prompt_len=%s timeout=%ss "
                "error_type=%s err=%s",
                model,
                prompt_len,
                timeout,
                type(exc).__name__,
                exc,
            )
            raise

    result = _merge_thinking_response(body)
    if use_cache:
        _cache_put(cache_key, result)
    return result


def ollama_json(
    prompt: str,
    *,
    system: str | None = None,
    model: str | None = None,
    timeout_sec: int | None = None,
    retries: int | None = None,
) -> dict[str, Any]:
    """Structured JSON completion; returns a graceful fallback on service or parse failure."""
    sys = (system or "") + "\nReply with valid JSON only — no markdown fences."
    try:
        _full, text = ollama_complete(
            prompt,
            system=sys.strip() or None,
            model=model,
            timeout_sec=timeout_sec,
            json_mode=True,
            use_cache=False,
            retries=retries,
        )
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        logger.warning(
            "Ollama JSON completion failed model=%s error_type=%s err=%s",
            model or config.OLLAMA_MODEL,
            type(exc).__name__,
            exc,
        )
        return {
            "raw": "",
            "parse_error": True,
            "service_error": True,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }

    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                parsed = json.loads(m.group(0))
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                pass
    return {"raw": text[:500], "parse_error": True}


def format_ollama_status_line() -> str | None:
    """One-line Ollama status for startup banners."""
    if not config.PAPER_THINKING_ENGINE_ENABLED and not config.effective_thinking_engine_enabled():
        thinking = "OFF"
    else:
        thinking = "ON" if config.effective_thinking_engine_enabled() else "armed"
    if not ollama_available():
        return f">>> Ollama / Thinking: {thinking} | host unreachable ({config.OLLAMA_HOST}) <<<"
    installed = sorted(ollama_installed_models())
    chain = resolve_model_chain()
    active = chain[0] if chain else config.OLLAMA_MODEL
    ver = ollama_version() or "?"
    inst = ", ".join(installed[:3]) + ("..." if len(installed) > 3 else "")
    kimi = "Kimi ON" if config.effective_kimi_deep_thinker_enabled() else "Kimi OFF"
    return (
        f">>> Ollama v{ver} | Thinking: {thinking} | model {active} "
        f"(chain: {', '.join(chain[:3])}) | installed: {inst or 'none'} | {kimi} <<<"
    )
