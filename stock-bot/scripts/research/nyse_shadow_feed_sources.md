# Research-only feed for the NYSE baseline versus 12-to-7 shadow

**Decision: inconclusive.** No market data was fetched. No tracker was written or started. No subscription was purchased and no credential was requested. No strategy return is reported. The candidate is not rejected and is not promoted. Trading code, production settings, journals, and files under `sentiment/sources/` were not changed. No broker was contacted.

This note checks the feed rules in `scripts/research/nyse_12_7_shadow_plan.md` against the order code, compares official data sources, and revises the observation protocol before any collection. The production baseline is unchanged.

Checked on 2026-10-12. Prices below are the figures printed on the cited pages that day. A page that did not show a figure is marked unknown. A configured key is not a subscription, and a subscription is not a data entitlement.

## 1. Feed requirements against the order code

### Baseline score, preserved

`_equity_momentum_candidates` (`modules/pipeline_strategies.py` lines 986–994) sets `ma_window = 20 if ipo else 50`, then `prices.rolling(window=window).mean()`. The sorted score is `current / ma - 1`. A name stays in the list when `current > ma * (1 - NYSE_MA_ENTRY_TOLERANCE_PCT)`. The checked-in tolerance is `0.01` (`config.py` line 1485). The function does not resample and does not call `effective_nyse_ma_window()`.

The live and paper cycles pass `load_live_close_matrix()` through unresampled (`run_all.py` lines 709 and 1630–1640). `modules/real_time_data.py` is not in this checkout, and no live-matrix snapshot is on disk. **The bar spacing of that score is unproven.** The score file must store timestamps and the observed median spacing. It must not be labeled 5-minute or 50-session.

### Candidate score, preserved

Signal date is the NYSE session date of the decision.

`score = split_adj_close(S7) / split_adj_close(S12) - 1`

- `S12` is the last NYSE session on or before 12 calendar months before the signal date.
- `S7` is the last NYSE session on or before 7 calendar months before the signal date.
- Both endpoints use the same split-only series. Cash dividends stay in a separate field and are not multiplied into this ratio.
- A weekend or exchange-holiday row is not a session.
- A missing endpoint, or a missing split factor for that name, removes the name from **both** books on that decision.

No other lookback is registered.

### Remaining discrepancies

1. **The written “50 rows required” rule is stricter than the order function.** Lines 990–991 set `window = min(ma_window, len(prices))`. A non-IPO is skipped only when fewer than 20 rows remain (line 986). An IPO is skipped only when fewer than `IPO_MIN_TRADING_DAYS` remain. That checked-in default is 5 (`modules/dynamic_universe.py` line 27); the IPO window is then below 30 trading days (lines 26 and 309–310). The 50-row and 20-row figures are the target windows. A non-IPO with 20–49 rows is scored on that shorter mean. The shadow must follow this function. It must not invent rows to force the window to 50, and it must not drop a name the function would have scored.

