# Handoff brief: Exhaustive campaign / Monte Carlo backtest (PythonTrading)

Copy-paste context for Grok / Claude. Read-only research. Do **not** invent live/paper `.env` changes unless the owner explicitly lifts freeze.

## Snapshot 2026-08-17 ~15:40 CT (send this)

As-of Monday 2026-08-17. **Do not retune from this.** Freeze still ON through **~2026-08-26** (9 calendar / 7 trading days; soft date, not auto-lift).

### Books now

| Book | Equity | Cash | VTI core | NYSE | Notes |
|------|--------|------|----------|------|--------|
| Paper `alpaca_paper` | **$97,316** | $22,970 (23.6%) | $48,907 (50.3%) | $19,423 / 25 names (~20%) | Freeze start 2026-07-29 **$94,625** → **+$2,691 / +2.84%** in ~19 calendar days |
| Live `alpaca_live` | **$306.34** | $17.64 (5.8%) | $258.62 (85%) | $30.04 / 7 names | Jul 29 pack ~$295 → ~**+$11 / +3.8%** |

Paper session 2026-08-17: open equity ~$97,411 → $97,316 (−0.10%). Regime now **RHYME_D**. Wisdom governor_stress, sizing ×0.5. SPY/crypto/metal = 0. Paper SPY satellite **OFF**. Live SPY trend **ON** but not held.

### Forward paper (14d attribution, generated 2026-08-17 04:15 UTC)

Window 2026-08-03 → 08-16: **$95,273 → $97,726 (+2.57%)**. Journal fills 47 / exits 19 (NYSE only). Realized PnL in that report **$0** (journal is not a complete blotter). vs scaled 90d STRICT envelope +2.43% (delta +0.14pp — noise). **SPY fills = 0** (lock ok).

### Today’s tape (journal `signal`/`exit` only — undercounts reduces)

| Day | Paper buy/sell | Live buy/sell |
|-----|----------------|---------------|
| Thu 8/13 | 22 / 12 ATR | 71 / 44 |
| Fri 8/14 | 25 / 7 ATR | 55 / 35 |
| **Mon 8/17** | **42 / 14** smart-ATR | **32 / 26** ATR |

Not a Monday rebalance. NYSE MA50 + ATR churn. Live $5 clips; same-name round-trips (e.g. SPCX sold+rebought in 4s) before same-day reentry block restart ~13:37 CT. NYSE nearly full on both books.

**Journal honesty:** executor does **not** log every fill. `log_signal` after some entries; `log_exit` on ATR/stop path only. Rebalance / `execute_reduce_notional` / IPO trim often missing. Overnight pack “Entries: 0 \| Exits: 0” can be a lie. Ticker **S** (SentinelOne) had ~$809 then stub ~$13 with **zero** journal rows. Do not treat journal as Alpaca SoT.

### Monte Carlo Phase 4 (**COMPLETE 200/200** — still do not promote)

`campaign_20260811T221624Z_full/mc/` · paper-aggressive, deep indicators, 365d window **2025-10-24 → 2026-08-11** (292 bars), 200 runs, noise 0.01 / regime 0.1, seed 42.

Finished **2026-08-18 22:26 CT** · ~30.9 min/run · ~102.9 hours CPU.

| Metric | mean | median | p5 | p95 |
|--------|-----:|-------:|---:|----:|
| Return % | 21.16 | **4.71** | **−43.83** | 145.94 |
| Sharpe | 0.34 | **0.33** | −4.38 | 5.05 |
| MaxDD % | −18.32 | **−13.47** | **−44.84** | −4.45 |

P(return>0) **52.5%** · P(Sharpe>1) **37.5%** · fat right tail, ugly left tail. SoT: `runs.jsonl`. Next: dedupe → regime-noise check → STRICT-leg MC → Phase 5 walk-forward. **No .env from this MC. Freeze still ON. Journal P0 does not unlock promote.**

### Locked STRICT evidence (already finished; two different 365d windows)

Paper-aggressive dual eval (window 2025-05-28 → 2026-08-05), VTI B&H +3.24%:

| Mode | Return | Sharpe | MaxDD |
|------|--------|--------|-------|
| STRICT PIT | **−1.42%** | −0.09 | −9.73% |
| FULL overlays | −2.45% | −0.19 | −10.29% |

STRICT beat FULL → overlays look like drag. **Do not promote live from FULL.**

Older STRICT multi-window panel (generated 2026-07-27, different end date 2026-07-26): 90d +15.63% Sharpe 2.44; 180d +25.83% / 2.31; 365d +29.16% / 1.49. **Do not mix with the −1.42% 365d row** — different windows.

TOD: best entry **midday @ 11:00**; stat-arb **last_hour**; open/first 30m weak.

### Ask Grok/Claude for (allowed)

- Interpret **complete 200/200** MC shape (median vs mean, left tail) without retune / `.env` advice
- Post-freeze **proposals only**: journal-at-executor, dynamic cash bands, STRICT-leg MC
- What would falsify the stack after freeze (~Aug 26) + walk-forward + 365d STRICT promote rule

### Do not ask them to

- Change paper/live `.env`, lift freeze, add sleeves, or “fix” VTI 50% vs NYSE 20% from 14d packs
- Promote from 14d forward paper, today’s churn, or this MC (even at 200/200)
- Treat journal fills as complete P&L

### Grok read-back received 2026-08-17 15:50 CT — AGREED

Freeze stays on. No `.env` / promote / live changes from this packet.

- Paper **+2.84%** = allowed freeze noise, not a mandate to loosen
- Live ~$306 = still noise scale
- Journal not a blotter; S / $0 realized = honesty flags
- Today = MA50+ATR ops noise, not edge
- MC **200/200 COMPLETE** 2026-08-18 22:26 CT: median **+4.7%**, mean **+21%** (skew), p5 **~−44%**, P(>0) **52.5%** — fragile center + fat tails; **design for survival**, not p95. Still no promote / `.env`.
- STRICT vs FULL: STRICT **less bad** (−1.4% vs −2.5%); **not** the Jul 26 +29% window
- Post-freeze only (not this week): journal-at-executor; cash bands after full MC + gates; STRICT-first research default
- **When 200/200 completes:** refresh this file and score for real. **Done 2026-08-18 22:26 CT.** Next is campaign queue #1–4, still freeze-on / no `.env`.

---

## What this project is

Desktop Python trading stack (`stock-bot/`):
- **Paper book** (~$97k Alpaca paper) — “Realistic Research” / paper-aggressive Profile B
- **Live book** (~$300) — conservative Profile A
- Multi-sleeve: VTI core, NYSE momentum, optional SPY/crypto/metal/stat-arb, cash buffer (~15%)
- Research campaign under `scripts/research/exhaustive_campaign/` — **does not place trades**

## Freeze (critical)

**FORWARD_PAPER_FREEZE** is on: measure only.
- No paper/live strategy retunes from partial MC or 14d forward noise
- No new sleeves, no live Profile A changes from this work
- Dynamic cash sleeve is a **post-campaign design idea**, not implemented yet

## Campaign structure (full profile)

| Phase | Name | Status |
|------:|------|--------|
| 1 | compare-final 365d + regimes | Done earlier |
| 2 | STRICT vs FULL 365d | Done |
| 3 | Time-of-day (TOD) 365d | Done |
| 4 | **Monte Carlo 200 × 365d** | **IN PROGRESS** |
| 5 | Walk-forward 4 | Pending |
| 6–7 | Heavy experiments | Pending (full profile) |

Orchestrator: `run_campaign.py`  
Watchdog: `campaign_watchdog.py` (Task Scheduler ~5 min)  
Keep-awake: prevents Windows sleep during long runs

## Phase 2 snapshot (already finished)

Paper-aggressive, 365d, no-thinking dual eval:

| Mode | Return | Sharpe | MaxDD |
|------|--------|--------|-------|
| STRICT PIT | −1.42% | −0.09 | −9.73% |
| FULL overlays | −2.45% | −0.19 | −10.29% |

