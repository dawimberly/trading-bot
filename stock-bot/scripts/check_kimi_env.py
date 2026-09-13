"""Check Kimi/NVIDIA env configuration (masks secrets)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())


def mask(val: str) -> str:
    if not val:
        return "(not set)"
    if len(val) <= 12:
        return f"(set, len={len(val)})"
    return f"{val[:8]}...{val[-4:]}"


KEYS = [
    "KIMI_API_ENABLED",
    "KIMI_API_KEY",
    "NVIDIA_API_KEY",
    "KIMI_API_BASE_URL",
    "KIMI_MODEL",
    "KIMI_DAILY_THINK",
    "PAPER_THINKING_ENGINE_ENABLED",
    "PAPER_CHASE_MODE",
]

print(f".env path: {find_dotenv() or '(not found)'}")
print("--- raw os.environ (after load_dotenv) ---")
for k in KEYS:
    v = os.getenv(k, "")
    if "KEY" in k:
        print(f"  {k}={mask(v)}")
    else:
        print(f"  {k}={v or '(not set)'}")

import config
from modules.kimi_client import format_kimi_deep_thinker_banner, kimi_configured

print("--- config module ---")
print(f"  KIMI_API_KEY={mask(config.KIMI_API_KEY)}")
print(f"  KIMI_API_ENABLED={config.KIMI_API_ENABLED}")
print(f"  KIMI_API_BASE_URL={config.KIMI_API_BASE_URL}")
print(f"  KIMI_MODEL={config.KIMI_MODEL}")
print(f"  kimi_configured={kimi_configured()}")
print(f"  effective_kimi_deep_thinker={config.effective_kimi_deep_thinker_enabled()}")
print(f"  banner={format_kimi_deep_thinker_banner()}")
