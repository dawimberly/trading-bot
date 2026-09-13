# Sneaky Pivot research backtest — `baseline_w3_next_open`

Generated: 2026-08-02 20:21 UTC

**Freeze-safe research only** — not wired into paper/live `run_all`.

## Config

- Days requested: 90
- Confirm window: 3
- Fill: `next_open`
- Stretch target: False
- Universe: SPY, QQQ, IWM, AAPL, MSFT, NVDA, BRK.B
- Risk: 1% equity to stop; max notional 10%

## Results

| Metric | Value |
|--------|------:|
| Trades | 152 |
| Signals | 152 |
| Win rate | 58.6% |
| Total PnL | $1,902.80 |
| Final equity | $101,902.80 |
| Avg R | 0.01 |
| Median R | 0.01 |
| Max DD | -0.60% |
| Skip (no levels) | 141 |
| Skip (no OR touch) | 132 |

## Last 15 trades

| Date | Sym | Side | Entry | Exit | R | Reason |
|------|-----|------|------:|-----:|--:|--------|
| 2026-07-21 | NVDA | short | 204.49 | 207.17 | -0.13 | eod |
| 2026-07-21 | BRK.B | long | 489.66 | 489.66 | 0.00 | eod |
| 2026-07-21 | AAPL | long | 326.71 | 327.59 | 0.03 | eod |
| 2026-07-22 | BRK.B | short | 489.94 | 489.37 | 0.01 | eod |
| 2026-07-23 | IWM | long | 292.05 | 292.68 | 0.02 | stop |
| 2026-07-23 | BRK.B | long | 487.83 | 490.97 | 0.06 | eod |
| 2026-07-23 | AAPL | long | 321.89 | 322.22 | 0.01 | stop |
| 2026-07-24 | IWM | short | 292.11 | 291.20 | 0.03 | eod |
| 2026-07-27 | SPY | short | 740.97 | 737.29 | 0.05 | target |
| 2026-07-27 | IWM | short | 293.39 | 292.92 | 0.02 | eod |
| 2026-07-27 | BRK.B | short | 495.62 | 497.19 | -0.03 | eod |
| 2026-07-27 | MSFT | short | 389.75 | 391.78 | -0.05 | stop |
| 2026-07-28 | QQQ | long | 671.93 | 675.50 | 0.05 | eod |
| 2026-07-28 | NVDA | long | 196.01 | 196.98 | 0.05 | eod |
| 2026-07-28 | BRK.B | short | 508.53 | 502.99 | 0.11 | stop |