**STRICT beat FULL** on this window → overlays look like drag/noise; do not promote live from FULL alone. Best Paper 365d was weak vs VTI — not promote-ready from that alone.

## Phase 3 snapshot (TOD)

- Best entry bucket: **midday @ 11:00**
- Stat arb best: **last_hour**
- Open / first 30m: weak — observe, don’t force entries

## Phase 4 — what “the big backtest” is

**Not** a single historical backtest. It is **Monte Carlo stress** of the paper-aggressive stack:

```text
python scripts/analysis/monte_carlo_backtest.py
  --paper-aggressive
  --days 365
  --mc-runs 200
  --export-dir <artifacts>/mc
```

### Method
1. Load ~365d daily history (deep indicators on; **not** `--fast-mode`)
2. For each of **200** runs: perturb market data (price noise + regime noise)
3. Re-run full paper-aggressive backtest on perturbed path
4. Record return %, Sharpe, max drawdown (+ diagnostics)

### Current run config (meta.json)
- Profile: `paper-aggressive, deep-indicators`
- Window: **2025-10-24 → 2026-08-11** (~292 sim bars)
- `noise_level=0.01`, `regime_noise=0.1`, `seed=42`
- `fast_mode=false` (full universe / screener path — **slow**)
- Wall clock: historically ~**1 hour per run** on this HP ZBook → ~**1 week** for 200 runs

### Why so slow
Each run is a full multi-sleeve simulation; dynamic/screener path can expand toward **~180+ tickers**. Machine: ~8 logical CPUs / 24GB RAM; MC is mostly **serial** (one fat process). Not parallelized.

### Durability / logging
After a power-loss restart (2026-08-11), export-dir is on:

`stock-bot/scripts/research/exhaustive_campaign/runs/artifacts/campaign_20260811T221624Z_full/mc/`

| File | Meaning |
|------|---------|
| `runs.jsonl` | One JSON object per **completed** MC run (append + fsync) |
| `summary.md` / `summary.json` | Rolling stats |
| `results.json` | Partial then final |
| `progress.txt` / `in_progress.json` | Live progress |
| `phase4_mc_200_365.log` | Huge verbose console log |

Index: `.../runs/LOGGING.md`

**Important:** An earlier MC attempt reached ~70/200 but **had no JSONL** (started before export-dir). That progress was lost on power cut. Current job restarted **0/200** with logging.

## Ops context (owner machine)
- HP ZBook 15 G2; charger issues; using universal **18.5V HP tip (M12)** as temporary (OEM wants **19.5V**)
- Keep AC connected; sleep inhibited via keep_awake
- Paper/live bots are separate processes; campaign must not be confused with trading

## Paper trading notes (not MC outputs)
- SPCX held, roughly **+20%** MTM, small speculative sleeve (~$1.1k)
- Exit-opt trail arms ~**+50%** then ~35% pullback — **+20% is not an auto-sell**
- ~15% cash is intentional **cash buffer**, not “unused ammo for SPCX”
- Bot does **not** sell other names to concentrate into winners (sleeve caps + speculative caps)

## What to help the owner with
- Interpret MC distributions once `runs.jsonl` / `summary.md` have enough runs
- Design **post-freeze** ideas (e.g. dynamic cash sleeve bands) as proposals only
- Optional later: thorough MC on **traded/holdings universe only** as a **separate** faster job (not replacing full MC)
- Do **not** recommend changing live/paper `.env` from partial results
- Do **not** claim SPCX Monday-open edge — no solid Fri→Mon study for +15–20% single names

## Status line (update when asking again)
- Phase **4 / MC 200 COMPLETE** 2026-08-18 22:26 CT · median **+4.7%** · mean **+21%** · p5 **−44%** · P(>0) **52.5%**
- Freeze ON through **~2026-08-26** (soft). Next: dedupe jsonl, regime-noise check, STRICT-leg MC, walk-forward
- Journal P0 still post-freeze. No `.env` / promote from this MC.

---

## Research backlog / later campaigns (append-only)

**Rules:** FORWARD_PAPER_FREEZE stays ON until ~2026-08-26. Phase 4 MC **200/200 is done** — do **not** change `.env` / trading from these results. Next campaign items below are still research-only.

