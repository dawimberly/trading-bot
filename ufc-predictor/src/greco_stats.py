"""Pre-fight rolling stats from Greco1899 ufc_fight_stats.csv."""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Any

import numpy as np
import pandas as pd

import config
from src.data_loader import _download_greco_csv, _parse_ctrl_seconds, _parse_of_fraction, _parse_pct_value, clean_fighter_name

logger = logging.getLogger(__name__)

_STAT_FIELDS = (
    "sig_strike_acc",
    "td_acc",
    "sig_strikes_per_min",
    "td_defense",
    "sub_avg",
    "control_time_per_min",
)

_HISTORY_FILL_MAP = {
    "sig_strike_acc": "sig_strike_acc",
    "td_acc": "td_acc",
    "sig_strikes_per_min_roll": "sig_strikes_per_min",
    "td_defense": "td_defense",
    "sub_avg": "sub_avg",
    "control_time_per_min": "control_time_per_min",
}

_FEATURE_FILL_MAP = {
    "sig_strike_acc": "sig_strike_acc",
    "td_acc": "td_acc",
    "sig_strikes_per_min": "sig_strikes_per_min",
    "td_defense": "td_defense",
    "sub_avg": "sub_avg",
}


def _parse_round_num(value: Any) -> float:
    text = str(value or "")
    match = re.search(r"(\d+)", text)
    return float(match.group(1)) if match else 3.0


@lru_cache(maxsize=1)
def load_greco_bout_stats(*, force_refresh: bool = False) -> pd.DataFrame:
    """Per-fighter per-bout aggregates with event dates."""
    try:
        stats = _download_greco_csv("ufc_fight_stats.csv", force_refresh=force_refresh)
        events = _download_greco_csv("ufc_event_details.csv", force_refresh=force_refresh)
    except Exception as exc:
        logger.warning("Greco bout stats unavailable: %s", exc)
        return pd.DataFrame(columns=["fighter", "bout_date", *_STAT_FIELDS])

    stats = stats.rename(columns=str.upper)
    events = events.rename(columns=str.upper)

    landed, attempted = zip(*stats["SIG.STR."].map(_parse_of_fraction))
    stats["sig_landed"] = landed
    stats["sig_attempted"] = attempted
    td_landed, td_attempted = zip(*stats["TD"].map(_parse_of_fraction))
    stats["td_landed"] = td_landed
    stats["td_attempted"] = td_attempted
    stats["sig_acc_pct"] = stats["SIG.STR. %"].map(_parse_pct_value)
    stats["ctrl_sec"] = stats["CTRL"].map(_parse_ctrl_seconds)
    stats["sub_att"] = pd.to_numeric(stats["SUB.ATT"], errors="coerce")
    stats["fighter"] = stats["FIGHTER"].map(clean_fighter_name)
    stats["round_num"] = stats["ROUND"].map(_parse_round_num)

    bout = stats.groupby(["EVENT", "BOUT", "FIGHTER"], as_index=False).agg(
        sig_landed=("sig_landed", "sum"),
        sig_attempted=("sig_attempted", "sum"),
        td_landed=("td_landed", "sum"),
        td_attempted=("td_attempted", "sum"),
        sub_att=("sub_att", "sum"),
        ctrl_sec=("ctrl_sec", "sum"),
        max_round=("round_num", "max"),
        sig_acc_pct=("sig_acc_pct", "mean"),
    )
    bout["fighter"] = bout["FIGHTER"].map(clean_fighter_name)
    bout["fight_minutes"] = bout["max_round"].fillna(3) * 5.0
    bout["sig_strikes_per_min"] = bout["sig_landed"] / bout["fight_minutes"].replace(0, np.nan)
    bout["sig_strike_acc"] = np.where(
        bout["sig_attempted"] > 0,
        bout["sig_landed"] / bout["sig_attempted"],
        bout["sig_acc_pct"],
    )
    bout["td_acc"] = np.where(
        bout["td_attempted"] > 0,
        bout["td_landed"] / bout["td_attempted"],
        np.nan,
    )
    bout["sub_avg"] = bout["sub_att"]
    bout["control_time_per_min"] = bout["ctrl_sec"] / bout["fight_minutes"].replace(0, np.nan)

    opp = bout[["EVENT", "BOUT", "fighter", "td_landed", "td_attempted"]].rename(
        columns={"fighter": "opponent", "td_landed": "opp_td_landed", "td_attempted": "opp_td_attempted"}
    )
    paired = bout.merge(opp, on=["EVENT", "BOUT"], how="left")
    paired = paired[paired["opponent"] != paired["fighter"]]
    paired["td_defense"] = np.where(
        paired["opp_td_attempted"] > 0,
        1.0 - paired["opp_td_landed"] / paired["opp_td_attempted"],
        np.nan,
    )
    bout = bout.merge(
        paired.groupby(["EVENT", "BOUT", "fighter"], as_index=False)["td_defense"].mean(),
        on=["EVENT", "BOUT", "fighter"],
        how="left",
    )

    event_dates = events.set_index("EVENT")["DATE"]
    bout["bout_date"] = pd.to_datetime(bout["EVENT"].map(event_dates), errors="coerce")
    bout = bout.dropna(subset=["fighter", "bout_date"])
    return bout.sort_values(["fighter", "bout_date"]).reset_index(drop=True)


