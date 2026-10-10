# VTI reduce cash / %VTI snapshot

Generated: 2026-08-21 04:02 UTC

**Measure only. No vti_core sell-rule. No live Profile A / Dynamic VTI / .env change. Do not promote from this file.**

As-of join: Alpaca paper closed VTI fills × portal `event=cycle` equity/cash (last cycle at or before fill, America/Chicago).

| Metric | Value |
|--------|-------|
| VTI sells | 75 |
| cash ≥ sell notional (unused, NaN→tight) | 58 |
| cash < sell notional or missing (tight heuristic) | 17 |
| cash_unused / cash_tight / cash_unknown | 58 / 8 / 9 |
| would skip under cash-need candidate (unused + VTI ≥ 40%) | 58 |
| portal cycles | 10982 |
| VTI fills walked | 159 |
| ending walk qty vs Alpaca | 127.983 / 127.983 (anchor 7.0) |

## Reading

Most historical paper VTI sells happened while cash already covered the sell notional — band trim / target oscillation, not raising cash for NYSE.
Candidate (not coded, freeze still on): skip VTI **sell** when cash covers the reduce **and** current VTI is still ≥ the 40% paper floor. Buys when underweight are unchanged. Live Profile A (85% VTI) is out of scope.

## Clusters

- 2026-06-16: 26 VTI sells, cash often -2–30% at ~83% VTI; heuristic unused 20/26
- 2026-07-13: 5 VTI sells at ~162% VTI; heuristic unused 0/5
- 2026-08-05: 38 VTI sells, cash often 8–34% at ~54% VTI; heuristic unused 37/38

## Sample rows

| ts CT | qty sold | px | qty before | VTI $ | equity | cash | cash% | VTI% | label | skip? |
|-------|---------:|---:|-----------:|------:|-------:|-----:|------:|-----:|-------|-------|
| 2026-06-01 14:46:12 | 2.028 | 373.74 | 11.929 | 4458 | None | None | None | None | cash_tight | False |
| 2026-06-16 14:13:09 | 81.049 | 370.75 | 221.433 | 82095 | 98919 | 2366 | 2.4 | 83.0 | cash_tight | False |
| 2026-06-16 14:14:27 | 80.036 | 370.71 | 220.425 | 81714 | 98912 | 29727 | 30.1 | 82.6 | cash_unused | True |
| 2026-06-16 14:15:25 | 80.043 | 370.68 | 220.421 | 81706 | 98905 | 29721 | 30.1 | 82.6 | cash_unused | True |
| 2026-06-16 14:17:13 | 80.059 | 370.39 | 220.463 | 81657 | 98867 | 29723 | 30.1 | 82.6 | cash_unused | True |
| 2026-06-16 14:18:11 | 80.036 | 370.57 | 220.432 | 81686 | 98837 | 29705 | 30.1 | 82.6 | cash_unused | True |
| 2026-06-16 14:19:11 | 80.027 | 370.62 | 220.425 | 81693 | 98863 | 29711 | 30.1 | 82.6 | cash_unused | True |
| 2026-06-16 14:21:00 | 80.000 | 370.87 | 220.375 | 81730 | 98897 | 29714 | 30.0 | 82.6 | cash_unused | True |
| … | | | | | | | | | | |
| 2026-08-05 12:34:26 | 10.306 | 380.54 | 138.370 | 52655 | 97438 | 32672 | 33.5 | 54.0 | cash_unused | True |
| 2026-08-05 12:38:00 | 10.291 | 380.65 | 138.311 | 52648 | 97493 | 21315 | 21.9 | 54.0 | cash_unused | True |
| 2026-08-05 12:41:54 | 10.268 | 380.67 | 138.260 | 52632 | 97463 | 21430 | 22.0 | 54.0 | cash_unused | True |
| 2026-08-05 12:44:37 | 10.257 | 380.69 | 138.231 | 52623 | 97458 | 21500 | 22.1 | 54.0 | cash_unused | True |
| 2026-08-05 12:48:53 | 10.147 | 380.67 | 143.436 | 54602 | 97453 | 21523 | 22.1 | 56.0 | cash_unused | True |
| 2026-08-05 12:53:08 | 5.281 | 380.73 | 143.371 | 54586 | 97500 | 21530 | 22.1 | 56.0 | cash_unused | True |
| 2026-08-05 12:58:41 | 10.100 | 380.72 | 138.090 | 52573 | 97489 | 31202 | 32.0 | 53.9 | cash_unused | True |
| 2026-08-05 13:19:10 | 15.374 | 380.77 | 143.357 | 54586 | 97455 | 29373 | 30.1 | 56.0 | cash_unused | True |

Measure only. No vti_core sell-rule. No live Profile A / Dynamic VTI / .env change. Do not promote from this file.
