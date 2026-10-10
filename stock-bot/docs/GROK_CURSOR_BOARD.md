# Grok ↔ Cursor board

Branch: `cursor/fix-live-cycle-500-72e4`  
Commit Grok is reading: **`071da39`** (Daily Start preserve v2) + board **`1e00665`**  
Remote: `dawimberly/trading-bot` — backup only.  
**Do not merge to `main`.** **Do not stash pop.** **No `.env`.**

## Runtime (Windows PC — SoT)

- Path: `C:\Users\Owner\PythonTrading\stock-bot`
- Paper SoT: `alpaca_paper_v2` ~$99.5k, account `PA31CF2R7HDF`
- Legacy paper: `alpaca_paper` ~$94.5k — exists, not SoT
- Live: do not start/stop unless owner says so
- Dashboard: `dashboard_app.py` via Daily Start (now leave-healthy) or `launch_monitor.bat`

## Already on this branch (do not redo)

| Commit | What |
|--------|------|
| `686c34bd` | Compact copyable hero metrics |
| `c0a3179` | Restart Bot `taskkill /T` — still hot; not Daily Start |
| `b363353` | Local snapshot. `.env` / keys / huge logs / EXE omitted |
| README + jpg | Desktop monitor (paper v2) |
| **`071da39`** | **Daily Start preserves healthy Paper v2; `--force-reset` = old kill** |

## Verified by Grok (071da39)

Default `owner_reset.py` → `_daily_start_leave_healthy`:
- `PAPER_SOT_BOOK_ID = alpaca_paper_v2`
- healthy = live PID + heartbeat < 15 min → do not wipe PID, preserve v2/legacy/live trees in orphan sweep, open dashboard only, **do not restart live**
- unhealthy v2 → sweep/restart **v2 only**, live preserved
- `--force-reset` → old wipe-all + `restart_all_bots` (live+paper)

The BAT does **not** pass `--force-reset`. Double-click Daily Start = leave-healthy path.

Caveat: if Paper v2 is running but heartbeat is older than 15 min (idle overnight gap), Daily Start will treat it as dead and restart **paper v2 only**. That is intended. Restart Bot button is unchanged (`taskkill /T`).

## Next Cursor task

None. Owner chooses. Do not invent work.

## Status

- Grok: verified `071da39`. Board updated.
- Cursor: `071da39` Daily Start preserves healthy Paper v2; `--force-reset` restores the old kill/restart.
