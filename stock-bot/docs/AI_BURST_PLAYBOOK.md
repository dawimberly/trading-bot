# AI-Burst Preparedness Playbook

**Status:** research / policy draft  
**Date:** 2026-08-19  
**Scope:** Paper book first. Live Profile A is too small for options.  
**Not a promote:** nothing here changes `.env`, sleeves, or live until named gates pass.

---

## 1. Thesis

AI-related valuations can unwind the way late-1990s tech and 2007–08 housing did: crowded narrative, high multiples, then a multi-quarter or multi-year de-rating.

**Goal is preparedness, not a timed top.** You can be early for a long time. The playbook must still work if the unwind is 12–24 months away.

This is not a prediction that every stock goes to zero. Expected damage if the thesis is right:

- High-multiple AI / software names hit hardest
- Speculative and unprofitable tech hit hard
- Anything priced for perfection on AI narratives
- Broader risk assets if liquidity tightens

---

## 2. Books and constraints

| Book | Approx equity | Core | Role |
|------|---------------|------|------|
| Paper | ~$97,000 | Dynamic VTI 40–75% (hard floor 40%) | Research + hedge home |
| Live | ~$300 | ~85% VTI | Do not hedge with options |

### Non-negotiable rules

1. Never sell the VTI floor to fund hedges or shorts.
2. Hedge budget is a **cost**, not an alpha engine.
3. Defined risk only: long puts or debit put spreads. No naked short stock. No short calls as the hedge.
4. Two buckets stay separate:
   - **Book hedge** — broad crash protection
   - **Thesis ticket** — optional single-name (e.g. PLTR)
5. When the quarter’s budget is used, stop. No “one more.”
6. Survive being early. Do not raise the budget because you feel late.

---

## 3. Budget (paper)

Recast dollars from percent if equity changes. Do not raise the percent.

| Bucket | Cap | Purpose |
|--------|-----|---------|
| Book hedge | **0.75% of paper equity / quarter** (~$725 at $97k) | QQQ or SPY puts / put spreads |
| Thesis ticket | **0.25% / quarter** (~$240) | Optional 1-contract long-dated put on one AI name |
| Combined ceiling | **1.0% / quarter** | Hard stop |

---

## 4. Instruments

### Primary — book hedge

- Underlying: **QQQ** first (more AI-heavy). **SPY** as second pass.
- **v1:** long put, ~120–180 DTE, ~0.20–0.30 delta (fallback: 10–15% OTM).
- **v2 (only after v1 is understood):** put debit spread (buy one put, sell a lower put) to cut premium and cap payout.

### Secondary — thesis ticket (optional)

- One name you think is most exposed (e.g. PLTR).
- Long-dated put only.
- **Do not use** the failed rule: 60–90 DTE / 2× take-profit / 50% premium stop / 21 DTE. That rule lost on 2024-02 → 2026-05 (45 trades, 20% win rate, −$2,824, median trade −51%).
- 1 contract max until a *new* long-dated rule is backtested.

---

## 5. When to add, hold, or cut

| Condition | Action |
|-----------|--------|
| New quarter, budget unused, vol not exploding | Initiate or roll book hedge inside budget |
| IV cheap, thesis unchanged | Prefer buy / roll (still inside budget) |
| IV already spiked, puts expensive | Skip or use a cheaper spread. Do not blow the budget |
| Hedge is up a lot (drawdown underway) | Take partial profit on the hedge. **Do not** dump the VTI floor |
| Hedge expired worthless, thesis unchanged | Accept the cost. Next quarter is new budget, not “make it back” |
| Thesis invalidated | Stop new hedges. Expire or close what remains |

---

## 6. What you will not do

- Strip VTI to 0 to “go to cash and puts”
- Short a basket of AI names with open-ended risk
- Wire this into `run_all.py` before backtest gates pass
- Size live options off a ~$300 account
- Reuse the failed PLTR 2× / 50% / 21-DTE recipe
- Optimize 50 deltas × 20 DTEs and call it research

---

## 7. Success definition

A good quarter is:

- VTI floor intact
- Hedge spend ≤ budget
- In a **−20% to −40%** QQQ / AI drawdown, the hedge offsets a **meaningful slice** of book pain (not necessarily 100%)
- In a **grind-up** year, you only lost the budgeted premium

