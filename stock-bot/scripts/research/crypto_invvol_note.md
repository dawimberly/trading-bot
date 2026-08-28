# Crypto inv-vol (research only) — 2026-08-26

Not a sleeve. Production crypto stays 0%. No promote.

Documented flatten: **ATR% 3.0%** (`--atr-flat 0.03`). Coins: BTC/USD ETH/USD SOL/USD. Fee 0.25%/leg. Size `clip(0.015/ATR%,0,0.40)` + GARCH shrink vs 15% ann.

365d @ 3%: ret **+12.7%**, maxDD **−11.6%**, Sharpe **0.84**, 91 trades, **65.6% cash**. 4.5% flatten was −21% / −43% DD. 2% ≈ always cash.

90d BH ETH/SOL still beat inv-vol. No MC (NYSE helper only).

Run: `python scripts/research/backtest_crypto_invvol.py --days 365 --atr-flat 0.03`
