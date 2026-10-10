# Paper 33/67 vs 33/57/10 always-on metals (STRICT PIT)

Generated: 2026-09-07 23:23 UTC

**Research only. No `.env` write. No orders. No live. No restart.**

**Capital:** $100,000 paper-scale.  
**Baseline:** VTI 33% / NYSE 67%, metal OFF (`metal_sleeve_enabled=False`).  
**Treatment:** VTI 33% / NYSE 57% / metals 10% always-on GLD/SLV/CPER (50%/30%/20%) via thin drift overlay (not `backtester_metals` stress gate).  
Miners stay in NYSE; `METALS_AS_EQUITY=false`; no CEXY.

| Window | Dates | Base ret | Base Sharpe | Base MaxDD | Treat ret | Treat Sharpe | Treat MaxDD | d ret vs base | d MaxDD vs base | d treat vs VTI | NYSE trades base/treat | Base metals% | Treat metals% | Treat metal $ |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 90d | 2026-04-25 -> 2026-09-07 | +4.59% | 1.65 | -2.38% | +3.87% | 1.12 | -3.30% | -0.72pp | -0.92pp | +1.88pp | 0/0* | 0.00% | 9.11% | $9,466 |
| 180d | 2026-01-25 -> 2026-09-07 | +8.54% | 1.67 | -3.66% | +6.71% | 1.00 | -5.17% | -1.83pp | -1.51pp | -8.56pp | 0/0* | 0.00% | 10.04% | $10,714 |
| 365d | 2025-07-24 -> 2026-09-07 | +13.38% | 1.43 | -3.67% | +17.22% | 1.47 | -5.07% | +3.84pp | -1.40pp | -4.35pp | 0/0* | 0.00% | 10.44% | $12,238 |

\* `nyse_signals` counter reported 0 on both legs; baseline still beat VTI B&H on 90d/365d so the active book is not pure idle cash — treat the NYSE trade column as unreliable this run. Metals proof uses sleeve-path / overlay MTM.

## Verdict

On 365d, 10% metals lifted return without MaxDD help (MaxDD -3.67% -> -5.07%) — looks like metal beta when gold/copper ran, not survival; **HOLD / no promote**.

**HOLD / no promote. Do not copy into alpaca_paper_v2/.env.**

## Method notes

- Both legs: STRICT PIT, no thinking overlays, paper NYSE hygiene ON, SPY/crypto/stat-arb/shorts/social/felix/ORB/sector/vol-BO/options OFF, `PAPER_DYNAMIC_VTI=false`, fixed VTI 33%.
- Baseline metals_pct from `track_sleeve_path` end snap (expect ~0) — **proved ~0%**.
- Treatment: paper stack at 90% capital with VTI/NYSE = 33/57 of **total** (i.e. VTI=36.67% / NYSE=63.33% of the 90% book), then always-on metal overlay at 10% with +/-2pp drift rebalance — **end metals_pct ~9–10.4% (not 0)**.
- Overlay is research-only; not wired into `run_backtest` / game_plan.
- Script: `scripts/research/paper_3310_metals_ab.py`
