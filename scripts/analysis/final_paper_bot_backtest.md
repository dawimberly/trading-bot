# Final Paper Bot — Comprehensive Backtest

Generated: 2026-06-12 02:06

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

### 365d (2025-08-06 → 2026-06-12, 310 bars)

Config                               Return  Sharpe    MaxDD   vs VTI  Pairs  AvgRisk
----------------------------------------------------------------------------------------
Best Paper Bot (current)            +63.19%    2.46   -9.64%  +47.15pp    253    2.00%
Best Paper (live vol parity)        +53.71%    2.59   -8.30%  +37.67pp    262    2.00%
Legacy paper (pre-sleeve stack)     +15.47%    1.08  -11.44%   -0.57pp      0    2.00%
Live small-account sim              +12.04%    0.93  -10.33%   -4.00pp      0        —
VTI buy & hold                      +16.04%       —       —       —      —        —
----------------------------------------------------------------------------------------
Note: vol overlay PnL is synthetic in backtest; live/cloud logs only (see 'live vol parity' row).

## Verdict

- **Sharpe vs legacy paper:** current stack improves Sharpe by **+1.38** on average across windows (365d 2.46 vs 1.08).
- **365d return vs VTI:** +63.19% vs VTI +16.04% (+47.15 pp).
- **365d risk:** Max DD -9.64% | Sortino 3.81 | avg risk 2.00%.

### Ready as default Best Paper Bot?

**Mutual-fund benchmark:** typical active funds ~0.4–0.7 Sharpe; Best Paper **365d Sharpe 2.46**.
**Locked as default** — `config.get_best_paper_bot_stack()` matches this profile. Beats legacy on 365d; monitor 1000d Max DD. Keep **social/SPY-exit OFF**.

**Laptop policy:** keep this profile; add only lightweight tweaks here. Heavy compute → `cloud_bot/` (see `README_CLOUD.md`).