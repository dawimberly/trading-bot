# Findings (measured, as of 2026-10-10, session bars, disjoint years)

Do not re-litigate a reject without a new window and a new knob.

Research SoT for the isolated NYSE MA70 walk is **S&P 500 membership by
date**, Mon–Fri session bars only, interior ffill (no post-delist flat),
10 bps round trip, sleeve 0.95, drawdown marked to market
(`scripts/analysis/pit_universe_rerun.py`). Numbers that used today's
screener on history (`current`) are look-ahead. Do not quote them.

Windows are **two disjoint 365-session walks**, not a full-DB that contains
the recent year:

| Window | Walk dates | Coverage |
|--------|------------|----------|
| **365d_recent** | 2025-04-11 → 2026-10-09 | **97.4%** (530 / 544 members) |
| **365d_prior** | 2023-10-02 → 2025-04-10 | **95.4%** (515 / 540 members) |

Missing names are Yahoo-dead M&A tickers (ANSS, DFS, JNPR, WBA, …), not
the old “sqlite only had the fetch list” hole. The 55% / overlapping
full-DB figures from earlier on 2026-10-10 are superseded.

DSR is directional only (family ledger mixes windows and universes).
Prior-year Sharpe is not distinguishable from the best of N lucky trials
(pick_15 DSR 0.06). PSR>0 is high on the recent year only.

## Keep / watch

**NYSE MA70 rank, 30d, 2× ATR, no 1R, pick_15** is the isolated-walk keep.
The recent year is the one that looked like +100%. The prior year is almost
flat. That is the honest two-year picture.

| Universe | 365d_recent book / DD / Sharpe | 365d_prior book / DD / Sharpe |
|----------|--------------------------------|-------------------------------|
| current (look-ahead) | +266% / 14.4 / 2.51 | **−14.2% / 31.0 / −0.47** |
| frozen (static lists) | +125% / 13.7 / 2.06 | −9.7% / 22.8 / −0.45 |
| **sp500 PIT** | **+100.2% / 18.7 / 1.66** | **+6.2% / 22.8 / 0.27** |

Medium SoT is the cousin (portal 15 names / 2.0×; disk file still says 10 /
2.5× — see MEDIUM_DRIFT.md).

**Name count on S&P PIT** (isolated walk). The STRICT executor table in
NAME_COUNT.md is **not PIT** — `backtester.py` still uses today's screener.

| Count | recent book | recent DD | prior book | prior DD | vs pick_15 |
|------:|------------:|----------:|-----------:|---------:|------------|
| 8 | +167.3% | 26.7% | +1.6% | 29.0% | FAIL DD both |
| 15 | +100.2% | 18.7% | +6.2% | 22.8% | base |
| 25 | +68.3% | 17.4% | +10.1% | 17.9% | FAIL recent ret; PASS prior |

Aggressive runtime stays **Lab 8** from the non-PIT STRICT executor table, not
because this walk prefers 8.

**Lab 8 concentrated** on S&P PIT: recent +96% / 16.0 (FAIL ret vs pick_15);
prior +24.0% / 24.8 (PASS, DD +2.0 pp on the gate). HOLD both-windows. Not a
promote. Runtime stays Lab 8 via portal.

**A2B1 skip-Monday fade** (≥8% prior close, next open) on S&P PIT:
recent **+108.1% / 16.0 / 1.90** vs pick_15 +100.2% / 18.7 (**+7.9 pp**, DD
−2.7 pp, 118 fades, 0 weekend); prior **+6.7% / 19.0 / 0.29** vs +6.2% / 22.8
(**+0.5 pp**, DD −3.8 pp, 37 fades). **CANDIDATE** both windows. Prior-year
edge is tiny. Lab already has the flag. **No `.env`.** Not Medium/live
until named.

**STRICT vs FULL on employed Lab 8 / VTI=0** (365d 2025-11-14→2026-09-20,
non-PIT): STRICT +1.60% / 0.16 / −11.90%; FULL +7.66% / 0.57 / −12.38%.
Do not promote scanners. Runtime scanners stay ON.

**Time of day:** a 9:30–10:00 entry block failed the prior-365d gate
(pre-PIT). Do not deploy it.

## Reject

