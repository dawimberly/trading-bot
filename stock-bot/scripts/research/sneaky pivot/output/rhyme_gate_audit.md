# RHYME A/B Gate Audit

Static scan of `stock-bot/` for RHYME_A / RHYME_B / Euphoric / Panic references.
Heuristic GATE vs REPORT vs UNCLEAR — manually verify GATE/UNCLEAR before
treating as production behavior. Research only; freeze unchanged.

Total hits: 95  |  GATE: 58  |  REPORT: 8  |  UNCLEAR: 29

Companion finding: `cross_asset_vol_score` currently zeros on wide daily matrices (`dropna(how='any')`), so these gates rarely/never see A/B fire.

## GATE (real behavioral impact — check these FIRST)

### `_bt_exp_365_runner.py:2638`

```python
    hi = config.effective_protective_short_max_pct()
    configs = [
        ("RR v1.4 (shorts OFF)", {**base_kwargs, "opportunistic_short": False}),
        (f"RR v1.4 (shorts tuned {lo:.0%}-RHYME_E {config.SHORT_RHYME_E_MAX_PCT:.0%}/RHYME_B {config.SHORT_RHYME_B_MAX_PCT:.0%})", {**base_kwargs, "opportunistic_short": True}),
    ]
    print(f"--- PROTECTIVE SHORTS A/B (Realistic Research v1.4 tuned) ---")
    print(
```

### `backtester.py:3289`

```python
    hi = config.effective_protective_short_max_pct()
    configs = [
        ("RR v1.4 (shorts OFF)", {**base_kwargs, "opportunistic_short": False}),
        (f"RR v1.4 (shorts tuned {lo:.0%}-RHYME_E {config.SHORT_RHYME_E_MAX_PCT:.0%}/RHYME_B {config.SHORT_RHYME_B_MAX_PCT:.0%})", {**base_kwargs, "opportunistic_short": True}),
    ]
    print(f"--- PROTECTIVE SHORTS A/B (Realistic Research v1.4 tuned) ---")
    print(
```

### `backtester_macro_hedge.py:70`

```python
NORMAL_CASH_PCT = config.effective_cash_buffer_pct()
STRESS_CASH_PCT = 0.25
BEAR_REGIME = "RHYME_E: Steady_Bearish_Decline"
PANIC_REGIME = "RHYME_B: Panic_Volatility"

_normalize_df = normalize_yfinance_df

```

### `backtester_macro_hedge.py:231`

```python
def _sh_enter(regime: str, window: pd.DataFrame) -> bool:
    """Bear hedge: SPY below MA200 and bonds/yields stressed; skip euphoric rallies."""
    bullish, _ = _spy_market_up_signal(window, config.SPY_BOT_SYMBOL, config.SPY_MA_WINDOW)
    if bullish or regime in ("RHYME_A: Euphoric_Volatility", PANIC_REGIME):
        return False
    return _bond_stress(window) or not bullish

```

### `config.py:431`

```python
#
# === MOST REALISTIC PAPER / RESEARCH DEFAULT (locked 2026-06) ===
# v1.0: Minimal + Deep History Indicators Only (walk-forward + Monte Carlo robust).
# v1.1: v1.0 + Tail Risk Controls (vol ceiling, DD scaling, RHYME_B buffers, sector safety).
# Core: locked SPY @ 40% passive (90d Sharpe VTI 0.56 vs SPY 0.60) — 60% active sleeve budget.
#
# Tail-risk additions in v1.1 (modules/paper_risk_controls.py, modules/sector_screener.py):
```

### `config.py:439`

```python
#   - Vol ceiling — scale risk when ann. vol > PAPER_VOL_CEILING_PCT (default 17%)
#   - Portfolio vol cap — rolling equity vol vs PORTFOLIO_VOL_CEILING_PCT (18%)
#   - Drawdown tiers — 5% DD → 0.6× risk; 8% DD → 0.3× risk
#   - RHYME_B — sleeve cap trim + PAPER_REGIME_B_RISK_MULT (0.50×) + cash buffer boost
#   - Per-name cap — PAPER_MAX_POSITION_PCT 8%
#   - Weak-regime sleeve cap — PAPER_REGIME_WEAK_SLEEVE_MAX_PCT 25% in B/D/E
#   - Sector screener — limit expansion when SECTOR_HIGH_VOL_CEILING_PCT exceeded
```

### `config.py:1022`

```python
STAT_ARB_CORR_HALF_LIFE = float(os.getenv("STAT_ARB_CORR_HALF_LIFE", "25"))
# --- Protective / opportunistic shorts (Realistic Research v1.5 tuned, paper only) ---
# Entry:
#   RHYME_B — VIX≥22 rising + momentum exhaustion + depth≥2% (cap 18% gross)
#   RHYME_E — 3-bar SPY bear streak + depth≥3% + bubble≥60 (cap 12%); VIX≥28 waives
#             full streak if ≥2 down days; waiver entries sized at 75%
#   Sector ETFs — weak momentum + bubble≥55 + full 3-bar streak; ≤8%/name
```