This is insurance. Hero P&L on puts is not the target.

---

## 8. Two research tracks

Do not mix them.

### Track A — Equity hygiene (already in motion)

Stop the NYSE buy → ATR-stop → next-name recycle so the unhedged book is less fragile.

| Step | What | Pass / fail |
|------|------|-------------|
| A1 | Journal honesty | After next filling session, `event=fill` exists; recon fill count ≈ `bot_actions` fills; sells with avg entry show non-zero `realized_pnl` |
| A2 | ATR-stop sleeve cooldown | Median daily cancel vs old **0.83**; fewer new NYSE names the same day after a full ATR stop |
| A3 | If cancel stays high | Change **one** extra lever (wider stop **or** stricter entry), not both. Re-measure 5–10 sessions |
| A4 | Only then | Short STRICT-leg or walk-forward on the **equity** stack. Compare median / left tail to VTI |

Paper already restarted 2026-08-19 ~15:24 CT with:

`NYSE hygiene ON (max 2 adds | same-day block | min $25 | ATR-stop sleeve cooldown)`

Measure after the next session that produces a full ATR stop.

### Track B — Hedge backtest (new, standalone)

Prove or kill a budgeted QQQ (then SPY) long-put overlay.

**Pass bar (insurance, not alpha):**

- Up-market drag stays near the budget (order of &lt;1% per quarter, not a second −5% sleeve)
- In the worst 20–40% drawdown window in sample, the hedge recovers enough that you would still have paid for it
- If both fail: playbook stays **cash + VTI floor + no options**

**v2** (put debit spread) only after v1 is honest.

---

## 9. Equity stack reminder (do not retune from this doc)

Paper (aggressive research):

- Smart Dynamic VTI 40–75% (floor 40%)
- SPY satellite OFF
- NYSE: max 2 adds, same-day rebuy blocked, $25 min, ATR-stop sleeve cooldown
- GARCH ON, daily bank ON, ARIMA OFF, crypto OFF, conviction top-N 0

Live (Profile A):

- ~85% VTI, SPY trend ON, 1% risk / $10 max under $500
- Stat arb, shorts-as-sleeve, ARIMA, daily bank OFF

Locked-off / rejected: `exit_h45_tight`, conviction top-N, live SPY-off, paper ARIMA, zero-core VTI.

Promote still needs 365d STRICT on return **and** Sharpe, MaxDD not worse by &gt;1.0pp. MC 200 (median +4.7%, p5 ≈ −44%) does not unlock promote.

---

## 10. Known evidence (do not mix windows)

- MC 200 paper-aggressive, window 2025-10-24 → 2026-08-11: median return **+4.7%**, p5 **−44%**, P(return&gt;0) **52.5%**
- STRICT vs FULL 365d (2025-05-28 → 2026-08-05): STRICT **−1.42%** vs FULL **−2.45%**; VTI B&H **+3.24%**. Overlays looked like drag.
- RHYME conflict audit: **0 of 49** round-trips changed RHYME letter. Problem is NYSE stop/recycle, not regime-letter war. Median daily cancel **0.83**.
- PLTR long-put rule (2× / 50% / 21 DTE, 2024-02-01 → 2026-05-29): 45 trades, 20% win, total **−$2,824**. Do not reuse.

---

## 11. Implementation order

1. Save this playbook in the repo (`docs/AI_BURST_PLAYBOOK.md` or equivalent).
2. Keep Track A running (journal + cooldown measurement).
3. Run Track B v1 QQQ hedge backtest (Cursor prompt below).
4. Read overlay: cost of being early vs help in a crash.
5. Only if v1 passes: optional 1-contract paper QQQ put **inside the budget**, same exits.
6. Single-name thesis ticket is a **separate** backtest with a new long-dated rule.

---

## 12. After a session — commands

```powershell
python scripts/analysis/trade_reconciliation.py --days 1
python scripts/analysis/rhyme_conflict_audit.py --since 2026-08-19 --book paper
```

Look for: `event=fill` rows, non-zero realized_pnl on sells, cancel ratio vs 0.83, fewer same-symbol round-trips.

---

## 13. Document control

