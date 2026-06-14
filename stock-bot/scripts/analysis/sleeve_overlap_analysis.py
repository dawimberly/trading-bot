"""Sleeve overlap, correlation, and concurrent-risk analysis (SPY / NYSE / crypto).

Run from repo root:
  python scripts/analysis/sleeve_overlap_analysis.py
  python scripts/analysis/sleeve_overlap_analysis.py --max
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config
from modules.crypto_vol_gate import crypto_trading_allowed
from modules.data_loader import load_close_matrix
from modules.market_context import (
    get_market_regime,
    get_price_sentiment,
    get_volatility,
)
from modules.pipeline_strategies import (
    COOLDOWN_SECONDS,
    PAUSED_REGIMES,
    _crypto_pair_z,
    _equity_momentum_candidates,
    _spy_market_up_signal,
    crypto_trade_intents,
)

MIN_HISTORY = max(50, config.SPY_MA_WINDOW)
DAILY_COOLDOWN_BARS = 1
CRYPTO_Z_THRESHOLD = 2.0

# Manual sector tags for overlap narrative (not exhaustive GICS)
SECTOR_MAP = {
    "AAPL": "Tech",
    "MSFT": "Tech",
    "NVDA": "Tech",
    "AMD": "Tech",
    "GOOGL": "Tech",
    "AMZN": "Tech",
    "TSLA": "Tech",
    "META": "Tech",
    "VTI": "Broad",
    "QQQ": "Tech/Broad",
    "IWM": "SmallCap",
    "XOM": "Energy",
    "CVX": "Energy",
    "LNG": "Energy",
    "RTX": "Defense",
    "LMT": "Defense",
    "KTOS": "Defense",
    "JPM": "Financials",
    "BAC": "Financials",
    "GS": "Financials",
    "JNJ": "Healthcare",
    "UNH": "Healthcare",
    "PFE": "Healthcare",
    "URA": "Commodity",
    "PPLT": "Commodity",
    "DBB": "Commodity",
    "GDX": "Commodity",
}


def _nyse_universe_columns(data: pd.DataFrame) -> list[str]:
    return [
        c
        for c in data.columns
        if not config.is_crypto(c)
        and c != config.SPY_BOT_SYMBOL
        and not config.is_metal_symbol(c)
    ]


def _on_cooldown(pair_cooldown, key, now, cooldown_bars=DAILY_COOLDOWN_BARS):
    last = pair_cooldown.get(key)
    if last is None:
        return False
    return (now - last) < cooldown_bars


def _crypto_candidates(data: pd.DataFrame) -> list[tuple]:
    crypto_cols = [c for c in data.columns if config.is_crypto(c)]
    out = []
    for i in range(len(crypto_cols)):
        for j in range(i + 1, len(crypto_cols)):
            t1, t2 = crypto_cols[i], crypto_cols[j]
            if data[t1].corr(data[t2]) < config.CRYPTO_MIN_CORRELATION:
                continue
            z = _crypto_pair_z(data, t1, t2)
            if abs(z) > CRYPTO_Z_THRESHOLD:
                out.append((abs(z), z, t1, t2))
    out.sort(reverse=True)
    return out


def simulate_sleeve_signals(data: pd.DataFrame) -> pd.DataFrame:
    """Per-bar sleeve intents (mirrors backtester daily loop; crypto notional ignored)."""
    rows = []
    pair_cooldown: dict = {}

    for i in range(MIN_HISTORY, len(data)):
        window = data.iloc[: i + 1]
        ts = data.index[i]
        sentiment = get_price_sentiment(window)
        vol = get_volatility(window)
        regime = get_market_regime(sentiment, vol)
        regime_paused = regime in PAUSED_REGIMES

        spy_sym = config.SPY_BOT_SYMBOL
        spy_bull, spy_mom = _spy_market_up_signal(window, spy_sym, config.SPY_MA_WINDOW)
        spy_key = f"{spy_sym}/MA{config.SPY_MA_WINDOW}"
        spy_wants = spy_bull and not regime_paused
        spy_fires = spy_wants and not _on_cooldown(pair_cooldown, spy_key, i)
        if spy_fires:
            pair_cooldown[spy_key] = i

        equity_cols = _nyse_universe_columns(window)
        ranked = _equity_momentum_candidates(window, equity_cols)
        nyse_top = ranked[0] if ranked else None
        nyse_wants = bool(ranked) and not regime_paused
        nyse_key = f"{nyse_top}/MA50" if nyse_top else None
        nyse_fires = (
            nyse_wants
            and nyse_key
            and not _on_cooldown(pair_cooldown, nyse_key, i)
        )
        if nyse_fires:
            pair_cooldown[nyse_key] = i

        gate = crypto_trading_allowed(vol, regime, spacex_snapshot=None)
        crypto_allowed = gate["allowed"]
        crypto_pairs = _crypto_candidates(window) if crypto_allowed else []
        crypto_intents = []
        if crypto_allowed:
            crypto_intents = crypto_trade_intents(
                window,
                regime,
                i,
                pair_cooldown,
                cooldown_bars=DAILY_COOLDOWN_BARS,
                volatility=vol,
                spacex_snapshot=None,
                notional=1.0,
            )
        crypto_fires = len(crypto_intents) > 0

        spy_ret = float(window[spy_sym].pct_change().iloc[-1]) if spy_sym in window else np.nan
        nyse_ret = (
            float(window[nyse_top].pct_change().iloc[-1])
            if nyse_top and nyse_top in window
            else np.nan
        )
        crypto_ret = np.nan
        if crypto_pairs:
            _az, z, t1, t2 = crypto_pairs[0]
            spread = window[t1] - window[t2]
            crypto_ret = float(spread.pct_change().iloc[-1])

        rows.append(
            {
                "date": ts,
                "regime": regime,
                "vol": vol,
                "sentiment": sentiment,
                "regime_paused": regime_paused,
                "spy_bull": spy_bull,
                "spy_wants": spy_wants,
                "spy_fires": spy_fires,
                "spy_momentum": spy_mom,
                "nyse_top": nyse_top,
                "nyse_wants": nyse_wants,
                "nyse_fires": nyse_fires,
                "crypto_allowed": crypto_allowed,
                "crypto_gate_reason": gate["reason"],
                "crypto_fires": crypto_fires,
                "crypto_n_pairs": len(crypto_pairs),
                "spy_ret": spy_ret,
                "nyse_ret": nyse_ret,
                "crypto_spread_ret": crypto_ret,
                "triple_wants": spy_wants and nyse_wants and crypto_allowed,
                "triple_fires": spy_fires and nyse_fires and crypto_fires,
                "any_two_fires": sum([spy_fires, nyse_fires, crypto_fires]) >= 2,
            }
        )

    return pd.DataFrame(rows).set_index("date")


def nyse_pick_stats(signals: pd.DataFrame, data: pd.DataFrame) -> dict:
    """Frequency and SPY correlation of NYSE top picks."""
    picks = signals["nyse_top"].dropna()
    pick_counts = Counter(picks)
    total_days = len(signals)
    pick_days = len(picks)

    spy = data["SPY"].pct_change().dropna()
    betas = {}
    corrs = {}
    for sym in _nyse_universe_columns(data):
        if sym not in data.columns:
            continue
        s = data[sym].pct_change().dropna()
        aligned = pd.concat([s, spy], axis=1, join="inner").dropna()
        if len(aligned) < 60:
            continue
        corrs[sym] = float(aligned.iloc[-252:].corr().iloc[0, 1])
        cov = aligned.iloc[-252:].cov().iloc[0, 1]
        var_spy = aligned.iloc[-252:, 1].var()
        betas[sym] = float(cov / var_spy) if var_spy > 0 else np.nan

    top_pick_corrs = {
        sym: corrs.get(sym, np.nan)
        for sym, _ in pick_counts.most_common(10)
    }
    sector_when_top = Counter(SECTOR_MAP.get(s, "Other") for s in picks)

    return {
        "nyse_universe": sorted(_nyse_universe_columns(data)),
        "excludes_spy": True,
        "excludes_crypto": True,
        "excludes_live_metals": sorted(config.LIVE_METAL_SYMBOLS),
        "days_with_momentum_pick": pick_days,
        "days_spy_above_ma200": int(signals["spy_bull"].sum()),
        "top_pick_counts": dict(pick_counts.most_common(15)),
        "sector_distribution_when_top": dict(sector_when_top),
        "spy_correlation_by_universe": dict(
            sorted(corrs.items(), key=lambda x: -abs(x[1]))
        ),
        "top_picks_spy_corr": top_pick_corrs,
        "top_picks_beta_spy": {
            sym: betas.get(sym, np.nan)
            for sym, _ in pick_counts.most_common(10)
        },
    }


REGIME_BUCKET = {
    "RHYME_A: Euphoric_Volatility": "high_vol",
    "RHYME_B: Panic_Volatility": "bear",
    "RHYME_C: Steady_Bullish_Growth": "bull",
    "RHYME_D: Range_Bound_Neutral": "neutral",
    "RHYME_E: Steady_Bearish_Decline": "bear",
}


def _corr(a, b):
    m = pd.concat([a, b], axis=1).dropna()
    if len(m) < 20 or m.iloc[:, 0].std() == 0 or m.iloc[:, 1].std() == 0:
        return np.nan
    return float(m.iloc[:, 0].corr(m.iloc[:, 1]))


def _regime_distribution(signals: pd.DataFrame) -> dict:
    by_rhyme = signals["regime"].value_counts().to_dict()
    by_vol = signals["vol"].value_counts().to_dict()
    sent = signals["sentiment"]
    return {
        "rhyme_counts": by_rhyme,
        "vol_counts": by_vol,
        "sentiment_min": round(float(sent.min()), 4),
        "sentiment_max": round(float(sent.max()), 4),
        "sentiment_pct_above_0_5": round(100 * (sent > 0.5).mean(), 2),
        "note": (
            "Live get_market_regime() needs sentiment beyond ±0.5; daily price "
            "sentiment rarely hits that, so rhyme buckets often collapse to "
            "RHYME_D. Use derived_buckets for historical splits."
        ),
    }


def _derive_bucket(row) -> str:
    """Mutually exclusive buckets for historical correlation splits."""
    if row["regime_paused"] or REGIME_BUCKET.get(row["regime"]) == "bear":
        return "bear"
    if not row["spy_bull"]:
        return "bear"
    if row["vol"] == "High":
        return "high_vol"
    return "bull"


def correlation_by_regime(signals: pd.DataFrame) -> dict:
    """SPY / NYSE / crypto correlations by rhyme and derived bull/bear/high_vol/neutral."""
    df = signals.copy()
    df["rhyme_bucket"] = df["regime"].map(REGIME_BUCKET).fillna("neutral")
    df["derived_bucket"] = df.apply(_derive_bucket, axis=1)

    def _bucket_corr(sub: pd.DataFrame) -> dict:
        if len(sub) < 30:
            return {"days": len(sub), "note": "insufficient sample"}
        spy = sub["spy_ret"].where(sub["spy_wants"])
        nyse = sub["nyse_ret"].where(sub["nyse_wants"])
        crypto = sub["crypto_spread_ret"].where(sub["crypto_allowed"])
        return {
            "days": len(sub),
            "pct_of_sample": round(100 * len(sub) / len(df), 1),
            "corr_spy_nyse_when_wants": _corr(spy, nyse),
            "corr_spy_crypto_when_wants": _corr(spy, crypto),
            "corr_nyse_crypto_when_wants": _corr(nyse, crypto),
            "corr_spy_nyse_on_fire": _corr(
                sub["spy_ret"].where(sub["spy_fires"]),
                sub["nyse_ret"].where(sub["nyse_fires"]),
            ),
            "pct_days_spy_wants": round(100 * sub["spy_wants"].mean(), 1),
            "pct_days_crypto_allowed": round(100 * sub["crypto_allowed"].mean(), 1),
        }

    rhyme_out = {}
    for bucket in ("bull", "bear", "high_vol", "neutral"):
        sub = df[df["rhyme_bucket"] == bucket]
        rhyme_out[bucket] = _bucket_corr(sub)

    derived_out = {}
    for bucket in ("bull", "bear", "high_vol", "neutral"):
        sub = df[df["derived_bucket"] == bucket]
        derived_out[bucket] = _bucket_corr(sub)

    return {
        "regime_distribution": _regime_distribution(signals),
        "rhyme_buckets": rhyme_out,
        "derived_buckets": derived_out,
    }


def correlation_by_volatility(signals: pd.DataFrame) -> dict:
    """Split correlations by cross-asset vol High/Low (crypto gate driver)."""
    out = {}
    for vol in ("High", "Low"):
        sub = signals[signals["vol"] == vol]
        if len(sub) < 30:
            out[vol] = {"days": len(sub), "note": "insufficient sample"}
            continue
        spy = sub["spy_ret"].where(sub["spy_wants"])
        nyse = sub["nyse_ret"].where(sub["nyse_wants"])
        crypto = sub["crypto_spread_ret"].where(sub["crypto_allowed"])
        out[vol] = {
            "days": len(sub),
            "pct_of_sample": round(100 * len(sub) / len(signals), 1),
            "corr_spy_nyse_when_wants": _corr(spy, nyse),
            "corr_spy_crypto_when_wants": _corr(spy, crypto),
            "corr_nyse_crypto_when_wants": _corr(nyse, crypto),
            "pct_days_crypto_allowed": round(100 * sub["crypto_allowed"].mean(), 1),
            "pct_days_triple_fires": round(100 * sub["triple_fires"].mean(), 1),
        }
    return out


def cofire_and_spikes(signals: pd.DataFrame, window: int = 20) -> dict:
    """Co-fire days and rolling-correlation spikes when multiple sleeves active."""
    df = signals.copy()
    active_count = (
        df["spy_fires"].astype(int)
        + df["nyse_fires"].astype(int)
        + df["crypto_fires"].astype(int)
    )
    df["active_sleeves"] = active_count

    spy_s = df["spy_ret"].where(df["spy_wants"])
    nyse_s = df["nyse_ret"].where(df["nyse_wants"])
    crypto_s = df["crypto_spread_ret"].where(df["crypto_allowed"])
    roll = pd.DataFrame(
        {
            "spy_nyse": spy_s.rolling(window).corr(nyse_s),
            "spy_crypto": spy_s.rolling(window).corr(crypto_s),
            "nyse_crypto": nyse_s.rolling(window).corr(crypto_s),
        }
    )

    cofire_2 = df[active_count >= 2]
    cofire_3 = df[df["triple_fires"]]
    solo = df[active_count <= 1]

    def _spike_stats(series, label):
        valid = series.dropna()
        if valid.empty:
            return {}
        p90 = float(valid.quantile(0.9))
        spikes = valid[valid >= p90]
        return {
            f"{label}_roll_p90": round(p90, 3),
            f"{label}_spike_days_ge_p90": int(len(spikes)),
            f"{label}_max_roll": round(float(valid.max()), 3),
        }

    base = {
        "rolling_window_days": window,
        "days_two_plus_fires": len(cofire_2),
        "days_triple_fires": len(cofire_3),
        "pct_sample_two_plus_fires": round(100 * len(cofire_2) / len(df), 1),
        "spy_nyse_corr_on_cofire2": _corr(
            cofire_2["spy_ret"], cofire_2["nyse_ret"]
        ),
        "spy_nyse_corr_on_solo": _corr(solo["spy_ret"], solo["nyse_ret"]),
        "spy_crypto_corr_on_cofire2": _corr(
            cofire_2["spy_ret"], cofire_2["crypto_spread_ret"]
        ),
        "spy_crypto_corr_on_solo": _corr(
            solo["spy_ret"], solo["crypto_spread_ret"]
        ),
    }
    for col in roll.columns:
        base.update(_spike_stats(roll[col], col))
        on_cofire = roll[col].reindex(cofire_2.index).dropna()
        if len(on_cofire) >= 5:
            base[f"{col}_mean_on_cofire2"] = round(float(on_cofire.mean()), 3)
    return base


def diversification_effectiveness(signals: pd.DataFrame) -> dict:
    """Compare capped multi-sleeve proxy vs single-sleeve-only curves."""
    alloc = config.fund_allocation_pct()

    def _sleeve_series(mask_col, ret_col, weight):
        return signals[ret_col].where(signals[mask_col]) * weight

    spy_r = _sleeve_series("spy_fires", "spy_ret", alloc["spy"])
    nyse_r = _sleeve_series("nyse_fires", "nyse_ret", alloc["nyse"])
    crypto_r = _sleeve_series("crypto_fires", "crypto_spread_ret", alloc["crypto"])
    combined = spy_r.fillna(0) + nyse_r.fillna(0) + crypto_r.fillna(0)

    def _stats(series, name):
        s = series.dropna()
        if len(s) < 30:
            return {"name": name, "note": "insufficient"}
        vol = float(s.std() * np.sqrt(252))
        cum = (1 + s).cumprod()
        total_ret = float(cum.iloc[-1] - 1)
        dd = (cum / cum.cummax() - 1).min()
        sharpe = (
            float(s.mean() / s.std() * np.sqrt(252)) if s.std() > 0 else np.nan
        )
        return {
            "name": name,
            "ann_vol_pct": round(vol * 100, 2),
            "total_return_pct": round(total_ret * 100, 2),
            "max_drawdown_pct": round(float(dd) * 100, 2),
            "sharpe_proxy": round(sharpe, 3),
            "active_days": int((series != 0).sum()),
        }

    naive_sum_vol = float(
        (spy_r.fillna(0).std() + nyse_r.fillna(0).std() + crypto_r.fillna(0).std())
        * np.sqrt(252)
    )
    combined_vol = float(combined.std() * np.sqrt(252))
    vol_reduction = (
        round(100 * (1 - combined_vol / naive_sum_vol), 1)
        if naive_sum_vol > 0
        else None
    )

    return {
        "sleeve_stats": [
            _stats(spy_r, "spy_only_fires"),
            _stats(nyse_r, "nyse_only_fires"),
            _stats(crypto_r, "crypto_only_fires"),
            _stats(combined, "capped_combined_fires"),
        ],
        "naive_sum_daily_vol_ann_pct": round(naive_sum_vol * 100, 2),
        "combined_ann_vol_pct": round(combined_vol * 100, 2),
        "vol_reduction_vs_naive_sum_pct": vol_reduction,
        "interpretation": (
            "Positive vol_reduction means combined sleeve returns are less volatile "
            "than summing individual sleeve vols (diversification benefit)."
        ),
    }


def testable_improvements(report: dict) -> list[dict]:
    """Concrete hypotheses from data; preserves caps/architecture."""
    items = []
    overlap = report.get("overlap_spy_nyse", {})
    nyse = report.get("nyse_universe_stats", {})
    conc = report.get("concurrent_risk", {})
    div = report.get("diversification_effectiveness", {})
    cofire = report.get("cofire_spikes", {})

    med_corr = overlap.get("median_60d_corr_nyse_pick_vs_spy")
    if med_corr is not None and med_corr > 0.75:
        items.append(
            {
                "id": "NYSE_TECH_CAP",
                "change": "When SPY sleeve active, block NYSE picks with 60d SPY corr > 0.85 "
                "or rotate to second-ranked momentum name.",
                "metric": f"median pick-SPY corr={med_corr}",
                "preserves": "45/20/20 caps; same MA50 + SPY MA200 logic",
            }
        )

    pct_tech = overlap.get("pct_nyse_pick_tech_when_spy_on")
    if pct_tech is not None and pct_tech > 40:
        items.append(
            {
                "id": "NYSE_SECTOR_ROTATE",
                "change": "Cap single-sector (Tech) to 1 of top-3 momentum slots per week.",
                "metric": f"{pct_tech}% tech top-pick when SPY on",
                "preserves": "NYSE 20% cap; pipeline_strategies ranking",
            }
        )

    triple = conc.get("pct_days_triple_wants", 0)
    if triple > 5:
        items.append(
            {
                "id": "CRYPTO_VOL_GATE_STRICT",
                "change": "Require CRYPTO_VOL_ONLY + vol High for crypto when SPY+NYSE both want "
                "(no SpaceX override on triple-want days).",
                "metric": f"{triple}% days triple-want",
                "preserves": "crypto 20% cap; existing vol gate module",
            }
        )

    cofire2 = cofire.get("days_two_plus_fires", 0)
    spy_nyse_cofire = cofire.get("spy_nyse_corr_on_cofire2")
    spy_nyse_solo = cofire.get("spy_nyse_corr_on_solo")
    if (
        cofire2 > 20
        and spy_nyse_cofire is not None
        and spy_nyse_solo is not None
        and abs(spy_nyse_cofire) > abs(spy_nyse_solo) + 0.1
    ):
        items.append(
            {
                "id": "COFIRE_NYSE_DEFER",
                "change": "On days SPY fires, defer NYSE entry 1 bar unless top pick beta < 0.9.",
                "metric": f"cofire corr {spy_nyse_cofire:.2f} vs solo {spy_nyse_solo:.2f}",
                "preserves": "cooldown keys; run_equity_strategy interface",
            }
        )

    vol_red = (div.get("vol_reduction_vs_naive_sum_pct") or 0)
    if vol_red < 5:
        items.append(
            {
                "id": "ALLOC_REBALANCE_TEST",
                "change": "Backtest shifting 5% from NYSE to crypto when weekly SPY-NYSE corr > 0.6.",
                "metric": f"vol_reduction only {vol_red}%",
                "preserves": "85% deployed; effective_sleeve_cap() pattern",
            }
        )

    if not items:
        items.append(
            {
                "id": "MONITOR_ONLY",
                "change": "Re-run monthly; no structural change until cofire corr > 0.8 in bull bucket.",
                "metric": "thresholds not breached",
                "preserves": "all caps",
            }
        )
    return items


def correlation_analysis(signals: pd.DataFrame) -> dict:
    """Return proxies: in-signal-day returns vs active-signal masks."""
    df = signals.copy()

    # Proxy A: return on days sleeve *wants* to be active (regime + signal)
    spy_series = df["spy_ret"].where(df["spy_wants"])
    nyse_series = df["nyse_ret"].where(df["nyse_wants"])
    crypto_series = df["crypto_spread_ret"].where(df["crypto_allowed"])

    # Proxy B: return only on *fire* days (cooldown-aware entries)
    spy_fire = df["spy_ret"].where(df["spy_fires"])
    nyse_fire = df["nyse_ret"].where(df["nyse_fires"])
    crypto_fire = df["crypto_spread_ret"].where(df["crypto_fires"])

    weekly = df[["spy_ret", "nyse_ret", "crypto_spread_ret"]].resample("W").sum()

    return {
        "daily_corr_spy_nyse_when_wants": _corr(spy_series, nyse_series),
        "daily_corr_spy_crypto_when_wants": _corr(spy_series, crypto_series),
        "daily_corr_nyse_crypto_when_wants": _corr(nyse_series, crypto_series),
        "daily_corr_spy_nyse_on_fire_days": _corr(spy_fire, nyse_fire),
        "daily_corr_spy_crypto_on_fire_days": _corr(spy_fire, crypto_fire),
        "daily_corr_nyse_crypto_on_fire_days": _corr(nyse_fire, crypto_fire),
        "weekly_corr_spy_nyse": _corr(weekly["spy_ret"], weekly["nyse_ret"]),
        "weekly_corr_spy_crypto": _corr(
            weekly["spy_ret"], weekly["crypto_spread_ret"]
        ),
        "weekly_corr_nyse_crypto": _corr(
            weekly["nyse_ret"], weekly["crypto_spread_ret"]
        ),
        "note": (
            "Correlations use same-day returns; SPY sleeve proxy = SPY return when "
            "above MA200 & not paused; NYSE = top momentum name return; crypto = "
            "strongest pair spread return when vol gate open (not leg P&L)."
        ),
    }


def cofire_counts(signals: pd.DataFrame) -> dict:
    """Pairwise and triple co-fire / co-want day counts."""
    n = len(signals)
    spy = signals["spy_fires"]
    nyse = signals["nyse_fires"]
    crypto = signals["crypto_fires"]
    spy_w = signals["spy_wants"]
    nyse_w = signals["nyse_wants"]
    crypto_w = signals["crypto_allowed"]

    def _pct(count):
        return round(100 * count / n, 1) if n else 0.0

    return {
        "co_want_spy_nyse": int((spy_w & nyse_w).sum()),
        "co_want_spy_crypto": int((spy_w & crypto_w).sum()),
        "co_want_nyse_crypto": int((nyse_w & crypto_w).sum()),
        "co_want_all_three": int(signals["triple_wants"].sum()),
        "co_fire_spy_nyse": int((spy & nyse).sum()),
        "co_fire_spy_crypto": int((spy & crypto).sum()),
        "co_fire_nyse_crypto": int((nyse & crypto).sum()),
        "co_fire_all_three": int(signals["triple_fires"].sum()),
        "co_fire_any_two": int(signals["any_two_fires"].sum()),
        "pct_co_fire_spy_nyse": _pct(int((spy & nyse).sum())),
        "pct_co_fire_spy_crypto": _pct(int((spy & crypto).sum())),
        "pct_co_fire_nyse_crypto": _pct(int((nyse & crypto).sum())),
        "pct_co_fire_all_three": _pct(int(signals["triple_fires"].sum())),
        "pct_co_fire_any_two": _pct(int(signals["any_two_fires"].sum())),
    }


def concurrent_risk(signals: pd.DataFrame, data: pd.DataFrame) -> dict:
    """Triple/double fire counts and drawdown on high-exposure days."""
    n = len(signals)
    triple_wants = int(signals["triple_wants"].sum())
    triple_fires = int(signals["triple_fires"].sum())
    any_two = int(signals["any_two_fires"].sum())

    regime_triple = (
        signals.loc[signals["triple_wants"]]
        .groupby("regime")
        .size()
        .sort_values(ascending=False)
        .to_dict()
    )

    vol_triple = (
        signals.loc[signals["triple_wants"]]
        .groupby("vol")
        .size()
        .to_dict()
    )

    # Simple combined proxy equity curve: sum capped sleeve returns on fire days
    alloc = config.fund_allocation_pct()
    rets = []
    for _, row in signals.iterrows():
        r = 0.0
        if row["spy_fires"] and np.isfinite(row["spy_ret"]):
            r += alloc["spy"] * row["spy_ret"]
        if row["nyse_fires"] and np.isfinite(row["nyse_ret"]):
            r += alloc["nyse"] * row["nyse_ret"]
        if row["crypto_fires"] and np.isfinite(row["crypto_spread_ret"]):
            r += alloc["crypto"] * row["crypto_spread_ret"]
        rets.append(r)
    proxy = pd.Series(rets, index=signals.index)
    cum = (1 + proxy).cumprod()
    dd = (cum / cum.cummax() - 1)
    max_dd = float(dd.min())

    high_exposure = signals["any_two_fires"] | signals["triple_fires"]
    dd_high = dd[high_exposure]
    dd_low = dd[~high_exposure]

    worst_clusters = []
    in_cluster = False
    cluster_start = None
    for dt, d in dd.items():
        if d < -0.02 and high_exposure.loc[dt]:
            if not in_cluster:
                in_cluster = True
                cluster_start = dt
        else:
            if in_cluster:
                worst_clusters.append(
                    {
                        "start": str(cluster_start.date()),
                        "end": str(dt.date()),
                        "min_dd_pct": round(float(dd.loc[cluster_start:dt].min()) * 100, 2),
                    }
                )
                in_cluster = False

    return {
        "simulation_days": n,
        "pct_days_spy_wants": round(100 * signals["spy_wants"].mean(), 1),
        "pct_days_nyse_wants": round(100 * signals["nyse_wants"].mean(), 1),
        "pct_days_crypto_allowed": round(100 * signals["crypto_allowed"].mean(), 1),
        "pct_days_triple_wants": round(100 * triple_wants / n, 1),
        "pct_days_triple_fires": round(100 * triple_fires / n, 1),
        "pct_days_any_two_fires": round(100 * any_two / n, 1),
        "triple_wants_by_regime": regime_triple,
        "triple_wants_by_vol": vol_triple,
        "proxy_max_drawdown_pct": round(max_dd * 100, 2),
        "avg_dd_on_high_exposure_days_pct": round(float(dd_high.mean()) * 100, 3)
        if len(dd_high)
        else None,
        "avg_dd_on_other_days_pct": round(float(dd_low.mean()) * 100, 3)
        if len(dd_low)
        else None,
        "drawdown_clusters_ge_2pct": worst_clusters[:8],
    }


def overlap_with_spy_holdings(signals: pd.DataFrame, data: pd.DataFrame) -> dict:
    """When SPY sleeve active, how correlated is NYSE pick to SPY?"""
    both = signals[signals["spy_wants"] & signals["nyse_wants"]].copy()
    if both.empty:
        return {"days_both_want": 0}

    corrs = []
    for dt, row in both.iterrows():
        sym = row["nyse_top"]
        if sym not in data.columns:
            continue
        w = data.loc[:dt, [sym, "SPY"]].tail(60).pct_change().dropna()
        if len(w) < 30:
            continue
        corrs.append(w[sym].corr(w["SPY"]))

    qqq_overlap = int((both["nyse_top"] == "QQQ").sum())
    nvda_overlap = int((both["nyse_top"] == "NVDA").sum())
    tech_top = int(both["nyse_top"].isin(
        ["NVDA", "AMD", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "QQQ"]
    ).sum())

    return {
        "days_both_spy_and_nyse_want": len(both),
        "pct_nyse_pick_tech_when_spy_on": round(100 * tech_top / len(both), 1),
        "qqq_as_top_pick_when_spy_on": qqq_overlap,
        "nvda_as_top_pick_when_spy_on": nvda_overlap,
        "median_60d_corr_nyse_pick_vs_spy": round(float(np.median(corrs)), 3)
        if corrs
        else None,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", action="store_true", help="Use full daily history")
    parser.add_argument("--days", type=int, default=config.BACKTEST_DAYS)
    parser.add_argument("--json", action="store_true", help="Print JSON only")
    args = parser.parse_args()

    data = load_close_matrix(interval="1d", days=None if args.max else args.days)
    if len(data) < MIN_HISTORY + 10:
        print(f"Insufficient rows ({len(data)}); run fetch_data.py --daily")
        return 1

    signals = simulate_sleeve_signals(data)
    report = {
        "data_window": {
            "start": str(signals.index[0].date()),
            "end": str(signals.index[-1].date()),
            "bars": len(signals),
            "tickers": len(data.columns),
        },
        "config_caps": config.fund_allocation_pct(),
        "paused_regimes": list(PAUSED_REGIMES),
        "crypto_vol_only": config.CRYPTO_VOL_ONLY,
        "nyse_universe_stats": nyse_pick_stats(signals, data),
        "overlap_spy_nyse": overlap_with_spy_holdings(signals, data),
        "correlations": correlation_analysis(signals),
        "correlations_by_regime": correlation_by_regime(signals),
        "correlations_by_volatility": correlation_by_volatility(signals),
        "cofire_counts": cofire_counts(signals),
        "cofire_counts": cofire_counts(signals),
        "cofire_spikes": cofire_and_spikes(signals),
        "diversification_effectiveness": diversification_effectiveness(signals),
        "concurrent_risk": concurrent_risk(signals, data),
        "limitations": [
            "Daily bars only; live bot uses 5m (vol gate threshold 0.02 mean std differs).",
            "Backtester.py crypto orders skip without compute_crypto_notional on BacktestExecutor.",
            "Crypto return proxy uses strongest pair spread, not mirrored leg P&L.",
            "SPY_EXIT_ON_MA_BREAK=False — simulated SPY stays 'wanted' until regime pause.",
            "No wisdom/yield gates; SpaceX crypto override not applied (snapshot=None).",
        ],
    }

    report["testable_improvements"] = testable_improvements(report)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print("=" * 60)
        print("SLEEVE OVERLAP ANALYSIS")
        print("=" * 60)
        for section, key in [
            ("Data", "data_window"),
            ("Caps", "config_caps"),
            ("NYSE picks", "nyse_universe_stats"),
            ("SPY×NYSE overlap", "overlap_spy_nyse"),
            ("Correlations", "correlations"),
            ("Regime correlations", "correlations_by_regime"),
            ("Co-fire counts", "cofire_counts"),
            ("Co-fire / spikes", "cofire_spikes"),
            ("Diversification", "diversification_effectiveness"),
            ("Concurrent risk", "concurrent_risk"),
            ("Improvements", "testable_improvements"),
            ("Limitations", "limitations"),
        ]:
            print(f"\n--- {section} ---")
            print(json.dumps(report[key], indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
