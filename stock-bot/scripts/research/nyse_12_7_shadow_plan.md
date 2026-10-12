# Prospective NYSE ranking comparison — 12-to-7 shadow plan

**Decision: inconclusive.** No shadow tracker was written or started. No strategy return is reported. The candidate is not rejected and is not promoted. No live or paper path, credential, journal, or production setting was changed. No broker was contacted. No market data was fetched. Files under `sentiment/sources/` were not edited.

Checked-in defaults below are `os.getenv` fallbacks in `config.py` and values in `stock-bot/.env.example`. No `stock-bot/.env`, repo `.env`, or `data/portal/users/` directory is in this checkout. Those effective runtime settings are unverified. No bot process was inspected.

## 1. Baseline score

`_equity_momentum_candidates` (`modules/pipeline_strategies.py` lines 986–994) sets `ma_window = 20 if ipo else 50`, then `window = min(ma_window, len(prices))` and `prices.rolling(window=window).mean()` on the column it was given. The sorted score is `current / ma - 1`. A name stays in the list when `current > ma * (1 - NYSE_MA_ENTRY_TOLERANCE_PCT)` (lines 968–994). The checked-in tolerance is `0.01` (`config.py` line 1485). The function does not resample and does not call `effective_nyse_ma_window()`.

The 50-row and 20-row figures are the target windows. A non-IPO is skipped only below 20 rows (line 986). An IPO is skipped only below `IPO_MIN_TRADING_DAYS`, checked-in default 5 (`modules/dynamic_universe.py` lines 27 and 309–310). A shorter series that still passes that minimum is scored on `min(target, len)`. The shadow follows that function. It does not invent rows to force a 50-row mean.

The live and paper cycles load `data = load_live_close_matrix()` (`run_all.py` line 709) and pass that frame, unresampled, into `run_nyse_momentum_and_stat_arb` (lines 1630–1640). `modules/real_time_data.py` is not in this checkout. No live matrix snapshot is on disk. **The bar spacing of the live score is unproven.** This plan does not assign it a size.

`fetch_and_store` (`fetch_data.py` lines 34–46) requests `interval="5m"` and stores Close only. The live cycle does not call that function, and its output is not in this checkout, so that request is not the live score’s spacing. `load_close_matrix` (`modules/data_loader.py` lines 98–129) selects unsuffixed tables for `interval="5m"` and `*_daily` tables for `interval="1d"`. `.env.example` lines 117–126 comment `REAL_TIME_USE_QUOTES=false` and `REAL_TIME_DATA_FEED=iex`. No checked-in Python reads those names.

The daily backtester is a different caller. `ensure_daily_data_cached` calls `load_close_matrix(interval="1d")` (`modules/backtester_core.py` lines 180, 206, and 217). `run_backtest` (`backtester.py` line 1140) runs on that daily frame. Its 50 rows are daily-loader rows. That does not measure the live cycle.

`NYSE_MA_WINDOW` checked-in default is 50 (`config.py` line 1477). `PAPER_NYSE_MA_WINDOW` checked-in default is 70 (line 1480). `effective_nyse_ma_window()` (lines 5910–5914) is the startup banner (`run_all.py` lines 2254–2259). The order score ignores it.

`calculate_atr` (`modules/risk_management.py` lines 403–418) averages the last `ATR_PERIOD` absolute close-to-close changes. Checked-in default period is 14 (`config.py` line 1168). It does not resample. Live exits read `executor._sizing_data` (`modules/exit_management.py` lines 315–321), set from the same cycle frame (`modules/pipeline_strategies.py` lines 1448–1449). The spacing of those rows is the unproven live spacing. The daily backtester sets the context from its daily window (`backtester.py` line 2083).

`data/cache/backtest/days_266.pkl` has a one-day median step, close only, weekend rows, and no quotes. It ends 2026-06-14 (`scripts/research/nyse_comparison_data_inventory.json`). It does not prove the live spacing, and it cannot fill an order. It was not scored.

## 2. Checked-in defaults versus unverified runtime

