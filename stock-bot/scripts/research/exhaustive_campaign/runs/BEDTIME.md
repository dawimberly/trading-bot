# Bedtime checklist — exhaustive campaign (2026-08-02 night)

Target: still running (or completed) when you get off work tomorrow evening.

## Right now (verified at install)

- Phase 1 `compare-final` is **live** and writing logs
- Watchdog task: `PythonTradingExhaustiveCampaignWatchdog` every **5 minutes**
- Keep-awake helper + AC sleep/hibernate timeouts set to **never**
- If the campaign process dies, watchdog **auto-restarts** with `--resume`
- If status becomes `halted` (critical phase failed 3x), it will **not** blind-restart — check logs

## What you should do before sleep

1. Leave the PC **on** and **plugged in** (desktop AC).
2. You can lock the screen. Do **not** choose Sleep / Hibernate / Shut down.
3. Leave yourself logged in as `Owner` (task is Interactive logon).

## When you get home tomorrow

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\Owner\PythonTrading\stock-bot\scripts\research\exhaustive_campaign\status_campaign.ps1
```

Or open:
- `stock-bot\scripts\research\exhaustive_campaign\runs\STATUS.md`
- `stock-bot\scripts\research\exhaustive_campaign\runs\campaign_master.log`

## Expected timeline

Full chain can take **many hours** (MC 200 + full experiment are the bulk).
By tomorrow evening you should see either:
- `status=completed` in `campaign_state.json`, or
- still `running` on a later phase (4–9), which is OK — let it finish

Freeze unchanged — research only; nothing wires into paper/live.