### `config.py:3470`

```python
def _yield_gate_hard_regime(regime: str | None) -> bool:
    """True for strong bear / panic regimes where paper override still blocks."""
    reg = str(regime or "")
    return any(tag in reg for tag in ("RHYME_B", "RHYME_E", "Panic_Volatility", "Bearish_Decline"))


def effective_yield_gate(
```

### `config.py:3482`

```python

    Live / non-paper: unchanged (fully gated when raw_gated).
    Paper with ``PAPER_YIELD_GATE_OVERRIDE``: soften mild rate/bond stress so
    deployment can continue; still block in strong bear/panic (RHYME_B/E).
    """
    if not raw_gated:
        return False
```

### `config.py:3561`

```python
            expanded = True
        target = max(target, high)

    if regime and effective_tail_risk_controls() and "RHYME_B" in str(regime):
        target = round(target * (1.0 - float(PAPER_REGIME_B_CASH_BUFFER_BOOST)), 6)
    if regime:
        try:
```

### `config.py:5540`

```python
    return (
        f"Tail Risk: {tail_flag} | {core_label} | "
        f"risk {RISK_PER_TRADE:.1%} | hold {PAPER_POSITION_MAX_HOLD_BARS}d | "
        f"vol cap {PAPER_VOL_CEILING_PCT:.0%} | RHYME_B x{PAPER_REGIME_B_SIZING_MULT:.2f} | "
        f"sector {sector_flag} | {format_universe_pool_label()}"
        f"{short_note}"
    )
```

### `config.py:5996`

```python

def effective_protective_short_max_pct(regime: str | None = None) -> float:
    reg = str(regime or "")
    if "RHYME_B" in reg:
        return max(0.0, min(0.50, float(SHORT_RHYME_B_MAX_PCT)))
    if "RHYME_E" in reg:
        return max(0.0, min(0.50, float(SHORT_RHYME_E_MAX_PCT)))
```

### `config.py:6052`

```python
    hi = effective_protective_short_max_pct()
    line = (
        f"Protective Shorts: ON ({lo:.0%}-{hi:.0%} gross, "
        f"RHYME_E<={SHORT_RHYME_E_MAX_PCT:.0%} RHYME_B<={SHORT_RHYME_B_MAX_PCT:.0%}, "
        f"partial@{SHORT_PARTIAL_PROFIT_RR:.0f}:1, trail {SHORT_TRAILING_ARM_FRAC:.0%}/{SHORT_TRAILING_PULLBACK_FRAC:.0%})"
    )
    if not effective_short_rhyme_e_exhaustion_required():
```

### `config.py:6179`

```python
        f">>> PAPER TUNED DEFAULTS (tail-risk Option A) — "
        f"SPY MA{PAPER_SPY_MA_WINDOW} | NYSE MA{PAPER_NYSE_MA_WINDOW} | "
        f"risk {PAPER_RISK_PER_TRADE:.1%} calm / {PAPER_RISK_MODERATE_PCT:.1%} mod / "
        f"{PAPER_RISK_STRESS_PCT:.1%} stress | RHYME_B x{PAPER_REGIME_B_SIZING_MULT:.2f} | "
        f"RHYME_E x{PAPER_REGIME_E_SIZING_MULT:.2f} | "
        f"max hold {PAPER_POSITION_MAX_HOLD_BARS} bars | "
        f"vol cap {PAPER_VOL_CEILING_PCT:.0%} | "
```

### `data\_bt_exp_365_runner.py:2638`

```python
    hi = config.effective_protective_short_max_pct()
    configs = [
        ("RR v1.4 (shorts OFF)", {**base_kwargs, "opportunistic_short": False}),
        (f"RR v1.4 (shorts tuned {lo:.0%}-RHYME_E {config.SHORT_RHYME_E_MAX_PCT:.0%}/RHYME_B {config.SHORT_RHYME_B_MAX_PCT:.0%})", {**base_kwargs, "opportunistic_short": True}),
    ]
    print(f"--- PROTECTIVE SHORTS A/B (Realistic Research v1.4 tuned) ---")
    print(
```

### `modules\backtest_attribution.py:925`

```python
    if not trigger.get("allowed"):
        return ""
    raw = str(trigger.get("trigger_reason") or "")
    regime = "RHYME_B" if "RHYME_B" in raw else "RHYME_E" if "RHYME_E" in raw else "SHORT"
    vix = str(trigger.get("vix_reason") or "VIX confirm")
    exh = str(trigger.get("exhaustion_reason") or "exhaustion")
    bubble = float(trigger.get("bubble_score") or 0.0)
```

### `modules\bot_health.py:384`

```python
    reg_u = reg.upper()
    if "RHYME_C" in reg_u or "RHYME_D" in reg_u:
        _apply(8.0, "regime_stable", "Stable regime (C/D)")
    elif "RHYME_A" in reg_u:
        _apply(6.0, "regime_stable", "Risk-on regime (A)")
    elif "RHYME_E" in reg_u:
        _apply(5.0, "regime_stable", "Bear regime managed (E)")
```

