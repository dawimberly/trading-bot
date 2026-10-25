# Strategic review — October 2026

Update after the pick-quality test: this account is the experiment, and the savings are elsewhere. Holding VTI is the savings policy. It is not the next change for the experiment. The measured next step is in `RESULTS_PICK_QUALITY.md`: rerun the frozen selector test on a free historical SIP panel before touching the stop.

Frank notes for Dan. Research only. Nothing here places an order or edits a live or Medium book.

The account in view is about $300 at Alpaca. Data stays free or delayed. The first goal is to beat VTI after realistic costs. The long-term aspiration is Medallion-like returns. Eight months of live trading have been about break-even.

## What “beat VTI” can mean here

VTI over the two frozen windows was about +17% (through October 2025) and about +13% (through October 2026), after the same 5 or 10 bps per side charged to the active books. Break-even against that is a loss of those returns. The research ledger agrees with the live experience: after costs, the books that were actually frozen and run do not clear both windows.

Medallion is a different sport. That fund’s record was short-horizon, capacity-capped, and closed to outside money, with a research and execution stack a $300 account cannot rent. The live entry rule refuses a quote older than 7 minutes. A delayed feed is about 15 minutes old, so it cannot drive entries, and it should not. Under $25,000, a margin account is also limited in day trades. An overnight hold does not hit that limit. An intraday Medallion copy does. Expected value of building toward that copy, on Yahoo daily bars or delayed SIP, is negative. It spends the time that would otherwise sit in VTI.

At this balance the arithmetic is small and the drag is not. The repo’s default live minimum notional is $10, and a 9.5% clip of $300 clears it, so order size is not the blocker. Turnover is. The prior-window re-arm book bought about 17 times its average NAV. On a $300 account that is roughly $5,200 of buys in a year. At 10 bps per side, with sells of similar size, modeled slippage is about $10, around 3% of the account, before the spread on a thinner name. Alpaca’s stock commission is zero. The spread is the fee. A strategy that turns over 15–20 times has to clear that hurdle every year and still beat VTI. The signal can look slow, because it ranks a 12-month return, and the book can still trade most days, because the hold is 30 sessions and the stop is 5%.

Taxes do not change the decision at $300. A year of VTI dividends on that balance is a few dollars. Tax-loss harvesting and asset location need more capital than this before they move the outcome. The useful part of “index plus a sleeve” is the sleeve cap, not the tax treatment.

Survivorship is the other trap. The June 2026 screener, 27 names after `SPCX` was dropped for short history, earned about +59% and +38% just held equal-weight over the two windows. VTI earned +17% and +13%. Any ranker on that list inherits a list that already won. Equal-weight being ahead of a ranker is a warning, not a benchmark to beat.

## What the frozen tests showed

These are the results already written down. They are not a menu to mix.

- Local expectancy, pairwise rank, and median linear lost to a simple extension rank after costs. Local expectancy also lost to VTI on both windows (about −1.7% and −1.0% at 5 bps per side).
- 12-to-7 momentum beat VTI on the recent window (+30.5% versus +13.1% at 5 bps) and lost the prior window (−18.3% versus +17.0%). Drawdown on the loss was 21.6%. Turnover stayed about 10x and 19x.
- Replacing the sticky 15% halt with a 21-session re-arm was frozen before it was run. On the prior window the book re-entered and finished +3.8% at 5 bps, with the same 21.6% drawdown, still about 13 points behind VTI. Turnover rose to about 17x. The recent window never halted, still beat VTI (+29.0% versus +13.1%), and finished a little behind the sticky 12-to-7 book.
- The paired extension rank with the same re-arm made +33.3% on the prior window, ahead of VTI, and +7.7% on the recent window, behind VTI. That +33% is one window of a book that was not the hypothesis. It fails the same two-window rule.

The halt was real: it had locked the prior window in cash from early April 2025. Letting it re-arm recovered the second half and was not enough. The rank that did better on that rebound still failed the other window.

## Ranked by expected value

Expected value here means wealth versus keeping the live bot as it is, over the next few years, after costs, on about $300, with free or delayed data. Beating VTI is called out separately, because several high-value choices match VTI and do not beat it.

### 1. Hold VTI. Turn the live stock bot off.

This is the highest expected value for the money. One fractional VTI buy matches the stated benchmark within a few basis points of expense ratio (VTI’s is 0.03%) and commission. It does not beat VTI. It stops losing to VTI, which is the measured outcome of the last eight months and of every frozen rule that had to win both windows.

Leave research on a paper clock with no new dollars. The seven-minute quote cap stays. No live book, no Medium book, no config edit from this file.

### 2. If the goal stays “beat VTI,” the sleeve is $0 until a rule wins both windows

Core in VTI. Tactical sleeve unfunded. At $300, a 20% sleeve is $60. Adding 2 points of account return requires that sleeve to beat VTI by about 10 points after costs, including in a year like the prior window. No frozen rule has done that twice. Funding the sleeve now pays for variance.

This is the grown-up version of “index plus a tactical sleeve.” The tax half of that slogan can wait until the balance is large enough for harvesting to matter. The cap is the part that matters now.

### 3. One monthly momentum test, and only on a point-in-time universe

This is the best research bet left, and the chance it beats VTI after costs is still low. The idea is the slow form of 12-to-7: rank liquid names by a year of return, skip the most recent month, rebalance monthly, hold a handful, and judge it against VTI on two windows at 5 and 10 bps per side. A monthly book turns over about once or twice a year. The current engine turns over 10–19 times because of the stop and the 30-session hold. Those are different tests.

Do this only with names that existed on the decision date, including names that later died. The June 2026 list cannot be that universe. Free data makes delistings the hard part. If the universe cannot be built without a paid tape, skip the test. One freeze, no scan of lookbacks, and a written reject if either window loses to VTI. A pass on this daily survivorship panel would still not be a reason to go live.

### 4. Own a published factor fund instead of rebuilding it

A cheap value or momentum ETF (the Avantis and Vanguard factor funds are the usual examples) is how a $300 account takes a long-run premium without another backtest. The expense ratio is higher than VTI by a few tenths of a point. Over one year this is a coin flip against VTI. Over a long hold, the historical premium is a few points in some decades and missing in others, with long stretches of lag. That is a modest, uncertain edge. It is higher expected value than another ranker on 27 names. It does not look like Medallion, and it should not be asked to.

## What to stop

- Another rank, neighbor count, Monday filter, or halt length on this 27-name overnight book. Four rankers and one halt change have had their frozen tests. The paired extension re-arm is already measured and fails the recent window.
- Treating the +33% prior-window extension path, or the +30% recent-window 12-to-7 path, as an edge. Each one lost the other window.
- Relaxing the seven-minute entry gate so a delayed feed can trade. That feed is research-only.
- Buying a market-data subscription to rescue these windows. The daily test is the easier, lower-turnover clock, and it still failed both windows.
- Intraday research, options, and a Medallion-shaped target. Day-trade limits, the quote gate, spreads, and a $300 balance all point the same way. A 50% year is not a planning number.
- Reading the equal-weight hold of the current screener as evidence of skill. It is evidence the list is flattered.

## Where that leaves the account

The realistic way to beat VTI from here is a low-turnover rule that wins two disjoint windows after costs, on names chosen without survivorship, and then gets a small sleeve. That rule is not in hand. Until it is, the account’s expected value sits in VTI, and the research stays off the live book.
