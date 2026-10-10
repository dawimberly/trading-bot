# Freeze daily hygiene — 2026-08-28

Generated: 2026-08-28 15:41 UTC
Equity: 96686.65 | Regime: RHYME_C: Steady_Bullish_Growth | Heartbeat age: 0m

**Freeze-safe.** Cleanups below are for human CONFIRM / DENY / HOLD. No auto strategy or live changes.

## Cleanup candidates (consider today)

| ID | Sev | Default | Item | Detail |
|----|-----|---------|------|--------|
| `journal_parse_error` | cleanup | **HOLD** | Journal had bad CSV lines (skipped) | Used on_bad_lines=skip for paper_chase_journal.csv |
| `daily_errors_present` | cleanup | **HOLD** | Today's error log has 1 error(s) | Review obvious repeats; do not retune strategy from errors alone. |
| `attribution_stale` | cleanup | **CONFIRM** | Sleeve attribution stale (211h) | Run: python scripts/analysis/forward_sleeve_attribution.py |

## Info

- **No SPY sleeve fills in window** — Consistent with SPY-off freeze lock.

## How to respond

- Reply in Telegram: `CONFIRM <id>` / `DENY <id>` / `HOLD <id>` (or ignore = HOLD).
- Or tick the weekly confirm/deny plan Saturday.
- Do **not** change live Profile A or invent new sleeves from this memo.

## Full detail

### `journal_parse_error` (cleanup)
- Default: **HOLD**
- Used on_bad_lines=skip for paper_chase_journal.csv
- Evidence: `C:\Users\Owner\PythonTrading\stock-bot\paper_chase_journal.csv`

### `spy_fills_ok` (info)
- Default: **HOLD**
- Consistent with SPY-off freeze lock.
- Evidence: `C:\Users\Owner\PythonTrading\stock-bot\paper_chase_journal.csv`

### `daily_errors_present` (cleanup)
- Default: **HOLD**
- Review obvious repeats; do not retune strategy from errors alone.
- Evidence: `C:\Users\Owner\PythonTrading\stock-bot\logs\daily_errors_2026-08-28.md`

### `attribution_stale` (cleanup)
- Default: **CONFIRM**
- Run: python scripts/analysis/forward_sleeve_attribution.py
- Evidence: `C:\Users\Owner\PythonTrading\stock-bot\scripts\analysis\forward_sleeve_attr_last.md`

- Note: Freeze-safe: recommendations are CONFIRM/DENY/HOLD for humans only.
- Note: No auto .env / strategy / live changes.
