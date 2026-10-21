# Lab Next

Separate virtual-paper application. It does not replace Live, Medium, or the current Lab, and it does not submit broker orders.

There is no `AGENTS.md` in this repo. `best_strategies/PROTOCOL.md` and `QUEUE.md` were read first. This package is not a protocol promotion. The rolling parameter selector in `research/learn_100_30_20261019` was rejected (76.65% versus the fixed baseline 87.21% on the same 720 sessions). Lab Next does not revive that selector. It learns only an entry ranking from completed virtual trades. Prediction error is not evidence that the book makes money.

## What the learner can change

After 100 complete exchange sessions and at least 30 matured baseline round trips, a ridge-style linear model may reorder names that already pass the fixed gates: positive MA50 extension, simple RSI14 below 70, and the 10:15 entry window through the bar before the session's last bar. The model is kept only when its validation MAE beats a constant trained on the earlier exit dates. It is then frozen for 30 further complete sessions. A failed or thin check uses the fixed extension ranking.

It cannot rewrite code, exits, the 5% hard stop, the 2% close trail, the 30-session hold, the 30-minute cooldown, position caps, or any production setting.

## Exit, on five-minute closes

This book watches completed regular-session five-minute **closes**. It does not see ticks or intrabar highs.

- Arm when a close is at least 5% above the filled entry.
- Remember the highest close since the fill.
- A close at or below 98% of that peak latches a full exit. A 1.99% retreat does not.
- A later bounce does not cancel the latch.
- The sell fills at the next scheduled bar's open, minus 5 bps. If that bar is missing, the next observed open is used and marked `gap_fill`.
- The same symbol can be bought again 30 minutes after that virtual fill. Another symbol can be bought on schedule.
- A new buy is cancelled if the next frame is not the next scheduled bar. That is not a "next available" fill.

The hard stop is a close at or below 95% of the filled entry. The maximum hold is 30 exchange sessions after the entry session. There is no averaging down and no learned increase in size or risk. New buys also stop for the session after a 2% drop from the prior session's equity, and they stay stopped for the run after a 15% peak drawdown. Protective exits continue.

## Calendar and corporate actions

Session dates through 2026-10-14 come from `research/nyse_cash_abc/data/calendar.json`. Close times from 2026-08-26 through 2027-01-15 come from the Alpaca calendar copy, including the 13:00 early closes on 2026-11-27 and 2026-12-24. A date that is only on the session list cannot become a complete training session, because its close time is not verified. A holiday that is on neither list is rejected.

`actions_checked: true` is not an audit. Synthetic frames must say so. Market frames need an independent ledger. Cash dividends are credited. Splits and stock dividends adjust shares, basis per share, and indicator history together. Mergers, renames, and spin-offs stop processing and name the event. Split-adjusted cache prices are not accepted as raw bars.

## Commands

From `stock-bot/research/lab_next`, with the existing Python 3.11 environment. Do not pip-install into it.

```
C:\Users\Owner\PythonTrading\venv311\Scripts\python.exe -B -m unittest discover -s tests -v
C:\Users\Owner\PythonTrading\venv311\Scripts\python.exe -B -m lab_next.smoke
C:\Users\Owner\PythonTrading\venv311\Scripts\python.exe -B -m lab_next.diagnose
C:\Users\Owner\PythonTrading\venv311\Scripts\python.exe -B -m lab_next.market --inspect
C:\Users\Owner\PythonTrading\venv311\Scripts\python.exe -B -m lab_next.market --forward-check
C:\Users\Owner\PythonTrading\venv311\Scripts\python.exe -B -m lab_next --run smoke --report-only
C:\Users\Owner\PythonTrading\venv311\Scripts\python.exe -B -m lab_next --run smoke --status
```

`--status` only reads the saved run. It does not start a process. There is no service to install. Resume a file feed with the same `--run` and `--feed`; an unchanged frame is skipped. A revised old frame is refused. Stop means do not launch the command.

`lab_next.smoke` writes a synthetic feed under `runtime/` and replays it. The second pass processes zero new frames. Those numbers are not market results. `lab_next.diagnose` reads the local five-minute cache and writes why a tradable historical feed is blocked. It does not tune anything and does not create virtual orders from those bars.

Runtime databases, the synthetic feed, and diagnostic output are gitignored.
