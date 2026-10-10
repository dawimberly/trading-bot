# Findings (measured, as of 2026-10-10)

Do not re-litigate a reject without a new window and a new knob.

Research SoT for the isolated NYSE MA70 walk is **S&P 500 membership by
date**, 10 bps round trip, sleeve 0.95, drawdown marked to market
(`scripts/analysis/pit_universe_rerun.py`). Numbers that used today's
screener on history (`current`, or any walk dated before 2026-10-10) are
look-ahead. Do not quote them as evidence.

## Keep / watch

**NYSE MA70 rank, 30d, 2× ATR, no 1R, pick_15** is the only simple walk that
is still decent on the honest book.

| Universe | 365d book / DD / Sharpe | full DB book / DD / Sharpe |
|----------|-------------------------|----------------------------|
| current (look-ahead) | +246% / 13.8 / 2.72 | +298% / 30.6 / 1.28 |
| frozen (static lists) | +71% / 14.5 / 1.63 | +103% / 22.2 / 0.90 |
| **sp500 PIT** | **+95.5% / 13.8 / 1.91** | **+134% / 19.6 / 1.06** |

365d = 2025-09-16 → 2026-10-10. Full DB = 2022-06-11 → 2026-10-10.
Deflated Sharpe on pick_15 / S&P (90 trials in the family ledger): 365d DSR
0.71, full-DB 0.16. PSR>0 is ~0.99 — the book beats cash; it is not the best
of N lucky trials at 95%.

Medium SoT is the cousin (portal 15 names / 2.0×; disk file still says 10 /
2.5× — see MEDIUM_DRIFT.md).

**Name count on S&P PIT** (same isolated walk; not the full-stack STRICT
table in NAME_COUNT.md):

| Count | 365d book | 365d DD | full book | full DD | vs pick_15 |
|------:|----------:|--------:|----------:|--------:|------------|
| 8 | +103.5% | 20.4% | +165.8% | 23.2% | FAIL DD |
| 15 | +95.5% | 13.8% | +134.4% | 19.6% | base |
| 25 | +71.8% | 12.6% | +109.0% | 14.2% | FAIL ret |

Fewer names no longer win once look-ahead is removed. pick_8 makes more and
breaks the 2 pp DD gate. Aggressive runtime stays **Lab 8** because that is
the executor stack (STRICT 8 vs 25 in NAME_COUNT.md still stands), not
because this isolated walk prefers 8.

**Lab 8 concentrated** on S&P PIT: 365d +121% / 20.8 DD; full +189% / 22.7.
More return, FAIL 2 pp DD vs pick_15 both windows. HOLD. Not a promote.

**STRICT vs FULL on employed Lab 8 / VTI=0** (365d 2025-11-14→2026-09-20):
STRICT +1.60% / 0.16 / −11.90%; FULL +7.66% / 0.57 / −12.38% (dDD +0.48pp).
FULL is **not PIT** — do not promote scanners. Runtime scanners stay ON.

**Time of day:** a 9:30–10:00 entry block failed the prior-365d gate. Do not
deploy it.

## Reject

| Idea | Evidence |
|------|----------|
| A2B1 skip-Monday fade (≥8% prior close, next open) | S&P PIT 10bps MTM 2026-10-10: 365d −8.9pp / DD +0.7pp (84 fades); full −22.2pp / DD −1.4pp (142 fades). FAIL return both windows. Open gaps yfinance (Alpaca SIP blocked). Pre-PIT +9pp does not survive. Lab flag left on — named kill only. |
| Risk-adjusted rank (distance / 20d vol) | S&P PIT 365d −61pp / full −103pp vs pick_15. 365d DD better. Kills the book. |
| 12% vol target (126-bar book vol) | S&P PIT 365d −23pp / full −29pp. DD −4.7 / −5.3 pp. FAIL return gate. |
| Lab 8 vs pick_15 on S&P PIT | More book, DD +7.0 / +3.0 pp. FAIL 2 pp gate. |
| Friday flatten | S&P PIT 365d +38% / 24.9 DD vs pick_15 +96% / 13.8; full +53% / 24.9 vs +134% / 19.6 |
| Fade Friday winners into Monday | After ≥5% up-day, Monday open **+0.70%** continuation |
| Thursday-only flatten on the long book | A1–A3 B2 negative on all four exit policies |
| EW short-winners overnight | −0.21%/night at ≥5%; Monday −0.73% (short beta) |
| take_1r as default | S&P PIT 365d +82% / 14.3 vs +96% / 13.8; full +111% / 19.9 vs +134% / 19.6 |
| Open-price models | Ridge / gap-persist lose to `pred = prior close` |
| “Monday is a unique first-hour dump” | WOP ≈ 1.0 every weekday; Tuesday slightly worse |
| MC 200/200 as a promote | Median return 4.7%, mean 21%, p5 −44% |
| Always-on metals 33/10 | HOLD vs 33/67; not promoted |
| VTI tactical $5k shadow | Promote criteria not passed |
| Block NYSE entries 9:30–10:00 | 5m walk: last 365d +3.09pp, prior 365d −0.25pp / Sharpe worse |
| Fat-loser open −1% drip | 365d −33pp book vs hold; 1000d −59pp. Shallower DD, kills the book |

## Do not confuse with production

The old Lab 8 walk that printed +123% / DD 12% (365d, no costs, today's
screener) is look-ahead. On S&P PIT with 10 bps it is +121% / 20.8 DD and
fails the gate vs pick_15. Aggressive runtime is still Lab 8 + scanners ON +
fat-loser OFF + A2B1 ON (portal). A2B1 is leftover wiring; the PIT walk
failed. Named kill only.

v1.5.4 “Dynamic VTI 40–75%” is the **documented** lock. Both paper books
currently run **VTI = 0**. Treat that as an explicit employed stack until a
STRICT A/B vs 40% floor is re-run on **this** tape.
