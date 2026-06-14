"""Smoke-test patched Grok MCP (_run_grok) without starting the MCP server."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MCP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MCP_DIR))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=False)
if os.getenv("XAI_API_KEY") and not os.getenv("GROK_API_KEY"):
    os.environ["GROK_API_KEY"] = os.environ["XAI_API_KEY"]
os.environ.setdefault(
    "GROK_CLI_PATH",
    str(MCP_DIR / "grok_cli.cmd"),
)

from grok_mcp.utils import _run_grok, response_text  # noqa: E402


async def main() -> int:
    prompt = (
        "Confirm you are SuperGrok by xAI. "
        "What is today's date? Answer in 1-2 sentences."
    )
    result = await _run_grok(prompt, simple_mode=True, timeout_s=300.0)
    text = response_text(result)
    print(text)
    if not text or len(text) < 10:
        print("FAIL: empty or too-short response", file=sys.stderr)
        return 1
    lowered = text.lower()
    if "supergrok" not in lowered and "grok" not in lowered:
        print("WARN: response may not confirm Grok identity", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
