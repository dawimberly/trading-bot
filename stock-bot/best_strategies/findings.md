# Findings (measured, as of 2026-09-20)

Do not re-litigate a reject without a new window and a new knob.

## Keep / watch

**NYSE MA70 rank, 30d, 2× ATR, no 1R** is the only walk that is both simple
and repeatedly decent. 365d pick_15 book +49.2% / DD 4.9%; 1000d +91.8% / 10.0%.
Medium SoT is the cousin (currently 10 names and 2.5× ATR — drift).

Aggressive **runtime is Lab 8** via portal (not disk 25). Full-stack STRICT:
8 beats 25; 15 fails; **6 fails vs 8** (365d flat-to-worse, 1000d −15.4pp). Stay on 8.

**Fewer names beat more names on this tape** (same walk):

| Count | 365d book | 365d DD | 1000d book | 1000d DD |
|------:|----------:|--------:|-----------:|---------:|
| 8 | +59.9% | 6.4% | +123.6% | 15.5% |
| 15 | +49.2% | 4.9% | +91.8% | 10.0% |
| 25 | +41.2% | 3.2% | +74.7% | 6.9% |

**Skip Monday after a big up-day** (hold A2B1, ≥8%, next open, not Monday)
was the only overlay cell that cleared the NYSE-walk promote rule
(+9.0 pp book, DD better) — **hold only**, 242 RTH bars, extra turnover.
Wired 2026-09-26 on Lab 8 portal (`PAPER_NYSE_A2B1_ENABLED`). Still needs a
full-stack second window before calling it a promote. Not Medium/live.

**STRICT vs FULL on employed Lab 8 / VTI=0** (365d 2025-11-14→2026-09-20):
STRICT +1.60% / 0.16 / −11.90%; FULL +7.66% / 0.57 / −12.38% (dDD +0.48pp).
FULL is **not PIT** — do not promote scanners. Old STRICT>FULL was a different
VTI mix; do not cut overlays from that story either. Runtime scanners stay ON.

**Time of day:** open / first 30m are weak on the % move report. A 9:30–10:00
**entry block** still failed the prior-365d gate (see reject table). Do not
deploy it. Midday / last hour remain descriptive, not a knob.

## Reject

| Idea | Evidence |
|------|----------|
| Friday flatten | 365d +32% hold vs +13% flatten; 1000d +60% vs +27% |
| Fade Friday winners into Monday | After ≥5% up-day, Monday open **+0.70%** continuation |
| Thursday-only flatten on the long book | A1–A3 B2 negative on all four exit policies |
| EW short-winners overnight | −0.21%/night at ≥5%; Monday −0.73% (short beta) |
| take_1r as default | Higher book, DD +4 pp vs hold (10.95 vs 6.63) — fail 2 pp gate |
| Open-price models | Ridge / gap-persist lose to `pred = prior close` |
| “Monday is a unique first-hour dump” | WOP ≈ 1.0 every weekday; Tuesday slightly worse |
| MC 200/200 as a promote | Median return 4.7%, mean 21%, p5 −44% |
| Always-on metals 33/10 | HOLD vs 33/67; not promoted |
| VTI tactical $5k shadow | Promote criteria not passed |
| Block NYSE entries 9:30–10:00 | 5m walk: last 365d +3.09pp, prior 365d −0.25pp / Sharpe worse |
| Fat-loser open −1% drip | 365d −33pp book vs hold; 1000d −59pp. Shallower DD, kills the book |

## Do not confuse with production

Lab 8-name concentrated walk printed +123% / DD 12% on 365d close-to-close
with no VTI, no costs, no scanners. That is **not** the live aggressive
executor. Aggressive is Lab 8 + scanners ON + fat-loser OFF (portal).

v1.5.4 “Dynamic VTI 40–75%” is the **documented** lock. Both paper books
currently run **VTI = 0**. Treat that as an explicit employed stack, not as
an accidental off switch, until a STRICT A/B vs 40% floor is re-run on
**this** tape.
