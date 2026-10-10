# RHYME / sleeve conflict audit

Generated: 2026-08-19 05:57
Book: `live` | Window: 2026-07-29 → 2026-08-19 (ET session dates)
Sources: jsonl:fill, jsonl:order_submitted

## How to read

- Cancel ratio = `1 - |net| / gross`. Persistently **> 0.60–0.70** = strong self-conflict.
- `count_cancel` uses fill counts (catches sell submits with null notional).
- Dollar cancel ignores those $0 sells; **verdict uses event=fill from 2026-08-12** when enough rows exist.
- Same-symbol RT = buy and sell of that ticker on the same session (daily) or paired within 2 sessions (top-10).
- Freeze: measure only — no `.env` / live / paper retune from this file.

## Window totals

- Fills: **350** (buy 223 / sell 127)
- Gross turnover: **$40,506** | Net signed: **$39,021**
- Cancel ratio (window): **0.04** | median daily: **0.93** | ex-core: **0.02**
- Paired round-trips (≤2 sessions): **14** | regime-mismatch pairs: **0**
- Days with regime flip **and** both buy+sell: **0** / 5
- Verdict: **cancellation not persistently in the 0.6-0.7 danger zone; RHYME not clearly subtractive on this window**

## Daily

| date | regime | #buy | #sell | gross $ | net $ | cancel $ | count_cancel | ex-core | same_sym_rt | notes |
|------|--------|-----:|------:|--------:|------:|---------:|-------------:|--------:|------------:|-------|
| 2026-08-12 | D | 30 | 7 | 39,253 | 39,011 | 0.01 | 0.38 | 0.00 | 4 | buy+sell same day; cross_sleeve_syms=1 |
| 2026-08-13 | D | 74 | 45 | 198 | 14 | 0.93 | 0.76 | 0.90 | 23 | buy+sell same day |
| 2026-08-14 | D | 71 | 41 | 503 | -4 | 0.99 | 0.73 | 0.97 | 22 | buy+sell same day |
| 2026-08-17 | D | 44 | 29 | 481 | 30 | 0.94 | 0.79 | 0.89 | 21 | buy+sell same day |
| 2026-08-18 | D | 4 | 5 | 71 | -31 | 0.56 | 0.89 | 0.97 | 0 | buy+sell same day |

## By RHYME letter (fill as-of)

| regime | #buy | #sell | gross $ | net $ | cancel | days |
|--------|-----:|------:|--------:|------:|-------:|-----:|
| D | 223 | 127 | 40,506 | 39,021 | 0.04 | 5 |

## Top 10 symbols by round-trip pairs (≤2 sessions)

| symbol | pairs | regime-mismatch | cross-sleeve | buy $ | sell $ |
|--------|------:|----------------:|-------------:|------:|-------:|
| VTI | 14 | 0 | 0 | 140 | 402 |

## Notes

- journal paper_journal.csv: rows=13159 preferred_trade_rows=1
- journal paper_chase_journal.csv: rows=818 preferred_trade_rows=0
- journal paper_journal.csv: rows=309 preferred_trade_rows=0
- journal has no event=fill in preferred set; using jsonl
- jsonl action rows after fill-over-submit: 1350
- verdict tape: 350 event=fill rows (submit $ is biased; many sells have null notional)
- submit+fill days with both sides: 10/11
