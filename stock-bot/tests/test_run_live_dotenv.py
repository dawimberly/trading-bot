"""Live child overlays allowed .env keys only; paper flags never land."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from run_live_bot import (
    _apply_live_env,
    live_env_key_allowed,
    overlay_live_stock_env,
)


class LiveEnvAllowlistTests(unittest.TestCase):
    def test_denylist_blocks_paper_and_hygiene_keys(self):
        self.assertFalse(live_env_key_allowed("PAPER_TRADING"))
        self.assertFalse(live_env_key_allowed("PAPER_NYSE_MAX_ADDS_PER_SYMBOL"))
        self.assertFalse(live_env_key_allowed("PAPER_CHASE_MODE"))
        self.assertFalse(live_env_key_allowed("RESEARCH_FOO"))

    def test_allowlist_live_telegram_apca(self):
        self.assertTrue(live_env_key_allowed("LIVE_VTI_CORE_PCT"))
        self.assertTrue(live_env_key_allowed("ALLOW_LIVE_TRADING"))
        self.assertTrue(live_env_key_allowed("APCA_API_KEY_ID"))
        self.assertTrue(live_env_key_allowed("TELEGRAM_BOT_TOKEN"))
        self.assertFalse(live_env_key_allowed("PAPER_APCA_API_KEY_ID"))


class LiveEnvOverlayTests(unittest.TestCase):
    def test_file_paper_trading_true_does_not_force_paper_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "PAPER_TRADING=true\nLIVE_VTI_CORE_PCT=0\n",
                encoding="utf-8",
            )
            inherited = {
                "PAPER_TRADING": "true",
                "LIVE_VTI_CORE_PCT": "0.85",
                "ALLOW_LIVE_TRADING": "yes",
            }
            over = overlay_live_stock_env(inherited, path)
            self.assertEqual(over["PAPER_TRADING"], "true")  # denylist: file ignored
            live = _apply_live_env(over)
            self.assertEqual(live["PAPER_TRADING"], "false")
            self.assertEqual(live["PAPER_CHASE_MODE"], "0")

    def test_live_vti_file_beats_stale_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("LIVE_VTI_CORE_PCT=0\nPAPER_TRADING=true\n", encoding="utf-8")
            inherited = {
                "LIVE_VTI_CORE_PCT": "0.85",
                "PAPER_TRADING": "true",
            }
            out = overlay_live_stock_env(inherited, path)
            self.assertEqual(out["LIVE_VTI_CORE_PCT"], "0")
            self.assertEqual(out["PAPER_TRADING"], "true")
            self.assertNotIn("PAPER_NYSE_MAX_ADDS_PER_SYMBOL", out)


if __name__ == "__main__":
    unittest.main()