### Post-freeze infrastructure (first after ~2026-08-26)

**Canonical checklist:** [POST_FREEZE_CHECKLIST.md](../../../POST_FREEZE_CHECKLIST.md) (repo root)

| Priority | Item | Notes |
|----------|------|-------|
| **P0 #1** | **Journal-at-executor on fill** | First infrastructure ship; not a trading retune; does not unlock promote |
| P0 verify | `trade_reconciliation.py --days 7` + attribution non-zero realized | After deploy |
| P1+ | Skip rows, conviction, garch_mult at fill | Only after P0 verified |
| Strategy | Core vs NYSE gates A–E | Only after journal trustworthy |

### Queue order (research campaign — do in this order)

| # | When | Item | Notes |
|--:|------|------|-------|
| 0 | **DONE** 2026-08-18 22:26 CT | Phase 4 MC **200 × 365d** | SoT = `mc/runs.jsonl` + `summary.md` COMPLETE |
| 1 | After #0 | **Dedupe / final score** current MC | Unique run ids; medians/percentiles from clean jsonl |
| 2 | After #0–1 | **Regime-noise honesty check** | Do `regime_counts` actually move across runs? (partial audit already: ~3 signatures / 45 runs) |
| 3 | After #0–2 | **STRICT-leg MC** (separate export-dir job) | Honesty vs FULL stack; no live promote from FULL alone |
| 4 | After #0–3 | Campaign **Phase 5** walk-forward (+ later phases 6–7 / synthesis as orchestrator defines) | Still research-only |
| 5 | **Later campaign** (after #1–4) | **Targeted data completeness for better MC / backtests** | See below — **not** “more tickers” |
| — | **Post-freeze P0** | **Journal-at-executor** (fill SoT) | **[POST_FREEZE_CHECKLIST.md](../../../POST_FREEZE_CHECKLIST.md) § #1 — first infra after freeze lift; before gate-driven VTI/NYSE decisions |
| — | Post-freeze design only | Dynamic VTI band (~75–85%), etc. | Proposals only until freeze lifts + P0 journal shipped |

### Later campaign — Targeted data completeness (not “more tickers”)

**Title:** Targeted data completeness for better MC / backtests  

**Context**
- Full-stack daily MC already has deep price history, VTI diagnostics, `runs.jsonl`
- Primary gap is robust edge + method (regime noise honesty, STRICT-leg MC), not empty disks
- More data only helps when it feeds gates the bot actually uses

**P1 — Audit (read-only)**
- List symbols/series live/paper can request vs what’s reliably in `market_data.db` / deep cache
- Flag delist/empty yfinance failures that affect MC
- List macro/regime inputs used in live gates (VIX, TNX/TLT, FRED-style) and whether backtest/MC loads them

**P2 — Source map (docs only)**  
Add a short section to `PAPER_RESEARCH_PROFILE.md` or `docs/DATA_SOURCES.md`:

| Need | Source | Priority |
|------|--------|----------|
| OHLCV daily | Existing DB + yfinance/Alpaca | have |
| Deep indicator history | `*_deep.pkl` path | have |
| VIX / rates / FRED macros | FRED + Yahoo **if gate uses them** | add if missing |
| Intraday 5m | Alpaca (TOD/ORB research only) | later |
| Insider | EDGAR RSS (existing) | have |
| News | proxies only; don’t block | low |

Explicit: **no** “download 500 more tickers” item.

**P3 — Only after MC 200 + method items (#1–3)**
1. Dedupe/final score (if not already automated) — covered by queue #1  
2. Regime-noise honesty — queue #2  
3. STRICT-leg MC — queue #3  
4. **Then** fetch any **missing gate series** from P1 into cache in a form MC/backtest can reuse (parquet/SQLite/pkl — match existing patterns)

**Out of scope for this backlog item**
- Cloud migration, new sleeves, Dynamic VTI trim, interrupting Phase 4  
- Paid data vendors  
- Rewriting `monte_carlo_backtest.py` as part of *this* item (method flags belong under queue #2–3, not data completeness)
