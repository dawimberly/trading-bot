# Fat-loser A/B — open −1% drip (done 2026-09-20)

One knob on the NYSE MA70 hold walk (8 names, 30d, 2× ATR). Medium not in
this test. 10 bps round-trip. Alpaca open/prior-close gap on sqlite closes.

ON: if overnight gap ≤ −1%, drip the name to 0.5% of book.

## Results

| Window | OFF book / DD | ON book / DD | trims |
|--------|---------------|--------------|------:|
| 365d (242 RTH) | +36.92% / 13.27% | +3.94% / 5.89% | 222 |
| 1000d (653 RTH) | +66.86% / 18.58% | +7.97% / 9.86% | 387 |

Gap coverage 99.6% / 99.9%.

## Verdict

**KILL.** Shallower drawdown, destroyed return (−33pp / −59pp). Leave
`PAPER_NYSE_FAT_LOSER_ENABLED` off. Portal already forces false. Do not
restore from disk `.env`.

Artifact: `scripts/analysis/eval_fat_loser_ab_last.md`
