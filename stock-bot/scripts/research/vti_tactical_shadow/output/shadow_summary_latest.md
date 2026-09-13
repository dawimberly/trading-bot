# VTI tactical $5k — shadow summary

Generated: 2026-08-05 03:39 UTC

## Provenance

- Rows in main log: 1
- Promote-eligible (fresh sqlite): 1
- Stale/non-eligible inside main log: 0 (should be 0)
- Rows in stale sidecar: 1
- data_source counts (main): {'sqlite': 1}
- Staleness rate vs main: 0.0%

## Actions (eligible only)

{'blocked': 1}
Prod vs shadow-equity RHYME mismatches (eligible): 0/1

## Known-event check (Aug 2026 rip)

- Eligible rows touching rip window: 1
- Of those with action=would_trim: 0
- Pass hint: need ≥1 fresh `would_trim` on/after the rip with gate D/C

## Pre-registered promote criteria

See `PROMOTE_CRITERIA.md` — do not invent thresholds after looking at results.

## Latest eligible event

```json
{
  "ts_utc": "2026-08-05 03:39:49 UTC",
  "symbol": "VTI",
  "bar_date": "2026-08-05",
  "last_close": 380.82,
  "data_source": "sqlite",
  "data_fresh": true,
  "promote_eligible": true,
  "stale_reason": "",
  "ret_1d_pct": 0.0,
  "ret_2d_pct": 1.8671,
  "from_local_high_pct": 0.0,
  "action": "blocked",
  "reason": "prod_RHYME_A_block_chase",
  "notional_cap": 5000.0,
  "size_mult": 0.85,
  "sized_notional": 4250.0,
  "rhyme_prod": "RHYME_A: Euphoric_Volatility",
  "rhyme_shadow_equity": "RHYME_A: Euphoric_Volatility",
  "rhyme_prod_letter": "A",
  "rhyme_shadow_letter": "A",
  "vol_score_prod": 0.021072,
  "vol_score_equity_shadow": 0.02246,
  "vol_score_fixed_shadow": 0.022252,
  "garch_ok": false,
  "garch_ratio": null,
  "garch_size_mult_raw": 1.0,
  "arima_enabled_for_decision": false,
  "arima_direction": "n/a",
  "arima_mult": null,
  "bh_equity_5k": 5000.0,
  "shadow_mark_5k": 5000.0,
  "notes": "ARIMA=short_history; load_err=none; RHYME equity-split SHADOW-only"
}
```

## Latest any (may be stale — do not use for promote)

```json
{
  "ts_utc": "2026-08-05 03:39:49 UTC",
  "symbol": "VTI",
  "bar_date": "2026-08-05",
  "last_close": 380.82,
  "data_source": "sqlite",
  "data_fresh": true,
  "promote_eligible": true,
  "stale_reason": "",
  "ret_1d_pct": 0.0,
  "ret_2d_pct": 1.8671,
  "from_local_high_pct": 0.0,
  "action": "blocked",
  "reason": "prod_RHYME_A_block_chase",
  "notional_cap": 5000.0,
  "size_mult": 0.85,
  "sized_notional": 4250.0,
  "rhyme_prod": "RHYME_A: Euphoric_Volatility",
  "rhyme_shadow_equity": "RHYME_A: Euphoric_Volatility",
  "rhyme_prod_letter": "A",
  "rhyme_shadow_letter": "A",
  "vol_score_prod": 0.021072,
  "vol_score_equity_shadow": 0.02246,
  "vol_score_fixed_shadow": 0.022252,
  "garch_ok": false,
  "garch_ratio": null,
  "garch_size_mult_raw": 1.0,
  "arima_enabled_for_decision": false,
  "arima_direction": "n/a",
  "arima_mult": null,
  "bh_equity_5k": 5000.0,
  "shadow_mark_5k": 5000.0,
  "notes": "ARIMA=short_history; load_err=none; RHYME equity-split SHADOW-only"
}
```
