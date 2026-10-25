# ARIMA and HMM — code check (2026-10-25)

Research note only. No deploy, no live knob change. The pass rules in
`PROTOCOL.md` live on `origin/cursor/phase4-integration` (not on `main`).
This note does not re-run the 2016–2026 walk.

## What the code does

### Missing `hmmlearn` was silent

`modules/markov_regime.py` imported `hmmlearn` inside the fit. `ImportError`
was logged at debug and stored as the same `fit_failed` reason as a real EM
crash. Sizing, VTI adjustment, and short boost then return the neutral
fallback (size ×1.0). `run_all.py` printed the HMM banner only when the fit
succeeded, so a paper or opted-in live process on a interpreter without
`hmmlearn` (the reported venv311 / 3.11 case) kept trading as if the model
were merely idle.

Live is already gated. `effective_markov_hmm_enabled()` is false unless the
process is in a paper/research context or `MARKOV_HMM_LIVE_ENABLED` is true.
`MARKOV_HMM_ENABLED=true` alone does not fit an HMM on a live book. The
paper default stays on, because flipping it would change paper books whose
environment does have `hmmlearn` (the reported 3.14 env). This change does
not alter that gate and does not change a successful fit's size.

The inactive state is now explicit:

- missing install → warning, `reason=hmmlearn_missing`, heartbeat
  `dependency_missing`, banner `INACTIVE hmmlearn missing, sizing x1.00`
- master flag on but the live/paper gate off → heartbeat
  `reason=gated_off`, `sizing_mult=1.0`, `inactive_explicit=true`
- failed fit still sizes at 1.0

### ARIMA sizing is up-only by default

`ARIMA_BOOST_MULT` defaults to 1.08 and `ARIMA_NEG_MULT` to 1.0
(`config.py`, and the same map in `PAPER_RESEARCH_PROFILE.md` on the
phase4 branch). A positive one-step forecast levers size up. A non-positive
forecast stays at 1.0. Hybrid vol scaling can pull a positive boost back to
1.0. It does not haircut a negative forecast. A window can "pass" because
the overlay is long-only leverage, not because the sign has skill.

`arima_directional_is_up_only()` records that on the forecast state,
banner, and heartbeat. `symmetric_research_multiplier()` pairs 1.08 with
0.92 for scoring. Order sizing is unchanged. ARIMA remains default off
(`ARIMA_ENABLED=false`); the 365d paper tune already said leave it off.

### Time-of-day lookahead in backtests

Two inputs were not a function of the bar being replayed:

- `_tod_feature_series` stamped the present-day TOD cache's best entry
  bucket onto every historical row.
- `_tod_soft_edge` called `datetime.now()` in America/New_York, so the
  size blend depended on the clock when the backtest was launched.

`run_backtest` now sets `set_research_tod_neutral(True)` for the replay and
clears it before returning. Neutralized bars use bucket code 0.5 and
entry multiplier 1.0, and they do not read the cache or the wall clock.
Live and paper cycles still use the session clock. Predictions carry
`tod_research=neutralized|live_clock`. HMM-on backtests from before this
change are not clean evidence.

## Repo backtests that mentioned ARIMA or HMM

These drivers are on `origin/cursor/phase4-integration`, not on `main`.

| Driver | What it actually tested | Survivorship / lookahead | Verdict in git |
|--------|-------------------------|--------------------------|----------------|
| `scripts/analysis/_run_v154_tune_ab.py` and `_run_single_ab_leg.py` | One 365d paper-aggressive stack. HMM forced off. ARIMA on vs off. | Full stack, not the S&P point-in-time walk. Single window. Not a 2016–2026 quad. | ARIMA ON +16.98% / 1.11 / −5.99% vs OFF +17.23% / 1.12 / −6.05% (−0.25 pp, −0.01 Sharpe). Recommendation: stay OFF. |
| `scripts/analysis/_run_markov_hmm_compare.py` | `--compare-markov-hmm`, default 100d paper-aggressive (365d only if `HMM_COMPARE_DAYS` is set). | Same full stack. Before this fix, HMM features could read the present-day TOD cache and the wall clock. | No numeric compare output is committed. Profile still says soft HMM on, primary off, until a 3-way beats RHYME. That bar was not met in git. |

Those files do not endorse ARIMA. They also do not contain a survivorship-free HMM win. The profile's "HMM soft-only" lock is a product default, not a passed walk. GARCH's locked paper ON cites a 365d A/B (~+1.06 pp, +0.04 Sharpe) whose compare output is not saved next to the driver, and an 80d window that was slightly negative. That is one window, not a protocol pass.

## Against the external 2016–2026 walk

That walk is not in this repo. Nothing here duplicates its four disjoint
periods or its top-1 MA50-extension baseline. Directionally:

- ARIMA fail: **duplicate.** The repo already rejected locking ARIMA on, and the code shows why a 30-day "pass" can be mechanical.
- HMM fail plus TOD lookahead: **mechanism confirmed, numeric fail not in git.** Treat older HMM-on stack runs as contaminated until they are re-run with `tod_research=neutralized`.
- Logistic overlay fail: **duplicate at the verdict, different harness.** See the inventory. The in-repo rejects use a 29-name retrospective cache, not the point-in-time quad.
- Break-even top-1 baseline: **not in git.** It does not contradict the MA70 pick_15 point-in-time walk (different rank, hold, and windows). It does contradict quoting `current` screener history or the 29-name cache as the edge.
