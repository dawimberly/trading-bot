# Optimized System Summary

**Date:** 2026-06-10  
**Status:** Two deployment profiles — live conservative (`current_dynamic`) vs paper research (`paper_aggressive`). Final paper defaults: dynamic VTI + overlap/chunk/co-fire **on**; macro adaptor, social sleeve, SPY MA exit **off**.

## Two profiles (source of truth)

| | **Profile A: Live** (`current_dynamic`) | **Profile B: Paper research** (`paper_aggressive`) |
|---|-------------------------------------------|-----------------------------------------------------|
| **Use when** | Live ~$100 account, default `run_all.py`, preflight live | Paper book, `run_paper_bot.py`, `backtester.py --paper-aggressive`, portal paper user |
| **VTI core** | 90% when equity &lt; $500; 80% when ≥ $500 | **Dynamic 40–75%** (`PAPER_DYNAMIC_VTI=true`); static 20% fallback |
| **Active sleeves** | ~10% (small) / ~20% (large) of equity | ~31% avg active with dynamic VTI; boost 1.40× on base caps |
| **Risk / order cap** | 1% / $10 (small) or 2% / scaled (large) | Full paper book sizing |
| **Game plan** | Yield-gate-only | Yield-gate-only |
| **NYSE overlap filter** | **off** (opt-in) | **on** (`PAPER_NYSE_OVERLAP_FILTER_ENABLED=true`) |
| **NYSE beta scaling** | **off** (opt-in) | **on** when `PAPER_CHASE_MODE` + `PAPER_CHASE_EXTRA` |
| **Adaptive chunk / co-fire** | **off** (opt-in) | **on** (paper defaults) |
| **SPY MA exit** | **off** (opt-in) | **off** (`PAPER_SPY_EXIT_ON_MA_BREAK=false`) |
| **Macro regime adaptor** | **off** | **off** (`PAPER_MACRO_REGIME_ADAPTOR_ENABLED=false`) |
| **Crypto vol gate** | High vol only | All vol (`PAPER_CRYPTO_VOL_ONLY=false`) |
| **Social / Felix sleeve** | **off** by default | **off** (`PAPER_SOCIAL_SLEEVE_ENABLED=false`; Felix sync optional via chase extras) |
| **Halt** | 10% DD; resume 8%; liquidate on breach | same |

Live defaults stay conservative in `config.py`. Paper chase enables aggressive layers via `configure_paper_chase()` / `init_paper_chase_if_enabled()` — no live behavior change unless you opt in via `.env`.

## Sleeve allocation (base caps)

| Sleeve | Base cap | Strategy | Session |
|--------|----------|----------|---------|
| SPY | 45% | Price > MA200 | US equity |
| Crypto | 20% | Z-score pairs; vol-gated on live | 24/7 |
| NYSE | 20% | Top momentum above MA50 (excludes SPY) | US equity |
| Cash buffer | 15% | Structural headroom | — |
| Metal | 0% | Disabled in yield-gate-only mode | — |

Effective caps = base × `active_sleeve_scale()` (VTI core + paper boost). Enforced per buy in `modules/alpaca_executor.py`.

## Game plan (yield-gate-only — both profiles)

| Component | Default | Notes |
|-----------|---------|-------|
| `GAME_PLAN_YIELD_GATE_ONLY` | `true` | Blocks new SPY buys when 10Y (TNX) above MA50 and rising |
| `YIELD_GATE_ENABLED` | `true` | Same gate in backtests |
| Metal sleeve | off | No GLD/SLV/CPER deploy |
| Stress cash | off | No trim-to-25% on macro stress |
| Long scale 0.9 | off | Full 45/20/20/15 base caps |

**A/B (`game_plan_ab_test.py`, verified 2026-06-02):** yield-gate-only ≈ baseline long-window return with simpler ops. Full `game_plan_gld_slv_cper` helps fresh 2022 MaxDD but drags recent windows — opt-in only.

## Risk controls (shared)

| Control | Live small (&lt; $500) | Live large | Paper research |
|---------|------------------------|------------|----------------|
| Per-order risk | 1% | 2% | 2% (paper book equity) |
| Max per order | $10 | scaled | scaled |
| Stop-loss | 5% | 5% | 5% |
| Max drawdown halt | 10% | 10% | 10% |
| Halt resume | 8% DD | 8% DD | 8% DD |
| Liquidate on breach | true → 25% cash | same | same |
| Derived bear pause | **off** | **off** | **off** |

