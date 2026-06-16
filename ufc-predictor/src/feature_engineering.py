"""Leakage-safe differential features for UFC fight prediction."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import config
from src.data_loader import _fighters_same_person, clean_fighter_name, ensure_data_dirs
from src.greco_stats import apply_greco_to_features, fill_history_from_greco, greco_pre_fight_rolling

logger = logging.getLogger(__name__)

KEY_DIFF_COVERAGE_COLS = [
    "wc_age_advantage_diff",
    "similar_opp_win_rate_diff",
    "short_notice_perf_diff",
    "long_layoff_perf_diff",
    "height_diff",
    "reach_diff",
    "striking_acc_diff",
    "sig_strikes_per_min_diff",
    "td_acc_diff",
    "td_defense_diff",
    "elo_diff",
    "win_rate_diff",
]

# Opponent similarity tolerances for "vs similar opponent" rolling win rates.
SIMILAR_OPP_TOLERANCES: dict[str, float] = {
    "sig_strike_acc": 0.12,
    "td_defense": 0.15,
    "reach_in": 3.0,
    "age": 4.0,
}
SIMILAR_OPP_PROFILE_FIELDS = tuple(SIMILAR_OPP_TOLERANCES.keys())

SHORT_NOTICE_DAYS = 10
LONG_LAYOFF_DAYS = 180

# Age disadvantage scales up in heavier divisions (younger = less penalty).
WC_AGE_SENSITIVITY: dict[str, float] = {
    "flyweight": 0.60,
    "bantamweight": 0.70,
    "featherweight": 0.75,
    "lightweight": 0.85,
    "welterweight": 0.95,
    "middleweight": 1.00,
    "light heavyweight": 1.10,
    "heavyweight": 1.25,
    "open weight": 1.20,
    "catch weight": 0.90,
    "women": 0.90,
}

# Stats expected by build_matchup_features (per fighter, pre-fight).
FIGHTER_STAT_FIELDS = [
    "age",
    "height_in",
    "reach_in",
    "stance_orthodox",
    "stance_southpaw",
    "stance_switch",
    "win_rate",
    "sig_strike_acc",
    "td_acc",
    "sub_avg",
    "ko_rate",
    "last5_win_rate",
    "momentum",
    "sig_strikes_per_min",
    "td_defense",
    "control_time_per_min",
    "elo",
    "days_since_last_fight",
    "fight_count",
    "striker_score",
    "grappler_score",
    "similar_opp_win_rate",
    "short_notice_flag",
    "long_layoff_flag",
    "short_notice_win_rate",
    "long_layoff_win_rate",
]

# Differential feature names produced for modeling.
DIFF_FEATURE_FIELDS = [
    "age_diff",
    "height_diff",
    "reach_diff",
    "stance_matchup",
    "southpaw_advantage",
    "striker_score_diff",
    "grappler_score_diff",
    "striker_vs_grappler",
    "style_clash",
    "win_rate_diff",
    "striking_acc_diff",
    "takedown_acc_diff",
    "sub_avg_diff",
    "ko_rate_diff",
    "last5_winrate_diff",
    "momentum_diff",
    "sig_strikes_per_min_diff",
    "td_defense_diff",
    "control_time_diff",
    "elo_diff",
    "days_since_last_fight_diff",
    "experience_diff",
    "similar_opp_win_rate_diff",
    "wc_age_advantage_diff",
    "short_notice_flag_diff",
    "long_layoff_flag_diff",
    "short_notice_perf_diff",
    "long_layoff_perf_diff",
]

# --- Interaction candidates (products of base diffs; subset selected at train time) ---
@dataclass(frozen=True)
class InteractionSpec:
    """Named product of two differential features."""

    name: str
    factor_a: str
    factor_b: str
    label: str


INTERACTION_SPECS: tuple[InteractionSpec, ...] = (
    InteractionSpec("ix_age_x_reach", "age_diff", "reach_diff", "Age x Reach"),
    InteractionSpec(
        "ix_strike_acc_x_td_def",
        "striking_acc_diff",
        "td_defense_diff",
        "Striking Accuracy x Takedown Defense",
    ),
    InteractionSpec(
        "ix_last5_x_layoff",
        "last5_winrate_diff",
        "days_since_last_fight_diff",
        "Recent Form x Layoff Days",
    ),
    InteractionSpec(
        "ix_wc_age_x_striker",
        "wc_age_advantage_diff",
        "striker_score_diff",
        "WC Age Advantage x Striker Score",
    ),
    InteractionSpec(
        "ix_striker_x_grappler",
        "striker_score_diff",
        "grappler_score_diff",
        "Striker x Grappler Style",
    ),
    InteractionSpec(
        "ix_southpaw_x_layoff",
        "southpaw_advantage",
        "long_layoff_flag_diff",
        "Southpaw Advantage x Long Layoff",
    ),
    InteractionSpec(
        "ix_stance_x_clash",
        "stance_matchup",
        "style_clash",
        "Stance Matchup x Style Clash",
    ),
    InteractionSpec(
        "ix_short_notice_x_perf",
        "short_notice_flag_diff",
        "short_notice_perf_diff",
        "Short Notice x Short-Notice Record",
    ),
    InteractionSpec(
        "ix_elo_x_momentum",
        "elo_diff",
        "momentum_diff",
        "Elo x Momentum",
    ),
    InteractionSpec(
        "ix_reach_x_striker",
        "reach_diff",
        "striker_score_diff",
        "Reach x Striker Score",
    ),
    InteractionSpec(
        "ix_similar_opp_x_td_def",
        "similar_opp_win_rate_diff",
        "td_defense_diff",
        "Similar Opponent Record x TD Defense",
    ),
    InteractionSpec(
        "ix_wc_age_x_layoff",
        "wc_age_advantage_diff",
        "long_layoff_flag_diff",
        "WC Age x Long Layoff",
    ),
    InteractionSpec(
        "ix_grappler_x_td_acc",
        "grappler_score_diff",
        "takedown_acc_diff",
        "Grappler Score x Takedown Accuracy",
    ),
    InteractionSpec(
        "ix_striker_grappler_clash",
        "striker_vs_grappler",
        "style_clash",
        "Striker-vs-Grappler x Style Clash",
    ),
    InteractionSpec(
        "ix_experience_x_form",
        "experience_diff",
        "last5_winrate_diff",
        "Experience x Recent Form",
    ),
    InteractionSpec(
        "ix_form_x_long_layoff_perf",
        "last5_winrate_diff",
        "long_layoff_perf_diff",
        "Recent Form x Long-Layoff Record",
    ),
    InteractionSpec(
        "ix_height_x_reach",
        "height_diff",
        "reach_diff",
        "Height x Reach",
    ),
    InteractionSpec(
        "ix_ko_rate_x_striker",
        "ko_rate_diff",
        "striker_score_diff",
        "KO Rate x Striker Score",
    ),
    InteractionSpec(
        "ix_control_x_grappler",
        "control_time_diff",
        "grappler_score_diff",
        "Control Time x Grappler Score",
    ),
    InteractionSpec(
        "ix_winrate_x_elo",
        "win_rate_diff",
        "elo_diff",
        "Win Rate x Elo",
    ),
)

MOMENTUM_WEIGHTS = np.array([0.35, 0.25, 0.20, 0.12, 0.08])
ELO_START = 1500.0
ELO_K = 32.0

_STANCE_ALIASES = {
    "orthodox": "stance_orthodox",
    "southpaw": "stance_southpaw",
    "switch": "stance_switch",
    "open": "stance_switch",
    "sideways": "stance_switch",
}


def _parse_fight_minutes(round_val: Any, time_val: Any, default: float = 7.5) -> float:
    """Estimate fight length in minutes from round and mm:ss clock."""
    try:
        rnd = int(round_val)
    except (TypeError, ValueError):
        rnd = 1
    minutes = max(0, rnd - 1) * 5.0
    if time_val is None or (isinstance(time_val, float) and np.isnan(time_val)):
        return minutes + 2.5 if minutes > 0 else default
    text = str(time_val).strip()
    if ":" in text:
        parts = text.split(":")
        if len(parts) == 2:
            try:
                minutes += int(parts[0]) + int(parts[1]) / 60.0
                return max(minutes, 0.5)
            except ValueError:
                pass
    return minutes + 2.5 if minutes > 0 else default


def _method_flags(method: Any) -> tuple[int, int, int]:
    text = str(method or "").upper()
    is_ko = int("KO" in text or "TKO" in text)
    is_sub = int("SUB" in text)
    is_dec = int("DEC" in text or "DECISION" in text)
    return is_ko, is_sub, is_dec


def _coerce_numeric(series: pd.Series) -> pd.Series:
    if series.dtype.kind in "biufc":
        return pd.to_numeric(series, errors="coerce")
    extracted = series.astype(str).str.extract(r"(\d+(?:\.\d+)?)", expand=False)
    return pd.to_numeric(extracted, errors="coerce")


def _clip01(value: float) -> float:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return 0.0
    return float(np.clip(value, 0.0, 1.0))


def compute_style_scores(
    *,
    sig_strikes_per_min: float,
    sig_strike_acc: float,
    ko_rate: float,
    td_acc: float,
    sub_avg: float,
    td_defense: float,
) -> tuple[float, float]:
    """
    Classify fighter tendency: striker vs grappler (0–1 composites).

    Uses pre-fight rolling stats only.
    """
    sspm = _clip01(sig_strikes_per_min / 6.0)
    sacc = _clip01(sig_strike_acc)
    ko = _clip01(ko_rate)
    td = _clip01(td_acc)
    sub = _clip01(sub_avg / 2.0)
    tdef = _clip01(td_defense)
    striker = 0.45 * sspm + 0.35 * sacc + 0.20 * ko
    grappler = 0.40 * td + 0.40 * sub + 0.20 * tdef
    return striker, grappler


def weight_class_age_sensitivity(weight_class: Any) -> float:
    """Return age-impact multiplier for a division (heavier = age matters more)."""
    if weight_class is None or (isinstance(weight_class, float) and np.isnan(weight_class)):
        return 1.0
    text = str(weight_class).strip().lower()
    if not text:
        return 1.0
    if "women" in text or "female" in text:
        return WC_AGE_SENSITIVITY["women"]
    for key, sens in WC_AGE_SENSITIVITY.items():
        if key in text:
            return sens
    return 1.0


def _opponent_profiles_similar(
    target: dict[str, float],
    candidate: dict[str, float],
    *,
    tolerances: dict[str, float] | None = None,
) -> bool:
    """True when opponent profiles match within per-dimension tolerances."""
    tol = tolerances or SIMILAR_OPP_TOLERANCES
    matched = 0
    required = 0
    for field, band in tol.items():
        a = target.get(field)
        b = candidate.get(field)
        if a is None or b is None or (isinstance(a, float) and np.isnan(a)) or (
            isinstance(b, float) and np.isnan(b)
        ):
            continue
        required += 1
        if abs(float(a) - float(b)) <= band:
            matched += 1
    if required < 2:
        return False
    return matched >= max(2, required - 1)


def _shifted_conditional_win_rate(
    won: pd.Series,
    condition: pd.Series,
    *,
    window: int = 12,
) -> pd.Series:
    """Rolling win rate using only prior fights where ``condition`` was true."""
    masked = won.where(condition.astype(bool))
    return masked.shift(1).rolling(window, min_periods=1).mean()


def _attach_opponent_prefight_profiles(history: pd.DataFrame) -> pd.DataFrame:
    """Join each row with the opponent's pre-fight rolling profile on the same card."""
    profile_cols = [c for c in SIMILAR_OPP_PROFILE_FIELDS if c in history.columns]
    if not profile_cols:
        return history
    opp = history[[config.FIGHT_ID_COLUMN, "fighter", *profile_cols]].copy()
    rename = {"fighter": "opponent"}
    rename.update({c: f"opp_profile_{c}" for c in profile_cols})
    opp = opp.rename(columns=rename)
    return history.merge(opp, on=[config.FIGHT_ID_COLUMN, "opponent"], how="left")