| Item | Checked-in default | Unverified at runtime |
|------|--------------------|------------------------|
| Live universe | `get_nyse_universe_fixed()` (`config.py` lines 268–270): `UNIVERSE` minus crypto and the sleeve ETF set. 30 names in this file. Used when `not PAPER_TRADING` (`modules/pipeline_strategies.py` lines 903–907). | Whether the running process is paper is not visible. |
| `USE_DYNAMIC_UNIVERSE` | `false` (`config.py` lines 217–221; `.env.example` line 332). | A user env can turn it on. |
| Entry band | `NYSE_MA_ENTRY_TOLERANCE_PCT` 0.01 (`config.py` line 1485). | Env override possible. |
| Quality filters | `PAPER_MOMENTUM_QUALITY_FIXES` default `false` (`config.py` lines 702–704; `.env.example` line 335). `_nyse_momentum_quality_skip` returns immediately (lines 260–263). The 9:30–10:00 window is lines 167–174. The gap test is line 267. | Effective only with the flag and a paper context (`config.py` lines 6293–6298). |
| Scan clock | Orders from session open plus `EQUITY_SCAN_AFTER_OPEN_MIN` (checked-in default 5, `config.py` line 1522) through the close (`modules/scan_schedule.py` lines 75–84). Cycle default 60 seconds (`config.py` line 1524). | Env can change both. The helper also calls `trading_client.get_clock()` (`modules/scan_schedule.py` line 71). That client was not created here. |
| Entries per cycle | `effective_max_equity_trades()` returns 1 unless paper-aggressive or realistic-research, then `PAPER_MAX_EQUITY_TRADES` (default 3, line 809) clamped to 1–5 (lines 3370–3378). | The process context is unverified. |
| Caps | `PER_NAME_MAX_PCT` 0.08 (line 771), `MAX_ACTIVE_TICKERS` 25 (line 777), `NYSE_SLEEVE_CAP_PCT` 0.20 (line 1414). | Env and account-size branches can move the paper cap. |
| Exit optimization | Flag default true (lines 1188–1190). `effective_exit_optimization_enabled` (lines 6231–6247) is on for the listed paper contexts and when `live_conservative_lock_active()`. Hold cap is then 35 (line 1194). Otherwise the cap is 30 (line 770). Age is wall-clock days (`modules/position_exits.py` lines 87–88 and 91–94). | `run_all.py` lines 2455–2456 call `enforce_live_conservative_profile()` when `PAPER_TRADING` is false, then lines 2460–2463 load a broker account. Not observed. |
| Smart stops | `PAPER_SMART_STOPS` default true (line 1899). Live only if `SMART_STOPS_LIVE_ENABLED` (default false, lines 1900 and 7003–7014). Reeval defaults −5% and −10% (lines 1908–1923). | Env can flip both flags. |
| ATR multiple | `ATR_STOP_MULTIPLIER` default 2.0 (lines 1901–1903). | Env can change it. |
| WebSocket | `effective_real_time_websocket_enabled` (lines 7223–7228): explicit env wins; otherwise on when `PAPER_TRADING` is true. `run_all.py` lines 2472–2474 would call `start_realtime_feed()` from the missing module. | Paper versus live is unverified. |
| Overlap filter | Live env default false (`config.py` lines 1430–1433). The paper helper imports missing `modules/core_allocator.py`. | Not executed. |

Live and paper orders are Alpaca day market orders. `get_order_params` sets `TimeInForce.DAY` for equities (`modules/alpaca_executor.py` lines 782–790). `execute_order` submits `MarketOrderRequest` with notional on the buy path (lines 1564–1568) and quantity on the capped sell path (lines 1522–1527). This plan does not call `execute_order`. The broker print is not in this checkout.

The daily backtester sets `prices = data.iloc[i]` (`backtester.py` line 1566) and builds `BacktestExecutor(portfolio, prices)` (lines 2020–2025) before the strategy call. `BacktestPortfolio.trade` (line 709) fills that same price, then `effective_execution` (`modules/backtester_core.py` lines 290–307). That same-row fill is not the shadow fill rule.

## 3. Candidate score

Signal date is the NYSE session date of the decision timestamp.

`score = split_adj_close(S7) / split_adj_close(S12) - 1`

- `S12` is the last NYSE session on or before 12 calendar months before the signal date.
- `S7` is the last NYSE session on or before 7 calendar months before the signal date.
- `split_adj_close` is the split-adjusted close. Cash dividends are not in this ratio. Both endpoints use the same split-only method, named in the feed sidecar. An unadjusted close is not used for one endpoint and an adjusted close for the other. Alpaca `adjustment=all` includes cash dividends and is not this method.
- A weekend or holiday row is not a session.
- If either session is missing, or the factor for that name is missing, the name is ineligible for **both** books on that decision.

This is the return from the 12-month session through the 7-month session. No other pair is registered. The baseline sort key remains `current / rolling_mean - 1`. Shared eligibility is the live MA band, the fixed list, the position limits, and the requirement that both endpoints exist. The candidate does not add names the baseline would have dropped, and a missing endpoint does not leave the name in the baseline book.

## 4. Fill rule

