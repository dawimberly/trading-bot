# UFC Predictor

Standalone UFC fight prediction and betting-signal pipeline. **Not tied to PythonTrading** — all paths are relative to this project root.

## Project layout

```
ufc-predictor/
├── src/                       Python modules + dashboard
├── data/
│   ├── raw/                   fights.csv
│   ├── processed/             fight_features.csv
│   ├── cache/                 odds, fighter cache, background snapshots
│   └── logs/
├── models/                    ensemble_winner.joblib
├── dist/                      ufc-predict.exe, ufc-dashboard.exe
├── ufc_betting_bot/           vendored edge/Kelly/backtest helpers
├── build_exe.bat
├── build_dashboard.bat
├── config.py
├── main.py
└── README.md
```

Frozen EXEs resolve paths from `dist/` (same folder as the `.exe`): `dist/data/`, `dist/models/`, and load `.env` from **both** `dist/.env` and the **project root** `.env`.

## Quick start

```bash
cd ufc-predictor
pip install -r requirements.txt
python scripts/preflight.py
python main.py --backtest-2025
python main.py --odds --explain --alerts
python src/ufc_dashboard.py
```

## Dashboard

Launch: `python src/ufc_dashboard.py` or `dist/ufc-dashboard.exe`

| Tab | Purpose |
|-----|---------|
| **Overview** | Top recommended bets, card summary, fight table |
| **BetNow / DraftKings / MyBookie** | Per-book odds, edges, parlays |
| **Props** | Method/rounds/decision prop singles (per book) |
| **Next Two Cards** | Upcoming event cards |
| **Risk Analysis** | Monte Carlo drawdown / ruin stats |
| **Grok Analysis** | Optional narrative edge + Kelly multiplier |

**Toolbar:** Profile (Paper/Live), Refresh Next Two, Quick Odds, Process New Card, **Grok Analysis**, Auto Watch.

**Budget Manager** (header bar): total bankroll, per-card budget, per-book balances and enable toggles. Stakes on Overview and Props scale to the card pool. Persists to `data/budget.json` and syncs `INITIAL_BANKROLL` / `CARD_BUDGET` to `.env`.

**Background runner** (`src/background_runner.py`): scheduled full analysis at midnight/startup; lightweight odds refresh when snapshot is fresh. Caches under `data/cache/background/` for instant dashboard load.

```bash
python src/background_runner.py --mode auto --trigger startup
```

## EXE builds

```bat
build_exe.bat              rem dist\ufc-predict.exe + data + models + .env
build_dashboard.bat        rem windowed dist\ufc-dashboard.exe (no console)
build_dashboard.bat --debug-build   rem console EXE for troubleshooting
```

Runtime assets copied automatically: `data/`, `models/`, `.env`.

Release builds use `console=False` in `ufc-dashboard.spec`. Pass `--debug` at runtime to allocate a console for logs.

## CLI (production)

```bash
dist\ufc-predict.exe "Freedom 250" --odds --explain
dist\ufc-predict.exe --watch --auto-odds --discord
python main.py --preflight
python main.py --watch --auto-odds --dry-run
python main.py --backtest-2025
```

## Architecture

```
data_loader → feature_engineering (+ fighter_cache) → model_trainer (LGBM+XGB)
      → predictor (shared singleton) → backtester / risk_manager (Monte Carlo)
      → strategy (Kelly + parlays + budget) → explainability (SHAP)
      → alerts + scheduler (watch mode) + background_runner
      → ufc_dashboard (optional Grok overlay)
```

