# trading-bot

**Alpaca stock fund bot** — clone once, run on your own PC. UFC betting files were removed; this repo is **trading bot only** (`stock-bot/`).

**Repo (personal):** [github.com/dawimberly/trading-bot](https://github.com/dawimberly/trading-bot)  
**Repo (group / infinite-robots):** [github.com/infinite-robots/python-trading](https://github.com/infinite-robots/python-trading)

Both remotes were synced to the same commits on `main` and all cursor branches (starting point: `03abb6c`). They are **independent from here on** — updates to one are not automatic on the other.

| Remote | GitHub | Who |
|--------|--------|-----|
| `origin` | `dawimberly/trading-bot` | Your sandbox — default `git push` / `git pull` |
| `infinite-robots` | `infinite-robots/python-trading` | Group clone — push when you want to share |

**You (after clone with both remotes):** `git pull origin main` · `git push origin main` · share to group: `git push infinite-robots main`  
**Group:** `git clone https://github.com/infinite-robots/python-trading.git` only — do not use the personal URL.

Add the org remote once: `git remote add infinite-robots https://github.com/infinite-robots/python-trading.git`

| Folder | What it is | Get started |
|--------|------------|-------------|
| [`stock-bot/`](stock-bot/) | Alpaca live + paper fund bot + portal | `cd stock-bot` → `friend_setup.bat` |

**Friend setup guide:** [FRIENDS.md](FRIENDS.md) (stock bot)

**Environment:** copy [`stock-bot/.env.example`](stock-bot/.env.example) → `stock-bot/.env` (placeholders only — never commit real keys).

## Quick start (stock bot)

```powershell
git clone https://github.com/dawimberly/trading-bot.git
cd trading-bot\stock-bot
friend_setup.bat
```

Root launchers (`launch.bat`, `friend_setup.bat`) forward into `stock-bot/` for backward compatibility.

Full docs: [stock-bot/README.md](stock-bot/README.md) — see **“Final recommended configuration”** and **“What the bot is set to do”** for Live Profile A (90% VTI, crypto OFF, thinking OFF) vs Best Paper Bot v2.1 (crypto OFF, dynamic universe ON, thinking opt-in). Good to paste into Grok with `python status.py` output.
