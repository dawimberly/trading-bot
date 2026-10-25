# 12-to-7 with a recovering halt — reject

**It does not beat VTI on both windows.** Promotion is false. The 50% annual target is not claimed.

The rule was frozen in `preregistration_halt_rearm.json` before these numbers. One change versus sticky 12-to-7: when book equity is 15% below its peak, new entries pause, and they resume on the close 21 exchange sessions later. That close becomes the new peak, so another 15% loss is allowed. The rank stays the adjusted return from t-12 months to t-7 months. A rule that waits for the drawdown to heal was not used. A book that has sold to cash has a flat equity, so that drawdown never heals and the rule would have matched the sticky halt.

## Required windows, net of cost

VTI is the first comparison. The sticky 12-to-7 book is the one-knob baseline. A cell passes only if return is strictly above VTI, strictly above sticky 12-to-7, and drawdown is not more than 2.0 percentage points worse than sticky 12-to-7.

| Window | Cost / side | 12-to-7 re-arm | Sticky 12-to-7 | VTI | Turnover | Passed |
|---|---:|---:|---:|---:|---:|---|
| 2024-10-24 → 2025-10-23 (250 sessions) | 5 bps | **+3.81%** / DD 21.64% | −18.27% / 21.64% | +17.03% / 19.30% | 17.26x (181 trades) | no |
| same | 10 bps | **+5.04%** / DD 20.09% | −16.59% / 19.97% | +16.91% / 19.30% | 17.19x (181) | no |
| 2025-10-24 → 2026-10-23 (251 sessions) | 5 bps | **+28.96%** / DD 10.53% | +30.48% / 10.53% | +13.06% / 8.92% | 18.92x (203) | no |
| same | 10 bps | **+27.19%** / DD 10.83% | +28.70% / 10.83% | +12.95% / 8.92% | 18.93x (203) | no |

Sharpe, rf = 0. Prior window at 5 bps: re-arm 0.29, VTI 0.90. Recent window at 5 bps: re-arm 1.43, sticky 12-to-7 1.50, VTI 1.02.

On the prior window the halt fired once (2025-04-03 at 5 bps, 2025-03-31 at 10 bps) and re-armed 21 sessions later (2025-05-05 and 2025-04-30). The first half matches the sticky book exactly (−18.27% and −16.59%). The second half is the re-entry: +27.02% and +25.93%. Drawdown matches the sticky book at 5 bps and is 0.12 points worse at 10 bps, inside the 2.0 point gate. The terminal result is still about 13 points behind VTI. The 10 bps path ends higher than the 5 bps path because the halt and the re-arm each print a few sessions earlier. That is fill noise around the same crash.

On the recent window the halt never fired. Return still beats VTI at both costs, with drawdown 1.61 and 1.92 points deeper than VTI and identical to sticky 12-to-7. It finishes behind sticky 12-to-7 (+28.96% versus +30.48% at 5 bps). The gap is an order decided on the session before the window. The sticky book was still halted from the prior crash, so it carried no order into 2025-10-24. The re-arm book did. Second-half returns match the sticky book (+17.20% and +16.82%).

Turnover on the prior window rose from about 9.6x to about 17.2x because the second half traded. The recent window stayed near 19x.

## Paired extension book, same halt, not the hypothesis

The engine ran ma50-extension rank with the same 21-session re-arm. It is reported and it is not a pass.

| Window | Cost / side | Extension re-arm | Sticky extension | VTI |
|---|---:|---:|---:|---:|
| Prior | 5 bps | +33.35% / DD 18.26% | +0.23% / 18.26% | +17.03% |
| Prior | 10 bps | +31.09% / 18.51% | −0.64% / 18.51% | +16.91% |
| Recent | 5 bps | +7.71% / DD 10.94% | +9.36% / 9.85% | +13.06% |
| Recent | 10 bps | +5.57% / 11.62% | +7.19% / 10.35% | +12.95% |

On the prior window the extension rank kept more of the rebound than 12-to-7 did, and that one window finishes ahead of VTI. The recent window finishes behind VTI. Same two-window failure, different rank. The +33% figure is one half of a failed pair. It is not a strategy.

Equal-weight hold of the same 27 names remains +59.31% and +38.22% at 5 bps. That list is the June 2026 screener.

## Verdict

Reject. Re-arming the halt did the job the clock was written to do: the prior window came back from cash and finished positive, with the same drawdown as the sticky book. It still loses to VTI on that window. The recent window beats VTI and loses to the sticky 12-to-7 book, and the halt never fired there. Nothing here changes a live, Medium, or Lab book. `ENTRY_QUOTE_MAX_AGE_SECONDS` stays 420.