2. **The previous fill tape had one `quote_time`.** The shadow plan and the readiness note, before this revision, stored a single quote timestamp. The comparison needs an event time and a receipt time so a stale or delayed quote can be excluded. Alpaca’s quote object has only `t` ([historical quotes](https://docs.alpaca.markets/us/reference/stockquotesingle-1)). A historical download of that object cannot grow a second timestamp after the fact. Sections 4 and 5 of the shadow plan now require both timestamps.

3. **Five basis points on top of the bid or ask double-counts the spread.** Before this revision, the shadow plan and the readiness cost row applied `effective_execution` (`modules/backtester_core.py` lines 300–305) to every executable fill. That helper multiplies buys by `1 + slip` and sells by `1 - slip`, with checked-in equity slippage of 5 bps and equity commission of 0 (lines 29–31). `execute_order` does not add that slippage (`modules/alpaca_executor.py` lines 1522–1527 and 1564–1568). A quote fill already pays the spread by buying the ask and selling the bid. Adding 5 bps on that price counts the spread twice. The revised cost rule is in section 4 of this note. The backtester helper itself is unchanged.

4. **`adj_close` can be read as dividend-adjusted.** The candidate ratio is split-only. Alpaca `adjustment=all` applies split, cash-dividend, and spin-off adjustments together, and `adjustment=dividend` adjusts price for cash dividends ([historical bars](https://docs.alpaca.markets/us/reference/stockbars)). Those settings must not be used for this ratio. Databento’s combined adjustment-factor series is also the wrong input when the factor includes a cash dividend ([adjustment factors](https://databento.com/docs/venues-and-datasets/adjustment-factors)).

5. **A vendor tape is not the live matrix.** `fetch_and_store` requests `interval="5m"` (`fetch_data.py` lines 34–46) and the daily backtester uses `load_close_matrix(interval="1d")` (`modules/backtester_core.py` lines 180, 206, and 217). Neither call is `load_live_close_matrix()`. Until a snapshot of that function exists, any vendor close file used for the 50-row score is a research proxy. The report must say so. It does not prove the running bot’s bar spacing.

6. **The daily backtester still fills the signal row.** `backtester.py` line 1566 takes `prices = data.iloc[i]`, lines 2020–2025 build `BacktestExecutor` on that row, and `trade` (line 709) fills it. That same-row fill is not the shadow fill.

7. **The previous sample gate was weaker than this protocol.** Before this revision, the shadow plan called the result inconclusive if either book had no completed round trip. It did not set a minimum trade count above zero, and it did not define a paired daily-return comparison. Both are registered in section 6 below and in the shadow plan’s section 7.

8. **No source in this checkout can supply the feed.** `stock-bot/.env` and the repo `.env` are absent. `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, `PAPER_APCA_API_KEY_ID`, `PAPER_APCA_API_SECRET_KEY`, `POLYGON_API_KEY`, `TIINGO_API_KEY`, `NASDAQ_DATA_LINK_API_KEY`, `FMP_API_KEY`, `DATABENTO_API_KEY`, and `MASSIVE_API_KEY` are unset in this process. `.env.example` lines 19–20 are the placeholders `your_paper_key_id` and `your_paper_secret_key`. No secret value was read. An unset key is not a subscription, and this note does not assume an entitlement.

## 2. Sources that are not the fill tape

| Source | Why it is not used for this feed |
|--------|----------------------------------|
| yfinance via `fetch_data.py` | Close only. The daily path sets `auto_adjust=True` and then drops every column except Close (lines 20–31 and 67–77). No bid, ask, event time, or receipt time. Running it would be a fetch. |
| Alpaca Basic | Free. Real-time equities coverage is IEX only. Historical SIP quotes and trades in the latest 15 minutes require Algo Trader Plus. Without that plan, historical `end` must be at least 15 minutes old for SIP ([About Market Data API](https://docs.alpaca.markets/us/docs/about-market-data-api), [Market Data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq)). A 15-minute delay fails the receipt-lag rule below. IEX is one exchange, not the consolidated bid and ask. |
| Massive Stocks Basic, Starter, and Developer | Quotes are not included. Starter and Developer aggregates are 15-minute delayed. Basic aggregates are end-of-day ([quotes](https://massive.com/docs/rest/stocks/trades-quotes/quotes), [custom bars](https://massive.com/docs/rest/stocks/aggregates/custom-bars)). Starter is $29/month and Developer is $79/month on [the stocks page](https://massive.com/stocks). |
| Nasdaq Data Link, Sharadar, CRSP, FMP, Tiingo | No key is configured. This pass did not retrieve an official quote schema from any of them with both an event timestamp and a receipt timestamp plus explicit split factors. They are not viable on the evidence collected. Remaining details are unknown. |

## 3. Viable sources

A source is listed here only if its official docs show a path to timestamped US equity quotes or OHLC for a fixed US list plus SPY, a way to keep prints strictly after a decision, at least 12 months of daily history, and an explicit split factor. Coverage of the checkout’s 30-name list, including SPCX, was not looked up symbol by symbol. A missing symbol later removes that name from both books. It does not change the universe definition.

### Databento EQUS.MINI plus split events

| Item | Documented fact |
|------|-----------------|
| Historical depth | EQUS.MINI history starts 2023-03-28. That is more than 12 months before 2026-10-12. Historical access is usage-based or included with any subscription ([announcement](https://databento.com/blog/databento-us-equities-mini-now-available)). |
| Quote timestamps | `ts_event` is the matching-engine-received timestamp. `ts_recv` is the capture-server-received timestamp. Both are nanoseconds since the Unix epoch. `ts_in_delta` is always 0 on this dataset ([EQUS.MINI spec](https://databento.com/docs/venues-and-datasets/equs-mini)). |
| What the quote is | One aggregated BBO per instrument, not a per-venue quote. Size is summed at the best price, including odd lots. `bid_ct_00` and `ask_ct_00` are always 0. The publisher id does not identify the source venue. |
| Bar timestamps | Schemas include OHLCV-1s, 1m, 1h, and 1d. The rendered OHLCV schema page did not return bar-edge text on 2026-10-12. Which timestamp is the left edge of an OHLCV bar is **unknown** from the page retrieved here. Do not use those bars as the fill until that edge is documented. |
| Adjustment | The EQUS.MINI spec describes trade and BBO aggregates. It does not document a split or dividend adjustment on those records. Corporate-action history is a separate dataset from 2018-05-01, updated 4 times per day ([corporate actions](https://databento.com/docs/venues-and-datasets/corporate-actions)). Forward and reverse splits are `FSPLT` and `RSPLT`, with `ratio_old`, `ratio_new`, and `ex_date`. The NVDA example is `ratio_old=1`, `ratio_new=4` for the 4-for-1 split ([splits example](https://databento.com/docs/examples/corporate-actions/splits-and-reverse-splits)). `ratio_new` for `FSPLT` includes existing holdings, so the ratio can differ from a press release. `FSPLT` can also represent a stock dividend, bonus, or subdivision. Cash dividends are `DIV` events and must stay out of the price ratio. The combined adjustment-factor dataset can include cash dividends; do not use that combined factor for this score. |
| Delay | Live EQUS.MINI is included with Standard, Plus, or Unlimited of the Databento US Equities service. The announcement describes it as real-time and says users do not sign a separate exchange license for this derived feed. Measured lag is **unknown**. |
| Coverage limits | Derived from proprietary direct feeds of RegNMS venues and ATSs. Not the CTA/UTP SIP. Symbol convention is Nasdaq (`BRK.B`). Whether every name in `get_nyse_universe_fixed()`, including SPCX, is present is **unknown**. |
| Access | A Databento API key. Live data requires a subscription. Historical data can be usage-based. No key is configured here. |
| Cost | The January 13, 2025 plan post lists Standard at **$199/month**, with 7 years of OHLCV, 12 months of L0 and L1 history, 1 month of L2 and L3, and live data without exchange license fees ([plan post](https://databento.com/blog/introducing-databento-us-equities); the December 11, 2024 notice states the same $199 figure, effective January 13, 2025). The live [pricing page](https://databento.com/pricing) and the [US equities catalog](https://databento.com/catalog/us-equities) did not render dollar amounts on 2026-10-12. The current listed price is **unknown**. Usage-based historical dollars per gigabyte are **unknown** from those pages. Plus and Unlimited prices are **unknown**. |

`ts_recv` is the vendor capture receipt. It is not the clock time of a later research process. A prospective capture must also store that local receipt.

### Massive Stocks Advanced (formerly Polygon)

Polygon.io endpoints continue to work. Massive is the same platform ([stocks page](https://massive.com/stocks)).

| Item | Documented fact |
|------|-----------------|
| Historical depth | Quote records on Advanced date back to September 10, 2003, described as all history ([quotes](https://massive.com/docs/rest/stocks/trades-quotes/quotes)). Custom-bar history on Advanced is all history, with records back to September 10, 2003; Basic is 2 years, Starter 5 years, Developer 10 years ([custom bars](https://massive.com/docs/rest/stocks/aggregates/custom-bars)). The stocks page also says tick quotes back to 2003 and corporate actions back to 2008. That marketing sentence is not a coverage proof for this 30-name list. |
| Quote timestamps | `participant_timestamp` is when the exchange generated the quote, nanosecond Unix. `sip_timestamp` is when the SIP received it. `trf_timestamp` is when a trade-reporting facility received it ([quotes](https://massive.com/docs/rest/stocks/trades-quotes/quotes)). |
| Bar timestamps | Custom-bar `t` is the Unix millisecond timestamp for the **start** of the window. The endpoint is described as Eastern Time. An empty interval is omitted when no eligible trade occurs. `bar_end` is that start plus the requested multiplier and timespan. |
| Adjustment | The aggregate `adjusted` flag defaults to true and is documented as adjustment **for splits**. Whether that flag also includes cash dividends is **unknown**. Do not use `adjusted=true` as the candidate close. Use the splits endpoint: `execution_date`, `split_from`, `split_to`, `historical_adjustment_factor`, and `adjustment_type` of `forward_split`, `reverse_split`, or `stock_dividend` ([splits](https://massive.com/docs/rest/stocks/corporate-actions/splits)). The documented adjustment is: for a price on date D, multiply by the `historical_adjustment_factor` of the first split whose `execution_date` is after D. Split history on Starter and above is all history; Basic splits are 2 years. A cash-dividend amount is not a field of this splits response. |
| Delay | Advanced quotes are real-time. Starter and Developer aggregates are 15-minute delayed. Real-time also requires exchange agreements signed in the dashboard, separate from payment. Professional use needs a business license ([subscription article](https://massive.com/knowledge-base/article/what-are-the-different-massive-subscriptions-i-can-use)). |
| Coverage limits | Quotes are NBBO. Individual plans, Basic through Advanced, are licensed for personal and non-professional use. Business is $2,499/month. Whether SPCX is in the catalog is **unknown**. |
| Access | Quotes require Stocks Advanced or a business plan that includes them. Starter and Developer do not include quotes. No Massive or Polygon key is configured here. |
| Cost | Starter **$29/month**, Developer **$79/month**, Stocks Business **$2,499/month** ([stocks page](https://massive.com/stocks)). The Advanced monthly price was not on the stocks-page text retrieved here, and [the pricing page](https://massive.com/pricing) showed a $99/month figure only for the Benzinga dataset. Stocks Advanced price is **unknown**. |

`sip_timestamp` is the SIP’s receipt, not the research process’s receipt.

### Alpaca Algo Trader Plus

| Item | Documented fact |
|------|-----------------|
| Historical depth | Trading API equities history is “since 2016” on Basic and on Algo Trader Plus ([About Market Data API](https://docs.alpaca.markets/us/docs/about-market-data-api)). That covers 12 months of daily bars. |
| Quote timestamps | One field, `t`, RFC-3339 with nanosecond precision. Bid and ask are `bp`/`bs` and `ap`/`as`. There is no second timestamp. Whether `t` is the participant time or the SIP time is **unknown** ([historical quotes](https://docs.alpaca.markets/us/reference/stockquotesingle-1)). |
| Bar timestamps | Minute and daily bars are aggregated from trades. The SIP timestamp of the trade is truncated to the minute, or to the New York day. The bar timestamp is the left edge: 14:52:00 contains trades from 14:52:00 inclusive to 14:53:00 exclusive ([Market Data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq)). |
| Adjustment | Bar `adjustment` values are `raw` (the default), `split`, `dividend` (cash dividends), `spin-off`, and `all`. They can be combined, for example `split,spin-off` ([historical bars](https://docs.alpaca.markets/us/reference/stockbars)). The candidate close uses `split` only. Corporate actions include `forward_split` and `reverse_split` with required `old_rate` and `new_rate`, plus separate `cash_dividend` and `stock_dividend` types. Alpaca gives no guarantee on when a corporate action appears, and delays are possible ([corporate actions](https://docs.alpaca.markets/us/reference/corporateactions-1)). The checkout’s `_fetch_alpaca_daily_closes` does not set an adjustment argument (`modules/data_loader.py` lines 248–253). It was not run. |
| Delay | Algo Trader Plus has no “latest 15 minutes” SIP restriction and covers all US stock exchanges. Basic real-time equities data is IEX only, and SIP history must end at least 15 minutes in the past. |
| Coverage limits | Plus websocket subscriptions are unlimited symbols; Basic is 30. Plus historical calls are 10,000 per minute; Basic is 200. Feeds include `sip`, `iex`, `boats`, and `otc`. The quote query also accepts `asof` for symbol renames. SPCX coverage is **unknown**. |
| Access | Header pair `APCA-API-KEY-ID` and `APCA-API-SECRET-KEY`. Those env names are unset here. A paper or live trading account on the Basic plan is not Algo Trader Plus. This note does not assume the entitlement. |
| Cost | Algo Trader Plus is **$99/month**. Basic is free ([About Market Data API](https://docs.alpaca.markets/us/docs/about-market-data-api)). |

Alpaca can supply the split-adjusted daily score today, on paper, once an entitled key exists. It cannot supply a historical quote that already contains both an event time and a receipt time. A prospective capture can store `t` as the vendor time and stamp local receipt when the research process stores the message. That is the only Alpaca quote path that meets section 5.

## 4. How a print is kept after the decision

`T` is the decision time. The signal row is the last score-file close with `bar_time <= T`. That row is an input to the baseline score. It is never the fill.

Official session bounds come from the NYSE hours calendar ([NYSE hours and calendars](https://www.nyse.com/markets/hours-calendars)). The page fetched on 2026-10-12 lists a core session of 9:30 a.m. to 4:00 p.m. Eastern Time, and early closes at 1:00 p.m. Eastern Time on the days it marks. `bar_end` uses that day’s official close. A weekend or a holiday on that page is not a session. The calendar file itself was not saved into the repo.

**Quote path, preferred when an eligible quote exists.** Store:

- `event_time`: exchange or matching-engine time (`ts_event`, or Massive `participant_timestamp`). For Alpaca this is `t` only if a prospective capture has no better event field, and the file must say that `t`’s meaning is unknown.
- `vendor_receipt_time`: `ts_recv` or Massive `sip_timestamp`, when the vendor publishes one. Alpaca historical quotes leave this null.
- `local_receipt_time`: clock time when the research process stored the record. Required for any prospective capture. Null on a bulk historical file, and it must not be invented.

`available_time` is `local_receipt_time` when that field is present, otherwise `vendor_receipt_time`. A quote with neither receipt time is unusable. A quote is eligible only when all of the following hold:

- `event_time > T`
- `available_time > T`
- `available_time - event_time` is less than or equal to **2.000 seconds**
- the side used has price > 0 and size > 0
- `ask >= bid` when both prices are present

The fill is the first eligible quote in `available_time` order, then `event_time`. The buy price is the ask. The sell price is the bid. The recorded fill time is `available_time`. A quote that was generated after `T` but delivered later than the 2-second ceiling is excluded, not delayed into a fill. A documented 15-minute delay fails this ceiling.

The 2.000-second ceiling is pre-registered here, before any collection. It is an eligibility filter. It is not an extra cost in basis points, and it is not a measured vendor latency.

**OHLC path, only when no eligible quote exists.** The fill is the `open` of the first bar with `bar_start > T` and `bar_end` at or before that session’s official close. A bar that contains `T` (`bar_start <= T < bar_end`) is not a fill. Its open can be a trade from before `T`.

For Alpaca and Massive, `bar_start` is the documented left edge and `bar_end` is `bar_start` plus the bar size. The first 1-minute bar after a decision at exactly 10:00:00 starts at 10:01:00. Databento OHLCV is not used for this path while its bar edge is unknown.

Either price is a **simulated fill proxy**. It is not an Alpaca order print, not a call to `execute_order`, and not evidence of what a market order would have received.

**Costs, without counting the same friction twice.**

| Component | Quote fill | OHLC-open fill |
|-----------|------------|----------------|
| Spread | Paid by buying the ask and selling the bid. | Not in the open. Apply the checked-in 5 bps once: buys multiply by `1.0005`, sells by `0.9995`. |
| Fees | Equity commission 0 bps, the checked-in research default (`modules/backtester_core.py` line 31). This is not a broker invoice. | Same 0 bps. |
| Latency | The 2-second receipt ceiling excludes the quote. No added bps. | No added bps. The bar must still start after `T`. |
| Slippage | No further slippage. The 5 bps term is not applied. | The 5 bps term is the only slippage. No second half-spread. |

If an eligible quote exists, use the quote path only. Do not also apply 5 bps. Unfilled decisions stay unfilled and count against fill coverage.

## 5. Local read-only schema and checks

Directory, not created here: `scripts/research/feeds/`. The tracker is still not implemented. Nothing under this directory was written.

`nyse_shadow_score_closes.jsonl`, one object per symbol per close the baseline function would roll:

- `symbol`, `bar_time` (ISO-8601, `America/New_York`), `close`
- `source` is `live_matrix_snapshot` or `research_proxy`
- `median_spacing_seconds` on the file’s sidecar, measured, not assumed

Rows with `bar_time <= T` are the only rows in the mean. The window length is the order function’s `min(target, len)`, not a forced 50. The file covers `get_nyse_universe_fixed()` and SPY. A `research_proxy` file does not prove the running bot’s matrix.

`nyse_shadow_fills.jsonl`:

- Quotes: `symbol`, `event_time`, `vendor_receipt_time`, `local_receipt_time`, `bid`, `ask`, `bid_size`, `ask_size`, `source`
- OHLC, only as the fallback: `symbol`, `bar_start`, `bar_end`, `open`, `high`, `low`, `close`, `source`

`nyse_shadow_sessions.jsonl`, one object per symbol per NYSE session, history starting at least 12 calendar months before the first signal date:

- `session_date`, `symbol`, `unadj_close`
- `split_adj_close`, built only from `unadj_close` and the split events below
- `split_factor` (1 when no split applies), `split_source`
- `cash_dividend` (0 when none), kept out of `split_adj_close`

`nyse_shadow_splits.jsonl`: `symbol`, `ex_date` or `execution_date`, `ratio_old`, `ratio_new`, `event_type`, `source`. Cash-dividend rows do not go in this file.

`nyse_sessions.txt`: the official NYSE session dates used to choose `S7` and `S12`, including early closes. Saturday, Sunday, and holiday dates are absent.

`adjustment.json` names the method: split ratios only, cash dividends excluded, both endpoints from `split_adj_close`.

Checks that must pass before any performance number. Weekend and holiday rows are in neither the numerator nor the denominator.

| Check | Pass rule |
|-------|-----------|
| Score rows | For every in-universe name the order function would score, the file has the rows that function would roll, and no later row. A non-IPO with fewer than 20 rows, or an IPO with fewer than the checked-in 5, is absent from both books. Spacing is reported and not relabeled. |
| 12-to-7 | At least 90% of in-universe name-decisions have both split-adjusted endpoints. A missing endpoint or missing factor removes that name from both books and counts as uncovered. |
| Fills | At least 90% of entry and exit decisions have an eligible quote, or an OHLC open under the rule above, strictly after `T` and at or before the official close. |
| Quote freshness | Every quote used as a fill has `event_time`, an `available_time`, a positive lag, and a lag of at most 2.000 seconds. Duplicate `(symbol, event_time, source sequence)` keys fail the file. |
| Calendar | Session dates match `nyse_sessions.txt`. A Saturday, Sunday, or holiday row fails the file. |
| Stale prices | A session close unchanged versus the prior session for 5 or more consecutive sessions is flagged. If flagged name-sessions exceed 2% of the panel, stop. |
| Corporate actions | A split-adjusted move larger than 25% versus the prior session, without a split event on or between those sessions, fails that name-session. |
| Same books | Same decision times, eligible names, limits, sizing, exits, and fills. Only the sort key changes. |
| Proxy label | If `source` is `research_proxy`, the report says the live matrix was not observed. |

## 6. Revised observation protocol

Registered on 2026-10-12, before any feed file is collected and before any tracker starts. Not revised after the first decision timestamp is written into a feed file.

- The performance window is **at least six calendar months** of prospective NYSE sessions, beginning on the first session after the checks in section 5 pass.
- Six months is a minimum window. It is not proof of future profitability. Completing it does not authorize a live or paper change.
- **Minimum sample, also pre-registered.** Each book must finish at least **30 completed round trips**. A completed round trip is one name that goes from flat to a position on an entry fill and later returns to flat on an exit fill, both fills inside the window. A position still open at the window’s end is not completed. The books do not need the same names.
- **Paired daily returns, also pre-registered.** A session counts only when both books have a finite daily return. Book equity at a session close is cash plus open positions marked at that session’s `split_adj_close`. The same close marks both books. That mark is not the fill price. The daily return is equity at this session close divided by equity at the previous NYSE session close, minus 1. The comparison series is the candidate return minus the baseline return. At least **120** paired sessions are required. Six months of NYSE sessions is about 126 sessions, so this gate asks for nearly the whole window.
- If fill coverage, 12-to-7 coverage, or score-row coverage is under 90%, **or** if either book has fewer than 30 completed round trips, **or** if fewer than 120 paired sessions exist, the result is **inconclusive**. No return, Sharpe, drawdown, or turnover is reported in that case.
- Meeting the six-month window, the 30-round-trip floor, and the 120-session floor does not prove that the candidate will be profitable, and it does not prove that it will beat the baseline out of sample. If those gates pass, the note may show the paired difference series, its mean, the trade counts, the cost totals under section 4, max drawdown, turnover, and coverage. That note still does not change the bot.
- Interim notes, before the window ends, list diagnostics only: row counts, missing endpoints, duplicate timestamps, stale sessions, unfilled decisions, receipt-lag failures, feed gaps, and exceptions. They do not include a return and they are not used to change the lag ceiling, the trade floor, the bands, the caps, or the exits.
- The earlier 35-day burn-in, 60-session window, and 20-round-trip gate stay retired. The 35-day figure remains only the checked-in exit-optimization hold cap (`config.py` line 1194). It is a position rule, not this window.

## 7. Ranked recommendation

The comparison stays **inconclusive**. No feed exists, so no observation exists.

1. **Databento EQUS.MINI quotes, plus `FSPLT` and `RSPLT` ratios.** This is the only source whose fetched spec already has both an event timestamp and a vendor receipt timestamp, raw prices, and split ratios kept apart from cash dividends. Daily session closes for the candidate can be the last EQUS.MINI trade with `ts_event` inside the official NYSE session, then split-adjusted with those ratios. Do not use the combined adjustment-factor file. Do not use EQUS.MINI OHLCV as the fill while the bar edge is unknown. The $199 Standard price is the January 2025 announcement, not a price re-read from the live pricing page on 2026-10-12.

2. **Massive Stocks Advanced NBBO, plus the splits endpoint.** `participant_timestamp` and `sip_timestamp` support the same event-versus-receipt test, and the splits fields are explicit. Quotes are not on the $29 or $79 plans. The Advanced dollar price is unknown, and real-time use also needs a signed exchange agreement. `sip_timestamp` still is not the local receipt.

3. **Alpaca Algo Trader Plus at the documented $99/month, prospective capture only.** Daily bars with `adjustment=split`, plus `old_rate` and `new_rate`, can build the candidate score. Quote history has only `t`, so it fails the two-timestamp rule unless the research process stamps `local_receipt_time` as messages arrive. Basic and IEX do not substitute.

**Prerequisite before a local feed can be built.** A person who already controls an account must hold, or obtain outside this session, an entitlement for one of those three quote tapes and for that source’s explicit split events, and must place the credential in an uncommitted env file. This checkout does not contain that credential. This session does not subscribe, request a key, download a bar or a quote, create `scripts/research/feeds/`, or start a tracker. After that entitlement exists, the next research step is still a read-only file drop that passes section 5, not a change to the bot.
