"""Safely stop stray backtester.py runs and mark incomplete output files.

Run from stock-bot/:
  python scripts/cancel_backtest.py
  python scripts/cancel_backtest.py --delete-partial
  python scripts/cancel_backtest.py --output backtest_v15_1000day_full_news.txt
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("PYTHONTRADING_ROOT", str(ROOT))

_COMPLETE_MARKERS = ("Total Return:", "Sharpe Ratio:", "Rolling Sharpe:")


def _log(msg: str) -> None:
    print(msg, flush=True)


def _find_backtest_pids() -> list[int]:
    from modules.portal_bot import _find_script_pids

    pids = set(_find_script_pids("backtester.py"))
    # Also catch `python -u backtester.py` launched from parent directory.
    for pid in _find_script_pids("backtester"):
        pids.add(pid)
    alive: list[int] = []
    for pid in sorted(pids):
        try:
            from modules.portal_bot import _process_cmdline, _pid_alive

            if not _pid_alive(pid):
                continue
            cmd = (_process_cmdline(pid) or "").lower()
            if "backtester" not in cmd:
                continue
            alive.append(pid)
        except Exception:
            alive.append(pid)
    return alive


def _stop_pids(pids: list[int]) -> int:
    from modules.portal_bot import _graceful_stop_pid

    stopped = 0
    for pid in pids:
        _log(f"Stopping backtester PID {pid}...")
        ok, msg = _graceful_stop_pid(pid, wait_sec=8.0)
        _log(msg if ok else f"[WARN] {msg}")
        if ok:
            stopped += 1
        time.sleep(0.3)
    return stopped


def _read_output_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


def _is_complete_output(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 200:
        return False
    try:
        text = _read_output_text(path)
    except OSError:
        return False
    return any(marker in text for marker in _COMPLETE_MARKERS)


def _candidate_output_files(extra: list[str] | None = None) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for base in (ROOT, ROOT.parent):
        for pattern in ("backtest*.txt", "backtest*.log", "backtest*.txt.cancelled"):
            for path in base.glob(pattern):
                if path.is_file() and path not in seen:
                    seen.add(path)
                    paths.append(path)
    for raw in extra or []:
        path = Path(raw)
        if not path.is_absolute():
            path = ROOT / path
        if path.is_file() and path not in seen:
            seen.add(path)
            paths.append(path)
    return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)


def _clean_partial_outputs(
    *,
    delete: bool = False,
    extra_paths: list[str] | None = None,
) -> list[str]:
    notes: list[str] = []
    for path in _candidate_output_files(extra_paths):
        if _is_complete_output(path):
            if path.name.endswith(".cancelled"):
                restored = path.with_name(path.name.removesuffix(".cancelled"))
                try:
                    if restored.exists():
                        restored.unlink()
                    path.rename(restored)
                    notes.append(f"Restored complete output -> {restored.name}")
                except OSError as exc:
                    notes.append(f"[WARN] Could not restore {path}: {exc}")
            continue
        if path.name.endswith(".cancelled"):
            continue
        if delete:
            try:
                path.unlink(missing_ok=True)
                notes.append(f"Deleted partial output: {path}")
            except OSError as exc:
                notes.append(f"[WARN] Could not delete {path}: {exc}")
        else:
            cancelled = path.with_suffix(path.suffix + ".cancelled")
            try:
                if cancelled.exists():
                    cancelled.unlink()
                path.rename(cancelled)
                notes.append(f"Renamed partial output -> {cancelled.name}")
            except OSError as exc:
                notes.append(f"[WARN] Could not rename {path}: {exc}")
    return notes


def main() -> int:
    parser = argparse.ArgumentParser(description="Cancel running backtester.py processes")
    parser.add_argument(
        "--delete-partial",
        action="store_true",
        help="Delete incomplete backtest output files (default: rename to .cancelled)",
    )
    parser.add_argument(
        "--output",
        action="append",
        default=[],
        help="Specific output file to treat as partial (repeatable)",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="Skip partial output cleanup",
    )
    args = parser.parse_args()

    print("=" * 60, flush=True)
    print("=== Cancel backtest processes ===", flush=True)
    print(f"Project root: {ROOT}", flush=True)

    pids = _find_backtest_pids()
    if not pids:
        _log("No running backtester.py processes found.")
    else:
        _log(f"Found {len(pids)} backtester process(es): {pids}")
        stopped = _stop_pids(pids)
        _log(f"Stopped {stopped} backtest process(es).")

    if not args.no_clean:
        notes = _clean_partial_outputs(delete=args.delete_partial, extra_paths=args.output)
        if notes:
            for line in notes:
                _log(line)
        else:
            _log("No partial backtest output files to clean.")

    print("=" * 60, flush=True)
    _log("Backtest cancel complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
