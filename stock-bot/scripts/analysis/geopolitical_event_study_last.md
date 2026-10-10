# Geopolitical event study (report only)

Generated: 2026-07-29 21:28 UTC
Research DB: `data\research\geopolitical\research_macro.db`

**REPORT ONLY - freeze on; labels only; no trade signals; no promote; no live/paper default changes**

## Fidelity legend

| Grade | Meaning |
|-------|---------|
| macro_only | VIX/WTI (+ available equity proxies) |
| partial_strategy_proxy | SPY + VIX + WTI coverage around event |
| full_freeze_compatible | VTI/SPY/XLE/GLD/VIX/WTI all cover the window |
| insufficient_data | Cannot grade — skip or macro-only if any series |

## Series manifest (research store)

| Series | Source | First | Last | Rows | Checksum |
|--------|--------|-------|------|------|----------|
| VIX | FRED:VIXCLS | 1990-01-02 | 2026-07-28 | 9239 | `48e3e1d55d12eec4` |
| WTI | FRED:DCOILWTICO | 1986-01-02 | 2026-07-27 | 10210 | `a6e9e3c8145804df` |
| SPY | yfinance:SPY | 1993-01-29 | 2026-07-29 | 8431 | `c88b1837c78489a2` |
| VTI | yfinance:VTI | 2001-06-15 | 2026-07-29 | 6316 | `4de3c8ae96583a71` |
| XLE | yfinance:XLE | 1998-12-22 | 2026-07-29 | 6941 | `5bde84e5ef5de99f` |
| GLD | yfinance:GLD | 2004-11-18 | 2026-07-29 | 5456 | `8dd616f190e7a32c` |
| VIX_YF | yfinance:^VIX | 1990-01-02 | 2026-07-29 | 9211 | `cae517c3b710b9d7` |
| CL_F | yfinance:CL=F | 2000-08-23 | 2026-07-29 | 6510 | `b0571391c2842e7e` |
| GC_F | yfinance:GC=F | 2000-08-30 | 2026-07-29 | 6501 | `e1c6498ea45f99ed` |

## Event windows

### 1990-08-02 — Iraq invades Kuwait / Gulf War start