### `modules\bot_health.py:388`

```python
        _apply(6.0, "regime_stable", "Risk-on regime (A)")
    elif "RHYME_E" in reg_u:
        _apply(5.0, "regime_stable", "Bear regime managed (E)")
    elif reg_u and "RHYME_B" not in reg_u:
        _apply(5.0, "regime_stable", "Regime stable")

    if config.effective_atr_sizing_enabled():
```

### `modules\bot_health.py:541`

```python
            f"Excessive no_room rejects ({no_room_rate_pct:.0f}% of skips)",
        )

    if "RHYME_B" in reg_u:
        _apply(-6.0, "regime_b", "RHYME_B panic — defensive posture")
    elif "RHYME_E" in reg_u and not sizing_stress:
        _apply(-2.0, "regime_e", "RHYME_E bear — elevated caution")
```

### `modules\bot_health.py:542`

```python
        )

    if "RHYME_B" in reg_u:
        _apply(-6.0, "regime_b", "RHYME_B panic — defensive posture")
    elif "RHYME_E" in reg_u and not sizing_stress:
        _apply(-2.0, "regime_e", "RHYME_E bear — elevated caution")

```

### `modules\crypto_vol_gate.py:70`

```python
        if config.effective_crypto_regime_filter():
            from modules.pipeline_strategies import PAUSED_REGIMES

            if reg == "RHYME_B: Panic_Volatility":
                return {
                    "allowed": False,
                    "vol": vol,
```

### `modules\historical_news.py:660`

```python
        f"Fed and macro data in focus as equities trade {reg} regime",
        "S&P 500 sector rotation continues amid earnings season",
    ]
    if "RHYME_E" in reg or "RHYME_B" in reg:
        lines.append("Risk-off headlines dominate — defensives bid, growth sold")
    elif "RHYME_A" in reg or "RHYME_C" in reg:
        lines.append("Risk-on narrative: cyclicals and tech leadership intact")
```

### `modules\historical_news.py:662`

```python
    ]
    if "RHYME_E" in reg or "RHYME_B" in reg:
        lines.append("Risk-off headlines dominate — defensives bid, growth sold")
    elif "RHYME_A" in reg or "RHYME_C" in reg:
        lines.append("Risk-on narrative: cyclicals and tech leadership intact")
    if vol_s.lower() not in ("low", ""):
        lines.append("VIX volatility spike stories drive hedging demand")
```

### `modules\insider_signal_handler.py:194`

```python
                reg = str(ctx.get("regime") or "")
        except Exception as exc:
            logger.debug("bubble context fetch failed for insider gate: %s", exc)
    rhyme_b = "RHYME_B" in reg
    rhyme_b_panic = rhyme_b and "Panic" in reg
    bear_regime = rhyme_b or "RHYME_E" in reg
    return score_100, reg, rhyme_b_panic, bear_regime
```

### `modules\insider_signal_handler.py:228`

```python
            }
            notes.append("stat arb insider mult damped 80%")

    if "RHYME_B" in regime and momentum_boosts:
        rhyme_mult = float(config.INSIDER_RHYME_B_BULLISH_MULT)
        momentum_boosts = {k: round(v * rhyme_mult, 4) for k, v in momentum_boosts.items()}
        stat_arb_boosts = {
```

### `modules\insider_signal_handler.py:234`

```python
        stat_arb_boosts = {
            k: round(1.0 + (v - 1.0) * rhyme_mult, 4) for k, v in stat_arb_boosts.items()
        }
        notes.append(f"RHYME_B: bullish insider boosts halved ({rhyme_mult:.0%})")

    if bear_regime and short_boosts:
        cap = float(config.INSIDER_SELL_SHORT_BOOST_MAX)
```

### `modules\insider_signal_handler.py:252`

```python
            meta["base"] = round(min(boosted, cap * 1.10), 4)
            meta["rhyme_b_amplified"] = True
            short_boosts[sym] = meta
        notes.append("short boosts amplified (RHYME_B panic)")

    return momentum_boosts, stat_arb_boosts, short_boosts, notes

```

### `modules\markov_regime.py:6`

```python
Trains ``hmmlearn.hmm.GaussianHMM`` on a rolling daily feature window
(SPY returns, vol, VIX, volume, sentiment proxy, bubble, insider) and
emits next-day regime probabilities over 5 hidden states aligned with
RHYME_A–E. Soft-signals Dynamic VTI, conviction, and short sizing.

Falls back to the current RHYME classifier (and a lightweight 3-state
count matrix for thinking-engine prompts) when hmmlearn is missing or
```

### `modules\opportunistic_short_sleeve.py:219`

```python

def _regime_size_multiplier(regime: str, bubble_score: float) -> float:
    reg = str(regime or "")
    if "RHYME_B" in reg:
        base = config.SHORT_REGIME_B_SIZE_MULT
    elif "RHYME_E" in reg:
        base = config.SHORT_REGIME_E_SIZE_MULT
```

### `modules\opportunistic_short_sleeve.py:262`

