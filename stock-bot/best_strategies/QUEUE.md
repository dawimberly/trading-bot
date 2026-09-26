# Queue — test, then maybe deploy

Ordered by `ledger.py` priority. Aggressive is the only book this agent will
change, and only after PROTOCOL gates. Medium/live stay owner-gated.

Print the same list: `python best_strategies/status.py`

## Now (do in order)

- **A2B1** wired on Lab 8 (`PAPER_NYSE_A2B1_ENABLED=true`, 8%, skip Monday).
  Collect the full-stack second window. Not Medium/live.

## Done this cycle (2026-09-20)

- **Q0** weekly_review Medium SoT — grade A, +5.66% week. 0.675 cap not applied.
- **Q1** name-count 8/15/25 — 15 fails. 8 beats 25. Already Lab 8.
- **6 vs 8** — FAIL both windows. Stay Lab 8. No `.env`.
- **Q2** Medium drift — report only. Portal is 15 / 2.0×, disk file is stale.
- **Q3** STRICT vs FULL on VTI=0 Lab 8 — FULL +6.06pp / +0.41 Sharpe / DD +0.48pp
  vs STRICT, but FULL is not PIT. **Do not promote or cut scanners.**
- **Q4** TOD 9:30–10:00 block — last 365d PASS, prior 365d FAIL. **KILL.** See
  [TOD_ENTRY.md](TOD_ENTRY.md).
- **Fat-loser** — **KILL.** 365d −33pp / 1000d −59pp vs hold. Portal already OFF.

## Later (watch)

6. **A2B1 second window** — already on Lab 8. Measure, then promote or kill.
   Not Medium.
7. **Dynamic VTI 40% floor vs VTI=0** — do not silently restore the old lock.
8. **Lab 8-name concentrated** — walk looks hot vs daily executor; already on via portal.
9. **VTI tactical $5k** — live/owner only.

## Never (reject)

Friday flatten · Thursday-only long flatten · short-winners overnight ·
Monday-only first-hour dump · take_1r as default · open-predict models ·
promote from MC 200/200 · default 9:30–10:00 NYSE entry block ·
fat-loser open −1% drip.

## Deploy rule of thumb

Name-count 15 failed. Aggressive stays Lab 8 (portal) with A2B1 on.
Next extra knob still needs PROTOCOL + a named sentence in chat. Medium/live
stay owner-gated.

