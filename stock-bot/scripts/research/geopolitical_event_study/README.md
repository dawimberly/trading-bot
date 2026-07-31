# Geopolitical event study (research only)

**Freeze-safe.** Does not overwrite prod `market_data.db`, change paper defaults, or touch live Profile A.

## Purpose

Honest macro / event-window study for geopolitical stress (wars, ME escalations, oil shocks).

- Event calendar = **labels only** (dates + dual sources), never trade signals
- Macro series live in a **separate research DB**
- Every window gets an explicit **fidelity grade**

## Fidelity grades

| Grade | Meaning |
|-------|---------|
| `macro_only` | VIX / WTI / equity-proxy responses only |
| `partial_strategy_proxy` | Some freeze-profile inputs present; VIX may be estimated |
| `full_freeze_compatible` | Required freeze series present with true history (not estimated) |

## Layout

```
data/research/geopolitical/
  research_macro.db          # immutable research store (created by backfill)
  series_manifest.json       # first/last, source, checksum
  event_calendar.json        # sourced event labels
scripts/research/geopolitical_event_study/
  audit_prod_coverage.py     # prod DB honesty audit
  backfill_research_macro.py # FRED + yfinance -> research DB only
  run_event_study.py         # report-only graded windows
  event_calendar.json        # curated seed calendar
```

## Run order

```powershell
cd stock-bot
# 1) Prod coverage honesty (read-only)
python scripts/research/geopolitical_event_study/audit_prod_coverage.py

# 2) Build / refresh research store (never writes prod DB)
python scripts/research/geopolitical_event_study/backfill_research_macro.py

# 3) Graded event windows (report only)
python scripts/research/geopolitical_event_study/run_event_study.py
```

Outputs:

- `scripts/analysis/audit_prod_coverage_last.md`
- `scripts/analysis/geopolitical_event_study_last.md`
- `scripts/analysis/geopolitical_event_study_last.json`

## Out of scope

- Trading signals / sleeve changes
- Promote language
- Wiring GDELT/Wayback into live or paper bots
- Overwriting `market_data.db`
