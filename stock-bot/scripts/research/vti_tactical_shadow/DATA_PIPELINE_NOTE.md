# Daily bar pipeline finding (2026-08-04 night)

## Why `VTI_daily` is behind

| Fact | Detail |
|------|--------|
| Last good VTI close in SQLite | **368.21 on 2026-07-31** |
| 2026-08-03 | Row present with **NULL** close |
| 2026-08-04 rip (~380) | **Missing** from `VTI_daily` |
| Live refresh path | `RefreshScheduler` → `fetch_and_store()` → **5m tables only** |
| Daily refresh path | Manual / backtest: `python fetch_data.py --daily --days N` |

So lag is expected whenever nobody runs `--daily` after the campaign/backtests last wrote history. Live bots do **not** roll `*_daily` on the 5m cadence.

## Does live/paper care?

**Yes — for RHYME / wisdom regime.**

`run_all` → `resolve_wisdom_regime(data)` → `get_regime_inputs` → `regime_dataframe`, which **prefers `*_daily`** when the cycle is fed a 5m matrix:

```text
live 5m matrix (fresh-ish via RefreshScheduler)
        │
        ▼
regime_dataframe() ──loads──► load_close_matrix(interval="1d")  # can be days stale
        │
        ▼
RHYME / sizing / pause decisions
```

Trading sleeves still use the live 5m matrix for signals. **Regime classification can be on stale daily bars with no freshness gate** — unlike the new VTI tactical shadow logger.

`market_context.py` has **no** `data_fresh` / bar-lag check.

## Extra ops issue tonight

`market_data.db` is often **locked** by the exhaustive campaign / MC workers. Even `mode=ro` reads fail. That blocks shadow `--require-fresh` and can stall anything else hitting SQLite.

## What to do (order)

1. When campaign lock clears: `python fetch_data.py --daily --days 90` (or targeted VTI/SPY refresh).
2. Confirm Aug 3–4 VTI closes look real (~380 area).
3. `python scripts/research/vti_tactical_shadow/run_shadow_once.py --require-fresh`
4. Only then start the shadow-week clock (K1–K5 in `PROMOTE_CRITERIA.md`).
5. Follow-up (separate ticket): add daily freshness warn to live heartbeat / wisdom path — not a silent freeze break.

## Non-conclusion

Empty promote stats ≠ “no edge.” Data layer isn’t ready. Don’t interpret until eligible rows exist.
