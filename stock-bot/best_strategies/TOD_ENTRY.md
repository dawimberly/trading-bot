# TOD entry A/B — block 9:30–10:00 ET (done 2026-09-20)

One knob: `block_open_chase` on the 5-minute MA50 research walk
(`scripts/research/backtest_intraday.py`). Not the
`PAPER_MOMENTUM_QUALITY_FIXES` bundle (gap / 1-entry/day / 12–14 ET).

Daily `backtester.py` cannot replay hour gates. Fixed NYSE universe (30 names,
PIT). 5 bps/side (10 bps RT). Gate: return > baseline, Sharpe ≥ baseline,
MaxDD not worse >2pp, **both** disjoint 365d windows. Coverage: 30/30 full-span
after `--refresh`.

## Results

| Window | allow open | block 9:30–10:00 | verdict |
|--------|------------|------------------|---------|
| prior 365d (2024-09-16→2025-09-15) | +0.46% / 0.11 / −11.18% | +0.20% / 0.07 / −11.49% | **FAIL** dRet −0.25pp, dSharpe −0.04 |
| last 365d (2025-09-15→2026-09-15) | +2.61% / 0.37 / −11.58% | +5.69% / 0.56 / −10.94% | PASS dRet +3.09pp, dSharpe +0.20, DD better |

Values are return / Sharpe / MaxDD. Cooldown blocked 3,005 / 4,384 signals.

## Verdict

**KILL.** Last year helps; the prior year does not. No deploy. Do not turn on
`PAPER_MOMENTUM_QUALITY_FIXES` from this either (that is four knobs).

A first pass on stale short caches (prior window = 1 ticker) is void.

Artifact: `scripts/analysis/eval_tod_entry_ab_last.md`
