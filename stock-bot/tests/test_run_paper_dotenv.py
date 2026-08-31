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


if __name__ == "__main__":
    unittest.main()
