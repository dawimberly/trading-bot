"""Exit / churn measure week — charter + Saturday evaluation (measure only).

Tracks paper_v2 exit quality for one calendar week. Never writes .env / never restarts.

Usage (from stock-bot/):
  python scripts/analysis/exit_measure_week.py --start          # seed charter (once)
  python scripts/analysis/exit_measure_week.py --refresh        # mid-week snapshot
  python scripts/analysis/exit_measure_week.py --evaluate       # Saturday verdict
  python scripts/analysis/exit_measure_week.py --status

Hooks: freeze_weekly_confirm_deny.py and weekly_review.py append the evaluate section.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

BOOK = ROOT / "data" / "portal" / "users" / "dawimberly" / "books" / "alpaca_paper_v2"
JOURNAL = BOOK / "paper_journal.csv"
CHARTER_DIR = ROOT / "data"
OUT_MD = Path(__file__).with_name("exit_measure_week_last.md")
OUT_JSON = Path(__file__).with_name("exit_measure_week_last.json")
PY = Path(sys.executable)

# Trading week under review (ET calendar): Mon open → Sat evaluate
WEEK_START = date(2026, 9, 8)  # first cash session after this Sunday start
WEEK_END = date(2026, 9, 12)  # Saturday evaluate target


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _charter_path(saturday: date | None = None) -> Path:
    sat = saturday or WEEK_END
    return CHARTER_DIR / f"exit_measure_week_{sat.isoformat()}.json"


def _active_charter() -> Path | None:
    """Prefer current week charter; else newest exit_measure_week_*.json."""
    cur = _charter_path()
    if cur.is_file():
        return cur
    found = sorted(CHARTER_DIR.glob("exit_measure_week_*.json"), reverse=True)
    return found[0] if found else None


def _load_charter(path: Path | None = None) -> dict[str, Any] | None:
    p = path or _active_charter()
    if p is None or not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_charter(data: dict[str, Any], path: Path | None = None) -> Path:
    p = path or _charter_path(date.fromisoformat(data["week_end"]))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return p


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _run_helpers() -> None:
    """Refresh paper_v2 measure scripts used as inputs (best-effort)."""
    cmds = [
        [str(PY), str(ROOT / "scripts/analysis/one_r_hit_test.py")],
        [str(PY), str(ROOT / "scripts/analysis/rhyme_conflict_audit.py"), "--book", "paper_v2", "--days", "7"],
        [str(PY), str(ROOT / "scripts/analysis/nyse_sell_followthrough.py"), "--book", "paper_v2"],
        [str(PY), str(ROOT / "scripts/analysis/forward_sleeve_attribution.py"), "--book", "paper_v2", "--days", "7"],
    ]
    for cmd in cmds:
        try:
            subprocess.run(cmd, cwd=str(ROOT), check=False, timeout=180)
        except Exception as exc:
            print(f"[exit_measure_week] helper warn: {cmd[-1]}: {exc}", flush=True)


def _snapshot_from_disk() -> dict[str, Any]:
    """Pull latest measure artifacts into one snapshot (no orders)."""
    one_r_md = (ROOT / "scripts/analysis/one_r_hit_test_last.md").read_text(encoding="utf-8", errors="replace")
    rhyme = _read_json(ROOT / "scripts/analysis/rhyme_conflict_audit_paper_v2_last.json")
    if not rhyme:
        rhyme = _read_json(ROOT / "scripts/analysis/rhyme_conflict_audit_last.json")
    attr = _read_json(ROOT / "scripts/analysis/forward_sleeve_attr_last.json")
    if not attr:
        attr = _read_json(ROOT / "scripts/analysis/forward_sleeve_attr_paper_v2_last.json")
    ab = _read_json(ROOT / "scripts/analysis/one_r_hit_backtest_last.json")
    follow_md = ROOT / "scripts/analysis/nyse_sell_followthrough_paper_v2_last.md"
    follow_text = follow_md.read_text(encoding="utf-8", errors="replace") if follow_md.is_file() else ""

    # Parse 1R headline from markdown (stable enough for charter)
    hit_closed = None
    hit_n = None
    import re

    for line in one_r_md.splitlines():
        if "hit 1R:" in line and "Closed rounds scored" in line:
            m = re.search(
                r"Closed rounds scored:\s*\*\*(\d+)\*\*\s*—\s*hit 1R:\s*\*\*(\d+)",
                line,
            )
            if m:
                hit_n = int(m.group(1))
                hit_closed = int(m.group(2))
            break

    stop_flags = follow_text.count("STOP+5%+")
    summ = (rhyme or {}).get("summary") or {}
    atr = (attr or {}).get("data_quality") or {}
    sleeves = (attr or {}).get("sleeves") or {}
    nyse = sleeves.get("nyse") or {}

    research = {}
    for key in ("hold", "take_1r", "wide_stop_3x", "long_hold_45"):
        block = (ab or {}).get(key) or {}
        if block:
            research[key] = {
                "book_ret_pct": block.get("book_ret_pct"),
                "max_dd_pct": block.get("max_dd_pct"),
                "win_rate": block.get("win_rate"),
                "trades": block.get("trades"),
            }

    hb_path = BOOK / "bot_heartbeat.json"
    equity = None
    if hb_path.is_file():
        try:
            hb = json.loads(hb_path.read_text(encoding="utf-8"))
            equity = hb.get("equity") or hb.get("portfolio_value")
        except Exception:
            pass

    return {
        "ts": _utc_now(),
        "journal": str(JOURNAL),
        "equity": equity,
        "one_r_closed_n": hit_n,
        "one_r_hits": hit_closed,
        "one_r_hit_rate_pct": (
            round(100.0 * hit_closed / hit_n, 1) if hit_n and hit_closed is not None else None
        ),
        "cancel_ratio": summ.get("cancel_ratio"),
        "cancel_ex_core": summ.get("cancel_ex_core"),
        "rhyme_fills": summ.get("n_fills"),
        "stop_bounce_flags_in_report": stop_flags,
        "nyse_realized_pnl": nyse.get("realized_pnl"),
        "nyse_unrealized_pnl": nyse.get("unrealized_pnl"),
        "attr_period_return_pct": (attr or {}).get("period_return_pct"),
        "attr_fills": atr.get("fills_count"),
        "research_exit_ab": research,
        "candidates": {
            "wide_stop_3x": "HOLD — research candidate; measure stop-bounce this week",
            "same_day_churn": "HOLD — cancel_ex_core / same-day exits; no overlay add",
            "take_1r_wire": "REJECT research default — DD >2pp vs hold on 365d A/B",
        },
    }


def start_week(*, force: bool = False, run_helpers: bool = True) -> Path:
    path = _charter_path(WEEK_END)
    if path.is_file() and not force:
        print(f"[exit_measure_week] Charter exists: {path.name} (use --force to overwrite)")
        return path
    if run_helpers:
        print("[exit_measure_week] Refreshing helpers for baseline…", flush=True)
        _run_helpers()
    snap = _snapshot_from_disk()
    charter = {
        "research_only": True,
        "book": "alpaca_paper_v2",
        "week_start": WEEK_START.isoformat(),
        "week_end": WEEK_END.isoformat(),
        "started_at": _utc_now(),
        "goal": (
            "Measure exit quality + churn on paper_v2 for one week; "
            "evaluate on Saturday weekly/freeze report. No .env changes."
        ),
        "evaluate_rules": {
            "wide_stop_3x": (
                "HOLD→consider next week only if stop_bounce_flags this week ≥2 "
                "AND cancel_ex_core does not worsen vs baseline by >0.10"
            ),
            "same_day_churn": (
                "HOLD→consider hygiene tighten only if cancel_ex_core ≥0.70 "
                "for the week window"
            ),
            "take_1r_wire": "Stay REJECT unless 365d A/B DD gap vs hold ≤2pp (re-run)",
            "one_r_reality": "Note only — paper 1R hit rate; do not retune from <10 closes",
        },
        "baseline": snap,
        "snapshots": [snap],
        "evaluation": None,
    }
    _save_charter(charter, path)
    _write_status_md(charter)
    print(f"Wrote {path}")
    print(f"Wrote {OUT_MD}")
    return path


def refresh(*, run_helpers: bool = True) -> Path | None:
    charter = _load_charter()
    if not charter:
        print("[exit_measure_week] No charter — run --start first")
        return None
    if run_helpers:
        print("[exit_measure_week] Refreshing helpers…", flush=True)
        _run_helpers()
    snap = _snapshot_from_disk()
    charter.setdefault("snapshots", []).append(snap)
    charter["last_refresh"] = snap["ts"]
    path = _save_charter(charter)
    _write_status_md(charter)
    print(f"Updated {path}")
    return path


def evaluate(*, run_helpers: bool = True) -> dict[str, Any]:
    charter = _load_charter()
    if not charter:
        print("[exit_measure_week] No charter — run --start first")
        return {}
    if run_helpers:
        print("[exit_measure_week] Refreshing helpers for evaluate…", flush=True)
        _run_helpers()
    end = _snapshot_from_disk()
    base = charter.get("baseline") or {}
    rules = charter.get("evaluate_rules") or {}

    bounce = int(end.get("stop_bounce_flags_in_report") or 0)
    # Prefer delta of flags if baseline had some already counted in same report window
    bounce_base = int(base.get("stop_bounce_flags_in_report") or 0)
    bounce_new = max(0, bounce - bounce_base)

    cancel_end = end.get("cancel_ex_core")
    cancel_base = base.get("cancel_ex_core")
    cancel_delta = None
    if cancel_end is not None and cancel_base is not None:
        try:
            cancel_delta = float(cancel_end) - float(cancel_base)
        except (TypeError, ValueError):
            cancel_delta = None

    decisions: list[dict[str, Any]] = []

    # wide_stop
    wide_default = "HOLD"
    wide_note = rules.get("wide_stop_3x", "")
    if bounce_new >= 2 and (cancel_delta is None or cancel_delta <= 0.10):
        wide_default = "HOLD"
        wide_note = (
            f"Stop-bounce new flags={bounce_new} (≥2). Still HOLD for human — "
            "do not auto-wire 3× ATR; discuss next week."
        )
    elif bounce_new >= 2:
        wide_note = f"Stop-bounce={bounce_new} but cancel_ex_core worsened ({cancel_delta:+.3f})."
    else:
        wide_note = f"Stop-bounce new flags={bounce_new} (<2). Keep HOLD / no wire."
    decisions.append(
        {
            "id": "exit_wide_stop_3x",
            "title": "Consider wide ATR stop (3×) on paper next",
            "default": wide_default,
            "detail": wide_note,
        }
    )

    # churn
    churn_default = "HOLD"
    churn_note = rules.get("same_day_churn", "")
    try:
        cx = float(cancel_end) if cancel_end is not None else None
    except (TypeError, ValueError):
        cx = None
    if cx is not None and cx >= 0.70:
        churn_note = (
            f"cancel_ex_core={cx:.3f} ≥0.70 — HOLD for human: same-day rebuy / "
            "concentration micro-trim hygiene (not new overlays)."
        )
    else:
        churn_note = f"cancel_ex_core={cx} — below 0.70 gate; HOLD / no change."
    decisions.append(
        {
            "id": "exit_same_day_churn",
            "title": "Consider same-day / concentration churn hygiene",
            "default": churn_default,
            "detail": churn_note,
        }
    )

    decisions.append(
        {
            "id": "exit_take_1r_wire",
            "title": "Wire take_1r early exits",
            "default": "DENY",
            "detail": rules.get("take_1r_wire", "365d DD too deep vs hold."),
        }
    )

    one_r_note = (
        f"1R hits end={end.get('one_r_hits')}/{end.get('one_r_closed_n')} "
        f"(baseline {base.get('one_r_hits')}/{base.get('one_r_closed_n')}). "
        "Informational only."
    )
    decisions.append(
        {
            "id": "exit_one_r_note",
            "title": "Note paper 1R hit reality (no retune)",
            "default": "HOLD",
            "detail": one_r_note,
        }
    )

    evaluation = {
        "evaluated_at": _utc_now(),
        "end_snapshot": end,
        "bounce_new": bounce_new,
        "cancel_ex_core_end": cancel_end,
        "cancel_ex_core_delta": cancel_delta,
        "decisions": decisions,
        "verdict": (
            "MEASURE WEEK COMPLETE — all defaults HOLD/DENY; "
            "nothing auto-applies. Owner confirms on freeze weekly plan."
        ),
    }
    charter["evaluation"] = evaluation
    charter["snapshots"] = list(charter.get("snapshots") or []) + [end]
    path = _save_charter(charter)
    _write_status_md(charter)
    print(evaluation["verdict"])
    print(f"Wrote {path}")
    print(f"Wrote {OUT_MD}")
    return evaluation


def render_section(charter: dict[str, Any] | None = None) -> str:
    """Markdown section for weekly_review / freeze_weekly."""
    c = charter or _load_charter()
    if not c:
        return (
            "## Exit measure week\n\n"
            "_No active charter. Seed with "
            "`python scripts/analysis/exit_measure_week.py --start`._\n"
        )
    base = c.get("baseline") or {}
    ev = c.get("evaluation")
    lines = [
        "## Exit measure week",
        "",
        f"**Book:** `{c.get('book')}` · **Window:** {c.get('week_start')} → {c.get('week_end')}  ",
        f"**Started:** {c.get('started_at')} · measure only (no `.env`).",
        "",
        "### Baseline (week start)",
        "",
        f"- Equity: {base.get('equity')}",
        f"- 1R hits: {base.get('one_r_hits')}/{base.get('one_r_closed_n')} "
        f"({base.get('one_r_hit_rate_pct')}%)",
        f"- cancel_ex_core: {base.get('cancel_ex_core')}",
        f"- Stop-bounce flags in follow-through report: {base.get('stop_bounce_flags_in_report')}",
        f"- NYSE realized / unrealized: {base.get('nyse_realized_pnl')} / {base.get('nyse_unrealized_pnl')}",
        "",
        "### Research reference (365d daily A/B — not promote)",
        "",
    ]
    res = base.get("research_exit_ab") or {}
    if res:
        lines.append("| policy | book% | maxDD% |")
        lines.append("|---|---:|---:|")
        for k in ("hold", "take_1r", "wide_stop_3x", "long_hold_45"):
            b = res.get(k) or {}
            if not b:
                continue
            lines.append(f"| {k} | {b.get('book_ret_pct')} | {b.get('max_dd_pct')} |")
        lines.append("")
    lines.append(
        "Candidates under watch: **wide_stop_3x** (if stop-bounce ≥2), "
        "**same_day_churn** (if cancel_ex_core ≥0.70), **take_1r** stays DENY."
    )
    lines.append("")

    if not ev:
        lines.extend(
            [
                "### Evaluation",
                "",
                f"_Pending Saturday {c.get('week_end')}. Run "
                "`python scripts/analysis/exit_measure_week.py --evaluate` "
                "(also hooked from freeze weekly / weekly review)._",
                "",
            ]
        )
        return "\n".join(lines)

    lines.extend(["### Evaluation", "", f"**{ev.get('verdict')}**", ""])
    lines.append("| ID | Default | Item | Detail |")
    lines.append("|----|---------|------|--------|")
    for d in ev.get("decisions") or []:
        lines.append(
            f"| `{d.get('id')}` | **{d.get('default')}** | {d.get('title')} | {d.get('detail')} |"
        )
    end = ev.get("end_snapshot") or {}
    lines.extend(
        [
            "",
            f"- End equity: {end.get('equity')}",
            f"- End 1R: {end.get('one_r_hits')}/{end.get('one_r_closed_n')}",
            f"- End cancel_ex_core: {end.get('cancel_ex_core')} "
            f"(Δ {ev.get('cancel_ex_core_delta')})",
            f"- New stop-bounce flags: {ev.get('bounce_new')}",
            "",
        ]
    )
    return "\n".join(lines)


def freeze_decision_rows(charter: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Rows for freeze weekly CONFIRM/DENY table."""
    c = charter or _load_charter()
    if not c:
        return []
    ev = c.get("evaluation")
    if ev and ev.get("decisions"):
        return [
            {
                "id": d["id"],
                "title": d["title"],
                "default": d["default"],
                "detail": d.get("detail") or "",
            }
            for d in ev["decisions"]
        ]
    # Pre-evaluate: keep as HOLD watch items
    return [
        {
            "id": "exit_wide_stop_3x",
            "title": "Exit measure: wide ATR stop (3×) — evaluate Sat",
            "default": "HOLD",
            "detail": "Charter active; verdict after --evaluate.",
        },
        {
            "id": "exit_same_day_churn",
            "title": "Exit measure: same-day / concentration churn — evaluate Sat",
            "default": "HOLD",
            "detail": "Charter active; verdict after --evaluate.",
        },
        {
            "id": "exit_take_1r_wire",
            "title": "Exit measure: wire take_1r",
            "default": "DENY",
            "detail": "365d DD >2pp vs hold.",
        },
    ]


def _write_status_md(charter: dict[str, Any]) -> None:
    md = render_section(charter)
    OUT_MD.write_text(md + "\n", encoding="utf-8")
    OUT_JSON.write_text(json.dumps(charter, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--start", action="store_true", help="Seed measure-week charter")
    g.add_argument("--refresh", action="store_true", help="Append mid-week snapshot")
    g.add_argument("--evaluate", action="store_true", help="Saturday evaluation")
    g.add_argument("--status", action="store_true", help="Print section from charter")
    ap.add_argument("--force", action="store_true", help="Overwrite charter on --start")
    ap.add_argument("--no-helpers", action="store_true", help="Skip re-running measure scripts")
    args = ap.parse_args(argv)

    if args.start:
        start_week(force=args.force, run_helpers=not args.no_helpers)
        return 0
    if args.refresh:
        refresh(run_helpers=not args.no_helpers)
        return 0
    if args.evaluate:
        evaluate(run_helpers=not args.no_helpers)
        return 0
    # status
    c = _load_charter()
    text = render_section(c)
    print(text)
    if c:
        _write_status_md(c)
    return 0 if c else 1


if __name__ == "__main__":
    raise SystemExit(main())
