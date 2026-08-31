"""Paper NYSE max-adds: 2 fills allowed while open; add 3 skipped. Live untouched."""

from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace

import config
from modules import pipeline_strategies as ps


class NyseMaxAddsTests(unittest.TestCase):
    def _paper(self, *, max_adds: int = 2):
        self._saved = {
            "PAPER_TRADING": config.PAPER_TRADING,
            "PAPER_NYSE_ENTRY_HYGIENE_ENABLED": config.PAPER_NYSE_ENTRY_HYGIENE_ENABLED,
            "PAPER_NYSE_MAX_ADDS_PER_SYMBOL": config.PAPER_NYSE_MAX_ADDS_PER_SYMBOL,
            "PAPER_NYSE_SAME_DAY_REENTRY_BLOCK": config.PAPER_NYSE_SAME_DAY_REENTRY_BLOCK,
            "PAPER_NYSE_ATR_STOP_SLEEVE_COOLDOWN": config.PAPER_NYSE_ATR_STOP_SLEEVE_COOLDOWN,
        }
        self._pac = config.paper_aggressive_context
        self._pcm = config.paper_chase_mode_enabled
        self._pos = config.paper_only_sleeves_active
        self._sold = ps._nyse_journal_sold_today
        self._buys = ps._nyse_journal_buys_since_flat
        config.PAPER_TRADING = True
        config.PAPER_NYSE_ENTRY_HYGIENE_ENABLED = True
        config.PAPER_NYSE_MAX_ADDS_PER_SYMBOL = max_adds
        config.PAPER_NYSE_SAME_DAY_REENTRY_BLOCK = True
        config.PAPER_NYSE_ATR_STOP_SLEEVE_COOLDOWN = True
        config.paper_aggressive_context = lambda: True  # type: ignore
        config.paper_chase_mode_enabled = lambda: True  # type: ignore
        config.paper_only_sleeves_active = lambda: True  # type: ignore
        ps._nyse_journal_sold_today = ps._nyse_session_sold_today  # type: ignore
        ps._nyse_journal_buys_since_flat = lambda _sym: None  # type: ignore
        ps.reset_nyse_entry_hygiene_state()

    def _live(self):
        self._saved = {
            "PAPER_TRADING": config.PAPER_TRADING,
            "PAPER_NYSE_ENTRY_HYGIENE_ENABLED": config.PAPER_NYSE_ENTRY_HYGIENE_ENABLED,
            "PAPER_NYSE_MAX_ADDS_PER_SYMBOL": config.PAPER_NYSE_MAX_ADDS_PER_SYMBOL,
            "PAPER_NYSE_SAME_DAY_REENTRY_BLOCK": config.PAPER_NYSE_SAME_DAY_REENTRY_BLOCK,
            "PAPER_NYSE_ATR_STOP_SLEEVE_COOLDOWN": config.PAPER_NYSE_ATR_STOP_SLEEVE_COOLDOWN,
        }
        self._pac = config.paper_aggressive_context
        self._pcm = config.paper_chase_mode_enabled
        self._pos = config.paper_only_sleeves_active
        self._sold = ps._nyse_journal_sold_today
        self._buys = ps._nyse_journal_buys_since_flat
        config.PAPER_TRADING = False
        config.PAPER_NYSE_ENTRY_HYGIENE_ENABLED = True
        config.PAPER_NYSE_MAX_ADDS_PER_SYMBOL = 2
        config.paper_aggressive_context = lambda: False  # type: ignore
        config.paper_chase_mode_enabled = lambda: False  # type: ignore
        config.paper_only_sleeves_active = lambda: False  # type: ignore
        ps._nyse_journal_sold_today = ps._nyse_session_sold_today  # type: ignore
        ps._nyse_journal_buys_since_flat = lambda _sym: None  # type: ignore
        ps.reset_nyse_entry_hygiene_state()

    def tearDown(self):
        if getattr(self, "_saved", None):
            config.PAPER_TRADING = self._saved["PAPER_TRADING"]
            config.PAPER_NYSE_ENTRY_HYGIENE_ENABLED = self._saved[
                "PAPER_NYSE_ENTRY_HYGIENE_ENABLED"
            ]
            config.PAPER_NYSE_MAX_ADDS_PER_SYMBOL = self._saved[
                "PAPER_NYSE_MAX_ADDS_PER_SYMBOL"
            ]
            config.PAPER_NYSE_SAME_DAY_REENTRY_BLOCK = self._saved[
                "PAPER_NYSE_SAME_DAY_REENTRY_BLOCK"
            ]
            config.PAPER_NYSE_ATR_STOP_SLEEVE_COOLDOWN = self._saved[
                "PAPER_NYSE_ATR_STOP_SLEEVE_COOLDOWN"
            ]
            config.paper_aggressive_context = self._pac
            config.paper_chase_mode_enabled = self._pcm
            config.paper_only_sleeves_active = self._pos
            ps._nyse_journal_sold_today = self._sold
            ps._nyse_journal_buys_since_flat = self._buys
        ps.reset_nyse_entry_hygiene_state()

    def test_paper_add_2_allowed_add_3_skipped(self):
        self._paper(max_adds=2)
        now = datetime(2026, 8, 31, 11, 0, 0)
        execu = SimpleNamespace(portfolio=SimpleNamespace(positions={"PBF": 10}))
        self.assertIsNone(ps._nyse_entry_hygiene_skip(execu, "PBF", now=now))
        ps._mark_nyse_open_add("PBF")
        reason = ps._nyse_entry_hygiene_skip(execu, "PBF", now=now)
        self.assertIsNotNone(reason)
        self.assertIn("add 3 blocked", reason or "")
        self.assertIn("(2/2", reason or "")
        self.assertGreaterEqual(ps.get_nyse_hygiene_skip_counts()["hygiene_max_adds"], 1)

    def test_paper_flat_first_buy_allowed(self):
        self._paper(max_adds=2)
        now = datetime(2026, 8, 31, 11, 0, 0)
        execu = SimpleNamespace(portfolio=SimpleNamespace(positions={}))
        self.assertIsNone(ps._nyse_entry_hygiene_skip(execu, "VALE", now=now))

    def test_paper_same_day_reentry_and_atr_cooldown_still_on(self):
        self._paper(max_adds=2)
        now = datetime(2026, 8, 31, 11, 0, 0)
        execu = SimpleNamespace(portfolio=SimpleNamespace(positions={}))
        ps.mark_nyse_sold_today("PBF", now=now)
        self.assertEqual(
            ps._nyse_entry_hygiene_skip(execu, "PBF", now=now),
            "same-day reentry block (sold earlier today)",
        )
        ps.reset_nyse_entry_hygiene_state()
        ps.mark_nyse_atr_stop_from_exit(
            "PATH", reason="smart_atr_stop", sleeve="NYSE", now=now
        )
        reason = ps._nyse_entry_hygiene_skip(execu, "VALE", now=now)
        self.assertIsNotNone(reason)
        self.assertIn("ATR-stop sleeve", reason or "")

    def test_live_profile_a_max_adds_not_applied(self):
        self._live()
        now = datetime(2026, 8, 31, 11, 0, 0)
        execu = SimpleNamespace(portfolio=SimpleNamespace(positions={"PBF": 10}))
        ps._nyse_open_add_counts["PBF"] = 5
        self.assertIsNone(ps._nyse_entry_hygiene_skip(execu, "PBF", now=now))
        self.assertFalse(config.effective_paper_nyse_entry_hygiene())

    def test_vti_core_still_off(self):
        self.assertFalse(config.vti_core_enabled())
        self.assertEqual(float(config.PAPER_VTI_CORE_PCT), 0.0)
        self.assertEqual(float(config.VTI_CORE_PCT), 0.0)

    def test_effective_paper_max_adds_is_2(self):
        self.assertEqual(int(config.PAPER_NYSE_MAX_ADDS_PER_SYMBOL), 2)
        self.assertEqual(config.effective_paper_nyse_max_adds_per_symbol(), 2)


if __name__ == "__main__":
    unittest.main()
