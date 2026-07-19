# Paper / Research Profile (Realistic Research v1.5.4 — **FINAL LOCK**)

**Audience:** Profile B — `alpaca_paper` / `--paper-aggressive` research book only. Live Profile A unchanged.

**Status:** Final lock for Monday / production-ready paper. Do not invent version bumps past **1.5.4**.

**Version:** `REALISTIC_RESEARCH_VERSION = "1.5.4"` (locked in `config.py` via `enforce_realistic_research_profile()`)

**Tagline:** `v1.5.4 — Sector-Aware Portfolio Constructor`

**Locked stack (confirm at startup):** GARCH paper ON / live OFF · Smart Dynamic VTI ON (35–75%) · optional VTI floor ON paper · SPY-like boost ON paper · Daily Profit Banking ON · RHYME primary · HMM soft-only · Stat Arb quality ON · ARIMA OFF

### 365d tune final lock (paper defaults)

Evidence: `scripts/analysis/_v154_tune_ab_365_recommendations.txt` (365d paper-aggressive, HMM off).

| Feature | Paper default | Rationale |
|---------|---------------|-----------|
| **Stat Arb v1.5.4 quality** | **ON (locked)** | +1.45pp return / +0.07 Sharpe vs fill-rate baseline; better DD |
| **ARIMA / ARIMA–GARCH hybrid** | **OFF** (`ARIMA_ENABLED=false`) | ON worse (−0.25pp / −0.01 Sharpe); hybrid may stay true for later opt-in |
| **Dynamic VTI optional floor** | **ON** (20%/0% path) | Flat this window; no harm; live OFF unless `DYNAMIC_VTI_OPTIONAL_LIVE` |
| **SPY-like boost** | **ON** (paper) | Flat this window; live OFF unless `SPY_LIKE_BOOST_LIVE_ENABLED` |

Keep existing locks: RHYME primary, HMM soft-only, GARCH paper ON / live OFF, Daily Banking, scanners, shorts.

```
>>> PAPER BOT: Realistic Research v1.5.4 (Aggressive) | v1.5.4 — Sector-Aware Portfolio Constructor | Live Bot: Conservative 85% VTI
>>> v1.5.4 — Sector-Aware Portfolio Constructor <<<
>>> REALISTIC RESEARCH v1.5.4 (LOCKED) — Smart Dynamic VTI (35-75%) + Portfolio Constructor + RVOL/ORB/Catalyst/ATR + ... | Paper Bot Default <<<
>>> Smart Dynamic VTI 52% — strong NYSE momentum + gold strength + insider cluster buys   # example startup line
>>> Portfolio Constructor — active x1.10 | stat arb x1.15 | short willingness x0.85 — broad sector rotation + insider cluster buys
>>> SMART DYNAMIC VTI DEFAULT — 35%-75% VTI | drivers: NYSE/metals momentum, sector regime, insider clusters, bubble/Buffett, regime, VTI vs SPY
>>> RVOL + ORB + Catalyst + ATR + Conviction + MTF + Exits + Corr Guard + Shorts + Stat Arb <<<
```

---

## v1.5.4 full feature set (locked)

