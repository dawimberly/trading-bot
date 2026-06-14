"""FastMCP server for Grok CLI (patched for current headless JSON output)."""

from __future__ import annotations

import json
import sys
from typing import Optional

from mcp.server.fastmcp.server import Context, FastMCP

from .router import router
from .types import GrokMessage
from .utils import DEFAULT_TIMEOUT_S

server = FastMCP(
    name="grok-mcp",
    instructions=(
        "This MCP server exposes the Grok CLI via tools for general queries, chat-style prompts, "
        "and code-oriented tasks. Use grok_query for generic prompts, grok_chat for role-based "
        "messages, and grok_code when asking for code with optional language/context. "
        "Simple queries may route to local Ollama; strategy/code/trading routes to Grok. "
        "Requires GROK_API_KEY for Grok paths."
    ),
    debug=False,
    log_level="INFO",
)


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
    return await router.route(
        prompt,
        "grok_query",
        model=model,
        timeout_s=timeout_s,
        ctx=ctx,
        raw_output=raw_output,
    )


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

    return await router.route(
        "\n".join(prompt_lines),
        "grok_chat",
        model=model,
        timeout_s=timeout_s,
        ctx=ctx,
        raw_output=raw_output,
    )


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
    return await router.route(
        prompt,
        "grok_code",
        model=model,
        timeout_s=timeout_s,
        ctx=ctx,
        raw_output=raw_output,
    )


def main() -> None:
    try:
        server.run("stdio")
    except KeyboardInterrupt:
        sys.exit(130)
