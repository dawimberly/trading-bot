# Macro and sector regime — postmortem

Research note only (2026-10-25). No orders, no live or paper config change.
Protocol rules live on `origin/cursor/phase4-integration` (`best_strategies/PROTOCOL.md`):
one knob, two windows, costs on, no lookahead. This file does not re-run anything.

There is no committed test of “GDP growth versus the NYSE book.” GDP appears
only as a valuation input and as a date on a hardcoded release calendar.
War, inflation, sector rotation, and the yield gate were built. None of them
is a protocol pass.

## What was built

| Attempt | Code | What it actually does |
|---------|------|------------------------|
| Yield gate | `modules/macro_signals.py` `yield_gate_blocks`; same rule in `backtester_macro_hedge.py` | Block new SPY buys when `^TNX` is above its 50-day average and higher than 5 sessions ago. If TNX is missing, fall back to TLT below its 50-day average. Default `YIELD_GATE_ENABLED=true`. Live profile uses yield-gate-only. |
| Game plan | `modules/game_plan.py`, `backtester_macro_hedge.py`, `scripts/analysis/game_plan_ab_test.py` | Yield gate plus a metal sleeve (GLD/SLV/CPER), cash raised toward 25% on “stress,” and a 0.9 scale on longs. Stress is SPY under its long MA, or TLT weak, or RHYME bear/panic. |
| Regime-shift adaptor | `modules/macro_regime_adaptor.py` | 5-session oil surge (XOM/USO, default +8%), GLD surge with VIX above a floor, TLT weak and TNX up, or a headline keyword (war, Iran, Israel, sanctions, Hormuz, …). Tilts SPY/NYSE by at most ~10–15% and can buy GLD or XLE. Default **off**. `enforce_best_paper_stack` forces the paper flag false. |
| War event study | phase4 `scripts/analysis/geopolitical_event_study_last.md` plus `data/research/geopolitical/` | Ten hand-picked dates. Report of pre/post returns for SPY, VTI, XLE, GLD, VIX, WTI. Labels only. |
| Inflation “regime” | `modules/risk_parity_sleeve.py` `detect_economic_regime` | Four labels (growth, inflation, recession, balanced) from the RHYME string, a macro-stress flag, and 5-session oil/gold moves. Hand-set All Weather weights. Paper risk parity is forced off in the best-paper stack. |
| Sector momentum sleeve | `modules/sector_rotation.py`, `modules/sector_screener.py` | Top 2–3 sector SPDRs by momentum and relative strength vs SPY. Monthly or on regime change. Backtest path slices `window.iloc[:i+1]`, so the price rank itself is causal. Paper default is **on** (`SECTOR_ROTATION_ENABLED`). Live sleeve opt-in is off. |
| Sector momentum script | `scripts/research/backtest_sector_rotation.py` | Separate Monday top-2 of 11 SPDRs, 20-day momentum, optional SPY 10-day drawdown gate and MA200 filter. Fee 5 bps per side. |
| Hand-labeled sector blend | Log only: phase4 `stock-bot/sector_rotation_compare.txt` | “Defense/space, oil, AI, rate cuts.” Boost or trim inside a conservative blend. The strings are not in current `main` Python. The log is the evidence. |
| Buffett / GDP | `modules/bubble_risk.py` | Market cap / FRED `GDP`, forward-filled. A score, not a sleeve. |
| Release calendar | `modules/macro_calendar.py` | Hardcoded NFP/CPI/FOMC/PPI/GDP dates from June through December 2026. Cuts size before the print. Not a historical regime model. |

No pull request is a macro backtest. PR 7 (open) is the v1.5.4 lock, which leaves the regime adaptor off. The geo study and the sector A/B log are on `origin/cursor/phase4-integration`, not on `main`.

## How each one was tested