| Feature | Default | Env keys |
|---------|---------|----------|
| **Smart Dynamic VTI core** | **35–75%** multi-signal allocator (default ON); optional floor →20%/0% on SPY-like strength | `PAPER_DYNAMIC_VTI=true`, `DYNAMIC_VTI_PAPER_FLOOR`, `DYNAMIC_VTI_OPTIONAL_ENABLED`, `SPY_LIKE_UNIVERSE` |
| **Portfolio constructor** | Sector-regime tilts on active sleeve / stat arb / short willingness (default ON) | `PORTFOLIO_CONSTRUCTOR_ENABLED`, `PORTFOLIO_ACTIVE_SLEEVE_MULT_FLOOR/CEILING`, `PORTFOLIO_STAT_ARB_MULT_FLOOR/CEILING`, `PORTFOLIO_SHORT_WILLINGNESS_FLOOR/CEILING` |
| **RVOL Scanner** | ON, min **2.0×**, boost @ **2.5×** | `RVOL_SCANNER_ENABLED`, `RVOL_MIN_THRESHOLD`, `RVOL_MOMENTUM_BOOST_THRESHOLD` |
| **ORB Scanner** | ON, **30m** range, RVOL ≥ **2.0×** | `ORB_ENABLED`, `ORB_BREAKOUT_MINUTES`, `ORB_RVOL_MIN` |
| **Catalyst Scoring** | ON, min **65**, boost @ **70** | `CATALYST_SCORING_ENABLED`, `CATALYST_MIN_SCORE` |
| **ATR Sizing** | ON, **14d** ATR, **2.0×** stop, **4%** cap | `ATR_SIZING_ENABLED`, `ATR_PERIOD`, `ATR_RISK_MULTIPLE`, `ATR_MAX_SIZE_PCT` |
| **Conviction sizing** | ON, **0.4×–1.8×** by signal strength | `CONVICTION_SIZING_ENABLED`, `CONVICTION_MIN_SCALE`, `CONVICTION_MAX_SCALE` |
| **Multi-timeframe** | ON, 5m/daily/weekly align ≥ **0.65** | `MULTI_TIMEFRAME_ENABLED`, `MULTI_TIMEFRAME_MIN_ALIGNMENT` |
| **Exit optimization** | ON, partial @ **1R**, dynamic trail, max **35** bars | `EXIT_OPTIMIZATION_ENABLED`, `PARTIAL_EXIT_RR`, `TRAIL_ARM_PCT` |
| **Correlation guard** | ON, max portfolio corr **0.65** | `CORRELATION_GUARD_ENABLED`, `MAX_PORTFOLIO_CORR` |
| **Insider monitor** | ON, cluster ≥2 buyers | `INSIDER_MONITOR_ENABLED`, `INSIDER_CLUSTER_MIN_BUYERS` |
| **Insider boosts** | ON — momentum / stat arb / shorts + VTI allocator tilt | `INSIDER_SIGNAL_BOOST_ENABLED` |
| **Protective shorts** | **8–18%** gross, RR **1.6:1** | `PROTECTIVE_SHORT_MIN_PCT`, `PROTECTIVE_SHORT_MAX_PCT` |
| **Sector shorts** | weak sectors + bubble≥55, ≤8%/name | `SECTOR_SHORT_ENABLED`, `SECTOR_SHORT_MAX_PCT` |
| **Stat arb** | **8–12** pairs (expand when low no_room), corr≥**0.68**, coint p&lt;0.12, Z **2.1–2.7**, RR **1.7:1**, trail **45%/30%**, partial@**1.2R**, ADV **$50M**, conviction **0.6–1.4×**, hold 35b, 7% cap + vol scaling | `PAPER_STAT_ARB_*`, `STAT_ARB_SLEEVE_CAP_*` |
| **Thinking engine** | ON (enriched Ollama context + backtest heuristic tilts) | `PAPER_THINKING_ENGINE_ENABLED` |
| **Tail risk** | vol ceiling 17%, DD tiers, RHYME_B cuts | `TAIL_RISK_CONTROLS_ENABLED`, `PAPER_VOL_CEILING_PCT` |
| **Bot Health Score** | 0–100, dashboard pill | computed at runtime |
| **Strategy performance** | 10 strategies, 30d rolling SQLite | `STRATEGY_METRICS_DB` |
| **Heartbeat watchdog** | ON, 90s cycle cap + supervisor auto-restart | `HEARTBEAT_WATCHDOG_*`, `PAPER_SUPERVISOR_AUTORESTART` |
| **Historical news** | backtest headline proxy | `HISTORICAL_NEWS_ENABLED`, `HISTORICAL_NEWS_CACHE_DIR` |
| **Dynamic Felix/social** | ON when **RHYME_E** or **bubble ≥ 65**; OFF in **RHYME_C/D** (manual override available). Live off. | `FELIX_SOCIAL_DYNAMIC_ENABLED`, `FELIX_SOCIAL_DYNAMIC_BUBBLE_THRESHOLD`, `FELIX_SOCIAL_MANUAL_OVERRIDE` |
| **Markov HMM regime** | **RHYME primary locked.** 5-state `GaussianHMM` soft-signal only (Dynamic VTI, sizing, shorts, conviction). `MARKOV_HMM_PRIMARY_REGIME=false` by default. Primary replacement is research-only. Falls back to RHYME if fit fails. Live off. | `MARKOV_HMM_ENABLED`, `MARKOV_HMM_PRIMARY_REGIME`, `HMM_N_STATES` |
| **Time-of-day analysis** | Session buckets (open / first_30m / mid_morning / midday / last_hour / close) — win rate, Sharpe, Stat Arb edges. Feeds Markov soft-signals. Live off. | `TIME_OF_DAY_ANALYSIS`, `TIME_OF_DAY_LIVE_ENABLED` |
| **Daily Profit Banking** | Bank when day gain ≥**0.8%**; risk **×0.4** + VTI boost for rest of day; reset **30m after open**. Live off. | `DAILY_BANK_ENABLED`, `DAILY_BANK_THRESHOLD_PCT`, `DAILY_BANK_RISK_MULT` |
| **GARCH vol sizing** | **Locked paper ON / live OFF.** GARCH(1,1) next-day σ → size **×0.55–1.0** + VTI nudge (high vol → smaller; default never sizes up). 365d lock rationale: ON beat OFF (~+1.06pp return, +0.04 Sharpe, MaxDD ≈ flat). | `GARCH_VOL_ENABLED`, `GARCH_VOL_LIVE_ENABLED`, `GARCH_VOL_MULT_*`, `GARCH_VOL_VTI_*` |
| **ARIMA / ARIMA–GARCH hybrid** | **Optional** (default **OFF** — **365d final lock**). ARIMA mean sign → boost **×1.08**; with `ARIMA_GARCH_HYBRID=true` (default when ARIMA on), high GARCH vol **scales down** the mean excess (does not re-multiply full GARCH size). Cap 1.15 / floor 0.55. Enforce forces OFF unless env-explicit. Paper only; live off. | `ARIMA_ENABLED`, `ARIMA_GARCH_HYBRID`, `ARIMA_WINDOW`, `ARIMA_BOOST_MULT` |
| **Smart ATR Stops** | Default **2.0×** ATR stop; at **−5%** unrealized: high conviction (RVOL>2.5 / catalyst>70 / insider cluster) → tighten **1.0×**, else cut size **50%**; at **−10%** hard full exit. Live off. | `PAPER_SMART_STOPS`, `ATR_STOP_MULTIPLIER`, `ATR_TIGHTEN_MULTIPLIER`, `STOP_LOSS_REEVAL_PCTS` |

### Dynamic Felix / social sleeve (paper)

