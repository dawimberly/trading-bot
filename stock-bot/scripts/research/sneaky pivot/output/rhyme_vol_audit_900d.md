# RHYME volatility classifier audit

Generated: 2026-08-02 21:48 UTC

**Research only — freeze unchanged. Market-wide finding, not Sneaky-Pivot-only.**

## How High/Low is defined (current code)

```
cross_asset_vol_score(data) = mean over assets of stdev(pct_change of FULL window)
get_volatility -> 'High' if score > REGIME_VOL_THRESHOLD_DAILY else 'Low'
```

- `REGIME_VOL_THRESHOLD_DAILY` = **0.02** (env overrideable; default 0.02)
- `REGIME_VOL_THRESHOLD_5M` = 0.008 (live 5m path)
- Threshold type: **fixed absolute cutoff** — not a rolling percentile, not adapting to recent vol regime
- Score type: **expanding-window full-history stdev** — stress spikes are diluted as `n_bars` grows; hysteresis does NOT change the High/Low cut (it only sticky-holds the final A–E letter after classify)

## Sentiment side (for A/B need High vol + extreme sentiment)

- `REGIME_SENTIMENT_THRESHOLD` = 0.12
- `REGIME_RAW_SENTIMENT_MAX` (normalize band) = 0.25
- Hysteresis enabled = True, dwell bars = 8

## Expanding-window vol score distribution (classifier input)

| Stat | Value |
|------|------:|
| n days | 880 |
| min | 0.00000 |
| p10 | 0.00000 |
| p25 | 0.00000 |
| p50 | 0.00000 |
| p75 | 0.00000 |
| p90 | 0.00000 |
| p95 | 0.00000 |
| p99 | 0.01640 |
| max | 0.02086 |
| threshold | 0.02000 |
| days above thresh | 3 (0.34%) |
| max / threshold | 1.043x |

## Counterfactual: same fixed thresh on 20d rolling score

| Stat | Value |
|------|------:|
| roll20 max | 0.02086 |
| roll20 p95 | 0.00000 |
| days roll20 > thresh | 3 (0.34%) |

If rolling-20 clears the thresh often but expanding never does, the issue is **dilution / expanding-window design**, not 'markets were calm.'

## RHYME letter counts (actual classifier walk)

| Letter | Days |
|--------|-----:|
| A | 0 |
| B | 0 |
| C | 354 |
| D | 332 |
| E | 194 |

## Highest expanding vol-score days (closest approaches to High)

       day  n_bars_in_window  vol_score_expanding  vol_score_roll20 rhyme
2026-07-31               898             0.020859          0.020859     D
2026-07-30               897             0.020823          0.020823     D
2026-08-01               899             0.020151          0.020151     D
2026-08-02               900             0.019548          0.019548     D
2026-07-29               896             0.018907          0.018907     D
2026-07-28               895             0.018088          0.018088     D
2026-07-24               891             0.017616          0.017616     D
2026-07-27               894             0.017141          0.017141     D
2026-07-25               892             0.016772          0.016772     D
2026-07-23               890             0.016300          0.016300     D
2026-07-26               893             0.015942          0.015942     D
2026-07-22               889             0.014084          0.014084     D
2026-07-21               888             0.012458          0.012458     D
2026-07-20               887             0.007266          0.007266     D
2026-07-19               886             0.000411          0.000411     D

## Highest 20d-rolling vol-score days (stress the expanding score missed)

       day  vol_score_expanding  vol_score_roll20 vol_label_if_roll20 rhyme
2026-07-31             0.020859          0.020859                High     D
2026-07-30             0.020823          0.020823                High     D
2026-08-01             0.020151          0.020151                High     D
2026-08-02             0.019548          0.019548                 Low     D
2026-07-29             0.018907          0.018907                 Low     D
2026-07-28             0.018088          0.018088                 Low     D
2026-07-24             0.017616          0.017616                 Low     D
2026-07-27             0.017141          0.017141                 Low     D
2026-07-25             0.016772          0.016772                 Low     D
2026-07-23             0.016300          0.016300                 Low     D
2026-07-26             0.015942          0.015942                 Low     D
2026-07-22             0.014084          0.014084                 Low     D
2026-07-21             0.012458          0.012458                 Low     D
2026-07-20             0.007266          0.007266                 Low     D
2026-07-19             0.000411          0.000411                 Low     D

## Known stress windows in this walk (if covered)

- **Aug 2024 yen-carry (approx)**: n=7, expanding max=0.00000, roll20 max=0.00000, expanding High days=0, roll20 High days=0, letters={'D': 5, 'E': 2}
- **Apr 2025 tariff selloff (approx)**: n=15, expanding max=0.00000, roll20 max=0.00000, expanding High days=0, roll20 High days=0, letters={'C': 4, 'E': 11}

## ROOT CAUSE (updated after matrix diagnostics)

`cross_asset_vol_score` does:

```python
vol = data.pct_change().dropna().std().mean()
```

`dropna()` defaults to `how="any"`. With a **wide** close matrix (~341 columns, staggered history), almost every calendar row has at least one NaN → **every return row is dropped** → `std().mean()` is NaN → coerced to **0.0** by the NaN guard.

Measured on the same 900d matrix at bar ~850 (340 assets populated that day):

| Method | Rows kept | Score |
|--------|----------:|------:|
| `pct_change().dropna()` (current = how any) | **0** | **0.0** |
| `pct_change().dropna(how="all")` then per-col std | 850 | ~0.022 |
| Apr 2025 window, per-col std skipna | 14 | **~0.040** (> 0.02 High) |

So April 2025 tariff stress **would** clear the fixed 0.02 threshold if the score weren't zeroed. Aug 2024 / Apr 2025 "calm" in the audit walk is an artifact, not the market.

Secondary issues (still real, even after a dropna fix):

1. **Fixed absolute thresh 0.02** — not percentile / adaptive.
2. **Expanding full-history stdev** — dilutes short spikes once dropna is fixed.
3. **When High did fire** (3 days late Jul 2026): `sentiment_norm` ≈ 0.09–0.10 **< 0.12** → stayed **RHYME_D**. A/B need High **and** extreme sentiment; vol alone is not enough.

Hysteresis / `set_regime_bar_index` did **not** cause A/B absence — vol never classified High for almost the entire walk because the score was stuck at 0.

## Verdict

**Classifier is not capable of producing meaningful A/B on current daily matrices** until `cross_asset_vol_score` stops using `dropna(how="any")` on wide panels. This is **market-wide** (`run_all`, paper, `compute_regime_breakdown`, every RHYME_A/B gate) — do not patch only the Sneaky Pivot harness.

Recommended design review (separate freeze-safe PR, not done here):

1. Fix score: per-column `std(skipna=True).mean()` or `dropna(how="all")` before std.
2. Decide: expanding vs rolling window for "current vol regime."
3. Revisit whether 0.02 stays as absolute cut or becomes a percentile.
4. Re-run this audit + a known stress window after (1).

Freeze unchanged. No config/code behavior changes in this audit beyond the diagnostic script.

Wrote companion CSVs alongside this report.
