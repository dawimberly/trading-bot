# NYSE ranking comparison — data readiness

**Decision: no-go.** A valid comparison of the current ranking with a 12-to-7-month ranking cannot be run from this checkout. No strategy backtest was run. Trading code, settings, credentials, and journals were not changed. No bot was started or stopped. Files under `sentiment/sources/` were not opened for editing.

Numbers below come from `scripts/research/nyse_comparison_data_inventory.py` on this checkout. Portal book `.env` files are gitignored (`data/portal/users/`) and are **not in this checkout**. `stock-bot/.env` and the repo `.env` are also absent. Checked-in defaults below are the `os.getenv` fallbacks in `config.py` and the values in `stock-bot/.env.example`. They are not a reading of a running process. Any override in a missing user `.env` is unverified.

Correction after a second code pass: the live ranking window is **50 rows of the matrix the cycle passes in**, not 50 NYSE sessions. See the ranking and ATR notes in section 1. The prospective comparison is specified in `scripts/research/nyse_12_7_shadow_plan.md`.

## 1. Launch path and order behavior

There is no book named Medium. Enabled books are `alpaca_live` and `alpaca_paper` (`modules/trading_books.py`).

| Book | How it starts | NYSE orders |
|------|----------------|-------------|
| Daily | `Start_Bot_and_Dashboard.bat` → `scripts/owner_reset.py` | Restarts both books. Not run here. |
| Live | `modules/portal_bot.py` `_bot_entry_script` (lines 399–408) returns `run_all.py` | `run_all.py` lines 2452–2456 call `init_paper_chase_if_enabled()`, then `enforce_live_conservative_profile()` when `PAPER_TRADING` is false. |
| Paper | `run_paper_bot.py` lines 141–147 set `PAPER_TRADING=true` and `PAPER_CHASE_MODE=1`, then start `run_all.py` | `config.py` lines 5567–5601: `init_paper_chase_if_enabled` tries `config.best_paper_config`. `config.py` shadows the `config/` directory, so the import fails and the fallback is `enforce_best_paper_stack()` → `enforce_realistic_research_profile()`, then `set_paper_aggressive_context(True)`. Lines 5583–5590 can call `maybe_refresh_screener_universe()`; that call was not made here. |

Shared order function: `run_all.py` line 1630 calls `run_nyse_momentum_and_stat_arb` (`modules/pipeline_strategies.py` lines 1833–1873). That calls `run_equity_strategy` (line 1634), which submits at lines 1802–1804: `executor.execute_order(..., sleeve="NYSE")`. The daily backtester calls the same function at `backtester.py` lines 2174–2186.

### Ranking

`run_equity_strategy` (`pipeline_strategies.py`) loads columns from `_nyse_equity_columns`, then `_equity_momentum_ranked` → `_equity_momentum_candidates`.

The score that is sorted is `(last close / rolling mean) - 1`. The length is hardcoded in `_equity_momentum_candidates` (`modules/pipeline_strategies.py` lines 987–994): `ma_window = 20 if ipo else 50`, then `prices.rolling(window=window).mean()` on that column. The function does not resample to NYSE sessions and does not call `effective_nyse_ma_window()`. A name is kept when `current > ma * (1 - NYSE_MA_ENTRY_TOLERANCE_PCT)`. The checked-in default of that tolerance is `0.01` (`config.py` line 1485). The running value is unverified without a user `.env`.

`NYSE_MA_WINDOW` checked-in default is 50 (`config.py` line 1477). `PAPER_NYSE_MA_WINDOW` checked-in default is 70 (line 1480). `effective_nyse_ma_window()` (lines 5910–5914) returns the paper value only in a paper-aggressive or backtest-paper context. `run_all.py` lines 2254–2259 print it on the startup banner. The order score ignores both helpers. It is the 50-row ratio (20 rows for an IPO) of whatever frame the caller supplied.

That frame is not the same on the two callers:

