# Frozen small-account edge hypotheses - written 2026-10-26 16:18 CDT BEFORE any backtest
Prior records checked (best_strategies/*.md, ledger.py): none of these tested before (insider only appeared as a FULL-stack overlay diagnostic).
Each is a standalone strategy; idle cash earns BIL. Signals use prior close only; trade at close (close-to-close returns).
Benchmarks (unchanged): (1) live H1D 50/50 mix (SSO/BIL on SPY 200d + 12 ETFs, 5bps/side); (2) VTI buy-and-hold.
- H1 Turn-of-month: hold IWM for the last trading day of each month + first 3 trading days of the next; else BIL. Cost 5 bps/side.
- H2 Pre-holiday: hold IWM only on the session before an NYSE holiday (a session whose next session is >1 weekday later, or a Friday whose next session is Tuesday or later); else BIL. Cost 5 bps/side.
- H3 Weekly short-term reversal, small caps: every week's last session, among CURRENT S&P 600 members with price > $5 and 20d median dollar volume > $2M, buy the 20 with the worst 5-session return, equal weight, hold to next week's last session. Cost 30 bps/side.
- H4 52-week-high momentum, small caps: first session of each month, same universe/filters, hold 30 names with the highest close / 252-session max close; equal weight. Cost 30 bps/side.
- Not run (data not feasible free/point-in-time in scope): PEAD (no free PIT earnings dates), insider Form-4 clusters (EDGAR parse too large for this pass), index add/delete (no free PIT change list). Logged as not-tested, not rejected.
SURVIVORSHIP: H3/H4 use today's S&P 600 list -> strongly biased UP. A pass on H3/H4 can at most justify a paper trial, never a promotion.
Windows: W1 2008-03..2012, W2 2013..2018, W3 2019..2026-10.
Pass rule (PROTOCOL + owner): every window CAGR > H1D AND > VTI, Sharpe >= H1D, MaxDD not worse than H1D by > 1.0 pp; full-period bootstrap 95% CI of daily excess vs H1D (annualised, 1000 resamples) > 0.
Kill: any failed gate -> reject, preserved, no re-run with new thresholds.
