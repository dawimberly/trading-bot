# STRICT vs FULL on employed Lab 8 / VTI=0 (365d, 2026-09-20)

Research only. FULL overlays are not point-in-time. No `.env` write.

Both legs: `TRADING_BOOK_ID=alpaca_paper`, Lab 8, VTI=0, fat-loser off, costs on,
thinking off, crypto off.

- **STRICT:** RVOL / ORB / catalyst / insider / hist-news / dyn-univ / Buffett / vol-breakout OFF
- **FULL:** those left ON (lookahead possible)

## Results

| leg | return | Sharpe | MaxDD | trades | NYSE |
|-----|-------:|-------:|------:|-------:|-----:|
| STRICT | +1.60% | 0.16 | -11.90% | 1482 | 221 |
| FULL | +7.66% | 0.57 | -12.38% | 1435 | 205 |
| VTI B&H | +14.82% | | | | |

Window: 2025-11-14 → 2026-09-20 (310 bars after warmup).

## Verdict

FULL is better on return and Sharpe; MaxDD is only 0.48pp worse (inside the 1pp
full-stack gate). **Do not promote scanners** — FULL is not PIT. **Do not cut
them either** — the old STRICT>FULL result was a different VTI mix and does not
hold on this employed stack.

Runtime aggressive already launches with scanners ON (`apply_realistic_research_env`).
Stay put. Second window not required for a non-deploy.

Artifact: `scripts/analysis/eval_strict_vs_full_employed_last.md`