def _similar_opponent_mask(
    target: np.ndarray,
    prior: np.ndarray,
    bands: np.ndarray,
) -> np.ndarray:
    """Vectorized similarity: target (d,), prior (n, d), bands (d,) -> bool (n,)."""
    valid = ~np.isnan(target) & ~np.isnan(prior)
    required = valid.sum(axis=1)
    diffs = np.abs(prior - target)
    within = valid & (diffs <= bands)
    matched = within.sum(axis=1)
    return (required >= 2) & (matched >= np.maximum(2, required - 1))


def _compute_similar_opponent_win_rates(history: pd.DataFrame) -> pd.DataFrame:
    """
    Per-appearance win rate vs historically similar opponents (leakage-safe).

    Uses only fights before the current bout for the same fighter.
    """
    work = _attach_opponent_prefight_profiles(history)
    available_fields = [
        f for f in SIMILAR_OPP_PROFILE_FIELDS if f"opp_profile_{f}" in work.columns
    ]
    if len(available_fields) < 2:
        work["similar_opp_win_rate"] = np.nan
        return work

    profile_keys = [f"opp_profile_{f}" for f in available_fields]
    bands = np.array([SIMILAR_OPP_TOLERANCES[f] for f in available_fields])
    max_prior = 50

    def _fighter_similar_wr(grp: pd.DataFrame) -> pd.Series:
        grp = grp.sort_values(config.DATE_COLUMN)
        profiles = grp[profile_keys].to_numpy(dtype=float)
        won = grp["won"].to_numpy(dtype=float)
        n = len(grp)
        rates = np.full(n, np.nan)
        for i in range(1, n):
            target = profiles[i]
            if np.all(np.isnan(target)):
                continue
            start = max(0, i - max_prior)
            prior_profiles = profiles[start:i]
            prior_won = won[start:i]
            mask = _similar_opponent_mask(target, prior_profiles, bands)
            if mask.any():
                rates[i] = float(prior_won[mask].mean())
        return pd.Series(rates, index=grp.index)

    logger.info("Computing similar-opponent win rates (leakage-safe)…")
    work["similar_opp_win_rate"] = work.groupby("fighter", group_keys=False).apply(
        _fighter_similar_wr
    )
    return work


def _layoff_context_flags(days_since_last: float) -> tuple[float, float]:
    """Return (short_notice_flag, long_layoff_flag) for a layoff in days."""
    if days_since_last is None or (isinstance(days_since_last, float) and np.isnan(days_since_last)):
        return 0.0, 0.0
    days = float(days_since_last)
    short_flag = 1.0 if days < SHORT_NOTICE_DAYS else 0.0
    long_flag = 1.0 if days > LONG_LAYOFF_DAYS else 0.0
    return short_flag, long_flag


def _stance_encoding(stance: Any) -> dict[str, float]:
    out = {"stance_orthodox": 0.0, "stance_southpaw": 0.0, "stance_switch": 0.0}
    if stance is None or (isinstance(stance, float) and np.isnan(stance)):
        return out
    key = str(stance).strip().lower()
    field = _STANCE_ALIASES.get(key)
    if field:
        out[field] = 1.0
    return out


def ensure_pipeline_columns(fights: pd.DataFrame) -> pd.DataFrame:
    """Map canonical fights.csv names onto feature-engineering aliases."""
    work = fights.copy()
    aliases = {
        "fighter1": "fighter_1",
        "fighter2": "fighter_2",
        "date": config.DATE_COLUMN,
        "event": "event_name",
    }
    for src, dst in aliases.items():
        if src in work.columns and dst not in work.columns:
            work[dst] = work[src]
    if config.DATE_COLUMN in work.columns:
        work[config.DATE_COLUMN] = pd.to_datetime(work[config.DATE_COLUMN], errors="coerce")
    return work


def _fighter_sort_key(name: Any) -> str:
    return clean_fighter_name(name).lower()


def _collect_f1_f2_swap_pairs(columns: pd.Index) -> list[tuple[str, str]]:
    """Column pairs to swap when canonicalizing fighter1/fighter2 slots."""
    pairs: list[tuple[str, str]] = [
        ("fighter_1", "fighter_2"),
        ("fighter1", "fighter2"),
        ("f1_odds", "f2_odds"),
    ]
    seen = {tuple(sorted(p)) for p in pairs}
    col_list = list(columns)
    for col in col_list:
        partner = None
        if col.endswith("_f1"):
            partner = col[:-3] + "_f2"
        elif col.endswith("_f2"):
            partner = col[:-3] + "_f1"
        elif col.startswith("fighter1_"):
            partner = "fighter2_" + col[len("fighter1_") :]
        elif col.startswith("fighter2_"):
            partner = "fighter1_" + col[len("fighter2_") :]
        if partner and partner in col_list:
            key = tuple(sorted((col, partner)))
            if key not in seen:
                pairs.append((col, partner))
                seen.add(key)
    return pairs


def _canonicalize_fighter_slots(fights: pd.DataFrame) -> pd.DataFrame:
    """
    Put fighters in stable alphabetical order so fighter_1 is not correlated with winner.

    Many historical CSVs (jansen/HuggingFace) list fighter1 as the favourite/winner ~98%
    of the time, which causes the model to always predict f1_win=1.
    """
    work = ensure_pipeline_columns(fights)
    if "fighter_1" not in work.columns or "fighter_2" not in work.columns:
        return work

    swap_pairs = _collect_f1_f2_swap_pairs(work.columns)
    swap_mask = work.apply(
        lambda r: _fighter_sort_key(r["fighter_1"]) > _fighter_sort_key(r["fighter_2"]),
        axis=1,
    )
    if not swap_mask.any():
        return work

    for idx in work.index[swap_mask]:
        for a, b in swap_pairs:
            if a in work.columns and b in work.columns:
                av, bv = work.at[idx, a], work.at[idx, b]
                work.at[idx, a], work.at[idx, b] = bv, av
    return work