```python
    hi = config.effective_protective_short_max_pct(regime)
    mid = (lo + hi) / 2.0
    reg = str(regime or "")
    reg_boost = 1.0 if "RHYME_B" in reg else 0.90 if "RHYME_E" in reg else 0.70
    bub = float(bubble_score)
    bub_norm = max(0.0, min(1.0, bub if bub <= 1.0 else bub / 100.0))
    tilt = (bub_norm - 0.50) / 0.50
```

### `modules\opportunistic_short_sleeve.py:934`

```python
        if sym == config.SPY_BOT_SYMBOL and "RHYME_E" in str(regime):
            if depth < config.SHORT_DEEP_BEAR_MIN_DEPTH:
                continue
        if "RHYME_B" in str(regime) and depth < config.SHORT_RHYME_B_MIN_DEPTH:
            continue
        pair_key = f"{sym}/SHORT/MA{ma_window}"
        trades += _open_short(
```

### `modules\opportunistic_short_sleeve.py:973`

```python
) -> int:
    if not config.effective_short_opportunistic_single_names():
        return 0
    if "RHYME_B" not in str(regime or ""):
        return 0
    if bubble_score < config.SHORT_BUBBLE_SCORE_MIN:
        return 0
```

### `modules\orb_momentum_sleeve.py:427`

```python
    if config.effective_yield_gate(yield_gated, regime=regime):
        result["skipped"] = "yield_gate"
        return result
    if "RHYME_B" in str(regime or "").upper():
        result["skipped"] = "regime_b"
        return result

```

### `modules\orb_momentum_sleeve.py:673`

```python
        return 0
    if not bool(getattr(config, "ORB_MOMENTUM_BACKTEST_ENABLED", True)):
        return 0
    if "RHYME_B" in str(regime or "").upper():
        return 0

    trades = 0
```

### `modules\paper_risk_controls.py:34`

```python
    mult = 1.0
    reg = str(regime or "")
    if not config.effective_regime_dynamic_sizing():
        if "RHYME_B" in reg:
            mult *= config.PAPER_REGIME_B_RISK_MULT
        elif "RHYME_D" in reg:
            mult *= config.PAPER_REGIME_D_RISK_MULT
```

### `modules\paper_risk_controls.py:91`

```python
    hard = config.paper_sleeve_hard_cap_pct(sleeve_key)
    if hard is not None:
        cap = min(cap, float(hard))
    if regime and config.effective_tail_risk_controls() and "RHYME_B" in str(regime):
        cap = round(cap * (1.0 - float(config.PAPER_REGIME_B_CASH_BUFFER_BOOST)), 6)
    if regime:
        from modules.regime_sizing import regime_sleeve_exposure_ceiling
```

### `modules\pipeline_strategies.py:18`

```python

logger = logging.getLogger(__name__)

PAUSED_REGIMES = ("RHYME_B: Panic_Volatility", "RHYME_E: Steady_Bearish_Decline")

# In-session NYSE momentum entries for one-per-day (backtest + live gate).
_nyse_mom_entries_by_day: dict[str, set[str]] = {}
```

### `modules\pipeline_strategies.py:2346`

```python
    volatility: str | None = None,
    vol_score: float | None = None,
) -> dict:
    """Protective shorts — RHYME_B: VIX + exhaustion + depth; RHYME_E: VIX + bubble + depth (exhaustion optional)."""
    from modules.bubble_risk import compute_bubble_risk

    reg = str(regime or "")
```

### `modules\pipeline_strategies.py:2371`

```python
        result["reject"] = "shorts_disabled"
        return result

    bear_b = "RHYME_B" in reg
    bear_e = config.SHORT_RHYME_E_ENABLED and "RHYME_E" in reg
    if not bear_b and not bear_e:
        result["reject"] = "regime_not_bear"
```

### `modules\pipeline_strategies.py:2377`

```python
        result["reject"] = "regime_not_bear"
        return result

    result["regime_path"] = "RHYME_B" if bear_b else "RHYME_E"
    bubble_ctx = compute_bubble_risk(data, regime, volatility=volatility, vol_score=vol_score)
    bubble = float(bubble_ctx["score_normalized"])
    result["bubble_score"] = bubble
```

### `modules\pipeline_strategies.py:2434`

```python
        if bearish and depth >= config.SHORT_RHYME_B_MIN_DEPTH:
            result["allowed"] = True
            result["trigger_reason"] = (
                f"RHYME_B|{exh_reason}|{vix_reason}|bubble={bubble:.2f}|depth={depth:.3f}"
            )
            return result
        result["reject"] = "depth_low"
```

### `modules\sector_rotation.py:350`

```python
    if config.effective_yield_gate(yield_gated, regime=regime):
        result["skipped"] = "yield_gate"
        return result
    if "RHYME_B" in str(regime or "").upper():
        result["skipped"] = "regime_b"
        return result

```

### `modules\sector_rotation.py:564`

```python
        return 0
    if not bool(getattr(config, "SECTOR_ROTATION_BACKTEST_ENABLED", True)):
        return 0
    if "RHYME_B" in str(regime or "").upper():
        return 0
    if window is None or getattr(window, "empty", True) or i < 20:
        return 0
```

