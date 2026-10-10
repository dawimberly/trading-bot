# Paper NYSE sell follow-through

Generated: 2026-08-21 16:49 CT
SoT: `C:\Users\Owner\PythonTrading\stock-bot\data\portal\users\dawimberly\books\alpaca_paper\paper_journal.csv`  event=fill side=sell  sleeve=NYSE  since 2026-08-19
Measure only. No orders. No ATR / min-hold / .env changes.

| # | symbol | exit reason | sell CT | sell px | realized_pnl | hold | 1d later | 5d later | 10d later | flag |
|---|--------|-------------|---------|---------|--------------|------|----------|----------|-----------|------|
| 1 | CAMT | nyse_fat_loser_trim | 2026-08-19 08:52 | 150.59 | -0.35 | — | 148.51 (-1.38%) 08-20 | 146.81 (-2.51%) last 08-21 | 146.81 (-2.51%) last 08-21 |  |
| 2 | PFE | concentration_guard_trim | 2026-08-19 08:52 | 28.11 | +1.39 | 1.9d | 27.79 (-1.15%) 08-20 | 28.07 (-0.15%) last 08-21 | 28.07 (-0.15%) last 08-21 |  |
| 3 | MU | smart_atr_stop | 2026-08-19 08:53 | 922.03 | -8.89 | 5.9d | 974.33 (+5.67%) 08-20 | 966.78 (+4.85%) last 08-21 | 966.78 (+4.85%) last 08-21 | STOP+5%+ |
| 4 | SHOP | smart_atr_stop | 2026-08-19 08:54 | 147.78 | -20.94 | 23.6h | 147.18 (-0.41%) 08-20 | 149.25 (+0.99%) last 08-21 | 149.25 (+0.99%) last 08-21 |  |
| 5 | VSEC | smart_atr_stop | 2026-08-19 08:55 | 233.18 | -2.71 | 2.0d | 214.51 (-8.01%) 08-20 | 217.58 (-6.69%) last 08-21 | 217.58 (-6.69%) last 08-21 |  |
| 6 | PFE | concentration_guard_trim | 2026-08-19 09:08 | 28.25 | +0.60 | 2.0d | 27.79 (-1.64%) 08-20 | 28.07 (-0.64%) last 08-21 | 28.07 (-0.64%) last 08-21 |  |
| 7 | PFE | concentration_guard_trim | 2026-08-19 09:24 | 28.36 | +0.64 | 2.0d | 27.79 (-2.02%) 08-20 | 28.07 (-1.03%) last 08-21 | 28.07 (-1.03%) last 08-21 |  |
| 8 | DBB | concentration_guard_trim | 2026-08-19 12:22 | 25.24 | +0.02 | 3.7h | 25.16 (-0.32%) 08-20 | 25.55 (+1.22%) last 08-21 | 25.55 (+1.22%) last 08-21 |  |
| 9 | EAT | smart_atr_stop | 2026-08-19 13:07 | 231.71 | -20.83 | 1.2d | 233.10 (+0.60%) 08-20 | 246.06 (+6.19%) last 08-21 | 246.06 (+6.19%) last 08-21 | STOP+5%+ |
| 10 | MHK | smart_atr_stop | 2026-08-20 08:46 | 131.76 | -8.57 | 23.5h | 136.22 (+3.38%) 08-21 | 136.22 (+3.38%) last 08-21 | 136.22 (+3.38%) last 08-21 |  |
| 11 | DBB | concentration_guard_trim | 2026-08-20 12:30 | 25.15 | +0.01 | 1.2d | 25.55 (+1.58%) 08-21 | 25.55 (+1.58%) last 08-21 | 25.55 (+1.58%) last 08-21 |  |
| 12 | TPG | nyse_fat_loser_trim | 2026-08-20 14:07 | 51.90 | -3.97 | 1.2d | 52.40 (+0.96%) 08-21 | 52.40 (+0.96%) last 08-21 | 52.40 (+0.96%) last 08-21 |  |
| 13 | DBB | concentration_guard_trim | 2026-08-21 10:44 | 25.52 | +0.37 | 2.1d | — | — | — |  |
| 14 | TPG | nyse_fat_loser_trim | 2026-08-21 11:06 | 52.21 | -0.01 | 2.1d | — | — | — |  |
| 15 | DBB | concentration_guard_trim | 2026-08-21 11:09 | 25.51 | +0.02 | 2.1d | — | — | — |  |
| 16 | DBB | concentration_guard_trim | 2026-08-21 11:22 | 25.54 | +0.05 | 2.1d | — | — | — |  |
| 17 | CAMT | smart_atr_stop | 2026-08-21 14:58 | 146.24 | -20.30 | — | — | — | — |  |

## Flags
- MU smart_atr_stop sold 08-19 08:53 then +5.7% by 2026-08-20 (within 5d)
- EAT smart_atr_stop sold 08-19 13:07 then +6.2% by 2026-08-21 (within 5d)

## Journal notes
- header had 16 columns; mapped trailing extras to ['order_id', 'book', 'entry_hour', 'realized_pnl', 'realized_pnl_pct', 'is_partial']

