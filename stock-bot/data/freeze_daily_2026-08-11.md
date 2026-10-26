# Freeze daily hygiene — 2026-08-11

Generated: 2026-08-11 21:57 UTC
Equity: 97460.72 | Regime: RHYME_C: Steady_Bullish_Growth | Heartbeat age: 1329m

**Freeze-safe.** Cleanups below are for human CONFIRM / DENY / HOLD. No auto strategy or live changes.

## Cleanup candidates (consider today)

| ID | Sev | Default | Item | Detail |
|----|-----|---------|------|--------|
| `attribution_stale` | cleanup | **CONFIRM** | Sleeve attribution stale (96h) | Run: python scripts/analysis/forward_sleeve_attribution.py |
| `heartbeat_stale` | anomaly | **CONFIRM** | Paper heartbeat stale (1329 min) | Restart paper bot if unexpected overnight halt. |

## Info

- **No SPY sleeve fills in window** — Consistent with SPY-off freeze lock.

## How to respond

- Reply in Telegram: `CONFIRM <id>` / `DENY <id>` / `HOLD <id>` (or ignore = HOLD).
- Or tick the weekly confirm/deny plan Saturday.
- Do **not** change live Profile A or invent new sleeves from this memo.

## Full detail

### `heartbeat_stale` (anomaly)
- Default: **CONFIRM**
- Restart paper bot if unexpected overnight halt.
- Evidence: `age_min=1328.7`

### `spy_fills_ok` (info)
- Default: **HOLD**
- Consistent with SPY-off freeze lock.
- Evidence: `C:\Users\Owner\PythonTrading\stock-bot\paper_chase_journal.csv`

### `attribution_stale` (cleanup)
- Default: **CONFIRM**
- Run: python scripts/analysis/forward_sleeve_attribution.py
- Evidence: `C:\Users\Owner\PythonTrading\stock-bot\scripts\analysis\forward_sleeve_attr_last.md`

- Note: Freeze-safe: recommendations are CONFIRM/DENY/HOLD for humans only.
- Note: No auto .env / strategy / live changes.
