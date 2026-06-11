# Final Paper Bot — Comprehensive Backtest

Generated: 2026-06-11 01:25

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
Best Paper Bot (current)            +28.88%    1.63   -6.45%  +12.00pp     38    2.00%
Best Paper (live vol parity)        +21.46%    1.55   -9.45%   +4.58pp     38    2.00%
Legacy paper (pre-sleeve stack)     +14.84%    0.99  -11.65%   -2.04pp      0    2.00%
Live small-account sim              +13.58%    1.03  -10.07%   -3.30pp      0        —
VTI buy & hold                      +16.88%       —       —       —      —        —
----------------------------------------------------------------------------------------
Note: vol overlay PnL is synthetic in backtest; live/cloud logs only (see 'live vol parity' row).

### 1000d (2024-02-12 → 2026-06-10, 850 bars)

Config                               Return  Sharpe    MaxDD   vs VTI  Pairs  AvgRisk
----------------------------------------------------------------------------------------
Best Paper Bot (current)            +77.25%    1.19  -17.60%  +29.30pp     50    2.00%
Best Paper (live vol parity)        +59.96%    1.16  -15.10%  +12.01pp     44    2.00%
Legacy paper (pre-sleeve stack)     +40.69%    0.81  -19.38%   -7.26pp      0    2.00%
Live small-account sim              +44.80%    0.94  -17.83%   -3.15pp      0        —
VTI buy & hold                      +47.95%       —       —       —      —        —
----------------------------------------------------------------------------------------
Note: vol overlay PnL is synthetic in backtest; live/cloud logs only (see 'live vol parity' row).

### max (2023-08-01 → 2026-06-10, 1045 bars)

Config                               Return  Sharpe    MaxDD   vs VTI  Pairs  AvgRisk
----------------------------------------------------------------------------------------
Best Paper Bot (current)            +77.44%    1.03  -12.99%  +14.29pp     60    2.00%
Best Paper (live vol parity)        +56.60%    0.84  -11.71%   -6.55pp     54    2.00%
Legacy paper (pre-sleeve stack)     +65.52%    0.97  -17.69%   +2.37pp      0    2.00%
Live small-account sim              +58.75%    0.99  -18.08%   -4.40pp      0        —
VTI buy & hold                      +63.15%       —       —       —      —        —
----------------------------------------------------------------------------------------
Note: vol overlay PnL is synthetic in backtest; live/cloud logs only (see 'live vol parity' row).

## Verdict

- **Sharpe vs legacy paper:** current stack improves Sharpe by **+0.36** on average across windows (365d 1.63 vs 0.99, 1000d 1.19 vs 0.81, max 1.03 vs 0.97).
- **365d return vs VTI:** +28.88% vs VTI +16.88% (+12.00 pp).
- **365d risk:** Max DD -6.45% | Sortino 2.56 | avg risk 2.00%.
- **1000d return vs VTI:** +77.25% vs VTI +47.95% (+29.30 pp).
- **1000d risk:** Max DD -17.60% | Sortino 1.75 | avg risk 2.00%.
- **max return vs VTI:** +77.44% vs VTI +63.15% (+14.29 pp).
- **max risk:** Max DD -12.99% | Sortino 1.62 | avg risk 2.00%.

### Ready as default Best Paper Bot?

**Mutual-fund benchmark:** typical active funds ~0.4–0.7 Sharpe; Best Paper **365d Sharpe 1.63**.
**Locked as default** — `config.get_best_paper_bot_stack()` matches this profile. Beats legacy on 365d; monitor 1000d Max DD. Keep **social/SPY-exit OFF**.

**Laptop policy:** keep this profile; add only lightweight tweaks here. Heavy compute → `cloud_bot/` (see `README_CLOUD.md`).