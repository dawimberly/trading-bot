# Left-Tail Investigation — Paper Aggressive + Dynamic VTI (40–75%)

**Date:** 2026-07-24  
**Trigger:** 90d MC wipeout cluster (runs 19/39/42/44/45) in `monte_carlo_v154_dyn_vti_locked_90.json`  
**Primary evidence:** longer-window MC with VTI/regime diagnostics  
**File:** `scripts/analysis/monte_carlo_v154_dyn_vti_locked_365.json`

---

## Setup

| Item | Value |
|------|-------|
| Profile | `paper-aggressive, deep-indicators` |
| Allocator | Dynamic VTI **LOCKED 40–75%** |
| Thinking | **OFF** (`--no-thinking`) |
| Window | **365d label → 2025-10-05 → 2026-07-23 (292 sim bars)** |
| Runs | 50 · seed 42 · noise 0.01 · regime_noise 0.1 |
| New diagnostics | `vti_min/max/avg`, `vti_at_max_dd`, `regime_at_max_dd`, `regime_counts`, `max_dd_bar` |

Instrumentation added:
- `backtester.py` exports `vti_core_series`
- `monte_carlo_backtest.py` extracts VTI path stats + regime at max-DD trough

---

## 1. Longer-window distribution (365d / 292 bars)

Compared with the flattering 90d (~72 bar) MC:

| Metric | 90d (prior) | **365d (this run)** |
|--------|------------:|--------------------:|
| Median return | +9.0% | **−8.2%** |
| Mean return | +6.3% | +29.4% *(right-tail pulled)* |
| p10 / p90 return | −14.8% / +25.0% | **−37.2% / +140.6%** |
| Median Sharpe | +1.94 | **−0.48** |
| p10 / p90 Sharpe | −3.41 / +4.79 | **−2.97 / +5.16** |
| Median max DD | −4.6% | **−16.0%** |
| p10 (worst) max DD | −16.2% | **−38.2%** |
| P(return > 0) | 66% | **46%** |
| P(Sharpe > 1) | 58% | **36%** |

**Takeaway:** the 90d left-tail cluster was **not** a short-window fluke in the sense of “only short samples look bad.” On a ~year window the **center** deteriorates (median Sharpe negative) while a **fat right tail** (up to +251%) inflates the mean. Risk is two-sided path fragility, not a thin 10% bug only.

---

## 2. Worst 5 runs (by Sharpe; same set by max DD)

| Run | Return | Sharpe | Max DD | vol_mult | VTI avg | VTI min | VTI max | VTI @ max DD | Regime @ max DD | max_dd_bar |
|----:|-------:|-------:|-------:|---------:|--------:|--------:|--------:|-------------:|-----------------|-----------:|
| 10 | −43.5% | −4.24 | −44.5% | 0.90 | 0.678 | 0.439 | 0.75 | **0.559** | RHYME_C | 284 |
| 27 | −43.2% | −3.94 | −43.9% | 0.95 | 0.667 | 0.439 | 0.75 | **0.712** | RHYME_D | 290 |
| 13 | −41.5% | −3.60 | −41.6% | 0.92 | 0.672 | 0.439 | 0.75 | **0.750** | RHYME_D | 289 |
| 3 | −37.8% | −3.44 | −38.4% | 0.93 | 0.686 | 0.439 | 0.75 | **0.553** | RHYME_C | 286 |
| 49 | −41.3% | −3.40 | −43.4% | 1.07 | 0.698 | 0.439 | 0.75 | **0.750** | RHYME_D | 291 |

Contrast — best 5 by Sharpe (same VTI band, same regime day counts):

| Run | Return | Sharpe | Max DD | VTI avg | VTI @ DD | Regime @ DD | vol_mult |
|----:|-------:|-------:|-------:|--------:|---------:|-------------|---------:|
| 18 | +251% | 8.12 | −2.9% | 0.687 | 0.657 | RHYME_C | 0.93 |
| 16 | +234% | 6.63 | −2.9% | 0.687 | 0.657 | RHYME_C | 1.07 |
| 28 | +188% | 6.45 | −4.1% | 0.686 | 0.657 | RHYME_C | 0.97 |
| 7 | +208% | 6.08 | −3.8% | 0.686 | 0.750 | RHYME_E | 1.03 |
| 21 | +160% | 5.39 | −5.8% | 0.687 | 0.750 | RHYME_E | 1.01 |

---

## 3. What the wipeouts have in common

### A. Late-window troughs (strongest shared trait)
All five worst paths print **max DD in the last ~10 bars** of a 292-bar window (bars 284–291).  
This points to **end-of-sample market conditions** (spring–summer 2026 in this dataset) interacting with path noise — not a mid-sample regime trap.

### B. Dynamic VTI is *high*, not collapsed
Across wipeouts:
- **VTI average ≈ 0.67–0.70** (same as winners ≈ 0.69)
- Band always spans **~0.44 → 0.75** (uses ceiling; min sits slightly above the 40% hard floor)
- At the DD trough, VTI is often **mid-to-high** (0.55–0.75), including **0.75 on 2/5 wipeouts**

