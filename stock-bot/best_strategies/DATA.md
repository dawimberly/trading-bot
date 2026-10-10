# Data inventory

Trust order: **Alpaca book state > SQLite PIT prices > Alpaca OHLC gaps >
heartbeat MTM > journals**. Journals are not a blotter.

## Price

| Store | What | Use |
|-------|------|-----|
| SQLite market DB (`config.resolve_db_path` / `ensure_market_db`) | Daily closes, PIT | NYSE walks, MA, ATR, STRICT |
| Alpaca daily OHLC | Open/high/low/close | Overnight **gap** = open/prior close from the **same** feed |
| Alpaca 30m RTH | First hour 9:30–10:30 ET | WOP / FHDS only |
| yfinance | Fallback / TOD hourly | Never promote from yf-only |

Alignment rules that already bit us:

- Naive sqlite midnight treated as UTC shifts the calendar date. Normalize
  naive indexes as dates; do not `utc=True` then convert to NY.
- Mix sqlite close with Alpaca open **levels** → phantom 5–10× trades. Apply
  the Alpaca gap to the sqlite close.
- Sqlite extra sessions (e.g. Sunday 2026-09-20) break weekday labels.
  Intersect with Alpaca RTH majority calendar.

## Books / ops

| Store | Book | Notes |
|-------|------|--------|
| `data/portal/users/dawimberly/books/alpaca_paper/` | Aggressive | Agent-controlled. VTI 0, NYSE ~95%. |
| `data/portal/users/dawimberly/books/alpaca_paper_v2/` | Medium SoT | Owner-signed desktop UI. Weekly review SoT. |
| `data/portal/users/dawimberly/books/alpaca_live/` | Live | Read-only from this folder. |
| Heartbeats | all | MTM, not realized. |
| `paper_journal.csv` | paper | Ragged rows. Pad via `read_csv_file`. Executor does not log every fill. |

## Research artifacts (inputs, not promote)

| Artifact | Window | Keep |
|----------|--------|------|
| `scripts/analysis/paper_v2_best_backtest_last.json` | 365d + 1000d | 8 vs 15 vs 25 names |
| `scripts/analysis/eval_name_count_6_8_last.md` | 365d + 1000d | 6 fails vs 8; stay Lab 8 |
| `scripts/analysis/one_r_hit_backtest_last.json` | ~365d | hold vs 1R vs 3× vs 45d |
| `scripts/analysis/weekday_gain_exit_grid_last.json` | 242 RTH bars | A1–A3 × B1–B3 |
| `scripts/analysis/weekday_gain_open_last.json` | 2y OHLC | Thursday fade / Monday continuation |
| `scripts/analysis/weekday_first_hour_last.json` | 365d 30m | WOP not Monday-specific |
| `scripts/analysis/friday_flatten_backtest_last.json` | 365d / 1000d | flatten loses ~19–33 pp |
| `scripts/analysis/paper_lab_concentrated_backtest_last.json` | 365d | 8-name trail, high DD |
| `scripts/analysis/eval_strict_vs_full_last.md` | 365d | STRICT > FULL on old VTI mix (−1.4 pp overlays) |
| `scripts/analysis/eval_strict_vs_full_employed_last.md` | 365d Lab 8 VTI=0 | FULL +6.06pp vs STRICT; not PIT; no overlay change |
| `scripts/research/exhaustive_campaign/` | MC 200/200 | fat right tail, ugly left; do not promote |
| `scripts/research/open_price_predict_last.md` | 252d | prior close beats models |
| `scripts/analysis/eval_fat_loser_ab_last.md` | 365d + 1000d NYSE walk | open −1% drip kills book |
| `data/weekly_review_*.md` | Saturdays | 2026-09-19 memo is **stale grade C** (Lab CSV). Loader now pads Medium SoT. Re-run before using that HOLD. |

## Quality grades

Weekly review A/B/C is **equity-path cleanliness**, not strategy skill.
Grade C → no mandate, no `.env`. Same spirit as VTI shadow D1–D6
(`scripts/research/vti_tactical_shadow/PROMOTE_CRITERIA.md`).
