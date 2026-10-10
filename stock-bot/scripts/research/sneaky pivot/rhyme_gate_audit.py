"""
Static audit: find every place RHYME_A / RHYME_B (or A/B regime labels)
are referenced anywhere in stock-bot, and classify whether each hit
looks like a real behavioral gate (risk, sizing, exposure, position
control) versus just reporting/logging/attribution.

This does NOT execute any trading logic. Text search + heuristic
classifier — answers: "how many places would have behaved differently
if A/B had ever actually fired?" before touching cross_asset_vol_score.

Run from stock-bot/:
    python "scripts/research/sneaky pivot/rhyme_gate_audit.py"
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# stock-bot root (…/scripts/research/sneaky pivot/file.py → parents[3])
REPO_ROOT = Path(__file__).resolve().parents[3]

# Patterns that indicate an A/B regime reference at all
REGIME_REF_PATTERNS = [
    r"rhyme\s*==\s*[\'\"]A[\'\"]",
    r"rhyme\s*==\s*[\'\"]B[\'\"]",
    r"regime\s*==\s*[\'\"]A[\'\"]",
    r"regime\s*==\s*[\'\"]B[\'\"]",
    r"RHYME_A\b",
    r"RHYME_B\b",
    r"Euphoric_Volatility",
    r"Panic_Volatility",
    r"""['\"]A['\"].*['\"]B['\"]""",  # A and B nearby in same line
    r"in\s*\[.*[\'\"]A[\'\"].*[\'\"]B[\'\"].*\]",
    r"in\s*\(.*[\'\"]A[\'\"].*[\'\"]B[\'\"].*\)",
]

# Keywords near a hit that suggest a REAL behavioral gate
GATE_KEYWORDS = [
    "size",
    "sizing",
    "position",
    "exposure",
    "reduce",
    "flat",
    "block",
    "skip",
    "halt",
    "pause",
    "risk",
    "stop_loss",
    "max_position",
    "allocation",
    "weight",
    "leverage",
    "gate",
    "disable",
    "enable",
    "trade_allowed",
    "should_trade",
    "can_trade",
    "paused",
    "trim",
    "mult",
    "cap",
    "buffer",
    "short",
    "deploy",
    "refuse",
    "allow",
]

REPORT_KEYWORDS = [
    "print",
    "log",
    "report",
    "breakdown",
    "attribution",
    "chart",
    "plot",
    "summary",
    "csv",
    "dashboard",
    "banner",
    "docstring",
    "comment",
    "label",
    "rhyme_regime_labels",
]

CODE_EXTENSIONS = {".py"}
EXCLUDE_DIR_NAMES = {
    ".git",
    "__pycache__",
    "venv",
    ".venv",
    "node_modules",
    "venv311",
    "build",
    "dist",
    ".pytest_cache",
}
# Skip our research sidecar so we don't count audit/docs as live gates
EXCLUDE_PATH_SUBSTR = (
    "scripts\\research",
    "scripts/research",
    "backup_",
)


@dataclass
class Hit:
    file: Path
    line_no: int
    line: str
    context: str
    classification: str  # GATE | REPORT | UNCLEAR


def classify(context: str) -> str:
    ctx_lower = context.lower()
    gate_score = sum(kw in ctx_lower for kw in GATE_KEYWORDS)
    report_score = sum(kw in ctx_lower for kw in REPORT_KEYWORDS)

    if gate_score > 0 and gate_score >= report_score:
        return "GATE"
    if report_score > 0:
        return "REPORT"
    return "UNCLEAR"


def _should_skip(path: Path) -> bool:
    parts = set(path.parts)
    if parts & EXCLUDE_DIR_NAMES:
        return True
    s = str(path)
    return any(excl in s for excl in EXCLUDE_PATH_SUBSTR)


