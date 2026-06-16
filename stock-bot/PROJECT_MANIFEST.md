# PythonTrading — Project Manifest

## 1. Core loop
- **`run_all.py`**: 24/7 orchestrator — data refresh, wisdom regime, sleeve strategies, Alpaca execution, heartbeat.
- **`modules/pipeline_strategies.py`**: Shared crypto z-pair, SPY MA200, and NYSE MA50 logic (live + backtester).
- **`backtester.py`**: Mirrors the live stack on historical daily bars.

## 2. Profiles
- **Profile A (live)**: 90% VTI core, yield-gate-only, small-account safety — see `config.py` / `current_dynamic`.
- **Paper v2.1**: Aggressive paper chase profile — isolated env via `run_paper_bot.py`.

## 3. Data & ops
- **`fetch_data.py`** + **`modules/data_loader.py`**: SQLite OHLCV (5m live, daily history).
- **`dashboard_app.py`**, **`status.py`**, **`portal.py`**: Monitor and friend portal (no legacy advisor-ranker loop).

## 4. Removed legacy (Phase 3.1)
Pre-2026 advisor ranker, watchlist duplicates, `scripts/dev/` prototypes, and orphan `strategy/` stubs — superseded by `pipeline_strategies` + `config.UNIVERSE`.