Creator-macro sleeve (Felix / Andrei transcripts → GLD / XLE / SPY / cash) is **not always-on** under Realistic Research. With `FELIX_SOCIAL_DYNAMIC_ENABLED=true` (paper default):

| Condition | Sleeve |
|-----------|--------|
| `RHYME_E` (bearish decline) | **ON** |
| `bubble_score_100 >= 65` (and not RHYME_C/D) | **ON** |
| `RHYME_C` or `RHYME_D` | **OFF** (unless `FELIX_SOCIAL_MANUAL_OVERRIDE=true` or explicit `PAPER_SOCIAL_SLEEVE_ENABLED=true`) |
| Live Profile A | Always **off** (`SOCIAL_SLEEVE_ENABLED` default false; dynamic gate is paper-only) |

Startup banner: `>>> Felix/social: dynamic (ON/OFF based on regime) <<<`

### Markov HMM regime (paper)

**RHYME is primary regime detection (locked).** HMM is soft-signal only under Realistic Research (`MARKOV_HMM_PRIMARY_REGIME=false`).

5-state `hmmlearn.GaussianHMM` trained on rolling daily features (SPY returns/vol, VIX proxy, volume z, sentiment, bubble, insider). Emits next-day probs over RHYME_A–E and soft-signals:

- **Dynamic VTI** — higher core when bear/panic predicted
- **Sizing** — de-risk multiplier on regime sizing
- **Shorts** — boost gross/notional when bear/panic predicted
- **Conviction** — blends into regime conviction component

Falls back to current RHYME if hmmlearn is missing or fit fails. Live off unless `MARKOV_HMM_LIVE_ENABLED=true`. Optional HMM primary (`MARKOV_HMM_PRIMARY_REGIME=true`) is research-only — enable only after a 365d 3-way compare clearly beats RHYME.

3-way compare (RHYME | HMM soft | HMM primary):

```powershell
python backtester.py --days 365 --paper-aggressive --compare-markov-hmm
```

Startup: `>>> Regime lock: RHYME primary | HMM soft-signal only (MARKOV_HMM_PRIMARY_REGIME=false) <<<`

### Time-of-day predictability (paper)

`modules/time_of_day.py` owns session buckets and metrics. Markov HMM consumes `tod_bucket` as an observation and blends entry/Stat Arb multipliers from the cached 365d edge table.

| Bucket | ET window |
|--------|-----------|
| `open` | 9:30–9:45 |
| `first_30m` | 9:30–10:00 |
| `mid_morning` | 10:00–11:30 |
| `midday` | 11:30–14:00 |
| `last_hour` | 15:00–16:00 |
| `close` | 15:45–16:00 |

Run analysis (fast — hourly market data + trade journals, not full-stack BT):

```powershell
python scripts/analysis/run_tod_analysis.py --days 365
```

Cache: `data/tod_analysis_cache.json`. Startup banner: `>>> Time-of-day: ON | best entry=… <<<`

### Daily Profit Banking (paper)

`modules/daily_profit_banking.py` tracks day-open equity vs current mark. When session gain ≥ **0.8%**, risk per trade is cut to **×0.4** and Dynamic VTI gets a **+10pp** defensive nudge for the rest of the day. Resets **30 minutes after the open** each session.

- Config: `DAILY_BANK_ENABLED`, `DAILY_BANK_THRESHOLD_PCT=0.8`, `DAILY_BANK_RISK_MULT=0.4`, `DAILY_BANK_LIVE_ENABLED=false`
- Compare: `python backtester.py --days 365 --paper-aggressive --compare-daily-bank`
- Banner: `>>> Daily Profit Banking: ON (0.8% threshold) <<<`
- Dashboard pill shows today's locked gain when banked

### GARCH(1,1) volatility sizing (locked paper)

`modules/garch_vol.py` fits a lightweight variance-targeting GARCH(1,1) on daily SPY (configurable) log returns — no `arch` package required. Next-day σ is compared to a rolling realized-vol median (**anchor**). Ratio → size / VTI multiplier:

| Forecast / anchor | Size mult (defaults) |
|-------------------|----------------------|
| ≤ **0.85** | **1.00** (cap — never size up vs baseline) |
| **0.85–1.35** | linear interpolate |
| ≥ **1.35** | **0.55** floor |

Also nudges Dynamic VTI (+pp when elevated; tiny cut when calm, capped) and soft-trims the conviction regime component (`GARCH_VOL_CONVICTION_BLEND=0.35`).

**Locked for Realistic Research** (same style as RHYME primary / Daily Banking): paper **ON**, live **OFF** unless `GARCH_VOL_LIVE_ENABLED=true`. Enforce keeps `GARCH_VOL_ENABLED=true` when not env-explicit. 365d A/B: ON beat OFF (~+1.06pp return, +0.04 Sharpe, MaxDD ≈ flat); shorter 80d window was slightly negative — lock uses the 365d evidence.

- Config: `GARCH_VOL_ENABLED`, `GARCH_VOL_LIVE_ENABLED=false`, `GARCH_VOL_MULT_MIN=0.55`, `GARCH_VOL_MULT_MAX=1.0`, `GARCH_VOL_LOOKBACK=252`
- Compare: `python backtester.py --days 365 --paper-aggressive --compare-garch-vol`
- Quiet runner: `python scripts/analysis/_run_garch_vol_compare.py` (`GARCH_COMPARE_DAYS` env)
- Banner: `>>> GARCH Vol: ON locked paper (σ̂/anchor … → size x…, VTI …pp) <<<`
- Lock line: `>>> GARCH vol lock: paper ON | live OFF (GARCH_VOL_LIVE_ENABLED=false) <<<`