**Conclusion:** wipeouts are **not** explained by Dynamic VTI pinning at 40% and dumping risk into active sleeves. If anything, failures occur **while the book is still majority-VTI**.

Overall sample: **74%** of runs have `vti_at_max_dd ≥ 0.65`; only **12%** have `vti_at_max_dd ≤ 0.50`.

### C. Regime day counts are identical for *all* 50 runs
Every run shows the same histogram:

`RHYME_D: 124 · RHYME_C: 109 · RHYME_E: 59`

Price/vol perturbations in this MC **do not change RHYME occupancy**. Differentiating wipeouts vs winners is therefore **not** “a different regime sequence.”

At the trough label:
- Wipeouts: **RHYME_D (3) / RHYME_C (2)** — *not* mostly E
- Full sample troughs: E 21 / D 17 / C 12

So the catastrophic cluster’s troughs often happen in **C/D** with **high VTI**, while many non-wipeout paths trough in **E**. Regime *label at trough* is not the discriminator.

### D. vol_mult / regime_drift
Wipeout `vol_mult` is mostly **≤ 1.0** (slightly calmer scaling), except run 49.  
`regime_drift` is mildly negative (~−0.003).  
**Not** a “extra volatility shock” story.

### E. Same failure shape as 90d, thicker on 365d
90d: ~10% cluster with −16% to −27% DD.  
365d: worst decile reaches **~−38% to −45% DD**, and the **median** path is already underwater. The left tail is structural to this window + stack, not a one-off RNG hiccup.

---

## 4. Is Dynamic VTI contributing?

| Hypothesis | Evidence | Verdict |
|------------|----------|---------|
| VTI floor too low → over-active in stress | Wipeouts keep **high** VTI avg; trough VTI often ≥55% | **Unlikely primary cause** |
| VTI ceiling trapping beta in a grind-down | Several wipeouts hit DD with VTI at **0.75** | **Plausible amplifier** — locked high beta into a late adverse stretch |
| Allocator thrashing | min/max still 0.44–0.75 every run; avg stable ~0.69 | Thrash amplitude exists but **doesn’t separate** wipeouts from winners |
| Lack of thinking | All runs `no-thinking` (same as 90d MC) | **Cannot exonerate or blame** thinking from this design — need A/B with thinking ON |

**Working theory:** Dynamic VTI is doing its job as a **high core** (often 65–75%). Left-tail damage is mostly **path noise on the residual active book + being long beta into a late-window drawdown**, not “VTI abandoned the portfolio.” High VTI may **limit** how bad paths get versus a 20% VTI paper profile, but it also **does not prevent** −40% DD when the residual book and/or VTI itself sell off together.

The enormous **right tail** (+160% to +251%) with the same VTI averages shows active sleeves (or path compounding) can dominate either direction — another sign the stack is **highly path-sensitive** on this window.

---

## 5. Note on replaying the original 90d wipeout IDs

A seed-faithful replay of runs 19/39/42/44/45 on current caches **did not reproduce** the original equity outcomes (universe drifted **362 → 338** symbols, changing RNG consumption per bar).  

Replay artifact file: `monte_carlo_v154_wipeout_replay_90.json` (diagnostics only; **not** used as primary evidence).

The 365d MC above is the authoritative left-tail study under current data + diagnostics.

---

## 6. Recommendations

1. **Do not treat the 90d MC as the paper baseline alone.** 365d shows median Sharpe &lt; 0 and much deeper DD — short-window optimism was real.
2. **Dynamic VTI 40–75% is not the smoking gun for the wipeout cluster.** Next experiments should instrument **active sleeve PnL during the DD window** (SPY/NYSE/stat-arb/shorts), not further VTI floor tweaks as the first lever.
3. **Optional VTI stress test:** force a higher floor (e.g. 55–75%) on the same 365d MC to see if late-window wipeouts shrink — tests the “ceiling trap” amplifier hypothesis.
4. **Thinking A/B:** re-run 365d with thinking ON (capped ±6%/±10%) vs OFF to see if LLM tilts reduce late-window left tail — currently unknown.
5. **Path-fragility check:** the +250% right tail with ~69% avg VTI is suspicious; confirm no leverage/notional bug on perturbed paths before celebrating upside.

---

## 7. Bottom line

The catastrophic cluster **persists and worsens** on a 365d window. Wipeouts share **late-sample troughs**, **high Dynamic VTI**, and **non-E trough regimes** — not low-VTI abandonment or a unique regime sequence.  

**Dynamic VTI is more likely an incomplete shield (and possible high-beta amplifier) than the root cause.**  
**Lack of thinking remains untested.**  

Next useful experiment: same 365d MC with **per-sleeve DD attribution** and/or **thinking ON**, not another VTI-floor-only tweak.