- Grade: **macro_only** (calendar tier: macro_only)
- Sources:
  - [Encyclopaedia Britannica](https://www.britannica.com/event/Persian-Gulf-War)
  - [US State Department Office of the Historian](https://history.state.gov/milestones/1989-1992/gulf-war)

| Series | Pre-20d | Imm-5d | Post-20d | Post-60d | Level@event |
|--------|---------|--------|----------|----------|-------------|
| SPY | n/a | n/a | n/a | n/a | n/a |
| VTI | n/a | n/a | n/a | n/a | n/a |
| XLE | n/a | n/a | n/a | n/a | n/a |
| GLD | n/a | n/a | n/a | n/a | n/a |
| VIX | +19.54% | +60.30% | +49.53% | +37.35% | 20.43 |
| WTI | +29.07% | +24.84% | +28.72% | +56.39% | 23.71 |
| CL_F | n/a | n/a | n/a | n/a | n/a |
| GC_F | n/a | n/a | n/a | n/a | n/a |

### 2001-09-11 — September 11 attacks

- Grade: **partial_strategy_proxy** (calendar tier: partial_strategy_proxy)
- Sources:
  - [911 Memorial & Museum](https://www.911memorial.org/911-faqs)
  - [NYSE historical note (market closure)](https://www.nyse.com/publicdocs/nyse/historical/NYSE_Timeline.pdf)

| Series | Pre-20d | Imm-5d | Post-20d | Post-60d | Level@event |
|--------|---------|--------|----------|----------|-------------|
| SPY | -5.96% | -5.22% | +0.35% | +8.48% | 70.07 |
| VTI | -6.20% | -4.75% | -1.11% | +7.28% | 32.33 |
| XLE | -0.86% | -2.04% | -9.47% | -2.74% | 7.35 |
| GLD | n/a | n/a | n/a | n/a | n/a |
| VIX | +41.89% | +31.16% | -22.61% | -34.29% | 31.84 |
| WTI | +1.77% | +7.02% | -16.38% | -19.60% | 27.65 |
| CL_F | +4.78% | +4.27% | -21.16% | -24.75% | 27.63 |
| GC_F | -1.20% | +6.70% | +0.69% | -4.41% | 271.60 |

### 2003-03-20 — Iraq War invasion begins

- Grade: **partial_strategy_proxy** (calendar tier: partial_strategy_proxy)
- Sources:
  - [Encyclopaedia Britannica](https://www.britannica.com/event/Iraq-War)
  - [US Army Center of Military History](https://history.army.mil/html/bookshelves/resmat/iraq.html)

| Series | Pre-20d | Imm-5d | Post-20d | Post-60d | Level@event |
|--------|---------|--------|----------|----------|-------------|
| SPY | +3.83% | -0.31% | -0.87% | +5.53% | 57.43 |
| VTI | +3.98% | -0.17% | -0.79% | +6.17% | 27.10 |
| XLE | +1.52% | -1.68% | -3.05% | +3.18% | 5.97 |
| GLD | n/a | n/a | n/a | n/a | n/a |
| VIX | +2.73% | -5.55% | -10.94% | -32.62% | 30.44 |
| WTI | -22.14% | +16.77% | +1.08% | +0.77% | 28.62 |
| CL_F | -21.83% | -2.24% | +0.84% | +0.77% | 28.61 |
| GC_F | -4.94% | -1.35% | -2.19% | +9.40% | 332.90 |

### 2011-03-19 — Libya intervention / oil spike window

- Grade: **partial_strategy_proxy** (calendar tier: partial_strategy_proxy)
- Sources:
  - [NATO Libya operation timeline](https://www.nato.int/cps/en/natolive/topics_71652.htm)
  - [Encyclopaedia Britannica](https://www.britannica.com/event/Libya-Revolt-of-2011)

| Series | Pre-20d | Imm-5d | Post-20d | Post-60d | Level@event |
|--------|---------|--------|----------|----------|-------------|
| SPY | -3.63% | +0.89% | +2.40% | +3.56% | 97.64 |
| VTI | -3.54% | +0.86% | +2.67% | +3.66% | 50.65 |
| XLE | -3.69% | +0.40% | +2.63% | -3.82% | 22.43 |
| GLD | +0.52% | +0.06% | +3.25% | +4.64% | 138.37 |
| VIX | +33.19% | -12.66% | -13.29% | -21.25% | 24.44 |
| WTI | +4.08% | +2.62% | +9.68% | -2.77% | 101.06 |
| CL_F | +4.23% | +3.20% | +10.22% | -2.18% | 101.07 |
| GC_F | +0.47% | +0.60% | +3.31% | +4.87% | 1415.90 |

### 2014-03-18 — Russia annexes Crimea

- Grade: **partial_strategy_proxy** (calendar tier: partial_strategy_proxy)
- Sources:
  - [Encyclopaedia Britannica](https://www.britannica.com/place/Crimea)
  - [Council on Foreign Relations timeline](https://www.cfr.org/timeline/ukraines-struggle-independence)

| Series | Pre-20d | Imm-5d | Post-20d | Post-60d | Level@event |
|--------|---------|--------|----------|----------|-------------|
| SPY | +1.52% | -0.34% | -1.34% | +0.65% | 152.04 |
| VTI | +1.52% | -0.44% | -2.04% | -0.59% | 79.70 |
| XLE | +0.52% | -0.09% | +1.73% | +7.18% | 27.35 |
| GLD | +1.96% | -1.65% | -4.37% | -4.69% | 130.62 |
| VIX | +1.18% | +3.31% | +7.23% | -14.33% | 14.52 |
| WTI | -2.77% | -0.11% | +0.35% | +2.23% | 100.08 |
| CL_F | -2.82% | -0.24% | +0.74% | +2.33% | 99.70 |
| GC_F | +2.32% | -1.69% | -4.49% | -4.83% | 1359.00 |

### 2014-11-27 — Oil price collapse (late 2014)

- Grade: **macro_only** (calendar tier: macro_only)
- Sources:
  - [EIA oil price retrospective](https://www.eia.gov/todayinenergy/detail.php?id=19451)
  - [Reuters OPEC 2014 coverage archive](https://www.reuters.com/article/us-opec-meeting-idUSKCN0JB1M120141127)

| Series | Pre-20d | Imm-5d | Post-20d | Post-60d | Level@event |
|--------|---------|--------|----------|----------|-------------|
| SPY | +2.11% | -0.05% | -2.61% | -0.30% | 170.57 |
| VTI | +2.10% | -0.23% | -2.43% | +0.14% | 87.98 |
| XLE | -2.84% | +1.44% | -3.43% | -1.98% | 27.04 |
| GLD | +1.94% | +2.70% | +1.93% | +9.70% | 115.16 |
| VIX | -8.00% | -3.60% | +45.84% | +16.43% | 12.07 |
| WTI | -6.37% | +1.59% | -14.42% | -32.06% | 73.70 |
| CL_F | -6.31% | +1.10% | -14.63% | -31.75% | 73.69 |
| GC_F | +2.31% | +2.04% | +1.63% | +8.87% | 1196.60 |

### 2020-01-03 — Soleimani strike / Iran escalation

- Grade: **partial_strategy_proxy** (calendar tier: partial_strategy_proxy)
- Sources:
  - [Encyclopaedia Britannica](https://www.britannica.com/biography/Qassem-Soleimani)
  - [CRS report summary](https://crsreports.congress.gov/product/pdf/IN/IN11212)

| Series | Pre-20d | Imm-5d | Post-20d | Post-60d | Level@event |
|--------|---------|--------|----------|----------|-------------|
| SPY | +1.41% | +0.63% | +2.89% | -6.88% | 293.88 |
| VTI | +1.42% | +0.60% | +2.90% | -6.89% | 149.20 |
| XLE | +2.77% | -1.14% | -5.17% | -24.40% | 23.12 |
| GLD | +4.91% | +0.69% | +0.86% | +5.51% | 145.86 |
| VIX | +15.49% | -4.07% | -7.42% | +162.62% | 14.02 |
| WTI | +4.63% | -5.32% | -11.89% | -24.97% | 63.00 |
| CL_F | +4.72% | -5.46% | -11.83% | -25.17% | 63.05 |
| GC_F | +5.03% | +0.53% | +0.99% | +6.00% | 1549.20 |

### 2022-02-24 — Russia full-scale invasion of Ukraine

- Grade: **partial_strategy_proxy** (calendar tier: partial_strategy_proxy)
- Sources:
  - [Encyclopaedia Britannica](https://www.britannica.com/event/2022-Russian-invasion-of-Ukraine)
  - [UN news timeline](https://news.un.org/en/story/2022/02/1112592)

| Series | Pre-20d | Imm-5d | Post-20d | Post-60d | Level@event |
|--------|---------|--------|----------|----------|-------------|
| SPY | -4.55% | +0.39% | +1.71% | +0.36% | 403.00 |
| VTI | -4.19% | +0.52% | +1.41% | -0.24% | 203.29 |
| XLE | -2.60% | +6.23% | +6.53% | +10.84% | 28.68 |
| GLD | +4.90% | +2.53% | +1.62% | -0.05% | 177.14 |
| VIX | +30.58% | +9.89% | -12.04% | -10.88% | 30.32 |
| WTI | +0.54% | +11.74% | +2.24% | +7.36% | 92.77 |
| CL_F | +0.54% | +11.42% | +2.40% | +6.17% | 92.81 |
| GC_F | +6.56% | +0.90% | -0.89% | -1.66% | 1925.10 |

### 2023-10-07 — Hamas attack on Israel / Gaza war start

- Grade: **partial_strategy_proxy** (calendar tier: partial_strategy_proxy)
- Sources:
  - [Encyclopaedia Britannica](https://www.britannica.com/event/Israel-Hamas-War)
  - [CFR backgrounder](https://www.cfr.org/global-conflict-tracker/conflict/israeli-palestinian-conflict)

| Series | Pre-20d | Imm-5d | Post-20d | Post-60d | Level@event |
|--------|---------|--------|----------|----------|-------------|
| SPY | -3.18% | +0.32% | -5.00% | +5.20% | 415.28 |
| VTI | -3.31% | +0.21% | -5.22% | +5.44% | 205.48 |
| XLE | -6.93% | -1.05% | -4.47% | -7.54% | 39.23 |
| GLD | -5.40% | +0.25% | +7.71% | +8.69% | 169.70 |
| VIX | +24.64% | -5.71% | +20.17% | -26.72% | 17.45 |
| WTI | -9.45% | -3.52% | +0.17% | -19.69% | 82.83 |
| CL_F | -9.50% | -4.02% | -0.97% | -19.68% | 82.79 |
| GC_F | -5.24% | +1.07% | +7.52% | +9.79% | 1830.20 |

### 2024-04-13 — Iran direct strike on Israel (Apr 2024)

- Grade: **full_freeze_compatible** (calendar tier: full_freeze_compatible)
- Sources:
  - [Encyclopaedia Britannica](https://www.britannica.com/event/Israel-Hamas-War)
  - [Reuters timeline coverage](https://www.reuters.com/world/middle-east/)

| Series | Pre-20d | Imm-5d | Post-20d | Post-60d | Level@event |
|--------|---------|--------|----------|----------|-------------|
| SPY | -1.72% | -0.98% | +1.36% | +7.32% | 497.43 |
| VTI | -1.91% | -1.09% | +1.46% | +6.75% | 246.02 |
| XLE | +3.08% | -1.46% | -2.79% | -6.00% | 44.76 |
| GLD | +7.91% | -0.28% | -3.62% | -2.82% | 216.89 |
| VIX | +31.24% | -6.40% | -29.85% | -37.39% | 17.31 |
| WTI | +4.91% | -3.14% | -7.61% | -7.71% | 86.46 |
| CL_F | +4.53% | -3.14% | -8.55% | -8.09% | 85.66 |
| GC_F | +8.34% | +0.70% | -2.82% | -1.26% | 2356.20 |

## Interpretation rules

- Do not retune the bot from these windows.
- Do not treat headlines as alpha features.
- 2022 Russia/Ukraine row is macro/proxy context for the STRICT **price-path / realized-vol** stress notebook (VIX gates partially simulated in prod).
- Full freeze-profile claims only where grade = `full_freeze_compatible`.

**REPORT ONLY - freeze on; labels only; no trade signals; no promote; no live/paper default changes**