### `modules\vol_breakout_sleeve.py:576`

```python
    if config.effective_yield_gate(yield_gated, regime=regime):
        result["skipped"] = "yield_gate"
        return result
    if "RHYME_B" in str(regime or "").upper():
        result["skipped"] = "regime_b"
        return result

```

### `modules\vol_breakout_sleeve.py:832`

```python
        return 0
    if not bool(getattr(config, "VOL_BREAKOUT_BACKTEST_ENABLED", True)):
        return 0
    if "RHYME_B" in str(regime or "").upper():
        return 0

    trades = 0
```

### `modules\weekly_report.py:675`

```python
    lines.extend(["## Protective shorts", ""])
    lines.append(f"- {config.format_opportunistic_short_banner()}")
    lines.append(
        "- Triggers: RHYME_B + VIX≥22 rising + exhaustion + depth≥2%; "
        f"RHYME_E + VIX≥22 rising + bubble≥{config.effective_short_bubble_min_for_rhyme_e():.0%} "
        f"+ depth≥{config.SHORT_DEEP_BEAR_MIN_DEPTH:.0%}"
        + (" + exhaustion" if config.effective_short_rhyme_e_exhaustion_required() else " (exhaustion waived)")
```

### `modules\weekly_report.py:689`

```python
        )
    lines.append(
        f"- Sizing: {config.effective_protective_short_min_pct():.0%}-"
        f"RHYME_E {config.SHORT_RHYME_E_MAX_PCT:.0%} / RHYME_B {config.SHORT_RHYME_B_MAX_PCT:.0%} gross | "
        f"partial 50% @ 1:1 | trail arm {config.SHORT_TRAILING_ARM_FRAC:.0%} / pull {config.SHORT_TRAILING_PULLBACK_FRAC:.0%} | "
        f"RR {config.SHORT_PROFIT_TARGET_PCT/config.SHORT_STOP_LOSS_PCT:.1f}:1 + trail | "
        f"max hold {config.SHORT_MAX_HOLD_BARS}b"
```

### `modules\wisdom_sentiment.py:28`

```python
MODES = LIVE_MODES + DEPRECATED_MODES
PAUSE_REGIME = "RHYME_E: Steady_Bearish_Decline"
BEAR_REGIME = "RHYME_E: Steady_Bearish_Decline"
PANIC_REGIME = "RHYME_B: Panic_Volatility"


def normalize_wisdom_mode(mode: str | None) -> str:
```

### `run_paper_bot.py:9`

```python
  - Stat Arb: 10–14 pairs, 1.6:1 RR, trailing stop, $25M liquidity filter
  - Tuned protective + sector shorts (8–18% gross, partial@1:1, trail 50%/35%)
  - Insider monitor + signal boosts + risk guard (paper only)
  - Tail Risk Controls ON (vol ceiling, DD scaling, RHYME_B buffers, sector screener)
  - Friday weekly Telegram summary (after 4:30 PM ET)
Run:
    python run_paper_bot.py
```

### `scripts\analysis\analyze_yield_gate.py:88`

```python
    lines.append("  - Live: raw gate blocks SPY / NYSE / sleeves when True")
    lines.append(
        "  - Paper (PAPER_YIELD_GATE_OVERRIDE=true): raw gate only blocks in "
        "RHYME_B/E (panic/bear); mild rate stress is softened"
    )
    lines.append("")
    lines.append(
```

### `scripts\analysis\analyze_yield_gate.py:170`

```python
    )
    lines.append(
        "  3. Live-only: add LIVE_YIELD_GATE_SOFT=true to mirror paper soft-override "
        "outside RHYME_B/E (optional; currently live stays hard)."
    )
    lines.append(
        "  4. Do NOT disable YIELD_GATE_ENABLED entirely — historical yield_gate_only "
```

### `scripts\analysis\parameter_tune.py:294`

```python
    ranked = sorted(rows, key=lambda r: (-r.composite_score, -r.sharpe, r.p5_return_pct))
    print(f"\n=== Top {min(top_n, len(ranked))} tail-tune combos ===")
    header = (
        f"{'#':>2} {'Risk%':>5} {'RHYME_B':>6} {'Hold':>4} {'VolCap':>6} "
        f"{'SPY':>4} {'Ret%':>7} {'Sharpe':>6} {'p5 Ret%':>8} {'Score':>6}"
    )
    print(header)
```

### `scripts\ops\monday_live_ops.py:178`

```python
    """Return (display line, defensive|range|growth|euphoric|unknown code)."""
    regime = (regime_raw or "").strip()
    upper = regime.upper()
    if "RHYME_B" in upper or "RHYME_E" in upper:
        return "⚠️ DEFENSIVE — reduce new entries, protect capital", "DEFENSIVE"
    if "RHYME_D" in upper:
        return "📊 RANGE — selective entries, no force-deploy", "RANGE"
```

### `scripts\ops\monday_live_ops.py:184`

