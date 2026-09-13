# Forward paper sleeve attribution (7d)

Generated: 2026-09-06 19:30 UTC
Window start: 2026-08-30
Journal: `C:\Users\Owner\PythonTrading\stock-bot\data\portal\users\dawimberly\books\alpaca_paper_v2\paper_journal.csv`
Freeze: See FORWARD_PAPER_FREEZE.md (2-4 weeks, no new features)

Period equity: $95,050.41 -> $100,421.52 (+5.65%) | equity_source: `journal_equity_marks` | closed exits: 78

## data_quality

- journal_path: `C:\Users\Owner\PythonTrading\stock-bot\data\portal\users\dawimberly\books\alpaca_paper_v2\paper_journal.csv`
- heartbeat_path: `C:\Users\Owner\PythonTrading\stock-bot\data\portal\users\dawimberly\books\alpaca_paper_v2\bot_heartbeat.json`
- rows_in_window: 2759 / rows_total: 2759
- window_ts: 2026-09-01 16:13:56+00:00 → 2026-09-06 14:28:29+00:00
- equity_source: `journal_equity_marks` (marks_in_window=2703)
- fills_count: 53 | exits_count: 78 | spy_fills: 0
- event_counts: `{'cycle': 2502, 'fill': 131, 'signal': 45, 'startup': 42, 'exit': 25, 'error': 14}`
- equity_fallback_chain: ['journal_equity_marks', 'journal_prewindow_start+heartbeat_end', 'heartbeat_equity_end_only', 'missing']

## Sleeve table

| Sleeve | Fills | Exits | Realized PnL | Win rate | Unrealized | Open val | Open |
|--------|------:|------:|-------------:|---------:|-----------:|---------:|-----:|
| nyse | 45 | 78 | $-705.81 | 54% | $840.31 | $64,179.75 | 15 |
| core | 0 | 0 | $0.00 | n/a | $268.09 | $33,268.08 | 1 |
| vti_core | 0 | 0 | $0.00 | n/a | $0.00 | $33,268.08 | 1 |
| vti | 8 | 0 | $0.00 | n/a | $0.00 | $0.00 | 0 |
| spy | 0 | 0 | $0.00 | n/a | $0.00 | $0.00 | 0 |
| crypto | 0 | 0 | $0.00 | n/a | $0.00 | $0.00 | 0 |
| metal | 0 | 0 | $0.00 | n/a | $0.00 | $0.00 | 0 |

## vs STRICT envelope (honesty check)

- Reference: STRICT paper windows (~90d) (scripts/analysis/eval_strict_windows_last.md)
- Envelope expected return: +1.21%
- Observed period return: +5.65%
- Delta: +4.44pp
- Note: 90d STRICT window scaled by 7/90 (envelope only)
- Caveat: Short live-paper samples are noisy; do not retune from this delta. Dashboard Sharpe over days/weeks is not comparable to STRICT backtest Sharpe.

## Notes

- Forward-paper freeze: measure only - no .env / live / sleeve changes from this report.
- SPY fills on paper should be ~0 (satellite OFF). Non-zero SPY fills -> check restart after lock.
- Closed exits = event in {exit,sell,close} OR sell-side trade event. Cycle rows are marks, not exits.
- journal_load: candidate paper_journal.csv: rows=2759 ts_max=2026-09-06 14:28:29+00:00
- journal_load: candidate paper_journal.csv: rows=25048 ts_max=2026-09-01 16:10:53+00:00
- journal_load: candidate paper_chase_journal.csv: rows=950 ts_max=2026-08-13 22:52:56+00:00
- journal_load: candidate paper_journal.csv: rows=375 ts_max=2026-08-05 13:11:26+00:00
