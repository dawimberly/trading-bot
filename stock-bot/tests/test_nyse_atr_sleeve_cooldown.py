"""Paper ATR-stop sleeve cooldown: no new NYSE names rest of session."""

from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace

import config
from modules import pipeline_strategies as ps


class NyseAtrSleeveCooldownTests(unittest.TestCase):
    def _paper(self):
        self._saved = {
            "PAPER_TRADING": config.PAPER_TRADING,
            "PAPER_NYSE_ENTRY_HYGIENE_ENABLED": config.PAPER_NYSE_ENTRY_HYGIENE_ENABLED,
            "PAPER_NYSE_ATR_STOP_SLEEVE_COOLDOWN": config.PAPER_NYSE_ATR_STOP_SLEEVE_COOLDOWN,
        }
        config.PAPER_TRADING = True
        config.PAPER_NYSE_ENTRY_HYGIENE_ENABLED = True
        config.PAPER_NYSE_ATR_STOP_SLEEVE_COOLDOWN = True
        self._pac = config.paper_aggressive_context
        self._pcm = config.paper_chase_mode_enabled
        self._pos = config.paper_only_sleeves_active
        config.paper_aggressive_context = lambda: True  # type: ignore
        config.paper_chase_mode_enabled = lambda: True  # type: ignore
        config.paper_only_sleeves_active = lambda: True  # type: ignore
        ps.reset_nyse_entry_hygiene_state()

    def tearDown(self):
        if getattr(self, "_saved", None):
            config.PAPER_TRADING = self._saved["PAPER_TRADING"]
            config.PAPER_NYSE_ENTRY_HYGIENE_ENABLED = self._saved[
                "PAPER_NYSE_ENTRY_HYGIENE_ENABLED"
            ]
            config.PAPER_NYSE_ATR_STOP_SLEEVE_COOLDOWN = self._saved[
                "PAPER_NYSE_ATR_STOP_SLEEVE_COOLDOWN"
            ]
            config.paper_aggressive_context = self._pac
            config.paper_chase_mode_enabled = self._pcm
            config.paper_only_sleeves_active = self._pos
        ps.reset_nyse_entry_hygiene_state()

    def test_atr_stop_blocks_new_name_not_held(self):
        self._paper()
        now = datetime(2026, 8, 19, 11, 0, 0)
        execu = SimpleNamespace(portfolio=SimpleNamespace(positions={}))
        self.assertIsNone(ps._nyse_entry_hygiene_skip(execu, "TXG", now=now))
        ps.mark_nyse_atr_stop_from_exit(
            "PATH", reason="smart_atr_stop", sleeve="NYSE", now=now
        )
        reason = ps._nyse_entry_hygiene_skip(execu, "TXG", now=now)
        self.assertIsNotNone(reason)
        self.assertIn("ATR-stop sleeve", reason or "")
        self.assertGreaterEqual(ps.get_nyse_hygiene_skip_counts()["hygiene_atr_sleeve"], 1)

    def test_atr_stop_allows_add_to_held_name(self):
        self._paper()
        now = datetime(2026, 8, 19, 11, 0, 0)
        execu = SimpleNamespace(portfolio=SimpleNamespace(positions={"NTRA": 10}))
        ps.mark_nyse_atr_stop_from_exit(
            "PATH", reason="smart_atr_stop", sleeve="NYSE", now=now
        )
        self.assertIsNone(ps._nyse_entry_hygiene_skip(execu, "NTRA", now=now))

    def test_atr_stop_clears_next_session(self):
        self._paper()
        day1 = datetime(2026, 8, 19, 15, 0, 0)
        day2 = datetime(2026, 8, 20, 10, 0, 0)
        execu = SimpleNamespace(portfolio=SimpleNamespace(positions={}))
        ps.mark_nyse_atr_stop_from_exit(
            "TWST", reason="smart_hard_exit", sleeve="NYSE", now=day1
        )
        self.assertIsNotNone(ps._nyse_entry_hygiene_skip(execu, "TXG", now=day1))
        self.assertIsNone(ps._nyse_entry_hygiene_skip(execu, "TXG", now=day2))

    def test_partial_reduce_does_not_arm_sleeve_cooldown(self):
        self._paper()
        now = datetime(2026, 8, 19, 11, 0, 0)
        execu = SimpleNamespace(portfolio=SimpleNamespace(positions={}))
        ps.mark_nyse_atr_stop_from_exit(
            "PATH", reason="smart_size_reduce", sleeve="NYSE", now=now
        )
        self.assertIsNone(ps._nyse_entry_hygiene_skip(execu, "TXG", now=now))

    def test_live_profile_a_does_not_arm_sleeve_cooldown(self):
        config.PAPER_TRADING = False
        config.paper_aggressive_context = lambda: False  # type: ignore
        config.paper_chase_mode_enabled = lambda: False  # type: ignore
        config.paper_only_sleeves_active = lambda: False  # type: ignore
        config.PAPER_NYSE_ATR_STOP_SLEEVE_COOLDOWN = True
        ps.reset_nyse_entry_hygiene_state()
        now = datetime(2026, 8, 19, 11, 0, 0)
        execu = SimpleNamespace(portfolio=SimpleNamespace(positions={}))
        ps.mark_nyse_atr_stop_from_exit(
            "PATH", reason="smart_atr_stop", sleeve="NYSE", now=now
        )
        self.assertIsNone(ps._nyse_entry_hygiene_skip(execu, "TXG", now=now))


if __name__ == "__main__":
    unittest.main()