def _winner_is_fighter(winner: Any, fighter: Any) -> bool:
    """Match winner to a fighter slot; stricter than generic _fighters_same_person."""
    w = clean_fighter_name(winner)
    f = clean_fighter_name(fighter)
    if not w or not f:
        return False
    wl, fl = w.lower(), f.lower()
    if wl == fl:
        return True
    w_parts = set(wl.split())
    f_parts = set(fl.split())
    if len(w_parts.intersection(f_parts)) >= 2:
        return True
    w_tokens = wl.split()
    f_tokens = fl.split()
    if len(w_tokens) >= 2 and len(f_tokens) >= 2 and w_tokens[-1] == f_tokens[-1]:
        if len(w_tokens[-1]) > 3 and w_tokens[0][0] == f_tokens[0][0]:
            return True
    return _fighters_same_person(w, f) if len(w_parts.intersection(f_parts)) >= 2 else False


def _encode_f1_win_target(df: pd.DataFrame) -> pd.Series:
    """
    Target = 1 when canonical fighter_1 won.

    Uses fuzzy name match (data_loader rules), not raw string equality.
    """

    def _row_target(row: pd.Series) -> float:
        winner = row.get("winner", "")
        f1 = row.get("fighter_1", row.get("fighter1", ""))
        f2 = row.get("fighter_2", row.get("fighter2", ""))
        w = clean_fighter_name(winner)
        if not w or w.lower() in {"draw", "no contest", "nc", "d"}:
            return np.nan
        if _winner_is_fighter(w, f1):
            return 1.0
        if _winner_is_fighter(w, f2):
            return 0.0
        return np.nan

    return df.apply(_row_target, axis=1).astype(float)


def assert_target_encoding(
    features: pd.DataFrame,
    *,
    min_rows_for_balance: int = 50,
) -> float:
    """Assert f1_win is balanced and consistent with winner vs fighter_1."""
    if config.TARGET_COLUMN not in features.columns:
        raise ValueError(f"Missing target column '{config.TARGET_COLUMN}'.")

    y = features[config.TARGET_COLUMN].dropna()
    if y.empty:
        raise ValueError("No valid target rows.")

    mean_target = float(y.mean())
    if len(y) >= min_rows_for_balance and not (
        config.TARGET_MEAN_MIN <= mean_target <= config.TARGET_MEAN_MAX
    ):
        raise AssertionError(
            f"Target mean {mean_target:.3f} outside expected "
            f"[{config.TARGET_MEAN_MIN}, {config.TARGET_MEAN_MAX}]. "
            "Fighter slot encoding may still be biased."
        )

    if "winner" in features.columns and "fighter_1" in features.columns:
        recomputed = _encode_f1_win_target(features)
        mask = recomputed.notna() & features[config.TARGET_COLUMN].notna()
        if mask.any():
            mism = (
                recomputed.loc[mask].astype(int)
                != features.loc[mask, config.TARGET_COLUMN].astype(int)
            ).sum()
            if mism:
                raise AssertionError(
                    f"Target mismatch vs winner column on {int(mism)} fights."
                )
    return mean_target


def decimal_odds_to_implied(f1_odds: pd.Series, f2_odds: pd.Series) -> pd.Series:
    """Normalize decimal odds into de-vigged implied probability for fighter 1."""
    o1 = pd.to_numeric(f1_odds, errors="coerce")
    o2 = pd.to_numeric(f2_odds, errors="coerce")
    # American odds: positive > 100, negative < -100
    american = (o1.abs() > 100) | (o2.abs() > 100)
    if american.any():
        def _american_to_decimal(odds: float) -> float:
            if pd.isna(odds):
                return np.nan
            if odds >= 100:
                return 1.0 + odds / 100.0
            if odds <= -100:
                return 1.0 + 100.0 / abs(odds)
            return odds

        o1 = o1.where(~american, o1.map(_american_to_decimal))
        o2 = o2.where(~american, o2.map(_american_to_decimal))
    p1 = 1.0 / o1.replace(0, np.nan)
    p2 = 1.0 / o2.replace(0, np.nan)
    denom = p1 + p2
    return (p1 / denom).where(denom > 0)


def _series_get(stats: dict[str, Any] | pd.Series, key: str, default: float = np.nan) -> float:
    if isinstance(stats, pd.Series):
        val = stats.get(key, default)
    else:
        val = stats.get(key, default)
    try:
        if val is None or (isinstance(val, float) and np.isnan(val)):
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def build_matchup_features(
    fighter1_stats: dict[str, Any] | pd.Series,
    fighter2_stats: dict[str, Any] | pd.Series,
    *,
    impute_values: dict[str, float] | None = None,
    weight_class: str | None = None,
) -> pd.Series:
    """
    Build a differential feature vector (fighter1 minus fighter2).

    Like trading indicator spreads: positive values favor fighter1.
    Missing inputs are filled from ``impute_values`` when provided.
    """
    impute = impute_values or {}

    def _val(stats: dict[str, Any] | pd.Series, key: str) -> float:
        raw = _series_get(stats, key, np.nan)
        if np.isnan(raw) and key in impute:
            return float(impute[key])
        return raw

    f1 = {k: _val(fighter1_stats, k) for k in FIGHTER_STAT_FIELDS}
    f2 = {k: _val(fighter2_stats, k) for k in FIGHTER_STAT_FIELDS}

    # Stance matchup: 1 when opposite-handedness (southpaw vs orthodox), else 0.
    f1_sw = f1["stance_southpaw"]
    f2_sw = f2["stance_southpaw"]
    f1_or = f1["stance_orthodox"]
    f2_or = f2["stance_orthodox"]
    stance_matchup = float(
        (f1_sw == 1 and f2_or == 1) or (f1_or == 1 and f2_sw == 1)
    )
    southpaw_advantage = float(
        (f1_sw == 1 and f2_or == 1) * 0.08 - (f1_or == 1 and f2_sw == 1) * 0.08
    )
    f1_striker, f1_grappler = compute_style_scores(
        sig_strikes_per_min=f1["sig_strikes_per_min"],
        sig_strike_acc=f1["sig_strike_acc"],
        ko_rate=f1["ko_rate"],
        td_acc=f1["td_acc"],
        sub_avg=f1["sub_avg"],
        td_defense=f1["td_defense"],
    )
    f2_striker, f2_grappler = compute_style_scores(
        sig_strikes_per_min=f2["sig_strikes_per_min"],
        sig_strike_acc=f2["sig_strike_acc"],
        ko_rate=f2["ko_rate"],
        td_acc=f2["td_acc"],
        sub_avg=f2["sub_avg"],
        td_defense=f2["td_defense"],
    )
    striker_vs_grappler = float(
        (f1_striker > 0.55 and f2_grappler > 0.55 and f1_striker >= f1_grappler)
        or (f2_striker > 0.55 and f1_grappler > 0.55 and f2_striker >= f2_grappler)
    )
    style_clash = float(
        (f1_striker > f1_grappler and f2_grappler > f2_striker)
        or (f1_grappler > f1_striker and f2_striker > f2_grappler)
    )

    wc_sens = weight_class_age_sensitivity(weight_class)
    wc_age_advantage_diff = (f2["age"] - f1["age"]) * wc_sens

    features = {
        "age_diff": f1["age"] - f2["age"],
        "wc_age_advantage_diff": wc_age_advantage_diff,
        "height_diff": f1["height_in"] - f2["height_in"],
        "reach_diff": f1["reach_in"] - f2["reach_in"],
        "stance_matchup": stance_matchup,
        "southpaw_advantage": southpaw_advantage,
        "striker_score_diff": f1_striker - f2_striker,
        "grappler_score_diff": f1_grappler - f2_grappler,
        "striker_vs_grappler": striker_vs_grappler,
        "style_clash": style_clash,
        "win_rate_diff": f1["win_rate"] - f2["win_rate"],
        "striking_acc_diff": f1["sig_strike_acc"] - f2["sig_strike_acc"],
        "takedown_acc_diff": f1["td_acc"] - f2["td_acc"],
        "sub_avg_diff": f1["sub_avg"] - f2["sub_avg"],
        "ko_rate_diff": f1["ko_rate"] - f2["ko_rate"],
        "last5_winrate_diff": f1["last5_win_rate"] - f2["last5_win_rate"],
        "momentum_diff": f1["momentum"] - f2["momentum"],
        "sig_strikes_per_min_diff": f1["sig_strikes_per_min"] - f2["sig_strikes_per_min"],
        "td_defense_diff": f1["td_defense"] - f2["td_defense"],
        "control_time_diff": f1["control_time_per_min"] - f2["control_time_per_min"],
        "elo_diff": f1["elo"] - f2["elo"],
        "days_since_last_fight_diff": f1["days_since_last_fight"] - f2["days_since_last_fight"],
        "experience_diff": f1["fight_count"] - f2["fight_count"],
        "similar_opp_win_rate_diff": f1["similar_opp_win_rate"] - f2["similar_opp_win_rate"],
        "short_notice_flag_diff": f1["short_notice_flag"] - f2["short_notice_flag"],
        "long_layoff_flag_diff": f1["long_layoff_flag"] - f2["long_layoff_flag"],
        "short_notice_perf_diff": f1["short_notice_win_rate"] - f2["short_notice_win_rate"],
        "long_layoff_perf_diff": f1["long_layoff_win_rate"] - f2["long_layoff_win_rate"],
    }
    return pd.Series(features, dtype=float)


