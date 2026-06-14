# VTI level comparison — Best Paper v2.1 + Thinking Engine (upgraded 2026-06-13)

Command:
```
python backtester.py --days 365 --paper-aggressive --compare-vti-levels
```

Window (365d): 2025-03-20 -> 2026-06-12 (310 sim bars) | VTI B&H: +33.46% | tilt cap ±6%

| VTI level | Return | Sharpe | MaxDD | AvgAct | vs VTI |
|-----------|--------|--------|-------|--------|--------|
| 90% (live-like) | +61.35% | 1.80 | -9.51% | 10.0% | +27.9 pp |
| 80% | +75.63% | 1.90 | -9.59% | 20.0% | +42.2 pp |
| 75% | +73.20% | 1.90 | -11.20% | 25.0% | +39.7 pp |
| 70% | +76.23% | 1.90 | -11.44% | 30.0% | +42.8 pp |

Best Sharpe: 80% VTI (1.90) | Best return: 70% VTI (+76.23%) | Shallowest MaxDD: 90% (-9.51%)

Recommendation: **80% fixed VTI** for risk-adjusted beat on paper; **90% live** below $500; **dynamic 40-75%** for paper research profile.
