# Final Paper Bot — Comprehensive Backtest

Generated: 2026-06-13 15:47

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

### 365d (2025-03-20 → 2026-06-12, 310 bars)

Config                         Ret    Sh    So      DD  Win   PF  Ord Hal   RSh   vsVTI
----------------------------------------------------------------------------------------
Best Paper Bot (current)    +78.2%  1.84  3.00  -10.8%  53% 1.41  385   2  1.67  +44.7p
Best Paper (live vol parit  +71.6%  1.85  2.78  -13.0%  56% 1.41  386   2  1.81  +38.2p
Legacy paper (pre-sleeve s  +26.2%  1.19  1.57  -13.0%  56% 1.25   84   2  1.47   -7.2p
Live small-account sim      +29.6%  1.35  1.73  -12.9%  57% 1.31    4   2  1.69   -3.9p
VTI buy & hold              +33.5%     -     -      -   -    -    -   -     -       -
----------------------------------------------------------------------------------------
Costs: equity slip 5bps + comm 0bps | crypto slip 10bps + fee-aware taker
Note: vol overlay PnL is synthetic in backtest; live/cloud logs only (see 'live vol parity' row).

## Verdict

- **Sharpe vs legacy paper:** current stack improves Sharpe by **+0.65** on average across windows (365d 1.84 vs 1.19).
- **365d return vs VTI:** +78.16% vs VTI +33.46% (+44.70 pp).
- **365d risk:** Max DD -10.76% | Sortino 3.00 | avg risk 2.00%.

### Ready as default Best Paper Bot?

**Mutual-fund benchmark:** typical active funds ~0.4–0.7 Sharpe; Best Paper **365d Sharpe 1.84**.
**Locked as default** — `config.get_best_paper_bot_stack()` matches this profile. Beats legacy on 365d; monitor 1000d Max DD. Keep **social/SPY-exit OFF**.

**Laptop policy:** keep this profile; add only lightweight tweaks here. Heavy compute → `cloud_bot/` (see `README_CLOUD.md`).