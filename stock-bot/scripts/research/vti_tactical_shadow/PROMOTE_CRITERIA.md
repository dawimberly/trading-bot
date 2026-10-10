# VTI tactical $5k — pre-registered promote criteria

Written **before** the shadow week accumulates. Do not edit thresholds to fit results.
Freeze stays ON until the explicit break phrase after these pass.

## Data quality (hard gates — fail closed)

| # | Criterion | Pass |
|---|-----------|------|
| D1 | Promote stats use **only** `promote_eligible=true` rows | Required |
| D2 | `data_source` must be `sqlite` for eligible rows | Required |
| D3 | `data_fresh=true` (bar lag ≤1 session vs expected last RTH close) | Required |
| D4 | Stale / yfinance rows go to `shadow_events_stale.jsonl` only | Required |
| D5 | Staleness rate in **main promote stream** = **0%** | Required |
| D6 | Pre-provenance / unlabeled rows quarantined to archive | Required |

If D1–D6 fail, the week is **invalid for promote** regardless of P&L.

## Known-event check (week-1 meaning)

| # | Criterion | Pass |
|---|-----------|------|
| K1 | At least one **eligible** snapshot on/after the Aug 3–4 2026 VTI rip | Required for week-1 |
| K2 | That snapshot (or same rip window) shows `action=would_trim` | Required |
| K3 | Gate letter on that trim is **D or C** (prod log), and shadow-equity letter is also logged | Required |
| K4 | `sized_notional` ∈ **[$2,750, $4,250]** i.e. $5k × [0.55, 0.85] | Required |
| K5 | `ret_2d_pct` on rip snapshot ≥ **+1.5** (matches RIP_PCT) | Required |

K1–K5 are the “is logging real?” bar. Fail any → do not call week one meaningful.

## Aggregate shadow week (after ≥5 eligible trading-day snapshots)

| # | Criterion | Pass |
|---|-----------|------|
| A1 | ≥ **5** distinct `bar_date` values with eligible rows | Required |
| A2 | Shadow would-have path **MaxDD** not worse than $5k VTI B&H by **>1.0 pp** | Required |
| A3 | Shadow would-have **total return** ≥ VTI B&H on same $5k − **0.5 pp** (i.e. not clearly worse) | Required |
| A4 | No bleed intents into NYSE/ORB (this sleeve logs VTI/SPY only) | Required |
| A5 | ARIMA never set `arima_enabled_for_decision=true` | Required |

A2/A3 are **hold-the-line** vs buy&hold, not “must crush VTI in one week.”

## Sequencing (still required before wire)

| # | Criterion | Pass |
|---|-----------|------|
| S1 | Equity-only RHYME split **promoted** into `market_context` *or* dual-gate (prod+shadow) still required at fill time | Required |
| S2 | ARIMA stays decision-OFF until separate walk-forward sleeve A/B | Required |
| S3 | Explicit owner phrase: `break freeze — wire $5k VTI tactical (GARCH+HMM, ARIMA off until sleeve A/B passes, SPY/VTI)` | Required |

## Explicit non-criteria (do not promote on these)

- Gut call “up 0.5% then drop”
- A pretty week that includes **any** silent stale/fallback rows in promote stats
- Full-sample ARIMA re-tune without holdout
- “Everything on $5k” multi-sleeve collision
