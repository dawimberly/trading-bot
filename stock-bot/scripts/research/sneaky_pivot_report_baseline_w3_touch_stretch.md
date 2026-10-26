# Sneaky Pivot research backtest — `baseline_w3_touch_stretch`

Generated: 2026-08-02 20:21 UTC

**Freeze-safe research only** — not wired into paper/live `run_all`.

## Config

- Days requested: 90
- Confirm window: 3
- Fill: `touch`
- Stretch target: True
- Universe: SPY, QQQ, IWM, AAPL, MSFT, NVDA, BRK.B
- Risk: 1% equity to stop; max notional 10%

## Results

| Metric | Value |
|--------|------:|
| Trades | 152 |
| Signals | 152 |
| Win rate | 55.3% |
| Total PnL | $2,996.62 |
| Final equity | $102,996.62 |
| Avg R | 0.02 |
| Median R | 0.01 |
| Max DD | -0.41% |
| Skip (no levels) | 141 |
| Skip (no OR touch) | 132 |

## Last 15 trades

| Date | Sym | Side | Entry | Exit | R | Reason |
|------|-----|------|------:|-----:|--:|--------|
| 2026-07-21 | NVDA | short | 204.82 | 207.17 | -0.11 | eod |
| 2026-07-21 | BRK.B | long | 489.42 | 489.66 | 0.00 | eod |
| 2026-07-21 | AAPL | long | 326.16 | 327.59 | 0.04 | eod |
| 2026-07-22 | BRK.B | short | 489.90 | 489.37 | 0.01 | eod |
| 2026-07-23 | IWM | long | 292.57 | 292.68 | 0.00 | stop |
| 2026-07-23 | BRK.B | long | 488.33 | 490.97 | 0.05 | eod |
| 2026-07-23 | AAPL | long | 321.93 | 322.22 | 0.01 | stop |
| 2026-07-24 | IWM | short | 291.33 | 291.20 | 0.00 | eod |
| 2026-07-27 | SPY | short | 741.60 | 739.02 | 0.03 | eod |
| 2026-07-27 | IWM | short | 294.37 | 292.92 | 0.05 | eod |
| 2026-07-27 | BRK.B | short | 495.34 | 497.19 | -0.04 | eod |
| 2026-07-27 | MSFT | short | 390.17 | 391.78 | -0.04 | stop |
| 2026-07-28 | QQQ | long | 670.64 | 675.50 | 0.07 | eod |
| 2026-07-28 | NVDA | long | 195.17 | 195.06 | -0.01 | stop |
| 2026-07-28 | BRK.B | short | 508.51 | 502.99 | 0.11 | stop |
