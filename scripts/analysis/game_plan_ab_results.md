# Game Plan A/B Results

Generated: 2026-06-01 22:10

Variants:
- **baseline** — no game plan (full caps: SPY 45%, crypto 20%, NYSE 20%, 15% cash)
- **game_plan_gld_slv_cper** — full plan (yield gate + 10% metal + stress cash + 0.9 long scale)
- **yield_gate_only** — yield gate only (full caps, no metal, no stress cash)

### full_2017_2026 (2017-10-18 to 2026-05-22)

| Strategy | Return | Sharpe | Max DD | Gate days | Cash trims | Metal $ |
|----------|--------|--------|--------|-----------|------------|---------|
| baseline | +629.67% | 1.05 | -35.81% | 0 | 0 | $0 |
| game_plan_gld_slv_cper | +519.07% | 1.06 | -32.71% | 137 | 0 | $3,320 |
| yield_gate_only | +628.32% | 1.05 | -35.85% | 130 | 0 | $0 |

**game_plan_gld_slv_cper** vs baseline: return -110.60 pp, Sharpe +0.01, MaxDD +3.10 pp
**yield_gate_only** vs baseline: return -1.35 pp, Sharpe +0.00, MaxDD -0.04 pp

### fresh_2022 (2022-01-03 to 2022-12-30)

| Strategy | Return | Sharpe | Max DD | Gate days | Cash trims | Metal $ |
|----------|--------|--------|--------|-----------|------------|---------|
| baseline | -8.63% | -0.38 | -15.80% | 0 | 0 | $0 |
| game_plan_gld_slv_cper | -4.14% | -0.33 | -10.08% | 81 | 0 | $990 |
| yield_gate_only | -3.94% | -0.27 | -9.04% | 138 | 0 | $0 |

**game_plan_gld_slv_cper** vs baseline: return +4.49 pp, Sharpe +0.05, MaxDD +5.72 pp
**yield_gate_only** vs baseline: return +4.69 pp, Sharpe +0.11, MaxDD +6.76 pp

### recent_750d (2023-05-26 to 2026-05-22)

| Strategy | Return | Sharpe | Max DD | Gate days | Cash trims | Metal $ |
|----------|--------|--------|--------|-----------|------------|---------|
| baseline | +128.35% | 1.00 | -40.94% | 0 | 0 | $0 |
| game_plan_gld_slv_cper | +89.12% | 0.82 | -35.36% | 73 | 21 | $1,023 |
| yield_gate_only | +126.70% | 0.99 | -40.98% | 73 | 0 | $0 |

**game_plan_gld_slv_cper** vs baseline: return -39.23 pp, Sharpe -0.18, MaxDD +5.58 pp
**yield_gate_only** vs baseline: return -1.65 pp, Sharpe -0.01, MaxDD -0.04 pp

## Recommendation

Average Sharpe across windows: baseline=0.56, game_plan_gld_slv_cper=0.52, yield_gate_only=0.59

**Adopt yield-gate-only.** It keeps the macro SPY filter without metal sleeve drag, stress cash trims, or 0.9 long scaling. Set `GAME_PLAN_YIELD_GATE_ONLY=true` and `GAME_PLAN_ENABLED=false` (or disable metal/stress in live via the flag).
