# Frozen cross-asset hypotheses - written 2026-10-26 16:03 CDT BEFORE any backtest
Baseline: live H1D 50/50. Sleeve A = SSO if SPY prior close > SMA200 else BIL (daily check, trade on flip). Sleeve B = 12 equal (w=0.5/12): XLB XLE XLF XLI XLK XLP XLU XLV XLY SPY TLT GLD. Rebalance monthly (first trading day) + on flip.
Knob: ONE tilt to Sleeve B only, set on the first trading day of each month from data up to the prior close (FRED: one extra day of lag). Sleeve A untouched. Sleeve B total stays 50%.
Costs 5 bps/side on all turnover. Data: yfinance adjusted closes, FRED public CSV.
- H1 stock-bond corr: 63-trading-day corr of SPY vs TLT daily returns > 0 -> TLT weight moved to BIL.
- H2 gold/real rates: FRED DFII10 change over last 63 trading days < 0 -> GLD weight = 2w, funded pro rata from the other 11 Sleeve B funds.
- H3 credit: HYG/IEF price ratio below its 126-day average (spreads widening) -> the 10 equity funds (9 XL + SPY) halved; freed weight split 50/50 TLT/BIL. (HYG/IEF chosen over FRED HY OAS because FRED's public OAS history is truncated.)
- H4 dollar: FRED DTWEXBGS 126-trading-day change > 0 -> XLE, XLB, GLD halved; freed weight spread equally over the other 9 funds.
Windows (disjoint): W1 start(2008-03, limited by BIL/HYG + 200d)-2012, W2 2013-2018, W3 2019-2026-10.
Pass rule (PROTOCOL.md full-stack gates + owner rule): in EVERY window CAGR > baseline, Sharpe >= baseline, MaxDD not worse by > 1.0 pp; AND bootstrap 95% CI (1000 resamples, daily excess return, annualised) over full period excludes zero (> 0).
Kill rule: fail any gate in any window -> reject, kept in ledger, not re-run with a new story. No threshold tuning after results.
