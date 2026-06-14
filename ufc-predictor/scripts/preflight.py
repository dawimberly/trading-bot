#!/usr/bin/env python3
"""CLI wrapper for pre-flight checklist."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_REPO = _ROOT.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_REPO) not in sys.path:
    sys.path.insert(1, str(_REPO))

from src.preflight import run_preflight
from src.safe_io import install_safe_stdout


def main() -> int:
    install_safe_stdout()
    parser = argparse.ArgumentParser(description="UFC predictor pre-flight checklist")
    parser.add_argument("--profile", choices=["live", "research"], help="Override UFC_PROFILE")
    args = parser.parse_args()
    return run_preflight(profile=args.profile)


if __name__ == "__main__":
    raise SystemExit(main())
