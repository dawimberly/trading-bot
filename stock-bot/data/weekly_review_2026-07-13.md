# Paper Book Weekly Research Note — 2026-07-13

> Classification: Internal research / decision support. **Not** an order. Parameter changes require owner approval. This artifact never mutates `.env` or live books.

## 0. Executive Decision
**Recommendation: NEEDS MORE DATA**
Effect size within noise / mixed (ΔReturn=-0.01pp, ΔSharpe=+0.00, Δ|DD|=+0.00pp). Hold parameters; gather another week of clean closed trades. Caveats: live 7d data grade C — lean on A/B, not mandate score; thin closed-trade sample in live window; mandate score unreliable this week.

Proposed line (manual only): `PAPER_NYSE_SLEEVE_CAP_PCT=0.15`

## 1. Mandate & Success Criteria
| Metric | Success | Failure / risk bound |
|--------|---------|----------------------|
| Ann. Sharpe (7d daily) | ≥ 1.0 | < 1.0 |
| Max DD magnitude (7d) | ≤ 15.0% | > 15.0% |
| 7d total return | ≥ 0.5% | < 0.5% |
| Closed trades (7d) | ≥ 5 | < 5 (underpowered) |

**Mandate score: UNRELIABLE (data quality C)**
- Toward failure: Data grade C — do not trust mandate score this week

## 2. Data Quality & Methodology
- Grade: **C** | Equity source: `paper_journal.csv+wisdom_filter(pct_drop_89.7%)`
- Clean daily obs: 3 (raw daily points considered: 0)
- Discontinuities removed: 0 (threshold jump≥25% or ratio≥5)
- Returns: end-of-day equity from cycle marks; rf≈0 for short-horizon Sharpe
- Sharpe scale: √252; lookback: 7d live / 90d experiment
- Scientific rule: **one** exogenous parameter change per review
- QC: Sparse/noisy sample — mandate scoring and sleeve trade stats unreliable.

## 3. Performance (cleaned 7d)
| Metric | Value |
|--------|-------|
| Start equity | 98038.09 |
| End equity | 98165.73 |
| Period return | 0.13% |
| Ann. volatility | n/a |
| Sharpe (ann.) | n/a |
| Sortino (ann.) | n/a |
| Calmar (period ret / |DD|) | n/a |
| Max DD (magnitude) | 0.00% |
| Daily hit rate | 100.0% |
| Profit factor (daily) | n/a |
| Skew / excess kurtosis | n/a / n/a |
| Closed trades | 0 (WR n/a, expectancy n/a, PF n/a) |
| Trades / day | 0.00 |
| Regime | RHYME_C: Steady_Bullish_Growth |
- Heartbeat: equity=97996.75 halted=False crypto_vol_only=True

## 4. Sleeve Attribution
Best: **crypto** (+29.71) | Worst: **nyse** (-168.91)

| Sleeve | Source | Closed | Win% | Realized | Unrealized | Positions | Contrib |
|--------|--------|--------|------|----------|------------|-----------|---------|
| nyse | unrealized | 0 | n/a | +0.00 | -168.91 | 14 | -168.91 |
| spy | unrealized | 0 | n/a | +0.00 | +0.00 | 0 | +0.00 |
| metal | unrealized | 0 | n/a | +0.00 | +0.00 | 0 | +0.00 |
| crypto | unrealized | 0 | n/a | +0.00 | +29.71 | 1 | +29.71 |

## 5. Single-Factor Hypothesis (Scientific Method)
**H1 (what):** Single factor under investigation: nyse sleeve is the weakest 7d contributor (contrib=-168.91, -17.2 bps of book).
**Rationale (why):** Regime=RHYME_C: Steady_Bullish_Growth. Data grade=C. Closed trades=0. Sleeve source=unrealized. Under a single-factor discipline we adjust only the exposure/risk lever mapped to this sleeve — not an omnibus retune.
**Mechanism (treatment):** Tighten `PAPER_NYSE_SLEEVE_CAP_PCT` from 0.20 → 0.15 to change that sleeve's capital allocation while holding all other policy constants fixed.
**Success definition:** On a 90d paper-aggressive backtest, proposed config should improve Sharpe and not worsen max-DD magnitude materially (ΔSharpe>+0.05, ΔReturn≥−0.5pp, Δ|DD|≤+0.5pp).
**Falsification:** Reject if proposed Sharpe worsens by >0.05, or return drops >1pp with worse |DD|, or data grade remains C with no closed-trade corroboration.
**Confounders / threats to validity:**
- 7d sample is short vs parameter half-life — A/B is 90d to compensate
- Mark-to-market sleeve PnL ≠ closed PnL when exits are sparse
- Regime conditioning (RHYME) may dominate sleeve caps this week
- Backtest path risk: one seed/path; not a monte-carlo IC memo

## 6. Controlled Experiment (90d paper-aggressive A/B)
| Metric | Baseline (current) | Treatment (proposed) | Δ |
|--------|--------------------|----------------------|---|
| Return | 10.60% | 10.59% | -0.01 pp |
| Sharpe | 2.93 | 2.93 | 0.00 |
| Sortino | 5.14 | 5.14 | 0.00 |
| Calmar | 4.62 | 4.62 | 0.00 |
| Max DD | -2.29% | -2.29% | |DD| Δ 0.00 pp |
| Daily win rate | 42.3% | 42.3% | — |

## 7. Investment Committee Recommendation
**NEEDS MORE DATA**
Effect size within noise / mixed (ΔReturn=-0.01pp, ΔSharpe=+0.00, Δ|DD|=+0.00pp). Hold parameters; gather another week of clean closed trades. Caveats: live 7d data grade C — lean on A/B, not mandate score; thin closed-trade sample in live window; mandate score unreliable this week.

## 8. Implementation (owner-only)
If APPROVE, add/change **paper** `.env` only:
```
PAPER_NYSE_SLEEVE_CAP_PCT=0.15
```
Do **not** apply to live. Restart paper bot after edit. Re-evaluate next Saturday.

## Appendix
### Wisdom corroboration
- 2026-07-08: ret=None sharpe=None src=wisdom_journal.csv
- 2026-07-09: ret=0.3617383687374076 sharpe=None src=wisdom_journal.csv
- 2026-07-10: ret=0.16992248049405578 sharpe=None src=wisdom_journal.csv
- 2026-07-13: ret=-0.5289396794591061 sharpe=None src=wisdom_journal.csv
### Notes
- Lookback had few rows; extended history used then jump-filtered.
- No closed trades in window — sleeve ranking uses mark-to-market (unrealized) only.
- Wisdom corroboration series: 4 point(s).

<!-- advisory only; never auto-apply -->
