"""Run the crypto vol mean-reversion sleeve on the isolated PAPER_APCA book.

Examples:
  python scripts/research/run_crypto_vol_piece.py
  python scripts/research/run_crypto_vol_piece.py --apply

Dry-run (default): evaluates signals and logs intents; no orders unless --apply.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modules.crypto_vol_sleeve import (
    crypto_vol_paper_available,
    run_crypto_vol_sleeve_cycle,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run crypto vol sleeve (paper-only, PAPER_APCA_* book)"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Place orders (default: dry-run / signal scan only)",
    )
    args = parser.parse_args()

    if not crypto_vol_paper_available():
        raise SystemExit(
            "Needs PAPER_APCA_API_KEY_ID / PAPER_APCA_API_SECRET_KEY in .env"
        )

    mode = "apply" if args.apply else "dry-run"
    print(f"Crypto vol sleeve | mode={mode}")
    result = run_crypto_vol_sleeve_cycle(dry_run=not args.apply)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
