# Open-print vs fade - NYSE smart ATR stops

Paper journal exits vs the session open and the 5-minute print at the sell. Not a promote. Do not write .env from this file.

Generated 2026-09-26T12:13:23.
Week window from 2026-09-19 (the nine Saturday-review exits).
Stop is the 5-minute close at the journal sell. Bot entry is that print divided by (1 + note percent).
The bot ATR used for a sanity check is 14d mean |dClose|, same as calculate_atr.

Alpaca paper still open: AAPL x42.97 @333.532206, ARM x21.10 @286.627447, CVX x70.04 @207.983304, DBB x294.72 @25.407756, JNJ x27.65 @271.214649, KTOS x156.32 @48.234948, PFE x649.41 @27.720226, PLTR x73.17 @181.18161

## This week

Open vs stop: {'fade': 8, 'gap_through': 1}
Overnight gap vs logged loss: {'gap_then_fade': 7, 'gap_covers_loss': 1, 'up_open_fade': 1}

| Sold ET | Sym | Note | Bot entry | Stop | Prior | Open | Print | Gap | Class | vs note | Held |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|---:|
| 2026-09-21 09:36 | CVX | -1.79% | 210.61 | 206.84 | 209.40 | 207.80 | 206.84 | -0.76% | fade | gap_then_fade | 70.04 |
| 2026-09-23 09:53 | FRO | -2.81% | 48.42 | 47.06 | 47.89 | 47.88 | 47.06 | -0.02% | fade | gap_then_fade | n/a |
| 2026-09-23 09:53 | GOOGL | -1.71% | 351.03 | 345.03 | 351.18 | 349.78 | 345.03 | -0.40% | fade | gap_then_fade | n/a |
| 2026-09-23 11:11 | AUGO | -2.08% | 87.47 | 85.65 | 92.80 | 90.27 | 85.65 | -2.73% | fade | gap_covers_loss | n/a |
| 2026-09-23 12:54 | MPC | -2.36% | 392.39 | 383.13 | 389.84 | 394.00 | 383.13 | +1.07% | fade | up_open_fade | n/a |
| 2026-09-25 09:34 | TGT | -1.54% | 157.26 | 154.84 | 156.28 | 154.84 | 154.84 | -0.92% | gap_through | gap_then_fade | n/a |
| 2026-09-25 09:45 | DINO | -3.16% | 106.89 | 103.51 | 105.81 | 104.64 | 103.51 | -1.11% | fade | gap_then_fade | n/a |
| 2026-09-25 10:06 | NTSK | -3.31% | 18.63 | 18.01 | 18.57 | 18.46 | 18.01 | -0.59% | fade | gap_then_fade | n/a |
| 2026-09-25 15:26 | CRM | -2.13% | 238.64 | 233.56 | 238.41 | 237.03 | 233.56 | -0.58% | fade | gap_then_fade | n/a |

8 faded after an alive open; 1 gapped through; 0 were already dead. This week's stops are session fades (and a few noisy exit rows), not overnight gaps through a daily ATR stop. Leave the book alone. Journal logged an exit and Alpaca still holds CVX. Those rows are not closed lots.

TGT is the one open print (09:34 ET, sale = the open). AUGO's overnight gap vs Friday's close was larger than the note, but Monday's open (90.27) was still above the stop (85.65); it died later that morning. CVX still shows 70 shares on the paper account after the Sep 21 exit row.

Do not add an open delay, change the ATR multiple, or touch `.env` from nine names.

Paper journal exits vs the session open and the 5-minute print at the sell. Not a promote. Do not write .env from this file.

## Prior 7-14d (same rule, not in the Saturday table)

Counts: {'already_through': 10, 'fade': 17, 'gap_through': 2}

