# Felix / Social Sleeve Bearish Macro Tuning Review

**Date:** 2026-07-15  
**Backtest:** `python backtester.py --days 365 --paper-aggressive --compare-felix-social`  
**Log:** `scripts/analysis/_felix_compare_365_quiet.log`  
**Window:** 2025-09-09 → 2026-07-15 (310 sim bars)  
**Benchmark:** VTI buy & hold **+16.55%**

---

## Goal

On strong bearish Felix/Andrei sentiment (unwind / crash language), tilt the **paper** social sleeve toward **GLD / cash** instead of SPY.

Gate: paper-only via `PAPER_SOCIAL_SLEEVE_ENABLED` + `FELIX_SENTIMENT_ENABLED` (+ `PAPER_SOCIAL_MACRO_BOOST_ENABLED` for enhanced weights).

---

## What changed

| Area | Change |
|------|--------|
| `modules/sentiment_keywords.py` | High-negative macro terms (`unwind`, `crash`, `collapse`, `correction`, `meltdown`, `blow off top`, `overvalued`, `bubble`, `recession`, `hard landing`); heavier weights + creator (Felix/Andrei) penalty |
| `modules/felix_sentiment.py` | Creator transcript scoring when paper social + Felix enabled; macro hit counts on enrich |
| `modules/social_sleeve.py` | If blended score **&lt; `SOCIAL_BEARISH_GLD_THRESHOLD` (−0.6)** and macro keywords present → `GLD` (`strong_macro_bear_gld`) |
| `config.py` | `SOCIAL_BEARISH_GLD_THRESHOLD=-0.6`, `PAPER_SOCIAL_MACRO_BOOST_ENABLED`, `paper_social_bearish_tuning_enabled()` |
| Compare runner | 3-way: social off / legacy / enhanced Felix |

---

## 365d paper-aggressive A/B

| Config | Return | Sharpe | MaxDD | AvgAct | GLD% | Social sleeve |
|--------|--------|--------|-------|--------|------|---------------|
| **Paper aggressive (social off)** | **+29.95%** | **1.51** | −7.24% | 52.4% | 0.0% | — |
| Paper social (legacy tuning) | +27.30% | 1.42 | −7.56% | 52.4% | 0.0% | — |
| Paper social (**enhanced Felix**) | +26.03% | 1.37 | −7.27% | 52.4% | **94.9%** | **+7.0%** |

Δ vs social off (enhanced): return **−3.92 pp**, Sharpe **−0.14**, MaxDD roughly flat, **GLD target share 0% → 94.9%**.

---

## Interpretation

1. **Detection / tilt worked.** Enhanced path spent ~95% of social-target time in GLD; legacy stayed at 0% GLD. That is the intended behavior for strong bearish creator macro language.
2. **Social sleeve PnL was positive (+7.0%)** under enhanced tuning, but the **book as a whole lagged** social-off in this window because the tape was bullish (VTI +16.5%, paper ~+30%). A GLD/cash tilt is a hedge: it costs return when risk assets keep rising.
3. **Legacy social** underperformed social-off without showing a GLD tilt (GLD% 0), so it was not a useful defensive sleeve in this sample.
4. **Max drawdown** for enhanced (−7.27%) was slightly better than legacy (−7.56%) and similar to social-off (−7.24%) — no clear drawdown win on this bullish 310-bar window.

---

## Recommendation

| Decision | Detail |
|----------|--------|
| **Keep the code path** | Lightweight, paper-gated, configurable (`SOCIAL_BEARISH_GLD_THRESHOLD`, `PAPER_SOCIAL_MACRO_BOOST_ENABLED`). |
| **Do not make enhanced the default paper winner for return** | In this bullish 365d window, social-off still wins on total return / Sharpe. |
| **Use enhanced for hedge / crash-language experiments** | Enable on paper when validating unwind/crash detection: set `PAPER_SOCIAL_SLEEVE_ENABLED=true`, `FELIX_SENTIMENT_ENABLED=true`, `PAPER_SOCIAL_MACRO_BOOST_ENABLED=true`. |
| **Live** | Unchanged — live Profile A does not pick up this boost unless explicitly mirrored later. |

**Verdict:** **HOLD as optional paper feature** (not reject the mechanism). Mechanism proven (GLD% spike); portfolio edge not proven on this bullish window. Re-check after the next risk-off / high-macro-language stretch, or run a shorter window around known unwind narratives.

---

## Config knobs (paper)

```env
PAPER_SOCIAL_SLEEVE_ENABLED=true
FELIX_SENTIMENT_ENABLED=true
PAPER_SOCIAL_MACRO_BOOST_ENABLED=true   # enhanced weights + strong-bear GLD path
SOCIAL_BEARISH_GLD_THRESHOLD=-0.6       # score < this + macro hits → GLD
```

Re-run compare:

```powershell
Set-Location c:\Users\Owner\PythonTrading\stock-bot
$env:PAPER_DEPLOY_DEBUG="false"
python backtester.py --days 365 --paper-aggressive --compare-felix-social
```
