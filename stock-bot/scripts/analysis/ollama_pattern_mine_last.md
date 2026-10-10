# Local AI pattern mine (Ollama qwen2.5-coder:14b)

Generated: 2026-07-27 (ET)
Source: `ollama_pattern_mine_last.json` (pass 2, mechanism-constrained)
Evidence: STRICT windows / conviction / MaxDD ladder / MC sleeve attribution

**Research only — do not change live Profile A from these.**

## Caveat

Pass 1 restated window stats and **inverted** conviction (claimed it wins; it loses). Pass 2 is usable after mapping onto **real config knobs** below.

## Ranked mechanisms (AI idea → concrete STRICT A/B)

| Pri | AI theme | Why it fits our facts | Concrete STRICT test | Kill if |
|-----|----------|----------------------|----------------------|---------|
| 1 | **Hold-time / exits** (P1) | Edge decays in later window half; sizing doesn't move DD → exits/turnover may be the real bottleneck | A/B `PAPER_POSITION_MAX_HOLD_BARS` 20 vs 30 vs 45; and trail `PAPER_TRAILING_STOP_ARM_PCT` / `TRAIL_PCT` mild widen vs tighten — 90d+365d STRICT | Sharpe or MaxDD worse than baseline on both windows |
| 2 | **SPY vs VTI dual-core bleed** (from MC, stronger than AI P5) | MC wipeouts: high VTI at trough, SPY sleeve marked bleed; aggression doesn't raise path MaxDD | STRICT legs: SPY satellite OFF / smaller SPY sleeve vs baseline (keep Dynamic VTI) | Return/Sharpe drop >1pp with no DD improvement |
| 3 | **Yield gate / entry throttle** (P4) | Fact C: risk knobs don't bind → gates may clip deployment | STRICT: yield gate OFF vs ON; optional vol-scaled threshold — 90d then 365d | More trades but worse Sharpe/DD |
| 4 | **Correlation guard tightness** (P3-ish) | Breadth wins; crowding may hurt without needing top-N | A/B `CORR_GUARD_CEILING` 0.75 vs 0.85 vs 0.95 — STRICT 90d | No lift or kills breadth edge |
| 5 | **Rebalance cadence** (P5) | Path dependence in MC; drift % is a quiet lever | A/B `PAPER_VTI_REBALANCE_DRIFT_PCT` 0.5% vs 1% vs 2% | Flat vs baseline |
| 6 | **Rank half-life / stale names** (operator+AI overlap) | Conviction fails but breadth works → ranks decay; holding stale top ranks hurts | Keep 25 names; add max age / re-rank every N bars (no top-N shrink) | No improvement vs baseline |

## Anti-patterns (skip)

- Overlays, kill VTI, conviction top-N, raise MaxDD/risk budget (already tested)
- Optimizing from live 2-week Sharpe
- Treating MC −30..−52% wipeouts as the single-path STRICT reality (−7% DD)

## Best next experiment

**Hold-time / trailing-stop ladder on STRICT 90d** (maps to Ollama `best_next: P1`), then if any leg wins, confirm on 365d. Parallel cheap diagnostic: **SPY sleeve shrink/off** under STRICT (MC bleed).

## Files

- `scripts/analysis/ollama_pattern_mine_last.json`
- `scripts/analysis/ollama_pattern_mine_v2.raw.txt`
- Evidence pack: `scripts/analysis/_ollama_pattern_evidence.json`
