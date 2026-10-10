# NYSE Entry Quality Review — `PAPER_MOMENTUM_QUALITY_FIXES`

**Date:** 2026-07-15  
**Paper env:** `data/portal/users/dawimberly/books/alpaca_paper/.env` (`USE_DYNAMIC_UNIVERSE=true`, `PAPER_MOMENTUM_QUALITY_FIXES=true`)  
**Backtest:** `python scripts/research/backtest_intraday.py --quality-fixes --days 365`  
*(Note: comparison is built into `--quality-fixes`; there is no separate `--compare` flag.)*

---

## Executive verdict

| Horizon | Return (no fixes → fixes) | Sharpe | Max DD | Verdict |
|---------|---------------------------|--------|--------|---------|
| **365d** (103 tickers) | 3.58% → **4.45%** | 0.26 → **0.29** | -15.46% → **-15.22%** | **Net positive** on risk-adjusted returns |
| **730d** (prior run) | 9.57% → 6.86% | **0.30** → 0.27 | **-15.69%** → -16.97% | Slightly negative over full 2y window |

**Recommendation:** **Keep `PAPER_MOMENTUM_QUALITY_FIXES=true` as the paper default.** The filters improve the most recent 365-day window (higher return, Sharpe, and shallower drawdown) while blocking 12,860 open-cooldown signals and 49 gap-up entries. The 2-year sim is mixed — likely regime/sample drift — but the live rationale (avoid open-chase on gappy names like AMD/LMND) still holds. Tune rather than disable.

---

## 365-day comparison (primary)

| Metric | Without fixes | With fixes | Δ |
|--------|---------------|------------|---|
| Total return | 3.58% | **4.45%** | +0.87 pp |
| Sharpe (daily equity) | 0.26 | **0.29** | +0.03 |
| Max drawdown | -15.46% | **-15.22%** | +0.24 pp |
| Total trades | 324 | 316 | -8 |
| Win rate | 45.5% | 44.4% | -1.1 pp |
| Avg PnL / trade | 0.212% | **0.281%** | +0.069 pp |
| Avg hold (min) | 885 | 896 | +11 |
| Open cooldown blocked | 0 | **12,860** | — |
| Gap opens blocked (>2%) | 0 | **49** | — |

**Interpretation:** Quality fixes trade a small number of marginal entries for better average trade quality. Win rate dips slightly, but per-trade edge and portfolio Sharpe improve. Cooldown is doing the heavy lifting; gap filter is secondary but still meaningful.

---

## Entry-hour performance (365d intraday sim)

Per-hour Sharpe uses trade-level PnL: `mean(pnl) / std(pnl) × √n` (research metric, not portfolio Sharpe).

### Without quality fixes

| Hour (ET) | Trades | Win rate | Avg PnL % | Sharpe |
|-----------|--------|----------|-----------|--------|
| 09:30 | 134 | 47.8% | +0.35% | 1.15 |
| 10:00 | 61 | 50.8% | +0.73% | 1.55 |
| 11:00 | 42 | 31.0% | **-1.20%** | **-1.84** |
| 12:00 | 22 | 63.6% | +1.16% | 1.69 |
| 13:00 | 22 | 45.5% | +0.09% | 0.11 |
| 14:00 | 16 | 37.5% | +0.10% | 0.11 |
| 15:00 | 27 | 40.7% | +0.07% | 0.08 |

### With quality fixes

| Hour (ET) | Trades | Win rate | Avg PnL % | Sharpe |
|-----------|--------|----------|-----------|--------|
| 09:30 | **0** | — | — | — |
| 10:00 | 190 | 42.6% | +0.20% | 0.77 |
| 11:00 | 38 | 36.8% | **-0.59%** | **-1.10** |
| 12:00 | 25 | 56.0% | +1.14% | 1.59 |
| 13:00 | 11 | 45.5% | +0.49% | 0.43 |
| 14:00 | 23 | 47.8% | +0.78% | 1.10 |
| 15:00 | 29 | 55.2% | +0.71% | 1.10 |

---

## Open-chase vs midday windows

| Window | Variant | Trades | Win rate | Avg PnL % | Total PnL % | Sharpe |
|--------|---------|--------|----------|-----------|-------------|--------|
| **9:30–10:00** (open-chase) | No fixes | 134 | 47.8% | +0.35% | +46.5% | 1.15 |
| **9:30–10:00** | With fixes | **0** | — | — | — | — |
| **10:00–11:00** (post-cooldown pile-up) | No fixes | 61 | 50.8% | +0.73% | +44.3% | 1.55 |
| **10:00–11:00** | With fixes | 190 | 42.6% | +0.20% | +38.6% | 0.77 |
| **12:00–14:00** (midday preferred) | No fixes | 44 | 54.5% | +0.63% | +27.6% | 1.15 |
| **12:00–14:00** | With fixes | 36 | **52.8%** | **+0.94%** | +33.9% | **1.56** |

