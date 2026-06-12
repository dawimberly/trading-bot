# Final Paper Bot — Comprehensive Backtest

Generated: 2026-06-12 00:23

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

### 365d (2025-08-05 → 2026-06-10, 310 bars)

Config                               Return  Sharpe    MaxDD   vs VTI  Pairs  AvgRisk
----------------------------------------------------------------------------------------
Best Paper Bot (current)            +29.29%    1.63   -6.45%  +12.41pp     38    2.00%
Best Paper (live vol parity)        +21.84%    1.54   -9.87%   +4.96pp     38    2.00%
Legacy paper (pre-sleeve stack)     +14.49%    0.96  -11.92%   -2.39pp      0    2.00%
Live small-account sim              +13.58%    1.03  -10.07%   -3.30pp      0        —
VTI buy & hold                      +16.88%       —       —       —      —        —
----------------------------------------------------------------------------------------
Note: vol overlay PnL is synthetic in backtest; live/cloud logs only (see 'live vol parity' row).

## Verdict

- **Sharpe vs legacy paper:** current stack improves Sharpe by **+0.67** on average across windows (365d 1.63 vs 0.96).
- **365d return vs VTI:** +29.29% vs VTI +16.88% (+12.41 pp).
- **365d risk:** Max DD -6.45% | Sortino 2.54 | avg risk 2.00%.

### Ready as default Best Paper Bot?

**Mutual-fund benchmark:** typical active funds ~0.4–0.7 Sharpe; Best Paper **365d Sharpe 1.63**.
**Locked as default** — `config.get_best_paper_bot_stack()` matches this profile. Beats legacy on 365d; monitor 1000d Max DD. Keep **social/SPY-exit OFF**.

**Laptop policy:** keep this profile; add only lightweight tweaks here. Heavy compute → `cloud_bot/` (see `README_CLOUD.md`).