### Optional ARIMA / ARIMA–GARCH hybrid (paper; default OFF)

`modules/arima_forecast.py` fits **statsmodels** ARIMA on a rolling window of daily **log returns** (stationarity-friendly vs close levels). Default order **(1,0,1)** with fallback **(1,1,1)**; if both fail, uses the last return. Next-step **mean forecast sign** is a soft directional signal:

| Forecast | Mean mult (defaults) |
|----------|----------------------|
| **> 0** | **×1.08** (cap **1.15** via `ARIMA_MULT_MAX`) |
| **≤ 0** | **×1.0** (neutral — no size-up; set `ARIMA_NEG_MULT` e.g. `0.97` for slight dampen) |

**Hybrid** (`ARIMA_GARCH_HYBRID=true`, default when ARIMA is on): combines mean direction with GARCH vol awareness without stacking GARCH twice.

| Mode | Combine |
|------|---------|
| GARCH already in `effective_risk_per_trade` | `size = clamp(1 + (mean_mult − 1) × vol_scale, 0.55, 1.15)` where `vol_scale` maps GARCH σ̂/anchor ratio (low→1.0, high→0.0) |
| GARCH off / no state | `size = mean_mult` |

Applied on the same paths as before: `effective_risk_per_trade` (momentum + Stat Arb notional) and a soft conviction-regime blend (`ARIMA_CONVICTION_BLEND=0.25`). Set `ARIMA_GARCH_HYBRID=false` for pure mean-only ARIMA (legacy A/B).

**Optional — final lock default OFF.** Enforce forces `ARIMA_ENABLED=false` unless env-explicit. 365d tune: ON worse return/Sharpe. Live always off unless `ARIMA_LIVE_ENABLED=true`.

- Enable (paper): `ARIMA_ENABLED=true` in `.env` (or portal paper env)
- Config: `ARIMA_WINDOW=252`, `ARIMA_GARCH_HYBRID=true`, `ARIMA_BOOST_MULT=1.08`
- Smoke: `python -m pytest tests/test_arima_forecast.py -q`
- Banner (when enabled): `>>> ARIMA–GARCH hybrid: ON optional paper (fc … → size x…) <<<`

### Smart ATR Stops (paper)

`modules/smart_atr_stops.py` sets a default **2.0× ATR** protective stop on new tactical longs. At **−5%** unrealized: high conviction (RVOL > 2.5 OR catalyst > 70 OR insider cluster) tightens to **1.0× ATR**; otherwise size is cut **50%** with the stop kept on the remainder. At **−10%**: hard full exit (no exceptions). Live stays off unless `SMART_STOPS_LIVE_ENABLED`.

- Config: `PAPER_SMART_STOPS`, `ATR_STOP_MULTIPLIER=2.0`, `ATR_TIGHTEN_MULTIPLIER=1.0`, `STOP_LOSS_REEVAL_PCTS=[-5,-10]`
- Compare: `python backtester.py --days 365 --paper-aggressive --compare-smart-stops --no-thinking`
- Banner: `>>> Smart ATR Stops: ON (2.0x → tighten 1.0x @ -5% / hard -10%) <<<`

Compare:

```powershell
python backtester.py --days 90 --paper-aggressive --compare-felix-dynamic
```

**Disable for static-off:** `FELIX_SOCIAL_DYNAMIC_ENABLED=false`  
**Force always-on (paper):** `FELIX_SOCIAL_MANUAL_OVERRIDE=true`

### Smart Dynamic VTI allocator (v1.5.3+, sector-aware since v1.5.4)

Replaces the legacy 63d Sharpe VTI/SPY picker and locked SPY@40% slice on paper research.

**Range:** 35–75% VTI normally (`DYNAMIC_VTI_PAPER_FLOOR` → `DYNAMIC_VTI_PAPER_CEILING`)

**Optional VTI floor (paper-first, v1.5.4):** VTI is not mandatory at 35%+. When **SPY-like**
confluence is strong (RVOL + ORB + Catalyst + insider on `SPY_LIKE_UNIVERSE`), the effective
floor can drop to `DYNAMIC_VTI_FLOOR_MIN` (~20%) or **0%** if `DYNAMIC_VTI_ALLOW_ZERO` /
`VTI_OPTIONAL` is on (Realistic Research enforce default). Live keeps the higher floor unless
`DYNAMIC_VTI_OPTIONAL_LIVE=true`. This is **not** locked as mandatory — disable with
`DYNAMIC_VTI_OPTIONAL_ENABLED=false`.

**SPY-like boost (paper-first):** names in
`SPY,QQQ,VTI,VOO,IWM,AAPL,MSFT,NVDA,GOOGL,AMZN,META,AVGO` get a conservative **1.05–1.2×**
size mult when ≥`SPY_LIKE_CONFLUENCE_MIN` (default 3) of RVOL/ORB/Catalyst/insider fire.
Wired into conviction sizing (`get_conviction_based_notional` / `scale_notional_by_conviction`).
Live off unless `SPY_LIKE_BOOST_LIVE_ENABLED=true`.

**Drivers (weighted):**

