"""Filter / normalize malformed rows from paper / chase / wisdom journal CSVs.

Discovers journals via config + runtime_paths (and portal book dirs):
  - paper_chase_journal.csv (PAPER_CHASE_JOURNAL)
  - paper_journal.csv (PAPER_JOURNAL_CSV)
  - wisdom_journal.csv (WISDOM_JOURNAL_FILE)
  - spy_paper_journal.csv
  - data/portal/users/*/books/*/{paper,wisdom}_journal.csv

Row handling:
  - empty / whitespace-only → drop
  - fewer columns than header (schema drift) → pad with blanks (default)
  - more columns than header → truncate (default) or drop with --drop-wide
  - timestamp present but unparseable → drop when --strict-timestamp
  - event blank → drop when --require-event (default on; skipped if no event col)
  - equity present + parseable → keep only when in [$10k, $200k] (default band);
    blank equity kept; unparseable equity dropped as equity_bad
  - live book paths (alpaca_live / books/.../live) skip the equity band automatically

Usage (from stock-bot/, prefer repo .venv):
  ..\\.venv\\Scripts\\python.exe scripts\\maintenance\\cleanup_journal_csv.py --dry-run
  ..\\.venv\\Scripts\\python.exe scripts\\maintenance\\cleanup_journal_csv.py --all --dry-run
  ..\\.venv\\Scripts\\python.exe scripts\\maintenance\\cleanup_journal_csv.py --all --backup
  ..\\.venv\\Scripts\\python.exe scripts\\maintenance\\cleanup_journal_csv.py --path paper_journal.csv --backup
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Sane paper / research book equity band (USD).
DEFAULT_EQUITY_MIN = 10_000.0
DEFAULT_EQUITY_MAX = 200_000.0

_EQUITY_COL_NAMES = ("equity", "Equity", "account_equity", "portfolio_equity")


def _resolve_root() -> Path:
    try:
        from modules.runtime_paths import resolve_runtime_root

        return resolve_runtime_root(ROOT)
    except Exception:
        return ROOT


def _default_journal_paths() -> list[Path]:
    """Discover paper/chase/wisdom journals via config + runtime layout."""
    root = _resolve_root()
    data_root = root
    try:
        from modules.runtime_paths import resolve_data_root

        data_root = resolve_data_root(root)
    except Exception:
        pass

    chase = "paper_chase_journal.csv"
    paper = "paper_journal.csv"
    wisdom = "wisdom_journal.csv"
    spy = "spy_paper_journal.csv"
    try:
        import config as cfg
        import os

        chase = os.getenv("PAPER_CHASE_JOURNAL", "paper_chase_journal.csv")
        paper = getattr(cfg, "PAPER_JOURNAL_CSV", paper) or paper
        wisdom = getattr(cfg, "WISDOM_JOURNAL_FILE", wisdom) or wisdom
        spy = getattr(cfg, "SPY_PAPER_JOURNAL_CSV", spy) or spy
    except Exception:
        pass

    candidates: list[Path] = [
        root / chase,
        data_root / chase,
        root / paper,
        data_root / paper,
        root / wisdom,
        data_root / wisdom,
        root / spy,
        data_root / spy,
    ]
    for base in (root, data_root):
        portal = base / "data" / "portal" / "users"
        if portal.is_dir():
            candidates.extend(sorted(portal.glob("*/books/*/paper_journal.csv")))
            candidates.extend(sorted(portal.glob("*/books/*/wisdom_journal.csv")))
            candidates.extend(sorted(portal.glob("*/books/*/paper_chase_journal.csv")))

    seen: set[Path] = set()
    out: list[Path] = []
    for p in candidates:
        try:
            key = p.resolve() if p.exists() else p
        except OSError:
            key = p
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


def _parse_equity(raw: str) -> float | None:
    """Parse equity cell; None if blank, raises ValueError if garbage."""
    s = (raw or "").strip()
    if not s:
        return None
    cleaned = s.replace(",", "").replace("$", "").replace(" ", "")
    if not cleaned or cleaned.lower() in ("nan", "none", "null", "-"):
        return None
    # Reject obvious non-numeric junk early
    if not re.match(r"^[+-]?\d+(\.\d+)?([eE][+-]?\d+)?$", cleaned):
        raise ValueError(f"bad equity: {raw!r}")
    return float(cleaned)


def _equity_col_index(header: list[str]) -> int | None:
    lower_map = {name.strip().lower(): i for i, name in enumerate(header)}
    for name in _EQUITY_COL_NAMES:
        idx = lower_map.get(name.lower())
        if idx is not None:
            return idx
    return None


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
    equity_min: float | None,
    equity_max: float | None,
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

    eq_idx = _equity_col_index(header)
    if eq_idx is not None and (equity_min is not None or equity_max is not None):
        try:
            equity = _parse_equity(fixed[eq_idx] if eq_idx < len(fixed) else "")
        except ValueError:
            return "equity_bad", None
        if equity is not None:
            lo = equity_min if equity_min is not None else float("-inf")
            hi = equity_max if equity_max is not None else float("inf")
            if equity < lo or equity > hi:
                return f"equity_out_of_band:{equity:.2f}", None

    return None, fixed


def _is_live_book_path(path: Path) -> bool:
    """Portal/live books often have <$10k equity — do not apply paper equity band."""
    parts = {p.lower() for p in path.parts}
    name = path.name.lower()
    if "alpaca_live" in parts or "live" in parts and "books" in parts:
        return True
    if name.startswith("live_") or "_live_" in name:
        return True
    return False


def cleanup_file(
    path: Path,
    *,
    dry_run: bool,
    backup: bool,
    strict_timestamp: bool,
    require_event: bool,
    drop_wide: bool,
    drop_short: bool,
    equity_min: float | None,
    equity_max: float | None,
) -> dict:
    if not path.is_file():
        return {"path": str(path), "status": "missing"}

    # Paper $10k–$200k band is wrong for live small accounts.
    if _is_live_book_path(path) and (equity_min is not None or equity_max is not None):
        equity_min, equity_max = None, None

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
                equity_min=equity_min,
                equity_max=equity_max,
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
        "has_equity_col": _equity_col_index(header) is not None,
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
    try:
        tmp.replace(path)
    except PermissionError:
        # Windows: destination may be locked by a running bot — copy over then remove tmp.
        try:
            shutil.copy2(tmp, path)
            tmp.unlink(missing_ok=True)
        except PermissionError:
            result["status"] = "locked"
            result["error"] = f"Permission denied rewriting {path} (is the bot running?)"
            result["tmp"] = str(tmp)
            return result
    result["status"] = "rewritten"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
        help="Drop rows with blank event when event column exists (default: on).",
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
    parser.add_argument(
        "--equity-min",
        type=float,
        default=DEFAULT_EQUITY_MIN,
        help=f"Drop parseable equity below this USD (default {DEFAULT_EQUITY_MIN:.0f}).",
    )
    parser.add_argument(
        "--equity-max",
        type=float,
        default=DEFAULT_EQUITY_MAX,
        help=f"Drop parseable equity above this USD (default {DEFAULT_EQUITY_MAX:.0f}).",
    )
    parser.add_argument(
        "--no-equity-filter",
        action="store_true",
        help="Disable the equity band filter.",
    )
    args = parser.parse_args()

    require_event = bool(args.require_event) and not args.allow_blank_event
    dry_run = True
    if args.backup or args.write:
        dry_run = False
    if args.dry_run:
        dry_run = True

    equity_min: float | None = None if args.no_equity_filter else float(args.equity_min)
    equity_max: float | None = None if args.no_equity_filter else float(args.equity_max)

    root = _resolve_root()
    if args.path:
        paths = list(args.path)
    else:
        discovered = [p for p in _default_journal_paths() if p.is_file()]
        if args.all or not discovered:
            paths = discovered
        else:
            prefer = [
                root / "paper_chase_journal.csv",
                root / "paper_journal.csv",
                root / "wisdom_journal.csv",
            ]
            paths = [p for p in prefer if p.is_file()] or discovered[:1]

    if not paths:
        print("No journal CSV found. Pass --path or create paper_chase_journal.csv.")
        return 1

    any_changes = False
    had_lock_error = False
    for path in paths:
        if not path.is_absolute():
            path = (root / path).resolve()
        info = cleanup_file(
            path,
            dry_run=dry_run,
            backup=bool(args.backup) and not dry_run,
            strict_timestamp=bool(args.strict_timestamp),
            require_event=require_event,
            drop_wide=bool(args.drop_wide),
            drop_short=bool(args.drop_short),
            equity_min=equity_min,
            equity_max=equity_max,
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
        applied_band = (
            equity_min is not None
            and equity_max is not None
            and not _is_live_book_path(path)
        )
        if info.get("has_equity_col"):
            if applied_band:
                print(f"  equity_band=${equity_min:,.0f}-${equity_max:,.0f}")
            elif _is_live_book_path(path) and not args.no_equity_filter:
                print("  equity_band=skipped (live book path)")
        if info.get("backup"):
            print(f"  backup={info['backup']}")
        if status == "locked":
            had_lock_error = True
            print(f"  ERROR: {info.get('error')}")
            if info.get("tmp"):
                print(f"  cleaned tmp left at: {info['tmp']}")
        if info.get("needs_rewrite"):
            any_changes = True

    if dry_run and any_changes:
        print("\nRe-run with --backup to rewrite (keeps timestamped .bak).")
    return 1 if had_lock_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
