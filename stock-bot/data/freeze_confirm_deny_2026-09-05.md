# Freeze weekly confirm/deny plan — week ending 2026-09-05

Generated: 2026-09-05 14:15 UTC

**You confirm or deny. Nothing auto-applies.** Freeze-safe: no .env writes, no live Profile A, no new sleeves.

## Decisions required

| # | ID | Default | Your call | Item |
|---|----|---------|-----------|------|
| 1 | `keep_freeze` | **CONFIRM** | CONFIRM / DENY / HOLD | Keep forward-paper freeze (no new features) |
| 2 | `keep_spy_off` | **CONFIRM** | CONFIRM / DENY / HOLD | Keep paper SPY satellite OFF |
| 3 | `no_live_change` | **CONFIRM** | CONFIRM / DENY / HOLD | No live Profile A changes |
| 4 | `no_new_sleeve` | **DENY** | CONFIRM / DENY / HOLD | Deny any new sleeve / Iran / headline module |
| 5 | `journal_parse_error` | **HOLD** | CONFIRM / DENY / HOLD | Journal had bad CSV lines (skipped) |
| 6 | `daily_errors_present` | **HOLD** | CONFIRM / DENY / HOLD | Today's error log has 1 error(s) |
| 7 | `attribution_stale` | **CONFIRM** | CONFIRM / DENY / HOLD | Sleeve attribution stale (401h) |

## How to respond

1. Open this file in PyCharm (or Telegram summary).
2. Reply Telegram e.g. `FREEZE CONFIRM keep_freeze,keep_spy_off DENY no_new_sleeve`
3. Or edit a copy: mark Your call column — still manual; bot will not read it until you ask.
4. **Default if no reply: all DENY/HOLD as listed — freeze continues unchanged.**

## Standing rationale

- `keep_freeze` (CONFIRM): Continue measure-only until freeze end date.
- `keep_spy_off` (CONFIRM): 365d STRICT confirmed; live SPY unchanged.
- `no_live_change` (CONFIRM): Live SPY-off rejected on 365d live-shaped A/B.
- `no_new_sleeve` (DENY): Geopolitics stays research sidecar only.

## This week's hygiene trail

- 2026-08-30: `freeze_daily_2026-08-30.md` (2085 bytes)
- 2026-08-31: `freeze_daily_2026-08-31.md` (2087 bytes)
- 2026-09-01: `freeze_daily_2026-09-01.md` (1717 bytes)
- 2026-09-02: `freeze_daily_2026-09-02.md` (1717 bytes)
- 2026-09-03: `freeze_daily_2026-09-03.md` (1717 bytes)
- 2026-09-04: `freeze_daily_2026-09-04.md` (1718 bytes)

## Measurement snapshots

- Attribution: `present` — forward_sleeve_attr_last.md
- Geo event study: `present` — geopolitical_event_study_last.md
- Weekly review: `present` — weekly_review_latest.md
- Heartbeat age: 0m
- Equity: 100421.52
- Regime: RHYME_C: Steady_Bullish_Growth

## Full detail

### Hygiene findings

- `journal_parse_error` [cleanup] default=HOLD: Journal had bad CSV lines (skipped) — Used on_bad_lines=skip for paper_chase_journal.csv
- `spy_fills_ok` [info] default=HOLD: No SPY sleeve fills in window — Consistent with SPY-off freeze lock.
- `daily_errors_present` [cleanup] default=HOLD: Today's error log has 1 error(s) — Review obvious repeats; do not retune strategy from errors alone.
- `attribution_stale` [cleanup] default=CONFIRM: Sleeve attribution stale (401h) — Run: python scripts/analysis/forward_sleeve_attribution.py

### Attribution excerpt

