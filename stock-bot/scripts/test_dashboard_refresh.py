"""Quick smoke test for dashboard refresh snapshot (no GUI)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard_app import _collect_refresh_snapshot  # noqa: E402


def main() -> int:
    user = sys.argv[1] if len(sys.argv) > 1 else "owner"
    book = sys.argv[2] if len(sys.argv) > 2 else "alpaca_paper"
    for fast in (True, False):
        label = "fast" if fast else "full"
        t0 = time.perf_counter()
        snap = _collect_refresh_snapshot(user, book, fast=fast)
        elapsed = time.perf_counter() - t0
        errs = snap.get("partial_errors") or []
        print(
            f"{label}: {elapsed:.2f}s | equity={snap.get('equity')} | "
            f"errors={len(errs)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
