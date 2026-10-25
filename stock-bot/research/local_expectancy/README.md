# local_expectancy — frozen test

Research only. This folder does not submit orders, edit a book env, or relax the seven-minute entry quote cap.

## Hypothesis

Among names that pass the fixed eligibility gate (positive ma50 extension and RSI14 below 70), ranking by the mean net return of the 15 nearest matured baseline trades beats ranking by ma50 extension alone, after costs, on two disjoint 365-day windows, without a drawdown more than 2.0 percentage points worse.

The rank score is the only change. Stops, hold, sizing, and fills stay put. `pairwise_rank` and `median_linear` stay rejected and are not rerun.

The rules are in `preregistration.json`, written before the returns. They follow `best_strategies/PROTOCOL.md` at commit `8c819ac` (that file is not on main): one knob, two windows, costs on, failures kept.

## What actually runs

`modules/portal_bot.py` only starts and stops processes. Paper books enter through `run_paper_bot.py`; live enters through `run_all.py`. The checked-in ranker in `_equity_momentum_candidates` is `close / MA - 1` on the cycle matrix (50 rows, 20 for an IPO), not the MA70 figure in the startup banner. Scanner boosts are separate knobs and are not part of this test.

The unfinished challenger lived in the Lab Next research engine: fixed score = ma50 extension, learner score = local expectancy, fill on the next bar or cancel. This study keeps that ranker and that fill rule. It is not a replay of the portal stack.

## Data

Free Yahoo daily bars, adjusted with one scale (`adjclose / close`). Paid SIP is not used. A Yahoo 5-minute request older than the recent window returns HTTP 422, so the five-minute clock cannot cover the protocol windows. The private 247-session cache is not in this repo. Daily bars are the protocol price path, and that substitution is declared in the preregistration. It can reject the idea. It cannot clear the original five-minute execution.

Historical opens are modeled at quote age 0. `ENTRY_QUOTE_MAX_AGE_SECONDS` stays 420. A delayed quote fails that check and is not an entry feed.

## Run

```bash
cd stock-bot/research/local_expectancy
python -m unittest discover -s tests
python run_study.py --refresh
```

`results.json` is the gate output. Promotion stays false.
