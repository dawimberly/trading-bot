# STRICT MaxDD 25% risk ladder

Generated: 2026-07-27 04:13 UTC
Window: 2025-09-19 -> 2026-07-26 (365d requested)
Halt / budget: 25% MaxDD
Benchmark VTI B&H: +12.37%

**STRICT research only; MaxDD budget 25% is a halt/selection rail, not a live Profile A change**

| Leg | Return | Sharpe | MaxDD | In budget | Trades | NYSE | Notes |
|-----|--------|--------|-------|-----------|--------|------|-------|
| baseline_halt25 (OK) | +26.71% | 1.40 | -7.37% | YES | 2418 | 125 | current paper defaults; halt only raised |
| floor20_b1.8_r2.5 (OK) | +27.36% | 1.43 | -7.37% | YES | 2450 | 133 | milder core, higher boost/risk |
| floor10_b2.0_r3.5 ** (OK) | +27.82% | 1.44 | -7.32% | YES | 2431 | 126 | aggressive active sleeve |
| floor0_b2.2_r4.0 (OK) | +26.30% | 1.39 | -7.13% | YES | 2458 | 123 | zero-core allowed |
| floor0_b2.5_r5.0 (OK) | +26.91% | 1.41 | -7.33% | YES | 2527 | 163 | max ladder aggression |
| fixed20_b2.0_r3.5 (OK) | +16.05% | 0.98 | -8.21% | YES | 3877 | 724 | fixed 20% VTI core |

## Verdict

Winner under 25% MaxDD: floor10_b2.0_r3.5 return +27.82% Sharpe 1.44 MaxDD -7.32%. vs baseline_halt25: +1.11pp return. Winning leg still far from 25% DD - more aggression may still be available. STRICT research only; MaxDD budget 25% is a halt/selection rail, not a live Profile A change

## Suggested research knobs (winner)

- `MAX_DRAWDOWN_PCT=0.25`
- `PAPER_DYNAMIC_VTI=true`
- `DYNAMIC_VTI_PAPER_FLOOR=0.1`
- `DYNAMIC_VTI_PAPER_CEILING=0.65`
- `DYNAMIC_VTI_FLOOR_MIN=0.1`
- `DYNAMIC_VTI_ALLOW_ZERO=false`
- `PAPER_ACTIVE_SLEEVE_BOOST=2.0`
- `PAPER_RISK_PER_TRADE=0.035`
- `PAPER_VTI_CORE_PCT=0.1` (if fixed core)
- `--strict-pit --paper-aggressive --no-thinking`