```python
        return "📊 RANGE — selective entries, no force-deploy", "RANGE"
    if "RHYME_C" in upper:
        return "✅ STEADY GROWTH — standard risk", "GROWTH"
    if "RHYME_A" in upper:
        return "⚡ HIGH VOL / EUPHORIC — normal ops but watch size", "EUPHORIC"
    return "⚠️ REGIME UNKNOWN — check heartbeat path", "UNKNOWN"

```

### `scripts\ops\monday_live_ops.py:526`

```python
        )

    regime_u = (regime_raw or "").upper()
    deploy_ok = "RHYME_C" in regime_u or "RHYME_A" in regime_u
    if equity > 0 and cash_pct > EXCESS_CASH_PCT and deploy_ok:
        print("  💡 EXCESS CASH — optional deploy review")
        flags.append(
```

### `scripts\ops\overnight_research_pack.py:819`

```python
        high_stops = False
    ctx.high_stop_rate = high_stops

    if regime in ("RHYME_B", "RHYME_E") or (regime in ("RHYME_B", "RHYME_E") and high_stops):
        rec = "Reduce exposure; let positions breathe"
        if high_stops:
            rec = "Reduce exposure; let positions breathe (high stop rate overnight)"
```

### `scripts\ops\overnight_research_pack.py:827`

```python
        rec = "Normal ops; adds only on hygiene rules"
    elif regime == "RHYME_D":
        rec = "Hold; few or no new entries until regime clarifies"
    elif regime == "RHYME_A":
        rec = "Normal ops but watch size; prefer hygiene cuts over chasing"
    elif high_stops:
        rec = "Tighten risk; review stop clustering before new entries"
```

### `scripts\ops\overnight_research_pack.py:841`

```python
    _log(f"  {ctx.recommendation}", ctx=ctx)

    # Morning Cursor experiment idea (paper-only)
    if high_stops or regime in ("RHYME_B", "RHYME_E"):
        idea = (
            "Paper-only: tighten NYSE same-day reentry block / max-adds hygiene "
            "for one week; do not raise conviction floors; compare stop rate vs prior week."
```

## UNCLEAR (manually verify)

### `backtester_macro_hedge.py:238`

```python

def _sh_exit(regime: str, window: pd.DataFrame) -> bool:
    bullish, _ = _spy_market_up_signal(window, config.SPY_BOT_SYMBOL, config.SPY_MA_WINDOW)
    return bullish or regime in ("RHYME_C: Steady_Bullish_Growth", "RHYME_A: Euphoric_Volatility")


def run_macro_backtest(
```

### `config.py:7169`

```python
    if not effective_crypto_regime_filter():
        return 1.0
    reg = str(regime or "")
    if reg == "RHYME_B: Panic_Volatility":
        return 0.0
    if reg == "RHYME_E: Steady_Bearish_Decline":
        return 0.0
```

### `modules\bubble_risk.py:230`

```python
    spy = config.SPY_BOT_SYMBOL
    score = 0.0
    reg = str(regime or "")
    if "RHYME_B" in reg:
        score += 0.35
    elif "RHYME_E" in reg:
        score += 0.20
```

### `modules\bubble_risk.py:234`

```python
        score += 0.35
    elif "RHYME_E" in reg:
        score += 0.20
    if "RHYME_A" in reg:
        score += 0.15

    ma_window = config.effective_spy_ma_window()
```

### `modules\dynamic_vti_allocator.py:173`

```python
            return 0.72
        if "RHYME_E" in reg or "BEAR" in reg:
            return 0.28
        if "RHYME_B" in reg or "PANIC" in reg:
            return 0.15
        return 0.50

```

### `modules\macro_signals.py:17`

```python
from modules.pipeline_strategies import _spy_market_up_signal

BEAR_REGIME = "RHYME_E: Steady_Bearish_Decline"
PANIC_REGIME = "RHYME_B: Panic_Volatility"
_daily_cache: pd.DataFrame | None = None


```

### `modules\market_context.py:233`

```python
    bear = -base
    if not prior_regime:
        return bull, bear
    if any(tag in prior_regime for tag in ("RHYME_A", "RHYME_C")):
        bear = -(base + bump)
    elif any(tag in prior_regime for tag in ("RHYME_B", "RHYME_E")):
        bull = base + bump
```

### `modules\market_context.py:235`

```python
        return bull, bear
    if any(tag in prior_regime for tag in ("RHYME_A", "RHYME_C")):
        bear = -(base + bump)
    elif any(tag in prior_regime for tag in ("RHYME_B", "RHYME_E")):
        bull = base + bump
    elif "RHYME_D" in prior_regime:
        bull = base + bump * 0.5
```

### `modules\market_context.py:253`

```python
    bull_thresh, bear_thresh = _sentiment_thresholds(prior_regime)
    s = normalized_sentiment
    if s > bull_thresh and volatility == "High":
        return "RHYME_A: Euphoric_Volatility"
    if s < bear_thresh and volatility == "High":
        return "RHYME_B: Panic_Volatility"
    if s > bull_thresh and volatility == "Low":
```

### `modules\market_context.py:255`

