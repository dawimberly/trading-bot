"""Generate sleeve-attribution markdown from current MC JSON (partial OK)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JSON_PATH = ROOT / "scripts" / "analysis" / "monte_carlo_v154_sleeve_attribution_365.json"
RUNNER = ROOT / "scripts" / "analysis" / "_run_mc_sleeve_attribution_365.py"


def main() -> int:
    spec = importlib.util.spec_from_file_location("sleeve_attr_mod", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    payload = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    n = len(payload.get("runs") or [])
    analysis = mod.write_report(payload)
    print(f"sample_runs={n} partial={payload.get('partial')}")
    print(f"Wrote {mod.OUT_MD}")
    print("run_id | trough_dd | vti_pct | dominant_sleeve | peak vs trough shift")
    for row in analysis["runs"]:
        att = row["trough"]
        print(
            f"{row['run']} | {att.get('trough_dd_pct')}% | {att.get('vti_pct')} | "
            f"{row.get('dominant_sleeve')} | {row.get('peak_vs_trough_shift')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
