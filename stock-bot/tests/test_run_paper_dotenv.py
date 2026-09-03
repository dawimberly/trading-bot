"""Paper child loads stock-bot/.env with override=True; inherited parent env loses."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from run_paper_bot import paper_env_file_overlay


class PaperEnvOverlayTests(unittest.TestCase):
    def test_inherited_max_adds_1_file_2_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("PAPER_NYSE_MAX_ADDS_PER_SYMBOL=2\n", encoding="utf-8")
            env = {"PAPER_NYSE_MAX_ADDS_PER_SYMBOL": "1", "KEEP": "x"}
            out = paper_env_file_overlay(env, path)
            self.assertEqual(out["PAPER_NYSE_MAX_ADDS_PER_SYMBOL"], "2")
            self.assertEqual(out["KEEP"], "x")
            self.assertEqual(env["PAPER_NYSE_MAX_ADDS_PER_SYMBOL"], "1")

    def test_missing_file_keeps_inherited(self):
        env = {"PAPER_NYSE_MAX_ADDS_PER_SYMBOL": "1"}
        out = paper_env_file_overlay(env, Path("/no/such/.env"))
        self.assertEqual(out["PAPER_NYSE_MAX_ADDS_PER_SYMBOL"], "1")

    def test_portal_book_credentials_not_overwritten_by_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp) / "book.env"
            root = Path(tmp) / "root.env"
            book.write_text(
                "APCA_API_KEY_ID=BOOK_KEY\nAPCA_API_SECRET_KEY=BOOK_SECRET\n",
                encoding="utf-8",
            )
            root.write_text(
                "PAPER_APCA_API_KEY_ID=ROOT_KEY\n"
                "PAPER_APCA_API_SECRET_KEY=ROOT_SECRET\n"
                "PAPER_NYSE_MAX_ADDS_PER_SYMBOL=2\n",
                encoding="utf-8",
            )
            env = {
                "PORTAL_MANAGED_BOT": "1",
                "PYTHONTRADING_ENV_FILE": str(book),
                "APCA_API_KEY_ID": "BOOK_KEY",
                "APCA_API_SECRET_KEY": "BOOK_SECRET",
            }
            out = paper_env_file_overlay(env, root)
            self.assertEqual(out["APCA_API_KEY_ID"], "BOOK_KEY")
            self.assertNotIn("PAPER_APCA_API_KEY_ID", out)
            self.assertEqual(out["PAPER_NYSE_MAX_ADDS_PER_SYMBOL"], "2")

    def test_portal_book_strategy_keys_not_clobbered_by_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp) / "book.env"
            root = Path(tmp) / "root.env"
            book.write_text(
                "APCA_API_KEY_ID=BOOK_KEY\n"
                "APCA_API_SECRET_KEY=BOOK_SECRET\n"
                "PAPER_NYSE_SLEEVE_CAP_PCT=0.67\n"
                "PAPER_NYSE_PER_NAME_MAX_PCT=0.10\n"
                "ORB_MOMENTUM_ENABLED=false\n",
                encoding="utf-8",
            )
            root.write_text(
                "PAPER_NYSE_SLEEVE_CAP_PCT=0.90\n"
                "PAPER_NYSE_PER_NAME_MAX_PCT=0.08\n"
                "ORB_MOMENTUM_ENABLED=true\n"
                "PAPER_MAX_ADDS=9\n",
                encoding="utf-8",
            )
            env = {
                "PORTAL_MANAGED_BOT": "1",
                "PYTHONTRADING_ENV_FILE": str(book),
                "APCA_API_KEY_ID": "BOOK_KEY",
                "APCA_API_SECRET_KEY": "BOOK_SECRET",
                "PAPER_NYSE_SLEEVE_CAP_PCT": "0.67",
                "PAPER_NYSE_PER_NAME_MAX_PCT": "0.10",
                "ORB_MOMENTUM_ENABLED": "false",
            }
            out = paper_env_file_overlay(env, root)
            self.assertEqual(out["PAPER_NYSE_SLEEVE_CAP_PCT"], "0.67")
            self.assertEqual(out["PAPER_NYSE_PER_NAME_MAX_PCT"], "0.10")
            self.assertEqual(out["ORB_MOMENTUM_ENABLED"], "false")
            self.assertEqual(out["PAPER_MAX_ADDS"], "9")


if __name__ == "__main__":
    unittest.main()