| Layer | Modules | Role |
|-------|---------|------|
| Data | `data_loader`, `greco_stats` | Multi-source fights, odds, ufcstats enrichment |
| Features | `feature_engineering`, `fighter_cache` | Leakage-safe rolling stats; incremental card inference |
| Model | `model_trainer`, `ensemble`, `predictor`, `model_cache` | Calibrated ensemble; process-wide predictor singleton |
| Research | `backtester`, `risk_manager` | Walk-forward, 2025 event backtest, MC risk |
| Strategy | `strategy`, `props`, `parlay_builder` | Fractional Kelly, card caps, prop singles/parlays |
| Explain | `explainability`, `fight_brief` | SHAP + rule-based briefs |
| Production | `alerts`, `scheduler`, `circuit_breaker`, `background_runner` | Discord/Telegram, watch loop, cached snapshots |
| Dashboard | `ufc_dashboard`, `dashboard_service` | GUI + quick odds + auto watch + budget |
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
3. Click **Grok Analysis** (toolbar or tab).

Grok reviews top moneyline singles and prop lines, returning per-pick **narrative edge**, **crowd positioning**, **invalidation risks**, and a **Kelly adjustment** (default clamp `0.70`–`1.15`). Overview top bets show adjusted Kelly when analysis has run. Results cache under `data/cache/grok_analysis/` for 12h.

## Profiles (`UFC_PROFILE`)

| Profile | Use | Card cap (typical) | Kelly | Min alert edge |
|---------|-----|-------------------|-------|----------------|
| `paper` (default) | Simulation / dashboard | Higher % of bankroll | 0.35 | 3.5% |
| `live` | Real money | Hard USD cap ($12 default) | 0.12 | 8% |

Legacy env value `research` maps to `paper`. Set in `.env`: `UFC_PROFILE=live`

## Safety

- **Daily loss circuit breaker** — blocks new alerts after session loss exceeds profile limit (`src/circuit_breaker.py`)
- **Peak drawdown halt** — pauses alerts when bankroll falls from peak (`risk_manager.DrawdownHalt`)
- **Alert cooldown + fingerprint dedup** — no spam per event
- **Dry-run** — `ALERT_DRY_RUN=true` or `--dry-run`
- **Live small-bankroll warnings** — dashboard flags thin rolls in Live mode

## Ops artifacts

| File | Purpose |
|------|---------|
| `data/logs/ufc_bot.log` | Daily rotating logs |
| `data/logs/background_runner.log` | Scheduled runner |
| `data/bet_journal.csv` | Signal + alert audit trail |
| `data/budget.json` | Dashboard bankroll / book toggles |
| `data/cache/background/manifest.json` | Background snapshot metadata |
| `data/cache/fighter_cache_meta.json` | Incremental feature cache |
| `data/cache/heartbeat.json` | Watch loop liveness |
| `data/cache/circuit_breaker_state.json` | Session loss state |
| `data/cache/drawdown_state.json` | Peak bankroll / halt |

## Configuration

Copy `.env.example` → `.env`. Key variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `UFC_PROFILE` | paper | paper vs live risk caps |
| `INITIAL_BANKROLL` | 75 | Starting bankroll |
| `CARD_BUDGET` | 12 | Max stake pool per card |
| `THE_ODDS_API_KEY` | — | Live odds for edge |
| `ENABLE_PROPS` | false | Prop betting tabs |
| `MYBOOKIE_ENABLED` | true | MyBookie book + props |
| `GROK_ENABLED` | false | Dashboard Grok tab |
| `DISCORD_WEBHOOK` | — | Alert channel |
| `CIRCUIT_BREAKER_ENABLED` | true | Daily loss guard |
| `DRAWDOWN_HALT_ENABLED` | true | Peak DD guard |

After bootstrap (CLI or frozen EXE), `config.refresh_runtime_env()` re-reads `.env` so keys and flags apply without rebuilding.

## Design notes

- **Leakage-safe features**: rolling stats use only prior fights; fighter cache avoids full-history rebuilds each card.
- **No LLM in hot paths**: optional Grok runs only on explicit dashboard click.
- **Separate from PythonTrading**: no merge with Alpaca bot; patterns only.
- **Sibling `ufc_betting_bot`**: shared edge math for backtest reports.

## Tests

```bash
python -m pytest tests/ -q
```
