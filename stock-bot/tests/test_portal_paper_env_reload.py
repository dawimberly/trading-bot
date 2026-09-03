"""Paper Restart child env overlays stock-bot/.env; live spawn unchanged."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules import portal_bot


class OverlayDotenvTests(unittest.TestCase):
    def test_file_overrides_inherited_parent_max_adds(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "PAPER_NYSE_MAX_ADDS_PER_SYMBOL=2\nVTI_CORE_PCT=0\n",
                encoding="utf-8",
            )
            env = {
                "PAPER_NYSE_MAX_ADDS_PER_SYMBOL": "1",
                "KEEP": "x",
            }
            out = portal_bot._overlay_dotenv_file(env, path)
            self.assertEqual(out["PAPER_NYSE_MAX_ADDS_PER_SYMBOL"], "2")
            self.assertEqual(out["VTI_CORE_PCT"], "0")
            self.assertEqual(out["KEEP"], "x")
            self.assertEqual(env["PAPER_NYSE_MAX_ADDS_PER_SYMBOL"], "1")

    def test_missing_file_leaves_env(self):
        env = {"PAPER_NYSE_MAX_ADDS_PER_SYMBOL": "1"}
        out = portal_bot._overlay_dotenv_file(env, Path("/no/such/.env"))
        self.assertEqual(out["PAPER_NYSE_MAX_ADDS_PER_SYMBOL"], "1")

    def test_root_overlay_skips_credentials_when_portal_book_has_keys(self):
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
            out = portal_bot._overlay_dotenv_file(
                env, root, book_env_file=str(book)
            )
            self.assertEqual(out["APCA_API_KEY_ID"], "BOOK_KEY")
            self.assertNotIn("PAPER_APCA_API_KEY_ID", out)
            self.assertEqual(out["PAPER_NYSE_MAX_ADDS_PER_SYMBOL"], "2")

    def test_sanitize_drops_research_keys_for_portal_book(self):
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
                "PAPER_APCA_API_KEY_ID": "ROOT_KEY",
                "PAPER_APCA_API_SECRET_KEY": "ROOT_SECRET",
                "PAPER_CHASE_USE_RESEARCH_KEYS": "yes",
            }
            out = portal_bot.sanitize_portal_book_credentials(env)
            self.assertEqual(out["APCA_API_KEY_ID"], "BOOK_KEY")
            self.assertNotIn("PAPER_APCA_API_KEY_ID", out)
            self.assertNotIn("PAPER_CHASE_USE_RESEARCH_KEYS", out)
            self.assertEqual(out["APCA_API_SECRET_KEY"], "BOOK_SECRET")


class RestartBotPaperTests(unittest.TestCase):
    def test_restart_stops_when_already_running_then_starts(self):
        calls: list[str] = []

        def _running(_user, _book="alpaca_paper"):
            return calls.count("stop") == 0

        def _stop(_user, _book="alpaca_paper"):
            calls.append("stop")
            return True, "stopped"

        def _start(_user, _book="alpaca_paper", *, skip_orphan_stop=False):
            calls.append("start")
            return True, "started"

        with (
            patch.object(portal_bot, "bot_running", side_effect=_running),
            patch.object(portal_bot, "stop_bot", side_effect=_stop),
            patch.object(portal_bot, "start_bot", side_effect=_start),
            patch.object(portal_bot, "_is_paper_book", return_value=True),
        ):
            ok, msg = portal_bot.restart_bot("dawimberly", "alpaca_paper")
        self.assertTrue(ok)
        self.assertEqual(calls, ["stop", "start"])
        self.assertIn("restarted", msg.lower())

    def test_restart_retries_start_if_already_running(self):
        starts = {"n": 0}

        def _start(_user, _book="alpaca_paper", *, skip_orphan_stop=False):
            starts["n"] += 1
            if starts["n"] == 1:
                return False, "Bot is already running for alpaca_paper."
            return True, "started"

        with (
            patch.object(portal_bot, "bot_running", return_value=False),
            patch.object(portal_bot, "stop_bot", return_value=(True, "stopped")),
            patch.object(portal_bot, "start_bot", side_effect=_start),
            patch.object(portal_bot, "_is_paper_book", return_value=True),
        ):
            ok, msg = portal_bot.restart_bot("dawimberly", "alpaca_paper")
        self.assertTrue(ok)
        self.assertEqual(starts["n"], 2)
        self.assertIn("started", msg.lower())

    def test_live_restart_does_not_use_paper_already_running_retry(self):
        starts = {"n": 0}

        def _start(_user, _book="alpaca_live", *, skip_orphan_stop=False):
            starts["n"] += 1
            return False, "Bot is already running for alpaca_live."

        with (
            patch.object(portal_bot, "bot_running", return_value=False),
            patch.object(portal_bot, "stop_bot", return_value=(True, "stopped")),
            patch.object(portal_bot, "start_bot", side_effect=_start),
            patch.object(portal_bot, "_is_paper_book", return_value=False),
        ):
            ok, msg = portal_bot.restart_bot("dawimberly", "alpaca_live")
        self.assertFalse(ok)
        self.assertEqual(starts["n"], 1)
        self.assertIn("already running", msg.lower())


if __name__ == "__main__":
    unittest.main()
