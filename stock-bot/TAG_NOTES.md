# Tag notes: `paper-v154-spy-off-strict`

**Tag:** `paper-v154-spy-off-strict`  
**Created:** 2026-07-29  
**Branch:** `ollama-fallback-test` (at tag time)  
**Purpose:** Known-good rollback point for paper Realistic Research v1.5.4 during the forward freeze.

## Locked state

| Item | State |
|------|--------|
| Paper version | Realistic Research **v1.5.4** FINAL LOCK |
| SPY satellite (paper) | **OFF** (`SPY_SLEEVE_CAP_PCT=0` / `PAPER_SPY_MAX_EXPOSURE_PCT=0`) |
| Dyn VTI | **LOCKED 40–75%** (≥40% hard floor) |
| NYSE entry hygiene | **ON** |
| STRICT PIT research | Overlays off unless allowlisted; thinking off; hygiene ON |
| Conviction top-N | **OFF** (`PAPER_NYSE_TOP_N=0`) |
| Exit `exit_h45_tight` | **Not promoted** (365d lost) |
| Live Profile A | **Unchanged** (SPY trend ON; 365d live-shaped tied) |
| Forward freeze | **ON** — see `FORWARD_PAPER_FREEZE.md` (~2–4 weeks from 2026-07-29) |

## Evidence pointers

- Paper SPY-off 365d: `scripts/analysis/exit_spy_ab_365_last.md`
- Live SPY A/B 365d (keep ON): `scripts/analysis/live_spy_ab_365_last.md`
- Conviction top-N reject: `scripts/analysis/eval_strict_conviction_last.md`
- Rejected knobs registry: `config.REJECTED_STRICT_RESEARCH_KNOBS`
- Ops freeze: `FORWARD_PAPER_FREEZE.md`
- Attribution (measure only): `scripts/analysis/forward_sleeve_attribution.py`

## Rollback

```powershell
git checkout paper-v154-spy-off-strict
# or create a branch from the tag:
git switch -c restore/paper-v154-spy-off-strict paper-v154-spy-off-strict
```

Do **not** treat this tag as a live promote. Paper freeze remains measure-only until you explicitly end it.
