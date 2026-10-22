"""Historical cache diagnostic. Not a validation and not a tuned result."""
from __future__ import annotations

import json
from pathlib import Path

from .market import inspect

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    payload = inspect()
    payload["label"] = "HISTORICAL DIAGNOSTIC — NOT VALIDATION"
    payload["tuned"] = False
    payload["virtual_fills"] = 0
    folder = ROOT / "runtime" / "historical_diagnostic"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "diagnostic.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    lines = [
        "# Historical diagnostic",
        "",
        "This is not validation, not a profitability result, and not a reason to change a model.",
        "",
        f"Reader connected: {payload['connected']}. Tradable feed: {payload['tradable_feed']}.",
        f"Symbols in the saved cache: {payload['symbols']}. Newest bar: {payload['newest_bar']}.",
        "",
        "Blockers:",
    ]
    for reason in payload["blockers"]:
        lines.append(f"- {reason}")
    lines += [
        "",
        "No virtual orders were created from these bars. SPY and VTI total returns were not invented.",
        "The saved symbol list is not treated as historical S&P membership.",
    ]
    (folder / "DIAGNOSTIC.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"tradable_feed": payload["tradable_feed"], "blockers": len(payload["blockers"]), "running": False}))


if __name__ == "__main__":
    main()