- Live and paper cycles set `data = load_live_close_matrix()` (`run_all.py` line 709) and pass that frame into `run_nyse_momentum_and_stat_arb` (lines 1630–1640) with no resample. `modules/real_time_data.py`, which defines `load_live_close_matrix`, is **not in this checkout**, so the spacing of the running matrix is unverified.
- The checked-in live store is 5-minute. `fetch_and_store` (`fetch_data.py` lines 34–46) downloads `period="5d"`, `interval="5m"`, and writes unsuffixed tables. `load_close_matrix` (`modules/data_loader.py` lines 98–129) reads unsuffixed tables for interval `"5m"` and `*_daily` tables for `"1d"`. `infer_bar_interval` (`modules/market_context.py` lines 83–95) treats a matrix as daily only when the median gap is at least 12 hours.
- The daily backtester is a separate caller. `run_backtest` (`backtester.py` line 1140) runs on daily data, and `ensure_daily_data_cached` loads `load_close_matrix(interval="1d")` (`modules/backtester_core.py` lines 142–180). On that path the 50 rows are 50 daily closes, including weekend or holiday rows if the frame contains them. They are not forced onto an NYSE session calendar.

Calling the live score a 50-session mean conflicts with this code. It is a 50-row mean. Only the daily backtester supplies daily rows.

Live columns: if the process is not paper and not a paper-only sleeve, `_nyse_equity_columns` keeps only `get_nyse_universe_fixed()` (`config.py`), which is `UNIVERSE` minus crypto and the sleeve ETF set. That list is 30 symbols in this checkout’s `UNIVERSE`.

Paper columns: the screener-union branch runs only when `USE_DYNAMIC_UNIVERSE` is true. `stock-bot/.env.example` sets `USE_DYNAMIC_UNIVERSE=false`. `config.py` defaults `PAPER_DYNAMIC_UNIVERSE_ENABLED` to true, but that flag does not by itself select the union branch. If `effective_dynamic_sector_screener()` is true, the paper path calls `get_expanded_universe`. Expansion can only add symbols that already have price columns. With the cache below, the selectable equity names are the 29 present columns.

Optional rank add-ons (insider, RVOL, ORB, catalyst, multi-timeframe, 12:00–14:00 ET boost) run only when their own flags are on. They sit after the base ratio. A later comparison that changes only the ranking must replace the base ratio and leave those gates as coded.

When the SPY sleeve is active, `_equity_momentum_ranked` may call `effective_nyse_overlap_filter_enabled()`. That helper calls `get_paper_feature_flags()`, which imports `modules.core_allocator`. **`modules/core_allocator.py` is not in this checkout.** The call is unverified at runtime here and would raise on the paper path. The stored paper flag default is `PAPER_NYSE_OVERLAP_FILTER_ENABLED=true` (`config.py`). The live flag default is false (`NYSE_OVERLAP_FILTER_ENABLED`, env default `"false"`). This plan does not patch that import.

### Entry

- Regime pause can return before ranking (`regime_entries_paused` at the start of `run_equity_strategy`).
- Per cycle, `effective_max_equity_trades()` is 1 unless paper-aggressive or realistic-research context is on, in which case it is `PAPER_MAX_EQUITY_TRADES` (default 3), clamped to 1–5 (`config.py`).
- Same-symbol cooldown key is `symbol + "/MA50"`. Live cooldown constant is `COOLDOWN_SECONDS = 3600`. The daily backtester passes a bar index and `DAILY_COOLDOWN_BARS = 1` (`backtester.py`), so a daily replay blocks the same key for one bar.
- Quality filters (open cooldown, gap, one entry per symbol per day, RSI ceiling) run only inside `_nyse_momentum_quality_skip`, and only when `effective_paper_momentum_quality_fixes()` is true. That requires `PAPER_MOMENTUM_QUALITY_FIXES` and a paper context. The code default and `stock-bot/.env.example` are both `false`. The cooldown window in source is 9:30–10:00 ET (`time(9, 30) <= t <= time(10, 0)`). The gap test is `gap > 0.02`. When the flag is on, the gap prefers a live yfinance open versus prior close, and backtests are forced onto close versus prior close. **With the flag off, neither filter runs.**
- Scan clock, separate from that flag: `equity_scan_state` (`modules/scan_schedule.py`) starts equity orders at session open plus `EQUITY_SCAN_AFTER_OPEN_MIN` (default 5, so 9:35 ET) and stops at the session close. The loop cadence default is 60 seconds (`CYCLE_INTERVAL_SEC`).

### Exit

`run_all.py` calls `run_position_exits` (`modules/position_exits.py`) on open equity-session positions other than the VTI core.