| Signal | Effect on VTI core |
|--------|-------------------|
| Strong / firm NYSE cross-section momentum | Lower VTI → more active sleeve room |
| Gold / metals strength | Lower VTI |
| High-score insider cluster buys | Lower VTI (favor active sleeves) |
| Exec sells + high bubble score (≥65) | Higher VTI or cash tilt |
| Elevated bubble / Buffett ratio | Higher VTI |
| Macro stress / elevated vol | Higher VTI (defensive baseline) |
| Regime conviction + VTI vs SPY momentum | Fine-tune within range |
| **Sector regime score (v1.5.4)** | Broad/strong sector breadth → lower VTI; narrow/weak breadth → higher VTI |
| **SPY-like confluence (optional floor)** | Strong → lower floor (20% or 0%) + lower VTI target |

Startup prints current target % and top 3 drivers (banner notes reduced floor when active).
Per-cycle banner + heartbeat `dynamic_vti` block in `run_all.py`.

**Disable for the legacy fixed 80/20 split is still available with `PAPER_DYNAMIC_VTI=false` + `PAPER_VTI_CORE_PCT=0.80`.

Integrations: NYSE momentum rank tags (`pair_key|rvol+orb+catalyst+insider`), dashboard RVOL/ORB/catalyst/short/strategy tables, weekly Telegram blocks, `scripts/full_system_verify.py`.

### Sector-Aware Portfolio Constructor (v1.5.4 new)

`modules/portfolio_constructor.py` — a second, independent per-cycle decision layered on top of
the Smart Dynamic VTI core %. It does **not** recompute core %, avoiding double-counting the same
bubble/insider/regime signals; it only tilts three additional knobs *within their existing hard
bounds*:

| Tilt | Range | Consumed by |
|------|-------|--------------|
| `active_sleeve_mult` | 0.85–1.15× (`PORTFOLIO_ACTIVE_SLEEVE_MULT_FLOOR/CEILING`) | SPY + NYSE sleeve caps (`fund_allocation_pct()` dict in `run_all.py`; `_scaled_cap_pct()` in `backtester.py`) |
| `stat_arb_mult` | 0.75–1.25× (`PORTFOLIO_STAT_ARB_MULT_FLOOR/CEILING`) | `config.effective_stat_arb_cap()` |
| `short_willingness_mult` | 0.60–1.40× (`PORTFOLIO_SHORT_WILLINGNESS_FLOOR/CEILING`) | `opportunistic_short_sleeve.short_target_gross_pct()` — tilts *within* `PROTECTIVE_SHORT_MIN/MAX_PCT`, never past them |

**Drivers:** `sector_regime_score` from `modules/sector_screener.compute_sector_regime_score()`
(momentum + RS vs SPY + cross-sector breadth, 0–1), bubble risk score, insider cluster
buys/exec-sell clusters, and regime conviction. Broad strong rotation expands active sleeves and
lowers short willingness; narrow/choppy rotation favors stat arb pairs and raises short
willingness; high bubble + exec sells contracts active sleeves and raises short willingness.

Gated end-to-end by `config.effective_portfolio_constructor_enabled()` (`PORTFOLIO_CONSTRUCTOR_ENABLED`
AND `paper_aggressive_context()`) — structurally a no-op on the live conservative bot regardless of
flag misconfiguration. Per-cycle banner + heartbeat block in `run_all.py`; per-bar in `backtester.py`.

---

## Monday prep commands (v1.5.4 final lock)

Confirm before open (or Sunday night) — production-ready paper checklist:

| Lock | Expected |
|------|----------|
| Version | `REALISTIC_RESEARCH_VERSION=1.5.4` / banner `v1.5.4 — Sector-Aware Portfolio Constructor` |
| Regime | **RHYME primary**; `MARKOV_HMM_PRIMARY_REGIME=false` (HMM soft only) |
| GARCH | Paper **ON** / live **OFF** (`GARCH_VOL_LIVE_ENABLED=false`) |
| Dynamic VTI | **ON** 35–75% |
| Daily Banking | Paper **ON** (≥0.8% → risk ×0.4); live off |
| Portfolio constructor | ON (paper aggressive only) |

```powershell
cd C:\Users\Owner\PythonTrading\stock-bot
Lock_v15.bat                           # cancel backtests + verify v1.5.4 lock
.\.venv\Scripts\python.exe scripts\cancel_backtest.py
.\.venv\Scripts\python.exe scripts\full_system_verify.py   # 12 sections + Monday banner
.\.venv\Scripts\python.exe scripts\maintenance\cleanup_journal_csv.py --dry-run
.\.venv\Scripts\python.exe scripts\owner_reset.py          # restart live + paper + dashboard
.\.venv\Scripts\python.exe status.py
```

Journal hygiene (optional write with backup):

```powershell
.\.venv\Scripts\python.exe scripts\maintenance\cleanup_journal_csv.py --all --backup
```

Overnight: `Start_Autonomous.bat` → `scripts/autostart_paper_bot.py` (paper restart + 9:00 AM ET Telegram; log: `logs/autostart_paper.log`).

### Recommended thorough backtest (overnight)

Best-stack validation — 1000 days, enriched thinking, historical news, Monte Carlo 30:

```powershell
cd C:\Users\Owner\PythonTrading\stock-bot
python -u backtester.py --best-test --days 1000 > backtest_best_1000.txt 2>&1
```

Smart Dynamic VTI A/B (fixed vs multi-signal):

```powershell
python -u backtester.py --compare-dynamic-vti --days 90 --no-thinking > backtest_smart_vti_90.txt 2>&1
python -u backtester.py --compare-dynamic-vti --days 365 --no-thinking > backtest_smart_vti_365.txt 2>&1
```

