# Prod market_data.db coverage audit

Generated: 2026-07-29 21:27 UTC
DB: `C:\Users\Owner\PythonTrading\stock-bot\market_data.db` (105.59 MB)
Daily tables: 605
Audit hash: `14b1f1c78f8dc3d4`

**Union panel start != full-bot testability. 2022 STRICT = price-path / realized-vol stress; VIX-dependent behavior partially simulated.**

## Key series

| Series | Present | First | Last | Rows |
|--------|---------|-------|------|------|
| SPY | YES | 2016-07-29 | 2026-07-29 | 2513 |
| VTI | YES | 2001-06-15 | 2026-07-29 | 6316 |
| XLE | YES | 1998-12-22 | 2026-07-29 | 6941 |
| GLD | YES | 2016-07-29 | 2026-07-29 | 2513 |
| VIX | YES | 2024-06-17 | 2026-06-15 | 501 |
| ^VIX | NO | — | — | — |
| TLT | YES | 2016-07-29 | 2026-07-29 | 2513 |
| XOM | YES | 1962-01-02 | 2026-07-29 | 16251 |
| CL=F | NO | — | — | — |
| GC=F | NO | — | — | — |

## Coverage cutoffs (symbols with first<=cutoff and last>=2026)

- 1990-01-01: **141** / 605
- 2000-01-01: **205** / 605
- 2004-01-01: **224** / 605
- 2008-01-01: **240** / 605
- 2014-01-01: **263** / 605
- 2020-01-01: **305** / 605
- 2022-01-01: **324** / 605

## Earliest 10 tables (union start drivers)

- AEP: 1962-01-02 -> 2026-07-29 (16251 rows)
- BA: 1962-01-02 -> 2026-07-29 (16251 rows)
- CAT: 1962-01-02 -> 2026-07-29 (16251 rows)
- CVX: 1962-01-02 -> 2026-07-29 (16251 rows)
- DIS: 1962-01-02 -> 2026-07-29 (16251 rows)
- ED: 1962-01-02 -> 2026-07-29 (16251 rows)
- GD: 1962-01-02 -> 2026-07-29 (16251 rows)
- GE: 1962-01-02 -> 2026-07-29 (16251 rows)
- HON: 1962-01-02 -> 2026-07-29 (16251 rows)
- IBM: 1962-01-02 -> 2026-07-29 (16251 rows)

## Caveats

- SPY in prod DB starts 2016-07-29 — truncated vs ETF inception (~1993).
- GLD in prod DB starts 2016-07-29 — truncated vs ETF inception (~2004).
- VIX in prod DB starts 2024-06-17 — 2022 stress cannot use true VIX level/rising gates.
- WTI/gold futures missing from prod DB — oil/gold shock studies need research store / FRED.
- Panel union start 1962-01-02 is driven by AEP et al. — not full-strategy coverage.
- Missing key symbols: ^VIX, CL=F, GC=F

## Next step

- Build separate research store: `python scripts/research/geopolitical_event_study/backfill_research_macro.py`
- Do **not** overwrite prod `market_data.db` during freeze.
