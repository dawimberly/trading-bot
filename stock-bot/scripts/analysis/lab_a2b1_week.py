"""Saturday Lab 8 tally: A2B1 fires vs fat-loser leak.

Read-only. Never writes .env. Never restarts a bot.

Lab (`alpaca_paper`) is the A2B1 book. Medium and live are checked only
for `nyse_fat_loser_trim` leaks (should stay 0).

Usage (from stock-bot/):
  python scripts/analysis/lab_a2b1_week.py
  python scripts/analysis/lab_a2b1_week.py --days 7
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
_ET = ZoneInfo("America/New_York")
LAB_BOOK = "alpaca_paper"
WATCH_BOOKS = ("alpaca_paper", "alpaca_paper_v2", "alpaca_live")
A2B1_TOKEN = "a2b1_gain_fade"
FAT_TOKEN = "nyse_fat_loser_trim"
OUT_LATEST = ROOT / "data" / "lab_a2b1_week_latest.md"


def _portal_username() -> str:
    import os

    return (os.getenv("PORTAL_USERNAME") or "dawimberly").strip().lower() or "dawimberly"


def _book_journal(book_id: str) -> Path:
    return (
        ROOT
        / "data"
        / "portal"
        / "users"
        / _portal_username()
        / "books"
        / book_id
        / "paper_journal.csv"
    )


def _parse_ts(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00")[:19])
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_ET)
    return dt


def _row_blob(row: dict[str, str]) -> str:
    return " ".join(str(v or "") for v in row.values()).lower()


def _is_fill(row: dict[str, str]) -> bool:
    return (row.get("event") or "").strip().lower() in {"fill", "exit", "sell", "close"}


def _symbol(row: dict[str, str]) -> str:
    return (row.get("symbol") or row.get("ticker") or "").strip().upper()


def _pnl(row: dict[str, str]) -> float | None:
    for key in ("realized_pnl", "pnl"):
        raw = (row.get(key) or "").strip()
        if not raw:
            continue
        try:
            return float(raw)
        except ValueError:
            continue
    return None


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", errors="replace", newline="") as handle:
        return list(csv.DictReader(handle))


def _window_rows(path: Path, cutoff: datetime) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for row in _read_rows(path):
        ts = _parse_ts(row.get("timestamp") or "")
        if ts is None or ts < cutoff:
            continue
        out.append(row)
    return out


def collect_tally(
    *,
    lookback_days: int = 7,
    now: datetime | None = None,
    journals: dict[str, Path] | None = None,
) -> dict[str, Any]:
    as_of = now or datetime.now(_ET)
    cutoff = as_of - timedelta(days=max(1, int(lookback_days)))
    books: dict[str, Any] = {}
    for book_id in WATCH_BOOKS:
        path = (journals or {}).get(book_id) or _book_journal(book_id)
        rows = _window_rows(path, cutoff)
        a2b1: list[dict[str, Any]] = []
        fat: list[dict[str, Any]] = []
        for row in rows:
            if not _is_fill(row):
                continue
            blob = _row_blob(row)
            rec = {
                "ts": (row.get("timestamp") or "").strip(),
                "symbol": _symbol(row),
                "side": (row.get("side") or "").strip().lower(),
                "pnl": _pnl(row),
            }
            if A2B1_TOKEN in blob:
                a2b1.append(rec)
            if FAT_TOKEN in blob:
                fat.append(rec)
        books[book_id] = {
            "journal": str(path),
            "exists": path.is_file(),
            "a2b1": a2b1,
            "fat_loser": fat,
        }
    lab = books[LAB_BOOK]
    fat_leak = {
        book_id: len(payload["fat_loser"])
        for book_id, payload in books.items()
        if payload["fat_loser"]
    }
    return {
        "as_of": as_of.isoformat(timespec="seconds"),
        "lookback_days": int(lookback_days),
        "cutoff": cutoff.isoformat(timespec="seconds"),
        "books": books,
        "lab_a2b1_n": len(lab["a2b1"]),
        "fat_leak": fat_leak,
    }


def _fmt_pnl(val: float | None) -> str:
    if val is None:
        return "n/a"
    return f"{val:+.2f}"


def render_markdown(tally: dict[str, Any]) -> str:
    lab = tally["books"][LAB_BOOK]
    a2b1 = lab["a2b1"]
    leak = tally["fat_leak"]
    lines = [
        "## Lab 8 A2B1 / fat-loser tally",
        "",
        f"_Lab `{LAB_BOOK}` / last {tally['lookback_days']}d / advisory only. "
        "Not Medium SoT. Do not copy to live._",
        "",
        f"| Check | Count | Expect |",
        f"|-------|------:|--------|",
        f"| A2B1 fires (`{A2B1_TOKEN}`) | {tally['lab_a2b1_n']} | >=0 (skip Monday) |",
        f"| Fat-loser on Lab | {len(lab['fat_loser'])} | **0** |",
        f"| Fat-loser on Medium | {len(tally['books']['alpaca_paper_v2']['fat_loser'])} | **0** |",
        f"| Fat-loser on live | {len(tally['books']['alpaca_live']['fat_loser'])} | **0** |",
        "",
    ]
    if a2b1:
        lines.extend(
            [
                "**A2B1 exits (Lab)**",
                "",
                "| Time | Name | Side | Realized $ |",
                "|------|------|------|------------|",
            ]
        )
        for rec in a2b1:
            lines.append(
                f"| {rec['ts']} | {rec['symbol'] or '—'} | {rec['side'] or '—'} | "
                f"{_fmt_pnl(rec['pnl'])} |"
            )
        lines.append("")
    else:
        lines.append(
            "No A2B1 flatten this window. First eligible session is a Tuesday-Friday "
            "open after a >=8% prior close-to-close day."
        )
        lines.append("")
    if leak:
        lines.append("**Fat-loser leak (should be empty):**")
        for book_id, n in leak.items():
            names = ", ".join(
                sorted({r["symbol"] for r in tally["books"][book_id]["fat_loser"] if r["symbol"]})
            ) or "—"
            lines.append(f"- `{book_id}`: {n} trim(s) / {names}")
        lines.append("")
    else:
        lines.append("Fat-loser stays off on all three books this window.")
        lines.append("")
    if leak:
        verdict = "LEAK -- fat-loser fired; do not restore the knob, find the path."
    elif tally["lab_a2b1_n"]:
        verdict = "A2B1 sample on Lab. Keep Medium/live off. Second window still required."
    else:
        verdict = "HOLD -- no A2B1 sample yet; fat-loser clean."
    lines.append(f"**Saturday read:** {verdict}")
    lines.append("")
    return "\n".join(lines)


def write_tally(tally: dict[str, Any], review_date: date | None = None) -> Path:
    day = review_date or date.today()
    out = ROOT / "data" / f"lab_a2b1_week_{day.isoformat()}.md"
    body = (
        f"# Lab 8 A2B1 week - {day.isoformat()}\n\n"
        + render_markdown(tally)
        + "<!-- advisory only; never auto-apply -->\n"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    try:
        shutil.copyfile(out, OUT_LATEST)
    except OSError:
        pass
    return out


def append_to_weekly_review(md: str, *, lookback_days: int = 7) -> str:
    """Embed the tally in a Medium weekly_review body. Fail-soft."""
    try:
        tally = collect_tally(lookback_days=lookback_days)
        write_tally(tally)
        section = render_markdown(tally)
    except Exception as exc:
        section = (
            "## Lab 8 A2B1 / fat-loser tally\n\n"
            f"_Tally failed: {exc}. Run `python scripts/analysis/lab_a2b1_week.py`._\n"
        )
    marker = "<!-- advisory only; never auto-apply -->"
    if marker in md:
        return md.replace(marker, section + "\n" + marker, 1)
    return md.rstrip() + "\n\n" + section + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args(argv)
    tally = collect_tally(lookback_days=max(1, int(args.days)))
    path = write_tally(tally)
    text = render_markdown(tally)
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((text + "\n").encode("utf-8", errors="replace"))
    print(f"Wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
