# VTI vs SPY inside H1D (2026-10-26) - NOT pre-registered (ad-hoc owner question); informational only
Data yfinance adj close 2008-03-17..2026-10-23, 5 bps/side, prior-close signal, monthly + flip rebalance.
| Variant | CAGR | MaxDD | Sharpe | Turnover/yr | Excess vs base (95% CI) |
|---|---|---|---|---|---|
| a baseline (live) | 12.89% | -27.5% | 0.79 | 5.48 | - |
| b VTI replaces SPY in Sleeve B | 12.89% | -27.6% | 0.79 | 5.48 | 0.00pp [-0.04,+0.04] |
| c Sleeve A signal on VTI 200d | 13.41% | -27.5% | 0.82 | 5.81 | +0.43pp [-0.54,+1.45] |
| d dynamic VTI/SPY by 6-mo momentum | 12.88% | -27.6% | 0.79 | 5.67 | -0.01pp [-0.04,+0.02] |
Windows (c): 2008-12 +0.52pp, 2013-18 +1.41pp, 2019-26 -0.19pp. SPY/VTI daily corr 0.994; 200d signals agree 98.6%.
Verdict: no change. (c) fails 2019-26 window and CI includes zero.
