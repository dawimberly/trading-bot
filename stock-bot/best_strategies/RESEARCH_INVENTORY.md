# Research inventory

As of 2026-10-25. Read-only survey of this repo, its branches, and open
research PRs. No orders were sent and no book config was changed.

Macro, war, inflation, sector rotation, and the yield gate are written up
in `MACRO_REGIME_POSTMORTEM.md`, including one frozen CPI-vintage hypothesis.
None of those earlier attempts is a protocol pass.

There is **no** top-level `research/` directory. The body of work is:

| Location | Where it exists |
|----------|-----------------|
| `stock-bot/best_strategies/` | `origin/cursor/phase4-integration` (and `origin/cursor/fix-live-cycle-500-72e4`). Not on `main` before this note. Protocol, ledger, and measured rejects as of 2026-10-10. |
| `stock-bot/scripts/analysis/` | Drivers and result memos. The `_run_*` tune/HMM scripts and the point-in-time walks are on the phase4 branch. `main` has older integrated A/B memos only. |
| `stock-bot/scripts/research/` | Sleeve and campaign experiments. `main` has early scripts and CSVs. The `*_last.md` writeups and the exhaustive campaign are on phase4. |
| `stock-bot/docs/` on `main` | Appearance and ops. No strategy verdicts. |
| `stock-bot/research/` | Branch-only (PRs 19–21). Not on `main`. |
| `stock-bot/PAPER_RESEARCH_PROFILE.md` | On `main` and updated on phase4. Product lock narrative, not a walk-forward ledger. |

`PROTOCOL.md` (phase4): one knob, two windows, costs on, no lookahead, STRICT
point-in-time for full-stack `backtester.py`. Monte Carlo cannot promote.
Rejected ideas stay rejected. This inventory does not promote anything.

