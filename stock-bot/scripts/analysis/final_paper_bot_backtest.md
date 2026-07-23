# Final Paper Bot — Comprehensive Backtest

Generated: 2026-06-16 13:35

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

### 365d (2025-08-11 → 2026-06-16, 310 bars)

Config                         Ret    Sh    So      DD  Win   PF  Ord Hal   RSh   vsVTI
----------------------------------------------------------------------------------------
Best Paper Bot (current)    +50.5%  1.98  2.90   -8.3%  45% 1.48   36   0  1.77  +30.2p
Best Paper (live vol parit  +38.4%  1.88  2.27  -11.5%  41% 1.47   28   1  1.88  +18.1p
Legacy paper (pre-sleeve s  +37.4%  1.83  2.21  -11.7%  41% 1.46   29   1  1.81  +17.1p
Live small-account sim      +23.8%  1.38  2.13   -7.4%  43% 1.30    1   0  1.23   +3.5p
VTI buy & hold              +20.3%     -     -      -   -    -    -   -     -       -
----------------------------------------------------------------------------------------
Costs: equity slip 5bps + comm 0bps | crypto slip 10bps + fee-aware taker
Note: vol overlay PnL is synthetic in backtest; live/cloud logs only (see 'live vol parity' row).

## Verdict

- **Sharpe vs legacy paper:** current stack improves Sharpe by **+0.15** on average across windows (365d 1.98 vs 1.83).
- **365d return vs VTI:** +50.53% vs VTI +20.33% (+30.20 pp).
- **365d risk:** Max DD -8.30% | Sortino 2.90 | avg risk 1.62%.

### Ready as default Best Paper Bot?

**Mutual-fund benchmark:** typical active funds ~0.4–0.7 Sharpe; Best Paper **365d Sharpe 1.98**.
**Locked as default** — `config.get_best_paper_bot_stack()` matches this profile. Beats legacy on 365d; monitor 1000d Max DD. Keep **social/SPY-exit OFF**.

**Laptop policy:** keep this profile; add only lightweight tweaks here. Heavy compute → `cloud_bot/` (see `README_CLOUD.md`).