- Initial stop distance used by smart stops and by `atr_stop_price` is `ATR_STOP_MULTIPLIER` (default 2.0, falling back to `ATR_RISK_MULTIPLE` 2.0).
- `calculate_atr` (`modules/risk_management.py` lines 403–418) is the mean of the last `ATR_PERIOD` absolute close-to-close changes. The checked-in default period is 14 (`config.py` line 1168). The docstring says “daily close series.” The function does not resample and does not use high or low. Live exits read `executor._sizing_data` (`modules/exit_management.py` lines 315–321). `resolve_cycle_deploy` sets that to the cycle frame (`modules/pipeline_strategies.py` lines 1448–1449), which on the live cycle is `load_live_close_matrix()`. The daily backtester instead calls `set_sizing_context(window)` (`backtester.py` line 2083) with the daily window. “14-day” is accurate only for that daily caller.
- Paper smart stops default on (`PAPER_SMART_STOPS` default true). They reevaluate at −5% and hard-exit at −10% (`STOP_LOSS_REEVAL_PCTS`). Live smart stops stay off unless `SMART_STOPS_LIVE_ENABLED` (default false).
- Time exit: if exit optimization is on (default true), the cap is `EXIT_OPTIMIZATION_MAX_HOLD_BARS` (default 35). Otherwise the cap is `PAPER_POSITION_MAX_HOLD_BARS` (default 30). Age is wall-clock days from the position timestamp, not a count of exchange sessions.
- Exit optimization can also partial-exit and trail. Those paths are in `position_exits.py` and `modules/exit_management.py`.

### Sizing

`AlpacaExecutor.compute_nyse_notional` (`modules/alpaca_executor.py`) sizes from the NYSE sleeve cap (`NYSE_SLEEVE_CAP_PCT` default 0.20; paper floor `PAPER_NYSE_SLEEVE_CAP_PCT` 0.20, high-cash ceiling `PAPER_NYSE_HIGH_CASH_CAP_PCT` 0.22, hard cap `PAPER_NYSE_MAX_EXPOSURE_PCT` 0.22). Buys then pass `_atr_adjust_notional` when ATR sizing is on (default true), the 8% per-name cap (`PER_NAME_MAX_PCT`), and the 25-name non-core cap (`MAX_ACTIVE_TICKERS`). Further paper multipliers (GARCH, conviction, correlation, beta) apply only when their flags are on inside `run_equity_strategy`.

### Fills

Live and paper bots submit Alpaca **market** orders: notional on the buy path, quantity on the sell path, time in force **DAY** for equities (`AlpacaExecutor.get_order_params` and `execute_order`). The fill price is the broker print. This checkout has no journal of those fills (portal journals are gitignored). Slippage is not applied in `execute_order`.

The daily backtester sets `prices = data.iloc[i]` and builds `BacktestExecutor(portfolio, prices)` before `run_nyse_momentum_and_stat_arb` (`backtester.py`). `BacktestPortfolio.trade` fills at that same row’s price, then `effective_execution` (`modules/backtester_core.py`) applies equity slippage of `BACKTEST_EQUITY_SLIPPAGE_BPS` (default **5**) and equity commission of **0** (“Alpaca $0” in that function’s docstring). A same-bar close fill uses the bar’s close to decide and to fill. That is not the live market-order path.

## 2. Data actually in this checkout

Inventory file: `scripts/research/nyse_comparison_data_inventory.json`.

| Item | Verified value |
|------|----------------|
| Price file | `stock-bot/data/cache/backtest/days_266.pkl` only. `market_data.db` is absent. No parquet, no 5-minute file, no `data/intraday_cache/`. `scripts/research/backtest_intraday.py` is named in the README and `.env.example` and is **not in the tree**. |
| Raw span | 2025-04-14 through 2026-06-14. 426 rows, 62 columns. Median step 1 calendar day. |
| Fields | Close only. Open, high, low, volume are missing. Adjustment status **cannot be verified** from this file. |
| Weekends | 121 Saturday/Sunday rows. They are forward-filled copies (every AAPL Saturday equals the prior row). **They are not sessions.** |
| Weekday rows | 305. Zero duplicate timestamps. These are still not a confirmed NYSE calendar: AAPL and SPY each match the prior weekday close on 3.9% of weekday rows (same rate). That pattern fits holiday forward-fill. No exchange holiday file is in the checkout, so the holiday label is an inference, not a calendar join. |
| Symbols | 62 names from the static universe: 22 crypto, 11 sleeve ETFs/metals, 29 equities. `SPCX` has 2 non-null closes (1 on a weekday). `ONDS`, `SUI-USD`, and `PEPE-USD` are in `config.UNIVERSE` and absent. |
| After a 12-calendar-month lag, weekdays only | 44 rows, from 2026-04-14. After dropping 252 prior weekday rows, 53 rows remain. Neither count is a multi-fold test window. |
| Membership | No column and no file (no `sp500`, constituent, or delist data file). |
| Delisted names | Not represented. |
| Other database | `data/portal/users.db` is present (16 KB). It was not opened. It is not a price panel. |

