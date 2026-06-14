"""Send token-minimal bot manifest to Grok for full-system review.

Run:
  python scripts/mcp/grok_review_bot.py
  python scripts/mcp/grok_review_bot.py --ultra
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "data" / "bot_manifest.txt"


def main() -> int:
    ultra = "--ultra" in sys.argv
    export = ROOT / "scripts" / "mcp" / "export_bot_manifest.py"
    subprocess.check_call(
        [sys.executable, str(export)] + (["--ultra"] if ultra else []),
        cwd=ROOT,
    )
    manifest = MANIFEST.read_text(encoding="utf-8")
    prompt = (
        "You are reviewing a systematic trading fund (PythonTrading). "
        "Below is BOT_MANIFEST — a compact map, NOT full source. "
        "Give: (1) one-paragraph system summary, (2) live vs paper split, "
        "(3) main data/control flow, (4) top 5 risks or gaps, "
        "(5) 3 highest-value improvements. Be specific; cite manifest paths.\n\n"
        "--- BOT_MANIFEST ---\n"
        f"{manifest}\n"
        "--- END ---"
    )
    # Call grok.exe headless (loads GROK_API_KEY from .env)
    grok_exe = Path.home() / ".grok" / "bin" / "grok.exe"
    if grok_exe.is_file():
        from dotenv import load_dotenv
        import os

        load_dotenv(ROOT / ".env", override=False)
        if not os.environ.get("GROK_API_KEY") and os.environ.get("XAI_API_KEY"):
            os.environ["GROK_API_KEY"] = os.environ["XAI_API_KEY"]
        result = subprocess.run(
            [str(grok_exe), "-p", prompt, "--cwd", str(ROOT)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
            env=os.environ,
        )
        if result.returncode != 0:
            print(result.stderr or result.stdout, file=sys.stderr)
            return result.returncode or 1
        print(result.stdout)
        return 0

    print("grok.exe not found. Use MCP grok_query in Cursor with data/bot_manifest.txt", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
