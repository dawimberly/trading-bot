# Optimized System Summary

**Date:** 2026-06-02  
**Status:** Recommended live + backtest stack after session A/B grids.

## Executive summary

The integrated fund (`run_all.py`) runs three strategy sleeves on one Alpaca paper account with shared risk controls. Post-optimization, the stack favors **macro yield gate only** (no metal sleeve, no stress-cash trim, no 0.9 long-scale haircut), **full sleeve caps** (45% / 20% / 20% / 15% cash), **adaptive deployment sizing**, and **halt resume with breach liquidation**.

## Sleeve allocation (yield-gate-only)

| Sleeve | Cap | Strategy | Session |
|--------|-----|----------|---------|
| SPY | 45% | Price > MA200; exit on MA break | US equity |
| Crypto | 20% | Z-score pairs; **high vol only** | 24/7 |
| NYSE | 20% | Top momentum above MA50 (excludes SPY) | US equity |
| Cash buffer | 15% | Structural headroom | — |
| Metal | 0% | Disabled in yield-gate-only mode | — |

Caps are enforced per buy in `modules/alpaca_executor.py`. `config.fund_allocation_pct()` reports effective fractions at runtime.

## Game plan (yield-gate-only)

| Component | Live default | Notes |
|-----------|--------------|-------|
| `GAME_PLAN_YIELD_GATE_ONLY` | `true` | Blocks new SPY buys when 10Y (TNX) above MA50 and rising; TLT fallback |
| `YIELD_GATE_ENABLED` | `true` | Same gate in backtests |
| Metal sleeve | off | `metal_sleeve_enabled()` false — no GLD/SLV/CPER deploy |
| Stress cash | off | No trim-to-25% on macro stress |
| Long scale 0.9 | off | SPY/crypto/NYSE use **full** base caps |

**A/B (`game_plan_ab_test.py`, verified 2026-06-02, max daily history):**

| Window | Baseline | yield_gate_only | game_plan_gld_slv_cper |
|--------|----------|-----------------|------------------------|
| Full 2017–2026 (+259.75% / Sharpe 0.75) | — | +259.16% / 0.75 (−0.59 pp) | +257.01% / 0.80 |
| Fresh 2022 | −21.40% | **−17.75%** (+3.65 pp) | −13.16% (+8.24 pp) |
| Recent 750d | +44.90% | +44.85% (−0.05 pp) | +37.99% (−6.91 pp) |

**Live recommendation stays yield-gate-only:** near-baseline long-window return with full 45/20/20/15% caps, no metal deploy, no stress-cash trim, simpler live ops. Full `game_plan_gld_slv_cper` wins on averaged Sharpe in the grid but drags recent-window return and adds metal/stress complexity — use only if you explicitly want the metal sleeve.

`backtest_game_plan_live.py` (full metal blend reference, 2017–2023): baseline +97.78% vs `game_plan_gld_slv_cper` +89.55%; fresh 2022 baseline −21.40% vs full plan −13.16% (+8.24 pp).

Set `GAME_PLAN_YIELD_GATE_ONLY=false` and `GAME_PLAN_ENABLED=true` to restore full game plan (metal + stress cash + 0.9 scale).

## Risk controls

| Control | Default | Source |
|---------|---------|--------|
| Per-order risk | 2% of equity | `RISK_PER_TRADE` |
| Max per order | $10,000 | `MAX_NOTIONAL_PER_ORDER` |
| Stop-loss | 5% | `STOP_LOSS_PCT` |
| Max drawdown halt | 10% | `MAX_DRAWDOWN_PCT` |
| Halt resume | 8% DD | `HALT_RESUME_DRAWDOWN_PCT` |
| Liquidate on breach | true → 25% cash | `HALT_LIQUIDATE_ON_BREACH`, `HALT_TARGET_CASH_PCT` |
| Regime skip | Panic + Steady Bear | `market_context.py` |
| Crypto vol gate | High vol only | `CRYPTO_VOL_ONLY` |
| Crypto pair corr | ≥ 0.5 | `CRYPTO_MIN_CORRELATION` |
| Derived bear pause | **off** | `DERIVED_BEAR_PAUSE_ENABLED=false` |

**A/B (`risk_layer_ab.py`):** Halt resume + liquidate improved 500-day Sharpe (1.24 → 1.77) and max DD (~6 pp). Derived bear pause blocked too many days on daily bars — leave off.

## Deployment sizing

