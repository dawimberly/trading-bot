# Game Plan A/B Results

Generated: 2026-06-02 09:44

Variants:
- **baseline** — no game plan (full caps: SPY 45%, crypto 20%, NYSE 20%, 15% cash)
- **game_plan_gld_slv_cper** — full plan (yield gate + 10% metal + stress cash + 0.9 long scale)
- **yield_gate_only** — yield gate only (full caps, no metal, no stress cash)

### full_2017_2026 (2017-07-20 to 2026-06-02)

| Strategy | Return | Sharpe | Max DD | Gate days | Cash trims | Metal $ |
|----------|--------|--------|--------|-----------|------------|---------|
| baseline | +259.75% | 0.75 | -25.10% | 0 | 0 | $0 |
| game_plan_gld_slv_cper | +257.01% | 0.80 | -24.83% | 205 | 0 | $3,383 |
| yield_gate_only | +259.16% | 0.75 | -25.08% | 205 | 0 | $0 |

**game_plan_gld_slv_cper** vs baseline: return -2.74 pp, Sharpe +0.05, MaxDD +0.27 pp
**yield_gate_only** vs baseline: return -0.59 pp, Sharpe +0.00, MaxDD +0.02 pp

### fresh_2022 (2022-01-01 to 2022-12-31)

| Strategy | Return | Sharpe | Max DD | Gate days | Cash trims | Metal $ |
|----------|--------|--------|--------|-----------|------------|---------|
| baseline | -21.40% | -0.78 | -26.96% | 0 | 0 | $0 |
| game_plan_gld_slv_cper | -13.16% | -0.71 | -19.25% | 35 | 0 | $975 |
| yield_gate_only | -17.75% | -0.81 | -22.94% | 35 | 0 | $0 |

**game_plan_gld_slv_cper** vs baseline: return +8.24 pp, Sharpe +0.07, MaxDD +7.71 pp
**yield_gate_only** vs baseline: return +3.65 pp, Sharpe -0.03, MaxDD +4.02 pp

### recent_750d (2024-05-14 to 2026-06-02)

| Strategy | Return | Sharpe | Max DD | Gate days | Cash trims | Metal $ |
|----------|--------|--------|--------|-----------|------------|---------|
| baseline | +44.90% | 1.03 | -18.43% | 0 | 0 | $0 |
| game_plan_gld_slv_cper | +37.99% | 1.03 | -16.28% | 192 | 12 | $1,253 |
| yield_gate_only | +44.85% | 1.03 | -18.43% | 82 | 0 | $0 |

**game_plan_gld_slv_cper** vs baseline: return -6.91 pp, Sharpe +0.00, MaxDD +2.15 pp
**yield_gate_only** vs baseline: return -0.05 pp, Sharpe +0.00, MaxDD +0.00 pp

## Recommendation

Average Sharpe across windows: baseline=0.33, game_plan_gld_slv_cper=0.37, yield_gate_only=0.32

**Keep full game plan** (metals + stress cash + yield gate + 0.9 scale). The simplified yield-gate-only variant did not improve risk-adjusted returns.
