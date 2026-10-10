# Medium SoT drift (report only — no .env write)

As of 2026-09-20. Runtime SoT is the **portal_bot launch overlay**, not the
book `.env` file. Heartbeat confirms 15 NYSE names on Medium.

## Spec vs disk vs running

| Knob | Pick (`paper_v2_best`) | Disk `.env` | `portal_bot` launch | Heartbeat now |
|------|------------------------|-------------|---------------------|---------------|
| Names | 15 | **10** | **15** | **15** NYSE |
| ATR stop | 2.0x | **2.5** | **2.0** | (launch wins) |
| Hold | 30d | unset (default 30) | 30 | — |
| 1R / exit opt | off | unset | **false** | — |
| Fat-loser | off | false | false | — |
| VTI | 33/67 in old walk; employed **0** | 0 | 0 | sleeve cap 0 |
| Per-name | ~6.7% of 100k walk | **10%** | (env 10% unless overlay) | — |
| Scanners / stat-arb | off | off | off | — |

`PAPER_MEDIUM_STRATEGY` is **unset on disk**. Launch sets it `true`, which is
why `effective_max_active_tickers()` reads 15 even if `.env` says 10.

## What this means

- **Do not “fix” Medium.** The running bot already matches the 15-name / 2.0x
  pick. Editing disk `.env` to 15/2.0 would only remove confusion; it would
  not change live behavior while portal_bot overlays those keys.
- The real drift is **documentation vs disk file**, not vs the executor.
- Per-name 10% on disk is richer than the 15-slot walk’s equal clip. Not
  changed here.

## Aggressive (context for Q1, not a Medium change)

Disk `.env` says `MAX_ACTIVE_TICKERS=25`. `portal_bot` forces Lab concentrated
**8 names / 15% / trail**. Heartbeat **8** NYSE positions. So Q1’s “aggressive
is on 25” was the disk file, not the running book. Name-count STRICT A/B still
measures the scanner stack with a moving slot cap (Lab OFF in the test).
Deploying 15 would also require a `portal_bot.py` overlay change, not only
`.env`.
