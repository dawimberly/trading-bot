"""Event walk-forward backtest with odds-aware edge and bankroll rules."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from typing import Any

import numpy as np
import pandas as pd

from ufc_betting_bot.backtester.metrics import (
    build_calibration_bins,
    evaluate_classification,
    segment_metrics,
)
from ufc_betting_bot.backtester.reports import (
    print_backtest_summary,
    save_calibration_plot,
    save_roi_plot,
)
from ufc_betting_bot.config.settings import (
    BACKTEST_2025_CSV,
    DATE_COLUMN,
    PLOTS_DIR,
    REPORTS_DIR,
    TARGET_COLUMN,
    BankrollSettings,
    ensure_dirs,
    get_settings,
)
from ufc_betting_bot.modules.bankroll import (
    simulate_all_staking_modes,
    simulate_bankroll_bets,
    simulate_bankroll_bets_dynamic,
    sweep_bankroll_thresholds,
)
from ufc_betting_bot.modules.dynamic_thresholds import print_threshold_comparison_report
from ufc_betting_bot.modules.edge import compute_edge, market_probs
from ufc_betting_bot.modules.model_bridge import get_predictor, load_features, load_fights, model_exists

logger = logging.getLogger(__name__)


@dataclass
class Backtest2025Result:
    predictions: pd.DataFrame
    overall_metrics: dict[str, float]
    segment_metrics: dict[str, dict[str, float]]
    per_event: pd.DataFrame
    bankroll_sweep: pd.DataFrame
    bankroll_trades: pd.DataFrame
    bankroll_summary: dict[str, float]
    staking_modes: pd.DataFrame
    calibration_bins: pd.DataFrame
    target_year: int
    fights_with_odds: int
    report_dir: Path
    monte_carlo: Any = None
    threshold_comparison: dict[str, Any] | None = None


def _attach_event_names(features: pd.DataFrame) -> pd.DataFrame:
    out = features.copy()
    if "event" in out.columns and out["event"].notna().any():
        return out

    fights = load_fights()
    if "fight_id" not in out.columns or "event" not in fights.columns:
        out["event"] = out.get("event_name", pd.Series("Unknown", index=out.index))
        return out

    meta = fights[["fight_id", "event"]].drop_duplicates("fight_id")
    out = out.merge(meta, on="fight_id", how="left")
    out["event"] = out["event"].fillna("Unknown")
    return out


def walk_forward_events(
    features: pd.DataFrame,
    predictor,
    *,
    target_year: int,
) -> pd.DataFrame:
    """Imputer fit on fights before event N; predict event N with frozen model."""
    from src.feature_engineering import apply_imputer, apply_interaction_specs, fit_imputer

    interaction_specs = getattr(predictor, "interaction_specs", None) or []

    df = _attach_event_names(features)
    df[DATE_COLUMN] = pd.to_datetime(df[DATE_COLUMN], errors="coerce")
    df = df.dropna(subset=[DATE_COLUMN, TARGET_COLUMN])
    df["event_key"] = (
        df["event"].astype(str) + "|" + df[DATE_COLUMN].dt.normalize().astype(str)
    )

    year_mask = df[DATE_COLUMN].dt.year == target_year
    if not year_mask.any():
        return pd.DataFrame()

    events = (
        df.loc[year_mask, ["event_key", "event", DATE_COLUMN]]
        .drop_duplicates("event_key")
        .sort_values(DATE_COLUMN)
    )

    feature_cols = predictor.feature_columns
    rows: list[dict] = []

    for _, ev in events.iterrows():
        ev_date = ev[DATE_COLUMN]
        ev_key = ev["event_key"]
        train = df[df[DATE_COLUMN] < ev_date]
        test = df[(df["event_key"] == ev_key) & year_mask]
        if train.empty or test.empty:
            continue

        if interaction_specs:
            train = apply_interaction_specs(train, interaction_specs)
            test = apply_interaction_specs(test, interaction_specs)

        imputer = fit_imputer(train)
        prepared = apply_imputer(test, imputer).dropna(subset=feature_cols)
        if prepared.empty:
            continue

        proba = predictor.model.predict_proba(prepared[feature_cols])[:, 1]
        for i, (_, row) in enumerate(prepared.iterrows()):
            actual = int(row[TARGET_COLUMN])
            p1 = float(proba[i])
            market = market_probs(row)
            record = row.to_dict()
            record["prob_f1_win"] = p1
            record["prob_f2_win"] = 1.0 - p1
            record["predicted_winner"] = row.get("fighter_1") if p1 >= 0.5 else row.get("fighter_2")
            record["correct"] = int((p1 >= 0.5) == bool(actual))
            record["wf_train_rows"] = len(train)
            record["event_name"] = ev["event"]

            if market:
                m1, m2 = market
                edges = compute_edge(p1, 1.0 - p1, m1, m2)
                record["implied_prob_f1"] = m1
                record["implied_prob_f2"] = m2
                record.update(edges)
            else:
                record["implied_prob_f1"] = np.nan
                record["implied_prob_f2"] = np.nan
                record["edge_f1"] = np.nan
                record["edge_f2"] = np.nan
                record["best_edge"] = np.nan
                record["bet_side"] = ""
                record["edge"] = np.nan
            rows.append(record)

    return pd.DataFrame(rows)


def _per_event_breakdown(predictions: pd.DataFrame) -> pd.DataFrame:
    if predictions.empty or "event_name" not in predictions.columns:
        return pd.DataFrame()

    rows: list[dict] = []
    for event, grp in predictions.groupby("event_name"):
        m = evaluate_classification(grp[TARGET_COLUMN], grp["prob_f1_win"])
        odds_n = int(grp["f1_odds"].notna().sum()) if "f1_odds" in grp.columns else 0
        rows.append(
            {
                "event": event,
                "event_date": grp[DATE_COLUMN].iloc[0],
                "fights": len(grp),
                "fights_with_odds": odds_n,
                "accuracy": m["accuracy"],
                "log_loss": m["log_loss"],
                "brier_score": m["brier_score"],
                "avg_best_edge": float(grp["best_edge"].mean()) if "best_edge" in grp else np.nan,
            }
        )
    return pd.DataFrame(rows).sort_values("event_date")


def backtest_2025(
    features: pd.DataFrame | None = None,
    *,
    target_year: int | None = None,
    bankroll_settings: BankrollSettings | None = None,
    edge_thresholds: list[float] | None = None,
    save_outputs: bool = True,
    use_dynamic_thresholds: bool | None = None,
    compare_threshold_modes: bool | None = None,
    profile: str | None = None,
) -> Backtest2025Result:
    ensure_dirs()
    settings = get_settings()
    year = target_year or settings.backtest_year
    bankroll = bankroll_settings or settings.bankroll
    thresholds = edge_thresholds or settings.edge_thresholds

    if not model_exists():
        raise FileNotFoundError("No ufc-predictor model. Run: cd ufc-predictor && python main.py --train")

    if features is None:
        features = load_features()

    if TARGET_COLUMN not in features.columns:
        raise ValueError(f"Features missing '{TARGET_COLUMN}'.")

    predictor = get_predictor()
    predictions = walk_forward_events(features, predictor, target_year=year)

    if predictions.empty:
        raise ValueError(
            f"No fights for {year}. Max date: {features[DATE_COLUMN].max()}. "
            "Refresh ufc-predictor data first."
        )

    overall = evaluate_classification(
        predictions[TARGET_COLUMN], predictions["prob_f1_win"]
    )
    main_mask = predictions.get("is_main_event", pd.Series(0, index=predictions.index)).astype(bool)
    title_mask = predictions.get("is_title_fight", pd.Series(0, index=predictions.index)).astype(bool)
    undercard_mask = ~main_mask & ~title_mask

    segments = {
        "main_events": segment_metrics(predictions, main_mask, TARGET_COLUMN, "prob_f1_win"),
        "title_fights": segment_metrics(predictions, title_mask, TARGET_COLUMN, "prob_f1_win"),
        "undercard": segment_metrics(predictions, undercard_mask, TARGET_COLUMN, "prob_f1_win"),
    }

    fights_with_odds = int(predictions["f1_odds"].notna().sum()) if "f1_odds" in predictions else 0

    active_profile = profile
    if active_profile is None:
        try:
            import config as _cfg

            active_profile = _cfg.UFC_PROFILE
        except ImportError:
            active_profile = "research"

    dynamic_enabled = use_dynamic_thresholds
    if dynamic_enabled is None:
        try:
            import config as _cfg

            dynamic_enabled = _cfg.DYNAMIC_THRESHOLDS_ENABLED
        except ImportError:
            dynamic_enabled = True

    compare_modes = compare_threshold_modes
    if compare_modes is None:
        compare_modes = dynamic_enabled

    if compare_modes:
        bankroll_trades, static_summary = simulate_bankroll_bets(predictions, bankroll)
        dyn_trades, dynamic_summary = simulate_bankroll_bets_dynamic(
            predictions, bankroll, profile=active_profile
        )
        bankroll_summary = dynamic_summary if dynamic_enabled else static_summary
        threshold_comparison = {
            "static": static_summary,
            "dynamic": dynamic_summary,
            "static_trades": len(bankroll_trades),
            "dynamic_trades": len(dyn_trades),
            "profile": active_profile,
        }
        print_threshold_comparison_report(
            static_summary,
            dynamic_summary,
            profile=active_profile,
            target_year=year,
        )
    elif dynamic_enabled:
        bankroll_trades, bankroll_summary = simulate_bankroll_bets_dynamic(
            predictions, bankroll, profile=active_profile
        )
        threshold_comparison = None
    else:
        bankroll_trades, bankroll_summary = simulate_bankroll_bets(predictions, bankroll)
        threshold_comparison = None
    bankroll_sweep = sweep_bankroll_thresholds(predictions, bankroll, thresholds)
    staking_modes = simulate_all_staking_modes(predictions, bankroll)

    monte_carlo = None
    try:
        from src.risk_manager import (
            enrich_staking_modes_with_mc,
            run_monte_carlo,
            save_monte_carlo_report,
        )

        monte_carlo = run_monte_carlo(
            predictions,
            random_seed=42,
            initial_bankroll=bankroll.initial_bankroll,
            min_edge=bankroll.min_edge,
        )
        staking_modes = enrich_staking_modes_with_mc(staking_modes, monte_carlo)
    except Exception as exc:
        logger.warning("Monte Carlo risk analysis skipped: %s", exc)

    calibration_bins = build_calibration_bins(
        predictions[TARGET_COLUMN], predictions["prob_f1_win"]
    )
    per_event = _per_event_breakdown(predictions)

    result = Backtest2025Result(
        predictions=predictions,
        overall_metrics=overall,
        segment_metrics=segments,
        per_event=per_event,
        bankroll_sweep=bankroll_sweep,
        bankroll_trades=bankroll_trades,
        bankroll_summary=bankroll_summary,
        staking_modes=staking_modes,
        calibration_bins=calibration_bins,
        target_year=year,
        fights_with_odds=fights_with_odds,
        report_dir=REPORTS_DIR,
        monte_carlo=monte_carlo,
        threshold_comparison=threshold_comparison,
    )

    if save_outputs:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        PLOTS_DIR.mkdir(parents=True, exist_ok=True)
        predictions.to_csv(BACKTEST_2025_CSV, index=False)
        per_event.to_csv(REPORTS_DIR / f"backtest_{year}_per_event.csv", index=False)
        bankroll_sweep.to_csv(REPORTS_DIR / f"backtest_{year}_bankroll_sweep.csv", index=False)
        bankroll_trades.to_csv(REPORTS_DIR / f"backtest_{year}_bankroll_trades.csv", index=False)
        staking_modes.to_csv(REPORTS_DIR / f"backtest_{year}_staking_modes.csv", index=False)
        calibration_bins.to_csv(REPORTS_DIR / f"backtest_{year}_calibration.csv", index=False)

        summary = {
            "target_year": year,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "fights_with_odds": fights_with_odds,
            "bankroll_rules": bankroll.__dict__,
            **{f"overall_{k}": v for k, v in overall.items()},
            **{f"bankroll_{k}": v for k, v in bankroll_summary.items()},
            "dynamic_thresholds": dynamic_enabled,
            "threshold_comparison": threshold_comparison,
            "staking_modes": staking_modes.to_dict(orient="records"),
        }
        (REPORTS_DIR / f"backtest_{year}_summary.json").write_text(
            json.dumps(summary, indent=2, default=str),
            encoding="utf-8",
        )
        save_calibration_plot(calibration_bins, PLOTS_DIR / f"calibration_{year}.png")
        save_roi_plot(bankroll_sweep, PLOTS_DIR / f"bankroll_roi_{year}.png")
        if monte_carlo is not None:
            save_monte_carlo_report(monte_carlo, REPORTS_DIR, year=year)
        logger.info("Backtest saved -> %s", BACKTEST_2025_CSV)

    return result
