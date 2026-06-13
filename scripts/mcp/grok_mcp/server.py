"""FastMCP server for Grok CLI (patched for current headless JSON output)."""

from __future__ import annotations

import json
import sys
from typing import Optional

from mcp.server.fastmcp.server import Context, FastMCP

from .types import GrokMessage
from .utils import DEFAULT_TIMEOUT_S, _run_grok, response_text

server = FastMCP(
    name="grok-mcp",
    instructions=(
        "This MCP server exposes the Grok CLI via tools for general queries, chat-style prompts, "
        "and code-oriented tasks. Use grok_query for generic prompts, grok_chat for role-based "
        "messages, and grok_code when asking for code with optional language/context. "
        "Simple queries run headless with JSON output and without repo tools. Requires GROK_API_KEY."
    ),
    debug=False,
    log_level="INFO",
)


def _tool_result(result, *, raw_output: bool) -> str | dict:
    text = response_text(result)
    if raw_output:
        return {
            "text": text,
            "messages": [message.model_dump() for message in result.messages],
            "raw": result.raw,
            "model": result.model,
        }
    return text


@server.tool(
    name="grok_query",
    title="Grok Query",
    description=(
        "Send a single prompt to Grok via CLI headless mode. Returns the assistant's text. "
        "Use raw_output=true to get raw CLI output and parsed messages."
    ),
)
async def grok_query(
    prompt: str,
    model: Optional[str] = None,
    raw_output: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    ctx: Optional[Context] = None,
) -> str | dict:
    result = await _run_grok(
        prompt,
        model=model,
        timeout_s=timeout_s,
        simple_mode=True,
        ctx=ctx,
    )
    return _tool_result(result, raw_output=raw_output)


@server.tool(
    name="grok_chat",
    title="Grok Chat",
    description=(
        "Send a list of role/content messages to Grok by flattening into a single prompt. "
        "Useful for multi-turn context when the CLI only supports a single '-p' prompt."
    ),
)
async def grok_chat(
    messages: list[GrokMessage],
    model: Optional[str] = None,
    raw_output: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    ctx: Optional[Context] = None,
) -> str | dict:
    prompt_lines: list[str] = []
    for message in messages:
        if isinstance(message.content, str):
            content_str = message.content
        else:
            try:
                content_str = json.dumps(message.content, ensure_ascii=False)
            except Exception:
                content_str = str(message.content)
        prompt_lines.append(f"{message.role.capitalize()}: {content_str}")

    result = await _run_grok(
        "\n".join(prompt_lines),
        model=model,
        timeout_s=timeout_s,
        simple_mode=True,
        ctx=ctx,
    )
    return _tool_result(result, raw_output=raw_output)


@server.tool(
    name="grok_code",
    title="Grok Code Task",
    description=(
        "Ask Grok for code or code-related guidance. You can provide a language hint and context "
        "(e.g., file snippets or requirements). Returns assistant text by default."
    ),
)
async def grok_code(
    task: str,
    language: Optional[str] = None,
    context: Optional[str] = None,
    model: Optional[str] = None,
    raw_output: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    ctx: Optional[Context] = None,
) -> str | dict:
    sys_instructions = [
        "You are an expert software engineer.",
        "Respond with clear, correct, directly usable code and concise explanations.",
        "Prefer minimal dependencies and explain tradeoffs when relevant.",
    ]
    if language:
        sys_instructions.append(f"Primary language: {language}")
    if context:
        sys_instructions.append("Context:\n" + context.strip())

    prompt = "\n\n".join(["\n".join(sys_instructions), "Task:", task.strip()])
    result = await _run_grok(
        prompt,
        model=model,
        timeout_s=timeout_s,
        simple_mode=False,
        ctx=ctx,
    )
    return _tool_result(result, raw_output=raw_output)


def main() -> None:
    try:
        server.run("stdio")
    except KeyboardInterrupt:
        sys.exit(130)
