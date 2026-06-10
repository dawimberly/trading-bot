"""Launch grok-cli-mcp with secrets loaded from the project .env file."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _load_project_env() -> Path:
    root = Path(__file__).resolve().parents[2]
    env_file = root / ".env"
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        print("python-dotenv is required to load .env", file=sys.stderr)
        raise SystemExit(1) from exc

    if env_file.is_file():
        load_dotenv(env_file, override=False)
    return root


def _ensure_grok_api_key() -> None:
    if not os.environ.get("GROK_API_KEY") and os.environ.get("XAI_API_KEY"):
        os.environ["GROK_API_KEY"] = os.environ["XAI_API_KEY"]

    if not os.environ.get("GROK_API_KEY"):
        print(
            "Set GROK_API_KEY or XAI_API_KEY in the project .env file.",
            file=sys.stderr,
        )
        raise SystemExit(1)


def main() -> int:
    _load_project_env()
    _ensure_grok_api_key()
    return subprocess.call([sys.executable, "-m", "grok_cli_mcp"], env=os.environ)


if __name__ == "__main__":
    raise SystemExit(main())
