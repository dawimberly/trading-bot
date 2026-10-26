# Sneaky Pivot research backtest — `strict_c2_touch`

Generated: 2026-08-02 20:20 UTC

**Freeze-safe research only** — not wired into paper/live `run_all`.

## Config

- Days requested: 90
- Confirm window: 1
- Fill: `touch`
- Stretch target: False
- Universe: SPY, QQQ, IWM, AAPL, MSFT, NVDA, BRK.B
- Risk: 1% equity to stop; max notional 10%

## Results

| Metric | Value |
|--------|------:|
| Trades | 89 |
| Signals | 89 |
| Win rate | 56.2% |
| Total PnL | $1,018.85 |
| Final equity | $101,018.85 |
| Avg R | 0.01 |
| Median R | 0.01 |
| Max DD | -0.49% |
| Skip (no levels) | 141 |
| Skip (no OR touch) | 132 |

## Last 15 trades

| Date | Sym | Side | Entry | Exit | R | Reason |
|------|-----|------|------:|-----:|--:|--------|
| 2026-07-17 | SPY | long | 744.83 | 745.59 | 0.01 | stop |
| 2026-07-17 | QQQ | long | 693.41 | 700.91 | 0.11 | stop |
| 2026-07-17 | IWM | long | 294.97 | 294.15 | -0.03 | stop |
| 2026-07-17 | NVDA | long | 201.73 | 203.80 | 0.10 | stop |
| 2026-07-20 | SPY | short | 745.52 | 742.10 | 0.05 | eod |
| 2026-07-20 | QQQ | short | 700.66 | 696.00 | 0.07 | eod |
| 2026-07-20 | NVDA | short | 205.60 | 203.38 | 0.11 | eod |
| 2026-07-20 | BRK.B | long | 491.83 | 491.41 | -0.01 | eod |
| 2026-07-21 | NVDA | short | 204.82 | 207.17 | -0.11 | eod |
| 2026-07-21 | BRK.B | long | 489.42 | 489.66 | 0.00 | eod |
| 2026-07-22 | BRK.B | short | 489.90 | 489.37 | 0.01 | eod |
| 2026-07-24 | IWM | short | 291.33 | 291.20 | 0.00 | eod |
| 2026-07-27 | SPY | short | 741.60 | 737.29 | 0.06 | target |
| 2026-07-27 | IWM | short | 294.37 | 292.92 | 0.05 | eod |
| 2026-07-27 | BRK.B | short | 495.34 | 497.19 | -0.04 | eod |
