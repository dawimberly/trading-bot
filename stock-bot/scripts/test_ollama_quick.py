"""Minimal Ollama JSON test."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.ollama_client import ollama_json, resolve_model_chain, format_ollama_status_line

print(format_ollama_status_line())
model = "deepseek-r1:8b"
print("model:", model)
result = ollama_json(
    'Return JSON: {"ok": true, "capability": "ping", "confidence": 0.9}',
    system="Reply with valid JSON only.",
    model=model,
    timeout_sec=120,
)
print(json.dumps(result, indent=2))
