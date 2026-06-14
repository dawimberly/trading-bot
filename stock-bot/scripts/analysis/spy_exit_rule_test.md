# SPY MA-Break Exit Rule A/B

Generated: 2026-06-07 17:55

## Setup

- **Stack:** yield-gate-only + dynamic wisdom (live-like)
- **Variants:** `SPY_EXIT_ON_MA_BREAK` false vs true (sell full SPY when price < MA200)
- **Profiles:** active-only (0% VTI) vs 80/20 VTI core
- **Social sleeve:** off

Data: 945 bars | 2023-11-06 → 2026-06-07

## 365d (2025-06-08 → 2026-06-07)

### Active-only (0% VTI)

| Variant | Return | Sharpe | Sortino | Max DD | Calmar | Avg SPY | Entries | Exits | DD days % | DD cum ret |
|---------|-------:|-------:|--------:|-------:|-------:|--------:|--------:|------:|----------:|-----------:|
| SPY exit off | +25.25% | 1.10 | 1.39 | -12.43% | 2.03 | 42.1% | 63 | 0 | 83.2% | -20.84% |
| SPY exit on (MA break) | +8.87% | 0.49 | 0.60 | -12.13% | 0.73 | 34.0% | 91 | 21 | 86.3% | -21.69% |

Δ (exit − baseline): Return -16.38 pp | Sharpe -0.61 | Max DD +0.30 pp | DD cum ret -0.85 pp

### 80/20 VTI core (live)

| Variant | Return | Sharpe | Sortino | Max DD | Calmar | Avg SPY | Entries | Exits | DD days % | DD cum ret |
|---------|-------:|-------:|--------:|-------:|-------:|--------:|--------:|------:|----------:|-----------:|
| SPY exit off | +19.95% | 1.25 | 1.48 | -10.14% | 1.97 | 8.4% | 22 | 0 | 81.3% | -11.63% |
| SPY exit on (MA break) | +20.15% | 1.22 | 1.48 | -9.68% | 2.08 | 7.8% | 24 | 4 | 81.0% | -13.17% |

Δ (exit − baseline): Return +0.20 pp | Sharpe -0.03 | Max DD +0.46 pp | DD cum ret -1.54 pp

## 1000d (2024-05-24 → 2026-06-07)

### Active-only (0% VTI)

| Variant | Return | Sharpe | Sortino | Max DD | Calmar | Avg SPY | Entries | Exits | DD days % | DD cum ret |
|---------|-------:|-------:|--------:|-------:|-------:|--------:|--------:|------:|----------:|-----------:|
| SPY exit off | +36.74% | 0.82 | 0.92 | -16.89% | 2.18 | 46.1% | 23 | 0 | 85.8% | -18.13% |
| SPY exit on (MA break) | +26.03% | 0.71 | 0.80 | -10.83% | 2.40 | 33.8% | 117 | 9 | 89.5% | -16.48% |

Δ (exit − baseline): Return -10.71 pp | Sharpe -0.11 | Max DD +6.06 pp | DD cum ret +1.65 pp

### 80/20 VTI core (live)

| Variant | Return | Sharpe | Sortino | Max DD | Calmar | Avg SPY | Entries | Exits | DD days % | DD cum ret |
|---------|-------:|-------:|--------:|-------:|-------:|--------:|--------:|------:|----------:|-----------:|
| SPY exit off | +45.95% | 1.04 | 1.15 | -17.03% | 2.70 | 8.4% | 35 | 0 | 81.9% | -16.92% |
| SPY exit on (MA break) | +45.35% | 1.03 | 1.13 | -17.09% | 2.65 | 7.9% | 43 | 41 | 81.7% | -16.54% |

Δ (exit − baseline): Return -0.60 pp | Sharpe -0.01 | Max DD -0.06 pp | DD cum ret +0.38 pp

## max (2024-05-24 → 2026-06-07)

### Active-only (0% VTI)

