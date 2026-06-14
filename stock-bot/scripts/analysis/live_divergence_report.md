# Live vs Backtest Divergence Report

Generated: 2026-06-08  
Stack: **current recommended** — `WISDOM_MODE=dynamic`, game-plan **yield-gate-only**, halt resume 8% + liquidate, crypto vol-only gate.  
Live book: **Alpaca live ~$100** (small-account mode: 1% risk, $10 max order, **90% VTI**).  
Paper book: **~$100k** (same bot code, different API keys until 2026-06-07).

Commands run:

```bash
python scripts/analysis/live_vs_backtest_snapshot.py --refresh-eval --reconcile --days 365
python scripts/analysis/live_vs_backtest_snapshot.py --reconcile --days 1000
python backtester.py --days 365
python backtester.py --days 1000
```

Raw snapshots: `scripts/analysis/live_vs_backtest_365d.json`, `scripts/analysis/live_vs_backtest_1000d.json`.

---

## 1. Overall live vs sim return gap (pp)

### Headline number is **not real**

| Window | Live (journal) | Sim (dynamic) | Gap (pp) | Verdict |
|--------|---------------:|--------------:|---------:|---------|
| 30d (scorecard default) | **-99.90%** | -0.27% | **-99.63** | **Invalid** |
| 365d snapshot | **-99.90%** | +0.75% | **-100.65** | **Invalid** |
| 1000d snapshot | **-99.90%** | +0.75% | **-100.65** | **Invalid** |

**Cause:** On **2026-06-07** the wisdom journal switched from paper equity (~$98,281) to live equity ($100). `live_metrics()` uses first/last **daily** equity in the window, so it reports a fictional -99.9% “return.” Alpaca live equity is **$99.55** (-0.45% from $100 deposit). **None of the -99 pp gap is strategy performance.**

### Segmented windows (honest)

| Segment | Dates | Live return | Sim dynamic | Gap (pp) | Notes |
|---------|-------|------------:|------------:|---------:|-------|
| Paper only | 2026-05-25 → 2026-06-06 | **-1.89%** | -0.33% | **-1.56** | ~$100k paper; 257 `signal` rows in journal |
| Live only | 2026-06-07 → 2026-06-08 | **-0.50%** | +1.09%* | **-1.59** | 2 calendar days; 2 Alpaca fills |
| VTI buy & hold (same 14d window) | 2026-05-25 → 2026-06-08 | — | — | — | **-0.63%** |

\*Sim +1.09% over Jun 7–8 is **not comparable** to a fresh $100 live book: the evaluator slices a **$10k backtest** equity curve that already holds positions; live started flat then bought VTI/GLD on Jun 8.

### Long-horizon sim (signal quality reference — no live track record yet)

Fund backtester on recommended stack (`backtester.py`, $10k start, **80% VTI**, not small-account 90%):

| Horizon | Sim return | Sharpe | Max DD | VTI B&H |
|---------|----------:|-------:|-------:|--------:|
| 365d | **+10.87%** | 0.51 | -22.15% | (in-run benchmark) |
| 1000d | **+33.39%** | 0.55 | -20.51% | +71.26% |

Aligned evaluator window (2025-06-08 → 2026-06-08, dynamic mode): **+11.74%**, Sharpe **0.66**, 83 orders.

Independent stack validation (`final_recommended_stack_comparison.md`, 80% VTI): 365d **+19.95%** / Sharpe **1.25**; 1000d **+45.95%** / Sharpe **1.04**.

**Bottom line:** There is **no meaningful long-horizon live gap** yet — only **~2 days** of true live data. The only measurable short-window gaps are **~1.5 pp** (paper segment) and **~0.5%** absolute on live (mostly VTI price move).

---

## 2. Top 3 sources of divergence (estimated impact)

### #1 — Journal / account contamination (**~99.6 pp — accounting only**)