| Flag | Default | Effect |
|------|---------|--------|
| `ADAPTIVE_CHUNK_ENABLED` | `true` | Larger chunks when sleeve room > 5× base risk |
| `ADAPTIVE_CHUNK_MAX_PCT` | `0.05` | Cap per adaptive chunk |
| `COFIRE_BUDGET_ENABLED` | `true` | Shared budget when SPY + NYSE fire same bar |
| `COFIRE_BUDGET_PCT` | `0.06` | Co-fire notional cap |

**A/B (`deployment_efficiency_ab.py`):** Adaptive-only best on 2000d (+145 pp return vs fixed 2% chunks). Both flags on by default in `config.py`.

## SPY / NYSE refinements

| Flag | Default | A/B note |
|------|---------|----------|
| `SPY_EXIT_ON_MA_BREAK` | `true` | Keeps SPY from riding through MA200 breaks |
| `SPY_LADDER_SIZING_ENABLED` | `false` | **Do not enable by default** — ladder helped 2000d Sharpe but hurt 500d |
| `NYSE_OVERLAP_FILTER_ENABLED` | `true` | Skip NYSE pick when corr to SPY > 0.80 (config default) |
| `NYSE_SPY_CORR_MAX` | `0.80` | Grid found corr filter costly on recent_750d — optional off |
| `NYSE_BETA_SCALING_ENABLED` | **`true`** | **Recommended default** — best cross-window single toggle in refinements grid |

## Wisdom & sentiment

- `WISDOM_MODE=arbitrage` (default)
- `SENTIMENT_SOURCE=price` (free, matches backtests)
- `REGIME_SENTIMENT_THRESHOLD=0.5` (legacy; derived bear off)

## Backtest parity

| Script | Purpose |
|--------|---------|
| `backtester.py` | Integrated fund; `--max`, `--no-halt`, `--days N` |
| `backtester_metals.py` | Game plan variants incl. `yield_gate_only` |
| `backtester_macro_hedge.py` | Yield gate / GLD / stress cash grid |
| `backtester_wisdom.py` | Wisdom modes + game plan |
| `scripts/research/backtest_game_plan_live.py` | Live blend vs baseline CSVs |

Startup prints `config.print_recommended_stack_flags()` from preflight and backtesters:

```
--- Recommended stack flags ---
  game_plan:              yield-gate-only
  yield_gate:             True
  nyse_overlap_filter:    True (corr max 0.8)
  nyse_beta_scaling:      True
  spy_exit_on_ma_break:   True
  adaptive_chunk:         True
  cofire_budget:          True
  halt_resume_dd:         8% | liquidate_on_breach: True
  derived_bear_pause:     False
  sleeves: SPY 45% | crypto 20% | NYSE 20% | metal 0% | cash 15%
```

## Live vs backtest verification (2026-06-02)

`python scripts/analysis/live_vs_backtest_snapshot.py --refresh-eval`:

| Metric | Value |
|--------|-------|
| Live window | 2026-05-25 → 2026-05-30 (6 daily samples) |
| Live return | +0.18% |
| Active mode sim (governor) | +0.11% |
| Live − sim | +0.07 pp |
| VTI benchmark | +1.57% |
| Trade signals in window | 248 |

Short live window — use for drift tracking, not strategy validation. Re-run after each paper week.

## Analysis artifacts (repo)

| Report | Topic |
|--------|-------|
| `game_plan_ab_results.md` | Yield-gate-only vs full game plan |
| `risk_layer_results.md` | Halt resume + liquidate |
| `deployment_efficiency_results.md` | Adaptive chunk / co-fire |
| `refinements_grid_results.md` | SPY exit, ladder, NYSE beta |
| `nyse_anti_overlap_results.md` | Corr filter vs baseline |
| `ab_test_results_live_stack.md` | Sleeve-aware executor P0 |

Re-run grids: `python scripts/analysis/game_plan_ab_test.py`, `risk_layer_ab.py`, `deployment_efficiency_ab.py`, `refinements_grid_ab.py`, `nyse_anti_overlap_ab.py`.

## Recommended `.env` (minimal)

```env
GAME_PLAN_ENABLED=true
GAME_PLAN_YIELD_GATE_ONLY=true
YIELD_GATE_ENABLED=true
ADAPTIVE_CHUNK_ENABLED=true
COFIRE_BUDGET_ENABLED=true
SPY_EXIT_ON_MA_BREAK=true
HALT_RESUME_DRAWDOWN_PCT=0.08
HALT_LIQUIDATE_ON_BREACH=true
DERIVED_BEAR_PAUSE_ENABLED=false
NYSE_BETA_SCALING_ENABLED=true
WISDOM_MODE=arbitrage
SENTIMENT_SOURCE=price
```

See `.env.example` for the full commented list.
