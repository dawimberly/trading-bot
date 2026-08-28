# PythonTrading

Monorepo for local trading tools. **Only `stock-bot/` is the active trading project.**

| Folder | Purpose |
|--------|---------|
| [`stock-bot/`](stock-bot/) | Alpaca stock/crypto fund bot + portal (active) |
| [`ufc-predictor/`](ufc-predictor/) | UFC fight model + data pipeline (separate project) |

**Repo (personal):** [github.com/dawimberly/trading-bot](https://github.com/dawimberly/trading-bot)  
**Repo (group / infinite-robots):** [github.com/infinite-robots/python-trading](https://github.com/infinite-robots/python-trading)

| Remote | GitHub | Who |
|--------|--------|-----|
| `origin` (fetch) | `dawimberly/trading-bot` | Personal |
| `origin` (push ×2) | personal **and** `infinite-robots/python-trading` | One Commit/Push hits both |
| `infinite-robots` | `infinite-robots/python-trading` | Optional explicit group remote |

**Commit button:** `.githooks` updates root `README.md` **Recent changes** from the commit subject, then `post-commit` pushes `origin` (both GitHubs). Skip with `[skip-readme]` / `[no-push]`, or `SKIP_README_HOOK=1` / `SKIP_AUTO_PUSH=1`.

## Recent changes

- **2026-08-28** — Listen for freeze HOLD/CONFIRM on Telegram and stop live banners from advertising a VTI core.
- **2026-08-20** — Point recon at the portal journal and add AI-burst hedge v1.
- **2026-08-12** — Update README Recent changes for 2026-08-12.
- **2026-08-12** — Document commit-hook enablement in README recent changes.
- **2026-08-11** — Enable commit hooks: auto README Recent changes and dual-remote push.
- **2026-08-11** — Commit hooks: auto README Recent changes + dual-remote push via `origin`.

## Quick start

**Daily routine (owner, Live + Paper):** double-click **`Start_Bot_and_Dashboard.bat`** at the repo root. See [stock-bot/README.md — Daily usage](stock-bot/README.md#daily-usage-recommended).

**Environment:** copy [`stock-bot/.env.example`](stock-bot/.env.example) → `stock-bot/.env` (placeholders only — never commit real keys).

First-time / manual launch:

```bat
cd stock-bot
copy .env.example .env
python scripts\account\preflight.py
```

**Python 3.11 venv (owner):** from `stock-bot/`, run `scripts\setup_venv.bat` once → `C:\Users\Owner\PythonTrading\venv311`; daily `scripts\activate_venv.bat`. Details: [stock-bot/README — First-time setup](stock-bot/README.md#first-time-setup).

**Do not use for daily ops:** `start.bat`, `launch.bat`, `launch_both.bat`, or `Weinstein-Trading-Bot.exe` — they start a single bot with the wrong heartbeat paths for the portal dashboard.

Build frozen EXEs (optional): `stock-bot\build_all.bat`

Friend setup: [FRIENDS.md](FRIENDS.md) · Full bot docs: [stock-bot/README.md](stock-bot/README.md) — see **“What the bot is set to do (runtime defaults)”** for live vs paper vs research-only features (good to paste into Grok with `python status.py` output).

**Paper lock (v1.5.4):** Smart Dynamic VTI **40–75%** (≥40% floor) · **SPY satellite OFF** · portfolio guards (≤8%/name, dust &lt;$10, max 25) · equity `run_nyse_momentum_and_stat_arb` · Telegram fills ≥$5 + error watcher ON. Details: [`stock-bot/PAPER_RESEARCH_PROFILE.md`](stock-bot/PAPER_RESEARCH_PROFILE.md). **Forward freeze** (~2–4 weeks from 2026-07-29): measure-only — [`stock-bot/FORWARD_PAPER_FREEZE.md`](stock-bot/FORWARD_PAPER_FREEZE.md); known-good tag `paper-v154-spy-off-strict`; daily/Saturday freeze memos via Telegram. **Live Conservative** stays separate (~85% VTI; Profile A unchanged). Branch `ollama-fallback-test` includes `main` + WIP `f46f4b5`.

## Runtime data layout

Legacy state files from repo root were moved into:

- `stock-bot/data/` — runtime state (heartbeats, journals, wisdom state, etc.)
- `stock-bot/archive/` — backtest CSVs, jsonl backups, build logs, duplicate scripts

A timestamped backup at `backup_root_purge_2026-06-20/` holds copies of everything moved for easy rollback.
