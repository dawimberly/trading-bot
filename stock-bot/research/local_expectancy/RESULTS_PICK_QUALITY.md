# Pick quality — the mean clears, this tape cannot prove it

**The live ranker beat a random eligible name on the 20-session test that was frozen in advance. That is not an edge.** The names are the June 12, 2026 screener. Both windows had already been used. Promotion stays false. Nothing here changes a live or Medium book.

Actual NYSE entries could not be rebuilt. This checkout has no entry journal of symbol and date. Another sleeve's journal was not used. The numbers below are what `run_equity_strategy` would have ranked first: adjusted close / MA50 − 1, eligible when the close is above 99% of that average, one name, scanners off. `portal_bot.py` only launches the process. The 70-day MA in the paper banner is not this ranker.

## How a fill is defined

The signal is the close of day t. The entry is the next session's open. The primary return is that open against the open H sessions later, H = 1, 5, and 20. A path hit is a completed close, before that exit open, of at least +5%, +10%, or +20%. Intraday highs are not in the panel.

Filling the signal close instead would have changed the pooled 20-session mean by −0.21 percentage points. The forbidden fill was a small bias on this tape, and it ran against the picks. Stops are judged on the close and sold at the next open. In the prior window that gap averaged −0.04%. In the recent window the next open was 1.02% better than the trigger close, so the gap did not tax the stop.

## Turnover, three different numbers

- **Signal-change rate.** The top symbol changed on 21.8% of consecutive decision days in the pooled 20-session sample (20.0% prior, 23.1% recent). That is not dollar turnover.
- **One-way buy turnover.** Sum of buy notionals divided by mean NAV, over the window. Annualized means that total times 252 / sessions.
- **Two-sided turnover.** (buys + sells) / (2 × mean NAV). A round trip of the whole book is 1. Annualized the same way.

On the clip book with the 5% stop, two-sided turnover was 3.63 in the prior window and 3.57 in the recent window. Annualized, 3.66 and 3.58. The old “17x” figure was a window total of buy notional over mean NAV for a book that bought almost every day. It was not this rate.

## 20 sessions, open to open

Every decision day had a real choice. The eligible list averaged about 16 names.

| Sample | Days | Pick mean / median | Random mean | Excess vs random | p | Excess vs list, mean / median | Excess vs VTI, mean / median |
|---|---:|---:|---:|---:|---:|---:|---:|
| Pooled | 456 | +5.33% / +2.64% | +3.52% | **+1.81 pp** | 0.007 | +1.80 pp / **−1.62 pp** | +4.10 pp / +1.13 pp |
| 2024-10-24 → 2025-10-23 | 226 | +5.19% / +4.90% | +3.44% | +1.75 pp | 0.020 | +1.75 pp / −1.94 pp | +4.03 pp / +1.31 pp |
| 2025-10-24 → 2026-10-23 | 230 | +5.46% / +1.50% | +3.62% | +1.84 pp | 0.046 | +1.85 pp / −1.16 pp | +4.18 pp / +0.43 pp |

The frozen bar was a positive pooled excess versus random, p < 0.05, and a positive excess in each window. That bar is met. The median pick still finishes behind the equal-weight eligible list. The mean is a right tail.

The block bootstrap of pick-minus-VTI, block length 20, has a 5th percentile of −0.32 pp pooled (−2.48 pp prior, −0.89 pp recent). The average gap versus VTI includes zero once overlapping fortnights are not treated as independent bets. Beating VTI is not established.

At 1 session the pooled excess versus random is +0.08 pp (p = 0.26). At 5 sessions it is +0.37 pp (p = 0.12). The short horizons do not clear.

## Hit rate of +5 / +10 / +20, versus random

Path hits within 20 sessions, pooled:

| Move | Picks | Random eligible name | Difference | p |
|---|---:|---:|---:|---:|
| +5% | 74.1% | 58.5% | +15.6 pp | 0.0005 |
| +10% | 52.9% | 35.7% | +17.1 pp | 0.0005 |
| +20% | 25.4% | 15.7% | +9.7 pp | 0.0005 |

