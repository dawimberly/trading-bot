# RHYME / sleeve conflict audit

Generated: 2026-09-09 15:58
Book: `paper_v2` | Window: 2026-09-08 → 2026-09-09 (ET session dates)
Sources: journal:paper_journal.csv

## How to read

- Cancel ratio = `1 - |net| / gross`. Persistently **> 0.60–0.70** = strong self-conflict.
- `count_cancel` uses fill counts (catches sell submits with null notional).
- Dollar cancel ignores those $0 sells; **verdict uses event=fill from 2026-08-12** when enough rows exist.
- Same-symbol RT = buy and sell of that ticker on the same session (daily) or paired within 2 sessions (top-10).
- Freeze: measure only — no `.env` / live / paper retune from this file.

## Window totals

- Fills: **36** (buy 10 / sell 26)
- Gross turnover: **$65,764** | Net signed: **$-10,263**
- Cancel ratio (window): **0.84** | median daily: **0.86** | ex-core: **0.84**
- Paired round-trips (≤2 sessions): **5** | regime-mismatch pairs: **0**
- Days with regime flip **and** both buy+sell: **0** / 2
- Verdict: **strong internal disagreement (churn); regime-mismatch share is modest - more sleeve/stop recycling than letter wars**

## Daily

| date | regime | #buy | #sell | gross $ | net $ | cancel $ | count_cancel | ex-core | same_sym_rt | notes |
|------|--------|-----:|------:|--------:|------:|---------:|-------------:|--------:|------------:|-------|
| 2026-09-08 | D | 6 | 13 | 38,674 | -9,035 | 0.77 | 0.63 | 0.77 | 3 | buy+sell same day |
| 2026-09-09 | D | 4 | 13 | 27,090 | -1,228 | 0.95 | 0.47 | 0.95 | 4 | buy+sell same day |

## By RHYME letter (fill as-of)

| regime | #buy | #sell | gross $ | net $ | cancel | days |
|--------|-----:|------:|--------:|------:|-------:|-----:|
| D | 10 | 26 | 65,764 | -10,263 | 0.84 | 2 |

## Top 10 symbols by round-trip pairs (≤2 sessions)

| symbol | pairs | regime-mismatch | cross-sleeve | buy $ | sell $ |
|--------|------:|----------------:|-------------:|------:|-------:|
| LB | 1 | 0 | 0 | 4,032 | 3,969 |
| LMT | 1 | 0 | 0 | 3,909 | 3,813 |
| ONDS | 1 | 0 | 0 | 4,047 | 3,955 |
| TGT | 1 | 0 | 0 | 632 | 618 |
| TWST | 1 | 0 | 0 | 1,125 | 1,070 |

## Notes

- journal paper_journal.csv: rows=4335 preferred_trade_rows=167
- SoT journal: C:\Users\Owner\PythonTrading\stock-bot\data\portal\users\dawimberly\books\alpaca_paper_v2\paper_journal.csv
- using 167 journal event=fill rows
- jsonl present (1598) but ignored; journal event=fill is SoT
- verdict tape: 36 event=fill rows (submit $ is biased; many sells have null notional)
- submit+fill days with both sides: 2/2
