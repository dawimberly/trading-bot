# local_expectancy — reject

**No edge.** Promotion is false. The 50% annual target is not claimed.

The ranker was frozen in `preregistration.json` before these numbers. One change: score eligible names by the mean net return of the 15 nearest matured baseline trades, instead of by ma50 extension. Same eligibility, stops, hold, and sizing. Fills are the next session's open. A quote older than 7 minutes is refused. Costs are 5 bps per side (the protocol's 10 bps round trip) and 10 bps per side (the doubled-cost gate that already rejected the other two overnight models).

`pairwise_rank` and `median_linear` were not rerun.

## What was actually tested

`portal_bot.py` only launches `run_paper_bot.py` or `run_all.py`. The checked-in score in `_equity_momentum_candidates` is `close / MA - 1` on the cycle matrix (50 rows). This study is the unfinished Lab Next challenger, on the protocol's daily clock, which is the free tape that covers the windows.

Free five-minute history stops inside the last two months. A Yahoo 5-minute window ending in June 2025 returns HTTP 422. The private 247-session SIP cache is absent from this repo, and paid SIP was left unbought. Daily adjusted Yahoo bars are one series (`adjclose / close` applied to open and close). Historical opens are modeled at quote age 0. Delayed SIP stays research-only; a live entry still needs a quote younger than 7 minutes.

The universe is the 2026-06-12 screener list. `SPCX` was dropped before any return, for fewer than 51 closes on or before 2024-10-23. Twenty-seven names remained. That list is a current list, so the equal-weight hold is flattered by survivorship. VTI is not.

## Required windows, net of cost

Returns are total return of a book that starts flat at NAV 1. Drawdown is peak to trough. The extension column is the simple baseline.

| Window | Cost / side | Learner | Extension | Equal-weight | VTI | SPY |
|---|---:|---:|---:|---:|---:|---:|
| 2024-10-24 → 2025-10-23 (250 sessions) | 5 bps | **−1.66%** / DD 17.8% | +0.23% / 18.3% | +59.3% / 28.0% | +17.0% / 19.3% | +17.1% |
| same | 10 bps | **−2.44%** / 18.1% | −0.64% / 18.5% | +59.1% | +16.9% | +17.0% |
| 2025-10-24 → 2026-10-23 (251 sessions) | 5 bps | **−0.95%** / DD 11.9% | +9.36% / 9.9% | +38.2% / 15.0% | +13.1% / 8.9% | +13.4% |
| same | 10 bps | **−2.43%** / 12.2% | +7.19% / 10.4% | +38.1% | +12.9% | +13.3% |

Sharpe, rf = 0, on the recent window at 5 bps: learner 0.03, extension 0.56, VTI 1.02. Halves on that window, learner then extension: +3.9% / −4.7% versus +4.0% / +5.1%.

Turnover (buys over average NAV): about 9x on the prior window and about 20x on the recent one, on roughly 90 and 210 round trips. Doubling the slippage made both books worse. It did not create the loss. The learner was already behind at 5 bps.

On the recent window at 5 bps the learner's drawdown was 2.03 percentage points worse than extension, past the 2.0 point gate. At 10 bps the gap was 1.86 points, inside the gate, and the return still failed.

## Why the prior window's second half is zero

Both books are flat from 2025-04-07 through 2025-10-23. Each had risen (peaks about +20% and +23% from the flat start) and then given back about 18%, through the frozen 15% entry halt. The halt does not reset. Positions exited and the books sat in cash. A tie at zero fails the frozen rule that the learner has to finish ahead. The learner was already behind (−1.66% versus +0.23% for the whole window, all of it in the first half).

The recent window is a separate flat start. Its max drawdown stayed under 15%, the halt did not fire, and the second half is a real loss (−4.7% versus +5.1% for extension).

The model was accepted on some refits (3 to 5) and rejected on others. It was off at the end of every window. The refits changed the book: on the recent window the learner took the extension result from +9.4% to −1.0%.

A 1000-day span (2024-01-29 → 2026-10-23, 688 sessions) was recorded and cannot rescue a failed window. At 5 bps the learner made +13.0% and extension +15.2%, against VTI +59.1% and the survivorship-biased equal-weight hold +168%. The same sticky halt flattens the second half of that continuous run.

Bootstrap (200 resamples) cannot promote anything. Medians on the required windows were negative.

## Verdict

Reject. The learner does not beat extension on either required window, at either cost. It loses to VTI on both, and to holding the same names. Nothing here is a 50% year, and nothing here is a reason to change a live, Medium, or Lab book.

The original five-minute definition is still unmeasured on these windows. The daily book trades less often than a five-minute book, so the cost hurdle is lower. It still failed. A five-minute rerun would add turnover, which is how the linear ranker, `pairwise_rank`, and `median_linear` already failed.

## Next hypothesis, one knob

**12-to-7 momentum.** On each decision, among names that pass the same eligibility gate, rank by the split-adjusted return from the session 12 months earlier through the session 7 months earlier. Skip the most recent seven months. Do not change stops, sizing, or the quote cap.

This was already written down in `scripts/research/nyse_12_7_shadow_plan.md` (commit `62afcb4`) and left inconclusive, because that checkout had no price panel. It is the open item from that note: one pre-registered sort, on a fresh window, with k, features, and weekdays left alone.

It is the useful next test because the failure that keeps repeating is cost, not a missing neighbor count. The linear model, the pairwise model, the median model, and now local expectancy all rank the same extension / volatility / RSI block. This run turned the book over 9–20 times and still lost to VTI. A slow sort has few round trips, so a real gross edge has room to survive 10–20 bps. The entry can be the session open, which is a fresh print, so the seven-minute quote cap stays. Daily bars can cover both protocol windows. One change. If it loses to VTI or to extension after costs on both windows, it joins the ledger as a reject.

That test was run as `twelve_seven_v1` and is recorded in `RESULTS_12_7.md`. Status: **reject**. It beats VTI on the recent window only. The prior window loses to VTI and to extension.