---

## Key config values (reference)

| Key | Value |
|-----|-------|
| `REALISTIC_RESEARCH_VERSION` | `1.5.4` |
| `PAPER_DYNAMIC_VTI` | `true` (default) |
| `DYNAMIC_VTI_PAPER_FLOOR` | `0.35` |
| `DYNAMIC_VTI_PAPER_CEILING` | `0.75` |
| `DYNAMIC_VTI_OPTIONAL_ENABLED` | `true` (paper; live off unless `DYNAMIC_VTI_OPTIONAL_LIVE`) |
| `DYNAMIC_VTI_ALLOW_ZERO` | `true` under Realistic Research enforce (alias `VTI_OPTIONAL`) |
| `DYNAMIC_VTI_FLOOR_MIN` | `0.20` |
| `SPY_LIKE_UNIVERSE` | `SPY,QQQ,VTI,VOO,IWM,AAPL,MSFT,NVDA,GOOGL,AMZN,META,AVGO` |
| `SPY_LIKE_BOOST_ENABLED` | `true` (paper; live off unless `SPY_LIKE_BOOST_LIVE_ENABLED`) |
| `SPY_LIKE_BOOST_MULT` | `1.10` (clamped 1.05–1.20) |
| `CORE_ALLOCATOR_LOCKED` | `false` (Smart VTI replaces locked SPY@40%) |
| `PORTFOLIO_CONSTRUCTOR_ENABLED` | `true` (default) |
| `PORTFOLIO_ACTIVE_SLEEVE_MULT_FLOOR` / `_CEILING` | `0.85` / `1.15` |
| `PORTFOLIO_STAT_ARB_MULT_FLOOR` / `_CEILING` | `0.75` / `1.25` |
| `PORTFOLIO_SHORT_WILLINGNESS_FLOOR` / `_CEILING` | `0.60` / `1.40` |
| `PAPER_RISK_PER_TRADE` | `0.018` |
| `PAPER_POSITION_MAX_HOLD_BARS` | `30` |
| `PAPER_VOL_CEILING_PCT` | `0.17` |
| `PAPER_MAX_POSITION_PCT` | `0.08` |
| `PAPER_STAT_ARB_RISK_REWARD` | `1.7` |
| `PAPER_STAT_ARB_MAX_PAIRS` | `8` (→ 12 ceiling) |
| `RVOL_MIN_THRESHOLD` | `2.0` |
| `ORB_BREAKOUT_MINUTES` | `30` |
| `CATALYST_MIN_SCORE` | `65` |
| `ATR_RISK_MULTIPLE` | `2.0` |
| `PROTECTIVE_SHORT_MIN_PCT` | `0.08` |
| `PROTECTIVE_SHORT_MAX_PCT` | `0.18` |
| `MARKOV_HMM_ENABLED` | `true` (soft-signal ON) |
| `MARKOV_HMM_PRIMARY_REGIME` | `false` (RHYME primary locked) |
| `GARCH_VOL_ENABLED` | `true` (**locked** paper ON) |
| `GARCH_VOL_LIVE_ENABLED` | `false` (live OFF unless opt-in) |
| `GARCH_VOL_MULT_MIN` / `_MAX` | `0.55` / `1.0` |
| `ARIMA_ENABLED` | `false` (**optional** — set `true` to enable paper hybrid/boost) |
| `ARIMA_GARCH_HYBRID` | `true` (when ARIMA on: vol-aware mean; no double GARCH) |

`.env` overrides win — only set keys you intend to change.

---

## `.env` snippet (v1.5.4)

```env
PAPER_TRADING=true
PAPER_CHASE_MODE=1
PAPER_AGGRESSIVE=true
REALISTIC_RESEARCH_VERSION=1.5.4
PAPER_DYNAMIC_VTI=true
DYNAMIC_VTI_PAPER_FLOOR=0.35
DYNAMIC_VTI_PAPER_CEILING=0.75
DYNAMIC_VTI_OPTIONAL_ENABLED=true
DYNAMIC_VTI_ALLOW_ZERO=true
DYNAMIC_VTI_FLOOR_MIN=0.20
SPY_LIKE_UNIVERSE=SPY,QQQ,VTI,VOO,IWM,AAPL,MSFT,NVDA,GOOGL,AMZN,META,AVGO
SPY_LIKE_BOOST_ENABLED=true
SPY_LIKE_BOOST_MULT=1.10
CORE_ALLOCATOR_LOCKED=false
PORTFOLIO_CONSTRUCTOR_ENABLED=true
RVOL_SCANNER_ENABLED=true
RVOL_MIN_THRESHOLD=2.0
ORB_ENABLED=true
ORB_BREAKOUT_MINUTES=30
CATALYST_SCORING_ENABLED=true
CATALYST_MIN_SCORE=65
ATR_SIZING_ENABLED=true
ATR_RISK_MULTIPLE=2.0
PAPER_SMART_STOPS=true
ATR_STOP_MULTIPLIER=2.0
ATR_TIGHTEN_MULTIPLIER=1.0
STOP_LOSS_REEVAL_PCTS=[-5,-10]
MARKOV_HMM_ENABLED=true
MARKOV_HMM_PRIMARY_REGIME=false
GARCH_VOL_ENABLED=true
GARCH_VOL_LIVE_ENABLED=false
GARCH_VOL_MULT_MIN=0.55
GARCH_VOL_MULT_MAX=1.0
# Optional ARIMA / ARIMA–GARCH hybrid (default off):
# ARIMA_ENABLED=true
# ARIMA_GARCH_HYBRID=true
# ARIMA_WINDOW=252
# ARIMA_BOOST_MULT=1.08
```

