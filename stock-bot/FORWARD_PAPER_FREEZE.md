# Forward paper freeze (ops)

**Start:** 2026-07-29  
**Duration:** 2–4 weeks (through ~2026-08-26)  
**Scope:** Paper / Realistic Research v1.5.4 only. **Live Profile A unchanged.**

## Goal

Let the locked book learn in live paper time. No new sleeves, exits, overlays, or “one more indicator” A/Bs until the freeze ends.

## Locked stack (do not retune)

| Item | Paper state | Evidence |
|------|-------------|----------|
| SPY satellite | **OFF** | 365d STRICT `spy_off` +7.03pp / Sharpe 1.12→1.47 |
| Dyn VTI | **LOCKED 40–75%** (≥40% floor) | v1.5.4 lock |
| NYSE hygiene | **ON** | max 2 adds/symbol, same-day reentry block, $25 min |
| Exit ladder `exit_h45_tight` | **NOT promoted** | 365d −1.53pp vs baseline |
| Conviction top-N | **OFF** (`PAPER_NYSE_TOP_N=0`) | STRICT top3/top5 lost to diversified baseline |
| Live SPY trend | **ON** (live only) | 365d live-shaped A/B tied; promote rule failed |

## Ops cadence

1. Keep `run_paper_bot.py` running (restart once after SPY-off lock if not already).
2. **Daily hygiene (automated):** `freeze_daily_hygiene_memo.py` — small cleanup candidates at end of day; Telegram + optional `--open`.
3. **Weekly confirm/deny (automated):** Saturday `freeze_weekly_confirm_deny.py` — plan for you to CONFIRM / DENY / HOLD; default = freeze continues.
4. **Weekly pack** (Saturday): `python scripts/analysis/weekly_review.py --skip-backtest` during freeze — measure only, no hypothesis A/B.
5. **Sleeve attribution** (any day): `python scripts/analysis/forward_sleeve_attribution.py` → `scripts/analysis/forward_sleeve_attr_last.md`.
6. Journal + dashboard: treat short-sample Sharpe as noise; do not retune from 2–3% DD.

Install Task Scheduler (opens MD like weekly review):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\analysis\install_freeze_ops_tasks.ps1
```

Env (see `.env.example`): `FREEZE_OPS_ENABLED`, `FREEZE_DAILY_*`, `FREEZE_WEEKLY_*`, `FREEZE_OPS_TELEGRAM`.

**Analyst LLM (optional):** `FREEZE_OPS_OLLAMA=true` adds a non-binding narrative to the weekly plan only — never auto-applies knobs.

## Promote rule (unchanged)

Paper or live change only if a STRICT leg beats baseline on **return and Sharpe**, MaxDD not worse by **>1.0pp**, on **365d** (live-shaped for live). No combo until singles clear.

## After freeze

Resume research only from measured gaps (attribution / journal honesty / PIT as-of fixes) — not from inventing new alpha knobs.