| Attempt | Window | Walk-forward | PIT / as-of | Costs | Result |
|---------|--------|--------------|-------------|-------|--------|
| Game plan vs yield-gate-only vs baseline | 2017-07-20 → 2026-06-02; calendar 2022; last ~750 sessions to 2026-06-02 | Three windows, not purged folds. 2022 sits inside the long window. | Daily fund plus yfinance metals/yields. Not S&P membership. Adjusted prices. | Metal/hedge fills use `TX_COST=0.001` (10 bps per side). The memo does not print a cost line. The yield gate itself is a skipped buy, so its “cost” is the trades it did not take. | **Not a stable edge.** Full sample: full plan +257.01% / Sharpe 0.80 / DD −24.83% vs baseline +259.75% / 0.75 / −25.10% (−2.74 pp, +0.05 Sharpe). 2022: plan −13.16% vs baseline −21.40%, DD −19.25% vs −26.96%. Recent 750d: plan +37.99% vs baseline +44.90% (−6.91 pp), same Sharpe 1.03. Yield-gate-only is a wash: −0.59 pp, +3.65 pp, and −0.05 pp. The memo averaged Sharpe (0.37 vs 0.33) and said keep the full plan. Later `OPTIMIZED_SYSTEM_SUMMARY.md` and the recommended-stack note walked that back to yield-gate-only because metals dragged the recent window. |
| Regime-shift adaptor | `run_regime_shift_compare` exists. **No result file is committed.** | n/a | Price series can be sliced with `ts`. Geo text is not. | Hedge buys in `run_macro_regime_backtest_day` pass `tx_cost=0.0`. | **Untested in git.** Product default is off. |
| War study | 10 events, 1990-08-02 through 2024-04-13. Horizons: prior 20d, next 5/20/60d. | No. Each date is one story. | FRED VIX and WTI; yfinance SPY/VTI/XLE/GLD. Several early events lack GLD or even SPY (1990 is macro-only). | None. Not a strategy. | **Do not trade it.** The writeup says report only, no promote. Equity did not share one sign: Ukraine 2022 SPY was −4.55% into the day and +1.71% over the next 20 sessions; XLE was +6.53% over 20 sessions. Soleimani 2020: XLE −5.17% over 20 sessions and −24.40% over 60, while SPY was +2.89% over 20. Gaza 2023: SPY −5.00% over 20 sessions, GLD +7.71%. Oil and energy flip by war. |
| Risk-parity inflation label | No committed A/B. | n/a | Uses the same price window as the book. | n/a | **Off.** Classifying “inflation” from oil that already rose 4% in 5 sessions describes the move you would have needed to hold beforehand. |
| Integrated sector rotation | One 365d window, 2025-08-13 → 2026-06-18, 310 bars. Header says strict PIT plus thinking and news. | No second window. | Claimed strict PIT for the book. The rotation rules are hand-named themes, not a vintage macro series. | Stack costs (the log does not restate bps). | **Fail.** No rotation +59.60% / 2.05 / −7.24%. Conservative rotation +41.78% / 1.91 / −6.31% (**−17.82 pp**, −0.14 Sharpe). Full hybrid +24.61% / 1.42 (**−34.99 pp**, −0.63 Sharpe). Log line: keep sector rotation off on that window. VTI buy-and-hold in the same log was +16.15%, so the book’s return is the stack, not the sector sleeve. |
| Standalone top-2 sector script | Code supports `--days`. Committed CSV is a trade blotter from 2024-09-23, not an equity curve. | No. | Fixed list of today’s 11 SPDRs (XLC did not exist before 2018). The saved blotter starts in 2024, so that file does not show the inception hole. | 5 bps per side in code. | **No verdict.** Summary return and Sharpe were printed and not saved. |
| Buffett GDP ratio | Used as a live/paper score when enabled. No sleeve A/B. | n/a | `fredgraph.csv` is the **latest** vintage. `_ratio_for_date` only truncates that revised series. Quarterly GDP is forward-filled, including onto days before the release. | n/a | **Not evidence** for a GDP-versus-NYSE rule. A backtest that calls it with an old `as_of` still sees later revisions. |
| Macro calendar | Live clock only. Dates start June 2026. | Cannot walk history. | The calendar is a typed list, not ALFRED. | Sizing scale, not a measured cost. | **Unusable as a historical test.** |

## Why it did not work

These are separate failures. They compound when someone stacks them into one “macro” switch.

1. **The 2022 win was the whole story.** The full game plan helped the bear year and gave back more than that in the next bull window. Averaging three Sharpes, one of which contains 2022 twice (inside 2017–2026 and again as its own row), promoted a hedge that does not pay for itself across regimes. That is overfitting to one inflation shock, not a second disjoint pass.

2. **“Inflation” and “war” were coded as prices and keywords that move with the trade.** A 5-day oil jump, GLD already up, or the word “war” in today’s news cache is not a regime you knew before the sector moved. The adaptor’s geo path calls `_load_news_text()` on the current sentiment file whenever the caller omits `news_text`. `run_backtest` does omit it. Every historical bar can inherit today’s headline. Price series are sliced with `ts` when the caller passes one; the text is not.

3. **Too few regimes, and the labels disagree.** The war file has ten dates and at least three different XLE/SPY patterns. Risk parity has four hand-set mixes. The sector blend named four themes (defense, oil, AI, cuts) with no published rule for when each theme starts or ends. A classifier with a handful of hand states will fit the last crisis.

