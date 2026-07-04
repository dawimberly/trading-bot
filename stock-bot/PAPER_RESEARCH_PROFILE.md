# Paper Research Profile (velocity)

High **order flow**, **sleeve attribution**, and **actionable tuning knobs** for backtests and the paper book. This is a **`.env` overlay** on [Profile B](README.md#profile-b-best-paper-bot-v22-paper_aggressive) — it does not replace `PAPER_CHASE_MODE` or `run_paper_bot.py`.

**Locked profile:** **Realistic Research v1.3** — default for **alpaca_paper** (`run_paper_bot.py`, portal paper book). Stat Arb **v1.3** (10–12 pairs, sector-neutral preference), **dynamic core** VTI/SPY 30–50%, **protective shorts** (max 15%), weekly **Bot Health Score**.

**Startup line (paper + live):**
`>>> PAPER BOT: Realistic Research v1.3 (Aggressive) | Live Bot: Conservative 85% VTI`

**Headline (paper bot):**
`>>> REALISTIC RESEARCH v1.3 (LOCKED) - Stat Arb v1.3 | Dynamic Core 30-50% | Protective Shorts | Paper Bot Default <<<`

**Protective shorts (paper only):** `Protective Shorts: ON (max 15%, RHYME_B/E selective, RHYME_E ON, tail-risk sized)` — never on live Profile A.

**When to use:** iterating on NYSE stat arb logic, protective shorts, dynamic core, fill-rate funnels, and reject attribution — not for live ~$300 (Profile A uses Live Conservative 85% VTI).

**When you see it:** startup prints the headline + `>>> STAT ARB v1.3: ...` line when `PAPER_CHASE_MODE` is active (`run_paper_bot.py` / portal **alpaca_paper**). `run_all.py` prints the same banners under paper chase.

---

## Realistic Research v1.3 — full upgrade package (locked default)

Builds on v1.2: more stat-arb capacity, sector-neutral pair preference, dynamic core allocation, selective protective shorts, and enhanced weekly monitoring.

| Area | v1.3 default | Env |
|------|--------------|-----|
| **Stat arb max pairs** | **10 / 12 / 14** (dynamic scaling) | `PAPER_STAT_ARB_MAX_PAIRS`, `_EXPANDED`, `_CEILING` |
| Min correlation | **0.72** | `PAPER_STAT_ARB_MIN_CORR` |
| Cointegration p-value | **< 0.12** | `PAPER_STAT_ARB_COINT_PVALUE` |
| Sector-neutral boost | **ON** (×1.12 cross-sector pairs) | `PAPER_STAT_ARB_SECTOR_NEUTRAL_PREF` |
| Liquidity filter | **$25M** avg $vol | `PAPER_STAT_ARB_MIN_DOLLAR_VOLUME` |
| Exits | **1.6:1 RR + trail + 35b max** | `PAPER_STAT_ARB_RISK_REWARD`, trailing fracs, `_MAX_HOLD_BARS` |
| Z entry (vol) | **2.0–2.6** | `PAPER_STAT_ARB_Z_ENTRY_BASE`, `_MAX` |
| **Dynamic core** | **VTI/SPY 30–50%** via Sharpe | `DYNAMIC_CORE_ENABLED=true`, `DYNAMIC_CORE_MIN/MAX_PCT` |
| Core fallback (dynamic off) | **SPY @ 40% locked** | `CORE_ALLOCATOR_LOCKED=true` |
| **Protective shorts max** | **15%** | `PROTECTIVE_SHORT_MAX_PCT` |
| RHYME_E shorts | **ON** (deep bear + bubble) | `SHORT_RHYME_E_ENABLED` |
| Strong RHYME_B bubble gate | **≥0.55** | `SHORT_RHYME_B_STRONG_BUBBLE` |
| Strong RHYME_E bubble gate | **≥0.50** | `SHORT_RHYME_E_STRONG_BUBBLE` |
| Weekly monitoring | **Bot Health 0–100**, 30d/all-time Sharpe | `scripts/generate_weekly_report.py --test` |

Banner: `>>> STAT ARB v1.3: cointegration p<0.12 | corr>=0.72 | liquidity>$25M | max 10-14 pairs Z 2.0-2.6 | ... | RR 1.6:1 + trail`

Compare before/after push: `python backtester.py --paper-aggressive --compare-stat-arb-v13-push --days 365 --no-thinking`

---

## Realistic Research v1.2 — Stat Arb v1.2 (reference)

| Setting | Default | Env |
|---------|---------|-----|
| Max pairs (base / expanded / ceiling) | **8 / 9 / 10** | `PAPER_STAT_ARB_MAX_PAIRS`, `_EXPANDED`, `_CEILING` |
| Risk/reward | **1.5:1** | `PAPER_STAT_ARB_RISK_REWARD` |
| Max hold | **35 bars** | `PAPER_STAT_ARB_MAX_HOLD_BARS` |
| Min correlation | **0.75** | `PAPER_STAT_ARB_MIN_CORR` |
| Cointegration p-value | **< 0.10** | `PAPER_STAT_ARB_COINT_PVALUE` |
| Liquidity filter | **$25M** avg $vol | `PAPER_STAT_ARB_MIN_DOLLAR_VOLUME` |
| Trailing stop | **50% arm / 35% pullback** | `PAPER_STAT_ARB_TRAILING_ARM_FRAC`, `_PULLBACK_FRAC` |
| Dedicated cap | **7%** | `STAT_ARB_SLEEVE_CAP_PCT=0.07` |
| Vol scaling | **ON** when 20d vol > 18% | `STAT_ARB_VOL_SCALING_ENABLED` |

**365d validation (v1.2 locked):** +17.1% return, Sharpe 1.33, MaxDD −7.35%, 12 stat-arb pairs.

Compare vs v1.1: `python backtester.py --paper-aggressive --compare-stat-arb-v12 --days 365 --no-thinking`

---

## Realistic Research v1.1c — Stat Arb dedicated sleeve (Option 1)

Builds on v1.1b: stat arb has its **own sleeve cap** (no longer competes with NYSE momentum for `no_room`).

| Setting | Default | Env |
|---------|---------|-----|
| Dedicated cap | **7%** (~5.6% effective @ 40% core) | `STAT_ARB_SLEEVE_CAP_PCT=0.07` |
| Cap enabled | **ON** (paper/research) | `STAT_ARB_SLEEVE_CAP_ENABLED` |
| Vol scaling | **ON** when 20d vol > 18% | `STAT_ARB_VOL_SCALING_ENABLED` |
| Vol scale floor | **0.30×** (up to 70% notional cut) | `STAT_ARB_VOL_MIN_NOTIONAL_SCALE` |

---

## Protective shorts (v1.3 paper only)

Directional hedge on SPY/QQQ — **not** enabled on live Profile A.

| Setting | Default | Env |
|---------|---------|-----|
| Master switch | **ON** | `PROTECTIVE_SHORT_ENABLED` |
| Gross exposure range | **8%-15%** | `PROTECTIVE_SHORT_MIN_PCT`, `PROTECTIVE_SHORT_MAX_PCT` |
| RHYME_B trigger | **ON** + VIX>25 rising + exhaustion | depth ≥2% below MA |
| RHYME_E trigger | bubble **≥60%** + deep bear | `SHORT_RHYME_E_STRONG_BUBBLE=0.60` |
| Single-name shorts | **OFF** (broad only) | `SHORT_OPPORTUNISTIC_ENABLED=false` |
| Exits | **1.5:1 RR** (3%/2%) + trail + 30b max | `SHORT_PROFIT/STOP`, trailing fracs |
| Long hedge | **ON** (78% floor when shorts active) | `SHORT_LONG_HEDGE_ENABLED` |

Compare: `python backtester.py --paper-aggressive --compare-opportunistic-shorts --days 365 --no-thinking`

---

## `.env` snippet (v1.3)

```env
# --- Realistic Research v1.3 ---
PAPER_AGGRESSIVE=true
PAPER_CHASE_MODE=1
TAIL_RISK_CONTROLS_ENABLED=true

# Dynamic core (VTI/SPY 30-50% via Sharpe; fallback: CORE_ALLOCATOR_LOCKED=true @ 40%)
DYNAMIC_CORE_ENABLED=true
DYNAMIC_CORE_MIN_PCT=0.30
DYNAMIC_CORE_MAX_PCT=0.50
CORE_ALLOCATOR_LOCKED=false

STAT_ARB_SLEEVE_CAP_ENABLED=true
STAT_ARB_SLEEVE_CAP_PCT=0.07
STAT_ARB_VOL_SCALING_ENABLED=true

PAPER_STAT_ARB_ENABLED=true
PAPER_CRYPTO_ENABLED=false

PAPER_STAT_ARB_MIN_CORR=0.72
PAPER_STAT_ARB_COINT_PVALUE=0.12
PAPER_STAT_ARB_MAX_PAIRS=10
PAPER_STAT_ARB_MAX_PAIRS_EXPANDED=12
PAPER_STAT_ARB_MAX_PAIRS_CEILING=14
PAPER_STAT_ARB_SECTOR_NEUTRAL_PREF=true
PAPER_STAT_ARB_RISK_REWARD=1.6
PAPER_STAT_ARB_Z_ENTRY_MAX=2.6
PAPER_STAT_ARB_MIN_DOLLAR_VOLUME=25000000

PROTECTIVE_SHORT_MAX_PCT=0.15
SHORT_RHYME_E_ENABLED=true
SHORT_RHYME_B_STRONG_BUBBLE=0.55
SHORT_RHYME_E_STRONG_BUBBLE=0.50
```

---

## What “good” looks like (30d reference)

| Metric | Typical target |
|--------|----------------|
| Total orders | 100+ (equity-only) |
| Stat arb fill rate | 60–100% |
| Banners | `>>> REALISTIC RESEARCH v1.3 (LOCKED)` + `>>> STAT ARB v1.3: ...` + dynamic core line |
| Bot Health Score | **≥70** Good, **≥85** Excellent |
| Crypto | 0 entries unless `--paper-crypto` |

---

## vs Profile B v2.2 (Sharpe chase)

| | Profile B default | Research velocity (v1.3) |
|--|-------------------|---------------------------|
| Core | Dynamic 40–75% | **VTI/SPY 30–50% Sharpe-driven** |
| Tail risk | Opt-in | **ON by default** |
| Goal | Risk-adjusted return | **Order flow + attribution** |
| Crypto | Opt-in | **Off by default** |
| Stat arb | On | **On** (10–12 pairs, sector-neutral) |
| Shorts | Off on live | **Protective 15% max (paper)** |

---

## Commands

| Task | Command |
|------|---------|
| v1.3 vs v1.2 compare | `python backtester.py --paper-aggressive --compare-realistic-research-v13 --days 365 --no-thinking` |
| 30d smoke (equity) | `python backtester.py --days 30 --paper-aggressive --no-thinking` |
| 365d locked v1.3 | `python backtester.py --days 365 --paper-aggressive --no-thinking` |
| Weekly report (manual) | `python scripts/generate_weekly_report.py --test` |
| Stack check | `python status.py` |
