# Final Paper Bot — Comprehensive Backtest

Generated: 2026-06-12 20:52

## Stack tested (Best Paper Bot)

- Dynamic VTI (40–75%)
- Dynamic risk (1–3%)
- Statistical arbitrage (cointegration, both legs)
- Volatility overlay (VIX regime)
- Options income (covered calls)
- Advanced flags: overlap, adaptive chunk, co-fire
- Thinking engine: opt-in (Ollama, default off)
- Disabled: macro regime, risk parity, stat arb optimized, social, SPY MA exit

## Comparisons

- **Legacy paper** — dynamic VTI + sleeve flags only (no stat arb, vol, dynamic risk, options, macro)
- **Live small-account sim** — 90% VTI, 1% risk, $100 start
- **VTI buy & hold** — passive benchmark

### 365d (2025-08-07 → 2026-06-12, 310 bars)

Config                               Return  Sharpe    MaxDD   vs VTI  Pairs  AvgRisk
----------------------------------------------------------------------------------------
Best Paper Bot (current)            +64.74%    2.69   -7.46%  +45.93pp    210    2.00%
Best Paper (live vol parity)        +52.14%    2.73   -7.23%  +33.33pp    239    2.00%
Legacy paper (pre-sleeve stack)     +13.47%    0.97  -10.30%   -5.34pp      0    2.00%
Live small-account sim              +16.22%    1.26   -8.51%   -2.59pp      0        —
VTI buy & hold                      +18.81%       —       —       —      —        —
----------------------------------------------------------------------------------------
Note: vol overlay PnL is synthetic in backtest; live/cloud logs only (see 'live vol parity' row).

## Verdict

- **Sharpe vs legacy paper:** current stack improves Sharpe by **+1.72** on average across windows (365d 2.69 vs 0.97).
- **365d return vs VTI:** +64.74% vs VTI +18.81% (+45.93 pp).
- **365d risk:** Max DD -7.46% | Sortino 4.40 | avg risk 2.00%.

### Ready as default Best Paper Bot?

**Mutual-fund benchmark:** typical active funds ~0.4–0.7 Sharpe; Best Paper **365d Sharpe 2.69**.
**Locked as default** — `config.get_best_paper_bot_stack()` matches this profile. Beats legacy on 365d; monitor 1000d Max DD. Keep **social/SPY-exit OFF**.

**Laptop policy:** keep this profile; add only lightweight tweaks here. Heavy compute → `cloud_bot/` (see `README_CLOUD.md`).