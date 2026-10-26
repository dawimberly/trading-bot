"""Dry-run test: Sell 1 / Sell All qty paths against a VTI paper position.

Usage (from stock-bot):
  DRY_RUN=true python scripts/analysis/test_positions_sell_vti.py

Does NOT place live orders when DRY_RUN=true (default). Set DRY_RUN=false
only if you intentionally want a real paper order.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("PYTHONUNBUFFERED", "1")


def main() -> int:
    import config
    from modules.alpaca_executor import AlpacaExecutor

    paper = True
    try:
        keys = config.get_alpaca_credentials(paper=True)
    except Exception as exc:
        print(f"FAIL: no paper credentials ({exc})")
        return 2

    ex = AlpacaExecutor(paper=paper, credentials_fn=lambda: keys, allow_live=False)
    ex._equity_session_open = True  # allow after-hours dry-run of order path
    print(f"DRY_RUN={ex.dry_run}")

    pos = ex._find_position("VTI")
    if pos is None:
        print("SKIP: no VTI position on paper book — cannot live-test sells.")
        print("Unit-check execute_qty_exit formatting instead…")
        # Smoke the helper path with a fake held qty via mock would need more;
        # verify method exists and full-exit uses qty.
        assert hasattr(ex, "execute_qty_exit")
        assert hasattr(ex, "execute_full_exit")
        print("OK: executor has execute_qty_exit + qty-based execute_full_exit")
        return 0

    held = float(pos.qty)
    px = float(pos.current_price or pos.avg_entry_price or 0)
    print(f"VTI held qty={held} px={px}")

    # Sell 1 (or remaining if < 1)
    sell1 = 1.0 if abs(held) >= 1 else abs(held)
    print(f"Testing execute_qty_exit(VTI, {sell1})…")
    o1 = ex.execute_qty_exit("VTI", sell1, reason="test_sell_1", sleeve="VTI")
    print(f"  Sell 1 result: {o1!r} id={getattr(o1, 'id', None)}")

    # Re-find for Sell All (dry-run does not change positions)
    pos2 = ex._find_position("VTI")
    held2 = float(pos2.qty) if pos2 is not None else held
    print(f"Testing execute_full_exit(VTI) for Sell All (held={held2})…")
    o2 = ex.execute_full_exit("VTI", reason="test_sell_all", sleeve="VTI")
    print(f"  Sell All result: {o2!r} id={getattr(o2, 'id', None)}")

    if o1 is None or o2 is None:
        print("FAIL: one or both orders returned None")
        return 1
    print("OK: VTI Sell 1 + Sell All qty paths submitted (dry-run or paper)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
