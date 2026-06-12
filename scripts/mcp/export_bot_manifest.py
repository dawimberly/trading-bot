"""Export a token-minimal manifest of the trading bot for Grok / external LLMs.

Run: python scripts/mcp/export_bot_manifest.py
Output: data/bot_manifest.txt (~3-5k tokens vs full repo)
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "bot_manifest.txt"
SKIP_DIRS = {".venv", "dist", "build", "__pycache__", "nerdminer", "small claims"}


def _py_files() -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                out.append(Path(dirpath) / fn)
    return sorted(out)


def _module_blurb(path: Path) -> str:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return ""
    doc = ast.get_docstring(tree) or ""
    doc = " ".join(doc.split())[:120]
    rel = path.relative_to(ROOT).as_posix()
    return f"{rel}|{doc}" if doc else f"{rel}|"


def _read_summary_snippet() -> str:
    p = ROOT / "scripts" / "analysis" / "OPTIMIZED_SYSTEM_SUMMARY.md"
    if not p.is_file():
        return ""
    text = p.read_text(encoding="utf-8", errors="replace")
    # First ~80 lines only
    lines = text.splitlines()[:80]
    return "\n".join(lines)


def build_manifest() -> str:
    modules = [p for p in _py_files() if "modules" in p.parts]
    scripts = [p for p in _py_files() if "scripts" in p.parts]
    roots = [
        p
        for p in _py_files()
        if p.parent == ROOT
        and p.name
        in {
            "run_all.py",
            "run_paper_bot.py",
            "run_spy.py",
            "backtester.py",
            "config.py",
            "fetch_data.py",
            "portal.py",
            "dashboard_app.py",
            "status.py",
            "launch_bots.py",
        }
    ]

    lines = [
        "# PythonTrading BOT_MANIFEST (compact; not full source)",
        "# Use this for architecture review — ask for file paths to drill down.",
        "",
        "## PROFILES",
        "A live current_dynamic: 90% VTI (<$500), yield-gate-only, overlap/chunk/cofire OFF, ~$100 account.",
        "B paper_aggressive: dynamic VTI 40-75%, overlap+chunk+cofire ON, macro/social/options(opt) paper-only.",
        "",
        "## MAIN LOOP (run_all.py)",
        "fetch/refresh bars -> regime(vol,sentiment) -> game_plan yield gate -> VTI core rebalance",
        "-> optional macro/options/social sleeves (paper) -> position exits -> SPY/NYSE/crypto strategies",
        "-> AlpacaExecutor sleeve caps -> heartbeat JSON -> sleep cycle.",
        "",
        "## SLEEVES (pipeline_strategies + alpaca_executor)",
        "SPY 45% MA200 trend | Crypto 20% z-score pairs vol-gated live | NYSE 20% momentum MA50",
        "VTI passive core | Cash 15% | Metal 0% yield-gate-only",
        "",
        "## KEY ROOT FILES",
    ]
    for p in roots:
        lines.append(_module_blurb(p))

    lines.extend(["", "## MODULES (path|doc)"])
    for p in modules:
        lines.append(_module_blurb(p))

    lines.extend(["", "## SCRIPTS (path|doc)"])
    for p in scripts[:60]:  # cap script list
        lines.append(_module_blurb(p))
    if len(scripts) > 60:
        lines.append(f"... +{len(scripts) - 60} more scripts")

    lines.extend(["", "## OPTIMIZED_SYSTEM_SUMMARY (excerpt)", _read_summary_snippet()])
    return "\n".join(lines)


def build_ultra_manifest() -> str:
    """~1-2k tokens: architecture only, no script dump."""
    core = build_manifest().split("## SCRIPTS")[0]
    return core + "\n## NOTE\nFull file tree in data/bot_manifest.txt; drill down by path.\n"


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--ultra", action="store_true", help="Smaller manifest for Grok (~1.5k tokens)")
    args = parser.parse_args()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    text = build_ultra_manifest() if args.ultra else build_manifest()
    OUT.write_text(text, encoding="utf-8")
    chars = len(text)
    est_tokens = chars // 4
    print(f"Wrote {OUT} ({chars:,} chars, ~{est_tokens:,} tokens)")


if __name__ == "__main__":
    main()