### Key takeaways

1. **Open-chase is fully blocked** — no entries in the 9:30 bucket with fixes active. That was the design goal.
2. **Cooldown shifts volume to 10:00**, where edge is thinner (Sharpe 0.77 vs 1.15–1.55 in the baseline open window). The 11:00 hour remains the worst bucket in both variants.
3. **Midday window improves with fixes** — higher avg PnL (+0.94% vs +0.63%) and Sharpe (1.56 vs 1.15), consistent with the 12:00–14:00 rank boost and looser RSI (72 vs 70).
4. In the **baseline** sim, open-chase (9:30–10:00) is not catastrophic on its own (+0.35% avg); the **11:00** hour and gap-chase names are the bigger leaks. Fixes help by rerouting flow and skipping gaps, but **post-cooldown 10:00 clustering** is the remaining weak spot.

---

## Live / paper journal (`entry_hour`)

Searched `paper_journal.csv`, `paper_chase_journal.csv`, and portal book paths. **No trade-level `entry_hour` rows yet** — journals are cycle/startup logs without the new exit columns populated. `entry_hour` will only appear on NYSE exits after quality fixes have been live through full entry→exit cycles.

**Action:** Re-run this section after ~2–4 weeks of paper trading once `paper_journal` / per-book journals show `exit_reason` + `entry_hour` on NYSE closes.

---

## 730-day context (prior run, same universe)

| Metric | Without fixes | With fixes |
|--------|---------------|------------|
| Total return | **9.57%** | 6.86% |
| Sharpe | **0.30** | 0.27 |
| Max DD | **-15.69%** | -16.97% |
| Cooldown blocked | 0 | 21,771 |
| Gap blocked | 0 | 189 |

Over 2 years, fixes slightly hurt absolute return and Sharpe. The 365d window reverses that — suggesting **recent regime favors quality gating** or that early-sample open-chase winners don't repeat. Do not use 2y alone to disable; use 365d + live journal as the decision window.

---

## Proposed tunings (small, paper-only)

### 1. Extend open cooldown to **10:30 ET** (recommended)

**Why:** 11:00 hour is consistently negative (-1.20% / -0.59% avg in 365d sim). Many cooldown-deferred entries land at 10:00 with mediocre Sharpe (0.77). Pushing the no-entry window to 10:30 should reduce the 10:00 pile-up without blocking the strong 12:00–14:00 window.

**Change:** `NYSE_OPEN_COOLDOWN_END=10:30` (or hardcode in `_nyse_open_cooldown_active` / `COOLDOWN_END` in intraday backtester for A/B).

### 2. Tighten overnight gap skip to **1.5%** (recommended)

**Why:** 49 gap blocks at 2% over 365d; tightening catches more open-chase on extended names. Low risk — only skips entries, doesn't force exits.

**Change:** `gap > 0.015` in `_nyse_momentum_quality_skip` / `GAP_THRESHOLD = 0.015` in backtester.

### Optional: Stricter off-peak RSI (**68** outside 12:00–14:00)

**Why:** 11:00 weakness may be partially overbought momentum chasing. Keep RSI 72 in the preferred window; drop off-peak from 70 → 68.

**Change:** `NYSE_RSI_MAX_OFF_PEAK = 68` in `pipeline_strategies.py`.

---

## Should we keep it on paper default?

| Criterion | Assessment |
|-----------|------------|
| Risk-adjusted return (365d) | **Yes** — Sharpe 0.29 vs 0.26, better DD |
| Absolute return (365d) | **Yes** — +4.45% vs +3.58% |
| Risk-adjusted return (730d) | Marginal no — Sharpe 0.27 vs 0.30 |
| Open-chase harm | **Mitigated** — 0 trades 9:30–10:00; remaining leak is 10:00–11:00 |
| Midday bias | **Working** — 12:00–14:00 Sharpe 1.56 with fixes |
| Live observability | Pending — `entry_hour` not in journals yet |

**Bottom line:** On the **365-day** horizon that matches current paper evaluation, the filter set is **net positive on risk-adjusted returns**. Keep **`PAPER_MOMENTUM_QUALITY_FIXES=true`** on paper. Next step: A/B **cooldown to 10:30** and **gap 1.5%** in the intraday backtester before changing live paper config.

---

## Reproduce

```powershell
$env:PYTHONTRADING_ENV_FILE = "data/portal/users/dawimberly/books/alpaca_paper/.env"
python scripts/research/backtest_intraday.py --quality-fixes --days 365
```

Trade log (with fixes): `scripts/research/intraday_backtest_results.csv`  
Run log: `scripts/analysis/_intraday_365.log`
