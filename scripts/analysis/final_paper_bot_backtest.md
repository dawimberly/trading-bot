# Final Paper Bot — Comprehensive Backtest

Generated: 2026-06-11 20:39

## Stack tested (Best Paper Bot)

- Dynamic VTI (40–75%)
- Dynamic risk (1–3%)
- Statistical arbitrage (cointegration, both legs)
- Volatility overlay (VIX regime)
- Options income (covered calls)
- Advanced flags: overlap, adaptive chunk, co-fire
- Macro regime adaptor ON

## Comparisons

- **Legacy paper** — dynamic VTI + sleeve flags only (no stat arb, vol, dynamic risk, options, macro)
- **Live small-account sim** — 90% VTI, 1% risk, $100 start
- **VTI buy & hold** — passive benchmark

### 365d (2025-08-05 → 2026-06-10, 310 bars)

Config                               Return  Sharpe    MaxDD   vs VTI  Pairs  AvgRisk
----------------------------------------------------------------------------------------
Best Paper Bot (current)            +27.87%    1.57   -6.68%  +10.99pp     45    2.00%
Best Paper (live vol parity)        +19.99%    1.43   -9.98%   +3.11pp     34    2.00%
Legacy paper (pre-sleeve stack)     +14.84%    0.99  -11.65%   -2.04pp      0    2.00%
Live small-account sim              +13.58%    1.03  -10.07%   -3.30pp      0        —
VTI buy & hold                      +16.88%       —       —       —      —        —
----------------------------------------------------------------------------------------
Note: vol overlay PnL is synthetic in backtest; live/cloud logs only (see 'live vol parity' row).

## Verdict

- **Sharpe vs legacy paper:** current stack improves Sharpe by **+0.58** on average across windows (365d 1.57 vs 0.99).
- **365d return vs VTI:** +27.87% vs VTI +16.88% (+10.99 pp).
- **365d risk:** Max DD -6.68% | Sortino 2.41 | avg risk 2.00%.

### Ready as default Best Paper Bot?

**Mutual-fund benchmark:** typical active funds ~0.4–0.7 Sharpe; Best Paper **365d Sharpe 1.57**.
**Locked as default** — `config.get_best_paper_bot_stack()` matches this profile. Beats legacy on 365d; monitor 1000d Max DD. Keep **social/SPY-exit OFF**.

**Laptop policy:** keep this profile; add only lightweight tweaks here. Heavy compute → `cloud_bot/` (see `README_CLOUD.md`).