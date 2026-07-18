"""Filter / normalize malformed rows from paper / chase journal CSVs.

Default targets (first existing wins when --path omitted):
  - paper_chase_journal.csv
  - paper_journal.csv
  - portal book journals under data/portal/users/*/books/*/paper_journal.csv
  - wisdom_journal.csv

Row handling:
  - empty / whitespace-only → drop
  - fewer columns than header (schema drift) → pad with blanks (default)
  - more columns than header → truncate (default) or drop with --drop-wide
  - timestamp present but unparseable → drop when --strict-timestamp
  - event blank → drop when --require-event (default on)

Usage:
  .\\.venv\\Scripts\\python.exe scripts\\maintenance\\cleanup_journal_csv.py --dry-run
  .\\.venv\\Scripts\\python.exe scripts\\maintenance\\cleanup_journal_csv.py --path paper_chase_journal.csv
  .\\.venv\\Scripts\\python.exe scripts\\maintenance\\cleanup_journal_csv.py --all --backup
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _default_journal_paths() -> list[Path]:
    candidates: list[Path] = [
        ROOT / "paper_chase_journal.csv",
        ROOT / "paper_journal.csv",
        ROOT / "wisdom_journal.csv",
        ROOT / "spy_paper_journal.csv",
    ]
    portal = ROOT / "data" / "portal" / "users"
    if portal.is_dir():
        candidates.extend(sorted(portal.glob("*/books/*/paper_journal.csv")))
        candidates.extend(sorted(portal.glob("*/books/*/wisdom_journal.csv")))
    seen: set[Path] = set()
    out: list[Path] = []
    for p in candidates:
        key = p.resolve() if p.exists() else p
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _parse_timestamp(raw: str) -> bool:
    s = (raw or "").strip()
    if not s:
        return False
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d",
    ):
        try:
            datetime.strptime(s[:26], fmt)
            return True
        except ValueError:
            continue
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def normalize_row(row: list[str], width: int) -> list[str]:
    """Pad short rows / truncate long rows to header width (schema-safe)."""
    if len(row) == width:
        return row
    if len(row) < width:
        return row + [""] * (width - len(row))
    return row[:width]


def classify_row(
    row: list[str],
    *,
    width: int,
    header: list[str],
    strict_timestamp: bool,
    require_event: bool,
    drop_wide: bool,
    drop_short: bool,
) -> tuple[str | None, list[str] | None]:
    """Return (drop_reason, normalized_row). drop_reason set → discard."""
    if not row or all(not str(c).strip() for c in row):
        return "empty", None
    if len(row) > width and drop_wide:
        return f"wide_cols:{len(row)}>{width}", None
    if len(row) < width and drop_short:
        return f"short_cols:{len(row)}<{width}", None

    fixed = normalize_row(row, width)
    col_index = {name: i for i, name in enumerate(header)}
    if strict_timestamp and "timestamp" in col_index:
        ts = fixed[col_index["timestamp"]]
        if ts.strip() and not _parse_timestamp(ts):
            return "bad_timestamp", None
    if require_event and "event" in col_index:
        ev = fixed[col_index["event"]]
        if not str(ev).strip():
            return "blank_event", None
    return None, fixed


def cleanup_file(
    path: Path,
    *,
    dry_run: bool,
    backup: bool,
    strict_timestamp: bool,
    require_event: bool,
    drop_wide: bool,
    drop_short: bool,
) -> dict:
    if not path.is_file():
        return {"path": str(path), "status": "missing"}

    with path.open(newline="", encoding="utf-8", errors="replace") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if not header:
            return {"path": str(path), "status": "empty_file"}
        header = [str(c).strip() for c in header]
        width = len(header)
        kept: list[list[str]] = []
        dropped: list[tuple[int, str]] = []
        padded = 0
        truncated = 0
        for lineno, row in enumerate(reader, start=2):
            reason, fixed = classify_row(
                row,
                width=width,
                header=header,
                strict_timestamp=strict_timestamp,
                require_event=require_event,
                drop_wide=drop_wide,
                drop_short=drop_short,
            )
            if reason:
                dropped.append((lineno, reason))
                continue
            assert fixed is not None
            if len(row) < width:
                padded += 1
            elif len(row) > width:
                truncated += 1
            kept.append(fixed)

    result = {
        "path": str(path),
        "status": "ok",
        "header_cols": width,
        "kept": len(kept),
        "dropped": len(dropped),
        "padded": padded,
        "truncated": truncated,
        "drop_reasons": {},
        "dry_run": dry_run,
        "needs_rewrite": bool(dropped or padded or truncated),
    }
    for _ln, reason in dropped:
        key = reason.split(":")[0]
        result["drop_reasons"][key] = result["drop_reasons"].get(key, 0) + 1

    if dry_run or not result["needs_rewrite"]:
        return result

    if backup:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = path.with_suffix(path.suffix + f".bak.{stamp}")
        shutil.copy2(path, bak)
        result["backup"] = str(bak)

    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(kept)
    tmp.replace(path)
    result["status"] = "rewritten"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        action="append",
        type=Path,
        help="Journal CSV path (repeatable). Default: discover common journals.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process every discovered default journal that exists.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report without rewriting (default unless --backup/--write).",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Rewrite cleaned CSV and keep a .bak.<timestamp> copy.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Rewrite in place without backup (prefer --backup).",
    )
    parser.add_argument(
        "--strict-timestamp",
        action="store_true",
        help="Drop rows whose timestamp is non-empty but unparseable.",
    )
    parser.add_argument(
        "--require-event",
        action="store_true",
        default=True,
        help="Drop rows with blank event (default: on).",
    )
    parser.add_argument(
        "--allow-blank-event",
        action="store_true",
        help="Keep rows with blank event.",
    )
    parser.add_argument(
        "--drop-wide",
        action="store_true",
        help="Drop rows with more columns than the header (default: truncate).",
    )
    parser.add_argument(
        "--drop-short",
        action="store_true",
        help="Drop rows with fewer columns than the header (default: pad).",
    )
    args = parser.parse_args()

    require_event = bool(args.require_event) and not args.allow_blank_event
    dry_run = True
    if args.backup or args.write:
        dry_run = False
    if args.dry_run:
        dry_run = True

    if args.path:
        paths = list(args.path)
    else:
        discovered = [p for p in _default_journal_paths() if p.is_file()]
        if args.all or not discovered:
            paths = discovered
        else:
            prefer = [ROOT / "paper_chase_journal.csv", ROOT / "paper_journal.csv"]
            paths = [p for p in prefer if p.is_file()] or discovered[:1]

    if not paths:
        print("No journal CSV found. Pass --path or create paper_chase_journal.csv.")
        return 1

    any_changes = False
    for path in paths:
        if not path.is_absolute():
            path = (ROOT / path).resolve()
        info = cleanup_file(
            path,
            dry_run=dry_run,
            backup=bool(args.backup) and not dry_run,
            strict_timestamp=bool(args.strict_timestamp),
            require_event=require_event,
            drop_wide=bool(args.drop_wide),
            drop_short=bool(args.drop_short),
        )
        status = info.get("status")
        if status == "missing":
            print(f"SKIP missing {path}")
            continue
        dropped = int(info.get("dropped") or 0)
        kept = int(info.get("kept") or 0)
        padded = int(info.get("padded") or 0)
        truncated = int(info.get("truncated") or 0)
        reasons = info.get("drop_reasons") or {}
        mode = "DRY-RUN" if info.get("dry_run") else status
        print(f"[{mode}] {path}")
        print(
            f"  kept={kept} dropped={dropped} padded={padded} "
            f"truncated={truncated} drop_reasons={reasons or '-'}"
        )
        if info.get("backup"):
            print(f"  backup={info['backup']}")
        if info.get("needs_rewrite"):
            any_changes = True

    if dry_run and any_changes:
        print("\nRe-run with --backup to rewrite (keeps timestamped .bak).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
