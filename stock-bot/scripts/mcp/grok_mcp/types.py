"""Pydantic models for the patched Grok MCP server."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class GrokMessage(BaseModel):
    """A message in a Grok conversation."""

    role: str
    content: Any


class GrokParsedOutput(BaseModel):
    """Parsed output from Grok CLI."""

    messages: list[GrokMessage] = Field(default_factory=list)
    model: Optional[str] = None
    raw: Optional[str] = None
    text: Optional[str] = None