def scan_file(path: Path) -> list[Hit]:
    hits: list[Hit] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return hits

    for i, line in enumerate(lines):
        # Skip pure comment-only documentation lines that only name the label
        stripped = line.strip()
        if stripped.startswith("#") and not any(
            k in stripped.lower() for k in ("if ", "return", "and ", "or ")
        ):
            # still count if it looks like a commented-out gate
            if not any(k in stripped.lower() for k in GATE_KEYWORDS):
                continue

        for pattern in REGIME_REF_PATTERNS:
            if re.search(pattern, line):
                start = max(0, i - 3)
                end = min(len(lines), i + 4)
                context = "\n".join(lines[start:end])
                hits.append(
                    Hit(
                        file=path,
                        line_no=i + 1,
                        line=stripped,
                        context=context,
                        classification=classify(context),
                    )
                )
                break
    return hits


def scan_repo(root: Path) -> list[Hit]:
    all_hits: list[Hit] = []
    for path in root.rglob("*.py"):
        if _should_skip(path):
            continue
        if path.suffix not in CODE_EXTENSIONS:
            continue
        all_hits.extend(scan_file(path))
    return all_hits


def write_report(hits: list[Hit], out_path: Path) -> None:
    gate_hits = [h for h in hits if h.classification == "GATE"]
    report_hits = [h for h in hits if h.classification == "REPORT"]
    unclear_hits = [h for h in hits if h.classification == "UNCLEAR"]

    lines = [
        "# RHYME A/B Gate Audit",
        "",
        "Static scan of `stock-bot/` for RHYME_A / RHYME_B / Euphoric / Panic references.",
        "Heuristic GATE vs REPORT vs UNCLEAR — manually verify GATE/UNCLEAR before",
        "treating as production behavior. Research only; freeze unchanged.",
        "",
        f"Total hits: {len(hits)}  |  "
        f"GATE: {len(gate_hits)}  |  REPORT: {len(report_hits)}  |  "
        f"UNCLEAR: {len(unclear_hits)}",
        "",
        "Companion finding: `cross_asset_vol_score` currently zeros on wide daily "
        "matrices (`dropna(how='any')`), so these gates rarely/never see A/B fire.",
        "",
    ]

    for label, group in [
        ("GATE (real behavioral impact — check these FIRST)", gate_hits),
        ("UNCLEAR (manually verify)", unclear_hits),
        ("REPORT (informational only, lower priority)", report_hits),
    ]:
        lines.append(f"## {label}")
        lines.append("")
        if not group:
            lines.append("(none found)")
            lines.append("")
            continue
        # Dedupe identical file:line
        seen: set[tuple[str, int]] = set()
        for h in sorted(group, key=lambda x: (str(x.file), x.line_no)):
            key = (str(h.file), h.line_no)
            if key in seen:
                continue
            seen.add(key)
            try:
                rel = h.file.relative_to(REPO_ROOT)
            except ValueError:
                rel = h.file
            lines.append(f"### `{rel}:{h.line_no}`")
            lines.append("")
            lines.append("```python")
            lines.append(h.context)
            lines.append("```")
            lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    print(f"Scanning {REPO_ROOT} ...")
    hits = scan_repo(REPO_ROOT)
    out_path = Path(__file__).parent / "output" / "rhyme_gate_audit.md"
    out_path.parent.mkdir(exist_ok=True, parents=True)
    write_report(hits, out_path)

    gate_count = sum(1 for h in hits if h.classification == "GATE")
    unclear_count = sum(1 for h in hits if h.classification == "UNCLEAR")
    report_count = sum(1 for h in hits if h.classification == "REPORT")
    print(f"Scanned repo. {len(hits)} total A/B references found.")
    print(f"  GATE:    {gate_count}  -- check these first")
    print(f"  UNCLEAR: {unclear_count}")
    print(f"  REPORT:  {report_count}")
    print(f"Report written to: {out_path}")

    # Compact GATE file list for console
    gate_files: dict[str, list[int]] = {}
    for h in hits:
        if h.classification != "GATE":
            continue
        try:
            rel = str(h.file.relative_to(REPO_ROOT))
        except ValueError:
            rel = str(h.file)
        gate_files.setdefault(rel, []).append(h.line_no)
    if gate_files:
        print("\nGATE files:")
        for rel, nos in sorted(gate_files.items()):
            print(f"  {rel}: lines {', '.join(map(str, nos))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
