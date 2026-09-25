# Stall arm cutoff vs stronger MA signal

_NYSE hold walk, 15 names, 10bps round trip. Research only. Does not write `.env`._

**MIXED — book +7.75pp, max DD -0.69pp. Next 10 sessions the challenger averaged 2.33% vs -0.27% for the stalled name, and won the matchup 53.1% of the time.**

A name is stalled when it is up at least the arm percent from entry and its close is not higher than 3 sessions ago. A new name is more promising when it sits further above its moving average than that stalled name. The swap happens only when all 15 slots are full.

Hold book return 64.77%, max DD 8.42%.

| Arm | Rotations | Book Δ | DD Δ | Kept 10d | New 10d | New won |
|----:|----------:|-------:|-----:|---------:|--------:|--------:|
| 1% | 124 | -3.23 pp | -1.69 pp | 1.73% | 1.31% | 43.8% |
| 2% | 100 | -12.92 pp | -1.43 pp | 2.59% | 1.01% | 42.7% |
| 3% | 100 | -1.46 pp | -1.84 pp | 1.61% | 0.22% | 46.9% |
| 4% | 83 | +5.34 pp | -1.20 pp | 0.09% | 1.11% | 51.9% |
| 5% | 66 | +7.75 pp | -0.69 pp | -0.27% | 2.33% | 53.1% |
| 6% | 61 | +3.35 pp | -0.77 pp | -0.43% | 1.55% | 52.5% |
| 7% | 46 | +2.73 pp | -1.46 pp | 1.44% | 0.56% | 48.8% |
| 8% | 45 | +4.50 pp | -2.36 pp | 1.07% | 0.97% | 50.0% |
| 9% | 49 | +14.07 pp | -1.60 pp | 0.85% | 3.44% | 50.0% |
| 10% | 36 | +3.39 pp | -1.60 pp | 2.71% | 2.98% | 45.7% |

Window 2025-09-01 → 2026-09-25. MA70, max hold 30 bars. 5% arm book +7.75 pp, max DD -0.69 pp.
