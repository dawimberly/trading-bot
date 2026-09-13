# Post-freeze checklist (PythonTrading / stock-bot)

**Status:** Planning only while **FORWARD_PAPER_FREEZE** is ON (through **~2026-08-26**, soft date).

**Scope:** Paper / research infrastructure and measured follow-ups. **Live Profile A unchanged** unless a separate live promote clears existing STRICT rules.

**Canonical freeze doc:** [FORWARD_PAPER_FREEZE.md](FORWARD_PAPER_FREEZE.md)

---

## Before lifting freeze (unchanged)

1. Owner **explicitly** ends measure-only freeze (Saturday confirm/deny or manual lift).
2. **Phase 4 MC 200 × 365d** finished (or accepted partial with documented caveat — do not retune from partial).
3. Walk-forward / synthesis per [HANDOFF_GROK_CLAUDE.md](scripts/research/exhaustive_campaign/runs/HANDOFF_GROK_CLAUDE.md) campaign queue.
4. Promote / strategy changes still require **365d STRICT**: beat baseline on **return and Sharpe**, MaxDD not worse by **>1.0pp**. No combo until singles clear.

**Lifting freeze does not, by itself, authorize `.env` retunes or new sleeves.**

---

## After freeze (~2026-08-26+) — ordered action plan

### **#1 — P0 Journal honesty (first infrastructure item)**

Infrastructure / measure honesty only. **Does not unlock promote** or strategy changes by itself.

Audit context (2026-08-17): `paper_journal.csv` is sparse vs `logs/bot_actions.jsonl` (~4–5× more fills in jsonl); optional `log_fn` at call sites; `qty`/`price`/`sleeve` often 0% on trade rows; realized PnL usually missing → $0 attribution; multiline `exit_error` corrupts CSV (`on_bad_lines=skip`). Research (`forward_sleeve_attribution`, `weekly_review`, overnight packs) under-counts.

#### Step 1 — Journal-at-executor on fill

- **Single write point** after Alpaca fill is confirmed (e.g. after `order_fill_details` / `_emit_fill_notification` in `AlpacaExecutor`).
- Emit **`event=fill`** (ground-truth fill row), not only caller-dependent `signal` / `exit`.
- Covers all paths: `execute_order`, `execute_reduce_notional`, `execute_full_exit`, rebalance, VTI trim, game-plan, IPO trim, etc.

#### Step 2 — Minimum row fields

Every `fill` row should include:

| Always | Exits additionally |
|--------|-------------------|
| `timestamp`, `event`, `book`, `symbol`, `side`, `sleeve`, `regime` | `exit_reason` |
| `order_id`, `reason` / `pair_key` | `entry_hour` (ET) |
| `qty`, `price`, `notional` | `realized_pnl_usd`, `realized_pnl_pct` |
| `equity`, `cash` | `is_partial` |

Keep legacy `signal` / `exit` rows during transition if needed; **`fill` is SoT for attribution**.

#### Step 3 — CSV hygiene

- Stop appending multiline JSON / stack traces into journal `notes` (causes wide rows, silent skips).
- Route rich errors to a separate log (e.g. `logs/journal_exit_errors.jsonl`) or one-line sanitized `notes`.
- Run `scripts/maintenance/cleanup_journal_csv.py --backup` after schema change.

#### Step 4 — Optional backfill (freeze window)

- Reconstruct missing rows from `logs/bot_actions.jsonl` (`order_submitted` + `fill` since ~2026-08-12).
- Supplement with Alpaca closed orders API where jsonl gaps exist (Jul–Aug freeze window).
- Mark backfilled rows (`notes=backfill:bot_actions` or `source` column) so research can filter.

#### Step 5 — Verify

Manual or scheduled (Task Scheduler / weekly ops):

```powershell
python scripts/analysis/trade_reconciliation.py --days 7
python scripts/analysis/forward_sleeve_attribution.py --days 14
```

Pass criteria:

- Journal **`fill`** count within reason of `bot_actions` **`fill`** events (same book, same window).
- `forward_sleeve_attribution` shows **non-zero realized PnL** when closed exits exist.
- No new wide-row growth in portal `paper_journal.csv` after deploy.
- Overnight pack §4 entry/exit counts align with Alpaca activity (not “0 / 0” on active days).

**Do not restart live bot for journal feature unless owner requests.** Paper portal book first; live book same code path when validated.

---

### **#2 — Campaign / research queue (after P0 ships or in parallel if MC already done)**

See [HANDOFF_GROK_CLAUDE.md](scripts/research/exhaustive_campaign/runs/HANDOFF_GROK_CLAUDE.md) § Research backlog:

1. MC 200 dedupe / final score
2. Regime-noise honesty check
3. STRICT-leg MC (separate export-dir)
4. Phase 5 walk-forward + synthesis
5. Targeted data completeness (gate series audit — not “more tickers”)

---

### **#3 — Core vs NYSE performance gates (strategy, not infrastructure)**

Only after **P0 journal** so sleeve attribution is trustworthy.

See [FORWARD_PAPER_FREEZE.md](FORWARD_PAPER_FREEZE.md) § Core vs NYSE performance gates (gates A–E). VTI→40% only if all pass + explicit owner lift. **Open % ≠ promote.**

Helper (read-only until gates pass): `python scripts/analysis/core_nyse_performance_gates.py`

---

### **#4 — P1 journal enrichments (after P0 verified)**

Not part of first ship:

- Per-symbol **`skip`** rows (`yield_gated`, `nyse_cooldown`, etc.)
- `conviction`, `garch_multiplier`, `sizing_multiplier` / wisdom `gap_tier` at fill time
- `screener_rank`, `atr_at_entry`, `sim_notional` vs `filled_notional` for slippage studies

---

## Explicit non-goals (journal P0 work)

- **No** `.env` changes, `PAPER_RISK_*`, sleeve caps, Dynamic VTI targets, or live Profile A edits **as part of journal P0**
- **No** new strategies, sleeves, or overlays bundled with journal work
- **No** migrating / rewriting all 18k historical rows unless backfill step (optional) is explicitly run
- **No** changing `on_bad_lines` behavior in production read paths until CSV hygiene (step 3) is in place
- **No** interrupting **MC Phase 4** for journal work

---

## Gate language (for Grok / Claude / weekly confirm-deny)

| Statement | True? |
|-----------|-------|
| Journal P0 is **infrastructure / measure honesty** | Yes |
| Journal P0 **unlocks promote** by itself | **No** |
| Strategy promote still needs **full MC 200 + 365d STRICT** rules | Yes |
| Paper +2.84% during freeze is **allowed noise**, not a mandate to loosen | Yes |
| Fix journal **before** trusting 14d attribution for VTI/NYSE gate decisions | Yes |

---

## Related files

| File | Role |
|------|------|
| [FORWARD_PAPER_FREEZE.md](FORWARD_PAPER_FREEZE.md) | Freeze ops + promote rule + NYSE gates |
| [PAPER_RESEARCH_PROFILE.md](PAPER_RESEARCH_PROFILE.md) | Locked paper stack v1.5.4 |
| [scripts/research/exhaustive_campaign/runs/HANDOFF_GROK_CLAUDE.md](scripts/research/exhaustive_campaign/runs/HANDOFF_GROK_CLAUDE.md) | MC campaign + research backlog |
| `modules/trade_journal.py` | Current CSV writer (to extend post-freeze) |
| `modules/alpaca_executor.py` | Fill poll + `bot_actions` today; journal-at-executor target |
| `scripts/analysis/trade_reconciliation.py` | Alpaca vs journal verify |
| `scripts/analysis/forward_sleeve_attribution.py` | Sleeve PnL (broken until P0) |
| `logs/bot_actions.jsonl` | Shadow fill trail for backfill |
