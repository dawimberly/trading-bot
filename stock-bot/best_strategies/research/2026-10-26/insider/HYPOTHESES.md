# Frozen insider-cluster hypothesis - written 2026-10-26 16:25 CDT BEFORE any backtest or data inspection
Data: SEC "Insider Transactions Data Sets" quarterly bulk (Form 3/4/5), sec.gov, User-Agent with contact, <=10 req/s. Prices: yfinance adjusted closes.
Event: Form 4 non-derivative rows with TRANS_CODE='P' and acquired ('A'), value = shares*price. Per issuer (ISSUERCIK), an insider "counts" once its P purchases total >= $10,000. A CLUSTER fires on the FILING_DATE when the 3rd distinct reporting owner counts within a trailing 30-calendar-day window of filing dates. Point-in-time = filing date, never transaction date.
Trade: buy at the close of the first trading day AFTER the cluster filing date; hold 63 trading days; ignore new clusters for an issuer already held.
Portfolio: each open position weight = min(1/N_open, 5%); rest in BIL. Filters at entry (known at prior close): price > $2 and 20-day median dollar volume > $1M (mcap not available PIT for free, this is the proxy for excluding tiny names). Ticker = ISSUERTRADINGSYMBOL on the filing; skip if no yfinance history.
Cost: 30 bps/side on all stock turnover.
Benchmarks (unchanged): live H1D 50/50 mix; VTI buy-and-hold.
Windows: W1 2008-03..2012, W2 2013..2018, W3 2019..latest data set.
Pass rule: every window CAGR > H1D AND > VTI, Sharpe >= H1D, MaxDD not worse than H1D by > 1.0 pp; full-period bootstrap 95% CI of daily excess vs H1D > 0. Kill: any failed gate -> reject, preserved, no re-run with new thresholds.
Known bias: yfinance lacks most delisted tickers -> survivorship bias UP; ticker reuse possible. A pass can only justify a Lab paper trial.