def _normalize_rate(value: Any) -> float:
    """Map percentage (45) or decimal (0.45) to decimal rate."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return np.nan
    try:
        v = float(value)
    except (TypeError, ValueError):
        return np.nan
    if v > 1.0:
        return v / 100.0
    return v


def _fighter_history(fights: pd.DataFrame) -> pd.DataFrame:
    """Long-format fight history: one row per fighter appearance."""
    work = fights.copy()
    for col in (
        "sig_strikes_landed_f1",
        "sig_strikes_attempted_f1",
        "sig_strikes_landed_f2",
        "sig_strikes_attempted_f2",
        "takedowns_landed_f1",
        "takedowns_attempted_f1",
        "takedowns_landed_f2",
        "takedowns_attempted_f2",
        "fighter1_height",
        "fighter2_height",
        "fighter1_reach",
        "fighter2_reach",
        "fighter1_dob",
        "fighter2_dob",
        "fighter1_stance",
        "fighter2_stance",
        "fighter1_sig_strikes_landed_pm",
        "fighter2_sig_strikes_landed_pm",
        "fighter1_sig_strikes_accuracy",
        "fighter2_sig_strikes_accuracy",
        "fighter1_takedown_accuracy",
        "fighter2_takedown_accuracy",
        "fighter1_takedown_defence",
        "fighter2_takedown_defence",
        "fighter1_submission_avg_attempted_per15m",
        "fighter2_submission_avg_attempted_per15m",
    ):
        if col in work.columns:
            work[col] = _coerce_numeric(work[col]) if "stance" not in col and "dob" not in col else work[col]

    base_cols = [
        config.FIGHT_ID_COLUMN,
        config.DATE_COLUMN,
        "fighter_1",
        "fighter_2",
        "winner",
        "weight_class",
        "method",
        "round",
        "time",
    ]
    optional = [
        "sig_strikes_landed",
        "sig_strikes_attempted",
        "takedowns_landed",
        "takedowns_attempted",
        "sig_strikes_landed_f1",
        "sig_strikes_attempted_f1",
        "takedowns_landed_f1",
        "takedowns_attempted_f1",
        "sig_strikes_landed_f2",
        "sig_strikes_attempted_f2",
        "takedowns_landed_f2",
        "takedowns_attempted_f2",
        "finish",
        "reach_in",
        "height_in",
        "age",
        "stance",
        "is_title_fight",
        "is_main_event",
        "scheduled_rounds",
        "f1_odds",
        "f2_odds",
        "fighter1_height",
        "fighter2_height",
        "fighter1_reach",
        "fighter2_reach",
        "fighter1_dob",
        "fighter2_dob",
        "fighter1_stance",
        "fighter2_stance",
        "fighter1_sig_strikes_landed_pm",
        "fighter2_sig_strikes_landed_pm",
        "fighter1_sig_strikes_accuracy",
        "fighter2_sig_strikes_accuracy",
        "fighter1_takedown_accuracy",
        "fighter2_takedown_accuracy",
        "fighter1_takedown_defence",
        "fighter2_takedown_defence",
        "fighter1_submission_avg_attempted_per15m",
        "fighter2_submission_avg_attempted_per15m",
    ]
    cols = [c for c in base_cols + optional if c in work.columns]

    f1 = work[cols].copy()
    f1["fighter"] = f1["fighter_1"]
    f1["opponent"] = f1["fighter_2"]
    f1["won"] = f1.apply(lambda r: int(_winner_is_fighter(r["winner"], r["fighter_1"])), axis=1)
    f1["side"] = 1
    for src, dst in (
        ("sig_strikes_landed_f1", "sig_strikes_landed"),
        ("sig_strikes_attempted_f1", "sig_strikes_attempted"),
        ("takedowns_landed_f1", "takedowns_landed"),
        ("takedowns_attempted_f1", "takedowns_attempted"),
        ("fighter1_height", "height_in"),
        ("fighter1_reach", "reach_in"),
        ("fighter1_stance", "stance"),
        ("fighter1_sig_strikes_landed_pm", "sig_strikes_per_min_static"),
        ("fighter1_sig_strikes_accuracy", "sig_strike_acc_static"),
        ("fighter1_takedown_accuracy", "td_acc_static"),
        ("fighter1_takedown_defence", "td_defense_static"),
        ("fighter1_submission_avg_attempted_per15m", "sub_avg_static"),
    ):
        if src in f1.columns:
            f1[dst] = f1[src].map(_normalize_rate) if "acc" in dst or "defense" in dst else f1[src]
    if "fighter1_dob" in f1.columns and config.DATE_COLUMN in f1.columns:
        dob = pd.to_datetime(f1["fighter1_dob"], errors="coerce")
        f1["age"] = (f1[config.DATE_COLUMN] - dob).dt.days / 365.25

    f2 = work[cols].copy()
    f2["fighter"] = f2["fighter_2"]
    f2["opponent"] = f2["fighter_1"]
    f2["won"] = f2.apply(lambda r: int(_winner_is_fighter(r["winner"], r["fighter_2"])), axis=1)
    f2["side"] = 2
    for src, dst in (
        ("sig_strikes_landed_f2", "sig_strikes_landed"),
        ("sig_strikes_attempted_f2", "sig_strikes_attempted"),
        ("takedowns_landed_f2", "takedowns_landed"),
        ("takedowns_attempted_f2", "takedowns_attempted"),
        ("fighter2_height", "height_in"),
        ("fighter2_reach", "reach_in"),
        ("fighter2_stance", "stance"),
        ("fighter2_sig_strikes_landed_pm", "sig_strikes_per_min_static"),
        ("fighter2_sig_strikes_accuracy", "sig_strike_acc_static"),
        ("fighter2_takedown_accuracy", "td_acc_static"),
        ("fighter2_takedown_defence", "td_defense_static"),
        ("fighter2_submission_avg_attempted_per15m", "sub_avg_static"),
    ):
        if src in f2.columns:
            f2[dst] = f2[src].map(_normalize_rate) if "acc" in dst or "defense" in dst else f2[src]
    if "fighter2_dob" in f2.columns and config.DATE_COLUMN in f2.columns:
        dob = pd.to_datetime(f2["fighter2_dob"], errors="coerce")
        f2["age"] = (f2[config.DATE_COLUMN] - dob).dt.days / 365.25

    long = pd.concat([f1, f2], ignore_index=True)
    long = long.sort_values(["fighter", config.DATE_COLUMN]).reset_index(drop=True)

    for col in ("takedowns_landed", "takedowns_attempted"):
        if col not in long.columns:
            long[col] = np.nan

    # Opponent offensive stats on the same fight (for td_defense).
    opp = long[
        [config.FIGHT_ID_COLUMN, "fighter", "takedowns_landed", "takedowns_attempted"]
    ].rename(
        columns={
            "fighter": "opponent",
            "takedowns_landed": "opp_takedowns_landed",
            "takedowns_attempted": "opp_takedowns_attempted",
        }
    )
    long = long.merge(
        opp,
        on=[config.FIGHT_ID_COLUMN, "opponent"],
        how="left",
    )

    ko, sub, dec = zip(*long.get("method", pd.Series(dtype=object)).map(_method_flags))
    long["is_ko"] = ko
    long["is_sub"] = sub
    long["is_dec"] = dec
    long["ko_win"] = ((long["won"] == 1) & (long["is_ko"] == 1)).astype(int)
    long["sub_win"] = ((long["won"] == 1) & (long["is_sub"] == 1)).astype(int)
    long["fight_minutes"] = [
        _parse_fight_minutes(r, t)
        for r, t in zip(long.get("round", 1), long.get("time", ""))
    ]
    long["sig_strikes_per_min"] = np.where(
        long["fight_minutes"] > 0,
        _coerce_numeric(long.get("sig_strikes_landed", pd.Series(np.nan, index=long.index)))
        / long["fight_minutes"],
        np.nan,
    )
    long["td_defense_fight"] = np.where(
        long["opp_takedowns_attempted"] > 0,
        1.0 - (long["opp_takedowns_landed"] / long["opp_takedowns_attempted"]),
        np.nan,
    )
    return long


def _shifted_rolling_mean(series: pd.Series, window: int) -> pd.Series:
    return series.shift(1).rolling(window, min_periods=1).mean()


def _weighted_recent_wins(wins: pd.Series, weights: np.ndarray = MOMENTUM_WEIGHTS) -> pd.Series:
    """Momentum score from most recent shifted wins (newest gets highest weight)."""

    def _score(arr: np.ndarray) -> float:
        valid = arr[~np.isnan(arr)]
        if valid.size == 0:
            return np.nan
        w = weights[: valid.size]
        w = w / w.sum()
        return float(np.dot(valid[::-1], w))

    return wins.shift(1).rolling(len(weights), min_periods=1).apply(_score, raw=True)


def _compute_elo_state(history: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, float]]:
    """
    Chronological Elo updates. Returns pre-fight ratings per fight and final ratings.
    """
    fights = (
        history[history["side"] == 1][
            [config.FIGHT_ID_COLUMN, config.DATE_COLUMN, "fighter_1", "fighter_2", "winner"]
        ]
        .drop_duplicates(config.FIGHT_ID_COLUMN)
        .sort_values(config.DATE_COLUMN)
    )

    ratings: dict[str, float] = {}
    pre_fight: dict[str, dict[str, float]] = {}

    for row in fights.itertuples(index=False):
        f1 = row.fighter_1
        f2 = row.fighter_2
        r1 = ratings.get(f1, ELO_START)
        r2 = ratings.get(f2, ELO_START)
        pre_fight[row.fight_id] = {"f1_elo": r1, "f2_elo": r2}

        e1 = 1.0 / (1.0 + 10 ** ((r2 - r1) / 400.0))
        e2 = 1.0 - e1
        if _winner_is_fighter(row.winner, f1):
            s1, s2 = 1.0, 0.0
        elif _winner_is_fighter(row.winner, f2):
            s1, s2 = 0.0, 1.0
        else:
            s1, s2 = 0.5, 0.5
        ratings[f1] = r1 + ELO_K * (s1 - e1)
        ratings[f2] = r2 + ELO_K * (s2 - e2)

    elo_rows = [
        {config.FIGHT_ID_COLUMN: fid, "f1_elo": vals["f1_elo"], "f2_elo": vals["f2_elo"]}
        for fid, vals in pre_fight.items()
    ]
    return pd.DataFrame(elo_rows), ratings


def _compute_elo_ratings(history: pd.DataFrame) -> pd.DataFrame:
    """Chronological Elo updates. Returns pre-fight ratings per appearance."""
    elo_df, _ = _compute_elo_state(history)
    return elo_df


def elo_lookup_for_fights(
    fights: pd.DataFrame,
    ratings: dict[str, float],
) -> pd.DataFrame:
    """Pre-fight Elo for fights using cached post-history ratings (upcoming-safe)."""
    side1 = (
        fights.drop_duplicates(config.FIGHT_ID_COLUMN)
        .sort_values(config.DATE_COLUMN)
    )
    rows: list[dict[str, Any]] = []
    for _, row in side1.iterrows():
        f1 = row.get("fighter_1", "")
        f2 = row.get("fighter_2", "")
        rows.append(
            {
                config.FIGHT_ID_COLUMN: row[config.FIGHT_ID_COLUMN],
                "f1_elo": ratings.get(f1, ELO_START),
                "f2_elo": ratings.get(f2, ELO_START),
            }
        )
    return pd.DataFrame(rows)


def _build_history_long_pipeline(fights: pd.DataFrame) -> pd.DataFrame:
    """Long-format history with rolling, Greco, and similar-opponent stats."""
    fights = _canonicalize_fighter_slots(fights)
    history = _rolling_stats(_fighter_history(fights))
    history = fill_history_from_greco(history, window=5)
    history = _compute_similar_opponent_win_rates(history)
    return history


def _recompute_long_stats(long_df: pd.DataFrame) -> pd.DataFrame:
    """Re-run rolling/Greco/similar-opp on a fighter subset (incremental cache)."""
    out = _rolling_stats(long_df.copy())
    out = fill_history_from_greco(out, window=5)
    out = _compute_similar_opponent_win_rates(out)
    return out


def _rolling_stats(history: pd.DataFrame) -> pd.DataFrame:
    """Compute per-fighter rolling stats using only prior fights."""
    g = history.groupby("fighter", group_keys=False)
    window = config.ROLLING_FIGHTS
    last5 = 5

    history["win_rate"] = g["won"].apply(lambda s: _shifted_rolling_mean(s, window))
    history["last5_win_rate"] = g["won"].apply(lambda s: _shifted_rolling_mean(s, last5))
    history["momentum"] = g["won"].apply(_weighted_recent_wins)
    history["ko_rate"] = g["ko_win"].apply(lambda s: _shifted_rolling_mean(s, window))
    history["sub_avg"] = g["sub_win"].apply(lambda s: _shifted_rolling_mean(s, window))

    if "finish" in history.columns:
        history["finish_rate"] = g["finish"].apply(lambda s: _shifted_rolling_mean(s, window))
    else:
        history["finish_rate"] = np.nan

    if {"sig_strikes_landed", "sig_strikes_attempted"}.issubset(history.columns):
        history["_sig_acc_fight"] = np.where(
            history["sig_strikes_attempted"] > 0,
            history["sig_strikes_landed"] / history["sig_strikes_attempted"],
            np.nan,
        )
        history["sig_strike_acc"] = g["_sig_acc_fight"].apply(
            lambda s: _shifted_rolling_mean(s, window)
        )
    elif "sig_strike_acc_static" in history.columns:
        static_roll = g["sig_strike_acc_static"].apply(
            lambda s: _shifted_rolling_mean(s.map(_normalize_rate), window)
        )
        history["sig_strike_acc"] = history["sig_strike_acc"].fillna(static_roll)
    else:
        history["sig_strike_acc"] = np.nan

    if {"takedowns_landed", "takedowns_attempted"}.issubset(history.columns):
        history["_td_acc_fight"] = np.where(
            history["takedowns_attempted"] > 0,
            history["takedowns_landed"] / history["takedowns_attempted"],
            np.nan,
        )
        history["td_acc"] = g["_td_acc_fight"].apply(lambda s: _shifted_rolling_mean(s, window))
    elif "td_acc_static" in history.columns:
        static_roll = g["td_acc_static"].apply(
            lambda s: _shifted_rolling_mean(s.map(_normalize_rate), window)
        )
        history["td_acc"] = history["td_acc"].fillna(static_roll)
    else:
        history["td_acc"] = np.nan

    if "td_defense_fight" in history.columns:
        history["td_defense"] = g["td_defense_fight"].apply(
            lambda s: _shifted_rolling_mean(s, window)
        )
    elif "td_defense_static" in history.columns:
        static_roll = g["td_defense_static"].apply(
            lambda s: _shifted_rolling_mean(s.map(_normalize_rate), window)
        )
        history["td_defense"] = history["td_defense"].fillna(static_roll)
    else:
        history["td_defense"] = np.nan

    if "sig_strikes_per_min" in history.columns:
        history["sig_strikes_per_min_roll"] = g["sig_strikes_per_min"].apply(
            lambda s: _shifted_rolling_mean(s, window)
        )
    elif "sig_strikes_per_min_static" in history.columns:
        static_roll = g["sig_strikes_per_min_static"].apply(
            lambda s: _shifted_rolling_mean(pd.to_numeric(s, errors="coerce"), window)
        )
        history["sig_strikes_per_min_roll"] = history["sig_strikes_per_min_roll"].fillna(static_roll)
    else:
        history["sig_strikes_per_min_roll"] = np.nan

    if "sub_avg_static" in history.columns:
        history["sub_avg"] = history["sub_avg"].fillna(
            g["sub_avg_static"].apply(lambda s: s.shift(1).ffill())
        )

    history["control_time_per_min"] = np.nan  # not in base dataset

    # Physical attributes at fight time are known pre-bout (DOB / profile); do not shift.
    for col in ("reach_in", "height_in", "age"):
        if col in history.columns:
            history[col] = g[col].apply(lambda s: s.ffill().bfill())

    if "stance" in history.columns:
        enc = history["stance"].map(_stance_encoding).apply(pd.Series)
        for col in enc.columns:
            history[col] = enc[col]
            history[col] = g[col].apply(lambda s: s.shift(1).ffill())

    history["days_since_last_fight"] = g[config.DATE_COLUMN].apply(lambda s: s.diff().dt.days)
    history["fight_count"] = g.cumcount()

    flag_pairs = history["days_since_last_fight"].map(_layoff_context_flags)
    history["short_notice_flag"] = [p[0] for p in flag_pairs]
    history["long_layoff_flag"] = [p[1] for p in flag_pairs]
    g_ctx = history.groupby("fighter", group_keys=False)
    history["short_notice_win_rate"] = g_ctx.apply(
        lambda s: _shifted_conditional_win_rate(s["won"], s["short_notice_flag"])
    )
    history["long_layoff_win_rate"] = g_ctx.apply(
        lambda s: _shifted_conditional_win_rate(s["won"], s["long_layoff_flag"])
    )

    history["striker_score"], history["grappler_score"] = zip(
        *history.apply(
            lambda row: compute_style_scores(
                sig_strikes_per_min=row.get("sig_strikes_per_min_roll", np.nan),
                sig_strike_acc=row.get("sig_strike_acc", np.nan),
                ko_rate=row.get("ko_rate", np.nan),
                td_acc=row.get("td_acc", np.nan),
                sub_avg=row.get("sub_avg", np.nan),
                td_defense=row.get("td_defense", np.nan),
            ),
            axis=1,
        )
    )
    return history


def _fighter_stat_row(row: pd.Series, *, prefix: str) -> dict[str, float]:
    """Extract fighter stat dict from a wide feature row."""
    mapping = {
        "age": f"{prefix}_age",
        "height_in": f"{prefix}_height",
        "reach_in": f"{prefix}_reach",
        "stance_orthodox": f"{prefix}_stance_orthodox",
        "stance_southpaw": f"{prefix}_stance_southpaw",
        "stance_switch": f"{prefix}_stance_switch",
        "win_rate": f"{prefix}_win_rate",
        "sig_strike_acc": f"{prefix}_sig_strike_acc",
        "td_acc": f"{prefix}_td_acc",
        "sub_avg": f"{prefix}_sub_avg",
        "ko_rate": f"{prefix}_ko_rate",
        "last5_win_rate": f"{prefix}_last5_win_rate",
        "momentum": f"{prefix}_momentum",
        "sig_strikes_per_min": f"{prefix}_sig_strikes_per_min",
        "td_defense": f"{prefix}_td_defense",
        "control_time_per_min": f"{prefix}_control_time_per_min",
        "elo": f"{prefix}_elo",
        "days_since_last_fight": f"{prefix}_days_since_last_fight",
        "fight_count": f"{prefix}_fight_count",
        "striker_score": f"{prefix}_striker_score",
        "grappler_score": f"{prefix}_grappler_score",
        "similar_opp_win_rate": f"{prefix}_similar_opp_win_rate",
        "short_notice_flag": f"{prefix}_short_notice_flag",
        "long_layoff_flag": f"{prefix}_long_layoff_flag",
        "short_notice_win_rate": f"{prefix}_short_notice_win_rate",
        "long_layoff_win_rate": f"{prefix}_long_layoff_win_rate",
    }
    return {k: _series_get(row, col, np.nan) for k, col in mapping.items()}


def _build_impute_values(features: pd.DataFrame) -> dict[str, float]:
    """Global and weight-class median imputation table for fighter stat fields."""
    values: dict[str, float] = {}
    stat_cols = [c for c in features.columns if c.startswith(("f1_", "f2_"))]
    for field in FIGHTER_STAT_FIELDS:
        for prefix in ("f1", "f2"):
            col = f"{prefix}_{field}" if field != "sig_strike_acc" else f"{prefix}_sig_strike_acc"
            if col not in features.columns:
                alt = {
                    "sig_strike_acc": f"{prefix}_sig_strike_acc",
                    "sig_strikes_per_min": f"{prefix}_sig_strikes_per_min",
                }.get(field)
                col = alt or col
            if col in features.columns:
                med = features[col].median()
                if not np.isnan(med):
                    values[field] = float(med)
                    break
    values.setdefault("elo", ELO_START)
    values.setdefault("win_rate", 0.5)
    values.setdefault("sig_strike_acc", 0.4)
    values.setdefault("td_acc", 0.35)
    values.setdefault("td_defense", 0.65)
    values.setdefault("ko_rate", 0.15)
    values.setdefault("sub_avg", 0.1)
    values.setdefault("momentum", 0.5)
    values.setdefault("last5_win_rate", 0.5)
    values.setdefault("sig_strikes_per_min", 3.0)
    values.setdefault("control_time_per_min", 0.0)
    values.setdefault("fight_count", 3.0)
    values.setdefault("days_since_last_fight", 120.0)
    values.setdefault("striker_score", 0.45)
    values.setdefault("grappler_score", 0.35)
    values.setdefault("similar_opp_win_rate", 0.5)
    values.setdefault("short_notice_flag", 0.0)
    values.setdefault("long_layoff_flag", 0.0)
    values.setdefault("short_notice_win_rate", 0.5)
    values.setdefault("long_layoff_win_rate", 0.5)
    return values


@dataclass
class ImputerStats:
    """Train-only median fills for leakage-safe imputation."""

    global_fills: dict[str, float] = field(default_factory=dict)
    wc_fills: dict[str, dict[str, float]] = field(default_factory=dict)
    diff_fills: dict[str, float] = field(default_factory=dict)


_FIELD_TO_COL_SUFFIX = {
    "age": "age",
    "height_in": "height",
    "reach_in": "reach",
    "stance_orthodox": "stance_orthodox",
    "stance_southpaw": "stance_southpaw",
    "stance_switch": "stance_switch",
    "win_rate": "win_rate",
    "sig_strike_acc": "sig_strike_acc",
    "td_acc": "td_acc",
    "sub_avg": "sub_avg",
    "ko_rate": "ko_rate",
    "last5_win_rate": "last5_win_rate",
    "momentum": "momentum",
    "sig_strikes_per_min": "sig_strikes_per_min",
    "td_defense": "td_defense",
    "control_time_per_min": "control_time_per_min",
    "elo": "elo",
    "days_since_last_fight": "days_since_last_fight",
    "fight_count": "fight_count",
    "striker_score": "striker_score",
    "grappler_score": "grappler_score",
    "similar_opp_win_rate": "similar_opp_win_rate",
    "short_notice_flag": "short_notice_flag",
    "long_layoff_flag": "long_layoff_flag",
    "short_notice_win_rate": "short_notice_win_rate",
    "long_layoff_win_rate": "long_layoff_win_rate",
}


def fit_imputer(train_df: pd.DataFrame) -> ImputerStats:
    """Learn median imputation tables from the training slice only."""
    global_fills = _build_impute_values(train_df)
    wc_fills: dict[str, dict[str, float]] = {}
    if "weight_class" in train_df.columns:
        for wc, grp in train_df.groupby("weight_class"):
            wc_fills[str(wc)] = _build_impute_values(grp)

    diff_defaults = {
        "age_diff": 0.0,
        "height_diff": 0.0,
        "reach_diff": 0.0,
        "stance_matchup": 0.0,
        "southpaw_advantage": 0.0,
        "striker_score_diff": 0.0,
        "grappler_score_diff": 0.0,
        "striker_vs_grappler": 0.0,
        "style_clash": 0.0,
        "sentiment_diff": 0.0,
        "control_time_diff": 0.0,
        "experience_diff": 0.0,
        "days_since_last_fight_diff": 0.0,
        "wc_age_advantage_diff": 0.0,
        "similar_opp_win_rate_diff": 0.0,
        "short_notice_flag_diff": 0.0,
        "long_layoff_flag_diff": 0.0,
        "short_notice_perf_diff": 0.0,
        "long_layoff_perf_diff": 0.0,
    }
    diff_fills: dict[str, float] = {}
    for col in DIFF_FEATURE_FIELDS:
        if col not in train_df.columns:
            continue
        med = train_df[col].median()
        diff_fills[col] = diff_defaults.get(col, 0.0 if pd.isna(med) else float(med))

    for col in train_df.columns:
        if str(col).startswith("ix_"):
            med = train_df[col].median()
            diff_fills[str(col)] = 0.0 if pd.isna(med) else float(med)

    return ImputerStats(
        global_fills=global_fills,
        wc_fills=wc_fills,
        diff_fills=diff_fills,
    )


def apply_imputer(features: pd.DataFrame, stats: ImputerStats) -> pd.DataFrame:
    """Apply pre-fit imputation stats (no peeking at test/calibration rows)."""
    out = features.copy()
    for prefix in ("f1", "f2"):
        for field, suffix in _FIELD_TO_COL_SUFFIX.items():
            col = f"{prefix}_{suffix}"
            if col not in out.columns:
                continue
            global_fill = stats.global_fills.get(field, np.nan)
            fills = out[col].copy()
            if "weight_class" in out.columns:
                for wc, wc_vals in stats.wc_fills.items():
                    mask = out["weight_class"] == wc
                    fills.loc[mask] = fills.loc[mask].fillna(wc_vals.get(field, global_fill))
            out[col] = fills.fillna(global_fill)

    for col, fill in stats.diff_fills.items():
        if col in out.columns:
            out[col] = out[col].fillna(fill)
    return out


def _impute_feature_matrix(features: pd.DataFrame) -> pd.DataFrame:
    """Backward-compatible helper: fit + apply on the same frame (inference/debug only)."""
    stats = fit_imputer(features)
    return apply_imputer(features, stats)


def _nonzero_diff_mask(series: pd.Series) -> pd.Series:
    return series.notna() & (series != 0)


def log_feature_diff_coverage(
    features: pd.DataFrame,
    *,
    year: int | None = 2025,
    label: str = "features",
) -> dict[str, float]:
    """Log share of fights with non-zero key differential features."""
    if features.empty:
        return {}
    work = features.copy()
    work[config.DATE_COLUMN] = pd.to_datetime(work[config.DATE_COLUMN], errors="coerce")
    if year is not None:
        work = work[work[config.DATE_COLUMN].dt.year == year]
    if work.empty:
        logger.info("Feature coverage (%s): no rows for year=%s", label, year)
        return {}

    report: dict[str, float] = {}
    logger.info("Feature diff coverage (%s, n=%s, year=%s):", label, len(work), year)
    for col in KEY_DIFF_COVERAGE_COLS:
        if col not in work.columns:
            continue
        nonzero_pct = float(_nonzero_diff_mask(work[col]).mean())
        report[col] = nonzero_pct
        flag = " <<< sparse" if nonzero_pct < 0.25 else ""
        logger.info("  %-26s non-zero: %5.1f%%%s", col, nonzero_pct * 100, flag)
    return report


def feature_coverage_summary(
    features: pd.DataFrame,
    *,
    year: int | None = 2025,
) -> pd.DataFrame:
    """Dashboard-friendly coverage table for key differential features."""
    work = features.copy()
    work[config.DATE_COLUMN] = pd.to_datetime(work[config.DATE_COLUMN], errors="coerce")
    if year is not None:
        work = work[work[config.DATE_COLUMN].dt.year == year]
    rows = []
    for col in KEY_DIFF_COVERAGE_COLS:
        if col not in work.columns:
            continue
        nz = float(_nonzero_diff_mask(work[col]).mean()) if not work.empty else 0.0
        rows.append({"feature": col, "nonzero_pct": nz, "n_fights": len(work)})
    return pd.DataFrame(rows)


def apply_historical_stat_fallbacks(
    features: pd.DataFrame,
    *,
    reference_year: int | None = None,
    only_unlabeled: bool = False,
) -> pd.DataFrame:
    """
    Fill missing fighter stats from weight-class then global historical medians.

    Recomputes differential columns for rows that were imputed.
    When ``only_unlabeled=True``, skip labeled training rows (inference speed-up).
    """
    if features.empty:
        return features

    out = features.copy()
    out[config.DATE_COLUMN] = pd.to_datetime(out[config.DATE_COLUMN], errors="coerce")
    if reference_year is not None:
        ref = out[out[config.DATE_COLUMN].dt.year < reference_year]
        if ref.empty:
            ref = out
    else:
        ref = out

    global_fills = _build_impute_values(ref)
    wc_fills: dict[str, dict[str, float]] = {}
    if "weight_class" in ref.columns:
        for wc, grp in ref.groupby("weight_class"):
            wc_fills[str(wc)] = _build_impute_values(grp)

    stat_suffix_map = {
        "age": "age",
        "height_in": "height",
        "reach_in": "reach",
        "sig_strike_acc": "sig_strike_acc",
        "td_acc": "td_acc",
        "td_defense": "td_defense",
        "sig_strikes_per_min": "sig_strikes_per_min",
        "sub_avg": "sub_avg",
        "ko_rate": "ko_rate",
        "win_rate": "win_rate",
        "last5_win_rate": "last5_win_rate",
        "momentum": "momentum",
        "control_time_per_min": "control_time_per_min",
    }

    imputed_rows: list[int] = []
    target_col = config.TARGET_COLUMN
    if only_unlabeled and target_col in out.columns:
        row_iter = out.index[out[target_col].isna()]
    else:
        row_iter = out.index
    for idx in row_iter:
        row = out.loc[idx]
        touched = False
        wc = str(row.get("weight_class", ""))
        wc_vals = wc_fills.get(wc, global_fills)
        for field, suffix in stat_suffix_map.items():
            for prefix in ("f1", "f2"):
                col = f"{prefix}_{suffix}"
                if col not in out.columns:
                    continue
                val = row.get(col)
                if pd.notna(val):
                    continue
                greco_val = None
                fighter_col = "fighter_1" if prefix == "f1" else "fighter_2"
                if fighter_col in out.columns and config.DATE_COLUMN in out.columns:
                    as_of = row[config.DATE_COLUMN]
                    if pd.notna(as_of):
                        greco = greco_pre_fight_rolling(
                            str(row.get(fighter_col, "")),
                            as_of,
                            window=5,
                        )
                        greco_val = greco.get(field)
                fill = greco_val if pd.notna(greco_val) else wc_vals.get(field, global_fills.get(field, np.nan))
                if pd.notna(fill):
                    out.at[idx, col] = fill
                    touched = True
        if touched:
            imputed_rows.append(idx)

    if imputed_rows:
        for idx in imputed_rows:
            row = out.loc[idx]
            f1_stats = _fighter_stat_row(row, prefix="f1")
            f2_stats = _fighter_stat_row(row, prefix="f2")
            f1_stats["elo"] = row.get("f1_elo", ELO_START)
            f2_stats["elo"] = row.get("f2_elo", ELO_START)
            diffs = build_matchup_features(
                f1_stats, f2_stats, weight_class=str(row.get("weight_class", ""))
            )
            for col, val in diffs.items():
                if col in out.columns:
                    out.at[idx, col] = val
        if "striking_acc_diff" in out.columns and "sig_strike_acc_diff" in out.columns:
            out["sig_strike_acc_diff"] = out["striking_acc_diff"]

    logger.info(
        "Historical stat fallback: imputed %s/%s feature rows",
        len(imputed_rows),
        len(out),
    )
    return out


def _apply_greco_to_feature_stats(features: pd.DataFrame) -> pd.DataFrame:
    """Fill missing per-fighter model columns from Greco rolling bout stats."""
    if features.empty:
        return features

    out, touched = apply_greco_to_features(features, window=5)
    if not touched.any():
        return out

    for idx in np.where(touched)[0]:
        row = out.loc[idx]
        f1_stats = _fighter_stat_row(row, prefix="f1")
        f2_stats = _fighter_stat_row(row, prefix="f2")
        f1_stats["elo"] = row.get("f1_elo", ELO_START)
        f2_stats["elo"] = row.get("f2_elo", ELO_START)
        diffs = build_matchup_features(
            f1_stats,
            f2_stats,
            weight_class=str(row.get("weight_class", "")),
        )
        for col, val in diffs.items():
            if col in out.columns:
                out.at[idx, col] = val
    if "sig_strike_acc_diff" in out.columns and "striking_acc_diff" in out.columns:
        out["sig_strike_acc_diff"] = out["striking_acc_diff"]
    return out


def _assemble_wide_feature_matrix(
    history: pd.DataFrame,
    elo: pd.DataFrame,
    *,
    keep_unlabeled: bool = False,
    target_fight_ids: set[str] | None = None,
) -> pd.DataFrame:
    """Merge long history sides into wide differential modeling matrix."""
    work_history = history
    if target_fight_ids:
        work_history = history[
            history[config.FIGHT_ID_COLUMN].isin(target_fight_ids)
        ].copy()
        if config.DATE_COLUMN in elo.columns:
            elo = elo[elo[config.FIGHT_ID_COLUMN].isin(target_fight_ids)].copy()

    f1 = work_history[work_history["side"] == 1].copy()
    f2 = work_history[work_history["side"] == 2].copy()

    f2_rename = {
        "win_rate": "f2_win_rate",
        "last5_win_rate": "f2_last5_win_rate",
        "momentum": "f2_momentum",
        "ko_rate": "f2_ko_rate",
        "sub_avg": "f2_sub_avg",
        "finish_rate": "f2_finish_rate",
        "sig_strike_acc": "f2_sig_strike_acc",
        "td_acc": "f2_td_acc",
        "td_defense": "f2_td_defense",
        "sig_strikes_per_min_roll": "f2_sig_strikes_per_min",
        "control_time_per_min": "f2_control_time_per_min",
        "reach_in": "f2_reach",
        "height_in": "f2_height",
        "age": "f2_age",
        "stance_orthodox": "f2_stance_orthodox",
        "stance_southpaw": "f2_stance_southpaw",
        "stance_switch": "f2_stance_switch",
        "days_since_last_fight": "f2_days_since_last_fight",
        "fight_count": "f2_fight_count",
        "striker_score": "f2_striker_score",
        "grappler_score": "f2_grappler_score",
        "similar_opp_win_rate": "f2_similar_opp_win_rate",
        "short_notice_flag": "f2_short_notice_flag",
        "long_layoff_flag": "f2_long_layoff_flag",
        "short_notice_win_rate": "f2_short_notice_win_rate",
        "long_layoff_win_rate": "f2_long_layoff_win_rate",
        config.FIGHT_ID_COLUMN: config.FIGHT_ID_COLUMN,
    }
    f2_subset = f2[[c for c in f2_rename if c in f2.columns]].rename(columns=f2_rename)

    f1_rename = {
        "win_rate": "f1_win_rate",
        "last5_win_rate": "f1_last5_win_rate",
        "momentum": "f1_momentum",
        "ko_rate": "f1_ko_rate",
        "sub_avg": "f1_sub_avg",
        "finish_rate": "f1_finish_rate",
        "sig_strike_acc": "f1_sig_strike_acc",
        "td_acc": "f1_td_acc",
        "td_defense": "f1_td_defense",
        "sig_strikes_per_min_roll": "f1_sig_strikes_per_min",
        "control_time_per_min": "f1_control_time_per_min",
        "reach_in": "f1_reach",
        "height_in": "f1_height",
        "age": "f1_age",
        "stance_orthodox": "f1_stance_orthodox",
        "stance_southpaw": "f1_stance_southpaw",
        "stance_switch": "f1_stance_switch",
        "days_since_last_fight": "f1_days_since_last_fight",
        "fight_count": "f1_fight_count",
        "striker_score": "f1_striker_score",
        "grappler_score": "f1_grappler_score",
        "similar_opp_win_rate": "f1_similar_opp_win_rate",
        "short_notice_flag": "f1_short_notice_flag",
        "long_layoff_flag": "f1_long_layoff_flag",
        "short_notice_win_rate": "f1_short_notice_win_rate",
        "long_layoff_win_rate": "f1_long_layoff_win_rate",
    }
    features = f1.rename(columns=f1_rename)
    features = features.merge(f2_subset, on=config.FIGHT_ID_COLUMN, how="inner")
    features = features.merge(elo, on=config.FIGHT_ID_COLUMN, how="left")
    del f1, f2, f2_subset
    features["f1_elo"] = features["f1_elo"].fillna(ELO_START)
    features["f2_elo"] = features["f2_elo"].fillna(ELO_START)

    features[config.TARGET_COLUMN] = _encode_f1_win_target(features)
    if keep_unlabeled:
        labeled = features[config.TARGET_COLUMN].notna()
        if labeled.any():
            features.loc[labeled, config.TARGET_COLUMN] = features.loc[
                labeled, config.TARGET_COLUMN
            ].astype(int)
    else:
        features = features.dropna(subset=[config.TARGET_COLUMN]).copy()
        features[config.TARGET_COLUMN] = features[config.TARGET_COLUMN].astype(int)

    diff_rows = []
    for row in features.to_dict(orient="records"):
        f1_stats = _fighter_stat_row(pd.Series(row), prefix="f1")
        f2_stats = _fighter_stat_row(pd.Series(row), prefix="f2")
        f1_stats["elo"] = row.get("f1_elo", ELO_START)
        f2_stats["elo"] = row.get("f2_elo", ELO_START)
        diff_rows.append(
            build_matchup_features(
                f1_stats,
                f2_stats,
                weight_class=str(row.get("weight_class", "")),
            ).to_dict()
        )
    diff_df = pd.DataFrame(diff_rows)
    features = pd.concat([features.reset_index(drop=True), diff_df], axis=1)

    features = _apply_greco_to_feature_stats(features)

    features["sig_strike_acc_diff"] = features["striking_acc_diff"]
    features["td_acc_diff"] = features["takedown_acc_diff"]
    if "f1_finish_rate" in features.columns and "f2_finish_rate" in features.columns:
        features["finish_rate_diff"] = features["f1_finish_rate"] - features["f2_finish_rate"]
    else:
        features["finish_rate_diff"] = np.nan

    for col in ("is_title_fight", "is_main_event"):
        if col not in features.columns:
            features[col] = 0
    if "scheduled_rounds" not in features.columns:
        features["scheduled_rounds"] = np.where(
            features.get("is_title_fight", 0).astype(bool), 5, 3
        )

    if "f1_odds" in features.columns and "f2_odds" in features.columns:
        features["implied_prob_f1"] = decimal_odds_to_implied(
            features["f1_odds"], features["f2_odds"]
        )
        features["implied_prob_f2"] = 1.0 - features["implied_prob_f1"]

    min_fights = config.MIN_FIGHTS_PER_FIGHTER
    mask = (features["f1_fight_count"] >= min_fights) & (
        features["f2_fight_count"] >= min_fights
    )
    features = features.loc[mask].copy()
    features["sentiment_f1"] = 0.0
    features["sentiment_f2"] = 0.0
    features["sentiment_diff"] = 0.0
    features = features.sort_values(config.DATE_COLUMN).reset_index(drop=True)

    log_feature_diff_coverage(features, year=2025, label="before fallback")
    features = apply_historical_stat_fallbacks(
        features, reference_year=2025, only_unlabeled=keep_unlabeled
    )
    log_feature_diff_coverage(features, year=2025, label="after fallback")

    features = build_interaction_candidates(features)

    if features[config.TARGET_COLUMN].notna().any():
        assert_target_encoding(features)
    elif not keep_unlabeled:
        raise ValueError("No valid target rows.")

    return features


def build_feature_matrix(
    fights: pd.DataFrame,
    *,
    keep_unlabeled: bool = False,
    use_fighter_cache: bool = False,
    target_fight_ids: set[str] | None = None,
) -> pd.DataFrame:
    """
    Transform raw fights into a differential modeling matrix.

    Target ``f1_win`` is 1 when fighter_1 wins. All rolling/Elo features use
    only information available before each fight.

    When ``use_fighter_cache`` is True (inference on upcoming cards), reuses
    persisted per-fighter rolling stats and only recomputes fighters on the card.
    """
    if use_fighter_cache and keep_unlabeled and target_fight_ids:
        from src.fighter_cache import build_features_with_cache

        return build_features_with_cache(
            fights,
            target_fight_ids=target_fight_ids,
            keep_unlabeled=keep_unlabeled,
        )

    history = _build_history_long_pipeline(fights)
    elo = _compute_elo_ratings(history)
    features = _assemble_wide_feature_matrix(
        history,
        elo,
        keep_unlabeled=keep_unlabeled,
        target_fight_ids=target_fight_ids,
    )
    del history, elo
    return features


def _interaction_product(
    df: pd.DataFrame,
    factor_a: str,
    factor_b: str,
) -> pd.Series:
    """Element-wise product with NaN-safe zero fill for missing factors."""
    if factor_a not in df.columns or factor_b not in df.columns:
        return pd.Series(np.nan, index=df.index)
    a = pd.to_numeric(df[factor_a], errors="coerce").fillna(0.0)
    b = pd.to_numeric(df[factor_b], errors="coerce").fillna(0.0)
    return a * b


def build_interaction_candidates(features: pd.DataFrame) -> pd.DataFrame:
    """Add all predefined interaction candidate columns (train-time subset selected later)."""
    if features.empty:
        return features
    out = features.copy()
    for spec in INTERACTION_SPECS:
        out[spec.name] = _interaction_product(out, spec.factor_a, spec.factor_b)
    return out


def interaction_candidate_names() -> list[str]:
    return [spec.name for spec in INTERACTION_SPECS]


def interaction_specs_from_records(records: list[dict[str, Any]] | None) -> list[InteractionSpec]:
    """Rebuild InteractionSpec list from model artifact / JSON."""
    if not records:
        return []
    specs: list[InteractionSpec] = []
    for row in records:
        name = row.get("name")
        factor_a = row.get("factor_a")
        factor_b = row.get("factor_b")
        if not name or not factor_a or not factor_b:
            continue
        specs.append(
            InteractionSpec(
                name=str(name),
                factor_a=str(factor_a),
                factor_b=str(factor_b),
                label=str(row.get("label", name)),
            )
        )
    return specs


def apply_interaction_specs(
    features: pd.DataFrame,
    specs: list[InteractionSpec] | list[dict[str, Any]] | None,
) -> pd.DataFrame:
    """Compute only the interaction columns required for inference."""
    if features.empty or not specs:
        return features
    parsed = (
        specs
        if specs and isinstance(specs[0], InteractionSpec)
        else interaction_specs_from_records(specs)  # type: ignore[arg-type]
    )
    if not parsed:
        return features
    out = features.copy()
    for spec in parsed:
        out[spec.name] = _interaction_product(out, spec.factor_a, spec.factor_b)
    return out


def save_features(df: pd.DataFrame, path: Path | str | None = None) -> Path:
    """Write processed features to CSV."""
    ensure_data_dirs()
    out = Path(path) if path else config.PROCESSED_FEATURES_CSV
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    return out
