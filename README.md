# trading-bot

Monorepo for local trading and betting tools. Clone once, run what you need on your own PC.

**Repo:** [github.com/dawimberly/trading-bot](https://github.com/dawimberly/trading-bot)

| Folder | What it is | Get started |
|--------|------------|-------------|
| [`stock-bot/`](stock-bot/) | Alpaca stock/crypto fund bot + portal | `cd stock-bot` → `friend_setup.bat` |
| [`ufc-predictor/`](ufc-predictor/) | UFC fight model + data pipeline | Used by UFC betting bot |
| [`ufc_betting_bot/`](ufc_betting_bot/) | UFC value betting (dry-run) | `cd ufc_betting_bot` → `friend_setup.bat` |

**Friend setup guide:** [FRIENDS.md](FRIENDS.md)

## Quick start (stock bot)

```powershell
git clone https://github.com/dawimberly/trading-bot.git
cd trading-bot\stock-bot
friend_setup.bat
```

Root launchers (`launch.bat`, `friend_setup.bat`) forward into `stock-bot/` for backward compatibility.

Full docs: [stock-bot/README.md](stock-bot/README.md)
