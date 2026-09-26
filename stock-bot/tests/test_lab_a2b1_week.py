"""Saturday Lab A2B1 / fat-loser tally — journal-only, no Alpaca."""

from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "analysis"))

from lab_a2b1_week import (  # noqa: E402
    append_to_weekly_review,
    collect_tally,
    render_markdown,
)

_ET = ZoneInfo("America/New_York")
_HEADER = [
    "timestamp",
    "event",
    "symbol",
    "side",
    "pair_key",
    "notes",
    "realized_pnl",
]


def _write_journal(path: Path, rows: list[dict[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_HEADER)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _journals(tmp_path: Path, *, lab, medium=None, live=None) -> dict[str, Path]:
    return {
        "alpaca_paper": _write_journal(tmp_path / "lab.csv", lab),
        "alpaca_paper_v2": _write_journal(tmp_path / "medium.csv", medium or []),
        "alpaca_live": _write_journal(tmp_path / "live.csv", live or []),
    }


def test_collects_lab_a2b1_and_flags_fat_leak(tmp_path):
    now = datetime(2026, 9, 26, 12, 0, tzinfo=_ET)
    journals = _journals(
        tmp_path,
        lab=[
            {
                "timestamp": "2026-09-23T09:35:00-04:00",
                "event": "fill",
                "symbol": "AMD",
                "side": "sell",
                "pair_key": "",
                "notes": "a2b1_gain_fade prior +8.2%",
                "realized_pnl": "120.50",
            },
            {
                "timestamp": "2026-09-22T09:40:00-04:00",
                "event": "fill",
                "symbol": "NVDA",
                "side": "sell",
                "pair_key": "",
                "notes": "trail_stop",
                "realized_pnl": "10",
            },
        ],
        live=[
            {
                "timestamp": "2026-09-24T10:00:00-04:00",
                "event": "fill",
                "symbol": "INTC",
                "side": "sell",
                "pair_key": "",
                "notes": "nyse_fat_loser_trim",
                "realized_pnl": "-8.00",
            }
        ],
    )
    tally = collect_tally(lookback_days=7, now=now, journals=journals)
    assert tally["lab_a2b1_n"] == 1
    assert tally["books"]["alpaca_paper"]["a2b1"][0]["symbol"] == "AMD"
    assert tally["fat_leak"] == {"alpaca_live": 1}
    md = render_markdown(tally)
    assert "AMD" in md
    assert "+120.50" in md
    assert "LEAK" in md
    assert "INTC" in md


def test_old_rows_and_non_fills_ignored(tmp_path):
    now = datetime(2026, 9, 26, 12, 0, tzinfo=_ET)
    journals = _journals(
        tmp_path,
        lab=[
            {
                "timestamp": "2026-09-01T09:35:00-04:00",
                "event": "fill",
                "symbol": "OLD",
                "side": "sell",
                "pair_key": "",
                "notes": "a2b1_gain_fade",
                "realized_pnl": "1",
            },
            {
                "timestamp": "2026-09-24T09:35:00-04:00",
                "event": "signal",
                "symbol": "AMD",
                "side": "",
                "pair_key": "",
                "notes": "a2b1_gain_fade",
                "realized_pnl": "",
            },
        ],
    )
    tally = collect_tally(lookback_days=7, now=now, journals=journals)
    assert tally["lab_a2b1_n"] == 0
    assert tally["fat_leak"] == {}
    assert "HOLD" in render_markdown(tally)


def test_append_embeds_before_advisory_marker(tmp_path, monkeypatch):
    now = datetime(2026, 9, 26, 12, 0, tzinfo=_ET)
    journals = _journals(tmp_path, lab=[])
    monkeypatch.setattr(
        "lab_a2b1_week.collect_tally",
        lambda **kwargs: collect_tally(lookback_days=7, now=now, journals=journals),
    )
    monkeypatch.setattr("lab_a2b1_week.write_tally", lambda *a, **k: tmp_path / "skip.md")
    md = append_to_weekly_review(
        "## Medium\n\n<!-- advisory only; never auto-apply -->\n"
    )
    assert md.count("Lab 8 A2B1 / fat-loser tally") == 1
    assert md.index("Lab 8 A2B1") < md.index("<!-- advisory only")
    assert md.endswith("<!-- advisory only; never auto-apply -->\n")
