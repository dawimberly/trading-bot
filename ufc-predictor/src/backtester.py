"""Backtest classification quality, walk-forward CV, and value-betting edge."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.calibration import calibration_curve
from sklearn.metrics import (
    accuracy_score,
    brier_score_loss,
    log_loss,
    precision_score,
    recall_score,
    roc_auc_score,
)

import config
from src.data_loader import ensure_data_dirs
from src.feature_engineering import apply_imputer, fit_imputer
from src.model_trainer import load_trained_model, prepare_time_splits
from src.predictor import FightPredictor

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    predictions: pd.DataFrame
    classification: dict[str, float]
    summary: dict[str, float]
    trades: pd.DataFrame
    threshold_sweep: pd.DataFrame = field(default_factory=pd.DataFrame)
    walk_forward: pd.DataFrame = field(default_factory=pd.DataFrame)
    walk_forward_metrics: dict[str, float] = field(default_factory=dict)
    metrics_by_year: pd.DataFrame = field(default_factory=pd.DataFrame)
    importance_timeline: pd.DataFrame = field(default_factory=pd.DataFrame)
    calibration_bins: pd.DataFrame = field(default_factory=pd.DataFrame)
    report_dir: Path | None = None
    monte_carlo: Any = None
    prop_trades: pd.DataFrame = field(default_factory=pd.DataFrame)
    prop_summary: dict[str, float] = field(default_factory=dict)
    prop_accuracy: dict[str, float] = field(default_factory=dict)
    mixed_parlay_trades: pd.DataFrame = field(default_factory=pd.DataFrame)
    mixed_parlay_summary: dict[str, float] = field(default_factory=dict)


def _odds_to_decimal(price: float) -> float:
    """Convert decimal or American price to decimal payout odds."""
    if not np.isfinite(price):
        return np.nan
    if abs(price) > 100:
        if price >= 100:
            return 1.0 + price / 100.0
        return 1.0 + 100.0 / abs(price)
    return float(price)


def _has_valid_odds(row: pd.Series) -> bool:
    """True when both sides have usable closing prices."""
    if "f1_odds" not in row.index or "f2_odds" not in row.index:
        return False
    o1 = row.get("f1_odds")
    o2 = row.get("f2_odds")
    if pd.isna(o1) or pd.isna(o2):
        return False
    try:
        o1_f = float(o1)
        o2_f = float(o2)
    except (TypeError, ValueError):
        return False
    if abs(o1_f) > 100 or abs(o2_f) > 100:
        return abs(o1_f) >= 100 and abs(o2_f) >= 100
    return o1_f > 1 and o2_f > 1


def _fight_decimal_odds(row: pd.Series) -> tuple[float, float] | None:
    """Decimal payout odds for fighter1/fighter2 when prices exist."""
    if not _has_valid_odds(row):
        return None
    f1 = _odds_to_decimal(float(row["f1_odds"]))
    f2 = _odds_to_decimal(float(row["f2_odds"]))
    if not np.isfinite(f1) or not np.isfinite(f2) or f1 <= 1 or f2 <= 1:
        return None
    return f1, f2


def _decimal_implied_prob(f1_odds: float, f2_odds: float) -> tuple[float, float]:
    """Normalize decimal odds into market-implied probabilities."""
    p1 = 1 / f1_odds if f1_odds > 0 else np.nan
    p2 = 1 / f2_odds if f2_odds > 0 else np.nan
    total = p1 + p2
    if not np.isfinite(total) or total <= 0:
        return np.nan, np.nan
    return p1 / total, p2 / total


def _market_probs(row: pd.Series) -> tuple[float, float] | None:
    """Implied market probabilities only when both closing odds are present."""
    decimal = _fight_decimal_odds(row)
    if decimal is None:
        return None
    p1, p2 = _decimal_implied_prob(*decimal)
    if np.isfinite(p1) and np.isfinite(p2):
        return p1, p2
    return None


def evaluate_classification(
    y_true: pd.Series | np.ndarray,
    proba: pd.Series | np.ndarray,
) -> dict[str, float]:
    """Accuracy, precision, recall, log loss, Brier score, and ROC AUC."""
    y = np.asarray(y_true)
    p = np.asarray(proba)
    mask = np.isfinite(p) & np.isfinite(y)
    if mask.sum() == 0:
        return {
            "accuracy": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "log_loss": float("nan"),
            "brier_score": float("nan"),
            "roc_auc": float("nan"),
            "n_fights": 0.0,
        }

    y = y[mask].astype(int)
    p = p[mask]
    preds = (p >= 0.5).astype(int)
    result = {
        "accuracy": float(accuracy_score(y, preds)),
        "precision": float(precision_score(y, preds, zero_division=0)),
        "recall": float(recall_score(y, preds, zero_division=0)),
        "brier_score": float(brier_score_loss(y, p)),
        "n_fights": float(len(y)),
    }
    if len(np.unique(y)) > 1:
        result["log_loss"] = float(log_loss(y, p))
        result["roc_auc"] = float(roc_auc_score(y, p))
    else:
        result["log_loss"] = float("nan")
        result["roc_auc"] = float("nan")
    return result


def build_calibration_bins(
    y_true: np.ndarray | pd.Series,
    proba: np.ndarray | pd.Series,
    *,
    n_bins: int = 10,
) -> pd.DataFrame:
    """Reliability diagram bins: predicted vs observed frequency."""
    y = np.asarray(y_true, dtype=int)
    p = np.asarray(proba, dtype=float)
    mask = np.isfinite(y) & np.isfinite(p)
    y, p = y[mask], p[mask]
    if len(y) < n_bins:
        n_bins = max(2, len(y) // 2)

    try:
        frac_pos, mean_pred = calibration_curve(y, p, n_bins=n_bins, strategy="quantile")
    except ValueError:
        frac_pos, mean_pred = calibration_curve(y, p, n_bins=max(2, n_bins))

    counts = np.zeros(len(mean_pred), dtype=int)
    edges = np.quantile(p, np.linspace(0, 1, len(mean_pred) + 1))
    for i in range(len(mean_pred)):
        lo = edges[i]
        hi = edges[i + 1] if i + 1 < len(edges) else 1.0
        if i == len(mean_pred) - 1:
            counts[i] = int(((p >= lo) & (p <= hi)).sum())
        else:
            counts[i] = int(((p >= lo) & (p < hi)).sum())

    return pd.DataFrame(
        {
            "bin": range(1, len(mean_pred) + 1),
            "mean_predicted": mean_pred,
            "fraction_positive": frac_pos,
            "count": counts,
            "calibration_gap": mean_pred - frac_pos,
        }
    )


def simulate_value_bets(
    predictions: pd.DataFrame,
    *,
    min_edge: float,
    initial_bankroll: float,
    flat_stake: float,
) -> tuple[pd.DataFrame, dict[str, float]]:
    """Flat-stake value betting when model prob beats implied odds."""
    bankroll = initial_bankroll
    rows: list[dict] = []

    for _, row in predictions.iterrows():
        market = _market_probs(row)
        if market is None:
            continue

        market_p1, market_p2 = market
        model_p1 = float(row["prob_f1_win"])
        model_p2 = float(row["prob_f2_win"])
        actual_f1_win = row.get(config.TARGET_COLUMN, np.nan)
        if pd.isna(actual_f1_win):
            continue

        bet_side = None
        edge = 0.0
        odds = np.nan
        decimal_odds = _fight_decimal_odds(row)
        if decimal_odds is None:
            continue
        dec_f1, dec_f2 = decimal_odds

        if model_p1 - market_p1 >= min_edge:
            bet_side = "f1"
            edge = model_p1 - market_p1
            odds = dec_f1
        elif model_p2 - market_p2 >= min_edge:
            bet_side = "f2"
            edge = model_p2 - market_p2
            odds = dec_f2

        if bet_side is None or not np.isfinite(odds) or odds <= 1:
            continue

        won = (bet_side == "f1" and int(actual_f1_win) == 1) or (
            bet_side == "f2" and int(actual_f1_win) == 0
        )
        pnl = flat_stake * (odds - 1) if won else -flat_stake
        bankroll += pnl
        yield_pct = pnl / flat_stake * 100.0

        rows.append(
            {
                config.FIGHT_ID_COLUMN: row.get(config.FIGHT_ID_COLUMN),
                config.DATE_COLUMN: row.get(config.DATE_COLUMN),
                "fighter_1": row.get("fighter_1", row.get("fighter1")),
                "fighter_2": row.get("fighter_2", row.get("fighter2")),
                "min_edge": min_edge,
                "bet_side": bet_side,
                "edge": edge,
                "edge_pct": edge * 100.0,
                "stake": flat_stake,
                "odds": odds,
                "won": int(won),
                "pnl": pnl,
                "yield_pct": yield_pct,
                "equity": bankroll,
            }
        )

    trades = pd.DataFrame(rows)
    if trades.empty:
        summary = {
            "min_edge": min_edge,
            "trades": 0.0,
            "hit_rate": 0.0,
            "total_pnl": 0.0,
            "final_equity": initial_bankroll,
            "roi_pct": 0.0,
            "avg_yield_pct": 0.0,
        }
        return trades, summary

    summary = {
        "min_edge": min_edge,
        "trades": float(len(trades)),
        "hit_rate": float(trades["won"].mean()),
        "total_pnl": float(trades["pnl"].sum()),
        "final_equity": float(trades["equity"].iloc[-1]),
        "roi_pct": float((trades["equity"].iloc[-1] - initial_bankroll) / initial_bankroll * 100),
        "avg_yield_pct": float(trades["yield_pct"].mean()),
    }
    return trades, summary


def sweep_edge_thresholds(
    predictions: pd.DataFrame,
    *,
    thresholds: list[float] | None = None,
    initial_bankroll: float | None = None,
    flat_stake: float | None = None,
) -> pd.DataFrame:
    """ROI / yield / hit-rate across multiple minimum-edge thresholds."""
    edges = thresholds if thresholds is not None else config.EDGE_THRESHOLDS
    bankroll = initial_bankroll if initial_bankroll is not None else config.INITIAL_BANKROLL
    stake = flat_stake if flat_stake is not None else config.FLAT_STAKE

    rows: list[dict[str, float]] = []
    for edge in edges:
        _, summary = simulate_value_bets(
            predictions,
            min_edge=edge,
            initial_bankroll=bankroll,
            flat_stake=stake,
        )
        rows.append(summary)
    return pd.DataFrame(rows)


def _sort_chronologically(df: pd.DataFrame) -> pd.DataFrame:
    if config.DATE_COLUMN in df.columns:
        return df.sort_values(config.DATE_COLUMN).reset_index(drop=True)
    return df.reset_index(drop=True)


def walk_forward_predict(
    features: pd.DataFrame,
    predictor: FightPredictor,
    *,
    min_train_rows: int | None = None,
) -> pd.DataFrame:
    """
    Expanding-window walk-forward: train imputer on fights [0..N), predict fight N.

    Pipeline (leakage-safe):
    - **Imputer**: refit on past-only train_slice each step (simulates production)
    - **Model**: frozen weights from saved artifact (same as inference)
    - Hold-out backtest uses artifact imputer via ``FightPredictor`` instead
    """
    df = _sort_chronologically(features.dropna(subset=[config.TARGET_COLUMN]))
    feature_cols = predictor.feature_columns
    n = len(df)
    min_train = min_train_rows or max(50, int(n * config.WF_MIN_TRAIN_RATIO))
    if min_train >= n - 1:
        raise ValueError(
            f"Not enough rows ({n}) for walk-forward with min_train={min_train}."
        )

    rows: list[dict] = []
    for t in range(min_train, n):
        train_slice = df.iloc[:t]
        test_row = df.iloc[t : t + 1]
        imputer = fit_imputer(train_slice)
        prepared = apply_imputer(test_row, imputer).dropna(subset=feature_cols)
        if prepared.empty:
            continue

        proba = predictor.model.predict_proba(prepared[feature_cols])[:, 1]
        record = prepared.iloc[0].to_dict()
        record["prob_f1_win"] = float(proba[0])
        record["prob_f2_win"] = float(1.0 - proba[0])
        record["wf_train_rows"] = t
        record["wf_index"] = t
        rows.append(record)

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def metrics_by_year(predictions: pd.DataFrame) -> pd.DataFrame:
    """Rolling classification metrics grouped by calendar year."""
    if config.DATE_COLUMN not in predictions.columns:
        return pd.DataFrame()

    work = predictions.copy()
    work[config.DATE_COLUMN] = pd.to_datetime(work[config.DATE_COLUMN], errors="coerce")
    work = work.dropna(subset=[config.DATE_COLUMN, config.TARGET_COLUMN, "prob_f1_win"])
    if work.empty:
        return pd.DataFrame()

    work["year"] = work[config.DATE_COLUMN].dt.year
    rows: list[dict] = []
    for year, grp in work.groupby("year"):
        m = evaluate_classification(grp[config.TARGET_COLUMN], grp["prob_f1_win"])
        m["year"] = int(year)
        m["fights"] = len(grp)
        rows.append(m)
    return pd.DataFrame(rows).sort_values("year")


def importance_over_time(
    features: pd.DataFrame,
    feature_columns: list[str],
    *,
    min_train_rows: int | None = None,
    interval: int | None = None,
) -> pd.DataFrame:
    """
    Snapshot top feature importances at expanding-window checkpoints.

    Trains a lightweight LightGBM on past-only data at each checkpoint.
    """
    df = _sort_chronologically(features.dropna(subset=[config.TARGET_COLUMN]))
    n = len(df)
    min_train = min_train_rows or max(50, int(n * config.WF_MIN_TRAIN_RATIO))
    step = interval or config.WF_IMPORTANCE_INTERVAL
    checkpoints = list(range(min_train, n, step))
    if not checkpoints or checkpoints[-1] != n - 1:
        checkpoints.append(n - 1)

    rows: list[dict] = []
    for t in checkpoints:
        train_slice = df.iloc[:t]
        imputer = fit_imputer(train_slice)
        train_imp = apply_imputer(train_slice, imputer).dropna(subset=feature_columns)
        if len(train_imp) < 30:
            continue

        model = LGBMClassifier(
            n_estimators=120,
            num_leaves=31,
            learning_rate=0.08,
            verbose=-1,
            random_state=config.RANDOM_STATE,
        )
        model.fit(train_imp[feature_columns], train_imp[config.TARGET_COLUMN])
        values = model.feature_importances_
        ranked = sorted(zip(feature_columns, values), key=lambda kv: kv[1], reverse=True)[:5]
        fight_date = train_slice[config.DATE_COLUMN].iloc[-1] if config.DATE_COLUMN in train_slice else None
        row: dict = {
            "checkpoint_index": t,
            "train_rows": len(train_imp),
            "period_end": fight_date,
        }
        for i, (feat, imp) in enumerate(ranked, start=1):
            row[f"top_feature_{i}"] = feat
            row[f"importance_{i}"] = float(imp)
        rows.append(row)

    return pd.DataFrame(rows)


def _save_calibration_plot(calibration_bins: pd.DataFrame, path: Path) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        logger.warning("matplotlib not installed; skipping calibration plot.")
        return

    if calibration_bins.empty:
        return

    fig, ax = plt.subplots(figsize=(6, 5))
    ax.plot([0, 1], [0, 1], "k--", label="Perfect calibration", linewidth=1)
    ax.plot(
        calibration_bins["mean_predicted"],
        calibration_bins["fraction_positive"],
        "o-",
        label="Model",
        linewidth=2,
    )
    ax.set_xlabel("Mean predicted probability")
    ax.set_ylabel("Fraction of positives")
    ax.set_title("Reliability diagram")
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)


def _save_roi_plot(threshold_sweep: pd.DataFrame, path: Path) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        logger.warning("matplotlib not installed; skipping ROI plot.")
        return

    if threshold_sweep.empty:
        return

    fig, ax1 = plt.subplots(figsize=(7, 4))
    x = threshold_sweep["min_edge"] * 100
    ax1.plot(x, threshold_sweep["roi_pct"], "o-", color="tab:blue", label="ROI %")
    ax1.set_xlabel("Min edge threshold (%)")
    ax1.set_ylabel("ROI %", color="tab:blue")
    ax1.tick_params(axis="y", labelcolor="tab:blue")
    ax1.grid(alpha=0.3)

    if "trades" in threshold_sweep.columns:
        ax2 = ax1.twinx()
        ax2.bar(x, threshold_sweep["trades"], alpha=0.2, color="tab:gray", width=0.8)
        ax2.set_ylabel("Trades", color="tab:gray")
        ax2.tick_params(axis="y", labelcolor="tab:gray")

    ax1.set_title("Hypothetical yield by edge threshold")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)


def save_backtest_report(result: BacktestResult, *, report_dir: Path | None = None) -> Path:
    """Persist CSV reports and plots from a backtest run."""
    ensure_data_dirs()
    out = Path(report_dir) if report_dir else config.BACKTEST_DIR
    out.mkdir(parents=True, exist_ok=True)

    summary_rows = [
        {"metric": k, "value": v}
        for k, v in {**result.classification, **result.summary}.items()
    ]
    if result.prop_summary:
        for k, v in result.prop_summary.items():
            summary_rows.append({"metric": f"prop_{k}", "value": v})
    if result.prop_accuracy:
        for k, v in result.prop_accuracy.items():
            summary_rows.append({"metric": f"prop_acc_{k}", "value": v})
    if result.mixed_parlay_summary:
        for k, v in result.mixed_parlay_summary.items():
            summary_rows.append({"metric": f"mixed_parlay_{k}", "value": v})
    if result.walk_forward_metrics:
        for k, v in result.walk_forward_metrics.items():
            summary_rows.append({"metric": f"wf_{k}", "value": v})

    pd.DataFrame(summary_rows).to_csv(out / "backtest_summary.csv", index=False)
    result.predictions.to_csv(out / "holdout_predictions.csv", index=False)
    if not result.walk_forward.empty:
        result.walk_forward.to_csv(out / "walk_forward_predictions.csv", index=False)
    if not result.threshold_sweep.empty:
        result.threshold_sweep.to_csv(out / "threshold_roi.csv", index=False)
    if not result.importance_timeline.empty:
        result.importance_timeline.to_csv(out / "importance_timeline.csv", index=False)
    if not result.metrics_by_year.empty:
        result.metrics_by_year.to_csv(out / "metrics_by_year.csv", index=False)
    if not result.calibration_bins.empty:
        result.calibration_bins.to_csv(out / "calibration_bins.csv", index=False)
    if not result.prop_trades.empty:
        result.prop_trades.to_csv(out / "prop_trades.csv", index=False)
    if not result.mixed_parlay_trades.empty:
        result.mixed_parlay_trades.to_csv(out / "mixed_parlay_trades.csv", index=False)

    _save_calibration_plot(result.calibration_bins, out / "calibration_plot.png")
    _save_roi_plot(result.threshold_sweep, out / "roi_threshold_plot.png")

    if result.monte_carlo is not None:
        from src.risk_manager import save_monte_carlo_report

        save_monte_carlo_report(result.monte_carlo, out)

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report_dir": str(out),
        "holdout_fights": int(result.classification.get("n_fights", 0)),
        "walk_forward_fights": int(len(result.walk_forward)),
    }
    (out / "report_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    result.report_dir = out
    logger.info("Backtest report saved -> %s", out)
    return out


def load_backtest_summary(report_dir: Path | None = None) -> dict | None:
    """Load saved backtest summary for dashboard display."""
    base = Path(report_dir) if report_dir else config.BACKTEST_DIR
    summary_path = base / "backtest_summary.csv"
    if not summary_path.is_file():
        return None
    df = pd.read_csv(summary_path)
    return {row["metric"]: row["value"] for _, row in df.iterrows()}


def run_holdout_backtest(
    features: pd.DataFrame,
    *,
    model_path: str | Path | None = None,
    test_size: float | None = None,
    calibration_size: float | None = None,
    initial_bankroll: float | None = None,
    flat_stake: float | None = None,
    min_edge: float | None = None,
    edge_thresholds: list[float] | None = None,
    save_report: bool = True,
    run_walk_forward: bool = True,
) -> BacktestResult:
    """Evaluate on chronological hold-out + optional walk-forward CV."""
    splits = prepare_time_splits(
        features,
        test_size=test_size,
        calibration_size=calibration_size,
    )
    return run_backtest(
        splits.test,
        model_path=model_path,
        initial_bankroll=initial_bankroll,
        flat_stake=flat_stake,
        min_edge=min_edge,
        edge_thresholds=edge_thresholds,
        holdout_only=False,
        full_features=features,
        save_report=save_report,
        run_walk_forward=run_walk_forward,
    )


def run_backtest(
    features: pd.DataFrame,
    *,
    model_path: str | Path | None = None,
    initial_bankroll: float | None = None,
    flat_stake: float | None = None,
    min_edge: float | None = None,
    edge_thresholds: list[float] | None = None,
    holdout_only: bool = True,
    full_features: pd.DataFrame | None = None,
    save_report: bool = False,
    run_walk_forward: bool = True,
) -> BacktestResult:
    """
    Backtest predictions and value betting on provided features.

    When ``holdout_only=True``, restricts to the chronological test split.
    Uses saved model + per-fold imputer in walk-forward mode (no leakage).
    """
    bankroll = initial_bankroll if initial_bankroll is not None else config.INITIAL_BANKROLL
    stake = flat_stake if flat_stake is not None else config.FLAT_STAKE
    edge_threshold = min_edge if min_edge is not None else config.MIN_EDGE

    df = features.copy()
    feature_pool = full_features if full_features is not None else df
    if holdout_only:
        splits = prepare_time_splits(feature_pool)
        df = splits.test

    predictor = FightPredictor(model_path)
    predictions = predictor.predict_batch(df, apply_style_bonus=False)

    y_true = predictions.get(config.TARGET_COLUMN)
    if y_true is None:
        raise ValueError(f"Features must include target column '{config.TARGET_COLUMN}'.")

    classification = evaluate_classification(y_true, predictions["prob_f1_win"])
    calibration_bins = build_calibration_bins(y_true, predictions["prob_f1_win"])
    metrics_year = metrics_by_year(predictions)
    threshold_sweep = sweep_edge_thresholds(
        predictions,
        thresholds=edge_thresholds,
        initial_bankroll=bankroll,
        flat_stake=stake,
    )
    trades, summary = simulate_value_bets(
        predictions,
        min_edge=edge_threshold,
        initial_bankroll=bankroll,
        flat_stake=stake,
    )

    prop_trades = pd.DataFrame()
    prop_summary: dict[str, float] = {}
    prop_accuracy: dict[str, float] = {}
    mixed_parlay_trades = pd.DataFrame()
    mixed_parlay_summary: dict[str, float] = {}
    if config.ENABLE_PROPS:
        from src.props import (
            evaluate_prop_accuracy,
            simulate_mixed_parlays,
            simulate_prop_bets,
        )

        prop_trades, prop_summary = simulate_prop_bets(
            predictions,
            min_edge=config.PROP_MIN_EDGE,
            initial_bankroll=bankroll,
            flat_stake=stake,
        )
        prop_accuracy = evaluate_prop_accuracy(predictions)
        mixed_parlay_trades, mixed_parlay_summary = simulate_mixed_parlays(
            predictions,
            book="DraftKings",
            initial_bankroll=bankroll,
            flat_stake=stake,
        )

    wf_df = pd.DataFrame()
    wf_metrics: dict[str, float] = {}
    importance_tl = pd.DataFrame()
    if run_walk_forward:
        try:
            wf_df = walk_forward_predict(feature_pool, predictor)
            if not wf_df.empty and config.TARGET_COLUMN in wf_df.columns:
                wf_metrics = evaluate_classification(
                    wf_df[config.TARGET_COLUMN], wf_df["prob_f1_win"]
                )
            importance_tl = importance_over_time(
                feature_pool,
                predictor.feature_columns,
            )
        except (ValueError, KeyError) as exc:
            logger.warning("Walk-forward backtest skipped: %s", exc)

    monte_carlo = None
    try:
        from src.risk_manager import run_monte_carlo, save_monte_carlo_report

        monte_carlo = run_monte_carlo(
            predictions,
            random_seed=42,
            initial_bankroll=bankroll,
            min_edge=edge_threshold,
            n_simulations=min(config.MC_SIMULATIONS, 5000),
        )
    except Exception as exc:
        logger.warning("Monte Carlo skipped: %s", exc)

    result = BacktestResult(
        predictions=predictions,
        classification=classification,
        summary=summary,
        trades=trades,
        threshold_sweep=threshold_sweep,
        walk_forward=wf_df,
        walk_forward_metrics=wf_metrics,
        metrics_by_year=metrics_year,
        importance_timeline=importance_tl,
        calibration_bins=calibration_bins,
        monte_carlo=monte_carlo,
        prop_trades=prop_trades,
        prop_summary=prop_summary,
        prop_accuracy=prop_accuracy,
        mixed_parlay_trades=mixed_parlay_trades,
        mixed_parlay_summary=mixed_parlay_summary,
    )

    if save_report:
        save_backtest_report(result)

    return result


def diagnose_2025_predictions(
    predictions: pd.DataFrame | None = None,
    *,
    target_year: int = 2025,
    sample_size: int = 20,
    rerun_if_missing: bool = True,
) -> pd.DataFrame:
    """Debug low backtest accuracy: label alignment, side bias, feature shifts, baselines."""
    target_col = config.TARGET_COLUMN
    diff_features = [
        "age_diff",
        "reach_diff",
        "striking_acc_diff",
        "elo_diff",
        "win_rate_diff",
    ]

    if predictions is None:
        csv_candidates = [
            config.BACKTEST_2025_CSV,
            config.DATA_DIR / "reports" / "backtest_2025_results.csv",
            Path(__file__).resolve().parents[1] / "data" / "backtest_2025_results.csv",
        ]
        for path in csv_candidates:
            if path.is_file():
                predictions = pd.read_csv(path)
                print(f"Loaded predictions: {path} ({len(predictions)} rows)")
                break

    if predictions is None or predictions.empty:
        if not rerun_if_missing:
            raise FileNotFoundError("No backtest_2025_results.csv found.")
        print("No saved CSV — re-running walk-forward backtest (save_outputs=False)...")
        result = backtest_2025(save_outputs=False, target_year=target_year)
        predictions = result.predictions

    if predictions.empty:
        raise ValueError(f"No {target_year} prediction rows to diagnose.")

    df = predictions.copy()
    if target_col not in df.columns:
        raise ValueError(f"Missing target column '{target_col}'.")

    prob_col = "prob_f1_win" if "prob_f1_win" in df.columns else None
    if prob_col is None:
        raise ValueError("Missing prob_f1_win column.")

    f1_col = "fighter_1" if "fighter_1" in df.columns else "fighter1"
    f2_col = "fighter_2" if "fighter_2" in df.columns else "fighter2"
    event_col = "event_name" if "event_name" in df.columns else "event"

    df["_winner_is_f1"] = (
        df["winner"].astype(str).str.strip().str.lower()
        == df[f1_col].astype(str).str.strip().str.lower()
    ).astype(int)
    df["_label_mismatch"] = df["_winner_is_f1"] != df[target_col].astype(int)

    y = df[target_col].astype(int)
    p = df[prob_col].astype(float)
    pred_f1 = (p >= 0.5).astype(int)
    flipped_pred = 1 - pred_f1

    print("\n=== 2025 prediction diagnosis ===")
    print(f"Fights: {len(df)}")
    print(f"Model accuracy (pick F1 if prob>=0.5): {(pred_f1 == y).mean():.1%}")
    print(f"Flipped accuracy (pick F2 when model picks F1): {(flipped_pred == y).mean():.1%}")
    print(f"Always pick fighter_1 baseline: {y.mean():.1%}")
    print(f"Always pick fighter_2 baseline: {(1 - y).mean():.1%}")
    print(f"Model picks fighter_1: {pred_f1.mean():.1%} of fights")
    print(f"Actual fighter_1 win rate: {y.mean():.1%}")
    print(f"Label mismatches (winner name vs {target_col}): {df['_label_mismatch'].sum()}")

    if "f1_odds" in df.columns and "f2_odds" in df.columns:
        try:
            from ufc_betting_bot.modules.edge import market_probs
        except ImportError:
            market_probs = None  # type: ignore[assignment]
        if market_probs is not None:
            fav_f1 = []
            for _, row in df.iterrows():
                market = market_probs(row)
                if market is None:
                    fav_f1.append(np.nan)
                else:
                    fav_f1.append(int(market[0] >= market[1]))
            fav = pd.Series(fav_f1, index=df.index)
            odds_mask = fav.notna()
            if odds_mask.any():
                print(
                    f"Favorite-by-odds baseline (n={int(odds_mask.sum())}): "
                    f"{(fav[odds_mask].astype(int) == y[odds_mask]).mean():.1%}"
                )

    if "f1_elo" in df.columns and "f2_elo" in df.columns:
        higher_elo_f1 = (df["f1_elo"] >= df["f2_elo"]).astype(int)
        print(f"Higher-Elo fighter_1 baseline: {(higher_elo_f1 == y).mean():.1%}")

    unique_probs = p.round(6).nunique()
    print(f"Unique model probabilities (6dp): {unique_probs}")
    if unique_probs <= 5:
        print("WARNING: model outputs nearly constant probabilities — check features/imputer.")

    print(f"\n=== 2025 Prediction Samples (first {sample_size} fights) ===")
    show_cols = [
        event_col,
        f1_col,
        f2_col,
        prob_col,
        target_col,
        "winner",
        "predicted_winner",
        "correct",
    ]
    show_cols = [c for c in show_cols if c in df.columns]
    for i, (_, row) in enumerate(df.head(sample_size).iterrows()):
        actual_winner = row.get("winner", "?")
        correct = int(row.get("correct", (p.loc[row.name] >= 0.5) == bool(y.loc[row.name])))
        print(
            f"{i + 1:2d}. {row.get(event_col, '?')} | "
            f"{row[f1_col]} vs {row[f2_col]} | "
            f"P(F1)={float(row[prob_col]):.3f} | "
            f"Actual={actual_winner} | "
            f"{target_col}={int(row[target_col])} | "
            f"Pred={row.get('predicted_winner', '?')} | "
            f"Correct={'Y' if correct else 'N'}"
        )
        if row["_label_mismatch"]:
            print("     !! label mismatch: winner name != f1_win")

    present_diffs = [c for c in diff_features if c in df.columns]
    if present_diffs and "correct" in df.columns:
        print("\n=== Feature diff means (correct vs incorrect) ===")
        ok = df[df["correct"].astype(bool)]
        bad = df[~df["correct"].astype(bool)]
        for col in present_diffs:
            print(
                f"  {col:22s}  correct={ok[col].mean():+8.3f}  "
                f"incorrect={bad[col].mean():+8.3f}  delta={ok[col].mean() - bad[col].mean():+8.3f}"
            )

    wrong = df[pred_f1 != y]
    if len(wrong):
        low_prob_for_actual_winner = (
            wrong.apply(
                lambda r: float(r[prob_col]) if int(r[target_col]) == 1 else 1.0 - float(r[prob_col]),
                axis=1,
            ).mean()
        )
        print(
            f"\nWrong picks: model mean prob assigned to ACTUAL winner = {low_prob_for_actual_winner:.3f}"
        )
        if low_prob_for_actual_winner < 0.45:
            print("Pattern: model systematically assigns low probability to the real winner.")

    return df


# ---------------------------------------------------------------------------
# 2025 backtest — see ufc_betting_bot/backtester/backtest_2025.py
# ---------------------------------------------------------------------------


def _betting_bot_root() -> None:
    import sys
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def backtest_2025(*args, **kwargs):
    _betting_bot_root()
    from ufc_betting_bot.backtester.backtest_2025 import backtest_2025 as _bt
    return _bt(*args, **kwargs)


def print_backtest_2025_summary(result):
    _betting_bot_root()
    from ufc_betting_bot.backtester.reports import print_backtest_summary
    return print_backtest_summary(result)


def diagnose_2025_predictions(*args, **kwargs):
    _betting_bot_root()
    from ufc_betting_bot.debug_2025 import diagnose_2025_predictions as _diag
    return _diag(*args, **kwargs)


def __getattr__(name: str):
    if name == "Backtest2025Result":
        _betting_bot_root()
        from ufc_betting_bot.backtester.backtest_2025 import Backtest2025Result as cls
        globals()["Backtest2025Result"] = cls
        return cls
    if name in {"model_needs_retrain", "stale_model_warning"}:
        from src.model_freshness import model_needs_retrain as mnr, stale_model_warning as smw
        globals()["model_needs_retrain"] = mnr
        globals()["stale_model_warning"] = smw
        return mnr if name == "model_needs_retrain" else smw
    raise AttributeError(name)



def _model_exists_check() -> bool:
    return config.DEFAULT_MODEL_PATH.is_file() or config.LEGACY_MODEL_PATH.is_file()
