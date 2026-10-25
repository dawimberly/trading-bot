# 12-to-7 momentum — reject

**It does not beat VTI on both windows.** Promotion is false. The 50% annual target is not claimed.

The sort was frozen in `preregistration_12_7.json` before these numbers. One change versus the extension book: among names with positive ma50 extension, RSI14 below 70, and both endpoint closes, rank by the split-adjusted return from the session 12 calendar months earlier through the session 7 calendar months earlier. The extension book ranks that same set by ma50 extension. Stops, hold, sizing, and the 7-minute quote cap stay as in `local_expectancy_v1`. Fills are the next session's open. Costs are 5 bps per side and 10 bps per side.

The panel is the same file as the local-expectancy study (`panel_sha256` `54c9057ab11bbc43846e999c3fc4405a05deef4e5e8e788ea26e6b3ee46cffb2`). No new download. Twenty-seven names. `SPCX` stays out. VTI and SPY are benchmarks, not candidates.

## Required windows, net of cost

A cell passes only if 12-to-7 return is strictly above VTI, strictly above extension, and drawdown is not more than 2.0 percentage points worse than extension. Both windows and both costs are required.

| Window | Cost / side | 12-to-7 | Extension | VTI | Equal-weight | Passed |
|---|---:|---:|---:|---:|---:|---|
| 2024-10-24 → 2025-10-23 (250 sessions) | 5 bps | **−18.27%** / DD 21.64% | +0.23% / 18.26% | +17.03% / 19.30% | +59.31% | no |
| same | 10 bps | **−16.59%** / DD 19.97% | −0.64% / 18.51% | +16.91% / 19.30% | +59.15% | no |
| 2025-10-24 → 2026-10-23 (251 sessions) | 5 bps | **+30.48%** / DD 10.53% | +9.36% / 9.85% | +13.06% / 8.92% | +38.22% | yes |
| same | 10 bps | **+28.70%** / DD 10.83% | +7.19% / 10.35% | +12.95% / 8.92% | +38.08% | yes |

Sharpe, rf = 0. Prior window at 5 bps: 12-to-7 −1.17, extension 0.09, VTI 0.90. Recent window at 5 bps: 12-to-7 1.50, extension 0.56, VTI 1.02. At 10 bps the recent figures are 1.43, 0.46, and 1.01.

Turnover (buys over average NAV) did not fall. Prior window: 9.65x at 5 bps (93 round trips) and 9.34x at 10 bps (91). Recent window: 18.83x and 18.84x, 202 round trips at both costs. Extension turnover on the same runs is 9.3x and 19.4x. The sort changed which name was bought. The 30-session hold, the stops, and one name per session still recycle the book.

Drawdown versus extension: +3.38 percentage points on the prior window at 5 bps (fails the 2.0 point gate), +1.47 points at 10 bps (inside the gate, and the return still fails). Recent window: +0.67 and +0.48 points, inside the gate. Versus VTI the recent drawdown is still deeper, by 1.61 and 1.92 points.

## What the two windows actually did

On the prior window both books are flat from 2025-04-08 through 2025-10-23. The 15% entry halt does not reset. Extension had peaked near +22.6% on 2025-02-18 and given back 18.3% into that halt, finishing +0.23% at 5 bps. 12-to-7 had peaked at only +4.3% on 2024-11-11, then bled to a trough of 0.817 on 2025-04-07. The whole −18.27% is in the first half. The second half is zero because the book was in cash.

At 10 bps that same prior path ends at −16.59% instead of −18.27%. The halt date does not move. Two fills differ, the trough is 0.834 instead of 0.817, and the cash lock-in is slightly higher. That is not a cost edge. Both endings lose to VTI by about 34 percentage points and lose to extension.

On the recent window the halt does not fire. Both halves are positive at 5 bps (+11.33% then +17.20%) and at 10 bps (+10.17% then +16.82%). 12-to-7 finishes ahead of VTI and ahead of extension at both costs, with drawdown inside the extension gate. It finishes behind the equal-weight hold of the same 27 names (+38.22% at 5 bps). That hold is the June 2026 screener, so survivorship flatters it. It is reported because it is the dumber book of the same names. It is not the named pass rule.

One passing window does not clear a failed window. The prior window fails return against VTI, return against extension, and, at 5 bps, the drawdown gate.

## Verdict

Reject. 12-to-7 does not beat VTI after costs on both required windows. The recent window does, with a deeper drawdown than VTI and with turnover still near 19x. The prior window is a large loss. Nothing here is a reason to change a live, Medium, or Lab book. `ENTRY_QUOTE_MAX_AGE_SECONDS` stays 420. Historical opens are modeled at quote age 0.

`local_expectancy` stays rejected. This run does not retune it, the halt, or the hold length.

The sticky halt was tested later as its own frozen change, a 21-session re-arm, in `RESULTS_HALT_REARM.md`. That book also fails the two-window VTI test.
