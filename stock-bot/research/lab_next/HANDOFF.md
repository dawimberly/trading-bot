# Lab Next handoff

**BUILT.** Tradable market fills are **BLOCKED**. The app is **not RUNNING**.

Source: `C:\Users\Owner\PythonTrading\stock-bot\research\lab_next`

The reviewed prototype in `C:\workspace\lab_next_20261021` was not copied as a runtime database. Defects found after its passing tests are listed in `AUDIT.md` and fixed here. No existing book, setting, process, scheduled task, order, `.env`, or launcher was changed. Nothing was committed or pushed.

## Market feed

A read-only inspector is connected to `stock-bot/data/intraday_cache`. It saw 112 symbol files and opened a sample of 112,761 rows. It does not import an order executor and it does not use broker credentials.

It will not emit a virtual-paper feed. The cache is split-adjusted, not raw. There is no independent ledger for dividends, mergers, renames, or spin-offs. The file list is a saved snapshot, not point-in-time S&P membership. SPY and VTI total-return series are absent, so they are unavailable rather than zero. The newest stored bar is `2026-10-09T12:40:00-04:00`, which is stale for a forward pass on 2026-10-21. The diagnostic wrote `runtime/historical_diagnostic/DIAGNOSTIC.md` and created no virtual orders. It was not used to tune anything.

## Process

No background process exists. `python -m lab_next --run smoke --status` reports `running: false`, `pid: null`, 468 stored synthetic frames, last bar `2026-09-09T15:55:00-04:00`. That state is a finished file replay, not a live loop.

## What the learner can change

Only the ranking among names that already pass the fixed gates, and only after the sample, chronological MAE, and 30-session freeze rules in `README.md`. Until then, and whenever the prediction check fails, both books use MA50 extension. The learner cannot change exits, the hard stop, the 2% close trail, the hold, the cooldown, size, or risk, and it cannot touch production settings. An accepted model would still not be a portfolio-profitability promotion. The rejected 100/30 parameter selector stays rejected.

## Tests and smoke

`unittest discover -s tests -v`: **24 passed**.

Covered there: exact 2.00% versus 1.99% on the close, an intrabar low that does not fire, inclusive 5% hard stop, latched exit through a bounce, cooldown at the exact 30-minute mark across a reopened database, cancelled non-adjacent buys, labeled gap sells, holiday rejection, the 42-bar early close, a missing symbol bar resetting indicators, split economics, cash dividend credit, merger block, matured-versus-future labels, the 30-session freeze, and the market-feed block.

Synthetic smoke: first pass **468** frames, second pass **0** new frames, **6** complete sessions. Both books finished on the same synthetic path because training cannot start at 6 sessions. That path is plumbing. It is not market evidence and it is not validation.

Historical diagnostic: **BLOCKED**, 0 virtual fills, 5 blockers, not tuned.

## Remaining blockers

- Raw five-minute OHLC with as-of provenance. The current cache is split-adjusted.
- An independent corporate-action ledger covering the same symbols and dates, including dividends and the events that must block (mergers, renames, spin-offs).
- An explicit frozen as-of universe. The cache directory is not historical index membership.
- Verified SPY and VTI total-return series on the same dates, including dividends.
- A fresh completed bar if a forward virtual pass is wanted. Collection stays one-shot. Do not install a service, and do not connect a broker executor.

## Commands

From `C:\Users\Owner\PythonTrading\stock-bot\research\lab_next`:

```
C:\Users\Owner\PythonTrading\venv311\Scripts\python.exe -B -m unittest discover -s tests -v
C:\Users\Owner\PythonTrading\venv311\Scripts\python.exe -B -m lab_next.smoke
C:\Users\Owner\PythonTrading\venv311\Scripts\python.exe -B -m lab_next.diagnose
C:\Users\Owner\PythonTrading\venv311\Scripts\python.exe -B -m lab_next.market --forward-check
C:\Users\Owner\PythonTrading\venv311\Scripts\python.exe -B -m lab_next --run smoke --status
C:\Users\Owner\PythonTrading\venv311\Scripts\python.exe -B -m lab_next --run smoke --report-only
```

Stop: do not launch a command. There is no service and no PID to kill. Resume: run the same `--run` with the same `--feed`. Unchanged frames are skipped. A changed old frame or a changed spec hash requires a new run name.
