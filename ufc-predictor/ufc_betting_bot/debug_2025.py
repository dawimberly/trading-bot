#!/usr/bin/env python3
"""Debug 2025 backtest accuracy — label flip, name mismatch, model collapse."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

if __name__ == "__main__" and str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ufc_betting_bot.config.settings import (
    BACKTEST_2025_CSV,
    DATE_COLUMN,
    REPORTS_DIR,
    TARGET_COLUMN,
    get_settings,
)
from ufc_betting_bot.modules.model_bridge import _ensure_predictor_path
from ufc_betting_bot.modules.naming import clean_fighter_name, fighters_same_person

def _safe(s: object, width: int = 0) -> str:
    text = str(s or "").encode("ascii", "replace").decode("ascii")
    return text[:width] if width else text


DIFF_COLS = [
    "age_diff",
    "height_diff",
    "reach_diff",
    "striking_acc_diff",
    "elo_diff",
    "win_rate_diff",
    "sig_strikes_per_min_diff",
    "td_defense_diff",
]

ENRICH_COLS = [
    ("fighter1_height", "fighter2_height", "height"),
    ("fighter1_reach", "fighter2_reach", "reach"),
    ("fighter1_sig_strikes_accuracy", "fighter2_sig_strikes_accuracy", "sig_acc"),
    ("fighter1_dob", "fighter2_dob", "dob"),
]


def _winner_matches(fighter: str, winner: str) -> bool:
    return fighters_same_person(clean_fighter_name(fighter), clean_fighter_name(winner))


def _nonzero_pct(series: pd.Series) -> float:
    return float((series.notna() & (series != 0)).mean())


def _report_fight_enrichment(fights: pd.DataFrame, *, year: int, label: str) -> dict[str, float]:
    """Share of year fights with both fighters having enrichment columns filled."""
    fights = fights.copy()
    fights["event_date"] = pd.to_datetime(
        fights.get("event_date", fights.get("date")), errors="coerce"
    )
    sub = fights[fights["event_date"].dt.year == year]
    if sub.empty:
        print(f"\n=== Fight enrichment ({label}, {year}) ===")
        print(f"  No {year} fights in fights.csv")
        return {}

    print(f"\n=== Fight enrichment ({label}, {year}, n={len(sub)}) ===")
    report: dict[str, float] = {}
    for c1, c2, short in ENRICH_COLS:
        if c1 not in sub.columns or c2 not in sub.columns:
            continue
        both = sub[c1].notna() & sub[c2].notna()
        pct = float(both.mean())
        report[short] = pct
        flag = " <<< sparse" if pct < 0.5 else ""
        print(f"  {short:<10}  both fighters filled: {pct:.0%}{flag}")
    return report


def _report_feature_diffs(df: pd.DataFrame, *, label: str) -> dict[str, float]:
    print(f"\n=== Feature diffs ({label}, n={len(df)}) ===")
    report: dict[str, float] = {}
    for col in DIFF_COLS:
        if col not in df.columns:
            continue
        nz = _nonzero_pct(df[col])
        nuniq = int(df[col].nunique())
        report[col] = nz
        flag = " <<< ALL ZERO" if nuniq <= 1 else (" <<< sparse" if nz < 0.25 else "")
        print(f"  {col:<26}  unique={nuniq:>4}  non-zero={nz:.0%}{flag}")
    return report


def _load_predictions(*, rerun_events: int = 0) -> pd.DataFrame:
    if rerun_events <= 0 and BACKTEST_2025_CSV.is_file():
        return pd.read_csv(BACKTEST_2025_CSV, parse_dates=[DATE_COLUMN])

    from ufc_betting_bot.backtester.backtest_2025 import walk_forward_events
    from ufc_betting_bot.modules.model_bridge import get_predictor, load_features

    features = load_features()
    features[DATE_COLUMN] = pd.to_datetime(features[DATE_COLUMN], errors="coerce")
    year_feats = features[features[DATE_COLUMN].dt.year == get_settings().backtest_year]
    events = (
        year_feats[["event", DATE_COLUMN]]
        .drop_duplicates()
        .sort_values(DATE_COLUMN)
        .head(rerun_events)
    )
    if events.empty:
        raise ValueError("No 2025 events in feature matrix.")

    event_keys = (
        year_feats["event"].astype(str) + "|" + year_feats[DATE_COLUMN].dt.normalize().astype(str)
    )
    year_feats = year_feats.copy()
    year_feats["event_key"] = event_keys
    keep_keys = set(
        events["event"].astype(str) + "|" + events[DATE_COLUMN].dt.normalize().astype(str)
    )
    subset = year_feats[year_feats["event_key"].isin(keep_keys)]
    predictor = get_predictor()
    return walk_forward_events(subset, predictor, target_year=get_settings().backtest_year)


def diagnose_2025_predictions(
    predictions: pd.DataFrame | None = None,
    *,
    sample_n: int = 20,
    rerun_first_n_events: int = 0,
    compare_enrichment: bool = True,
) -> dict:
    """
    Diagnose low 2025 accuracy: label consistency, systematic side bias, feature shifts.
    """
    _ensure_predictor_path()
    target_year = get_settings().backtest_year
    df = predictions if predictions is not None else _load_predictions(rerun_events=rerun_first_n_events)

    if compare_enrichment:
        try:
            from src.data_loader import enrich_fights_with_ufcstats
            from src.feature_engineering import (
                apply_historical_stat_fallbacks,
                build_feature_matrix,
                log_feature_diff_coverage,
            )
            from ufc_betting_bot.modules.model_bridge import load_fights

            fights = load_fights()
            _report_fight_enrichment(fights, year=target_year, label="before ufcstats enrich")

            enriched = enrich_fights_with_ufcstats(
                fights, scrape_recent=False, force_refresh=False
            )
            _report_fight_enrichment(enriched, year=target_year, label="after ufcstats enrich")

            year_fights = enriched[
                pd.to_datetime(enriched.get("date", enriched.get("event_date")), errors="coerce").dt.year
                == target_year
            ]
            if not year_fights.empty:
                hist = enriched[
                    pd.to_datetime(enriched.get("date", enriched.get("event_date")), errors="coerce").dt.year
                    < target_year
                ]
                mini = pd.concat([hist.tail(3000), year_fights], ignore_index=True)
                raw_features = build_feature_matrix(mini)
                raw_2025 = raw_features[
                    pd.to_datetime(raw_features["event_date"], errors="coerce").dt.year == target_year
                ]
                _report_feature_diffs(raw_2025, label="after enrich (pre-fallback)")
                fb = apply_historical_stat_fallbacks(raw_features, reference_year=target_year)
                fb_2025 = fb[
                    pd.to_datetime(fb["event_date"], errors="coerce").dt.year == target_year
                ]
                _report_feature_diffs(fb_2025, label="after enrich + fallback")
                log_feature_diff_coverage(fb_2025, year=target_year, label="diagnose")
        except Exception as exc:
            print(f"\n=== Enrichment compare skipped: {exc} ===")

    required = ["fighter_1", "fighter_2", "winner", TARGET_COLUMN, "prob_f1_win", "correct"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Predictions missing columns: {missing}")

    df = df.copy()
    df["label_from_winner"] = df.apply(
        lambda r: int(_winner_matches(r["fighter_1"], r["winner"])), axis=1
    )
    df["label_mismatch"] = df[TARGET_COLUMN] != df["label_from_winner"]
    df["model_picks_f1"] = df["prob_f1_win"] >= 0.5
    df["actual_f1_won"] = df[TARGET_COLUMN] == 1
    df["actual_winner_is_f1"] = df.apply(
        lambda r: _winner_matches(r["fighter_1"], r["winner"]), axis=1
    )

    # --- Sample fights ---
    print("=== 2025 Prediction Samples (first 20 fights) ===")
    sample = df.head(sample_n)
    for _, row in sample.iterrows():
        actual = row["fighter_1"] if row[TARGET_COLUMN] == 1 else row["fighter_2"]
        ok = "Y" if row["correct"] else "N"
        print(
            f"  [{ok}] {_safe(row.get('event_name', row.get('event', '')), 36):<36} | "
            f"F1={_safe(row['fighter_1'], 22):<22} F2={_safe(row['fighter_2'], 22):<22} | "
            f"P(F1)={row['prob_f1_win']:.3f} | Actual={_safe(actual)} | "
            f"f1_win={int(row[TARGET_COLUMN])} label_ok={not row['label_mismatch']}"
        )

    # --- Systematic bias ---
    print("\n=== Systematic prediction bias ===")
    pick_f1_rate = float(df["model_picks_f1"].mean())
    f1_win_rate = float(df[TARGET_COLUMN].mean())
    prob_mean = float(df["prob_f1_win"].mean())
    prob_std = float(df["prob_f1_win"].std())
    print(f"  Model picks fighter_1:     {pick_f1_rate:.1%}  ({(df['prob_f1_win']>=0.5).sum()}/{len(df)} fights)")
    print(f"  Actual f1_win rate:        {f1_win_rate:.1%}")
    print(f"  prob_f1_win mean +/- std:  {prob_mean:.3f} +/- {prob_std:.3f}")
    print(f"  prob_f1_win min / max:     {df['prob_f1_win'].min():.3f} / {df['prob_f1_win'].max():.3f}")
    if pick_f1_rate > 0.95:
        print("  >>> LIKELY ROOT CAUSE: model almost always predicts fighter_1 wins.")
        print("      Accuracy ~= f1_win base rate, not random 50%.")

    # --- Label consistency ---
    print("\n=== Winner / f1_win label consistency ===")
    n_mismatch = int(df["label_mismatch"].sum())
    print(f"  f1_win != (winner==fighter_1):  {n_mismatch} / {len(df)} fights")
    if n_mismatch:
        bad = df[df["label_mismatch"]].head(5)
        for _, row in bad.iterrows():
            print(
                f"    MISMATCH: F1={_safe(row['fighter_1'])} F2={_safe(row['fighter_2'])} "
                f"winner={_safe(row['winner'])} f1_win={row[TARGET_COLUMN]}"
            )
    else:
        print("  Labels consistent - NOT a simple label flip.")

    # Wrong-side analysis when f2 actually won
    f2_wins = df[df[TARGET_COLUMN] == 0]
    print(f"\n  When fighter_2 won ({len(f2_wins)} fights):")
    print(f"    Model still picked F1: {(f2_wins['model_picks_f1']).sum()} ({f2_wins['model_picks_f1'].mean():.1%})")
    print(f"    Mean P(F1) on F2 wins:  {f2_wins['prob_f1_win'].mean():.3f}")

    # --- Prediction-row feature coverage (backtest output) ---
    _report_feature_diffs(df, label=f"backtest predictions ({target_year})")

    # --- Feature distributions ---
    print("\n=== Feature means: correct vs incorrect predictions ===")
    correct = df[df["correct"] == 1]
    wrong = df[df["correct"] == 0]
    rows = []
    for col in DIFF_COLS:
        if col not in df.columns:
            continue
        c_ok = correct[col].mean()
        c_bad = wrong[col].mean()
        rows.append((col, c_ok, c_bad, c_ok - c_bad))
        print(f"  {col:<22}  correct={c_ok:>8.4f}  wrong={c_bad:>8.4f}  delta={c_ok - c_bad:>8.4f}")

    # --- Baselines ---
    print("\n=== Baseline accuracies ===")
    acc_model = float(df["correct"].mean())
    acc_always_f1 = f1_win_rate
    acc_always_f2 = 1.0 - f1_win_rate

    # Favorite by lower decimal odds (when odds exist)
    fav_correct = np.nan
    if "f1_odds" in df.columns and "f2_odds" in df.columns:
        has_odds = df["f1_odds"].notna() & df["f2_odds"].notna()
        if has_odds.any():
            sub = df[has_odds].copy()
            sub["fav_is_f1"] = sub["f1_odds"] < sub["f2_odds"]
            sub["fav_correct"] = sub["fav_is_f1"] == (sub[TARGET_COLUMN] == 1)
            fav_correct = float(sub["fav_correct"].mean())
            print(f"  Pick odds favorite:        {fav_correct:.1%}  (n={len(sub)})")

    # Higher Elo
    if "f1_elo" in df.columns and "f2_elo" in df.columns:
        sub = df[df["f1_elo"].notna() & df["f2_elo"].notna()]
        if not sub.empty:
            elo_f1 = (sub["f1_elo"] >= sub["f2_elo"]) == (sub[TARGET_COLUMN] == 1)
            print(f"  Pick higher Elo:           {elo_f1.mean():.1%}  (n={len(sub)})")

    print(f"  Model (current):           {acc_model:.1%}")
    print(f"  Always pick fighter_1:     {acc_always_f1:.1%}")
    print(f"  Always pick fighter_2:     {acc_always_f2:.1%}")
    print(f"  Coin flip:                 50.0%")

    # --- Per-event quick view (first 5 events) ---
    if "event_name" in df.columns:
        print("\n=== First 5 events ===")
        for ev, grp in list(df.groupby("event_name"))[:5]:
            acc = grp["correct"].mean()
            p1 = grp["model_picks_f1"].mean()
            print(
                f"  {_safe(ev, 40):<40}  n={len(grp):>2}  acc={acc:.1%}  "
                f"picks_f1={p1:.0%}  f1_win_rate={grp[TARGET_COLUMN].mean():.0%}"
            )

    return {
        "n_fights": len(df),
        "accuracy": acc_model,
        "always_pick_f1_rate": pick_f1_rate,
        "f1_win_rate": f1_win_rate,
        "prob_f1_mean": prob_mean,
        "label_mismatches": n_mismatch,
        "baseline_always_f1": acc_always_f1,
        "baseline_favorite_odds": fav_correct,
    }


if __name__ == "__main__":
    diagnose_2025_predictions()