- Wisdom journal spans **paper $100k** and **live $100** in one CSV.
- Scorecard and snapshot script treat the window as one equity series → **-99.9% live return**.
- Trade reconciliation: **257 paper signals** vs **2 live Alpaca fills** → **0 matched trades**.
- **Impact:** Makes all automated `live_minus_active_sim_pp` metrics **wrong** until journals are split by book or filtered (`equity < $500` post-switch).

### #2 — Scale & portfolio construction mismatch (**~1–2 pp short windows; structural long-term**)

| Dimension | Live ($100) | Sim ($10k default) |
|-----------|-------------|-------------------|
| VTI core | **90%** (`SMALL_ACCOUNT_VTI_CORE_PCT`) | **80%** (`VTI_CORE_PCT`) |
| Risk / order | 1% risk, **$10 max**, min notional floors | 2% risk, **$2k** notionals in paper journal |
| Active sleeve budget | ~$10 total (SPY/crypto/NYSE caps ~$4.4 / $2 / $2) | Full 20% sleeve on $10k |
| Defensive wisdom | **0.5×** sizing (gap tier defensive, web -0.69) | Sim applies dynamic pauses but not identical micro-sizing |

- Paper segment gap **-1.56 pp** is consistent with **different capital scale + defensive sizing**, not a broken signal engine.
- On live, theoretical satellite notional ≈ **$1.00** (1% of $100); with **0.5×** wisdom → **$0.50** target; **GLD filled at $1.49** (share/min-notional floor).
- **Estimated impact:** **0.5–1.5 pp** on short windows; **larger tracking error vs 80/20 sim** over months because live is effectively a **90% VTI + micro-satellite** fund.

### #3 — Bar cadence & session timing (**~0.3–1.0 pp; grows with trade count**)

| Layer | Live | Sim |
|-------|------|-----|
| Signal bars | **5m** intraday (equity scans 09:35–16:00 ET) | **Daily** close |
| VTI rebalance | Session open; drift band 2% | Daily rebalance logic |
| Overnight | **crypto_only** (heartbeat: phase `overnight`, `equity_session_open: false`) | No session split |
| Live fills | VTI **2026-06-08 13:33:31** ET ($89.99), GLD 13:33:33 ($1.49) | Assumes close-price fills |

- Live **-0.50%** over 2 days ≈ **VTI +0.30%** plus small GLD/cash drag — execution is **passive-index dominated**, not satellite alpha.
- Sim over the same 2 days shows **+1.09%** because it continues an **existing** simulated book; apples-to-oranges.
- **Slippage / fill quality:** With only **2 fills**, average slippage vs journal/sim is **undefined**. VTI fill **$89.99 vs $90.00 target → ~1 bp** on core; no evidence of bad market fills.
- **Estimated impact:** **<1 pp** so far; will matter more once NYSE/SPY/crypto satellites trade at size on a **$300** account.

---

### Honest split: execution vs signal quality

| Component | Share of observed gap | Evidence |
|-----------|----------------------|----------|
| **Accounting / data** | **~100%** of -99.6 pp headline | Equity reset paper → live |
| **Signal quality** | **~0%** of headline; **~0–1 pp** on valid segments | Paper -1.89% vs sim -0.33%; live ≈ VTI |
| **Execution / microstructure** | **~0%** so far (2 fills) | VTI within 1 bp of target |
| **Structural mismatch** (90/10 live vs 80/20 sim, $100 vs $10k) | **Dominant** for future tracking error | Not yet measurable in returns |

**We cannot conclude the live stack is underperforming signals.** We *can* conclude reporting is broken across account switch, and live is **too small / too young** for Sharpe-level inference.

---

## 3. Specific recommendations

### Fix reporting first (high priority)

1. **Split journals by book** (`alpaca_live` vs `alpaca_paper` under `data/portal/users/.../books/`) or filter `live_metrics()` to `equity < $500` after `2026-06-07`.
2. **Reconciliation scope:** Run `trade_reconciliation` only on the active book’s journal; expect **0 matches** when paper signals are reconciled against live Alpaca.
3. **Scorecard copy:** Stop surfacing “Stay on dynamic — matches best rolling sim (-99.9% live)” — that string is a **bug**, not insight.