`data/screener_universe.json` is a screener snapshot (generated 2026-06-12 in the file) with placeholder scores (momentum and price at 0). It is not a price history.

## 3. Minimum dataset for a valid comparison

The comparison is: same order path, two scores. The baseline score is the live order score, `(last row / 50-row mean) - 1` on the cycle’s close matrix, with the checked-in 1% band. It is not a 50-session mean. See section 1.

The candidate score is the split-adjusted return from the last NYSE session on or before 12 calendar months before the **signal date** through the last NYSE session on or before 7 calendar months before that same signal date. An earlier draft of this note anchored those two dates to the prior session. That anchor is retired. The signal-date definition is pre-registered in `scripts/research/nyse_12_7_shadow_plan.md` and is not tuned. Both endpoint sessions must exist or the name is ineligible that day. The candidate uses daily session closes. It does not replace the 5-minute row length of the live baseline score.

The order path does **not** read an S&P 500 membership table. Replaying the code means the live fixed 30-name list, or the paper column set actually loaded. That list is a current survivor list. Results from it are conditional on those names still existing. They are not a full-index test.

Required fields, one row per symbol per **NYSE session** (weekends and exchange holidays omitted):

| Field | Why the order path needs it |
|-------|-----------------------------|
| `session_date` | Decision date. Must be an exchange session. |
| `asof_in_universe` | True only if the name was selectable by `_nyse_equity_columns` on that date. For the static list this is “name was on the list as of that date,” not today’s list copied backward. |
| `delist_date` | Names that leave the list must remain through the last tradable session, then exit. Absent today means survivorship is untested. |
| Unadjusted and split/dividend-adjusted `open`, `high`, `low`, `close` | Split-adjusted session close for the 12-to-7 ratio. The live 50-row mean and the live ATR use the cycle’s close matrix (checked-in contract: 5-minute), not this daily panel. Unadjusted open and prior session close for the gap test if that flag is on. A factor file may replace one of the two OHLC sets. |
| `prior_session_close` | Explicit previous **session**, not a weekend or holiday copy. Ranking, ATR, and the candidate ratio all stop at this close. |
| 5-minute OHLCV from 09:30 through the cash close, timestamped `America/New_York` | Live orders are DAY market orders starting at open + 5 minutes (`EQUITY_SCAN_AFTER_OPEN_MIN`) and repeating about every 60 seconds. The fill is the first 5-minute bar whose timestamp is **after** the decision time and at or before the close. |
| `split_factor`, `dividend` | So a large overnight move is not treated as momentum or as a tradable gap. Daily `fetch_data.py` uses `auto_adjust=True` and then drops every column except Close, so a later file cannot show whether it was adjusted. |

History length before the first decision: 12 calendar months of NYSE sessions for the candidate ratio, ending at the signal date’s 7-month and 12-month sessions. The live baseline mean needs 50 rows of the cycle matrix at or before the decision (about 50 five-minute bars under the checked-in live contract, not 50 sessions). The ATR needs `ATR_PERIOD` rows of that same matrix (checked-in default 14). After the last entry: at least 35 calendar days so the coded time exit can fire, and prices in between so the 2× close-to-close stop on that same matrix (and the −5%/−10% smart-stop path, when that flag is on) can fire. A held-out final segment must be declared before the run and left unused. This checkout does not contain that span.

Corporate-action requirement: every name-session needs a factor or both adjusted and unadjusted closes. Alpaca’s `StockBarsRequest` in `modules/data_loader.py` does not set an adjustment argument. What Alpaca returns by default is **unverified** here.

## 4. Sources and credentials documented here

No provider was contacted. No secret value was read into this report.

