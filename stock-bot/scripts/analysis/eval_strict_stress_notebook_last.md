# STRICT bear/stress notebook (report only)

Generated: 2026-07-29 20:17 UTC
Profile: paper-aggressive freeze (SPY off, Dyn VTI ON, hygiene ON)
**STRICT PIT: ON | no-thinking | overlays off**

**REPORT ONLY - freeze stays on; no promote recommendations; no live Profile A changes; no param search**

## Stress window

- Requested: 2022-01-03 -> 2022-12-30
- Effective sim (after warmup): 2021-11-24 -> 2022-12-30
- Slice note: ok
- Data coverage: 1962-01-02 -> 2026-07-29 (17602 bars)

| Leg | Window | Return | Sharpe | MaxDD | VTI B&H | Trades | SPY fills | NYSE |
|-----|--------|--------|--------|-------|---------|--------|-----------|------|
| stress_strict | 2021-11-24 -> 2022-12-30 | -12.15% | -0.56 | -15.27% | -19.16% | 1421 | 0 | 196 |
| bull_90d_strict_cached | 2026-04-01 -> 2026-07-26 | +15.63% | 2.44 | -3.09% | +13.18% | 779 | n/a | 82 |

## vs VTI B&H (stress leg)

- Strategy -12.15% vs VTI -19.16% (+7.01pp)

## vs recent bull 90d STRICT

- Bull 90d: +15.63% Sharpe 2.44 MaxDD -3.09% (source: eval_strict_windows_last.json)
- Stress: -12.15% Sharpe -0.56 MaxDD -15.27%
- Delta (stress - bull90): return -27.78pp, Sharpe -3.00, MaxDD -12.18pp

## Notes

- Freeze stays on - measurement only.
- Do not retune from a single stress window.
- Live Profile A unchanged.
- REPORT ONLY - freeze stays on; no promote recommendations; no live Profile A changes; no param search
