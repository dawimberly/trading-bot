# Deployment Efficiency A/B

Config: `RISK_PER_TRADE=0.02`, `ADAPTIVE_CHUNK_MAX_PCT=0.05`, `COFIRE_BUDGET_PCT=0.06`, `MAX_NOTIONAL_PER_ORDER=10000.0`.

SPY fill metric: cycles/trades/hours from first SPY buy signal to 90% of SPY sleeve cap.

## Window: 2000d

| Variant | Return | Sharpe | Max DD | SPY 90% cycles | SPY 90% trades | SPY 90% hrs | Orders |
|---------|--------|--------|--------|----------------|----------------|-------------|--------|
| baseline | +741.32% | 1.16 | -42.10% | 18 | 19 | 18.0 | 43 |
| adaptive_only | +886.48% | 1.18 | -43.00% | 7 | 8 | 7.0 | 31 |
| cofire_only | +761.38% | 1.16 | -42.20% | 8 | 9 | 8.0 | 30 |
| both | +761.38% | 1.16 | -42.20% | 8 | 9 | 8.0 | 30 |

## Window: 500d

| Variant | Return | Sharpe | Max DD | SPY 90% cycles | SPY 90% trades | SPY 90% hrs | Orders |
|---------|--------|--------|--------|----------------|----------------|-------------|--------|
| baseline | +15.30% | 0.45 | -24.93% | 18 | 19 | 18.0 | 50 |
| adaptive_only | +16.29% | 0.46 | -25.57% | 7 | 8 | 7.0 | 32 |
| cofire_only | +16.03% | 0.46 | -25.02% | 8 | 9 | 8.0 | 42 |
| both | +15.49% | 0.45 | -25.61% | 8 | 9 | 8.0 | 38 |

## Recommendation

### 500d
- **adaptive_only**: return +0.99 pp, Sharpe +0.01, MaxDD -0.64 pp, fill +61.1% vs baseline cycles
- **cofire_only**: return +0.73 pp, Sharpe +0.01, MaxDD -0.09 pp, fill +55.6% vs baseline cycles
- **both**: return +0.19 pp, Sharpe +0.00, MaxDD -0.68 pp, fill +55.6% vs baseline cycles
### 2000d
- **adaptive_only**: return +145.16 pp, Sharpe +0.02, MaxDD -0.90 pp, fill +61.1% vs baseline cycles
- **cofire_only**: return +20.06 pp, Sharpe +0.00, MaxDD -0.10 pp, fill +55.6% vs baseline cycles
- **both**: return +20.06 pp, Sharpe +0.00, MaxDD -0.10 pp, fill +55.6% vs baseline cycles

**Adaptive chunk only** wins on `2000d` — larger solo-sleeve chunks when room > 5×. Set `ADAPTIVE_CHUNK_ENABLED=true`.