# RHYME / sleeve conflict audit

Generated: 2026-08-21 16:49
Book: `paper` | Window: 2026-08-19 → 2026-08-21 (ET session dates)
Sources: journal:paper_journal.csv

## How to read

- Cancel ratio = `1 - |net| / gross`. Persistently **> 0.60–0.70** = strong self-conflict.
- `count_cancel` uses fill counts (catches sell submits with null notional).
- Dollar cancel ignores those $0 sells; **verdict uses event=fill from 2026-08-12** when enough rows exist.
- Same-symbol RT = buy and sell of that ticker on the same session (daily) or paired within 2 sessions (top-10).
- Freeze: measure only — no `.env` / live / paper retune from this file.

## Window totals

- Fills: **37** (buy 20 / sell 17)
- Gross turnover: **$6,109** | Net signed: **$1,427**
- Cancel ratio (window): **0.77** | median daily: **0.65** | ex-core: **0.77**
- Paired round-trips (≤2 sessions): **2** | regime-mismatch pairs: **0**
- Days with regime flip **and** both buy+sell: **0** / 3
- Verdict: **elevated self-conflict - keep RHYME but do not add more regime overlays until freeze ends**

## Daily

| date | regime | #buy | #sell | gross $ | net $ | cancel $ | count_cancel | ex-core | same_sym_rt | notes |
|------|--------|-----:|------:|--------:|------:|---------:|-------------:|--------:|------------:|-------|
| 2026-08-19 | A | 12 | 9 | 3,967 | 1,388 | 0.65 | 0.86 | 0.65 | 1 | buy+sell same day |
| 2026-08-20 | A | 8 | 3 | 1,645 | 537 | 0.67 | 0.55 | 0.67 | 0 | buy+sell same day |
| 2026-08-21 | A | 0 | 5 | 498 | -498 | 0.00 | 0.00 | 0.00 | 0 |  |

## By RHYME letter (fill as-of)

| regime | #buy | #sell | gross $ | net $ | cancel | days |
|--------|-----:|------:|--------:|------:|-------:|-----:|
| A | 20 | 15 | 6,081 | 1,456 | 0.76 | 3 |
| B | 0 | 1 | 23 | -23 | 0.00 | 1 |
| D | 0 | 1 | 5 | -5 | 0.00 | 1 |

## Top 10 symbols by round-trip pairs (≤2 sessions)

| symbol | pairs | regime-mismatch | cross-sleeve | buy $ | sell $ |
|--------|------:|----------------:|-------------:|------:|-------:|
| MHK | 1 | 0 | 0 | 96 | 198 |
| TPG | 1 | 0 | 0 | 185 | 351 |

## Notes

- journal paper_journal.csv: rows=19410 preferred_trade_rows=37
- SoT journal: C:\Users\Owner\PythonTrading\stock-bot\data\portal\users\dawimberly\books\alpaca_paper\paper_journal.csv
- using 37 journal event=fill rows
- jsonl present (2209) but ignored; journal event=fill is SoT
- verdict tape: 37 event=fill rows (submit $ is biased; many sells have null notional)
- submit+fill days with both sides: 2/3
