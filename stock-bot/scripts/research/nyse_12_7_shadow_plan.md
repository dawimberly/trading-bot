# Prospective NYSE ranking comparison — 12-to-7 shadow plan

**Decision: inconclusive.** No shadow tracker was started. The candidate is not rejected. Nothing here is evidence of expected profit. No live or paper strategy change was made. No order was submitted, no broker trading connection was opened, and no historical bars were fetched or invented.

This note checks `scripts/research/nyse_comparison_readiness.md` and `scripts/research/nyse_comparison_data_inventory.json` against the current checkout. Where that readiness note called the live score a 50-session mean, the code disagrees. Those sentences are corrected in the readiness note.

## 1. What the code actually ranks

`_equity_momentum_candidates` (`modules/pipeline_strategies.py` lines 987–994) sets `ma_window = 20 if ipo else 50` and takes `prices.rolling(window=window).mean()` on the column it was given. The sorted score is `current / ma - 1`. A name stays in the list when `current > ma * (1 - NYSE_MA_ENTRY_TOLERANCE_PCT)` (lines 968–994). The function does not convert rows into NYSE sessions.

The live and paper cycles load that frame with `data = load_live_close_matrix()` (`run_all.py` line 709) and pass it, unresampled, into `run_nyse_momentum_and_stat_arb` (lines 1630–1640). `modules/real_time_data.py` is not in this checkout, so the spacing of a running process’s matrix was not observed.

The checked-in producer of the live tables is `fetch_and_store` (`fetch_data.py` lines 34–46): yfinance `period="5d"`, `interval="5m"`, Close only, unsuffixed SQLite tables. `load_close_matrix` (`modules/data_loader.py` lines 98–129) reads those unsuffixed tables as interval `"5m"` and `*_daily` tables as `"1d"`. `infer_bar_interval` (`modules/market_context.py` lines 83–95) calls a matrix daily only when the median gap is at least 12 hours.

The daily backtester is a different caller. `run_backtest` (`backtester.py` line 1140) runs on daily data. `ensure_daily_data_cached` loads `load_close_matrix(interval="1d")` (`modules/backtester_core.py` lines 142–180). On that path the 50 rows are daily closes, weekends included if the frame has them.

`effective_nyse_ma_window()` (`config.py` lines 5910–5914) is the startup banner (`run_all.py` lines 2254–2259). The order function does not call it. Checked-in defaults: `NYSE_MA_WINDOW` 50 (line 1477), `PAPER_NYSE_MA_WINDOW` 70 (line 1480).

`calculate_atr` (`modules/risk_management.py` lines 403–418) is the mean of the last `ATR_PERIOD` absolute close-to-close changes (checked-in default 14, `config.py` line 1168). It does not resample. Live exits read `executor._sizing_data` (`modules/exit_management.py` lines 315–321), which `resolve_cycle_deploy` sets to the same cycle frame (`modules/pipeline_strategies.py` lines 1448–1449). The daily backtester sets that context from the daily window (`backtester.py` line 2083).

The live baseline score a shadow book must copy is therefore the 50-row (20-row if the IPO rule fires) ratio on the live close matrix, plus the 1% band as an eligibility filter. It is not a 50-session moving average. Applying that formula to `data/cache/backtest/days_266.pkl` would not be the live score: that file is daily, includes 121 weekend rows, and ends 2026-06-14 (`scripts/research/nyse_comparison_data_inventory.json`).

## 2. Checked-in defaults versus unverified runtime

No `stock-bot/.env`, no repo `.env`, and no `data/portal/users/` env files are in this checkout. A running bot’s overrides were not read. Statements below are the code fallbacks only.