**A/B (`risk_layer_ab.py`):** Halt resume + liquidate improved 500-day Sharpe; derived bear pause blocked too many days — leave off.

## Deployment sizing (profile-specific)

| Flag | Profile A (live default) | Profile B (paper chase) | Grid note |
|------|--------------------------|-------------------------|-----------|
| `ADAPTIVE_CHUNK_ENABLED` | `false` | `true` (`PAPER_ADAPTIVE_CHUNK_ENABLED`) | Best on long window in `deployment_efficiency_ab.py` |
| `COFIRE_BUDGET_ENABLED` | `false` | `true` (`PAPER_COFIRE_BUDGET_ENABLED`) | Co-fire when SPY+NYSE same bar |
| `NYSE_BETA_SCALING_ENABLED` | `false` | `true` (via paper chase extras) | Recommended for research grids |
| `NYSE_OVERLAP_FILTER_ENABLED` | `false` | `true` (`PAPER_NYSE_OVERLAP_FILTER_ENABLED`) | Paper default on |
| `SPY_EXIT_ON_MA_BREAK` | `false` | `false` (`PAPER_SPY_EXIT_ON_MA_BREAK=false`) | Opt-in only |
| `PAPER_MACRO_REGIME_ADAPTOR_ENABLED` | n/a | `false` | Opt-in only |
| `PAPER_SOCIAL_SLEEVE_ENABLED` | n/a | `false` | Opt-in only |
| `PAPER_DYNAMIC_VTI` | n/a | `true` | Dynamic 40–75% vs fixed 20% |

## Wisdom & sentiment (both profiles)

- `WISDOM_MODE=dynamic` (default)
- `SENTIMENT_SOURCE=price` (free, matches backtests)
- Paper: `PAPER_WISDOM_SIZING_FLOOR=1.0` (no defensive shrink)

## Preflight / startup output

`config.print_recommended_stack_flags()` dispatches by profile:

- **Live:** `print_live_stack_flags()` → `--- current_dynamic live stack (Profile A) ---`
- **Paper chase:** `print_paper_research_stack_flags()` → `--- paper_aggressive research stack (Profile B) ---`

Called from `scripts/account/preflight.py`, `run_all.py` (after `init_paper_chase_if_enabled()`), and `backtester.py` (with `profile=` arg).

### Profile A example (live ~$100)

```
--- current_dynamic live stack (Profile A) ---
  game_plan:              yield-gate-only
  yield_gate:             True
  nyse_overlap_filter:    False (corr max 0.8)
  nyse_beta_scaling:      False
  spy_exit_on_ma_break:   False
  adaptive_chunk:         False
  cofire_budget:          False
  halt_resume_dd:         8% | liquidate_on_breach: True
  derived_bear_pause:     False
  wisdom_mode:            dynamic
  small_account:        ON (<$500) | risk 1% | max order $10
  vti_core:             90% VTI passive | active 10%
  sleeves: SPY 5% | crypto 2% | NYSE 2% | metal 0% | cash 1%
```

### Profile B example (paper chase)

```
--- paper_aggressive research stack (Profile B) ---
  paper_chase_mode:       ON (PAPER_CHASE_MODE)
  game_plan:              yield-gate-only
  yield_gate:             True
  nyse_overlap_filter:    True
  nyse_beta_scaling:      True (recommended ON for research grids)
  spy_exit_on_ma_break:   False
  adaptive_chunk:         True
  cofire_budget:          True
  macro_regime_adaptor:   False
  social_sleeve:          off
  vti_core:             dynamic 40-75% VTI | active boost 1.40x
  crypto_vol_only:      False
  wisdom_sizing_floor:  1.0
  sleeves: SPY 18% | crypto 8% | NYSE 8% | metal 0% | cash 1%
```

## Final backtest results (2026-06-10)

Run: `python backtester.py --days N --paper-aggressive` (paper Profile B).  
VTI buy & hold benchmark shown in backtest output for context.

### Paper aggressive (`--paper-aggressive`, dynamic VTI + sleeve flags)

