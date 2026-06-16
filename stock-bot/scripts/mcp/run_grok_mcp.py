"""Launch grok-cli-mcp with secrets loaded from the project .env file."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _load_project_env() -> Path:
    root = Path(__file__).resolve().parents[2]
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        print("python-dotenv is required to load .env", file=sys.stderr)
        raise SystemExit(1) from exc

    for env_file in (root / ".env", root.parent / ".env"):
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


def _absolute_grok_cli() -> None:
    root = Path(__file__).resolve().parents[2]
    default = root / "scripts" / "mcp" / "grok_cli.cmd"
    raw = os.environ.get("GROK_CLI_PATH", "")
    path = Path(raw) if raw else default
    if not path.is_absolute():
        path = (root / path).resolve()
    os.environ["GROK_CLI_PATH"] = str(path)
    grok_exe = Path(os.environ.get("USERPROFILE", "")) / ".grok" / "bin" / "grok.exe"
    if not grok_exe.is_file() and not path.is_file():
        print(
            f"Grok CLI not found. Install: irm https://x.ai/cli/install.ps1 | iex\n"
            f"Expected: {grok_exe}",
            file=sys.stderr,
        )
        raise SystemExit(1)


def main() -> int:
    root = _load_project_env()
    _ensure_grok_api_key()
    _absolute_grok_cli()
    os.chdir(root)
    mcp_dir = Path(__file__).resolve().parent
    env = os.environ.copy()
    env["PYTHONPATH"] = str(mcp_dir) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.call([sys.executable, "-m", "grok_mcp"], env=env, cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