4. **GDP was never a tradable series.** FRED `GDP` on the public graph is revised. Forward-fill treats the quarter-end observation as known that day. The release calendar does not go back before 2026, so it cannot replay “what NYSE did after a GDP print.” There is no NYSE-versus-GDP table to fail or pass.

5. **Sector momentum was either unmeasured or lost to the stack.** The causal top-N rank in `sector_rotation.py` is the cleanest implementation of the set, and the one integrated 365d test of a sector overlay still lost 18 to 35 percentage points to the same book without it. The standalone script’s result was not archived. Keeping `SECTOR_ROTATION_ENABLED` true on paper after that log is a product default, not a passed walk.

6. **Costs and books were uneven.** The regime sleeve can trade with zero transaction cost. The game-plan metal sleeve pays 10 bps per side and still failed the recent window. Comparing those to a yield gate, which mostly refuses trades, mixes three different cost models.

7. **Data holes.** 1990 has no SPY/XLE/GLD in the war study. GLD starts in 2004. XLC starts in 2018. VIX and WTI from FRED are fine as context and were correctly labeled `macro_only` when the equity proxies were missing. They were not turned into a position rule, which is the right call given the sign flips.

## Do not re-run

- Full game plan (metals + stress cash + 0.9 scale) as a promote. The recent window already killed it.
- Yield-gate-only as a new idea. It is already a wash on three windows. Leave the live flag where the owner set it; this note does not change it.
- Keyword war / Hormuz / today’s news cache as a feature.
- 5-day oil or gold surge as an inflation classifier.
- Hand-labeled AI, defense, or rate-cut sector tilts.
- Buffett ratio on latest-vintage FRED GDP as a historical signal.
- The 2026-only macro calendar as a backtest.

## One frozen hypothesis

**Name:** `cpi_vintage_xle_xlu`

**Hypothesis:** The 12-month CPI inflation rate that had already been published, and no other macro input, picks the next 21 sessions of energy versus utilities. When that as-of print is above its own trailing 36-month median of as-of prints, the sleeve holds XLE. Otherwise it holds XLU.

**Why this knob:** Prior “inflation” rules watched oil and gold after they had already moved, which is the sector return in disguise. CPI is the series those rules were trying to name. It is monthly, free, and has a real-time vintage. GDP is the wrong first test: it is quarterly, revised for years, and this repo never scored it against the book.

**Book:** research sleeve only. Not the live book, not Medium, not the NYSE pick_15 walk.

**Baseline:** buy and hold SPY over the same sessions. One sleeve, 100% in either XLE or XLU. No stack, no HMM, no news, no yield gate, no GDP.

**Knob:** the CPI comparison above. Nothing else. Do not add a vol target, a stop, or a third ETF after seeing the result.

**Data, as known that day:**

- CPI: FRED `CPIAUCSL` through ALFRED vintages (or the vintage-date API). On session date `t`, use the vintage whose realtime period contains `t`. The 12-month change uses only observations inside that vintage. Do not use `fredgraph.csv` latest vintage and do not forward-fill a quarter onto days before the release.
- Prices: XLE, XLU, and SPY daily closes. A name is ineligible before its first listed session (XLE December 1998, XLU December 1998, SPY January 1993). No adjusted/unadjusted mix: use one adjustment convention and keep it for all three.
- No membership file is required. This is three funds, not a stock universe. If a session is missing for the held fund, hold cash for that session rather than borrowing the other fund’s return.

**Windows (both required):**

- W1: first session on or after 2005-01-03 through 2014-12-31.
- W2: 2015-01-02 through 2024-12-31.

W2 contains the 2021–2022 inflation spike. W1 does not. A pass on W2 alone is the game-plan mistake and is a kill.

**Costs:** 10 bps round trip (5 bps per side) charged when the sleeve changes fund or leaves cash. SPY baseline is one entry and one exit over the window, same 10 bps round trip.

**Pass (all of them):** sleeve total return above SPY on W1 and on W2; sleeve max drawdown not worse than SPY by more than 2.0 percentage points on each window; costs on; no other knob.

**Kill:** fails either window; the edge is only 2021–2022 inside W2; turnover is so high that gross beats SPY and net does not; or the result flips when the median lookback moves from 36 to 24 or 60 months (that check is a kill test, not a search for a better number).

**Not in this test:** war dates, keyword news, TNX yield gate, GDP, Buffett ratio, sector momentum rank, NYSE names, and the paper stack. If this passes, the next note can ask whether the same as-of CPI print changes the pick_15 book. That is a different knob.
