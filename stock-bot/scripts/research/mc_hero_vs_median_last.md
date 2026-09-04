# MC hero vs median vs wipeout — is the right tail cloneable?

Research only. No `.env`, no restart, no orders, no third process, no portal book, no promote.

**SoT:** `scripts/research/exhaustive_campaign/runs/artifacts/campaign_20260811T221624Z_full/mc/`
**Stack in THAT campaign:** `paper-aggressive` + Dynamic VTI 40–75% + deep-indicators. **Not** employed paper v2 (fixed 33/67, extra sleeves off).
**Window:** meta `2025-10-24 → 2026-08-11` (292 sim bars). `results.json` stamps the same 292 bars as `2025-10-27 → 2026-08-14`.
**MC:** 200 runs, seed 42, noise 0.01, regime_noise 0.1. Per-run RNG is `SeedSequence([42, run_id])`.
**Campaign argv:** `monte_carlo_backtest.py --paper-aggressive --days 365 --mc-runs 200 --export-dir …` (no `--no-realistic-costs`, so **default slippage already on**: ~5 bps equity / ~10 bps NYSE each way). No `--no-thinking`, no `--track-sleeve-path`.

## 1. Same file (200/200 unique)

Loaded `runs.jsonl` (200 lines, 200 unique run ids). Matches `summary.md` / `summary.json`.

| Metric | mean | median | p5 | p95 | min | max |
|--------|-----:|-------:|---:|----:|----:|----:|
| Return % | 21.16 | **4.71** | **−43.83** | **145.94** | −69.01 | **230.38** |
| Sharpe | 0.34 | 0.33 | −4.38 | 5.05 | −9.65 | 6.52 |
| MaxDD % | −18.32 | −13.47 | −44.84 | −4.45 | −69.01 | −3.31 |

P(return>0) **52.5%** · P(Sharpe>1) **37.5%** · 25 paths >+100% · 50 >+50% · 19 <−40%.

JSON keys present: `run, total_return_pct, sharpe, max_drawdown_pct, vol_mult, regime_drift, vti_min, vti_max, vti_avg, vti_at_max_dd, regime_at_max_dd, regime_counts, max_dd_bar, saved_at`.

**Not in the JSON (any run):** `n_trades`, win rate, sleeves, turnover, stop count, `trough_sleeve`, equity curve, trade log. Extra keys: none.

## 2. TOP 10 vs BOTTOM 10 vs MEDIAN band

Bands by **total_return_pct**. Median band = 10 runs nearest **+4.71%**.

Means (min–max in parentheses):

| | TOP 10 | MEDIAN 10 | BOTTOM 10 |
|---|---:|---:|---:|
| Runs | 162, 80, 39, 146, 79, 182, 161, 168, 20, 87 | 95, 81, 103, 67, 129, 119, 82, 194, 123, 6 | 19, 41, 43, 62, 9, 30, 171, 36, 128, **115** |
| Return % | **+185.5** (148–230) | **+5.3** (1.9–7.8) | **−55.1** (−69–−47) |
| Sharpe | **5.90** (5.10–6.52) | 0.37 (0.18–0.55) | **−5.78** (−9.65–−4.20) |
| Max DD % | **−4.81** (−6.3–−3.3) | −13.0 (−18.0–−9.6) | **−55.3** (−69.0–−47.8) |
| vol_mult | 1.009 (0.941–1.088) | 1.018 (0.915–1.097) | 0.981 (0.912–1.047) |
| **regime_drift** | **+0.0035** (+0.0027–+0.0046) | **−0.0005** (−0.0010–−0.0002) | **−0.0045** (−0.0085–−0.0031) |
| VTI avg | 0.643 (0.637–0.658) | 0.630 (0.612–0.647) | 0.674 (0.638–0.709) |
| VTI min | 0.404 (0.40–0.44) | 0.40 (all 0.40) | 0.503 (0.44–0.53) |
| VTI max | **0.75** (all) | **0.75** (all) | **0.75** (all) |
| VTI @ max DD | 0.725 (9/10 at 0.75) | 0.616 | 0.749 (8/10 at 0.75) |
| max_dd_bar | 110 (24–275) | 132 (26–274) | **287** (268–291) |
| Regime @ trough | E:7 D:3 | E:7 D:2 C:1 | **A:8 D:2** |