A close series, including a 5-minute close series, does not prove an executable fill. The signal row is the last score-file close with `bar_time <= decision_time`. That row forms the baseline score. It is never the fill. The full source comparison and the cost split are in `scripts/research/nyse_shadow_feed_sources.md`.

For a decision at time `T`, before the official NYSE close:

- Quote tape, used when an eligible quote exists. The record must carry `event_time` and a receipt time. `available_time` is the local receipt when the research process stored the record, otherwise the vendor receipt. A historical file must not invent the missing one. A quote is eligible only when `event_time > T`, `available_time > T`, and `available_time - event_time` is at most 2.000 seconds, with price > 0 and size > 0 on the side used. The buy fill is that quote’s ask. The sell fill is that quote’s bid. The recorded fill time is `available_time`. The 2-second ceiling is an eligibility filter, not an extra cost.
- OHLC tape, used only when no eligible quote exists: the fill is the `open` of the first bar with `bar_start > T` and `bar_end` at or before the session’s official close. A bar that contains `T` is not a fill.
- If neither print exists, the decision is unfilled and counts against fill coverage.

Both books use that same fill for the same decision. Either price is a simulated fill proxy, not an Alpaca print and not a call to `execute_order`.

Costs, applied only when a performance report is allowed, do not count the spread twice. A quote fill pays the spread through the ask or the bid, uses equity commission of 0 (`modules/backtester_core.py` line 31), and does **not** also multiply by the 5 bps slippage term. An OHLC open does not contain the spread, so that path applies the checked-in 5 bps once (lines 29 and 300–305) and does not also subtract a modeled half-spread. Latency is the 2-second exclusion, not more basis points. Applying those 5 bps on top of a bid or ask would double-count the spread. That earlier sentence in this section is withdrawn.

## 5. Feed schema

No file in this checkout matches this schema. The tracker is not implemented, because implementing it would require this unavailable feed. Do not add a writer to the bot.

Directory, read-only, not created here: `scripts/research/feeds/`.

`nyse_shadow_score_closes.jsonl`, one object per symbol per live-matrix row:

- `symbol`
- `bar_time` (ISO-8601, `America/New_York`)
- `close`
- `source` (`live_matrix_snapshot` or `research_proxy`)

Rows with `bar_time <= T` are the only rows in the mean. The mean length is the order function’s `min(target, len)`, with targets 50 and 20. The file must cover `get_nyse_universe_fixed()` and SPY. Record the observed median spacing. Do not relabel it. A `research_proxy` file is not a snapshot of `load_live_close_matrix()`.

`nyse_shadow_fills.jsonl`, one object per symbol per quote or bar. Quote records:

- `symbol`, `event_time`, `vendor_receipt_time`, `local_receipt_time`, `bid`, `ask`, `bid_size`, `ask_size`, `source`

OHLC records, used only when no eligible quote exists:

- `symbol`, `bar_start`, `bar_end`, `open`, `high`, `low`, `close`, `source`

`nyse_shadow_sessions.jsonl`, one object per symbol per NYSE session:

- `session_date`, `symbol`, `split_adj_close`, `unadj_close`, `split_factor`, `cash_dividend`

`split_adj_close` is split-adjusted only. `cash_dividend` is stored and is not multiplied into the 12-to-7 ratio. Alpaca `adjustment=all` and `adjustment=dividend` include cash dividends and do not satisfy this field.

No Saturday, Sunday, or holiday rows. History starts at least 12 calendar months before the first signal date. `adjustment.json` names the method as split ratios only. Both `S7` and `S12` read `split_adj_close` from this file only. Score-close records also carry `source` of `live_matrix_snapshot` or `research_proxy`. A proxy does not prove `load_live_close_matrix()`. `fetch_data.py` lines 20–31 and 67–77 keep Close after `auto_adjust=True` on the daily path, so a file built that way does not satisfy the factor fields.

`nyse_sessions.txt` lists the session dates used to choose `S7` and `S12`.

`market_data.db` is absent. It would qualify only if it already held the three record types above and was opened read-only. Creating it with `fetch_data.py` or `_fetch_alpaca_daily_closes` (`modules/data_loader.py` lines 234–253, `StockBarsRequest` with no adjustment argument) is a fetch and was not done.

## 6. Configured sources

Presence only. No secret value is recorded. No provider was called.

