# Monte Carlo Tail Analysis — Paper Aggressive + Dynamic VTI (40–75%)

**Source:** `scripts/analysis/monte_carlo_v154_dyn_vti_locked_90.json`  
**Saved:** 2026-07-24T00:53:45Z  
**Profile:** `paper-aggressive, deep-indicators`  
**Allocator:** Dynamic VTI **LOCKED 40–75%** (hard floor ≥40%)  
**Thinking:** off (`no-thinking`)  
**Window:** labeled 90d → sim **2026-05-13 → 2026-07-23** (**72** bars)  
**MC setup:** 50 runs · seed 42 · price noise 0.01 · regime noise 0.1  

Per-run fields available: `total_return_pct`, `sharpe`, `max_drawdown_pct`, `vol_mult`, `regime_drift`.  
**Not available in this export:** sleeve weights, Dynamic VTI path, or RHYME label series.

---

## 1. Full distribution summary

### Return (%)

| Stat | Value |
|------|------:|
| Median | **+9.01** |
| Mean | +6.32 |
| 10th | −14.81 |
| 25th | −3.22 |
| 75th | +16.21 |
| 90th | +25.00 |
| Min / Max | −26.49 / +38.68 |

### Sharpe

| Stat | Value |
|------|------:|
| Median | **1.94** |
| Mean | 1.02 |
| 10th | −3.41 |
| 25th | −0.61 |
| 75th | 3.41 |
| 90th | 4.79 |
| Min / Max | −8.33 / +7.37 |

### Max drawdown (%)

More negative = worse. “10th” is the bad tail; “90th” is the mild side.

| Stat | Value |
|------|------:|
| Median | **−4.65** |
| Mean | −7.41 |
| 10th (worst) | −16.16 |
| 25th | −8.80 |
| 90th | −2.79 |
| Worst single run | −27.38 |
| Mildest | −2.46 |

**Read:** Center of mass is strong (median Sharpe ~2, median DD ~−5%). Mean return/Sharpe sit below medians because a thin left tail pulls them down — classic right-skewed “good middle, ugly outliers” shape.

---

## 2. Worst 10% of runs (5 of 50)

The worst-10% sets by **Sharpe** and by **max drawdown are identical** (runs 19, 39, 42, 44, 45). That means the left tail is one coherent failure cluster, not two separate failure styles.

| Run | Return % | Sharpe | Max DD % | vol_mult | regime_drift |
|----:|---------:|-------:|---------:|---------:|-------------:|
| 39 | −26.49 | −8.33 | −27.38 | 1.057 | −0.0058 |
| 19 | −24.28 | −7.50 | −24.47 | 0.993 | −0.0038 |
| 44 | −21.67 | −7.14 | −22.63 | 0.977 | −0.0039 |
| 45 | −20.23 | −6.39 | −20.77 | 0.962 | −0.0040 |
| 42 | −15.81 | −3.96 | −17.83 | 1.013 | −0.0030 |

**Characteristics**
- All five finish deeply negative on **return, Sharpe, and DD together** (corr return↔Sharpe ≈ 0.98, Sharpe↔DD ≈ 0.93).
- `vol_mult` in the worst bucket averages **~1.00**, same as the rest (~1.003) — no evidence these fails are mostly “extra vol scaling” artifacts.
- `regime_drift` is slightly more negative in the worst bucket (**−0.0041** vs **+0.0002** elsewhere). Magnitude is small; at most a weak hint of adverse regime-path noise, not a smoking gun.
- **VTI behavior:** not logged per run in this export, so we cannot say whether fails coincided with VTI pinned at 40%, 75%, or mid-band.

---

## 3. Positive rate and Sharpe > 1.0 (confirmed)

| Metric | Value |
|--------|------:|
| P(return > 0) | **66.0%** (33 / 50) |
| P(Sharpe > 1.0) | **58.0%** (29 / 50) |
| P(return < 0) | 34.0% |
| P(Sharpe < 0) | 34.0% |

Matches the MC summary export.

---

## 4. Left-tail failure modes / clusters

**Primary failure mode:** a **single deep left-tail cluster** (~8–10% of runs) with return ≤ about −16%, Sharpe ≤ about −4, and max DD ≤ about −18%. Outside that cluster, outcomes are mostly modest losses or solid gains.

**Return histogram (coarse buckets):**

| Return bucket | Count |
|---------------|------:|
| (−40, −25] | 1 |
| (−25, −15] | 4 |
| (−15, −5] | 7 |
| (−5, 0] | 5 |
| (0, 5] | 5 |
| (5, 10] | 5 |
| (10, 20] | 15 |
| (20, 40] | 8 |

Notes:
- **14%** of runs have max DD ≤ −15%; **8%** ≤ −20%.
- The mass of the distribution sits in **+5% to +40%** (23/50 runs) — the strategy’s “normal” outcome under this noise model is profitable.
- Mean Sharpe (1.02) vs median (1.94) shows **outlier sensitivity**: the five-run cluster accounts for most of the mean degradation.
- No separate “high vol but flat return” or “tiny DD but awful Sharpe” mode shows up — fails are **path-wide risk-off wipeouts** in this short 72-bar window.

**Caveats for interpretation**
- Window is short (~3 months of sim bars). Left-tail severity can be amplified by a single adverse stretch.
- Noise is mild on prices (0.01) but regime noise (0.1) can still reshuffle sleeve timing; without sleeve/VTI traces we cannot attribute fails to Dynamic VTI vs active sleeves.

---

## 5. Recommendation

**Keep as the paper baseline for now — with eyes open on the left tail.**

**Why keep**
- Strong central tendency under the *current* paper-aggressive + Dynamic VTI 40–75% stack: median Sharpe ~1.9, median DD ~−4.6%, two-thirds of paths positive.
- Failures are concentrated, not diffuse; that is preferable to a strategy that is mediocre everywhere.
- This is confirmation of the **post–Dynamic VTI lock** stack, not a live 80/20 or pre-lock baseline.

**Red flags to address before the next experiment (not blockers to keep paper running)**
1. **Catastrophic cluster:** ~10% of paths with Sharpe &lt; −4 and DD around −18% to −27% over only ~72 bars — unacceptable if that rate persists on a longer window.
2. **Mean ≪ median:** report medians for go/no-go, but risk budgets should be sized off p10 / worst-decile DD, not the median.
3. **Missing diagnostics:** next MC export should include per-run (or sampled) **VTI weight path**, sleeve caps, and regime label so left-tail attribution is possible.

**Suggested next experiment (optional)**
- Re-run the same MC on **≥180–252 bars** with the same seed/noise, plus a lightweight per-run VTI/regime snapshot.
- If the worst-decile DD stays near −20%+ on a longer window, tighten paper risk (active sleeve caps / stress VTI tier) before promoting any live-like change.

**Verdict:** distribution is **robust enough to remain the paper baseline**; the main red flag is a **thin but severe wipeout cluster**, not a broken center. Fix attribution and re-check on a longer window before treating this as “live-ready” evidence.
