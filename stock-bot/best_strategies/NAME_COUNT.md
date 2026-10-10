# Name-count STRICT A/B (done 2026-09-20)

Paper-aggressive, VTI off, Lab concentrated OFF in the test, costs ON.
Baseline = 25 names. Gate: better return + Sharpe, MaxDD not worse >1pp.

Runtime aggressive is already **Lab 8** via `portal_bot` (heartbeat 8 NYSE).
This test is the scanner-stack slot cap, not the Lab trail book.

## Results

| Window | 25 | 15 | 8 | VTI B&H |
|--------|---:|---:|--:|--------:|
| 365d (310 bars) | +7.75% / 0.50 / -10.25% | +6.54% / 0.43 / -13.33% | **+8.20% / 0.52 / -10.25%** | +14.82% |
| 1000d (850 bars) | +2.53% / 0.12 / -12.11% | +13.05% / 0.37 / -13.51% | **+19.78% / 0.52 / -10.18%** | +40.83% |

Values are return / Sharpe / MaxDD.

## Verdict

- **15 names: fail.** 365d worse on all three. 1000d wins return/Sharpe but MaxDD is 1.4pp worse than 25 (gate is 1.0pp).
- **8 names: pass vs 25** both windows. Already what portal launches. **No .env write.**
- All three trail VTI on both windows. Slot count is not the missing alpha vs buy-and-hold.

Do not deploy `MAX_ACTIVE_TICKERS=15`.

## 6 vs 8 (done 2026-09-21)

Same STRICT stack, Lab OFF, no per-name size bump. Baseline = 8.

| Window | 8 | 6 | VTI B&H |
|--------|--:|--:|--------:|
| 365d | **+8.31% / 0.53 / −10.25%** | +8.19% / 0.52 / −10.25% | +14.82% |
| 1000d | **+24.55% / 0.64 / −11.34%** | +9.13% / 0.30 / −12.20% | +40.83% |

**FAIL.** 365d is a wash (6 slightly worse). 1000d 6 is −15.4pp / −0.34 Sharpe. Stay on 8.