| Source | Where it is documented | What this checkout shows |
|--------|------------------------|---------------------------|
| yfinance | `fetch_data.py` module docstring and `fetch_and_store` / `fetch_daily_history`. No API key. 5-minute download is `period="5d"`, `interval="5m"`, and `_normalize_df` keeps Close only. Daily download sets `auto_adjust=True`, `interval="1d"`, and also keeps Close only. | A code path, not a stored history. Not a membership or delist vendor. Whether yfinance still returns delisted symbols was not tested. |
| Alpaca market data | `modules/data_loader.py` `_fetch_alpaca_daily_closes` uses `StockHistoricalDataClient` with `get_alpaca_credentials(paper=True)` and `TimeFrame.Day`. Close is the only column kept. | No `stock-bot/.env` and no repo `.env`. `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` in `.env.example` are the placeholders `your_paper_key_id` / `your_paper_secret_key`. `PAPER_APCA_API_KEY_ID` and `PAPER_APCA_API_SECRET_KEY` are commented empty. A subscription is **not** configured in this checkout. |
| Alpaca trading | `.env.example` Profile A / Profile B comments. Orders go through the trading client, not a research database. | Same missing `.env`. Not used. |

No Polygon, Sharadar, CRSP, Tiingo, Databento, Nasdaq Data Link, or FMP key appears in `stock-bot/.env.example`.

One source that would satisfy section 3, if none of the above is extended:

- A vendor or file drop that includes **historical index or list membership with effective dates**, **delisted symbols**, **split and dividend factors**, **session OHLC**, and **5-minute bars** for those symbols over the window in section 3.
- Credentials for that vendor stored only in an uncommitted `.env`, with key names documented in `.env.example`. This plan does not add a key or assume one exists.
- yfinance plus the current Alpaca daily close fetch, as written, do not meet that list: they have no membership file, the 5-minute path keeps about five days and drops OHLC, and delist coverage is unverified.

## 5. Checks that must pass before any performance number

Do not report return, Sharpe, drawdown, turnover, or cost totals until every check below passes. Coverage uses **sessions × names in the universe that day**. Weekend rows and copied holiday rows are not in the denominator or the numerator.

| Check | Pass rule |
|-------|-----------|
| Session coverage | At least 90% of in-universe name-sessions have a non-null prior-session close. |
| 12-to-7 coverage | At least 90% of those name-sessions have both the 7-month and 12-month prior session closes. |
| Fill coverage | At least 90% of entry and exit decisions have a 5-minute bar strictly after the decision timestamp and at or before the session close. |
| Calendar | Dates match an explicit NYSE session list. Any Saturday, Sunday, or exchange holiday fails the file. |
| Duplicates | Zero duplicate `(symbol, session_date)` keys and zero duplicate 5-minute timestamps per symbol. |
| Stale prices | A close equal to the prior session close for 5 or more consecutive sessions is flagged. If flagged name-sessions exceed 2% of the panel, stop. |
| Look-ahead | The 50-row mean and the ATR use only rows whose timestamp is at or before the decision. The 12-to-7 endpoints are sessions on or before 7 and 12 calendar months before the signal date. Membership `asof` is on or before the decision. The fill timestamp is after the decision. The signal row is not the fill row. |
| Survivorship | If the claim is the static 30-name list, the report must say the result is conditional on those survivors unless an add/delete log is attached. If the claim is a broad index, a window with zero delist records fails the check. |
| Corporate actions | An adjusted close move larger than 25% versus the prior session without a split or dividend factor fails that name-session and counts against coverage. |
| Costs | Apply 5 bps per side and 0 commission on the executable 5-minute price (`backtester_core.py` defaults). Gross results do not satisfy this check. |
| Same names and dates | Baseline and candidate use the same sessions, the same eligible names, and the same fill and cost rules. Only the ranking score changes. |

The current pickle fails session definition (weekend and likely holiday copies), OHLC, 5-minute fills, membership, and delist checks. 5-minute coverage of the required name-sessions is 0%.

## 6. Next step

**No-go.**

Smallest step that makes a comparison of the **actual order path** possible: add a local research panel, not wired to either bot, with the fields in section 3 for the fixed universe (`get_nyse_universe_fixed`) plus SPY, using NYSE sessions only, long enough for the 12-month lag and the 35-day hold. Re-run `python scripts/research/nyse_comparison_data_inventory.py` and the checks in section 5. Do not compute performance until those checks pass. State on the result that the universe is the current static list.

If the comparison is meant to be a point-in-time index test, that panel is not enough. Add membership and delisted names first. That file is not in this checkout, and no configured credential can be shown to supply it.