```python
    if s > bull_thresh and volatility == "High":
        return "RHYME_A: Euphoric_Volatility"
    if s < bear_thresh and volatility == "High":
        return "RHYME_B: Panic_Volatility"
    if s > bull_thresh and volatility == "Low":
        return "RHYME_C: Steady_Bullish_Growth"
    if s < bear_thresh and volatility == "Low":
```

### `modules\markov_regime.py:29`

```python
HMM_STATE_KEYS = ("A", "B", "C", "D", "E")
HMM_STATE_TO_RHYME = {
    "A": "RHYME_A: Euphoric_Volatility",
    "B": "RHYME_B: Panic_Volatility",
    "C": "RHYME_C: Steady_Bullish_Growth",
    "D": "RHYME_D: Range_Bound_Neutral",
    "E": "RHYME_E: Steady_Bearish_Decline",
```

### `modules\markov_regime.py:35`

```python
    "E": "RHYME_E: Steady_Bearish_Decline",
}
RHYME_TO_HMM = {
    "RHYME_A": "A",
    "RHYME_B": "B",
    "RHYME_C": "C",
    "RHYME_D": "D",
```

### `modules\markov_regime.py:36`

```python
}
RHYME_TO_HMM = {
    "RHYME_A": "A",
    "RHYME_B": "B",
    "RHYME_C": "C",
    "RHYME_D": "D",
    "RHYME_E": "E",
```

### `modules\sector_rotation.py:272`

```python
        # Only major rhyme letter changes (A/B/C/D/E).
        def _letter(r: str) -> str:
            u = r.upper()
            for L in ("RHYME_A", "RHYME_B", "RHYME_C", "RHYME_D", "RHYME_E"):
                if L in u:
                    return L
            return u[:12]
```

### `modules\sharpe_history.py:204`

```python

def _regime_adj(regime: str) -> tuple[float, str]:
    reg = (regime or "").upper()
    if "RHYME_B" in reg:
        return -0.22, "RHYME_B"
    if "RHYME_E" in reg:
        return -0.12, "RHYME_E"
```

### `modules\sharpe_history.py:205`

```python
def _regime_adj(regime: str) -> tuple[float, str]:
    reg = (regime or "").upper()
    if "RHYME_B" in reg:
        return -0.22, "RHYME_B"
    if "RHYME_E" in reg:
        return -0.12, "RHYME_E"
    if "RHYME_C" in reg or "RHYME_D" in reg:
```

### `modules\sharpe_history.py:210`

```python
        return -0.12, "RHYME_E"
    if "RHYME_C" in reg or "RHYME_D" in reg:
        return 0.06, "RHYME_C/D"
    if "RHYME_A" in reg:
        return 0.04, "RHYME_A"
    return 0.0, (regime.split(":")[-1].strip() if regime else "n/a")

```

### `modules\sharpe_history.py:211`

```python
    if "RHYME_C" in reg or "RHYME_D" in reg:
        return 0.06, "RHYME_C/D"
    if "RHYME_A" in reg:
        return 0.04, "RHYME_A"
    return 0.0, (regime.split(":")[-1].strip() if regime else "n/a")


```

### `modules\wisdom_adaptor.py:13`

```python
from modules.wayback_sentiment import normalize_price_sentiment

BEAR_REGIME = "RHYME_E: Steady_Bearish_Decline"
PANIC_REGIME = "RHYME_B: Panic_Volatility"
BULL_REGIME = "RHYME_C: Steady_Bullish_Growth"


```

### `modules\wisdom_layer.py:65`

```python
class WisdomAdvisor:
    """Produce a single actionable core-shift recommendation per cycle."""

    BEAR_MARKERS = ("RHYME_E", "RHYME_B", "Bearish", "Panic")
    BULL_MARKERS = ("RHYME_C", "RHYME_A", "Bullish")

    def recommend(
```

### `modules\wisdom_layer.py:66`

```python
    """Produce a single actionable core-shift recommendation per cycle."""

    BEAR_MARKERS = ("RHYME_E", "RHYME_B", "Bearish", "Panic")
    BULL_MARKERS = ("RHYME_C", "RHYME_A", "Bullish")

    def recommend(
        self,
```

### `scripts\analysis\regime_effectiveness.py:100`

```python

def _regime_at_threshold(sentiment: float, vol: str, thresh: float) -> str:
    if sentiment > thresh and vol == "High":
        return "RHYME_A: Euphoric_Volatility"
    if sentiment < -thresh and vol == "High":
        return "RHYME_B: Panic_Volatility"
    if sentiment > thresh and vol == "Low":
```

### `scripts\analysis\regime_effectiveness.py:102`

```python
    if sentiment > thresh and vol == "High":
        return "RHYME_A: Euphoric_Volatility"
    if sentiment < -thresh and vol == "High":
        return "RHYME_B: Panic_Volatility"
    if sentiment > thresh and vol == "Low":
        return "RHYME_C: Steady_Bullish_Growth"
    if sentiment < -thresh and vol == "Low":
```

### `scripts\analysis\sleeve_overlap_analysis.py:248`

