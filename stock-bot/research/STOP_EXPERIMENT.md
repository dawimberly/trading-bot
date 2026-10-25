# Stop experiment — 2026-10-25

Dan approved one stop change on each book. This file records that experiment. The code change is dated 2026-10-25. The books start using it when Dan merges this pull request and redeploys on his PC. This change does not deploy anything.

## Arms

| Book | What changed | What the book does now |
| --- | --- | --- |
| `alpaca_paper` (legacy aggressive Lab) | Loss stop removed | Exit only on the existing 30-day hold (`PAPER_LAB_MAX_HOLD_DAYS`) |
| `alpaca_paper_v2` (medium paper) | Loss stop widened to 10% | Hard exit at −10%. The 2× ATR stop cannot close a name before that |
| `alpaca_live` | Loss stop widened to 10% | Hard exit at −10%. An ATR loss stop cannot close a name before that |

Compare **alpaca_live** with **alpaca_paper_v2** over the same dates after the redeploy. Both use a 10% loss stop, on different books. That pair is the comparison. `alpaca_paper` is the third arm: no loss stop, hold only. It is not the pair.

## What was already true on this stack

Lab’s disaster stop was already 10% (`PAPER_LAB_DISASTER_PCT`), plus an 8% trail after +10%, a half sale at +20%, and the A2B1 next-open flatten. Those exits are suspended while `PAPER_LAB_HOLD_ONLY=true`, so the hold is the only exit. The A2B1 settings stay in the Lab environment and come back if `PAPER_LAB_HOLD_ONLY` is turned off. Name count, the 15% name cap, the ranker, and the book halt are unchanged.

Medium’s binding stop was 2× ATR. The old −5% / −10% reeval had already been moved to −50%, so a flat 5% stop was not what closed Medium names. This experiment sets the hard loss exit at 10% and will not let 2× ATR close a name on a smaller loss. `ATR_STOP_MULTIPLIER` stays 2.0. The 15-name cap and the 30-day hold stay.

Live’s percentage stop was the shared `STOP_LOSS_PCT` of 5%. It is 10% for that book. Profit trails are unchanged. Shorts and crypto keep their own stops.

The seven-minute quote gate and `ufc-predictor` are untouched.

## Why this is an experiment

A pick-quality check on a June 2026 survivor list found that a 5% stop gave back about five percentage points per name versus holding 30 sessions, and that the stopped book finished behind VTI while the unstopped book finished ahead. That tape cannot prove an edge: the list looks ahead, and both windows were already used. This run asks whether that stop result shows up on the three books Dan actually runs. It is not a promotion rule.

## Reading it later

Start the clock at the redeploy, not at this commit. After both 10% books have a stretch of closed trades, compare live with medium paper on average exit result, how often the 10% stop fires, and how the 30-day hold does on the Lab book. Do not retune the ranker, size, halt, or quote gate from the first few trades.
