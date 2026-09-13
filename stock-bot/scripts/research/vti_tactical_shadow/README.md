# VTI tactical $5k — shadow first (freeze-safe)

**Status:** research sidecar only. No paper/live orders. Freeze unchanged.

## Provenance (do not skip)

Every event has:
- `data_source`: `sqlite` | `yfinance_fallback`
- `bar_date`, `data_fresh`, `promote_eligible`, `stale_reason`

Stale / fallback rows → `output/shadow_events_stale.jsonl` (not promote stats).
Pre-registered pass/fail: **`PROMOTE_CRITERIA.md`**.

## Run

```powershell
cd stock-bot
# Prefer failing closed when DB/fallback is stale:
python scripts/research/vti_tactical_shadow/run_shadow_once.py --require-fresh
python scripts/research/vti_tactical_shadow/summarize_shadow.py
```

`--allow-stale` only for debugging; never for promote-week stats.

## Consensus

Shadow first. Dual-log prod vs shadow-equity RHYME. ARIMA advisory-only.
Promote equity-split before real fills. No gut VTI dump.