| Idea | Evidence |
|------|----------|
| Close trail: arm at +5% of the filled entry, exit next open after a later close is 3% under the peak close | Daily baseline A, frozen ABC panel, 5 bps/side. Prior 2023-11-15→2025-05-01: **−11.7 pp** and drawdown worse. Recent 2025-05-02→2026-10-14: **−43.6 pp**, drawdown better. FAIL both windows. Not a Live test. |
| Risk-adjusted rank (distance / 20d vol) | Disjoint S&P PIT: recent **−61 pp** (FAIL); prior +17 pp (PASS). Kill — fails the year that has the edge. |
| 12% vol target (126-bar book vol) | Disjoint S&P PIT: recent **−31 pp** (FAIL); prior +5 pp (PASS). DD better. FAIL return gate on the recent year. |
| Lab 8 vs pick_15 on S&P PIT | recent FAIL ret; prior PASS. FAIL both-windows. |
| take_1r as default | recent scrapes PASS (+0.5 pp / DD +0.9); prior FAIL (−3.8 pp). FAIL both-windows. |
| Friday flatten | recent +28% / 33.7 vs pick_15 +100% / 18.7; prior +2.4% / 17.4 vs +6.2% / 22.8 |
| Fat-loser open −1% drip | Session S&P PIT recent +24% / 6.6 vs +100% / 18.7; prior +3.6% / 9.3 vs +6.2% / 22.8. FAIL ret. Portal already OFF. |
| Fade Friday winners into Monday *(pre-PIT)* | After ≥5% up-day, Monday open **+0.70%** continuation |
| Thursday-only flatten on the long book *(pre-PIT)* | A1–A3 B2 negative on all four exit policies |
| EW short-winners overnight *(pre-PIT)* | −0.21%/night at ≥5%; Monday −0.73% (short beta) |
| Open-price models *(pre-PIT)* | Ridge / gap-persist lose to `pred = prior close` |
| “Monday is a unique first-hour dump” *(pre-PIT)* | WOP ≈ 1.0 every weekday |
| MC 200/200 as a promote *(pre-PIT)* | Median return 4.7%, mean 21%, p5 −44% |
| Always-on metals 33/10 *(pre-PIT)* | HOLD vs 33/67; not promoted |
| VTI tactical $5k shadow *(pre-PIT)* | Promote criteria not passed |
| Block NYSE entries 9:30–10:00 *(pre-PIT)* | last 365d +3.09pp, prior 365d −0.25pp |
| VTI vs SPY inside H1D (2026-10-26, not pre-registered) | 2008-03..2026-10, 5 bps: VTI in Sleeve B 0.00pp, dynamic VTI/SPY -0.01pp, VTI 200d signal +0.43pp CI [-0.54,+1.45] and worse 2019-26. No change. `research/2026-10-26/vti_spy/` |
| Cross-asset Sleeve B tilts H1-H4 (2026-10-26, frozen+sha256) | Stock-bond corr->BIL +0.00pp; gold/real-rates -0.12pp; credit HYG/IEF -0.66pp and DD -3.3pp 2019-26; dollar +0.07pp. All fail >=1 window, CIs include zero. REJECT. `research/2026-10-26/cross_asset/` |
| Small-account edges H1-H4 (2026-10-26, frozen+sha256) | Turn-of-month IWM -0.34% CAGR; pre-holiday IWM 1.31%; weekly reversal S&P600 -8.2% (gross +21% eaten by 92x turnover @30bps, survivorship-biased); 52wk-high S&P600 -0.9%. All CIs vs H1D below zero. REJECT. PEAD / index add-delete untested (no free PIT data). `research/2026-10-26/small_edge/` |
| Insider Form-4 buying clusters (2026-10-26, frozen+sha256) | SEC bulk 2006q1-2026q1; >=3 insiders >=$10k open-market P within 30d by filing date, buy next close, hold 63d, 5% cap, 30 bps/side. 3,164 trades. CAGR 8.13% / DD -58.9% / Sharpe 0.41 vs H1D 12.99 / -27.5 / 0.79; won 2008-12 only (DD 2x worse), lost 2013-18 and 2019-26. Excess -1.41pp CI [-11.6,+8.6]. Survivorship-biased UP. REJECT. `research/2026-10-26/insider/` |

## Do not confuse with production

Aggressive runtime is still Lab 8 + scanners ON + fat-loser OFF + A2B1 ON
(portal). A2B1 now clears the isolated-walk gate; Lab already has it.
Medium and live stay off until you name them. Lab-8-as-walk-promote still
fails the honest book.

v1.5.4 “Dynamic VTI 40–75%” is the **documented** lock. Both paper books
currently run **VTI = 0**.