**vol_mult ranges overlap completely.** Heroes are not a low-vol or high-vol subset.

**regime_drift ranges do not overlap at all** (top min +0.0027 vs bottom max −0.0031). All 10 heroes have **positive** drift; all 10 wipeouts have **negative** drift. Across all 200: 80 +drift paths mean **+78%** (min −12%, max +230%); 120 −drift paths mean **−17%** (min −69%, max +31%). **No path with +50% return has negative drift. No path >+100% has negative drift.** Correlation(return, regime_drift) = **0.905** (r² = **0.82**). vol_mult vs return r = 0.09.

That scalar is **not a sleeve**. In `perturb_market_data` it is drawn once per run and added as a **market-wide daily shock** to every name (`regime_shock ~ N(regime_drift, …)`). Compounding +0.0032/day over ~291 bars ≈ **+153%** before stock selection — same order as run 162’s **+230%**. Compounding −0.0085/day ≈ **−92%** (clip + path) vs run 115 **−69%**.

VTI avg is **almost the same** (top 0.643 vs rest 0.650). Heroes sit a hair **lower** (more NYSE) because Dynamic VTI follows the lucky lifted path; wipeouts sit a bit **higher** (0.674) and still print **VTI = 0.75 at the trough**. You cannot lock “VTI 64%” to harvest +230%: the allocator is endogenous to the injected drift. `vti_max = 0.75` on **all 200** runs.

`regime_counts` is **not** per-path structure. Only **three** blobs, and they are **contiguous run-id blocks** (runs 1–13, 14–44, 45–200) — resume / window stamp, not a different calendar on heroes. Perturbations did not retune the RHYME mix.

## 3. Run 162 vs median vs wipeout 115 — equity / trades

**Per-run equity curves and trade logs do not exist** in this campaign folder (9 files: jsonl + summaries + meta; no `--track-sleeve-path`). Cannot compute % of P&L from best 5 trades, cannot compute symbol Jaccard.

What the summary row still shows:

| | Run **162** (hero) | Run **67** (nearest median, +4.12%) | Run **115** (wipeout) |
|---|---:|---:|---:|
| Return / Sharpe / Max DD | +230.38% / 6.52 / **−4.42%** | +4.12% / 0.30 / −10.00% | **−69.01%** / −9.65 / **−69.01%** |
| vol_mult | 1.035 | 1.009 | 1.047 |
| regime_drift | **+0.0032** | −0.0006 | **−0.0085** |
| VTI avg / min / @ DD | 0.640 / 0.40 / **0.75** | 0.631 / 0.40 / 0.75 | 0.638 / 0.53 / **0.75** |
| max_dd_bar (of 292) | **24** (early; ~Nov 2025) | 151 (mid) | **291** (last bar; window end ~2026-08-11) |
| Regime at trough | RHYME_E | RHYME_E | **RHYME_A** |

Trough **dates** are approximate (no `equity_index`). Heroes often trough in the first ~50 bars with a **tiny** DD; wipeouts trough in the **last ~10 bars** in **RHYME_A** with VTI still at the **75% ceiling**. Same finding as `scripts/analysis/mc_left_tail_investigation_v154.md` on the older 50-run 365d file.

## 4. Costs replay of run 162

**Not run. Runner cannot do a faithful cheap check.**

- Campaign already had **realistic costs on** (default; argv did not pass `--no-realistic-costs`). Replaying “costs ON” would not be a new condition.
- `monte_carlo_backtest.py` has **no `--only-run`**. Isolating seed `SeedSequence([42, 162])` would require a new one-off driver (user: do not invent a new MC).
- A full paper-aggressive path was **~1850s/run** in this campaign. Today’s tree has **employed** `PAPER_DYNAMIC_VTI=false` (fixed 33/67) plus later ATR/1R changes. Replaying with current code would **not** be campaign run 162.

No extra 5–10 bp round-trip print.

## Verdict

**PATH NOISE** — the right tail is the MC `regime_drift` lottery (market-wide injected daily drift), not a cloneable sleeve / name-count / VTI layout.

Neither. Do not spin a 3rd paper bot from run 162. Do not revive Dynamic VTI, stat-arb, shorts, or ORB. Employed 33/67 unchanged.
