#!/usr/bin/env python3
"""Install Ollama, pull a reasoning model, and smoke-test the thinking engine.

Usage:
  python scripts/setup_ollama.py
  python scripts/setup_ollama.py --model llama3.1:8b
  python scripts/setup_ollama.py --skip-install --test-only
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from modules.thinking_engine import (  # noqa: E402
    build_market_summary,
    get_market_reasoning,
    ollama_available,
)

DEFAULT_MODELS = (
    "deepseek-r1:8b",
    "llama3.1:8b",
    "deepseek-r1:1.5b",
    "llama3.1:70b",
)


def _run(cmd: list[str], *, check: bool = True, timeout: int | None = None) -> subprocess.CompletedProcess:
    print(f"> {' '.join(cmd)}")
    return subprocess.run(
        cmd,
        check=check,
        timeout=timeout,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )


def _ollama_bin() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    if local.is_file():
        return str(local)
    return None


def install_ollama() -> str:
    exe = _ollama_bin()
    if exe:
        print(f"[OK] Ollama found: {exe}")
        return exe

    print("Ollama not found — attempting install via winget...")
    try:
        _run(
            [
                "winget",
                "install",
                "-e",
                "--id",
                "Ollama.Ollama",
                "--accept-package-agreements",
                "--accept-source-agreements",
            ],
            timeout=600,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"[WARN] winget install failed: {exc}")
        print("Install manually from https://ollama.com/download then re-run this script.")
        raise SystemExit(1) from exc

    time.sleep(5)
    exe = _ollama_bin()
    if not exe:
        print("[FAIL] Ollama still not on PATH after install. Restart terminal and re-run.")
        raise SystemExit(1)
    return exe


def wait_for_server(timeout_sec: int = 90) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if ollama_available():
            return True
        time.sleep(2)
    return False


def start_ollama_service(exe: str) -> None:
    if ollama_available():
        print("[OK] Ollama API already running")
        return
    print("Starting Ollama service...")
    try:
        subprocess.Popen(
            [exe, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        print(f"[WARN] could not start ollama serve: {exc}")
    if not wait_for_server():
        print("[FAIL] Ollama API did not respond on http://localhost:11434")
        raise SystemExit(1)
    print("[OK] Ollama API is up")


def pull_model(exe: str, model: str) -> None:
    print(f"Pulling model {model} (may take several minutes)...")
    proc = subprocess.run(
        [exe, "pull", model],
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        print(proc.stderr or proc.stdout)
        raise SystemExit(f"Failed to pull model {model}")
    print(f"[OK] Model ready: {model}")


def choose_model(exe: str, preferred: str | None) -> str:
    if preferred:
        return preferred
    listed = subprocess.run(
        [exe, "list"],
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    have = listed.stdout.lower() if listed.returncode == 0 else ""
    for candidate in DEFAULT_MODELS:
        if candidate.split(":")[0] in have:
            print(f"[OK] Using installed model: {candidate}")
            return candidate
    return DEFAULT_MODELS[0]


def smoke_test(model: str) -> None:
    os.environ["OLLAMA_MODEL"] = model
    config.OLLAMA_MODEL = model
    summary = build_market_summary(
        data=__import__("pandas").DataFrame({"SPY": [500, 502, 505, 508, 510]}),
        regime="RHYME_D: Range_Bound_Neutral",
        vol="Low",
        wisdom={"web_sentiment": 0.05, "price_sentiment": 0.02},
        top_headline="Markets steady ahead of Fed speakers",
    )
    print("\n--- Smoke test prompt context ---")
    print(json.dumps(summary, indent=2))
    print("\n--- Calling local LLM (this may take 30-120s) ---")
    result = get_market_reasoning(summary)
    print("\n--- Reasoning (first 600 chars) ---")
    print(result["reasoning"][:600])
    print("\n--- Suggested tilt ---")
    print(json.dumps(result["suggested_tilt"], indent=2))
    print(f"Confidence: {result['confidence']}")
    print("\n[OK] Thinking engine smoke test passed")
    print(
        "\nEnable in paper bot: set PAPER_THINKING_ENGINE_ENABLED=true in .env "
        f"(model={model})"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Install Ollama and test thinking engine")
    parser.add_argument("--model", help="Model tag to pull/use (default: auto)")
    parser.add_argument("--skip-install", action="store_true", help="Skip Ollama install step")
    parser.add_argument("--skip-pull", action="store_true", help="Skip model pull")
    parser.add_argument("--test-only", action="store_true", help="Only run smoke test")
    args = parser.parse_args()

    if args.test_only:
        if not ollama_available():
            print("[FAIL] Ollama not reachable. Start Ollama app or run without --test-only.")
            raise SystemExit(1)
        model = args.model or config.OLLAMA_MODEL
        smoke_test(model)
        return

    exe = _ollama_bin() or (None if args.skip_install else install_ollama())
    if not exe:
        exe = install_ollama()
    start_ollama_service(exe)
    model = choose_model(exe, args.model)
    if not args.skip_pull:
        try:
            pull_model(exe, model)
        except SystemExit:
            if model != DEFAULT_MODELS[1]:
                print(f"Retrying with fallback model {DEFAULT_MODELS[1]}...")
                model = DEFAULT_MODELS[1]
                pull_model(exe, model)
            else:
                raise
    smoke_test(model)


if __name__ == "__main__":
    main()