| Item | Checked-in default | Unverified at runtime |
|------|--------------------|------------------------|
| Live universe | `get_nyse_universe_fixed()` (`config.py` lines 268–270): `UNIVERSE` minus crypto and the sleeve ETF set. 30 names in this file. Used when `not PAPER_TRADING` (`pipeline_strategies.py` lines 903–907). | A portal user env could set `PAPER_TRADING` or replace the process entry. Not visible here. |
| `USE_DYNAMIC_UNIVERSE` | `false` (`config.py` lines 217–221 and `stock-bot/.env.example` line 332). The screener-union branch does not run on that default. | A user env can turn it on. |
| Entry band | `NYSE_MA_ENTRY_TOLERANCE_PCT` 0.01 (`config.py` line 1485). | Env override possible. |
| Quality filters | `PAPER_MOMENTUM_QUALITY_FIXES` default `false` (`config.py` lines 702–704; `.env.example` line 335). `_nyse_momentum_quality_skip` returns immediately when the effective flag is off (`pipeline_strategies.py` lines 260–263). The 9:30–10:00 window (lines 167–174) and the 2% gap (line 267) do not run. | Env can enable them. Paper context also has to be on (`config.py` lines 6293–6298). |
| Scan clock | Orders from session open plus `EQUITY_SCAN_AFTER_OPEN_MIN` (default 5) through the close (`modules/scan_schedule.py` lines 75–84; `config.py` lines 1521–1522). Cycle default 60 seconds (`config.py` line 1524). | Env can change the minutes and the cadence. The scan helper also asks a trading client for the clock (`scan_schedule.py` line 71). |
| Entries per cycle | `effective_max_equity_trades()` returns 1 unless paper-aggressive or realistic-research, then `PAPER_MAX_EQUITY_TRADES` clamped to 1–5 (`config.py` lines 3370–3378). | Which context a process set is unverified. |
| Caps | `PER_NAME_MAX_PCT` 0.08 (line 771), `MAX_ACTIVE_TICKERS` 25 (line 777), `NYSE_SLEEVE_CAP_PCT` 0.20 (line 1414). | Env and account-size branches can move the paper cap. |
| Exit optimization | Flag default true (`config.py` lines 1188–1190). `effective_exit_optimization_enabled` (lines 6231–6247) is on for the listed paper contexts and also when `live_conservative_lock_active()`. Hold cap then is `EXIT_OPTIMIZATION_MAX_HOLD_BARS` 35 (line 1194). Age is wall-clock days (`modules/position_exits.py` lines 87–88 and 91–94). | Whether the lock is active depends on startup. The live branch calls `enforce_live_conservative_profile()` when `PAPER_TRADING` is false (`run_all.py` lines 2455–2456) and then loads the broker account (lines 2460–2463). Not observed. |
| Smart stops | `PAPER_SMART_STOPS` default true (line 1899). Effective on paper contexts; live only if `SMART_STOPS_LIVE_ENABLED` (default false, lines 1900 and 7003–7014). Reeval levels default −5% and −10% (lines 1908–1923). | Env can flip both flags. |
| ATR multiple | `ATR_STOP_MULTIPLIER` default 2.0 (lines 1901–1903). | Env can change it. |
| WebSocket | `effective_real_time_websocket_enabled` (`config.py` lines 7223–7228): explicit env wins; otherwise on when `PAPER_TRADING` is true. `run_all.py` lines 2472–2474 call `start_realtime_feed()` from the missing module. | Cannot run that call here. Paper versus live is unverified. |
| Overlap filter | Paper helper default true; live env default false (`config.py` lines 1430–1433). The paper helper imports missing `modules/core_allocator.py`. | Not executed. |

Fills in the bots are Alpaca day market orders inside `execute_order`. This plan does not call that function. The daily backtester fills the same row it used to decide (`backtester.py` builds the executor on the current window before the strategy call). That same-bar fill is not the shadow fill rule.

## 3. Candidate score (pre-registered, not tuned)

On a decision whose signal date is the NYSE session date of the decision timestamp:

`score = adj_close(S7) / adj_close(S12) - 1`

- `S12` is the last NYSE session on or before 12 calendar months before the signal date.
- `S7` is the last NYSE session on or before 7 calendar months before the signal date.
- `adj_close` is the split-adjusted close of that session. A weekend or holiday row is not a session.
- If either session is missing, the name is ineligible for both virtual books that day.

This is the split-adjusted return from the 12-month session through the 7-month session. It skips the most recent seven months. It is the 12-to-7 definition in the request. It is not 12-minus-1 momentum, and no other lag pair is added. The earlier readiness draft anchored the same offsets to the prior session. That one-session shift is retired so the two research notes do not disagree. The signal-date anchor does not use the decision’s own bar: both endpoints are months earlier.

The baseline score stays the live 50-row ratio. The candidate replaces only the sort key among names that pass the same eligibility set.

## 4. Why a no-order shadow tracker is not implemented

A standalone tracker could, in principle, read a local snapshot without stopping or restarting the bot: open the file read-only, never import `execute_order`, never construct a trading client. That is not possible on the files that exist here.

Verified absent or unusable:

- `modules/real_time_data.py` is missing, so the live matrix function cannot be called, and there is no local writer to tail.
- `market_data.db` is absent (`nyse_comparison_data_inventory.json`: `files.market_data_db` false). No unsuffixed 5-minute tables and no `*_daily` tables.
- No `data/intraday_cache/`, no heartbeat, no bot process, and no journal of live closes in this checkout.
- `data/cache/backtest/days_266.pkl` is a static daily close panel, 2025-04-14 through 2026-06-14, 426 rows of which 121 are weekend copies, close only, adjustment unverifiable, no membership column. It is not the live 5-minute matrix and it is not a prospective feed. Using it would be a historical replay of the wrong bar size. It was not used to score either book.