| Window | Return | Sharpe | Max DD | Notes |
|--------|--------|--------|--------|-------|
| **365d** (2025-08-05 → 2026-06-10) | **+5.48%** | **0.41** | **-14.74%** | Avg active 31%; avg VTI 69% |
| **1000d** (2024-02-12 → 2026-06-10) | **+25.84%** | **0.58** | **-19.06%** | vs VTI B&H +47.95% |

### Dynamic VTI A/B (365d, paper aggressive)

| Config | Return | Sharpe | Max DD |
|--------|--------|--------|--------|
| Fixed 20% VTI | -0.14% | 0.09 | -23.90% |
| **Dynamic 40–75% VTI** | **+5.48%** | **0.41** | **-14.74%** |

Run: `python backtester.py --days 365 --compare-dynamic-vti`

### Live baseline (Profile A, default backtester — no flags)

| Window | Return | Sharpe | Max DD | VTI core |
|--------|--------|--------|--------|----------|
| 365d | -11.48% | -0.35 | -26.14% | 80% |
| 1000d | +50.69% | 0.89 | -18.0% | 80% |

Use `--small-account` for live ~$100 profile (90% VTI, $10 max order).

## Backtest parity

| Script | Profile |
|--------|---------|
| `backtester.py` | Profile A default; `--small-account` for live ~$100; `--paper-aggressive` for Profile B |
| `backtester_metals.py` | Game plan variants |
| `scripts/research/run_paper_piece.py` | Isolated paper book pieces |

## Recommended `.env`

**Profile A — live (minimal; defaults in `config.py`):**

```env
WISDOM_MODE=dynamic
GAME_PLAN_ENABLED=true
GAME_PLAN_YIELD_GATE_ONLY=true
YIELD_GATE_ENABLED=true
VTI_CORE_ENABLED=true
PAPER_TRADING=false
ALLOW_LIVE_TRADING=yes
# Opt-in only after grids:
# NYSE_BETA_SCALING_ENABLED=true
# ADAPTIVE_CHUNK_ENABLED=true
# COFIRE_BUDGET_ENABLED=true
# SPY_EXIT_ON_MA_BREAK=true
# NYSE_OVERLAP_FILTER_ENABLED=true
```

**Profile B — paper research (add to paper bot / portal paper user `.env`):**

```env
PAPER_TRADING=true
PAPER_CHASE_MODE=1
PAPER_AGGRESSIVE=true
PAPER_DYNAMIC_VTI=true
DYNAMIC_VTI_PAPER_FLOOR=0.40
PAPER_NYSE_OVERLAP_FILTER_ENABLED=true
PAPER_ADAPTIVE_CHUNK_ENABLED=true
PAPER_COFIRE_BUDGET_ENABLED=true
PAPER_SPY_EXIT_ON_MA_BREAK=false
PAPER_MACRO_REGIME_ADAPTOR_ENABLED=false
PAPER_SOCIAL_SLEEVE_ENABLED=false
PAPER_ACTIVE_SLEEVE_BOOST=1.40
PAPER_CRYPTO_VOL_ONLY=false
PAPER_CHASE_EXTRA=true
# Optional isolated ~$98k book:
# PAPER_APCA_API_KEY_ID=...
# PAPER_APCA_API_SECRET_KEY=...
# PAPER_CHASE_USE_RESEARCH_KEYS=yes
```

See `.env.example` for the full commented list.

## Analysis artifacts

| Report | Topic |
|--------|-------|
| `game_plan_ab_results.md` | Yield-gate-only vs full game plan |
| `risk_layer_results.md` | Halt resume + liquidate |
| `deployment_efficiency_ab.py` | Adaptive chunk / co-fire |
| `refinements_grid_results.md` | SPY exit, ladder, NYSE beta |
| `nyse_anti_overlap_results.md` | Corr filter vs baseline |

Re-run: `python scripts/analysis/game_plan_ab_test.py`, `risk_layer_ab.py`, `deployment_efficiency_ab.py`, `refinements_grid_ab.py`, `nyse_anti_overlap_ab.py`.

## Quick status

```powershell
python status.py
```

Shows live + paper equity, regime (from heartbeat), and key flags for both profiles.