### Backtest / sim alignment (medium priority)

4. **Small-account backtest profile:** Add `$100 / $300` mode — 90% VTI, 1% risk, $10 max order, `MIN_NOTIONAL` floors — so sim return is comparable to live.
5. **Slippage model (when satellites trade):** Start with **5–10 bps** on NYSE market orders and **2–3 bps** on VTI MOC/limit — paper-only until calibrated from matched fills. *Not needed for current 2-fill sample.*
6. **Limit-first experiment (#2):** Paper only — grid shows little Sharpe upside; live should stay market for VTI core until limit logic is validated.

### Live execution tuning (low urgency at $100)

7. **VTI rebalance:** Drift **0.01%** vs **2%** band — **no change**. Threshold is fine; first deploy correctly bought ~90% VTI.
8. **Defensive wisdom 0.5×:** Working as designed (gap -0.69, defensive tier). At $100 this **suppresses satellites** — desirable. Revisit when equity **> $500** and caps scale up.
9. **Session timing:** Optional: log **signal bar timestamp vs fill timestamp** per order for future slippage attribution (5m signal vs daily sim).
10. **Weekend bar-age preflight:** Keep fix for Friday-bar false positive so live cycles aren’t skipped unnecessarily.

### Do **not** change on live yet

- **NYSE overlap**, **adaptive chunk**, **co-fire**, **SPY MA exit** — see §4.

---

## 4. Advanced flags — paper first?

From `sharpe_flag_grid_results.md` and `final_recommended_stack_comparison.md` (80% VTI, dynamic, yield-gate-only):

| Flag | 365d Sharpe Δ vs baseline | Recommendation |
|------|---------------------------|----------------|
| `NYSE_OVERLAP` / overlap | **0.00** | **Paper only** — no default uplift |
| `SPY_MA_EXIT` | **0.00** | **Paper only** |
| `ADAPTIVE_CHUNK` | **0.00** alone | **Paper only** |
| `ADAPTIVE_CHUNK` + co-fire | **-0.06** | **Do not enable live** |
| `DYNAMIC_SLEEVE_CAPS` | Not in backtester yet | **Paper only** until sim exists |

**Verdict:** Keep live on the **recommended baseline**. Use **Alpaca paper** to A/B overlap, adaptive chunk, and co-fire if you want marginal experiments — grid shows **no Sharpe gain** for overlap/SPY exit and **harm** for co-fire combo. Live $100–300 should remain **90% VTI ballast + defensive dynamic wisdom**.

---

## Appendix — Live snapshot facts (2026-06-08)

| Metric | Value |
|--------|------|
| Alpaca equity | $99.55 |
| Positions | VTI **$89.55** (89.9%), GLD **$1.49**, cash **$8.51** |
| VTI core cap | 90% ($89.55) — on target |
| Wisdom | dynamic, **sizing_multiplier 0.5**, gap_tier **defensive**, paused 21.6% of cycles (live era) |
| Scan phase | overnight, crypto_only (equity session closed) |
| Alpaca fills (live era) | 2 (VTI buy, GLD buy) |
| Paper signals in 30d window | 257 (all unmatched to live Alpaca) |
| `DYNAMIC_SLEEVE_CAPS_ENABLED` | false (default) |

---

## Summary

| Question | Answer |
|----------|--------|
| Overall gap? | **-99.6 pp reported → ignore.** True live: **-0.45%** from $100 deposit. |
| Why? | **Paper/live journal merge**, not trading losses. |
| Execution or signals? | **Neither explains -99 pp.** Valid segments: **~1.5 pp** — mostly **scale/cadence**, not bad fills. |
| Enable overlap/adaptive on live? | **No — paper only.** |
| Next step? | **Segment journals**, add **small-account backtest**, accumulate **≥30 live days** before re-running this report.