The tracker was not written and was not started. Writing a runner that downloads bars, or that invents a 5-minute history from the daily pickle, would violate the data rules. A script that only exits because the feed is missing would not record rankings.

## 5. Smallest safe interface

The bot does not already publish a research snapshot. The smallest feed that lets a later tracker match the live score without attaching to the process, the broker, or `execute_order` is two read-only files plus a calendar, dropped under `scripts/research/feeds/` by something other than the trading loop:

1. `nyse_shadow_live_closes.jsonl` — one object per symbol per live bar: `asof` (decision time the snapshot represents), `symbol`, `close`, `bar_end`, `interval`. `interval` must be the same spacing `load_live_close_matrix` would have returned. Under the checked-in contract that is `5m`. Rows used for a decision must have `bar_end` at or before the decision time. The file has to cover the fixed 30-name list (`get_nyse_universe_fixed`) and SPY, from 9:30 through the cash close, America/New_York.
2. `nyse_shadow_sessions.jsonl` — one object per symbol per NYSE session: `session_date`, `symbol`, `adj_close`, `unadj_close`, `split_factor`. No Saturday, Sunday, or exchange-holiday rows. History must start at least 12 calendar months before the first signal date. A sidecar `adjustment.json` names the adjustment method. A close-only file that was built with `auto_adjust=True` and then stripped (`fetch_data.py` lines 20–31 and 67–77) does not satisfy this, because the factor is no longer in the file.
3. `nyse_sessions.txt` — explicit NYSE session dates used to pick `S7` and `S12`.

An existing `market_data.db` would be an acceptable substitute only if it contained both the unsuffixed live tables and session tables that meet item 2, and the tracker opened it `mode=ro`. That file is not in the checkout. Creating it with `fetch_data.py` is a fetch and is out of scope.

The tracker, when that feed exists, must:

- Import the ranking and skip helpers, or reimplement them line-for-line, and must not call `execute_order`, `AlpacaExecutor`, or a trading client.
- Use the live branch of `_nyse_equity_columns` (fixed universe).
- Build one eligible set per decision: on the fixed list, passes the live MA band on rows at or before the decision, and has both `S7` and `S12`. Both books use that set. Ranking inside the set is the only difference (baseline `close/ma - 1`, candidate the 12-to-7 score).
- Apply the same sizing, sleeve cap, per-name cap, max active names, cooldown key `symbol + "/MA50"`, and exit rules, using checked-in defaults and recording each default in a manifest. Label the run “checked-in defaults,” not “the running bot,” until a flag dump from that process is stored next to the feed.
- Fill only on the first live bar with `bar_end` strictly after the decision and at or before the session close. If that bar is missing, the decision is unfilled. Never fill on the signal bar. Charge 5 bps per side and 0 commission (`modules/backtester_core.py` lines 29–31).
- Use one shared virtual account size for both books, recorded before the first decision (checked-in `REFERENCE_EQUITY` is 100000, `config.py` line 2122). Do not read the broker account.

## 6. Pre-registered observation and reporting

The clock starts only after section 5’s feed passes the readiness checks (session coverage, 12-to-7 coverage, fill coverage, calendar, duplicates, stale prices, look-ahead, survivorship label, corporate actions, costs). Until then, do not publish a return.

Rules fixed before any observation, and not to be revised after the first decision:

- Burn-in is the first 35 calendar days after the feed is valid. Positions may be tracked. No performance number is reported from the burn-in.
- The report window is the next 60 NYSE sessions.
- Each book needs at least 20 completed round trips in that window.
- At least 90% of in-universe name-decisions in the window need a prior live bar for the baseline score, both 12-to-7 sessions, and a fill bar strictly after the decision.
- Stale-price and corporate-action failures follow the readiness note: more than 2% stale name-sessions stops the report; an adjusted move larger than 25% without a factor fails that name-session.
- The only status allowed before those bars are met is **inconclusive**.
- The first report, if the bars are met, is paired and net of the 5 bps cost: net return, max drawdown, turnover, trade count, costs, and coverage, same dates and the same eligible names. No other candidate and no retuned dates.
- A shorter run is not a result. A published momentum paper is not evidence that this book will profit. The daily pickle is not a prospective sample.

## 7. Result

**Inconclusive.** Continued shadow observation is not supported on this checkout, because the live close matrix and the 12-month session panel are not on disk and the bot does not publish them. The candidate is not rejected. The comparison has not been run. No live or paper setting changes.
