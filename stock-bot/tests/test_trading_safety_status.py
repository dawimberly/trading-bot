"""Daily loss status / auto-clear tests.

Run: python tests/test_trading_safety_status.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from modules import trading_safety as ts


def run_tests() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "trading_safety_state.json"
        orig_file = config.TRADING_SAFETY_STATE_FILE
        config.TRADING_SAFETY_STATE_FILE = str(state_path)
        try:
            today = date.today().isoformat()
            state_path.write_text(
                json.dumps(
                    {
                        "live": {
                            "daily_equity_date": today,
                            "daily_equity_open": 300.0,
                            "circuit_tripped": True,
                            "loss_pct": 0.997,
                        }
                    }
                ),
                encoding="utf-8",
            )
            status = ts.get_daily_loss_status(paper=False, current_equity=298.0)
            assert not status["tripped"], f"false trip should clear: {status}"
            assert status["loss_pct"] is not None
            assert abs(float(status["loss_pct"])) < 5.0, status
            assert float(status["open_equity"] or 0) < 500, status

            contaminated = Path(tmp) / "contaminated.json"
            contaminated.write_text(
                json.dumps(
                    {
                        "live": {
                            "daily_equity_date": today,
                            "daily_equity_open": 98058.94,
                            "circuit_tripped": True,
                            "loss_pct": 0.996953,
                        }
                    }
                ),
                encoding="utf-8",
            )
            config.TRADING_SAFETY_STATE_FILE = str(contaminated)
            fixed = ts.get_daily_loss_status(paper=False, current_equity=298.74)
            assert not fixed["tripped"], fixed
            assert float(fixed["open_equity"]) == 298.74, fixed

            tripped, reason, ratio = ts.daily_loss_circuit_tripped(298.0, paper=False)
            assert not tripped, (tripped, reason, ratio)

            status2 = ts.get_daily_loss_status(paper=False, current_equity=290.0)
            assert status2["loss_pct"] is not None
            assert float(status2["loss_pct"]) > 0

            _check_paper_books_do_not_share_anchor(tmp, today)

            print("test_trading_safety_status: all passed")
        finally:
            config.TRADING_SAFETY_STATE_FILE = orig_file


def _check_paper_books_do_not_share_anchor(tmp: str, today: str) -> None:
    """Lab must not measure its equity against Medium's session open.

    Both are paper books; a shared "paper" key made Lab (~93.6k) look 6.4% down
    against Medium's ~100k open and tripped the 4% breaker all day.
    """
    import os

    shared = Path(tmp) / "two_paper_books.json"
    shared.write_text(
        json.dumps(
            {
                # Written by Medium under the old shared key.
                "paper": {
                    "daily_equity_date": today,
                    "daily_equity_open": 100032.54,
                }
            }
        ),
        encoding="utf-8",
    )
    config.TRADING_SAFETY_STATE_FILE = str(shared)
    prev_book = os.environ.get("TRADING_BOOK_ID")
    os.environ["TRADING_BOOK_ID"] = "alpaca_paper"
    try:
        tripped, reason, ratio = ts.daily_loss_circuit_tripped(93662.31, paper=True)
        assert not tripped, f"Lab tripped on Medium's anchor: {reason} {ratio}"
    finally:
        if prev_book is None:
            os.environ.pop("TRADING_BOOK_ID", None)
        else:
            os.environ["TRADING_BOOK_ID"] = prev_book


if __name__ == "__main__":
    run_tests()