Legend: **WF** = walk-forward or two disjoint windows. **PIT** = point-in-time
membership (not today's screener replayed on history). Costs are what the
writeup states.

## Credible after-cost edge?

**No new knob clears the protocol.** The honest isolated baseline is still
the only result that is both point-in-time and costed on two windows, and
the folder treats it as the baseline rather than as a promote:

- **NYSE MA70, pick_15, 30-day hold, 2× ATR, no 1R**, S&P membership by
  date, 10 bps round trip. 365d (2025-09-16 → 2026-10-10) **+95.5% / DD
  13.8% / Sharpe 1.91**. Full DB (2022-06-11 → 2026-10-10) **+134% / 19.6%
  / 1.06**. Deflated Sharpe 0.71 and 0.16 (90 trials). PSR vs cash is high.
  It is not "best of N" at 95%. Windows are not the 2016–2026 quad.
  Evidence: `best_strategies/findings.md`,
  `scripts/analysis/pit_universe_rerun_last.md` on phase4.

Everything else below either fails that baseline, uses one window, turns
costs off, or replays a fixed or current-day name list.

Single-window full-stack beats that are **not** protocol passes:

- Dynamic VTI ≥40% vs fixed 20% core, 365d paper-aggressive:
  **+26.18% / 1.40 / −7.61%** vs **+18.32% / 1.11 / −8.7%**
  (`scripts/analysis/dynamic_vti_ab_365.json`, phase4). Universe is the
  stack, not shown as S&P point-in-time.
- Stat-arb quality vs fill-rate, same tune, HMM off: **+1.45 pp return,
  +0.07 Sharpe**, 3–4 pairs (`_v154_tune_ab_365_recommendations.txt`).
- GARCH paper ON is locked from a stated 365d A/B (~+1.06 pp, +0.04 Sharpe)
  with an 80d window slightly negative (`PAPER_RESEARCH_PROFILE.md`). The
  compare file is not committed. Multiplier caps at 1.0 (never sizes up).
- `main` `scripts/analysis/final_paper_bot_backtest.md` (365d ending
  2026-06-16, 5 bps equity slip): stack about +50% and +30 pp vs VTI, Sharpe
  1.98. Survivorship is not addressed. Vol overlay PnL is marked synthetic.
  Do not quote it next to the point-in-time walk.

## External 2016–2026 walk (not in git)

Survivorship-free S&P membership, top-1 by MA50 extension, exits at 20
sessions and 30 days, 5 and 10 bps per side, four disjoint periods. Reported
by the stock assistant, not committed here.

| Claim | In this repo |
|-------|----------------|
| Baseline roughly break-even per pick after costs, much worse than a hindsight 29-name universe | **Not the same test** as MA70 pick_15. **Agrees** with the phase4 warning that `current` screener history is lookahead (365d `current` +246% vs S&P PIT +95.5% on the same MA70 walk). PR 19's learner cache is a 29-name retrospective list and is explicitly not a protocol window. |
| Logistic learner overlay fails | **Same verdict, different harness.** Codex `latest_review.json` on `origin/codex/research-review-evidence-20261024`: `pairwise_rank` and `median_linear` are `reject`, `promotion: false`. `local_expectancy` is `pending_or_blocked`. PR 19 says no winner. Not the four-period point-in-time score. |
| ARIMA size multiplier fails; a 30-day pass is mechanical because size never drops below 1.0 | **Duplicate.** 365d tune: ON worse by 0.25 pp. Code default is ×1.08 if forecast > 0 else ×1.0. See `ARIMA_HMM_FINDINGS.md`. The phrase "normalized 2/4" is not in the analysis artifacts. |
| 5-state 252-day HMM fails; time-of-day inputs read present-day state | **Mechanism confirmed. Numeric 0/4 and 2/4 not in git.** No committed `--compare-markov-hmm` table. Profile still documents soft HMM on. Backtests before 2026-10-25 could leak the TOD cache and the wall clock. |

## `best_strategies` ledger (phase4, as of 2026-10-10)

Isolated walk costs are 10 bps round trip unless noted. WF is "no" when the
design is 365d plus full DB (overlapping), not three purged folds.

| Experiment | Method | Windows | WF | PIT? | Costs | Verdict |
|------------|--------|---------|----|------|-------|---------|
| NYSE MA70 pick_15 | Rank vs MA70, 30d, 2× ATR, no 1R, 15 names | 365d + full DB | no | yes (S&P by date) | 10 bps | **Baseline.** See numbers above. |
| pick_8 / pick_25 | Same walk, slot count only | same | no | yes | 10 bps | **Fail** vs pick_15 (8 breaks the 2 pp DD gate; 25 loses return). |
| Lab 8 concentrated | 8 names, wider room, trail | same | no | yes | 10 bps | **Fail DD.** 365d +121% / DD 20.8 vs +95.5% / 13.8. |
| Name count 8 vs 25 on STRICT executor | Full-stack slot cap, VTI off | 365d + 1000d | no | STRICT when enforced | on | **8 beats 25** on that executor. Does **not** beat pick_15 on the isolated walk. 6 vs 8 fails on 1000d. |
| STRICT vs FULL, Lab 8 / VTI 0 | FULL turns scanners on | one 365d (2025-11-14 → 2026-09-20) | no | FULL is not PIT | on | FULL +7.66% / 0.57 vs STRICT +1.60% / 0.16. **Do not promote or cut scanners.** |
| A2B1 skip-Monday fade | Flatten after prior close ≥8% | 365d + full DB | no | yes | 10 bps | **Fail** both windows (−8.8 pp and −22.2 pp). Gaps were yfinance. |
| Fat-loser open ≤ −1% drip | Cut the name after a gap down | 365d + 1000d | no | not the S&P PIT table | 10 bps | **Reject.** About −33 pp and −59 pp vs hold. |
| Block entries 9:30–10:00 | 5m MA50, fixed 30-name list | two disjoint 365d | two windows | fixed list | 10 bps | **Reject.** Last year helps; prior year does not. |
| Risk-adjusted rank | Distance to MA / 20d vol | 365d + full DB | no | yes | 10 bps | **Reject.** About −61 pp and −103 pp vs pick_15. |
| 12% vol target | Scale sleeve to 12% book vol | same | no | yes | 10 bps | **Reject** on return (Sharpe barely up on 365d). |
| take_1R default | Full exit at 1R | same | no | yes | 10 bps | **Fail.** Lower book than hold. |
| Friday flatten | Flatten Friday close | same | no | yes | 10 bps | **Fail.** 365d +37.8% / DD 24.9 vs pick_15 +95.5% / 13.8. |
| Thursday-only long flatten | Weekday exit grid | event grid | no | unknown | unknown | **Reject.** Lost the long-book cells. |
| Fade Friday winners into Monday | After a ≥5% up day | event study | no | unknown | unknown | **Reject.** Monday open continued up. |
| Short winners overnight | Equal-weight overnight short | event study | no | unknown | unknown | **Reject.** Negative, and worse on Monday. |
| Monday first-hour dump | First hour by weekday | one 365d | no | unknown | unknown | **Reject.** Not unique to Monday. |
| Open-price models | Ridge / gap vs prior close | ~252d | walk-forward OOS | symbol list | n/a (forecast) | **Reject.** Prior close wins on MAE. |
| MC 200/200 | Noise around paper-aggressive | 365d paths | n/a | n/a | in the strict legs | **Cannot promote.** Median return about +4.7%, mean about +21%, left tail about −44%. |
| Dynamic VTI 40–75% | Documented v1.5.4 band | not re-run on the employed VTI=0 books | no | no | on when the stack is | **Watch.** Both paper books were at VTI 0. |
| Medium disk vs portal | Report only | snapshot | n/a | n/a | n/a | **Hold.** No env write. Portal 15 / 2.0× vs stale disk 10 / 2.5×. |
| Aggressive employed stack | Lab 8 via portal, scanners on, A2B1 still wired | mixed | partial | partial | on in STRICT tests | **Hold.** Not a walk pass. A2B1 kill needs an explicit name. |

Do not re-run, from `QUEUE.md`: Friday flatten, Thursday-only flatten,
short-winners overnight, Monday first-hour dump, take_1R as default,
open-predict models, promote from MC 200/200, the 9:30–10:00 entry block,
fat-loser drip, risk-adjusted rank, 12% vol target, A2B1 on the isolated walk.

## `scripts/analysis` (phase4 unless noted)

| Experiment | Method | Window | WF | PIT? | Costs | Verdict |
|------------|--------|--------|----|------|-------|---------|
| v1.5.4 tune (`_run_v154_tune_ab.py`) | Stat arb, then ARIMA, VTI floor, SPY-like. HMM off. | one 365d | no | no | stack costs on | ARIMA **off**. Stat-arb quality **keep**. VTI floor and SPY-like flat on that window. |
| Markov 3-way (`_run_markov_hmm_compare.py`) | RHYME vs HMM soft vs HMM primary | default 100d | no | no | stack | **No saved numbers.** TOD leak until the 2026-10-25 backtest guard. |
| GARCH compare driver | ON vs OFF, HMM forced off in the compare | 365d cited | no | no | stack | Profile lock ON. Output file not in git. |
| Daily profit banking compare | Intraday equity bank | not saved | no | no | stack | Feature documented ON. No isolated A/B file. |
| Diversification A/B | Pick rotation + sector RS | 365d | no | no | stack | Control log exists; treatment delta not cleanly committed. |
| Wheel + politician tune | Sleeve tune | 365d | no | no | stack | Tuned **11.17% / 0.58 / −9.69%** vs baseline **21.09% / 1.35 / −5.91%**. Keep off. |
| `one_r_hit_backtest.py` | Daily MA70 exit grid | one window | no | config universe, not the PIT harness | that run states 0 bps | take_1R higher book, worse DD. Superseded by the PIT rerun. |
| `eval_strict_vs_full.py` | STRICT vs FULL overlays | one 365d (through 2026-08-18) | no | STRICT claimed; FULL not | on | Older row: STRICT slightly ahead of FULL. Later employed-stack row (above) flips the return gap. Do not mix the windows. |
| SPY exit / SPY-off | STRICT 90d | 90d | no | STRICT | on | Small 90d bump. Queue said confirm on 365d. Not a lock. |
| MC left tail, dynamic VTI locked | 365d Monte Carlo | one window | n/a | stack | n/a | Median return negative in `mc_left_tail_investigation_v154.md`. Does not promote. |
| `full_strategy_experiment.py` | Grid + folds + MC | 4 folds | yes | not shown as S&P PIT | in the harness | Top combo still has a fat MC left tail. Not a lock. |
| `parameter_tune.py` | MA / hold / risk grid | 365d, 48 combos | no | no | harness | Exploratory best Sharpe. Not locked. |
| `regime_effectiveness.py` | Old daily DB scenarios | long sample in the json | no | no | old sim | Committed scenarios match each other. Not v1.5.4 evidence. Also on `main`. |
| `universe_screener.py` | Builds today's liquid list | n/a | n/a | **no** — lookahead if reused on history | n/a | Data tool, not a backtest. |

`main` memos (integrated daily sim, survivorship not discussed):

| Memo | Window | Costs | Verdict |
|------|--------|-------|---------|
| `final_paper_bot_backtest.md` | 365d to 2026-06-16 | 5 bps equity | Stack beat VTI on that window. See caveat above. |
| `final_recommended_stack_comparison.md` | 365d and longer, from 2023-11 | in the sim | Recommended stack Sharpe about 1.25 on 365d vs an old baseline 0.47. Metal sleeve not simulated. |
| `sharpe_phase_results.md` | 500/1000/2000d | integrated | No variant held a 1.2–1.5 Sharpe target across lengths. |
| `game_plan_ab_results.md` | 2017–2026, 2022, 750d | daily sim | Keep the full game plan; edge over yield-gate-only is small. |
| `nyse_anti_overlap_results.md` | full + recent 750d | backtester | Correlation filter hurt the recent window. Keep baseline. |
| `deployment_efficiency_results.md` | 500/2000d | stack | Adaptive chunk helped the long window. |
| `refinements_grid_results.md` | 500/2000d | stack | Best grid about +0.04 Sharpe on 2000d. 500d weak. |
| `spy_exit_rule_test.md` | 365/1000d | live-like | SPY MA exit off as default. |
| `risk_layer_results.md` | 500d + max history | halt variants | Resume-at-8% plus liquidate on the recent window. Not a bear-pause rule. |
| `OPTIMIZED_SYSTEM_SUMMARY.md` | synthesis of the above | — | Two profiles (live conservative, paper aggressive). Pre-PIT. |

## `scripts/research`

| Experiment | Method | Window | WF | PIT? | Costs | Verdict |
|------------|--------|--------|----|------|-------|---------|
| Crypto vol v5 | 15m mean-reversion + breakout | script `--days` | unknown | n/a (crypto) | fee-aware taker | No committed summary on `main`. Trade CSV only. |
| Crypto sleeve v2/v3 | Sleeve tweaks | CSV sample | unknown | n/a | crypto fees | Improved sleeve lost to current in the CSV aggregates. |
| Sector rotation | Top 2 of 11 sector ETFs, 20d momentum | e.g. 730d | unknown | fixed ETF list | unknown | No summary memo. |
| Wisdom governor / monthly | Governor and Wayback rollups | 2017–2023 era in the monthly script | unknown | Wayback-era | unknown | No promote memo on `main`. |
| Universe A/B CSV | Fixed 29 vs screener | unknown | no | **fixed 29** | unknown | Screener ahead of the fixed list in the CSV. The 29-name list is the hindsight-shaped universe later walks retired. |
| BB squeeze (phase4) | Squeeze on one crypto name | script window | no | n/a | unknown | **Reject** vs the v4 mean-reversion path in `bb_squeeze_results.csv`. |
| 33/67 vs NYSE 100 (phase4) | MA70 rank, 15 names | 2025-08-11 → 2026-09-04 | no | **no** (panel, not PIT) | **off** | Do not promote. Return ranking is uncosted. |
| Metals 33/57/10 (phase4) | Always-on metal overlay, STRICT | 90/180/365d | no | STRICT claimed | unknown | **Hold.** 365d return up, drawdown worse. |
| Open-price predict (phase4) | OOS vs prior close | from 2025-11 | yes | symbol list | n/a | **No model beats prior close.** |
| Sneaky pivot (phase4) | Touch / next-open variants | 90d matrix plus OOS files | partial | small hardcoded list | unknown | Exploratory. Not a promote. |
| RHYME vol audit (phase4) | Expanding vol vs a fixed threshold | 900d | no | market-wide | n/a | High-vol label almost never fired. |
| Live profile A STRICT (phase4) | Live-shaped 85% VTI stack | 90/180/365d | no | STRICT claimed | unknown | **No promote.** Did not beat VTI on 365d. |
| Exhaustive campaign (phase4) | Phases plus MC 200 | MC on a 365d span | planned, halted | STRICT leg cited | in strict legs | **No promote.** STRICT PIT on 2025-05-28 → 2026-08-05 was **−1.42% / −0.09 / −9.73%** vs VTI buy-and-hold **+3.24%**. An older 365d STRICT row (+29%) is a different window. Do not mix them. |
| VTI tactical shadow (phase4) | Shadow trim | sparse | no | unknown | n/a | Criteria not passed. |

## Pull requests and branches

| PR | State | What it tested | Verdict |
|----|-------|----------------|---------|
| [19](https://github.com/dawimberly/trading-bot/pull/19) | draft | Trade attribution plus three offline rankers (pairwise, median-linear, nearest-neighbor expectancy) at 5 and 10 bps per side. 247-session, 29-name cache. Purged chronological splits, not a Live/Medium replay. | **No winner.** Whole-book profit blocked by unknown opening inventory. Evidence refresh: `origin/codex/research-review-evidence-20261024`. |
| [20](https://github.com/dawimberly/trading-bot/pull/20) | draft | Fixed ranker (`close/MA50−1`), next-open fill, vs random on a June 2026 screener. 5 bps per side. | **Promotion false.** Mean can beat random; the tape cannot prove an edge. List is not point-in-time. |
| [21](https://github.com/dawimberly/trading-bot/pull/21) | merged | Live ops: Lab hold-only 30d; Medium/Live 10% hard stop vs a prior tighter stop. | Operational experiment, not a backtest. |
| [7](https://github.com/dawimberly/trading-bot/pull/7) | open | v1.5.4 lock: ARIMA off, GARCH paper, HMM soft-only. | Config lock, not a new walk. |
| [14](https://github.com/dawimberly/trading-bot/pull/14) | draft | NYSE 12-to-7 momentum screen | **No-go.** Close-only panel, no PIT tape. |
| [15](https://github.com/dawimberly/trading-bot/pull/15) | draft | Shadow plan for that screen | **Inconclusive.** Needs an entitled quote tape. |
| [17](https://github.com/dawimberly/trading-bot/pull/17) | draft | Virtual-paper Lab Next scaffold | No edge claim. |

Branches ahead of `main` that carry research, not look-only UI:

| Branch | Adds |
|--------|------|
| `origin/cursor/fix-live-cycle-500-72e4` | Point-in-time walk verdicts, `best_strategies/`, Saturday pack. 51 commits ahead of `main` at survey time. |
| `origin/cursor/phase4-integration` | That line plus Phase 4 fill accounting and the PR 21 stop experiment. |
| `origin/codex/research-review-evidence-20261024` | Sanitized learner review (`latest_review.json`). |
| `origin/codex/automated-trade-review-20261023` | PR 19 attribution and the overnight learner. |
| `origin/cursor/local-expectancy-protocol-e2b1` | PR 20 pick-quality / 12-to-7 notes. |
| `origin/cursor/nyse-12-7-momentum-screen-77ca` | Ranking comparison no-go. |
| `origin/cursor/nyse-12-7-shadow-plan-77ca` | Shadow pre-registration. |
| `origin/cursor/lab-next-virtual-paper` | Virtual-paper scaffold. |
| `origin/cursor/monitor-exe-tls-and-paper-v2` | v1.5.4 lock and the ARIMA–GARCH hybrid option. |
| `origin/ollama-fallback-test` | Paper v1.5.4 freeze memos. |

`main` itself stops at the paper-v2 monitor snapshot. Quoting a `main` backtest memo as if it were the 2026-10-10 point-in-time ledger will overstate the edge.