@lru_cache(maxsize=4)
def _greco_pre_fight_table(window: int = 5) -> pd.DataFrame:
    """Rolling means of prior Greco bouts per fighter (shifted, no leakage)."""
    bout = load_greco_bout_stats()
    if bout.empty:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for fighter, grp in bout.groupby("fighter"):
        grp = grp.sort_values("bout_date").reset_index(drop=True)
        for i in range(len(grp)):
            prior = grp.iloc[max(0, i - window) : i]
            if prior.empty:
                continue
            row: dict[str, Any] = {"fighter": fighter, "as_of_date": grp.iloc[i]["bout_date"]}
            for field in _STAT_FIELDS:
                row[field] = prior[field].mean()
            rows.append(row)
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    out["as_of_date"] = pd.to_datetime(out["as_of_date"], errors="coerce")
    return out.sort_values(["fighter", "as_of_date"]).reset_index(drop=True)


def _merge_asof_greco(
    left: pd.DataFrame,
    *,
    fighter_col: str,
    date_col: str,
    window: int = 5,
) -> pd.DataFrame:
    greco = _greco_pre_fight_table(window)
    if greco.empty or fighter_col not in left.columns or date_col not in left.columns:
        return left

    work = left.copy().reset_index(drop=True)
    work[date_col] = pd.to_datetime(work[date_col], errors="coerce")
    stat_cols = [c for c in _STAT_FIELDS if c in greco.columns]
    for col in stat_cols:
        work[col] = np.nan

    for fighter, grp in work.groupby(fighter_col, sort=False):
        g = greco[greco["fighter"] == clean_fighter_name(str(fighter))].sort_values("as_of_date")
        if g.empty:
            continue
        valid = grp[date_col].notna()
        if not valid.any():
            continue
        idx = g["as_of_date"].searchsorted(grp.loc[valid, date_col].values, side="right") - 1
        for col in stat_cols:
            values = np.where(idx >= 0, g[col].to_numpy()[np.maximum(idx, 0)], np.nan)
            values = np.where(idx >= 0, values, np.nan)
            work.loc[grp.index[valid], col] = values

    return work


def fill_history_from_greco(history: pd.DataFrame, *, window: int = 5) -> pd.DataFrame:
    """Fill NaN rolling stat columns using Greco pre-fight averages."""
    if history.empty:
        return history

    date_col = config.DATE_COLUMN
    if date_col not in history.columns or "fighter" not in history.columns:
        return history

    out = history.reset_index(drop=True).copy()
    merged = _merge_asof_greco(out, fighter_col="fighter", date_col=date_col, window=window)

    filled = 0
    for hist_col, greco_col in _HISTORY_FILL_MAP.items():
        if hist_col not in out.columns or greco_col not in merged.columns:
            continue
        mask = out[hist_col].isna() & merged[greco_col].notna()
        out.loc[mask, hist_col] = merged.loc[mask, greco_col]
        filled += int(mask.sum())

    if filled:
        logger.info("Greco rolling fill: %s stat cells", filled)
    return out


def greco_pre_fight_rolling(
    fighter: str,
    as_of: pd.Timestamp,
    *,
    window: int = 5,
) -> dict[str, float]:
    """Single-fighter lookup (used in fallbacks)."""
    clean = clean_fighter_name(fighter)
    if not clean:
        return {}
    ts = pd.to_datetime(as_of, errors="coerce")
    if pd.isna(ts):
        return {}
    greco = _greco_pre_fight_table(window)
    if greco.empty:
        return {}
    sub = greco[(greco["fighter"] == clean) & (greco["as_of_date"] < ts.normalize())]
    if sub.empty:
        return {}
    row = sub.iloc[-1]
    return {f: float(row[f]) for f in _STAT_FIELDS if f in row and pd.notna(row[f])}


def apply_greco_to_features(features: pd.DataFrame, *, window: int = 5) -> pd.DataFrame:
    """Vectorized Greco fill on wide feature rows for f1_/f2_ stat columns."""
    if features.empty:
        return features

    date_col = config.DATE_COLUMN
    out = features.copy()
    touched = np.zeros(len(out), dtype=bool)

    for prefix, fighter_col in (("f1", "fighter_1"), ("f2", "fighter_2")):
        if fighter_col not in out.columns:
            continue
        chunk = out[[fighter_col, date_col]].copy()
        chunk = chunk.rename(columns={fighter_col: "fighter"})
        enriched = _merge_asof_greco(chunk, fighter_col="fighter", date_col=date_col, window=window)

        for feat_suffix, greco_col in _FEATURE_FILL_MAP.items():
            col = f"{prefix}_{feat_suffix}"
            if col not in out.columns or greco_col not in enriched.columns:
                continue
            mask = out[col].isna() & enriched[greco_col].notna()
            out.loc[mask, col] = enriched.loc[mask, greco_col]
            touched[mask.to_numpy()] = True

    return out, touched