A random name from this list already prints +5% within 20 sessions more often than not. The ranker does it more often. That is the sense in which “these stocks go on to make 5–20%” is true, and it is only partly selection. Still being up by the exit open is rarer: 44.7% versus 38.2% at +5%, 34.6% versus 24.9% at +10%, 18.6% versus 11.6% at +20%.

The prior window’s path rates were 77.9% / 57.5% / 26.1% against 58.9% / 34.8% / 13.9%. The recent window’s were 70.4% / 48.3% / 24.8% against 58.3% / 36.7% / 17.5%.

## What the exit, the size, and the halt did with it

Read this as a drag on a selector that beat random **on this list**, not as a measured edge.

Per pick, a 30-session hold sold at the next open averaged +8.03% in the prior window (215 picks) and +8.51% in the recent window (219 picks). Adding the 5% stop cut those to +3.55% and +3.52%. The stop, not the overnight gap, is the giveback: −4.47 and −5.00 percentage points versus holding. About 136 of 215 and 150 of 219 picks stopped.

One account path, NAV starting at 1, 0 bps unless noted:

| Book | Prior window | Recent window | Prior two-sided turnover, annualized | Recent |
|---|---:|---:|---:|---:|
| 100% in the one name, no stop | +174.5%, DD 40.4% | +39.0%, DD 48.8% | 8.10 | 8.09 |
| 9.5% clips, up to 10 names, no stop | +21.3%, DD 17.3% | +18.4%, DD 12.0% | 2.94 | 2.96 |
| Same, plus the 5% stop | +7.0%, DD 12.4% | +9.6%, DD 9.9% | 3.66 | 3.58 |
| Same, plus the sticky 15% halt | +7.0%, DD 12.4% | +9.6%, DD 9.9% | 3.66 | 3.58 |
| Same, 5 bps per side | +6.6% | +9.2% | 3.65 | 3.58 |
| Same, 10 bps per side | +6.7% | +9.7% | 3.65 | 3.58 |

Sizing is why +174% and +5% per pick are different objects. One name, fully invested, compounds the right tail and draws down 40–49%. The clip book keeps most of the account in cash. The 5% stop then takes the clip book from +21.3% to +7.0% and from +18.4% to +9.6%. A year-long VTI hold on these same windows was about +17.0% and +13.1% at 5 bps per side. With the stop on, the clip book finishes behind that. The halt never fired once the stop was on, so its measured cost is zero. Five basis points per side costs about 0.4 percentage points of NAV. Ten versus five moved the path by noise.

## Why this still cannot prove an edge

The universe was generated on 2026-06-12. Names that had already won are in it. Names that died are not. Equal-weighting that list beat VTI by a wide margin in the earlier study. A ranker that beats a random member of a winning list has not beaten the market out of sample.

`prior_365` and `recent_365` were the pass/fail windows for local expectancy, 12-to-7, and the re-arm. The 1000-day supplement already covered 2024-01-29 onward. No untouched history is left in the panel. The clean window is the first 250 shared sessions after 2026-10-23, scored with `preregistration_pick_quality.json` unchanged.

## Free Alpaca SIP, and delistings

Alpaca’s Basic plan is free. Historical bars go back to 2016, and a SIP query is allowed when the end time is at least 15 minutes old. The latest 15 minutes of SIP is Algo Trader Plus, which this project will not buy. The assets endpoint can list `status=inactive`.

That is not a point-in-time universe. Delisted names that move to OTC have no free SIP bars, inactive symbols often return no history, and there is no delisting return. The asset list is who is inactive today, not who was listed on each past date. Free delayed SIP can widen the panel to names that still have consolidated bars. It cannot supply the dead names, and it cannot feed a live entry: the quote cap is 7 minutes.

## Single next change

Rerun this frozen test on the widest free Alpaca historical-SIP panel whose daily bars still exist, end time at least 15 minutes old, and write down the delisting hole. Do not change the stop, the hold, the size, or the halt on the way. Do not deploy.

The stop is the largest drag inside the book, about 5 percentage points per pick versus holding 30 sessions. It is the change to test only if that wider panel still shows the 20-session mean excess. Fitting the stop to this screener would spend the same tape again.