---

## Cloud / VPS Migration (v1.5.3)

Production migration from desktop (Windows) to a 24/7 Linux VPS. Separates **Paper (aggressive research)** and **Live (conservative small account)** into isolated systemd services with per-user secrets, health alerting, and a safe deploy workflow. Ubuntu 24.04 LTS + venv + systemd (no Docker required).

### Architecture

```
Desktop (dev)                Cloud VPS (Hetzner CPX41, Ubuntu 24.04)
-------------                ---------------------------------------
Cursor / git push  ─────►    /opt/PythonTrading (git checkout of tagged release)
rsync secrets      ─(scp)►   /etc/pythontrading/{paper,live}.env   (chmod 600)
rsync market_data.db ──►     stock-bot/market_data.db (until VPS is primary)

                             ┌── paper-bot.service (user: trader-paper) ──┐
                             │   run_paper_bot.py → run_all.py            │
                             │   Realistic Research v1.5.3, Smart Dyn VTI │──► Alpaca PAPER REST+WSS
                             └────────────────────────────────────────────┘
                             ┌── live-bot.service  (user: trader-live)  ──┐
                             │   run_all.py (conservative)                │──► Alpaca LIVE REST
                             └────────────────────────────────────────────┘
                             cron */5 → cloud_healthcheck.sh → Telegram
                             (optional) Ollama :11434 on GPU node / same box
```

### Recommended spec + cost

| Role | Provider / plan | Spec | ~Cost/mo |
|------|-----------------|------|----------|
| Trading VPS | **Hetzner CPX41** (US Ashburn) | 8 vCPU, **16 GB RAM**, 240 GB NVMe | ~$18 |
| Budget option | Hetzner CPX31 | 4 vCPU, 8 GB RAM | ~$12 |
| GPU inference (optional) | Vultr A5000 / RunPod persistent | 24 GB VRAM, 32 GB RAM | ~$80–120 |
| Kimi / NVIDIA NIM (daily deep-think) | API | — | ~$20–40 |
| Backups | Hetzner snapshot + Backblaze B2 | daily | ~$5 |

- **API-only thinking (no GPU box):** ~$35–60/mo — Ollama runs a small 7B/14B model on CPU for fast tilts; Kimi/NIM handles daily deep reasoning.
- **Full GPU stack:** ~$125–180/mo — 32B Ollama on a dedicated 24 GB node.

### Ollama + Kimi/NIM thinking strategy

Thinking is **off by default** on the VPS (`PAPER_THINKING_ENGINE_ENABLED=false` in the cloud profile) and only runs on the **paper** bot — never on live. Enable it in `/etc/pythontrading/paper.env` once inference is validated.

| Role | Model | Quant | VRAM/RAM | Env |
|------|-------|-------|----------|-----|
| Primary PM tilt | `qwen2.5:14b-instruct` | Q4_K_M | ~9 GB VRAM | `OLLAMA_MODEL` |
| Fast fallback | `qwen2.5-coder:14b` | Q4_K_M | ~9 GB | `OLLAMA_FALLBACK_MODELS` |
| Emergency fallback | `llama3.1:8b-instruct` | Q4_K_M | ~5 GB | `OLLAMA_FALLBACK_MODELS` |
| Deep reasoning (daily) | Kimi via NVIDIA NIM `moonshotai/kimi-k2.6` | API | — | `KIMI_API_ENABLED=true`, `KIMI_DAILY_THINK=true` |
| Upgrade (24 GB+ GPU only) | `qwen2.5:32b` | Q4_K_M | ~20 GB | `OLLAMA_MODEL` |

```bash
# On the GPU node (or same box if it has a GPU):
ollama pull qwen2.5:14b-instruct-q4_K_M
ollama pull qwen2.5-coder:14b-q4_K_M
ollama pull llama3.1:8b-instruct-q4_K_M
```

Point the paper bot at Ollama with `OLLAMA_HOST=http://<gpu-ip>:11434` (or `http://127.0.0.1:11434` if co-located). Live keeps `PAPER_THINKING_ENGINE_ENABLED=false`.

### Step-by-step migration checklist

**Phase 0 — Freeze (desktop)**
1. `git tag v1.5.3-pre-cloud && git push origin v1.5.3-pre-cloud`
2. Capture baseline: `python status.py`, `python backtester.py --compare-dynamic-vti --days 90 --no-thinking`
3. Redact and save paper vs live `.env` sections from `.env.example`.

**Phase 1 — Provision**
4. Create Hetzner CPX41, Ubuntu 24.04 LTS, US-East.
5. SSH hardening: key-only auth, `sudo ufw allow OpenSSH && sudo ufw enable`, disable root login, install `fail2ban`.
6. Create users: `sudo adduser --system --group trader-paper`, `trader-live`, and a `trader-deploy` (sudo) for deploys.
7. `sudo apt install -y git python3.12-venv sqlite3 logrotate`.

**Phase 2 — Deploy code**
```bash
sudo mkdir -p /opt/PythonTrading && sudo chown trader-deploy: /opt/PythonTrading
cd /opt/PythonTrading && git clone <YOUR_REPO_URL> .
python3 -m venv .venv && .venv/bin/pip install -r stock-bot/requirements.txt
```