| Sold ET | Sym | Note | Bot entry | Stop | Prior | Open | Print | Gap | Class | vs note | Held |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|---:|
| 2026-09-15 14:32 | BAC | -5.02% | 62.77 | 59.62 | 59.47 | 59.23 | 59.62 | -0.39% | already_through | gap_then_fade | n/a |
| 2026-09-16 10:11 | BB | -0.89% | 7.85 | 7.78 | 7.82 | 7.80 | 7.78 | -0.26% | fade | gap_then_fade | n/a |
| 2026-09-16 10:21 | XOM | -0.28% | 165.48 | 165.02 | 169.27 | 167.02 | 165.02 | -1.33% | fade | gap_covers_loss | n/a |
| 2026-09-16 10:34 | SMCI | -1.02% | 36.69 | 36.32 | 35.64 | 35.80 | 36.32 | +0.45% | already_through | up_open_fade | n/a |
| 2026-09-16 11:36 | TSLA | -0.64% | 364.22 | 361.89 | 356.68 | 357.62 | 361.89 | +0.26% | already_through | up_open_fade | n/a |
| 2026-09-16 11:53 | SNOW | -0.36% | 335.43 | 334.22 | 322.84 | 318.50 | 334.22 | -1.35% | already_through | gap_covers_loss | n/a |
| 2026-09-16 12:40 | SHOP | -0.51% | 132.01 | 131.34 | 129.88 | 130.00 | 131.34 | +0.09% | already_through | up_open_fade | n/a |
| 2026-09-16 13:24 | RTX | -0.09% | 197.28 | 197.10 | 195.47 | 195.15 | 197.10 | -0.16% | already_through | gap_covers_loss | n/a |
| 2026-09-16 14:24 | LB | -4.01% | 87.45 | 83.94 | 87.66 | 86.67 | 83.94 | -1.13% | fade | gap_then_fade | n/a |
| 2026-09-17 09:37 | META | -0.35% | 677.08 | 674.72 | 673.59 | 680.19 | 674.72 | +0.98% | fade | up_open_fade | n/a |
| 2026-09-17 09:48 | JPM | -0.72% | 350.78 | 348.25 | 349.02 | 352.25 | 348.25 | +0.92% | fade | up_open_fade | n/a |
| 2026-09-17 10:08 | JNJ | -0.34% | 269.29 | 268.38 | 267.30 | 268.17 | 268.38 | +0.33% | already_through | up_open_fade | 27.65 |
| 2026-09-17 11:24 | LMT | -0.36% | 527.65 | 525.75 | 537.32 | 538.98 | 525.75 | +0.31% | fade | up_open_fade | n/a |
| 2026-09-18 09:40 | PFE | -0.25% | 27.54 | 27.47 | 27.63 | 27.64 | 27.47 | +0.04% | fade | up_open_fade | 649.41 |
| 2026-09-18 09:40 | SNOW | -0.54% | 335.89 | 334.07 | 337.89 | 338.25 | 334.07 | +0.11% | fade | up_open_fade | n/a |
| 2026-09-18 09:46 | PFE | -0.13% | 27.45 | 27.41 | 27.63 | 27.64 | 27.41 | +0.04% | fade | up_open_fade | 649.41 |
| 2026-09-18 09:46 | PLTR | 0.12% | 173.20 | 173.41 | 176.16 | 177.41 | 173.41 | +0.70% | fade | up_open_fade | 73.17 |
| 2026-09-18 09:56 | SNOW | -0.41% | 335.11 | 333.74 | 337.89 | 338.25 | 333.74 | +0.11% | fade | up_open_fade | n/a |
| 2026-09-18 10:10 | BB | -1.07% | 7.96 | 7.87 | 7.94 | 8.01 | 7.87 | +0.88% | fade | up_open_fade | n/a |
| 2026-09-18 10:10 | KTOS | -0.92% | 47.66 | 47.22 | 47.68 | 48.28 | 47.22 | +1.26% | fade | up_open_fade | 156.32 |
| 2026-09-18 10:19 | LMT | -0.25% | 534.68 | 533.35 | 537.93 | 536.76 | 533.35 | -0.22% | fade | gap_then_fade | n/a |
| 2026-09-18 10:33 | NVDA | -0.54% | 220.46 | 219.27 | 219.40 | 219.06 | 219.27 | -0.15% | gap_through | gap_then_fade | n/a |
| 2026-09-18 10:37 | AMD | -0.54% | 548.57 | 545.61 | 544.86 | 547.56 | 545.61 | +0.50% | fade | up_open_fade | n/a |
| 2026-09-18 10:37 | ARM | -0.99% | 268.95 | 266.29 | 264.92 | 269.00 | 266.29 | +1.54% | fade | up_open_fade | 21.10 |
| 2026-09-18 11:13 | AMZN | -0.27% | 254.14 | 253.46 | 251.12 | 252.87 | 253.46 | +0.69% | already_through | up_open_fade | n/a |
| 2026-09-18 11:14 | META | -0.50% | 679.22 | 675.82 | 682.39 | 687.77 | 675.82 | +0.79% | fade | up_open_fade | n/a |
| 2026-09-18 11:40 | LNG | -0.31% | 271.96 | 271.12 | 269.82 | 269.44 | 271.12 | -0.14% | already_through | gap_then_fade | n/a |
| 2026-09-18 11:45 | XOM | -0.32% | 163.23 | 162.71 | 163.25 | 162.46 | 162.71 | -0.48% | gap_through | gap_covers_loss | n/a |
| 2026-09-18 11:56 | MU | -0.55% | 993.50 | 988.03 | 976.92 | 981.88 | 988.03 | +0.51% | already_through | up_open_fade | n/a |

