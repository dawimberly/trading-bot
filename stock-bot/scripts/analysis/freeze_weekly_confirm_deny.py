"""Weekly freeze confirm/deny plan — end-of-week human gate (measure only).

Aggregates daily hygiene, attribution, geo grades into a CONFIRM/DENY checklist.
Opens like weekly_review (--open) and notifies Telegram/email.
Never edits .env / strategy / live.

Usage (from stock-bot/):
  python scripts/analysis/freeze_weekly_confirm_deny.py --open
  python scripts/analysis/freeze_weekly_confirm_deny.py --test --open

Env:
  FREEZE_OPS_ENABLED=true
  FREEZE_WEEKLY_PLAN_ENABLED=true
  FREEZE_WEEKLY_OPEN=true
  FREEZE_OPS_TELEGRAM=true
  FREEZE_OPS_OLLAMA=false   # optional analyst narrative
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

from modules.freeze_ops_reports import (  # noqa: E402
    freeze_open_enabled,
    freeze_weekly_enabled,
    gather_hygiene,
    last_saturday,
    notify_owner,
    open_report,
    optional_ollama_narrative,
    pack_to_dict,
)

OUT_DIR = ROOT / "data"
OUT_LAST = ROOT / "scripts" / "analysis" / "freeze_weekly_confirm_deny_last.md"
OUT_JSON = ROOT / "scripts" / "analysis" / "freeze_weekly_confirm_deny_last.json"

# Standing freeze decisions — always on the weekly checklist
STANDING = [
    {
        "id": "keep_freeze",
        "title": "Keep forward-paper freeze (no new features)",
        "default": "CONFIRM",
        "detail": "Continue measure-only until freeze end date.",
    },
    {
        "id": "keep_spy_off",
        "title": "Keep paper SPY satellite OFF",
        "default": "CONFIRM",
        "detail": "365d STRICT confirmed; live SPY unchanged.",
    },
    {
        "id": "no_live_change",
        "title": "No live Profile A changes",
        "default": "CONFIRM",
        "detail": "Live SPY-off rejected on 365d live-shaped A/B.",
    },
    {
        "id": "no_new_sleeve",
        "title": "Deny any new sleeve / Iran / headline module",
        "default": "DENY",
        "detail": "Geopolitics stays research sidecar only.",
    },
]


def _read_text(path: Path, limit: int = 4000) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")[:limit]
    except Exception:
        return ""


def _collect_week_hygiene(saturday: date) -> list[dict]:
    items: list[dict] = []
    for i in range(7):
        d = saturday - timedelta(days=6 - i)
        p = OUT_DIR / f"freeze_daily_{d.isoformat()}.md"
        if not p.is_file():
            continue
        items.append({"date": d.isoformat(), "path": str(p), "bytes": p.stat().st_size})
    return items


def build_markdown(
    *,
    saturday: date,
    pack,
    week_files: list[dict],
    narrative: str | None,
) -> str:
    attr = ROOT / "scripts" / "analysis" / "forward_sleeve_attr_last.md"
    geo = ROOT / "scripts" / "analysis" / "geopolitical_event_study_last.md"
    weekly = OUT_DIR / "weekly_review_latest.md"

    lines = [
        f"# Freeze weekly confirm/deny plan — week ending {saturday.isoformat()}",
        "",
        f"Generated: {pack.generated_at}",
        "",
        "**You confirm or deny. Nothing auto-applies.** "
        "Freeze-safe: no .env writes, no live Profile A, no new sleeves.",
        "",
        "## Decisions required",
        "",
        "| # | ID | Default | Your call | Item |",
        "|---|----|---------|-----------|------|",
    ]
    n = 1
    for s in STANDING:
        lines.append(
            f"| {n} | `{s['id']}` | **{s['default']}** | CONFIRM / DENY / HOLD | {s['title']} |"
        )
        n += 1

    actionable = pack.cleanups() + pack.anomalies()
    # Dedupe standing-like
    for f in actionable:
        if f.id in {s["id"] for s in STANDING}:
            continue
        if f.severity == "info":
            continue
        lines.append(
            f"| {n} | `{f.id}` | **{f.default_action}** | CONFIRM / DENY / HOLD | {f.title} |"
        )
        n += 1

    lines.extend(
        [
            "",
            "## How to respond",
            "",
            "1. Open this file in PyCharm (or Telegram summary).",
            "2. Reply Telegram e.g. `FREEZE CONFIRM keep_freeze,keep_spy_off DENY no_new_sleeve`",
            "3. Or edit a copy: mark Your call column — still manual; bot will not read it until you ask.",
            "4. **Default if no reply: all DENY/HOLD as listed — freeze continues unchanged.**",
            "",
            "## Standing rationale",
            "",
        ]
    )
    for s in STANDING:
        lines.append(f"- `{s['id']}` ({s['default']}): {s['detail']}")

    lines.extend(["", "## This week's hygiene trail", ""])
    if not week_files:
        lines.append("_No daily freeze memos found — run daily hygiene this week._")
    else:
        for w in week_files:
            lines.append(f"- {w['date']}: `{Path(w['path']).name}` ({w['bytes']} bytes)")

    lines.extend(["", "## Measurement snapshots", ""])
    lines.append(f"- Attribution: `{'present' if attr.is_file() else 'MISSING'}` — {attr.name}")
    lines.append(f"- Geo event study: `{'present' if geo.is_file() else 'MISSING'}` — {geo.name}")
    lines.append(f"- Weekly review: `{'present' if weekly.is_file() else 'MISSING'}` — {weekly.name}")
    hb = (
        f"{pack.heartbeat_age_min:.0f}m"
        if pack.heartbeat_age_min is not None
        else "n/a"
    )
    lines.append(f"- Heartbeat age: {hb}")
    lines.append(f"- Equity: {pack.equity if pack.equity is not None else 'n/a'}")
    lines.append(f"- Regime: {pack.regime or 'n/a'}")

    if narrative:
        lines.extend(["", "## Analyst narrative (Ollama — non-binding)", "", narrative, ""])

    lines.extend(
        [
            "",
            "## Full detail",
            "",
            "### Hygiene findings",
            "",
        ]
    )
    for f in pack.findings:
        lines.append(
            f"- `{f.id}` [{f.severity}] default={f.default_action}: {f.title} — {f.detail}"
        )
    lines.extend(["", "### Attribution excerpt", "", "```", _read_text(attr, 2500) or "(none)", "```", ""])
    lines.extend(["### Geo study excerpt", "", "```", _read_text(geo, 2000) or "(none)", "```", ""])
    lines.append("**Freeze continues unless you explicitly CONFIRM ending it.**")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Weekly freeze confirm/deny plan")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--test", action="store_true", help="Any day; tag [TEST]")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--force", action="store_true", help="Overwrite existing Saturday plan")
    args = ap.parse_args(argv)

    if not args.test and not freeze_weekly_enabled():
        print("[freeze_weekly] Disabled (FREEZE_WEEKLY_PLAN_ENABLED / FREEZE_OPS_ENABLED).")
        return 0

    saturday = date.today() if args.test else last_saturday()
    dated = OUT_DIR / f"freeze_confirm_deny_{saturday.isoformat()}.md"
    if dated.is_file() and not args.force and not args.test:
        print(f"[freeze_weekly] Already wrote {dated.name} — skip (use --force).")
        if args.open or freeze_open_enabled(weekly=True):
            open_report(dated)
        return 0

    # Refresh attribution quietly if missing/stale? Skip — measure scripts stay explicit.
    pack = gather_hygiene(lookback_days=7)
    week_files = _collect_week_hygiene(saturday)
    ctx = build_markdown(saturday=saturday, pack=pack, week_files=week_files, narrative=None)
    narrative = optional_ollama_narrative(ctx)
    md = build_markdown(
        saturday=saturday, pack=pack, week_files=week_files, narrative=narrative
    )
    if args.test:
        md = md.replace("# Freeze weekly", "# [TEST] Freeze weekly", 1)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dated.write_text(md, encoding="utf-8")
    OUT_LAST.write_text(md, encoding="utf-8")
    shutil.copyfile(dated, OUT_DIR / "freeze_confirm_deny_latest.md")
    payload = {
        "saturday": saturday.isoformat(),
        "hygiene": pack_to_dict(pack),
        "week_files": week_files,
        "standing": STANDING,
        "test": bool(args.test),
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(md.encode("ascii", errors="replace").decode("ascii"))
    print(f"\nWrote {dated}")

    if not args.no_notify:
        tag = "[TEST] " if args.test else ""
        subject = f"{tag}[PythonTrading FREEZE] Weekly confirm/deny — {saturday.isoformat()}"
        notify_owner(subject, md, out_path=dated)

    if args.open or freeze_open_enabled(weekly=True):
        open_report(dated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