| Source | What is in the checkout | Can it supply the feed without disturbing a bot? |
|--------|-------------------------|-----------------------------------------------------|
| yfinance via `fetch_data.py` | Code only. No API key. 5-minute path keeps Close. Daily path keeps Close after `auto_adjust=True`. | No. Running it fetches, and the stored shape has no quotes, no bar OHLC, and no factor. |
| Alpaca daily bars via `_fetch_alpaca_daily_closes` | Code only. `get_alpaca_credentials` (`config.py` lines 2339–2344) reads env vars. `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` are unset in this process. `.env.example` lines 19–20 are the placeholders `your_paper_key_id` and `your_paper_secret_key`. `PAPER_APCA_API_KEY_ID` and `PAPER_APCA_API_SECRET_KEY` are commented empty (lines 558–559). | No. A subscription is not configured. The request is daily closes, not quotes, and does not set an adjustment argument. |
| Websocket comments | `.env.example` lines 117–126. `REAL_TIME_USE_QUOTES=false` is a comment. The module named beside it is missing. | No. There is no reader and no local tape. |
| `days_266.pkl` | Local close panel, 2025-04-14 through 2026-06-14. | No. Close only, not prospective, no fill tape, adjustment unverifiable. |
| Polygon, Sharadar, CRSP, Tiingo, Databento, Nasdaq Data Link, FMP | No key name with a value in `.env.example`. Those env names are unset in this process. | No source is configured. |

Exact fields a future read-only drop must have are in section 5. Which official sources can produce them, and which prices were actually printed on their pages, are in `scripts/research/nyse_shadow_feed_sources.md`. Access is a local file. This plan does not subscribe, fetch, or guess a vendor default. No key named in that note is configured here.

## 7. Observation protocol

Registered on 2026-10-12, before any feed file is collected and before any tracker start. Not revised after the first decision timestamp is written into a feed file. The worked rules are in `scripts/research/nyse_shadow_feed_sources.md` section 6.

- The performance window is **at least six calendar months** of prospective NYSE sessions, beginning on the first session after the feed passes the readiness checks.
- Six months is a minimum observation window. It is not proof of profitability. A completed window does not authorize a live or paper change.
- Each book must finish at least **30 completed round trips**. A completed round trip goes from flat to a position on an entry fill and later returns to flat on an exit fill, both inside the window. An open position at the end is not completed.
- At least **120 paired NYSE sessions** are required. A paired session is one where both books have a finite daily return. Equity at the session close is cash plus open positions marked at the same `split_adj_close`. That mark is not the fill. The comparison series is the candidate session return minus the baseline session return.
- During the window, interim notes may list data-quality and operational diagnostics only: row counts, missing endpoints, duplicate timestamps, stale sessions, unfilled decisions, receipt-lag failures, feed gaps, and exceptions. They do not include return, Sharpe, drawdown, turnover, or a promotion recommendation. They are not used to change the lag ceiling, the trade floor, bands, caps, or exits.
- At the end, report **inconclusive** if executable-fill coverage, 12-to-7 endpoint coverage, or score-row coverage is under 90% of in-universe name-decisions, **or** if either book has fewer than 30 completed round trips, **or** if fewer than 120 paired sessions exist. Weekend rows are not in those ratios. Failing either the coverage requirements or the sample requirements is inconclusive.
- The same stale-price rule (more than 2% of name-sessions unchanged for five sessions) and the same corporate-action rule (a split-adjusted move over 25% without a split factor fails that name-session) still block a performance number.
- If those checks pass, the first paired note may show the paired difference series and its mean, max drawdown, turnover, trade count, the section 4 costs, and coverage, on the same names and decision times. Meeting the six-month, 30-round-trip, and 120-session floors does not prove future profitability and does not change the bot.
- The earlier 35-day burn-in, 60-session window, and 20-round-trip gate are retired. The 35-day figure remains only as the checked-in exit-optimization hold cap, which is a position rule, not the observation window. The earlier “zero completed round trips” gate is also retired in favor of the 30-round-trip floor.

## 8. Tracker

Not implemented and not started. A standalone reader could avoid the bot only by reading section 5’s files. Those files are absent, so a tracker written now would have nothing valid to record.

Smallest next implementation step, after the feed exists and the inventory script has been re-run: add `scripts/research/nyse_12_7_shadow_tracker.py` that reads only that directory, writes diagnostics under `scripts/research/`, and does not import `execute_order` or a trading client. Do not add that file in this checkout while the feed is missing.

## 9. Result

**Inconclusive.** No observation exists, so no strategy return is reported. The candidate stays pre-registered and untested. The next concrete step is not a download from this checkout. It is the prerequisite in `scripts/research/nyse_shadow_feed_sources.md`: a user-held entitlement for a quote tape that already has an event time and a receipt time, plus explicit split events, with the credential placed in an uncommitted env file. No strategy or production setting changes.