**Phase 3 — Secrets (per user, chmod 600)**
```bash
sudo mkdir -p /etc/pythontrading
sudo install -m 600 -o trader-paper -g trader-paper paper.env /etc/pythontrading/paper.env
sudo install -m 600 -o trader-live  -g trader-live  live.env  /etc/pythontrading/live.env
```
`paper.env` must contain paper Alpaca keys + `PAPER_DYNAMIC_VTI=true`; `live.env` must contain live keys + `PAPER_TRADING=false` + `ALLOW_LIVE_TRADING=yes` and **no paper keys**.

**Phase 4 — Data seed**
8. `rsync -avz market_data.db trader-paper@vps:/opt/PythonTrading/stock-bot/` (weekly until VPS is primary writer).

**Phase 5 — Install services + monitoring**
```bash
sudo cp cloud_bot/deploy/systemd/paper-bot.service /etc/systemd/system/
sudo cp cloud_bot/deploy/systemd/live-bot.service  /etc/systemd/system/
sudo systemctl daemon-reload
# Line endings: if scripts came via Windows git, run: sed -i 's/\r$//' scripts/*.sh
chmod +x scripts/cloud_healthcheck.sh scripts/deploy_to_vps.sh
sudo systemctl enable --now paper-bot          # start paper first
```
Cron (`/etc/cron.d/pythontrading-health`):
```cron
*/5 * * * * trader-paper /opt/PythonTrading/stock-bot/scripts/cloud_healthcheck.sh paper
*/5 * * * * trader-live  /opt/PythonTrading/stock-bot/scripts/cloud_healthcheck.sh live
```

**Phase 6 — Paper soak (3–7 days)**
9. Compare cloud vs desktop heartbeat, equity, `dynamic_vti` block, cycle errors. Enable thinking only after Ollama/NIM validated.

**Phase 7 — Live**
10. `sudo systemctl enable --now live-bot` on the separate user; monitor first 48h closely.

**Phase 8 — Cutover**
11. Stop desktop bots; keep desktop as warm standby for 14 days.

### systemd services

Both units live in `cloud_bot/deploy/systemd/`:

- **`paper-bot.service`** — `run_paper_bot.py` as `trader-paper`, `Restart=always` (RestartSec=30), crash-loop guard (5 restarts / 5 min), `SIGTERM` graceful stop, `ProtectSystem=strict` sandbox.
- **`live-bot.service`** — `run_all.py` as `trader-live`, `Restart=on-failure` (RestartSec=60, max 3 / 10 min) to avoid hammering the broker after a crash, longer `TimeoutStopSec`.

### Deploy workflow

`scripts/deploy_to_vps.sh` — safe git-based deploy:
```bash
./scripts/deploy_to_vps.sh v1.5.3                  # paper only (default)
./scripts/deploy_to_vps.sh v1.5.3 --restart-live   # also restart live (10s abort window)
./scripts/deploy_to_vps.sh v1.5.3 --no-restart     # code only
```
Refuses to deploy over a dirty tree, checks out the ref, `pip install`s, runs the Smart Dynamic VTI allocator smoke test as a parity gate, restarts paper (live opt-in), verifies units are active, and prints a healthcheck. On any failure it rolls back to the previous commit.

### Healthcheck + alerting

`scripts/cloud_healthcheck.sh {paper|live|cloud}` — run from cron every 5 min. Checks (1) systemd unit active, (2) heartbeat fresh (`MAX_AGE_SEC`, default 600s), (3) `last_cycle_error` null. On failure it sends a Telegram alert via `modules/alerts.send_telegram` and debounces repeats (`ALERT_DEBOUNCE_SEC`, default 1800s) via a state file so cron does not spam.

### Backup + rollback

**Backup (daily cron):**
```bash
STAMP=$(date +%Y%m%d)
tar czf /var/backups/pythontrading-$STAMP.tar.gz \
  /opt/PythonTrading/stock-bot/market_data.db \
  /opt/PythonTrading/stock-bot/data \
  /opt/PythonTrading/stock-bot/logs \
  /opt/PythonTrading/stock-bot/*heartbeat*.json
find /var/backups -name 'pythontrading-*.tar.gz' -mtime +14 -delete
# rclone copy /var/backups/pythontrading-$STAMP.tar.gz b2:my-bucket/   # off-site
```
Encrypt and vault `/etc/pythontrading/*.env` separately (Bitwarden/1Password/age).

**Rollback:**
1. Misbehaving bot: `sudo systemctl stop paper-bot` (or `live-bot`).
2. Code: `./scripts/deploy_to_vps.sh v1.5.3-pre-cloud` (auto-rolls back on failed test/restart).
3. Full fallback: restart bots on the desktop (keep `.env` + `market_data.db` in sync).
4. **Live emergency:** stop `live-bot` first; positions/orders are authoritative at Alpaca — flatten via the Alpaca dashboard if needed. Never roll back live code without stopping the service.
5. DB corruption: restore yesterday's `market_data.db` from backup; broker remains source of truth for positions.

### Safety invariants

- Live and paper never share a process, user, or env file.
- `live.env` contains no paper keys; `paper.env` contains no live keys.
- Cloud profile (`cloud_bot/config/profile.py`) `CLOUD_FORCED_ENV` pins `PAPER_TRADING=true` / `ALLOW_LIVE_TRADING=false` for the supervisor path.
- Thinking engine runs on paper only; live is LLM-free.
- Deploy gate runs the allocator smoke test before restarting any service.
