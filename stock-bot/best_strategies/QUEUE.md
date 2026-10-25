# Queue — test, then maybe deploy

Ordered by `ledger.py` priority. Aggressive is the only book this agent will
change, and only after PROTOCOL gates. Medium/live stay owner-gated.

Print the same list: `python best_strategies/status.py`

## Now (do in order)

- Fat-loser stays 0 on all three books. No isolated-walk knob is next.
  A2B1 cleared the disjoint PIT gate; Lab already has it. Medium/live stay
  off until named.

## Done this cycle (2026-10-10)

- **Prefetch + disjoint years** — yfinance max history for missing S&P
  members; interior ffill (cut after last real print); two non-overlapping
  365-session windows. Coverage **97.4% / 95.4%** (was 55% / 51% on an
  overlapping full-DB). Time-exit on a dead name uses the last valid close
  so the book return stays finite.
- **pick_15 on honest two years** — recent **+100.2% / 18.7 / 1.66**;
  prior **+6.2% / 22.8 / 0.27**. Keep as walk SoT. The old +119% / full
  +133% mixed the same year twice at half coverage.
- **A2B1** — **CANDIDATE** both windows (+7.9 pp / −2.7 pp DD recent; +0.5 pp
  / −3.8 pp DD prior). 118 / 37 fades, 0 weekend. Lab flag left on. No
  `.env`. Not Medium/live.
- **Name count on S&P PIT** — pick_8 FAIL DD both; pick_25 FAIL recent ret.
  STRICT executor “8 beats 25” is **not PIT**.
- **Lab 8 vs pick_15** — recent FAIL ret; prior PASS. HOLD.
- **risk_adj rank** — recent −61 pp FAIL; prior PASS. KILL.
- **12% vol target** — recent −31 pp FAIL; prior PASS. KILL.
- **Saturday review `trade_closed` dollars** — 10s one-to-one pair; blank
  `trade_closed` yields to the percent `exit`. No `.env`.

## Done 2026-09-20

- **Q0** weekly_review Medium SoT — grade A, +5.66% week. 0.675 cap not applied.
- **Q1** name-count 8/15/25 — 15 fails. 8 beats 25. Already Lab 8.
- **6 vs 8** — FAIL both windows. Stay Lab 8. No `.env`.
- **Q2** Medium drift — report only. Portal is 15 / 2.0×, disk file is stale.
- **Q3** STRICT vs FULL on VTI=0 Lab 8 — FULL +6.06pp / +0.41 Sharpe / DD +0.48pp
  vs STRICT, but FULL is not PIT. **Do not promote or cut scanners.**
- **Q4** TOD 9:30–10:00 block — last 365d PASS, prior 365d FAIL. **KILL.** See
  [TOD_ENTRY.md](TOD_ENTRY.md).
- **Fat-loser** — **KILL.** Session PIT still FAIL ret. Portal already OFF.

## Later (watch)

6. **Dynamic VTI 40% floor vs VTI=0** — do not silently restore the old lock.
7. **Lab 8-name concentrated** — S&P PIT fails the both-windows return gate
   vs pick_15. Runtime stays Lab 8 via portal; do not promote from the walk.
8. **VTI tactical $5k** — live/owner only.
9. **A2B1 on Medium** — isolated walk is a candidate. Owner phrase only.

## Never (reject)

Friday flatten · Thursday-only long flatten · short-winners overnight ·
close trail armed at +5% / 3% under the peak close ·
Monday-only first-hour dump · take_1r as default · open-predict models ·
promote from MC 200/200 · default 9:30–10:00 NYSE entry block ·
fat-loser open −1% drip · risk-adjusted MA rank · 12% vol target on this book.

## Deploy rule of thumb

Isolated NYSE walk SoT is S&P PIT pick_15 (recent strong, prior almost
flat). Aggressive stays Lab 8 (portal) from the STRICT executor table.
A2B1 is on Lab and now also clears the isolated-walk gate — leave it;
do not flip Medium/live. Next extra knob still needs PROTOCOL + a named
sentence in chat. Medium/live stay owner-gated.
