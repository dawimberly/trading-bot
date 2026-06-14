"""Ultra-intelligent Grok delegation router for Cursor MCP."""

from __future__ import annotations

import os
from datetime import date
from typing import Any, Literal, Optional

from mcp.server.fastmcp.server import Context

from .types import GrokParsedOutput
from .utils import DEFAULT_TIMEOUT_S, _run_grok, response_text

try:
    import httpx

    OLLAMA_AVAILABLE = True
except ImportError:
    OLLAMA_AVAILABLE = False

QueryType = Literal["simple", "code", "reasoning", "trading", "creative"]
Provider = Literal["grok", "ollama"]

_GROK_TYPES: frozenset[QueryType] = frozenset({"reasoning", "code", "trading", "creative"})
_DEFAULT_OLLAMA_HOST = "http://localhost:11434"
_DEFAULT_OLLAMA_MODEL = "deepseek-r1:8b"
_GROK_TOKEN_ESTIMATE = 800


class QueryTypeNames:
    SIMPLE = "simple"
    CODE = "code"
    REASONING = "reasoning"
    TRADING = "trading"
    CREATIVE = "creative"


def _ollama_timeout_s() -> float:
    return float(os.environ.get("OLLAMA_ROUTE_TIMEOUT_S", "25"))


def _ollama_first_for_simple() -> bool:
    return os.environ.get("GROK_ROUTER_OLLAMA_FIRST", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )


class GrokRouter:
    """Route prompts to Grok (SuperGrok) or local Ollama by query value and type."""

    def __init__(self) -> None:
        self.daily_usage: dict[str, int] = {}
        self.project_personas: dict[str, str] = {
            "PythonTrading": (
                "You are a conservative, safety-first quantitative trading expert. "
                "Focus on risk-adjusted returns, capital preservation, and rigorous backtesting."
            ),
        }

    def classify_query(self, prompt: str, tool_name: str = "grok_query") -> QueryType:
        """Quick keyword classifier — scans first 1500 chars only."""
        p = prompt.lower()[:1500]
        if tool_name == "grok_code" or any(
            word in p
            for word in (
                "code",
                "refactor",
                "function",
                "class",
                "bug",
                "fix",
                "implement",
            )
        ):
            return QueryTypeNames.CODE
        if any(
            word in p
            for word in (
                "trade",
                "position",
                "alpaca",
                "vti",
                "risk",
                "tilt",
                "breaker",
                "paper bot",
                "live bot",
                "sharpe",
                "backtest",
            )
        ):
            return QueryTypeNames.TRADING
        if len(prompt) > 700 or any(
            word in p
            for word in (
                "strategy",
                "architecture",
                "recommend",
                "compare",
                "should we",
                "analysis",
                "optimize",
                "design",
            )
        ):
            return QueryTypeNames.REASONING
        if any(word in p for word in ("creative", "brainstorm", "ideas for")):
            return QueryTypeNames.CREATIVE
        return QueryTypeNames.SIMPLE

    def detect_project(self) -> str:
        cwd = os.getcwd().lower()
        if "pythontrading" in cwd:
            return "PythonTrading"
        return "general"

    def _update_usage(self, tokens_est: int) -> None:
        today = date.today().isoformat()
        self.daily_usage[today] = self.daily_usage.get(today, 0) + tokens_est

    def _ollama_host(self) -> str:
        return os.environ.get("OLLAMA_HOST", _DEFAULT_OLLAMA_HOST).rstrip("/")

    def _ollama_model(self) -> str:
        return os.environ.get("OLLAMA_MODEL", _DEFAULT_OLLAMA_MODEL)

    def _with_persona(self, prompt: str, project: str) -> str:
        persona = self.project_personas.get(project)
        if not persona:
            return prompt
        return f"Project: {project}\nPersona: {persona}\n\n{prompt}"

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

        if tool_name == "grok_code":
            qtype = QueryTypeNames.CODE
            simple_mode = False
        elif simple_mode is None:
            simple_mode = qtype == QueryTypeNames.SIMPLE

        use_grok = force_grok or qtype in _GROK_TYPES
        if qtype == QueryTypeNames.SIMPLE and not _ollama_first_for_simple():
            use_grok = True

        if use_grok:
            enriched = (
                self._with_persona(prompt, project)
                if qtype in _GROK_TYPES
                else prompt
            )
            if ctx:
                await ctx.info(f"Routing to Grok SuperGrok ({qtype}, simple={simple_mode})...")
            result = await self._call_grok(
                enriched,
                tool_name,
                model=model,
                timeout_s=timeout_s,
                simple_mode=simple_mode,
                ctx=ctx,
            )
            if qtype in _GROK_TYPES:
                self._update_usage(_GROK_TOKEN_ESTIMATE)
            return route_result_payload(
                result,
                provider="grok",
                qtype=qtype,
                raw_output=raw_output,
                daily_usage=self.daily_usage.get(date.today().isoformat(), 0),
            )

        if OLLAMA_AVAILABLE:
            try:
                if ctx:
                    await ctx.info(f"Routing to Ollama ({qtype})...")
                text = await self._call_ollama(prompt)
                result = GrokParsedOutput(text=text, model=self._ollama_model())
                return route_result_payload(
                    result,
                    provider="ollama",
                    qtype=qtype,
                    raw_output=raw_output,
                )
            except Exception as exc:
                if ctx:
                    await ctx.warning(f"Ollama failed ({exc}); falling back to Grok simple mode.")

        if ctx:
            await ctx.info("Fallback: Grok simple mode...")
        result = await self._call_grok(
            prompt,
            tool_name,
            model=model,
            timeout_s=timeout_s,
            simple_mode=True,
            ctx=ctx,
        )
        return route_result_payload(
            result,
            provider="grok",
            qtype=qtype,
            raw_output=raw_output,
        )

    async def _call_grok(
        self,
        prompt: str,
        tool_name: str,
        *,
        model: Optional[str] = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        simple_mode: bool = True,
        ctx: Optional[Context] = None,
    ) -> GrokParsedOutput:
        del tool_name  # reserved for future per-tool CLI flags
        return await _run_grok(
            prompt,
            model=model,
            timeout_s=timeout_s,
            simple_mode=simple_mode,
            ctx=ctx,
        )

    async def _call_ollama(self, prompt: str, model: Optional[str] = None) -> str:
        if not OLLAMA_AVAILABLE:
            raise RuntimeError("httpx not installed; Ollama routing unavailable")
        model = model or self._ollama_model()
        url = f"{self._ollama_host()}/api/chat"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": 0.7},
        }
        timeout = _ollama_timeout_s()
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
        content = (data.get("message") or {}).get("content")
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
    daily_usage: int = 0,
) -> str | dict[str, Any]:
    text = response_text(result)
    if not raw_output:
        return text
    payload: dict[str, Any] = {
        "text": text,
        "messages": [message.model_dump() for message in result.messages],
        "raw": result.raw,
        "model": result.model,
        "provider": provider,
        "query_type": qtype,
    }
    if daily_usage:
        payload["grok_tokens_est_today"] = daily_usage
    return payload
