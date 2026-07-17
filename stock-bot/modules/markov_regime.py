"""Gaussian HMM regime prediction for Realistic Research v1.5.

Trains ``hmmlearn.hmm.GaussianHMM`` on a rolling daily feature window
(SPY returns, vol, VIX, volume, sentiment proxy, bubble, insider) and
emits next-day regime probabilities over 5 hidden states aligned with
RHYME_A–E. Soft-signals Dynamic VTI, conviction, and short sizing.

Falls back to the current RHYME classifier (and a lightweight 3-state
count matrix for thinking-engine prompts) when hmmlearn is missing or
fit fails. Paper-focused; live stays off unless explicitly enabled.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)

# 5 HMM states ↔ RHYME labels (ordered after emission labeling)
HMM_STATE_KEYS = ("A", "B", "C", "D", "E")
HMM_STATE_TO_RHYME = {
    "A": "RHYME_A: Euphoric_Volatility",
    "B": "RHYME_B: Panic_Volatility",
    "C": "RHYME_C: Steady_Bullish_Growth",
    "D": "RHYME_D: Range_Bound_Neutral",
    "E": "RHYME_E: Steady_Bearish_Decline",
}
RHYME_TO_HMM = {
    "RHYME_A": "A",
    "RHYME_B": "B",
    "RHYME_C": "C",
    "RHYME_D": "D",
    "RHYME_E": "E",
}

# Legacy 3-state API (thinking-engine prompt context)
STATES = ("bull", "bear", "sideways")
ROLLING_WINDOW = 20
BULL_THRESHOLD = 0.05
BEAR_THRESHOLD = -0.05
HIGH_CONFIDENCE_FLOOR = 0.55

_last_prediction: dict[str, Any] | None = None
_model_cache: dict[str, Any] = {
    "model": None,
    "state_order": list(HMM_STATE_KEYS),  # model component i → letter
    "trained_at_len": 0,
    "feature_names": (),
    "transmat_labeled": None,
    "scale": None,  # (2, n_features) mu/sigma
}


def _empty_hmm_result(
    *,
    rhyme: str | None = None,
    reason: str = "unavailable",
) -> dict[str, Any]:
    letter = _rhyme_letter(rhyme) or "D"
    probs = {k: (1.0 if k == letter else 0.0) for k in HMM_STATE_KEYS}
    # Soften to avoid overconfidence when falling back
    for k in probs:
        probs[k] = 0.70 if k == letter else 0.075
    return {
        "enabled": False,
        "ok": False,
        "reason": reason,
        "current_state": letter,
        "current_rhyme": HMM_STATE_TO_RHYME[letter],
        "predicted_next": letter,
        "predicted_rhyme": HMM_STATE_TO_RHYME[letter],
        "confidence": float(probs[letter]),
        "confidence_label": "low",
        "next_probs": probs,
        "p_bull_tomorrow": round(probs["A"] + probs["C"], 4),
        "p_bear_tomorrow": round(probs["B"] + probs["E"], 4),
        "p_sideways_tomorrow": round(probs["D"], 4),
        "vti_adj_pp": 0.0,
        "sizing_mult": 1.0,
        "short_boost": 1.0,
        "conviction": 0.5,
        "transmat": None,
        "fallback": "rhyme",
    }


def _rhyme_letter(regime: str | None) -> str | None:
    if not regime:
        return None
    reg = str(regime).upper()
    for key, letter in RHYME_TO_HMM.items():
        if key in reg:
            return letter
    if "BULL" in reg:
        return "C"
    if "BEAR" in reg:
        return "E"
    if "PANIC" in reg or "VOL" in reg:
        return "B"
    return None


def _label_return(r: float) -> str:
    if r >= BULL_THRESHOLD:
        return "bull"
    if r <= BEAR_THRESHOLD:
        return "bear"
    return "sideways"


def _empty_legacy(current_state: str = "sideways") -> dict[str, Any]:
    return {
        "current_state": current_state,
        "p_bull_tomorrow": 1.0 / 3.0,
        "p_bear_tomorrow": 1.0 / 3.0,
        "p_sideways_tomorrow": 1.0 / 3.0,
        "confidence": "low",
    }


def compute_markov_regime(prices: pd.Series) -> dict[str, Any]:
    """Legacy 3-state count matrix from 20d rolling returns (thinking prompts).

    Prefer ``get_last_hmm_prediction()`` when the Gaussian HMM is active —
    this keeps the old prompt contract intact.
    """
    pred = _last_prediction
    if pred and pred.get("ok") and pred.get("enabled"):
        return {
            "current_state": (
                "bull"
                if pred["predicted_next"] in ("A", "C")
                else "bear"
                if pred["predicted_next"] in ("B", "E")
                else "sideways"
            ),
            "p_bull_tomorrow": float(pred.get("p_bull_tomorrow", 1 / 3)),
            "p_bear_tomorrow": float(pred.get("p_bear_tomorrow", 1 / 3)),
            "p_sideways_tomorrow": float(pred.get("p_sideways_tomorrow", 1 / 3)),
            "confidence": str(pred.get("confidence_label", "low")),
            "hmm": True,
        }

    if prices is None:
        return _empty_legacy()
    try:
        series = pd.to_numeric(prices, errors="coerce").dropna()
    except Exception:
        return _empty_legacy()
    if len(series) < ROLLING_WINDOW + 2:
        return _empty_legacy()

    rolling_ret = series / series.shift(ROLLING_WINDOW) - 1.0
    rolling_ret = rolling_ret.dropna()
    if len(rolling_ret) < 2:
        return _empty_legacy()

    labels = [_label_return(float(r)) for r in rolling_ret.to_numpy()]
    state_index = {s: i for i, s in enumerate(STATES)}
    counts = np.zeros((3, 3), dtype=float)
    for i in range(len(labels) - 1):
        a = state_index[labels[i]]
        b = state_index[labels[i + 1]]
        counts[a, b] += 1.0

    probs = np.zeros_like(counts)
    for i in range(3):
        row_sum = float(counts[i].sum())
        if row_sum <= 0:
            probs[i, :] = 1.0 / 3.0
        else:
            probs[i, :] = counts[i, :] / row_sum

    current_state = labels[-1]
    row = probs[state_index[current_state]]
    p_bull = float(row[state_index["bull"]])
    p_bear = float(row[state_index["bear"]])
    p_sideways = float(row[state_index["sideways"]])
    dominant = max(p_bull, p_bear, p_sideways)
    confidence = "high" if dominant > HIGH_CONFIDENCE_FLOOR else "low"

    return {
        "current_state": current_state,
        "p_bull_tomorrow": round(p_bull, 6),
        "p_bear_tomorrow": round(p_bear, 6),
        "p_sideways_tomorrow": round(p_sideways, 6),
        "confidence": confidence,
    }


markov_regime = compute_markov_regime


# ---------------------------------------------------------------------------
# Feature engineering + GaussianHMM
# ---------------------------------------------------------------------------


def _spy_series(data) -> pd.Series | None:
    if data is None or getattr(data, "empty", True):
        return None
    for col in (config.SPY_BOT_SYMBOL, "SPY", config.VTI_CORE_SYMBOL, "VTI"):
        if col in data.columns:
            s = pd.to_numeric(data[col], errors="coerce").dropna()
            if len(s) >= 40:
                return s
    return None


def _vix_series(data) -> pd.Series | None:
    if data is None:
        return None
    for col in ("VIX", "^VIX", "VIXY"):
        if col in getattr(data, "columns", []):
            s = pd.to_numeric(data[col], errors="coerce")
            if s.notna().sum() >= 20:
                return s
    return None


def _volume_proxy(data, spy: pd.Series) -> pd.Series:
    """Use SPY dollar-change magnitude as a volume stand-in when vol columns absent."""
    rets = spy.pct_change().abs()
    return rets.rolling(5, min_periods=3).mean().fillna(rets.mean())


def build_hmm_features(
    data,
    *,
    bubble_score_100: float | None = None,
    insider_intensity: float | None = None,
    sentiment: float | None = None,
) -> tuple[pd.DataFrame, list[str]] | tuple[None, list[str]]:
    """Build a daily feature matrix aligned to SPY closes."""
    spy = _spy_series(data)
    if spy is None:
        return None, []

    ret_1 = spy.pct_change()
    ret_5 = spy.pct_change(5)
    vol_20 = ret_1.rolling(20, min_periods=10).std()
    mom_20 = spy / spy.shift(20) - 1.0
    vol_z = _volume_proxy(data, spy)
    vol_z = (vol_z - vol_z.rolling(60, min_periods=20).mean()) / (
        vol_z.rolling(60, min_periods=20).std() + 1e-9
    )

    # Sentiment: caller override or price-based (-1..1 from 10d momentum)
    if sentiment is None:
        sent = mom_20.clip(-0.15, 0.15) / 0.15
    else:
        sent = pd.Series(float(sentiment), index=spy.index)

    vix = _vix_series(data)
    if vix is not None:
        vix = vix.reindex(spy.index).ffill()
        vix_lvl = (vix / 20.0).clip(0.3, 3.0)
        vix_chg = vix.pct_change().fillna(0.0).clip(-0.3, 0.3)
    else:
        # Vol-of-vol proxy when VIX missing
        vix_lvl = (vol_20 / 0.012).clip(0.3, 3.0)
        vix_chg = vol_20.pct_change().fillna(0.0).clip(-0.3, 0.3)

    bubble = float(bubble_score_100 if bubble_score_100 is not None else 50.0) / 100.0
    insider = float(insider_intensity if insider_intensity is not None else 0.0)
    bubble_s = pd.Series(bubble, index=spy.index)
    insider_s = pd.Series(insider, index=spy.index)

    frame = pd.DataFrame(
        {
            "ret_1": ret_1,
            "ret_5": ret_5,
            "vol_20": vol_20,
            "mom_20": mom_20,
            "vol_z": vol_z,
            "vix_lvl": vix_lvl,
            "vix_chg": vix_chg,
            "sentiment": sent,
            "bubble": bubble_s,
            "insider": insider_s,
        },
        index=spy.index,
    ).replace([np.inf, -np.inf], np.nan).dropna()
    names = list(frame.columns)
    return frame, names


def _label_states_by_emissions(
    means: np.ndarray, feature_names: list[str]
) -> list[str]:
    """Map unordered HMM components → A–E using return/vol emission means."""
    n = means.shape[0]
    ret_idx = feature_names.index("ret_1") if "ret_1" in feature_names else 0
    vol_idx = feature_names.index("vol_20") if "vol_20" in feature_names else min(2, means.shape[1] - 1)
    rows = []
    for i in range(n):
        rows.append((float(means[i, ret_idx]), float(means[i, vol_idx]), i))
    # Sort by return ascending → assign provisional bear→bull
    by_ret = sorted(rows, key=lambda t: t[0])
    # Among components, pick highest-vol for B (panic) and A (euphoric)
    vol_rank = sorted(rows, key=lambda t: t[1], reverse=True)
    high_vol = {vol_rank[0][2], vol_rank[1][2]} if n >= 2 else {vol_rank[0][2]}

    order = [""] * n
    # Lowest return + high vol → B; lowest return + low vol → E
    low_ret = [r[2] for r in by_ret[:2]] if n >= 2 else [by_ret[0][2]]
    high_ret = [r[2] for r in by_ret[-2:]] if n >= 2 else [by_ret[-1][2]]
    mid = [r[2] for r in by_ret if r[2] not in low_ret and r[2] not in high_ret]

    used: set[int] = set()
    for idx in low_ret:
        if idx in high_vol and "B" not in order:
            order[idx] = "B"
            used.add(idx)
            break
    for idx in low_ret:
        if idx not in used:
            order[idx] = "E"
            used.add(idx)
            break
    for idx in high_ret:
        if idx in high_vol and "A" not in order:
            order[idx] = "A"
            used.add(idx)
            break
    for idx in high_ret:
        if idx not in used:
            order[idx] = "C"
            used.add(idx)
            break
    for idx in mid:
        if idx not in used:
            order[idx] = "D"
            used.add(idx)
    # Fill any gaps
    remaining = [k for k in HMM_STATE_KEYS if k not in order]
    for i, letter in enumerate(order):
        if not letter and remaining:
            order[i] = remaining.pop(0)
    return order


def _fit_gaussian_hmm(
    X: np.ndarray, n_states: int, feature_names: list[str]
) -> tuple[Any, list[str], np.ndarray | None] | tuple[None, list[str], None]:
    try:
        from hmmlearn.hmm import GaussianHMM
    except ImportError as exc:
        logger.debug("hmmlearn unavailable: %s", exc)
        return None, [], None

    n_states = max(2, min(int(n_states), max(2, len(X) // 20)))
    # Z-score features for stabler EM (store mu/sigma for later transform)
    mu = X.mean(axis=0)
    sigma = X.std(axis=0)
    sigma = np.where(sigma < 1e-8, 1.0, sigma)
    Xz = (X - mu) / sigma
    try:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model = GaussianHMM(
                n_components=n_states,
                covariance_type="diag",
                n_iter=150,
                tol=1e-2,
                random_state=42,
                init_params="stmc",
                params="stmc",
                min_covar=1e-3,
            )
            model.fit(Xz)
        # Repair empty transition rows (common with sparse regimes)
        for i in range(model.n_components):
            row = model.transmat_[i]
            s = float(row.sum())
            if s <= 1e-12:
                model.transmat_[i] = np.full(model.n_components, 1.0 / model.n_components)
            else:
                model.transmat_[i] = row / s
        # Ensure startprob is valid
        sp = model.startprob_
        if float(sp.sum()) <= 1e-12:
            model.startprob_ = np.full(model.n_components, 1.0 / model.n_components)
        else:
            model.startprob_ = sp / sp.sum()
        order = _label_states_by_emissions(model.means_, feature_names)
        if len(order) < len(model.means_):
            for k in HMM_STATE_KEYS:
                if k not in order:
                    order.append(k)
            order = order[: len(model.means_)]
        return model, order, np.vstack([mu, sigma])
    except Exception as exc:
        logger.warning("GaussianHMM fit failed: %s", exc)
        return None, [], None


def _probs_from_model(
    model, state_order: list[str], X: np.ndarray, scale: np.ndarray | None
) -> tuple[str, str, dict[str, float], float, np.ndarray]:
    """Return current letter, predicted letter, next probs, confidence, labeled transmat."""
    X_use = X
    if scale is not None and scale.shape[0] >= 2:
        mu, sigma = scale[0], scale[1]
        X_use = (X - mu) / np.where(sigma < 1e-8, 1.0, sigma)
    post = model.predict_proba(X_use)[-1]
    next_raw = post @ model.transmat_
    probs = {k: 0.0 for k in HMM_STATE_KEYS}
    for i, letter in enumerate(state_order):
        if letter in probs:
            probs[letter] += float(next_raw[i])
    total = sum(probs.values()) or 1.0
    for k in probs:
        probs[k] = round(probs[k] / total, 6)

    cur_i = int(np.argmax(post))
    current = state_order[cur_i] if cur_i < len(state_order) else "D"
    predicted = max(probs, key=probs.get)
    confidence = float(probs[predicted])

    n = len(state_order)
    labeled = np.zeros((5, 5), dtype=float)
    idx = {letter: j for j, letter in enumerate(HMM_STATE_KEYS)}
    for i, li in enumerate(state_order):
        for j, lj in enumerate(state_order):
            if li in idx and lj in idx and i < n and j < n:
                labeled[idx[li], idx[lj]] += float(model.transmat_[i, j])
    for i in range(5):
        s = labeled[i].sum()
        if s > 0:
            labeled[i] /= s
        else:
            labeled[i, i] = 1.0
    return current, predicted, probs, confidence, labeled


def _signal_from_probs(probs: dict[str, float]) -> dict[str, float]:
    """Map next-day probs → VTI adj (pp), sizing mult, short boost, conviction."""
    p_bull = probs.get("A", 0) + probs.get("C", 0)
    p_bear = probs.get("B", 0) + probs.get("E", 0)
    p_panic = probs.get("B", 0)
    p_range = probs.get("D", 0)

    # Positive VTI adj = more defensive core
    vti_adj = (
        10.0 * p_bear
        + 6.0 * p_panic
        + 2.0 * p_range
        - 8.0 * p_bull
        - 4.0 * probs.get("A", 0)
    )
    # Sizing: de-risk when bear/panic predicted
    sizing = 1.0 + 0.15 * p_bull - 0.25 * p_bear - 0.20 * p_panic
    sizing = float(np.clip(sizing, 0.55, 1.25))
    # Short boost when bear/panic predicted
    short_boost = 1.0 + 0.35 * p_bear + 0.45 * p_panic - 0.20 * p_bull
    short_boost = float(np.clip(short_boost, 0.70, 1.55))
    # Conviction 0–1 (bullish = high for long sleeves)
    conviction = float(np.clip(0.35 + 0.45 * p_bull - 0.35 * p_bear - 0.25 * p_panic, 0.05, 0.95))
    return {
        "vti_adj_pp": round(float(vti_adj), 2),
        "sizing_mult": round(sizing, 4),
        "short_boost": round(short_boost, 4),
        "conviction": round(conviction, 4),
    }


def _insider_intensity(insider_state: dict | None) -> float:
    if not insider_state:
        return 0.0
    try:
        buys = int(insider_state.get("cluster_buys") or insider_state.get("buy_boosts") or 0)
        sells = int(
            insider_state.get("exec_sells")
            or insider_state.get("short_candidates")
            or 0
        )
        return float(np.clip((buys - sells) / 5.0, -1.0, 1.0))
    except Exception:
        return 0.0


def update_markov_hmm(
    data,
    *,
    regime: str | None = None,
    bubble_score_100: float | None = None,
    insider_state: dict | None = None,
    sentiment: float | None = None,
    force_retrain: bool = False,
) -> dict[str, Any]:
    """Train/predict HMM; store result for consumers. Safe no-op when disabled."""
    global _last_prediction

    if not config.effective_markov_hmm_enabled():
        result = _empty_hmm_result(rhyme=regime, reason="disabled")
        _last_prediction = result
        return result

    n_states = int(getattr(config, "HMM_N_STATES", 5) or 5)
    train_window = int(getattr(config, "HMM_TRAIN_WINDOW_DAYS", 252) or 252)
    retrain_every = int(getattr(config, "HMM_RETRAIN_EVERY_BARS", 5) or 5)

    features, names = build_hmm_features(
        data,
        bubble_score_100=bubble_score_100,
        insider_intensity=_insider_intensity(insider_state),
        sentiment=sentiment,
    )
    if features is None or len(features) < max(60, n_states * 12):
        result = _empty_hmm_result(rhyme=regime, reason="insufficient_data")
        _last_prediction = result
        return result

    window = features.tail(train_window)
    X = window.to_numpy(dtype=float)
    need_fit = (
        force_retrain
        or _model_cache["model"] is None
        or abs(len(window) - int(_model_cache["trained_at_len"])) >= retrain_every
    )
    if need_fit:
        model, order, scale = _fit_gaussian_hmm(X, n_states, names)
        if model is None:
            result = _empty_hmm_result(rhyme=regime, reason="fit_failed")
            _last_prediction = result
            return result
        _model_cache["model"] = model
        _model_cache["state_order"] = order
        _model_cache["trained_at_len"] = len(window)
        _model_cache["feature_names"] = tuple(names)
        _model_cache["scale"] = scale

    model = _model_cache["model"]
    order = _model_cache["state_order"]
    try:
        current, predicted, probs, confidence, labeled = _probs_from_model(
            model, order, X, _model_cache.get("scale")
        )
    except Exception as exc:
        logger.debug("HMM predict failed: %s", exc)
        result = _empty_hmm_result(rhyme=regime, reason="predict_failed")
        _last_prediction = result
        return result

    _model_cache["transmat_labeled"] = labeled
    signals = _signal_from_probs(probs)
    conf_label = "high" if confidence >= 0.45 else "low"

    result = {
        "enabled": True,
        "ok": True,
        "reason": "ok",
        "current_state": current,
        "current_rhyme": HMM_STATE_TO_RHYME.get(current, current),
        "predicted_next": predicted,
        "predicted_rhyme": HMM_STATE_TO_RHYME.get(predicted, predicted),
        "confidence": round(confidence, 4),
        "confidence_label": conf_label,
        "next_probs": probs,
        "p_bull_tomorrow": round(probs["A"] + probs["C"], 4),
        "p_bear_tomorrow": round(probs["B"] + probs["E"], 4),
        "p_sideways_tomorrow": round(probs["D"], 4),
        "transmat": labeled.round(3).tolist(),
        "fallback": None,
        "n_states": len(order),
        "train_bars": len(window),
        **signals,
    }
    _last_prediction = result
    return result


def get_last_hmm_prediction() -> dict[str, Any] | None:
    return dict(_last_prediction) if _last_prediction else None


def reset_markov_hmm_state() -> None:
    """Clear cached model + prediction (compare runs / tests)."""
    global _last_prediction
    _last_prediction = None
    _model_cache["model"] = None
    _model_cache["trained_at_len"] = 0
    _model_cache["transmat_labeled"] = None
    _model_cache["scale"] = None


def hmm_vti_adjustment_pp() -> float:
    if not config.effective_markov_hmm_enabled():
        return 0.0
    pred = _last_prediction
    if not pred or not pred.get("ok"):
        return 0.0
    return float(pred.get("vti_adj_pp") or 0.0)


def hmm_sizing_multiplier() -> float:
    if not config.effective_markov_hmm_enabled():
        return 1.0
    pred = _last_prediction
    if not pred or not pred.get("ok"):
        return 1.0
    return float(pred.get("sizing_mult") or 1.0)


def hmm_short_boost() -> float:
    if not config.effective_markov_hmm_enabled():
        return 1.0
    pred = _last_prediction
    if not pred or not pred.get("ok"):
        return 1.0
    return float(pred.get("short_boost") or 1.0)


def hmm_conviction_component() -> float | None:
    if not config.effective_markov_hmm_enabled():
        return None
    pred = _last_prediction
    if not pred or not pred.get("ok"):
        return None
    return float(pred.get("conviction"))


def format_markov_hmm_banner() -> str | None:
    if not config.effective_markov_hmm_enabled():
        return None
    n = int(getattr(config, "HMM_N_STATES", 5) or 5)
    pred = _last_prediction
    if pred and pred.get("ok"):
        return (
            f"Markov HMM: ON ({n} states, next={pred.get('predicted_next')} "
            f"p={pred.get('confidence', 0):.0%} | "
            f"bull={pred.get('p_bull_tomorrow', 0):.0%} "
            f"bear={pred.get('p_bear_tomorrow', 0):.0%})"
        )
    return f"Markov HMM: ON ({n} states, next-regime prob)"


def format_weekly_hmm_section() -> list[str]:
    """Markdown lines for weekly report — transition matrix + latest prediction."""
    lines = ["## Markov HMM regime", ""]
    if not config.effective_markov_hmm_enabled():
        lines.append("- Markov HMM: OFF")
        lines.append("")
        return lines

    pred = get_last_hmm_prediction()
    if not pred or not pred.get("ok"):
        reason = (pred or {}).get("reason", "no prediction")
        lines.append(f"- Markov HMM: ON but idle ({reason}); RHYME fallback active")
        lines.append("")
        return lines

    lines.append(
        f"- Current decoded: **{pred.get('current_state')}** "
        f"({pred.get('current_rhyme', '')})"
    )
    lines.append(
        f"- Predicted next: **{pred.get('predicted_next')}** "
        f"(confidence {pred.get('confidence', 0):.0%} / {pred.get('confidence_label')})"
    )
    probs = pred.get("next_probs") or {}
    if probs:
        parts = ", ".join(f"{k}={float(probs.get(k, 0)):.0%}" for k in HMM_STATE_KEYS)
        lines.append(f"- Next-day probs: {parts}")
    lines.append(
        f"- Soft signals: VTI {pred.get('vti_adj_pp', 0):+.1f}pp | "
        f"sizing x{pred.get('sizing_mult', 1):.2f} | "
        f"short x{pred.get('short_boost', 1):.2f}"
    )
    tm = pred.get("transmat") or _model_cache.get("transmat_labeled")
    if tm is not None:
        arr = np.asarray(tm, dtype=float)
        lines.append("")
        lines.append("**Transition matrix** (rows=from A–E, cols=to A–E):")
        lines.append("")
        header = "| from \\ to | " + " | ".join(HMM_STATE_KEYS) + " |"
        sep = "|---|" + "|".join(["---"] * 5) + "|"
        lines.append(header)
        lines.append(sep)
        for i, letter in enumerate(HMM_STATE_KEYS):
            row = " | ".join(f"{arr[i, j]:.2f}" for j in range(min(5, arr.shape[1])))
            lines.append(f"| {letter} | {row} |")
    lines.append("")
    return lines


def heartbeat_hmm_payload() -> dict[str, Any] | None:
    """Compact dict for heartbeat / dashboard."""
    if not config.effective_markov_hmm_enabled():
        return None
    pred = get_last_hmm_prediction()
    if not pred:
        return {"enabled": True, "ok": False, "reason": "pending"}
    return {
        "enabled": True,
        "ok": bool(pred.get("ok")),
        "current": pred.get("current_state"),
        "predicted": pred.get("predicted_next"),
        "confidence": pred.get("confidence"),
        "confidence_label": pred.get("confidence_label"),
        "next_probs": pred.get("next_probs"),
        "vti_adj_pp": pred.get("vti_adj_pp"),
        "sizing_mult": pred.get("sizing_mult"),
        "short_boost": pred.get("short_boost"),
        "reason": pred.get("reason"),
    }
