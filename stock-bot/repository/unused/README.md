# Unused / research archive

Code parked here is **not** part of the running paper or live bots.

## eod_winner_trim/

EOD fade-trim experiment (Sep 2026). Backtested vs alpaca_paper_v2 (33% VTI / 67% NYSE):

- Daily bars could not model intraday “weak close” fades
- **0 partial trims** fired on 365d / 180d A/B
- Never wired into `run_all.py` / paper bot

Keep **off**. Do not re-enable without intraday OHLC validation.

## Orphan flags (still in config, default false)

| Flag | Status |
|------|--------|
| `PAPER_SCALING_STRATEGY_ENABLED` | Module deleted; status.py note only |
| `PAPER_PROFIT_TARGET_ENABLED` | Backtest-only (`profit_target.py`); bots hard-gated |
| `PAPER_EOD_WINNER_TRIM_ENABLED` | Removed from config after archive |
