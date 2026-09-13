# Paper NYSE 1R hit test (historical)

Journal: `C:\Users\Owner\PythonTrading\stock-bot\data\portal\users\dawimberly\books\alpaca_paper_v2\paper_journal.csv`
Rule: 1R target = entry + max(2.0× daily ATR, 1% of entry). Hit if a later daily **close** reached it before the sell.

- Closed rounds scored: **21** — hit 1R: **0 (0%)**
- Held to a later day: **2** — hit 1R: **0 (0%)** (same-day exits never get a second close)
- Still open: 17 (already tagged 1R: 0)
- Median target: 2.48%  |  median MFE: -0.11%

## Closed rounds

| symbol | days | entry | target % | MFE % | MAE % | exit % | hit |
|---|---:|---:|---:|---:|---:|---:|---|
| EL | 0 | 103.38 | 2.39 | 0.46 | 0.46 | 0.23 | no |
| EL | 0 | 103.37 | 2.39 | 0.47 | 0.47 | 0.1 | no |
| GOLD | 0 | 41.40 | 2.58 | 0.19 | 0.19 | 0.77 | no |
| GOLD | 0 | 41.46 | 2.58 | 0.05 | 0.05 | -0.01 | no |
| ZETA | 1 | 32.69 | 3.55 | -0.03 | -4.10 | -4.22 | no |
| NFLX | 0 | 82.75 | 1.79 | -0.10 | -0.10 | -1.11 | no |
| XOM | 0 | 164.70 | 1.70 | -1.51 | -1.51 | -1.0 | no |
| RTX | 0 | 202.45 | 1.78 | -0.16 | -0.16 | -1.13 | no |
| KNSA | 0 | 80.25 | 1.82 | 0.27 | 0.27 | -1.24 | no |
| TWST | 0 | 130.85 | 9.00 | 0.11 | 0.11 | -0.46 | no |
| ELF | 0 | 108.12 | 3.77 | -0.70 | -0.70 | -0.67 | no |
| PSX | 0 | 256.90 | 1.79 | -0.32 | -0.32 | -0.25 | no |
| MPC | 0 | 389.37 | 1.73 | -0.61 | -0.61 | -0.39 | no |
| HALO | 0 | 109.23 | 1.80 | 0.39 | 0.39 | -0.36 | no |
| CAKE | 0 | 109.39 | 3.03 | -0.75 | -0.75 | -0.33 | no |
| LNG | 1 | 295.90 | 1.65 | -0.01 | -1.71 | -1.22 | no |
| KNSA | 0 | 79.47 | 1.96 | 0.53 | 0.53 | -0.5 | no |
| VLO | 0 | 367.89 | 1.89 | -0.49 | -0.49 | -0.43 | no |
| DINO | 0 | 106.29 | 3.01 | -0.22 | -0.22 | -0.21 | no |
| VEEV | 0 | 281.11 | 3.74 | -0.14 | -0.14 | -0.27 | no |
| PBF | 0 | 76.64 | 5.10 | -1.51 | -1.51 | -0.52 | no |
