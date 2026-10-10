# Queue — test, then maybe deploy

Ordered by `ledger.py` priority. Aggressive is the only book this agent will
change, and only after PROTOCOL gates. Medium/live stay owner-gated.

Print the same list: `python best_strategies/status.py`

## Now (do in order)

- Fat-loser stays 0 on all three books. No isolated-walk knob is next.
  Named kill only if you want A2B1 off Lab.

## Done this cycle (2026-10-10)

- **PIT universe + MTM drawdown** — `current` look-ahead +246% / 13.8 vs
  S&P PIT +95.5% / 13.8 (365d). New walk SoT is S&P membership by date.
  See [findings.md](findings.md).
- **Name count on S&P PIT** — pick_8 FAIL DD, pick_25 FAIL ret. Isolated
  walk no longer prefers 8. Aggressive stays Lab 8 from the STRICT
  executor table, not from this walk.
- **Lab 8 vs pick_15 on S&P PIT** — more book, DD +7.0 / +3.0 pp. HOLD.
- **risk_adj rank** — FAIL both windows (−61 / −103 pp). KILL.
- **12% vol target** — DD better, FAIL return both windows (−23 / −29 pp). KILL.
- **A2B1 on S&P PIT** — FAIL return both windows (−8.9 / −22.2 pp). 84 / 142
  fades. Open gaps yfinance (Alpaca SIP blocked). Lab flag left on.
- **Saturday review `trade_closed` dollars** — paired `exit` notes yield to
  `trade_closed` realized_pnl. Percent-only unpaired exits still count as
  direction. No `.env`.

## Done 2026-09-20

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

6. **Dynamic VTI 40% floor vs VTI=0** — do not silently restore the old lock.
7. **Lab 8-name concentrated** — S&P PIT fails the 2 pp DD gate vs pick_15.
   Runtime stays Lab 8 via portal; do not promote from the walk.
8. **VTI tactical $5k** — live/owner only.

## Never (reject)

Friday flatten · Thursday-only long flatten · short-winners overnight ·
Monday-only first-hour dump · take_1r as default · open-predict models ·
promote from MC 200/200 · default 9:30–10:00 NYSE entry block ·
fat-loser open −1% drip · risk-adjusted MA rank · 12% vol target on this book ·
A2B1 skip-Monday fade (isolated walk).

## Deploy rule of thumb

Isolated NYSE walk SoT is S&P PIT pick_15. Aggressive stays Lab 8 (portal)
from the STRICT executor table. A2B1 is still on Lab only because turning
it off needs a named kill — the honest walk failed. Next extra knob still
needs PROTOCOL + a named sentence in chat. Medium/live stay owner-gated.

