# STRICT Live Profile A vs VTI B&H

Generated: 2026-08-20 23:09 UTC

**STRICT live-shaped research only. Do not promote. Live Profile A unchanged. Research equity is scaled so the 5% SPY sleeve can fill; live ~$300 / $10 max cannot.**

Stack: 85% VTI core, SPY trend ON, NYSE leftover, same-day rebuy blocked (LIVE_NYSE_SAME_DAY_REENTRY_BLOCK). Stat arb / shorts / ARIMA / daily bank / RVOL-ORB-catalyst OFF. GARCH + ATR/exits + corr + tail ON via `enforce_live_conservative_profile()`.

Research sizing: $10,000 start / $500 max order (live 1% risk ratio; live $10 max cannot fill a 5% SPY sleeve).

| Window | Dates | Profile A return | Sharpe | MaxDD | VTI B&H | Δ vs VTI | Trades | SPY | NYSE |
|--------|-------|------------------|--------|-------|---------|----------|--------|-----|------|
| 365d | 2025-10-15 -> 2026-08-20 | +16.74% | 1.17 | -8.67% | +16.74% | -0.00pp | 40 | 25 | 15 |
| 180d | 2026-03-21 -> 2026-08-20 | +14.08% | 2.14 | -4.82% | +19.33% | -5.25pp | 40 | 12 | 28 |
| 90d | 2026-06-06 -> 2026-08-20 | +3.56% | 1.11 | -3.40% | +4.87% | -1.31pp | 24 | 14 | 10 |

## Caveats

- Not paper-aggressive; live-conservative context only.
- STRICT PIT kill-switches: insider / RVOL / catalyst / hist-news / LLM / dyn_univ / buffett fallback OFF.
- Same-day NYSE rebuy uses LIVE_NYSE_SAME_DAY_REENTRY_BLOCK (default true). Paper max-2-adds / $25 min / ATR sleeve cooldown are paper-only and stay off.
- Research start equity $10k / $500 max order so 5% SPY can fill. Live ~$300 / 1% / $10 max is smaller; SPY may not trade there.
- PAPER_TRADING is False only in-process for enforce_live_conservative_profile(); .env is not written.
- Backtest uses a stub PAPER_JOURNAL_CSV so live portal blotter is not re-parsed every bar.
- GARCH/ATR/corr/tail follow enforce_live_conservative_profile(); SMART_STOPS_LIVE default OFF unless env-explicit.
- No MC 200. Do not promote from this file.

No MC 200. No promote.
