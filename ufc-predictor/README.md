# UFC Predictor

Standalone UFC fight prediction and betting-signal pipeline. **Not tied to PythonTrading** — all paths are relative to this project root.

## Project layout

```
UFC-Predictor/                 (e.g. C:\UFC-Predictor)
├── src/                       Python modules + cli_entry.py
├── data/
│   ├── raw/                   fights.csv
│   ├── processed/             fight_features.csv
│   ├── cache/                 odds, event analysis, heartbeat
│   └── logs/
├── models/                    ensemble_winner.joblib
├── dist/                      ufc-predict.exe, ufc-dashboard.exe
├── ufc_betting_bot/           vendored edge/Kelly/backtest helpers
│   └── .env                   API keys (THE_ODDS_API_KEY, webhooks)
├── build_exe.bat
├── build_dashboard.bat
├── config.py
├── main.py
└── README.md
```

Frozen EXEs resolve paths from `dist/` (same folder as the `.exe`): `dist/data/`, `dist/models/`, and load `.env` from **both** `dist/.env` and the **project root** `C:\UFC-Predictor\.env`.

## One canonical copy (avoid duplicates)

| Location | Role |
|----------|------|
| **`C:\UFC-Predictor`** | **Canonical** — edit code, build EXEs, keep `.env` here |
| `PythonTrading\ufc-predictor` | Synced mirror — run `scripts\consolidate_ufc.ps1` after changes |
| `dist\` | Built EXEs only — copies `.env` from project root on build |

Put API keys and `ENABLE_PROPS=true` in **`C:\UFC-Predictor\.env`** (not only `dist\.env`).

```powershell
cd C:\UFC-Predictor
powershell -File scripts\consolidate_ufc.ps1   # sync monorepo copy + fix paths
build_dashboard.bat
```

## Migrate out of PythonTrading

From the old monorepo copy:

```powershell
powershell -File scripts\migrate_standalone.ps1
# or: -Destination C:\UFC-Bot
```

Then work only in the new folder:

```powershell
cd C:\UFC-Predictor
pip install -r requirements.txt
build_dashboard.bat
```

## Quick start

```bash
cd C:\UFC-Predictor
pip install -r requirements.txt
python scripts/preflight.py
python main.py --backtest-2025
python main.py --odds --explain --alerts
```

## EXE builds

```bat
build_exe.bat          rem dist\ufc-predict.exe + data + models + .env
build_dashboard.bat    rem dist\ufc-dashboard.exe (GUI)
```

Runtime assets copied automatically: `data/`, `models/`, `.env`, `ufc_betting_bot/.env`.

## CLI (production)

```bash
dist\ufc-predict.exe "Freedom 250" --odds --explain
dist\ufc-predict.exe --watch --auto-odds --discord
python main.py --preflight
python main.py --watch --auto-odds --dry-run
```

## Architecture

```
data_loader → feature_engineering → model_trainer (LGBM+XGB ensemble)
      → predictor → backtester / risk_manager (Monte Carlo)
      → strategy (Kelly + parlays) → explainability (SHAP)
      → alerts + scheduler (watch mode)
```

| Layer | Modules | Role |
|-------|---------|------|
| Data | `data_loader`, `greco_stats` | Multi-source fights, odds, ufcstats enrichment |
| Model | `model_trainer`, `ensemble`, `predictor` | Calibrated ensemble, conformal intervals |
| Research | `backtester`, `risk_manager` | Walk-forward, 2025 event backtest, MC risk |
| Strategy | `strategy` | Fractional Kelly, card caps, parlays |
| Explain | `explainability`, `fight_brief` | SHAP + rule-based briefs |
| Production | `alerts`, `scheduler`, `circuit_breaker` | Discord/Telegram, watch loop |
| Dashboard | `ufc_dashboard`, `dashboard_service` | GUI + quick odds + auto watch |
| Grok (optional) | `grok_analysis` | Narrative edge on top picks; Kelly multiplier (non-blocking) |

## Grok analysis (optional)

Optional xAI/Grok layer for the **dashboard only** — never blocks refresh, watch, or alerts.

1. Set in `.env`:
   ```env
   GROK_ENABLED=true
   GROK_API_KEY=...          # or XAI_API_KEY
   GROK_MODEL=grok-3-mini    # optional
   ```
2. **Refresh Next Two** to load the card.
3. Click **Grok Analysis** (toolbar or **Grok Analysis** tab).

Grok reviews top moneyline singles and prop lines, returning per-pick **narrative edge**, **crowd positioning**, **invalidation risks**, and a **Kelly adjustment** (default clamp `0.70`–`1.15`). Overview top bets show adjusted Kelly when analysis has run. Results cache under `data/cache/grok_analysis/` for 12h.

| Variable | Default | Purpose |
|----------|---------|---------|
| `GROK_ENABLED` | false | Master switch |
| `GROK_API_KEY` / `XAI_API_KEY` | — | xAI API key |
| `GROK_MODEL` | grok-3-mini | Chat model |
| `GROK_MAX_FIGHTS` | 6 | Max ML picks sent |
| `GROK_MAX_PROPS` | 6 | Max prop lines sent |
| `GROK_KELLY_ADJ_MIN` | 0.70 | Min Kelly multiplier |
| `GROK_KELLY_ADJ_MAX` | 1.15 | Max Kelly multiplier |

## Profiles (`UFC_PROFILE`)

| Profile | Card cap | Daily loss halt | Max drawdown | Min alert edge |
|---------|----------|-----------------|--------------|----------------|
| `research` (default) | 8% | 4% | 15% | 7% |
| `live` | 5% | 2% | 10% | 8% |

Set in `.env`: `UFC_PROFILE=live`

## Safety (ported from trading bot)

- **Daily loss circuit breaker** — blocks new alerts after session loss exceeds profile limit (`src/circuit_breaker.py`)
- **Peak drawdown halt** — pauses alerts when bankroll falls from peak (`risk_manager.DrawdownHalt`)
- **Alert cooldown + fingerprint dedup** — no spam per event
- **Dry-run** — `ALERT_DRY_RUN=true` or `--dry-run`

## Ops artifacts

| File | Purpose |
|------|---------|
| `data/logs/ufc_bot.log` | Daily rotating logs |
| `data/bet_journal.csv` | Signal + alert audit trail |
| `data/cache/heartbeat.json` | Watch loop liveness |
| `data/cache/circuit_breaker_state.json` | Session loss state |
| `data/cache/drawdown_state.json` | Peak bankroll / halt |

## Configuration

Key `.env` variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `UFC_PROFILE` | research | live vs research risk caps |
| `THE_ODDS_API_KEY` | — | Live odds for edge |
| `DISCORD_WEBHOOK` | — | Alert channel |
| `ALERT_MIN_EDGE` | 0.07 | Min edge for alerts |
| `CIRCUIT_BREAKER_ENABLED` | true | Daily loss guard |
| `DRAWDOWN_HALT_ENABLED` | true | Peak DD guard |
| `MC_SIMULATIONS` | 10000 | Monte Carlo paths |

## Design notes

- **Leakage-safe features**: rolling stats use only prior fights.
- **No LLM in hot paths**: `fight_brief` composes SHAP + edge + MC heuristically; optional Grok runs only on explicit dashboard click.
- **Separate from PythonTrading**: no merge with Alpaca bot; patterns only.
- **Sibling `ufc_betting_bot`**: shared edge math for backtest reports.

## Tests

```bash
python -m pytest tests/ -q
```
