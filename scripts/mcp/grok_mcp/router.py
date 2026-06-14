"""Route MCP prompts to Grok CLI or local Ollama based on query type."""

from __future__ import annotations

import os
from typing import Any, Literal, Optional

import httpx
from mcp.server.fastmcp.server import Context

from .types import GrokParsedOutput
from .utils import DEFAULT_TIMEOUT_S, _run_grok, response_text

QueryType = Literal["simple", "code", "reasoning", "trading", "creative"]
Provider = Literal["grok", "ollama"]

_GROK_TYPES: frozenset[QueryType] = frozenset({"reasoning", "code", "trading"})
_DEFAULT_OLLAMA_HOST = "http://localhost:11434"
_DEFAULT_OLLAMA_MODEL = "deepseek-r1:8b"


def _ollama_timeout_s() -> float:
    return float(os.environ.get("OLLAMA_ROUTE_TIMEOUT_S", "25"))


def _ollama_first_for_simple() -> bool:
    return os.environ.get("GROK_ROUTER_OLLAMA_FIRST", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )


class GrokRouter:
    def __init__(self) -> None:
        self.project_personas: dict[str, str] = {
            "PythonTrading": (
                "conservative trading bot expert, safety-first, quantitative, "
                "Alpaca live/paper, VTI core, thinking engine guarded on live"
            ),
        }

    def classify_query(self, prompt: str, tool_name: str) -> QueryType:
        """Lightweight keyword classifier for provider routing."""
        p = prompt.lower()
        if tool_name == "grok_code" or any(
            k in p for k in ("code", "function", "class", "refactor", "bug", "implement")
        ):
            return "code"
        if any(
            k in p
            for k in (
                "trade",
                "position",
                "vti",
                "alpaca",
                "risk",
                "tilt",
                "breaker",
                "sharpe",
                "backtest",
            )
        ):
            return "trading"
        if len(prompt) > 800 or any(
            k in p
            for k in (
                "strategy",
                "architecture",
                "compare",
                "should we",
                "recommend",
                "optimize",
                "design",
            )
        ):
            return "reasoning"
        if any(k in p for k in ("creative", "brainstorm", "ideas for")):
            return "creative"
        return "simple"

    def detect_project(self) -> str:
        cwd = os.getcwd()
        if "PythonTrading" in cwd:
            return "PythonTrading"
        return "general"

    def _ollama_host(self) -> str:
        return os.environ.get("OLLAMA_HOST", _DEFAULT_OLLAMA_HOST).rstrip("/")

    def _ollama_model(self) -> str:
        return os.environ.get("OLLAMA_MODEL", _DEFAULT_OLLAMA_MODEL)

    def _with_persona(self, prompt: str, project: str) -> str:
        persona = self.project_personas.get(project)
        if not persona:
            return prompt
        return f"Project context ({project}): {persona}\n\n{prompt}"

    async def route(
        self,
        prompt: str,
        tool_name: str = "grok_query",
        *,
        model: Optional[str] = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        simple_mode: Optional[bool] = None,
        ctx: Optional[Context] = None,
        force_grok: bool = False,
        raw_output: bool = False,
    ) -> str | dict[str, Any]:
        if tool_name == "grok_code":
            force_grok = True

        qtype = self.classify_query(prompt, tool_name)
        project = self.detect_project()
        enriched = self._with_persona(prompt, project)

        if tool_name == "grok_code":
            qtype = "code"
            simple_mode = False
        elif simple_mode is None:
            simple_mode = qtype == "simple"

        use_grok = force_grok or qtype in _GROK_TYPES or qtype == "creative"
        if qtype == "simple" and not _ollama_first_for_simple():
            use_grok = True

        if use_grok:
            if ctx:
                await ctx.info(f"Routing to Grok ({qtype}, simple={simple_mode})...")
            result = await _run_grok(
                enriched,
                model=model,
                timeout_s=timeout_s,
                simple_mode=simple_mode,
                ctx=ctx,
            )
            return route_result_payload(
                result, provider="grok", qtype=qtype, raw_output=raw_output
            )

        try:
            if ctx:
                await ctx.info(f"Routing to Ollama ({qtype})...")
            text = await self._call_ollama(enriched)
            result = GrokParsedOutput(text=text, model=self._ollama_model())
            return route_result_payload(
                result, provider="ollama", qtype=qtype, raw_output=raw_output
            )
        except Exception as exc:
            if ctx:
                await ctx.warning(f"Ollama failed ({exc}); falling back to Grok simple mode.")
            result = await _run_grok(
                enriched,
                model=model,
                timeout_s=timeout_s,
                simple_mode=True,
                ctx=ctx,
            )
            return route_result_payload(
                result, provider="grok", qtype=qtype, raw_output=raw_output
            )

    async def _call_ollama(self, prompt: str, model: Optional[str] = None) -> str:
        model = model or self._ollama_model()
        url = f"{self._ollama_host()}/api/chat"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=_ollama_timeout_s()) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        message = data.get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Ollama returned empty content")
        return content.strip()


router = GrokRouter()


def route_result_payload(
    result: GrokParsedOutput,
    *,
    provider: Provider,
    qtype: QueryType,
    raw_output: bool,
) -> str | dict[str, Any]:
    text = response_text(result)
    if not raw_output:
        return text
    return {
        "text": text,
        "messages": [message.model_dump() for message in result.messages],
        "raw": result.raw,
        "model": result.model,
        "provider": provider,
        "query_type": qtype,
    }
