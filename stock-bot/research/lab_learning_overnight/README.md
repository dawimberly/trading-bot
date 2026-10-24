# Frozen overnight Lab learning batch

This is offline research only. It cannot submit broker orders, change a book or deploy a model. The owner goal is a self-learning Lab that improves on Live and Medium, targeting 50% net annual return. No result is guaranteed; the worker does not search until it can manufacture a winner.

## Batch
Three registered learners, each at 5 and 10 basis points per side: pairwise_rank, median_linear and local_expectancy. The old rejected ranking model remains rejected and is not re-run under a new name. Each challenger changes only ranking-model objective/class. Original eligibility, sizing, exits, stops, trail, cooldown and drawdown limits stay fixed.

The input is the existing raw-SIP development cache: 247 continuous sessions and 29 retrospectively selected names. First 100 sessions generate training evidence; four full 30-session evaluation folds plus a 27-session tail follow. Model updates use only matured baseline outcomes, purged chronological inner splits and 30-session freezes. No rejected-candidate outcomes are invented.

All trials and failures are retained. Development screening requires strictly greater net return than fixed Lab ranking, drawdown within two percentage points, improvement in both chronological halves, outperformance with doubled costs and reconciled ledgers. A development pass is not promotion: required independent protocol windows, verified provenance and faithful Live/Medium decision history are missing. No Monte Carlo result can promote anything.

## Worker
One child replay at a time; six-hour total budget, 45-minute per-job budget, 2 GiB private-memory limit and Below Normal priority. It requests temporary prevention of idle system sleep while alive, without changing the power plan or keeping the display on. The computer must remain powered on.

Windows venv launcher monitoring was corrected: replays execute in the actual base interpreter, with existing venv site-packages on PYTHONPATH for tzdata. Memory and priority apply to that actual PID. Interrupted initial output is preserved; replacement outputs end in _r2. No strategy or pass-rule tuning occurred in that engineering restart.

registry.json, per-job progress/checkpoints and REPORT.md/REPORT.json preserve evidence locally. After job transitions the worker publishes sanitized research metrics inside the overnight_research field of latest_review.json on branch codex/research-review-evidence-20261024. Broker records and balances are not included. The shared daily research lock serializes Git publication with the existing daily review; a busy/failed publication leaves local results intact.

The worker completes the frozen queue once, or stops on budget/data/accounting defects. It does not run indefinitely until a favorable test appears. It does not claim faithful superiority over Live or Medium from their unmatched historical rules or inherited holdings.

## Local entrypoint
From C:/workspace/lab_next_overnight_20261024:

    C:/Users/Owner/PythonTrading/venv311/Scripts/python.exe -B worker.py

One worker is already active. Do not start a second or delete WORKER_LOCK without checking the recorded process and source path. This source is reviewable on GitHub; market data remain in the original private local study cache and are not committed.

Eight focused learner/supervisor tests passed. The copied base engine's earlier accounting and differential checks are separate evidence; these tests do not prove profitability or available real-time execution. A morning ChatGPT review is scheduled to read published results. Live, Medium and the running Lab are unchanged.
