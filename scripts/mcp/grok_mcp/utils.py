"""Utility functions for the patched Grok MCP server."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Optional, Sequence

from mcp.server.fastmcp.server import Context

from .types import GrokMessage, GrokParsedOutput

DEFAULT_GROK_BIN = "/opt/homebrew/bin/grok"
ENV_GROK_CLI_PATH = "GROK_CLI_PATH"
ENV_GROK_API_KEY = "GROK_API_KEY"
DEFAULT_TIMEOUT_S = 300.0

# Headless tools that slow MCP calls or fail in Cursor subprocesses.
_SIMPLE_DISALLOWED_TOOLS = (
    "run_terminal_cmd,read_file,grep,list_dir,web_search,web_fetch,"
    "search_replace,write_file,Agent"
)


def _resolve_grok_path() -> str:
    """Prefer grok.exe over .cmd wrappers so subprocess stdout capture is reliable on Windows."""
    home = Path(os.environ.get("USERPROFILE", ""))
    grok_exe = home / ".grok" / "bin" / "grok.exe"
    if grok_exe.is_file():
        return str(grok_exe)

    candidates: list[Optional[str]] = [
        os.environ.get(ENV_GROK_CLI_PATH),
        DEFAULT_GROK_BIN,
        shutil.which("grok"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.suffix.lower() == ".cmd" and grok_exe.is_file():
            return str(grok_exe)
        if path.is_file():
            return str(path)
    return candidates[-1] or "grok"


def _require_api_key() -> str:
    api_key = os.environ.get(ENV_GROK_API_KEY)
    if not api_key:
        raise ValueError(
            f"{ENV_GROK_API_KEY} is not set. Please export your Grok API key in the environment."
        )
    return api_key


def _simple_cwd() -> str:
    """Use a neutral cwd so simple prompts skip repo context upload."""
    path = os.path.join(tempfile.gettempdir(), "grok-mcp-simple")
    os.makedirs(path, exist_ok=True)
    return path


def _extract_json_from_text(text: str) -> Any:
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass

    last_obj = text.rfind("}")
    last_arr = text.rfind("]")
    last = max(last_obj, last_arr)
    if last == -1:
        raise ValueError("No JSON-like content found in CLI output.")

    first_obj = text.find("{")
    first_arr = text.find("[")
    first_candidates = [i for i in [first_obj, first_arr] if i != -1]
    if not first_candidates:
        raise ValueError("No JSON-like content found in CLI output.")
    first = min(first_candidates)
    return json.loads(text[first : last + 1].strip())


def _collect_assistant_text(messages: Sequence[GrokMessage]) -> str:
    chunks: list[str] = []
    for message in messages:
        if message.role != "assistant":
            continue
        content = message.content
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" and "text" in block:
                    chunks.append(str(block["text"]))
                elif isinstance(block, dict) and "content" in block:
                    chunks.append(str(block["content"]))
        elif isinstance(content, dict) and "text" in content:
            chunks.append(str(content["text"]))
        else:
            try:
                chunks.append(json.dumps(content, ensure_ascii=False))
            except Exception:
                chunks.append(str(content))
    return "\n".join(part for part in (segment.strip() for segment in chunks) if part)


def response_text(result: GrokParsedOutput) -> str:
    """Prefer headless JSON `text`, then chat messages, then raw stdout."""
    if result.text and result.text.strip():
        return result.text.strip()
    if result.messages:
        collected = _collect_assistant_text(result.messages)
        if collected.strip():
            return collected.strip()
    return (result.raw or "").strip()


def _parse_grok_stdout(stdout: str, *, model: Optional[str]) -> GrokParsedOutput:
    parsed = _extract_json_from_text(stdout)

    if isinstance(parsed, dict) and parsed.get("type") == "error":
        message = parsed.get("message") or parsed.get("text") or "Grok CLI error"
        raise RuntimeError(str(message))

    if isinstance(parsed, dict) and isinstance(parsed.get("text"), str):
        return GrokParsedOutput(messages=[], model=model, raw=stdout, text=parsed["text"])

    messages: list[GrokMessage] = []
    if isinstance(parsed, dict) and "role" in parsed and "content" in parsed:
        messages = [GrokMessage(**parsed)]
    elif isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict) and "role" in item and "content" in item:
                messages.append(GrokMessage(**item))
    elif isinstance(parsed, dict) and "messages" in parsed:
        for item in parsed.get("messages", []) or []:
            if isinstance(item, dict) and "role" in item and "content" in item:
                messages.append(GrokMessage(**item))
    else:
        return GrokParsedOutput(messages=[], model=model, raw=stdout)

    return GrokParsedOutput(messages=messages, model=model, raw=stdout)


def _build_grok_args(
    grok_bin: str,
    prompt: str,
    *,
    model: Optional[str],
    simple_mode: bool,
) -> list[str]:
    if simple_mode:
        today = date.today().strftime("%A, %B %d, %Y")
        prompt = f"Reference date (local): {today}.\n\n{prompt}"
    args = [grok_bin, "-p", prompt, "--output-format", "json"]
    if model:
        args += ["-m", model]
    if simple_mode:
        args += [
            "--max-turns",
            "1",
            "--yolo",
            "--disallowed-tools",
            _SIMPLE_DISALLOWED_TOOLS,
            "--cwd",
            _simple_cwd(),
        ]
    else:
        args += ["--yolo"]
    return args


async def _run_grok(
    prompt: str,
    *,
    model: Optional[str] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    simple_mode: bool = True,
    ctx: Optional[Context] = None,
) -> GrokParsedOutput:
    grok_bin = _resolve_grok_path()
    if not shutil.which(grok_bin) and not os.path.exists(grok_bin):
        raise FileNotFoundError(
            f"Grok CLI not found. Checked {grok_bin} and PATH. "
            f"Set {ENV_GROK_CLI_PATH} or install grok CLI."
        )

    _require_api_key()
    args = _build_grok_args(grok_bin, prompt, model=model, simple_mode=simple_mode)

    env = os.environ.copy()
    env[ENV_GROK_API_KEY] = env[ENV_GROK_API_KEY]

    if ctx:
        mode = "simple" if simple_mode else "full"
        await ctx.info(f"Invoking Grok CLI ({mode}) {'with model ' + model if model else ''}...")

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=_simple_cwd() if simple_mode else None,
    )

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise TimeoutError(f"Grok CLI timed out after {timeout_s:.0f}s")

    stdout = (stdout_b or b"").decode("utf-8", errors="replace")
    stderr = (stderr_b or b"").decode("utf-8", errors="replace")

    if proc.returncode != 0:
        detail = stderr.strip() or stdout.strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"Grok CLI failed (exit {proc.returncode}): {detail}")

    combined = stdout if stdout.strip() else stderr
    if not combined.strip():
        combined = stdout + stderr

    try:
        return _parse_grok_stdout(combined, model=model)
    except ValueError as exc:
        if ctx:
            await ctx.warning(f"Failed to parse Grok JSON output: {exc}. Returning raw output.")
        return GrokParsedOutput(messages=[], model=model, raw=combined or stdout)
