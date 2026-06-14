"""Print VTI-beat tuned thinking engine demo samples (no Ollama required)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.thinking_engine import build_demo_reasoning_samples, get_pm_system_prompt


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    print("=== SYSTEM PROMPT ===\n")
    print(get_pm_system_prompt())
    print("\n=== DEMO REASONING SAMPLES ===\n")
    for i, s in enumerate(build_demo_reasoning_samples(), 1):
        print(f"--- Sample {i}: {s.get('label')} ---")
        print(f"NARRATIVE: {s.get('narrative')}")
        print(f"ASYMMETRY: {s.get('asymmetry')}")
        print(f"SECTOR_VIEW: {s.get('sector_view')}")
        print(f"RECOMMENDED_TILT: {s.get('suggested_tilt')}")
        print(f"TILT_RATIONALE: {s.get('tilt_rationale')}")
        print(f"CONFIDENCE: {s.get('confidence')}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
