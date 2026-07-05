# Paper / Research Profile (Realistic Research v1.4 — **OFFICIALLY LOCKED**)

**Audience:** Profile B — `alpaca_paper` / `--paper-aggressive` research book only. Live Profile A unchanged.

**Locked profile:** **Realistic Research v1.4** — official paper bot default as of July 2026.

```
>>> PAPER BOT: Realistic Research v1.4 (Aggressive) | v1.4 — improved shorts + Stat Arb | Live Bot: Conservative 85% VTI
>>> REALISTIC RESEARCH v1.4 (LOCKED) - Stat Arb 10-14p | Dynamic Core 30-50% | Protective + Sector Shorts | RHYME_E waiver | v1.4 — improved shorts + Stat Arb | Paper Bot Default <<<
>>> v1.4 — improved shorts + Stat Arb <<<
```

---

## Realistic Research v1.4 — full upgrade package

Builds on v1.3: sector-level shorts, short RR 1.6:1, 8–15% dynamic gross, enhanced monitoring/Telegram.

| Area | v1.4 default | Env |
|------|--------------|-----|
| **Stat arb pairs** | 10 / 12 / 14 dynamic | `PAPER_STAT_ARB_MAX_PAIRS*` |
| Correlation floor | **≥0.72** | `PAPER_STAT_ARB_MIN_CORR` |
| Cointegration | **p < 0.12** | `PAPER_STAT_ARB_COINT_PVALUE` |
| Liquidity | **>$25M** avg $vol | `PAPER_STAT_ARB_MIN_DOLLAR_VOLUME` |
| Stat arb exits | **1.6:1 RR** + trail + **35b** max | `PAPER_STAT_ARB_RISK_REWARD`, `PAPER_STAT_ARB_MAX_HOLD_BARS` |
| **Dynamic core** | VTI/SPY **30–50%** (Sharpe-based) | `DYNAMIC_CORE_*` |
| Core fallback | **40% SPY** locked | `CORE_ALLOCATOR_LOCKED_CHOICE=spy` |
| **Protective shorts** | **8–15%** gross, RR **1.6:1** | `PROTECTIVE_SHORT_MIN/MAX_PCT`, `SHORT_PROFIT/STOP` |
| RHYME_B | VIX≥22 rising + exhaustion + depth≥2% | — |
| RHYME_E | VIX≥22 + bubble≥60 + depth≥3% (**exhaustion waived**) | `SHORT_RHYME_E_EXHAUSTION_REQUIRED=false` |
| **Sector shorts** | weak sectors, **≤8%/name**, total cap 15% | `SECTOR_SHORT_*` |
| Long hedge | **78% floor** when shorts active | `SHORT_LONG_HEDGE_*` |
| Monitoring | 30d/all-time Sharpe, Bubble Score, Health 0–100 | weekly MD/HTML + Telegram |

Banner: `>>> STAT ARB v1.4: ... | RR 1.6:1 + trail | max 10-14 pairs`

Protective banner: `Protective Shorts: ON (8%-15%, RR 1.6, selective) | RHYME_E waiver active | Sector shorts ≤8%/name`

---

## Protective + sector shorts (paper only)

| Setting | Default | Notes |
|---------|---------|-------|
| Master switch | **ON** | `PROTECTIVE_SHORT_ENABLED` |
| Sector shorts | **ON** | `SECTOR_SHORT_ENABLED` |
| Gross range | **8–15%** | dynamic by regime + bubble |
| Per-sector cap | **8%** | XLE, XLF, etc. when very weak |
| Single-name shorts | **OFF** | broad + sector ETFs only |

Compare: `python backtester.py --paper-aggressive --compare-opportunistic-shorts --days 365 --no-thinking`

---

## Backtest validation

| Compare | Command |
|---------|---------|
| **v1.4 vs v1.3** | `python backtester.py --paper-aggressive --compare-realistic-research-v14 --days 365 --no-thinking` |
| v1.3 vs v1.2 | `--compare-realistic-research-v13` |
| Shorts ON/OFF | `--compare-opportunistic-shorts` |

---

## `.env` snippet (v1.4)

```env
REALISTIC_RESEARCH_VERSION=1.4
PROTECTIVE_SHORT_MIN_PCT=0.08
PROTECTIVE_SHORT_MAX_PCT=0.15
SECTOR_SHORT_ENABLED=true
SECTOR_SHORT_MAX_PCT=0.08
SHORT_RHYME_E_EXHAUSTION_REQUIRED=false
SHORT_BUBBLE_MIN_FOR_RHYME_E=60
SHORT_VIX_MIN=22
SHORT_PROFIT_TARGET_PCT=0.032
SHORT_STOP_LOSS_PCT=0.02
PAPER_STAT_ARB_MAX_PAIRS=10
PAPER_STAT_ARB_MAX_PAIRS_CEILING=14
DYNAMIC_CORE_ENABLED=true
```

---

## Weekly monitoring

- **Reports:** `reports/weekly/YYYY-MM-DD.{md,html}` — Bot Health, 30d/all-time Sharpe, bubble score, short activity
- **Telegram:** Friday after close — equity week + research addon (health, Sharpe, bubble, shorts)
- **Generate test:** `python scripts/generate_weekly_report.py --test`
