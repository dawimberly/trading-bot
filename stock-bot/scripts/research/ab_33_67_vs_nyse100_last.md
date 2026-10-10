# A/B/C — paper v2 33/67 vs 100% NYSE vs 100% VTI

Research only. No `.env` change, no restart, no orders.

**Window:** 2025-08-11 → 2026-09-04 (390 daily bars, 346 NYSE names).
**Source:** adapted from `scripts/analysis/one_r_hit_backtest.py` (`scripts/research/one_r_hit_backtest.py` does not exist).
**Costs / slippage:** **off** (same as the source script — no model added).
**NYSE engine:** MA70 rank, max 15 names, clip = min(book×sleeve/15, 10%/name, cash). Stop = max(2× daily ATR, 1% of entry). Max hold 30 daily bars.
**Exits in this run:** 50% partial at 1R on daily close, then trail on the remainder (arm `TRAIL_ARM_PCT`=50% from entry, pullback `TRAIL_PULLBACK_PCT`=35% of peak). No conviction/regime scale.
**Not added:** RHYME, scanners, RSI/hygiene, thinking, extra sleeves.
**A:** 33% VTI shares at window open, never rebalanced (drift). 67% NYSE path. VTI marked daily.
**B:** 100% NYSE path (sleeve=1.0), same engine/exits.
**C:** 100% VTI buy-and-hold from the same open.

| Arm | Start | End | Return | CAGR | Sharpe | Max DD | Trades | Win% | 1R-touch% | Avg hold | stop | time | 1R-partial | trail | eod | vs C (pp) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A 33/67 | 100,000 | 165,444.22 | 65.44% | 60.44% | 2.13 | 10.23% | 342 | 52.3% | 57.6% | 17.1 | 189 | 138 | 197 | 0 | 15 | +43.36 |
| B 100% NYSE | 100,000 | 192,753.07 | 92.75% | 85.18% | 2.15 | 13.81% | 342 | 52.3% | 57.6% | 17.1 | 189 | 138 | 197 | 0 | 15 | +70.67 |
| C 100% VTI | 100,000 | 122,084.05 | 22.08% | 20.61% | 1.27 | 8.92% | 1 | 100.0% | n/a | 389.0 | 0 | 0 | 0 | 0 | 1 | +0.00 |

## What won

**B 100% NYSE** won on total return this window (A 65.44% / Sharpe 2.13 / DD 10.23%; B 92.75% / Sharpe 2.15 / DD 13.81%; C 22.08% / Sharpe 1.27 / DD 8.92%). B is the profit-max toolkit layout (all capital in the same NYSE engine). A is that engine with 33% VTI ballast marked. Trail exits on B = 0 (arm is +50% from entry, so remainder usually dies on stop/time, not trail).

## Why this is NOT a promote

Daily closes only; no 5-minute path, no commissions/slippage, no RHYME pause/sizing, no RSI/hygiene/same-day block, no scanners. Partial+trail is a close-to-close proxy of the paper exit knobs, not the live fill engine. One ~390-bar window is not a promote. B’s extra return is extra NYSE risk (deeper DD than A/C). Do not copy B to paper/live.

## Still missing vs live paper v2

Live paper v2 still has RHYME primary (paper B sizes ×0.3; live hard-pauses B/E), GARCH, conviction 0.4–2.0×, RVOL/ORB-scanner/catalyst/MTF/insider rank boosts, RSI 70/72, open cooldown, max 2 adds, same-day rebuy block, correlation/concentration guards, daily banking, smart-stop tighten @ −5%/hard −10%, 1R **intraday** (this test uses daily close), and yield-gate override. VTI here is buy-and-hold shares; paper does not trade VTI daily. Forward 33/67 from the Sep 2 $100k restock is +0.4% in 3 sessions — not this sim.

10%/name cap did not bind (67%/15 ≈ 4.47% of book; 100%/15 ≈ 6.67%). Trail arms only after a **+50% price gain from entry**, so trail exits on a 30-bar MA70 clip are expected to be rare.

