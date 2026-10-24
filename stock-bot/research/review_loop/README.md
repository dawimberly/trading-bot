# Automated trade and strategy review

The owner objective is a self-improving trading research process targeting **50% net annual return**, before personal taxes. That is a research objective, not a forecast, guarantee, leverage permission or deployment requirement that overrides risk controls. A losing or inconclusive result remains valid evidence.

## Implemented now
A Python standard-library evidence reviewer reads prepared trade-review JSON for Live, Medium and Lab, identifies stale/incomplete history, counts original purchase plans and matured 30-session post-sale paths, and imports the completed ranking-learner experiment verdict. It deliberately does not infer completed round trips from unknown inventory, fill in missing returns with zero, call price-only follow-through a sale-quality verdict or declare short development results an annual target achievement.

The output is a sanitized review.json and review.md. Account identifiers, account hashes, order IDs, raw positions and credentials are not exported. Public posting of any new source fields requires an explicit allowlist review. The supplied local reports currently date from October 18/19 UTC and have unresolved corporate-action/inventory history; the first review flags evidence gaps for all three books.

This runner has no broker client, credential access, production imports, executor, model trainer, deployment hook or self-scheduler. It is the review foundation, not an already operating autonomous strategy search or fresh broker-data collector.

## Intended automatic loop

1. Capture as-of candidate lists, scores/components, rejects, quotes, sizing, effective rules and the plan before each order. Reconcile broker fills, partials, fees, dividends, external cash and corporate actions by book. Avoid account data on public GitHub.
2. Update completed trade outcomes and in-trade adverse/favorable excursions at each session close. Keep following each sale for 30 exchange sessions. Separate price-only hindsight from actual net replacement-capital performance. Rejected-candidate labels require the full saved as-of candidate universe and an explicit hypothetical execution policy.
3. Review selection, entry timing, execution/spreads, exits, profit giveback, hold duration, sizing, concentration, sector exposure, regimes, idle cash, turnover, costs and plan adherence. Report sample sizes and unavailable aspects. Daily collection is not permission to retrain every second.
4. Run a bounded weekly research queue against the employed baseline. Fixed rules and adaptive models compete on identical data, dates, corporate actions, costs and capital. Pre-register one behavioral change per experiment. Track all trials and rejected ideas; no hidden searches or repeated peeking at the holdout.
5. Evaluate chronological out-of-sample return, drawdown, downside/tail behavior, turnover, exposure, matched SPY/VTI total returns and higher-cost sensitivity. Use the existing best_strategies/PROTOCOL.md as the promotion authority; two required windows cannot be replaced by one good short test. Missing benchmark or action evidence is a block. Monte Carlo may reject, never promote.
6. Challenge the incumbent first in an isolated shadow/paper comparison. Pin model versions, training cutoffs, features, baseline, code and input hashes. Predictions use only information available then. Keep an incumbent for rollback. Promotion and rollback execution are a later, separately tested integration; do not mutate the broker from a review script.
7. Live remains explicitly owner-gated. Medium stays an owner-gated comparison book. Lab is the experimental venue. Do not add leverage, widen stops, relax freshness gates, purchase data or increase risk just to reach 50%.

## Cadence and resource bounds
- At every decision/fill: observational capture through the already authorized bot path; no forced signals.
- After each session: reconciliation, performance attribution and 30-session follow-through refresh when the input bridge is installed.
- Weekly: review data quality, experiments and trial registry; launch only pre-registered bounded experiments with usable inputs.
- Model updates: use the frozen experiment schedule; existing ranking-learner research uses 100 prior sessions and a 30-session freeze. This is an experiment setting, not universal proof of the best cadence.
- Initial research budget: zero additional data spend; one local research job at a time; six-hour maximum per job; no unrestricted strategy generator.

## Current integration work remaining
- Reconcile uncommitted deployed Lab code with the older draft PR #17; a GitHub branch is not proof of running code.
- Build a GET-only, account-pinned fresh evidence exporter into a separate local research directory. It must preserve raw evidence privately, atomically export sanitized summaries, and never import an order executor.
- Resolve corporate actions/initial inventory and collect missing decision/quote/plan history prospectively. Historical missing plans stay missing.
- Add reconciled cash-flow-adjusted equity/benchmark series and in-trade path labels.
- Implement the experiment registry and bounded worker, then shadow model evaluation and controlled paper promotion/rollback. The inspected broker paper-owner currently uses fixed selection despite a model-check gate.

A recurring ChatGPT GitHub review is oversight only. It cannot see local files that were never published. Without a fresh sanitized evidence export, it must report the data gap instead of claiming to have reviewed today's broker trades. GitHub CI tests are also not an account-connected research worker.

## Run
From this directory, using Python 3.11:

    python -B -m unittest discover -s tests -v
    python -B review_loop.py --reports C:/Users/Owner/PythonTrading/stock-bot/reports/trade_review --output C:/workspace/stock_bot_review_loop/runtime/latest

Optional completed-study input:

    --experiment C:/workspace/lab_next_learning_study_20261023/RESULTS.json

Reports must match their expected book IDs and include timezone-aware as-of timestamps. Outputs are generated artifacts and should stay out of git. The script reads supplied reports, not .env files. No trading process needs a restart.

## Target measurement
Compare cash-flow-adjusted, net-of-cost portfolio returns on the same dates. Show actual cumulative return for periods shorter than 252 sessions; do not extrapolate them into a claimed 50%-annual accomplishment. At least 252 reconciled sessions permit a historical annualized calculation, but do not establish future attainability. Long-window robustness, risk and protocol gates remain separate from the numerical target.