```
# Forward paper sleeve attribution (14d)

Generated: 2026-08-19 20:51 UTC
Window start: 2026-08-05
Journal: `C:\Users\Owner\PythonTrading\stock-bot\data\portal\users\dawimberly\books\alpaca_paper\paper_journal.csv`
Freeze: See FORWARD_PAPER_FREEZE.md (2-4 weeks, no new features)

Period equity: $97,379.30 -> $97,003.26 (-0.39%) | equity_source: `journal_equity_marks` | closed exits: 9

## data_quality

- journal_path: `C:\Users\Owner\PythonTrading\stock-bot\data\portal\users\dawimberly\books\alpaca_paper\paper_journal.csv`
- heartbeat_path: `C:\Users\Owner\PythonTrading\stock-bot\data\portal\users\dawimberly\books\alpaca_paper\bot_heartbeat.json`
- rows_in_window: 4811 / rows_total: 18676
- window_ts: 2026-08-05 20:51:33+00:00 → 2026-08-19 15:50:31+00:00
- equity_source: `journal_equity_marks` (marks_in_window=4568)
- fills_count: 12 | exits_count: 9 | spy_fills: 0
- event_counts: `{'cycle': 4388, 'startup': 235, 'signal': 119, 'exit': 40, 'fill': 21, 'error': 5, 'exit_error': 3}`
- equity_fallback_chain: ['journal_equity_marks', 'journal_prewindow_start+heartbeat_end', 'heartbeat_equity_end_only', 'missing']

## Sleeve table

| Sleeve | Fills | Exits | Realized PnL | Win rate | Unrealized | Open val | Open |
|--------|------:|------:|-------------:|---------:|-----------:|---------:|-----:|
| core | 0 | 0 | $0.00 | n/a | $-160.82 | $48,633.68 | 1 |
| vti_core | 0 | 0 | $0.00 | n/a | $0.00 | $48,633.68 | 1 |
| nyse | 12 | 9 | $0.00 | 0% | $630.08 | $18,583.84 | 23 |
| metal | 0 | 0 | $0.00 | n/a | $2.14 | $367.44 | 1 |
| spy | 0 | 0 | $0.00 | n/a | $0.00 | $0.00 | 0 |
| crypto | 0 | 0 | $0.00 | n/a | $0.00 | $0.00 | 0 |

## vs STRICT envelope (honesty check)

- Reference: STRICT paper windows (~90d) (scripts/analysis/eval_strict_windows_last.md)
- Envelope expected return: +2.43%
- Observed period return: -0.39%
- Delta: -2.82pp
- Note: 90d STRICT window scaled by 14/90 (envelope only)
- Caveat: Short live-paper samples are noisy; do not retune from this delta. Dashboard Sharpe over days/weeks is not comparable to STRICT backtest Sharpe.

## Notes

- Forward-paper freeze: measure only - no .env / live / sleeve changes from this report.
- SPY fills on paper should be ~0 (satellite OFF). Non-zero SPY fills -> check restart after lock.
- Closed exits = event in {exit,sell,close} OR sell-side trade event. Cycle rows are marks, not exits.
- journal_load: candidate paper_journal.csv: rows=18676 ts_max=2026-08-19 15:50:31+00:00
- journal_load: candidate paper_cha
```

### Geo study excerpt

```
# Geopolitical event study (report only)

Generated: 2026-07-29 21:28 UTC
Research DB: `data\research\geopolitical\research_macro.db`

**REPORT ONLY - freeze on; labels only; no trade signals; no promote; no live/paper default changes**

## Fidelity legend

| Grade | Meaning |
|-------|---------|
| macro_only | VIX/WTI (+ available equity proxies) |
| partial_strategy_proxy | SPY + VIX + WTI coverage around event |
| full_freeze_compatible | VTI/SPY/XLE/GLD/VIX/WTI all cover the window |
| insufficient_data | Cannot grade — skip or macro-only if any series |

## Series manifest (research store)

| Series | Source | First | Last | Rows | Checksum |
|--------|--------|-------|------|------|----------|
| VIX | FRED:VIXCLS | 1990-01-02 | 2026-07-28 | 9239 | `48e3e1d55d12eec4` |
| WTI | FRED:DCOILWTICO | 1986-01-02 | 2026-07-27 | 10210 | `a6e9e3c8145804df` |
| SPY | yfinance:SPY | 1993-01-29 | 2026-07-29 | 8431 | `c88b1837c78489a2` |
| VTI | yfinance:VTI | 2001-06-15 | 2026-07-29 | 6316 | `4de3c8ae96583a71` |
| XLE | yfinance:XLE | 1998-12-22 | 2026-07-29 | 6941 | `5bde84e5ef5de99f` |
| GLD | yfinance:GLD | 2004-11-18 | 2026-07-29 | 5456 | `8dd616f190e7a32c` |
| VIX_YF | yfinance:^VIX | 1990-01-02 | 2026-07-29 | 9211 | `cae517c3b710b9d7` |
| CL_F | yfinance:CL=F | 2000-08-23 | 2026-07-29 | 6510 | `b0571391c2842e7e` |
| GC_F | yfinance:GC=F | 2000-08-30 | 2026-07-29 | 6501 | `e1c6498ea45f99ed` |

## Event windows

### 1990-08-02 — Iraq invades Kuwait / Gulf War start

- Grade: **macro_only** (calendar tier: macro_only)
- Sources:
  - [Encyclopaedia Britannica](https://www.britannica.com/event/Persian-Gulf-War)
  - [US State Department Office of the Historian](https://history.state.gov/milestones/1989-1992/gulf-war)

| Series | Pre-20d | Imm-5d | Post-20d | Post-60d | Level@event |
|--------|---------|--------|----------|----------|-------------|
| SPY | n/a | n/a | n/a | n/a | n/a |
| VTI | n/a | n/a | n/a | n/a | n/a |
| XLE | n/a | n/a | n/a | n/a | n/a |
| GLD | n/a | 
```

**Freeze continues unless you explicitly CONFIRM ending it.**
