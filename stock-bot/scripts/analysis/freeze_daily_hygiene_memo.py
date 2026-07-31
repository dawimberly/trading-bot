"""Daily freeze hygiene memo — small cleanups to consider (measure only).

Writes markdown, optional Telegram, optional --open (PyCharm/default .md app).
Never edits .env / strategy / live.

Usage (from stock-bot/):
  python scripts/analysis/freeze_daily_hygiene_memo.py
  python scripts/analysis/freeze_daily_hygiene_memo.py --open
  python scripts/analysis/freeze_daily_hygiene_memo.py --test

Env:
  FREEZE_OPS_ENABLED=true
  FREEZE_DAILY_HYGIENE_ENABLED=true
  FREEZE_DAILY_OPEN=false
  FREEZE_OPS_TELEGRAM=true
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

from modules.freeze_ops_reports import (  # noqa: E402
    freeze_daily_enabled,
    freeze_open_enabled,
    gather_hygiene,
    notify_owner,
    open_report,
    pack_to_dict,
)

OUT_DIR = ROOT / "data"
OUT_LAST = ROOT / "scripts" / "analysis" / "freeze_daily_hygiene_last.md"
OUT_JSON = ROOT / "scripts" / "analysis" / "freeze_daily_hygiene_last.json"


def build_markdown(pack, *, day: date) -> str:
    hb = (
        f"{pack.heartbeat_age_min:.0f}m"
        if pack.heartbeat_age_min is not None
        else "n/a"
    )
    lines = [
        f"# Freeze daily hygiene — {day.isoformat()}",
        "",
        f"Generated: {pack.generated_at}",
        f"Equity: {pack.equity if pack.equity is not None else 'n/a'} | "
        f"Regime: {pack.regime or 'n/a'} | Heartbeat age: {hb}",
        "",
        "**Freeze-safe.** Cleanups below are for human CONFIRM / DENY / HOLD. "
        "No auto strategy or live changes.",
        "",
        "## Cleanup candidates (consider today)",
        "",
    ]
    cleanups = pack.cleanups() + pack.anomalies()
    if not cleanups:
        lines.append("_No obvious cleanups today._")
    else:
        lines.append("| ID | Sev | Default | Item | Detail |")
        lines.append("|----|-----|---------|------|--------|")
        for f in cleanups:
            lines.append(
                f"| `{f.id}` | {f.severity} | **{f.default_action}** | {f.title} | {f.detail} |"
            )
    lines.extend(["", "## Info", ""])
    infos = [f for f in pack.findings if f.severity == "info"]
    if not infos:
        lines.append("_None._")
    else:
        for f in infos:
            lines.append(f"- **{f.title}** — {f.detail}")

    lines.extend(
        [
            "",
            "## How to respond",
            "",
            "- Reply in Telegram: `CONFIRM <id>` / `DENY <id>` / `HOLD <id>` (or ignore = HOLD).",
            "- Or tick the weekly confirm/deny plan Saturday.",
            "- Do **not** change live Profile A or invent new sleeves from this memo.",
            "",
            "## Full detail",
            "",
        ]
    )
    for f in pack.findings:
        lines.append(f"### `{f.id}` ({f.severity})")
        lines.append(f"- Default: **{f.default_action}**")
        lines.append(f"- {f.detail}")
        if f.evidence:
            lines.append(f"- Evidence: `{f.evidence}`")
        lines.append("")
    for n in pack.notes:
        lines.append(f"- Note: {n}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Daily freeze hygiene memo")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--test", action="store_true", help="Force run even if disabled")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--lookback-days", type=int, default=1)
    args = ap.parse_args(argv)

    if not args.test and not freeze_daily_enabled():
        print("[freeze_daily] Disabled (FREEZE_DAILY_HYGIENE_ENABLED / FREEZE_OPS_ENABLED).")
        return 0

    day = date.today()
    dated = OUT_DIR / f"freeze_daily_{day.isoformat()}.md"
    if dated.is_file() and not args.test:
        print(f"[freeze_daily] Already wrote {dated.name} today — skip.")
        if args.open or freeze_open_enabled(weekly=False):
            open_report(dated)
        return 0

    pack = gather_hygiene(lookback_days=max(1, int(args.lookback_days)))
    md = build_markdown(pack, day=day)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dated.write_text(md, encoding="utf-8")
    OUT_LAST.write_text(md, encoding="utf-8")
    shutil.copyfile(dated, OUT_DIR / "freeze_daily_latest.md")
    OUT_JSON.write_text(json.dumps(pack_to_dict(pack), indent=2), encoding="utf-8")
    print(md.encode("ascii", errors="replace").decode("ascii"))
    print(f"\nWrote {dated}")

    if not args.no_notify:
        n_clean = len(pack.cleanups()) + len(pack.anomalies())
        subject = f"[PythonTrading FREEZE] Daily hygiene — {n_clean} item(s) to consider"
        notify_owner(subject, md, out_path=dated)

    if args.open or freeze_open_enabled(weekly=False):
        open_report(dated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