| Variant | Return | Sharpe | Sortino | Max DD | Calmar | Avg SPY | Entries | Exits | DD days % | DD cum ret |
|---------|-------:|-------:|--------:|-------:|-------:|--------:|--------:|------:|----------:|-----------:|
| SPY exit off | +36.74% | 0.82 | 0.92 | -16.89% | 2.18 | 46.1% | 23 | 0 | 85.8% | -18.13% |
| SPY exit on (MA break) | +26.03% | 0.71 | 0.80 | -10.83% | 2.40 | 33.8% | 117 | 9 | 89.5% | -16.48% |

Δ (exit − baseline): Return -10.71 pp | Sharpe -0.11 | Max DD +6.06 pp | DD cum ret +1.65 pp

### 80/20 VTI core (live)

| Variant | Return | Sharpe | Sortino | Max DD | Calmar | Avg SPY | Entries | Exits | DD days % | DD cum ret |
|---------|-------:|-------:|--------:|-------:|-------:|--------:|--------:|------:|----------:|-----------:|
| SPY exit off | +45.95% | 1.04 | 1.15 | -17.03% | 2.70 | 8.4% | 35 | 0 | 81.9% | -16.92% |
| SPY exit on (MA break) | +45.35% | 1.03 | 1.13 | -17.09% | 2.65 | 7.9% | 43 | 41 | 81.7% | -16.54% |

Δ (exit − baseline): Return -0.60 pp | Sharpe -0.01 | Max DD -0.06 pp | DD cum ret +0.38 pp

## Verdict

### Active-only (0% VTI)
- **1000d:** Sharpe 0.82 → 0.71 | Max DD -16.89% → -10.83% | SPY exits 9 | DD-period cum ret -18.13% → -16.48%
- **365d:** Sharpe 1.10 → 0.49 | Max DD -12.43% → -12.13% | SPY exits 21 | DD-period cum ret -20.84% → -21.69%
- **max:** Sharpe 0.82 → 0.71 | Max DD -16.89% → -10.83% | SPY exits 9 | DD-period cum ret -18.13% → -16.48%
- Sharpe improved in **0/3** windows; Max DD improved in **3/3** windows.

### 80/20 VTI core (live)
- **1000d:** Sharpe 1.04 → 1.03 | Max DD -17.03% → -17.09% | SPY exits 41 | DD-period cum ret -16.92% → -16.54%
- **365d:** Sharpe 1.25 → 1.22 | Max DD -10.14% → -9.68% | SPY exits 4 | DD-period cum ret -11.63% → -13.17%
- **max:** Sharpe 1.04 → 1.03 | Max DD -17.03% → -17.09% | SPY exits 41 | DD-period cum ret -16.92% → -16.54%
- Sharpe improved in **0/3** windows; Max DD improved in **1/3** windows.

### Does SPY MA exit improve risk-adjusted returns?

**No on Sharpe; mixed on drawdown depth.**

| Profile | 365d Sharpe Δ | 1000d Sharpe Δ | Max DD help? |
|---------|--------------|----------------|--------------|
| Active-only | **−0.61** (1.10 → 0.49) | −0.11 | Yes (−17% → −11% on 1000d) |
| 80/20 VTI | −0.03 | −0.01 | Marginal (365d −10.1% → −9.7%) |

Exit rule **cuts average SPY exposure** (42% → 34% active-only) and triggers **21–117 re-entries** — whipsaw cost dominates on the full SPY sleeve. DD-period cumulative return is **not consistently better** (365d active-only worse).

### Where is the benefit bigger?

**Active-only** — exits matter only when SPY is a large sleeve (~42–46% avg exposure). Effect is **negative for Sharpe/return**, modest **positive for Max DD** on longer windows.

**80/20 VTI (live):** Only **4–41 exits**; tiny Sharpe/return/DD deltas. VTI ballast means MA-break exits on the ~8% SPY slice barely move the portfolio.

### Default `SPY_EXIT_ON_MA_BREAK=true`?

**No.** Keep `false` in the recommended stack.

- **Live 80/20:** Near-zero benefit; not worth the complexity.
- **Active-only:** Trades away return for slightly shallower troughs — poor Sharpe trade-off.

*Note: Prior backtests showed 0 SPY exits due to a `_holds_symbol` backtest bug (fixed for this run).*

Re-run: `python scripts/analysis/spy_exit_rule_test.py`