# PythonTrading — Project Manifest

## 1. Core loop
- **`run_all.py`**: 24/7 orchestrator — data refresh, wisdom regime, sleeve strategies, Alpaca execution, heartbeat JSON each cycle.
- **`modules/pipeline_strategies.py`**: Shared crypto z-pair, SPY MA200, and NYSE MA50 logic (live + backtester).
- **`backtester.py`**: Mirrors the live stack on historical daily bars; disk cache under `data/cache/backtest/`.

## 2. Profiles
- **Profile A (live / `current_dynamic`)**: 90% VTI core (< $500), yield-gate-only, overlap/chunk/co-fire off, 1% / $10 small-account caps — default `run_all.py` + preflight live.
- **Profile B (paper v2.1 / `paper_aggressive`)**: Dynamic VTI 40–75%, stat arb + vol overlay + options, overlap/chunk/co-fire on — `run_paper_bot.py`, `PAPER_CHASE_MODE=1`, portal paper user.

## 3. Monitor & ops
- **`dashboard_app.py`**: CustomTkinter desktop monitor (primary) — 60s refresh, optional RSS via psutil, journal tail reads.
- **`status.py`**: CLI at-a-glance — live + paper equity, regime, stack flags, heartbeat age (STALE > 90 min).
- **`portal.py`**: Friend onboarding — login, Alpaca keys, bot start/stop (Streamlit).
- **`modules/thinking_engine.py`**: Opt-in Ollama PM tilts — paper default off; live requires manual approval; non-blocking background thread.
- **`scripts/background_runner.py`**: Scheduled health checks — heartbeat staleness (30 min), safety snapshot, optional paper supervisor auto-start.

## 4. Removed legacy (Phase 3.1 — complete)

Superseded by `modules/pipeline_strategies.py` + `config.UNIVERSE`. **No trading logic changed** — dead entry points and duplicates only.

| Removed | Notes |
|---------|--------|
| `simulate.py` | Pre-pipeline sandbox (Phase 1 RAM cleanup) |
| `strategy/strategy.py`, `strategy/strategies.py` | Orphan strategy stubs |
| `strategy_module/` | Duplicate logic layer |
| `modules/advisor_ranker.py` | Pre-2026 advisor loop |
| `modules/market_screener.py` | Duplicate of universe/screener paths |
| `watchlist.py`, `watchlist.txt` | Duplicate of `config.UNIVERSE` |
| `portfolio_config.py` | Unused static PORTFOLIO dict |
| `scripts/dev/` | Broken prototypes: `test_logic.py`, `test_signals.py`, `test_strategy.py`, `trading_bot.py`, `market_screener.py` |

**Moved:** `scripts/dev/test_kraken_budget.py` → `tests/test_kraken_budget.py`

**Kept (not legacy):** `scripts/research/simulate_wayback_sentiment.py` — Wayback sentiment research tool (different from removed `simulate.py`).

## 5. Memory & I/O (Phase 3.2 — complete)

Bounds on RAM and file I/O for long-running bots and dashboard. **No trading logic changed.**

| Change | Location |
|--------|----------|
| Journal tail reads (last N CSV rows) | `dashboard_app._read_csv_tail()`, `_read_trade_journal_csv()` |
| Log tail seek from EOF | `modules/portal_bot.read_bot_log_tail()` |
| Backtest in-process cache cap + trim | `modules/backtester_core._DATA_CACHE_MAX_ENTRIES`, `_trim_data_cache()` |
| Release matrices between compare arms | `release_backtest_memory()` called from `backtester.py` |
| Dashboard refresh debounce | `dashboard_app.refresh_data()` — `_refresh_busy` / `_refresh_pending` |
| Optional process RSS in status bar | `dashboard_app._process_rss_mb()` (requires `psutil`) |

## 6. Wrap-up (Phase 3.3 — complete)

Documentation and manifest refresh only. See README sections: **System overview**, **Memory & performance**, **Live vs paper**, **Long-running stability**, **Remaining cleanups**.

**Regenerate compact manifest:** `python scripts/mcp/export_bot_manifest.py` → `data/bot_manifest.txt`

## 7. Remaining cleanups (non-blocking)

| Item | Where | Action |
|------|-------|--------|
| Stat-arb orphan warnings | `run_all.py` startup reconcile | Informational — paper Profile B; orphans = Alpaca positions not in stat-arb ledger |
| Kraken xStocks API off banner | `run_all.py` `_print_kraken_banner()` | Enable tokenized permission on Kraken key, or `KRAKEN_AUTOPILOT_ENABLED=false` for Alpaca-only |
| Kraken vs Alpaca live overlap | `preflight.py` | Prefer Alpaca-only for ~$100 live |
| Thinking engine calibration | paper opt-in | Keep off live; backtest with `--simulate-live-thinking` first |
| Universe screener age | `status.py` | Refresh weekly on paper: `scripts/analysis/universe_screener.py` |
| Legacy Streamlit dashboard | `dashboard.py` | Backup only; `dashboard_app.py` is primary |