```python


REGIME_BUCKET = {
    "RHYME_A: Euphoric_Volatility": "high_vol",
    "RHYME_B: Panic_Volatility": "bear",
    "RHYME_C: Steady_Bullish_Growth": "bull",
    "RHYME_D: Range_Bound_Neutral": "neutral",
```

### `scripts\analysis\sleeve_overlap_analysis.py:249`

```python

REGIME_BUCKET = {
    "RHYME_A: Euphoric_Volatility": "high_vol",
    "RHYME_B: Panic_Volatility": "bear",
    "RHYME_C: Steady_Bullish_Growth": "bull",
    "RHYME_D: Range_Bound_Neutral": "neutral",
    "RHYME_E: Steady_Bearish_Decline": "bear",
```

### `scripts\analysis\weekly_review.py:87`

```python

TRADE_EVENTS = {"exit", "sell", "close"}
BUY_EVENTS = {"buy", "entry", "open"}
DataGrade = Literal["A", "B", "C"]


@dataclass
```

### `scripts\analysis\weekly_review.py:145`

```python

    @property
    def usable_for_mandate(self) -> bool:
        return self.grade in ("A", "B")


@dataclass
```

### `scripts\ops\overnight_research_pack.py:177`

```python

def _regime_key(raw: str | None) -> str:
    text = (raw or "").strip().upper()
    for tag in ("RHYME_A", "RHYME_B", "RHYME_C", "RHYME_D", "RHYME_E"):
        if tag in text:
            return tag
    return (raw or "unknown").strip() or "unknown"
```

### `scripts\verify_insider_integration.py:164`

```python
        if baseline_mom or baseline_sa:
            test_guard = apply_insider_signals_to_strategies(
                bubble_score_100=90.0,
                regime="RHYME_B: Panic_Volatility",
            )
            live_mom = test_guard.get("momentum_boosts") or {}
            if all(float(live_mom.get(k, 0.0)) == 0.0 for k in baseline_mom):
```

## REPORT (informational only, lower priority)

### `modules\backtester_core.py:447`

```python


RHYME_REGIME_LABELS: tuple[str, ...] = (
    "RHYME_A: Euphoric_Volatility",
    "RHYME_B: Panic_Volatility",
    "RHYME_C: Steady_Bullish_Growth",
    "RHYME_D: Range_Bound_Neutral",
```

### `modules\backtester_core.py:448`

```python

RHYME_REGIME_LABELS: tuple[str, ...] = (
    "RHYME_A: Euphoric_Volatility",
    "RHYME_B: Panic_Volatility",
    "RHYME_C: Steady_Bullish_Growth",
    "RHYME_D: Range_Bound_Neutral",
    "RHYME_E: Steady_Bearish_Decline",
```

### `modules\markov_regime.py:26`

```python
logger = logging.getLogger(__name__)

# 5 HMM states ↔ RHYME labels (ordered after emission labeling)
HMM_STATE_KEYS = ("A", "B", "C", "D", "E")
HMM_STATE_TO_RHYME = {
    "A": "RHYME_A: Euphoric_Volatility",
    "B": "RHYME_B: Panic_Volatility",
```

### `modules\markov_regime.py:28`

```python
# 5 HMM states ↔ RHYME labels (ordered after emission labeling)
HMM_STATE_KEYS = ("A", "B", "C", "D", "E")
HMM_STATE_TO_RHYME = {
    "A": "RHYME_A: Euphoric_Volatility",
    "B": "RHYME_B: Panic_Volatility",
    "C": "RHYME_C: Steady_Bullish_Growth",
    "D": "RHYME_D: Range_Bound_Neutral",
```

### `modules\opportunistic_short_sleeve.py:17`

```python
logger = logging.getLogger(__name__)

BEARISH_REGIMES = (
    "RHYME_B: Panic_Volatility",
    "RHYME_E: Steady_Bearish_Decline",
)

```

### `modules\thinking_engine.py:318`

```python


def _regime_letter_index(regime: Any) -> int | None:
    """RHYME_A→0 … RHYME_E→4 for step-distance checks."""
    label = _normalize_regime_label(regime)
    if not label.startswith("RHYME_") or len(label) < 7:
        return None
```

### `modules\thinking_engine.py:462`

```python
    raw = str(regime or "")
    desc = raw.split(":", 1)[1].strip() if ":" in raw else ""
    catalog = {
        "RHYME_A": "Steady_Bullish_Growth",
        "RHYME_B": "Early_Bull_Recovery",
        "RHYME_C": "Steady_Bullish_Growth",
        "RHYME_D": "Range_Bound_Neutral",
```

### `modules\thinking_engine.py:463`

```python
    desc = raw.split(":", 1)[1].strip() if ":" in raw else ""
    catalog = {
        "RHYME_A": "Steady_Bullish_Growth",
        "RHYME_B": "Early_Bull_Recovery",
        "RHYME_C": "Steady_Bullish_Growth",
        "RHYME_D": "Range_Bound_Neutral",
        "RHYME_E": "Bearish_Decline",
```