- Owner must explicitly approve any live options or any raise of the 1% quarterly ceiling.
- Updates append; do not silently rewrite the budget or pass bars.
- This file is policy. Code lives in standalone `scripts/research/` until a later promote decision.

---

## 14. Track A journal path (2026-08-19, append)

Canonical paper journal the bot writes (do **not** add a second writer):

`stock-bot/data/portal/users/dawimberly/books/alpaca_paper/paper_journal.csv`

| Script | What it reads | Mismatch |
|--------|----------------|----------|
| `scripts/analysis/rhyme_conflict_audit.py --book paper` | Portal path first | Path is correct |
| `scripts/analysis/trade_reconciliation.py` | `config.PAPER_JOURNAL_CSV` default `paper_journal.csv` (stock-bot root). `.env` does not set this. | **Wrong file.** Root `stock-bot/paper_journal.csv` is stale (last row 2026-08-05). Live fills are on the portal CSV. |

Fill rows **do exist** on the portal file (2026-08-19 ~08:38–08:55 CT: DBB buy; CAMT / PFE / MU / SHOP / VSEC sells). `log_fill` writes `realized_pnl` as extra trailing fields.

Schema catch: the portal **header is the old 16-column row** (`...,exit_reason,notes`). `trade_journal._header_fieldnames` appends `order_id,book,entry_hour,realized_pnl,realized_pnl_pct,is_partial` only when writing, so pandas `read_csv` on that file will not name a `realized_pnl` column. Recon looking for `event=fill` + column `realized_pnl` will see fills (if pointed at the portal path) but treat P&amp;L as missing. No second writer.

---

## 15. Track B v1 result (2026-08-20, append — not a promote)

Standalone `scripts/research/ai_burst_hedge_backtest.py`. Window **2024-02-01 → 2026-08-19**. CSVs: `ai_burst_hedge_trades_QQQ.csv`, `ai_burst_hedge_trades_SPY.csv`.

| | QQQ | SPY |
|--|-----|-----|
| n | 6 | 5 |
| win rate | 50% (3/6) | 40% (2/5) |
| median trade | +$30.50 | −$331 |
| total hedge P&amp;L | **+$133** | **−$590** |
| hedge-curve max DD | −$661 | −$648 |
| up-quarter drag / book | −0.58% | −0.53% |
| worst book DD window | 2025-02-19 → 2025-04-08 | same |
| unhedged max DD | −$26,458 | −$24,289 |
| hedged max DD | −$25,797 | −$23,589 |
| lift at unhedged trough | **+$61** | **+$13** |

Apr-2025 crash trade (the one that 2×'d): QQQ +$661 (Apr 1–4), SPY +$700 (Apr 1–4). Net trough lift is tiny because prior expired hedges already spent ~$600.

**Pass-bar reading (insurance, not alpha):**

1. Up-market drag stays near the 0.75% budget — **pass** (not a second −5% sleeve).
2. Worst ~25% book drawdown: 1-lot / $750 cap cannot offset a meaningful slice of a $24–26k hole — **fail** as book insurance. A perfect 2× on the crash quarter is still ~$700.

No paper QQQ put, no live options, no budget raise. Prompt 5 (single-name thesis ticket) is still later.

---

## 16. Status 2026-08-20 (append)

Docs + recon path only. No budget / pass-bar / sleeve changes.

- **QQQ/SPY hedge v1:** cheap drag, not insurance. No options promote. See §15.
- **Journal:** fills write to the portal path. Recon now prefers that file (`scripts/analysis/trade_reconciliation.py`); stale root `paper_journal.csv` is fallback only. Trailing `realized_pnl` is mapped on read; historical rows are not rewritten.
- **Cooldown:** loaded 2026-08-19 ~15:24 CT. Measure after the next session that produces a full ATR stop.
- **Next measure (from `stock-bot/`):**

```powershell
python scripts/analysis/trade_reconciliation.py --days 1
python scripts/analysis/rhyme_conflict_audit.py --since 2026-08-19 --book paper
```

Dry check 2026-08-17 → 2026-08-20 on the portal journal: 21 `event=fill`, 9 sell fills, 9 with `realized_pnl`. Header still 16 columns (mapped on read).
