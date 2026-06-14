#!/usr/bin/env python3
"""
UFC Predictor CLI — trading-style pipeline runner.

Usage:
    python main.py
    python main.py --event "UFC 303"
    python main.py --refresh-data --train --odds --output preds.csv
    python main.py --backtest
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.project_paths import bootstrap

_PREDICTOR_ROOT = bootstrap(entry_file=Path(__file__))

import pandas as pd

import config
from src.backtester import backtest_2025, print_backtest_2025_summary, run_holdout_backtest
from src.data_loader import (
    DataLoaderError,
    ensure_data_dirs,
    enrich_fights_with_ufcstats,
    get_upcoming_card,
    list_upcoming_events,
    load_fights,
    load_historical_data,
    load_processed_features,
    save_fights,
)
from src.feature_engineering import build_feature_matrix, save_features
from src.model_freshness import model_needs_retrain, stale_model_warning
from src.model_trainer import load_trained_model, train_model
from src.predictor import OddsAPIError, merge_predictions_with_odds, predict_upcoming_card
from src.alerts import dispatch_alerts, format_alert_text, generate_alerts
from src.logging_utils import log_event, setup_logging as setup_file_logging
from src.preflight import run_preflight
from src.safe_io import install_safe_stdout
from src.scheduler import watch_loop

logger = logging.getLogger("ufc-predictor")

BANNER = """
  +-----------------------------------------------------------+
  |           UFC PREDICTOR  |  QUANT PIPELINE CLI            |
  +-----------------------------------------------------------+
"""


def setup_logging(verbose: bool) -> None:
    setup_file_logging(verbose=verbose, log_dir=config.LOG_DIR)
    logging.getLogger("optuna").setLevel(logging.WARNING)


def _step(n: int, total: int, msg: str) -> None:
    logger.info("[%s/%s] %s", n, total, msg)


def _model_exists() -> bool:
    return config.DEFAULT_MODEL_PATH.is_file() or config.LEGACY_MODEL_PATH.is_file()


def _features_exist() -> bool:
    return config.PROCESSED_FEATURES_CSV.is_file()


def _fights_exist() -> bool:
    return config.RAW_FIGHTS_CSV.is_file()


def _event_label(event: dict[str, str]) -> str:
    name = str(event.get("event_name", "")).strip()
    if name:
        return name
    path = str(event.get("event_path", "")).strip()
    return path.replace("/event/", "").replace("-", " ").title() or "Upcoming event"


def _event_sort_key(event: dict[str, str]) -> str:
    return str(event.get("event_date", "") or event.get("date", "") or "9999-12-31")


def _match_event_index(event_query: str, events: list[dict[str, str]]) -> int | None:
    query = event_query.strip().lower()
    if not query:
        return None
    query_slug = query.replace(" ", "-")
    matches = [
        i
        for i, e in enumerate(events)
        if query in str(e.get("event_name", "")).lower()
        or query in str(e.get("event_path", "")).lower()
        or query_slug in str(e.get("event_path", "")).lower()
    ]
    if not matches:
        return None
    if len(matches) > 1:
        logger.warning("Multiple event matches for %r; using index %s", event_query, matches[0])
    return matches[0]


def resolve_event(event_query: str | None) -> tuple[int, str]:
    """Map --event string to event_index and canonical name."""
    targets = resolve_event_targets(event_query)
    return targets[0]


def resolve_event_targets(
    event_query: str | list[str] | None = None,
    *,
    next_two: bool = False,
    last_two: bool = False,
    include_adjacent_week: bool = True,
) -> list[tuple[int, str]]:
    """
    Resolve one or more upcoming events for analysis.

    - ``next_two``: two soonest upcoming cards, chronological (closest first).
    - Single named card + ``include_adjacent_week``: also include the paired
      adjacent card (indices 0 and 1 on the upcoming slate).
    - Multiple queries: each resolved, deduped, sorted chronologically.
    """
    next_two = next_two or last_two
    events = list_upcoming_events()
    if not events:
        raise SystemExit("No upcoming events found. Check network or try again later.")

    indices: set[int] = set()

    if next_two:
        indices.update(range(min(2, len(events))))
    elif isinstance(event_query, list):
        for q in event_query:
            if not q:
                continue
            idx = _match_event_index(q, events)
            if idx is None:
                raise SystemExit(f"Event not found: {q}")
            indices.add(idx)
    elif event_query:
        idx = _match_event_index(event_query, events)
        if idx is None:
            logger.error("Event not found: %s", event_query)
            logger.info("Use --list-events to see available cards.")
            raise SystemExit(1)
        indices.add(idx)
        if include_adjacent_week and len(events) >= 2 and idx in (0, 1):
            indices.update({0, 1})
    else:
        indices.add(0)
        if include_adjacent_week and len(events) >= 2:
            indices.add(1)

    ordered = sorted(indices, key=lambda i: _event_sort_key(events[i]))
    return [(i, _event_label(events[i])) for i in ordered]


def list_events() -> None:
    events = list_upcoming_events()
    if not events:
        print("No upcoming events available.")
        return
    _print_table(
        ["IDX", "EVENT", "DATE"],
        [
            [str(i), _event_label(e)[:48], str(e.get("event_date", ""))]
            for i, e in enumerate(events)
        ],
        title="UPCOMING EVENTS",
    )


def _print_table(
    headers: list[str],
    rows: list[list[str]],
    *,
    title: str | None = None,
    aligns: list[str] | None = None,
) -> None:
    """ASCII table for CLI output."""
    if not rows:
        print("(no rows)")
        return

    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    align = aligns or ["<"] * len(headers)
    if len(align) < len(headers):
        align = align + ["<"] * (len(headers) - len(align))

    def _fmt_cell(text: str, width: int, spec: str) -> str:
        return f"{text:{spec}{width}}"

    if title:
        print(f"\n  {title}")
        print("  " + "-" * (sum(widths) + 2 * (len(headers) - 1) + 4))

    header_line = "  " + "  ".join(
        _fmt_cell(headers[i], widths[i], align[i]) for i in range(len(headers))
    )
    print(header_line)
    print("  " + "-" * len(header_line.strip()))
    for row in rows:
        print(
            "  "
            + "  ".join(
                _fmt_cell(str(row[i]), widths[i], align[i]) for i in range(len(headers))
            )
        )
    print()


def load_or_refresh_data(*, refresh: bool, enrich_ufcstats: bool = False) -> pd.DataFrame:
    if refresh or not _fights_exist():
        logger.info("Downloading / refreshing historical fight data…")
        fights = load_historical_data(
            force_refresh=refresh,
            enrich_ufcstats=enrich_ufcstats,
        )
        return fights
    logger.info("Loading cached fights from %s", config.RAW_FIGHTS_CSV)
    fights = load_fights()
    if enrich_ufcstats:
        logger.info("Enriching cached fights with ufcstats profiles…")
        fights = enrich_fights_with_ufcstats(fights)
        save_fights(fights)
    return fights


def build_or_load_features(fights: pd.DataFrame, *, refresh: bool) -> pd.DataFrame:
    if refresh or not _features_exist():
        logger.info("Building feature matrix…")
        features = build_feature_matrix(fights)
        path = save_features(features)
        logger.info("Saved %s feature rows -> %s", f"{len(features):,}", path)
        return features
    logger.info("Loading cached features from %s", config.PROCESSED_FEATURES_CSV)
    return load_processed_features()


def load_or_train_model(
    features: pd.DataFrame,
    *,
    train: bool,
    tune: str,
) -> None:
    if train or not _model_exists():
        logger.info("Training ensemble model…")
        result = train_model(features, tune=tune, calibration_method=config.CALIBRATION_METHOD)
        logger.info(
            "Model saved | test AUC %.3f | accuracy %.1f%%",
            result.metrics.get("roc_auc", 0),
            result.metrics.get("accuracy", 0) * 100,
        )
        return
    artifact = load_trained_model()
    metrics = artifact.get("metrics", {})
    logger.info(
        "Loaded model | AUC %.3f | %s rows trained",
        metrics.get("roc_auc", 0),
        int(metrics.get("train_rows", 0)),
    )


def fetch_event_card(event_index: int, *, refresh: bool) -> pd.DataFrame:
    logger.info("Fetching fight card (index %s)…", event_index)
    card = get_upcoming_card(event_index=event_index, force_refresh=refresh)
    if card.empty:
        raise SystemExit("Fight card is empty.")
    logger.info("Card loaded: %s bouts", len(card))
    return card


def generate_predictions(
    card: pd.DataFrame,
    fights: pd.DataFrame,
    *,
    use_odds: bool,
    refresh_odds: bool,
    explain: bool = False,
) -> pd.DataFrame:
    preds = predict_upcoming_card(
        card,
        historical_fights=fights,
        attach_odds=use_odds and bool(config.ODDS_API_KEY),
        force_refresh_odds=refresh_odds,
        explain=explain,
    )
    if use_odds and config.ODDS_API_KEY and "odds_matched" not in preds.columns:
        preds = merge_predictions_with_odds(preds, force_refresh=refresh_odds)
    if preds.empty:
        raise SystemExit(
            "No predictions generated. Fighters may lack enough UFC history "
            f"(min {config.MIN_FIGHTS_PER_FIGHTER} fights)."
        )
    return preds


def _fmt_pct(val, default: str = "-") -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    try:
        v = float(val)
        if v <= 1.0:
            return f"{v * 100:.1f}%"
        return f"{v:.1f}%"
    except (TypeError, ValueError):
        return default


def _fmt_edge(row: pd.Series) -> str:
    if pd.notna(row.get("best_edge")):
        return f"{float(row['best_edge']) * 100:+.1f}%"
    if pd.notna(row.get("edge_pct")):
        return f"{float(row['edge_pct']):+.1f}%"
    if pd.notna(row.get("edge_f1")):
        pick_f1 = row.get("predicted_winner") == row.get("fighter_1")
        edge = float(row["edge_f1"] if pick_f1 else row.get("edge_f2", 0))
        return f"{edge * 100:+.1f}%"
    return "-"


def print_predictions_table(preds: pd.DataFrame, event_name: str) -> None:
    """Render predictions sorted by edge (highest first)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n  EVENT   {event_name or 'Upcoming card'}")
    print(f"  TIME    {now}")
    print(f"  BOUTS   {len(preds)}")
    print()

    rows: list[list[str]] = []
    for _, row in preds.iterrows():
        f1 = str(row.get("fighter_1", ""))
        f2 = str(row.get("fighter_2", ""))
        fight = f"{f1} vs {f2}"
        if len(fight) > 36:
            fight = fight[:33] + "..."
        pick = str(row.get("predicted_winner", ""))
        if len(pick) > 18:
            pick = pick[:15] + "..."

        model_pct = _fmt_pct(row.get("predicted_prob", row.get("prob_f1_win")))
        if row.get("predicted_winner") == f2 and pd.notna(row.get("prob_f2_win")):
            model_pct = _fmt_pct(row["prob_f2_win"])

        impl = "-"
        if pd.notna(row.get("implied_prob_f1")):
            imp = float(
                row["implied_prob_f1"]
                if row.get("predicted_winner") == f1
                else row.get("implied_prob_f2", row["implied_prob_f1"])
            )
            impl = f"{imp * 100:.1f}%"

        edge = _fmt_edge(row)
        conf = str(row.get("confidence_label", "-")).upper()[:6]
        rank = str(int(row.get("edge_rank", 0))) if pd.notna(row.get("edge_rank")) else "-"
        rows.append([rank, fight, pick, model_pct, impl, edge, conf])

    _print_table(
        ["#", "FIGHT", "PICK", "MODEL", "IMPL", "EDGE", "CONF"],
        rows,
        title="PREDICTIONS (ranked by edge)",
        aligns=[">", "<", "<", ">", ">", ">", "<"],
    )

    matched = int(preds.get("odds_matched", pd.Series(dtype=bool)).sum())
    if "odds_matched" in preds.columns and config.ODDS_API_KEY:
        print(f"  Odds matched: {matched}/{len(preds)}")


def print_explanations(preds: pd.DataFrame) -> None:
    """Print SHAP reasoning for each fight (when --explain)."""
    if "reasoning" not in preds.columns:
        print("\n  SHAP explanations not available (re-run with --explain and pip install shap).")
        return

    print()
    print("  WHY THIS PICK? (SHAP — LightGBM drivers)")
    print("  " + "─" * 72)
    for _, row in preds.iterrows():
        f1 = str(row.get("fighter_1", ""))
        f2 = str(row.get("fighter_2", ""))
        pick = str(row.get("predicted_winner", ""))
        prob = _fmt_pct(row.get("predicted_prob", row.get("prob_f1_win")))
        reasoning = str(row.get("reasoning", ""))
        print(f"\n  {f1} vs {f2}")
        print(f"  Pick: {pick} ({prob})")
        print(f"  {reasoning}")
        toward = []
        if pd.notna(row.get("shap_explanation")):
            try:
                import json

                exp = json.loads(row["shap_explanation"])
                toward = exp.get("toward_pick") or exp.get("top_features") or []
            except (json.JSONDecodeError, TypeError):
                pass
        if toward:
            print("  Top drivers:")
            for feat in toward[:5]:
                label = feat.get("label", feat.get("feature", ""))
                shap_val = float(feat.get("shap", 0))
                print(f"    • {label}: {shap_val:+.4f} log-odds impact")


def save_predictions(preds: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    preds.to_csv(path, index=False)
    logger.info("Predictions saved -> %s", path.resolve())


def run_backtest_summary(features: pd.DataFrame) -> None:
    if not _model_exists():
        logger.warning("No model on disk; skipping backtest.")
        return
    logger.info("Running full backtest (hold-out + walk-forward)…")
    result = run_holdout_backtest(
        features,
        save_report=True,
        run_walk_forward=True,
    )
    cls = result.classification
    summ = result.summary
    wf = result.walk_forward_metrics

    print("\n  BACKTEST REPORT")
    print("  " + "=" * 72)

    _print_table(
        ["Metric", "Hold-out", "Walk-forward"],
        [
            ["Accuracy", f"{cls['accuracy']:.1%}", f"{wf.get('accuracy', 0):.1%}"],
            ["Precision", f"{cls.get('precision', 0):.1%}", f"{wf.get('precision', 0):.1%}"],
            ["Recall", f"{cls.get('recall', 0):.1%}", f"{wf.get('recall', 0):.1%}"],
            ["Log loss", f"{cls['log_loss']:.3f}", f"{wf.get('log_loss', 0):.3f}"],
            ["Brier", f"{cls['brier_score']:.3f}", f"{wf.get('brier_score', 0):.3f}"],
            ["ROC AUC", f"{cls.get('roc_auc', float('nan')):.3f}", f"{wf.get('roc_auc', float('nan')):.3f}"],
            ["Fights", str(int(cls.get('n_fights', 0))), str(int(wf.get('n_fights', 0)))],
        ],
        title="CLASSIFICATION",
        aligns=["<", ">", ">"],
    )

    _print_table(
        ["Metric", "Value"],
        [
            ["Min edge", f"{summ.get('min_edge', 0):.0%}"],
            ["Trades", str(int(summ.get("trades", 0)))],
            ["Hit rate", f"{summ.get('hit_rate', 0):.1%}"],
            ["ROI", f"{summ.get('roi_pct', 0):.1f}%"],
            ["Avg yield / bet", f"{summ.get('avg_yield_pct', 0):.1f}%"],
            ["Total PnL", f"${summ.get('total_pnl', 0):.2f}"],
        ],
        title=f"VALUE BETTING (edge >= {config.MIN_EDGE:.0%})",
        aligns=["<", ">"],
    )

    if not result.threshold_sweep.empty:
        sweep_rows = [
            [
                f"{row['min_edge']:.0%}",
                str(int(row.get("trades", 0))),
                f"{row.get('hit_rate', 0):.1%}",
                f"{row.get('roi_pct', 0):.1f}%",
                f"{row.get('avg_yield_pct', 0):.1f}%",
            ]
            for _, row in result.threshold_sweep.iterrows()
        ]
        _print_table(
            ["Min edge", "Trades", "Hit rate", "ROI", "Avg yield"],
            sweep_rows,
            title="THRESHOLD SWEEP",
            aligns=[">", ">", ">", ">", ">"],
        )

    if not result.metrics_by_year.empty:
        year_rows = [
            [
                str(int(row["year"])),
                str(int(row.get("fights", 0))),
                f"{row.get('accuracy', 0):.1%}",
                f"{row.get('log_loss', 0):.3f}",
            ]
            for _, row in result.metrics_by_year.iterrows()
        ]
        _print_table(
            ["Year", "Fights", "Accuracy", "Log loss"],
            year_rows,
            title="METRICS BY YEAR",
        )

    report_dir = result.report_dir or config.BACKTEST_DIR
    print(f"  Report saved -> {report_dir}")
    print(f"    - backtest_summary.csv, walk_forward_predictions.csv")
    print(f"    - threshold_roi.csv, calibration_plot.png, roi_threshold_plot.png")
    print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ufc-predictor",
        description="UFC fight prediction pipeline — load, train, predict.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python main.py
  python main.py --event "UFC 303"
  python main.py --list-events
  python main.py --refresh-data --train --odds --explain -o predictions.csv
  python main.py --refresh-data --predict-upcoming --explain --alerts --discord
  python main.py --watch --alerts --discord --telegram --dry-run
  python main.py --backtest
  python main.py --backtest-2025
  python main.py --refresh-data --train --backtest
  python main.py --refresh-data --enrich-ufcstats --train --backtest-2025
        """,
    )
    parser.add_argument(
        "--event",
        metavar="NAME",
        help='Target event (e.g. "UFC 303"). Default: next upcoming card.',
    )
    parser.add_argument(
        "--list-events",
        action="store_true",
        help="List upcoming UFC events and exit.",
    )
    parser.add_argument(
        "--refresh-data",
        action="store_true",
        help="Force refresh historical data and rebuild features.",
    )
    parser.add_argument(
        "--enrich-ufcstats",
        action="store_true",
        help="Merge ufcstats/Greco fighter profiles and career stats (fixes 2025 sparsity).",
    )
    parser.add_argument(
        "--train",
        action="store_true",
        help="Train / retrain the model before predicting.",
    )
    parser.add_argument(
        "--force-retrain",
        action="store_true",
        help="Always retrain even if feature fingerprint matches saved model.",
    )
    parser.add_argument(
        "--tune",
        choices=["none", "optuna", "grid"],
        default="none",
        help="Hyperparameter tuning when --train is set (default: none).",
    )
    parser.add_argument(
        "--odds",
        action="store_true",
        help="Fetch The Odds API lines and compute edge (requires THE_ODDS_API_KEY).",
    )
    parser.add_argument(
        "--backtest",
        action="store_true",
        help="Run full backtest report (hold-out + walk-forward) and save CSV/plots.",
    )
    parser.add_argument(
        "--backtest-2025",
        action="store_true",
        help="Run 2025 event walk-forward backtest (train up to event N-1, predict event N).",
    )
    parser.add_argument(
        "--dynamic-thresholds",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Adjust min edge / parlay thresholds by bankroll, form, confidence, time (default: on).",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="CSV",
        type=Path,
        help="Save predictions to CSV path.",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Attach SHAP feature explanations for each predicted fight (requires shap).",
    )
    parser.add_argument(
        "--predict-upcoming",
        action="store_true",
        help="Explicit upcoming-card prediction mode (default pipeline; use with --watch).",
    )
    parser.add_argument(
        "--alerts",
        action="store_true",
        help="Generate value-bet alerts (singles + parlays) with SHAP + MC risk summary.",
    )
    parser.add_argument(
        "--discord",
        action="store_true",
        help="Send alerts to DISCORD_WEBHOOK (.env).",
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="Send alerts to Telegram (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Alert dry-run: format messages but do not POST (also skips cooldown).",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help=f"Watch mode: poll every {config.ALERT_POLL_MINUTES}m, alert on new value bets.",
    )
    parser.add_argument(
        "--auto-odds",
        action="store_true",
        help="With --watch: quick BetNow + DraftKings odds refresh (card check + incremental cache).",
    )
    parser.add_argument(
        "--poll-minutes",
        type=int,
        default=None,
        metavar="N",
        help=f"Watch poll interval in minutes (default: {config.ALERT_POLL_MINUTES}).",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Run pre-flight checklist and exit (model, data, webhooks, profile caps).",
    )
    parser.add_argument(
        "--profile",
        choices=["live", "research"],
        default=None,
        help="Risk profile: live (conservative caps) or research (default).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.profile:
        config.UFC_PROFILE = args.profile
    config.apply_profile_overrides()
    config.DYNAMIC_THRESHOLDS_ENABLED = args.dynamic_thresholds
    install_safe_stdout()
    ensure_data_dirs()
    setup_logging(args.verbose)

    if args.preflight:
        return run_preflight(profile=args.profile)

    print(BANNER)

    if args.list_events:
        list_events()
        return 0

    if args.watch:
        if not _model_exists():
            raise SystemExit("No trained model. Run with --train first.")
        dry = args.dry_run or config.ALERT_DRY_RUN
        if not args.odds and not config.ODDS_API_KEY and not args.auto_odds:
            logger.warning("THE_ODDS_API_KEY not set — edge alerts need odds or --auto-odds.")
        watch_loop(
            poll_minutes=args.poll_minutes,
            refresh_data=args.refresh_data,
            use_odds=(args.odds or bool(config.ODDS_API_KEY)) and not args.auto_odds,
            explain=args.explain,
            discord=args.discord,
            telegram=args.telegram,
            dry_run=dry,
            min_edge=config.profile_value("alert_min_edge"),
            auto_odds=args.auto_odds,
        )
        return 0

    if args.backtest_2025:
        refresh_features = args.refresh_data or args.enrich_ufcstats or not _features_exist()
        if refresh_features:
            fights = load_or_refresh_data(
                refresh=args.refresh_data or not _features_exist(),
                enrich_ufcstats=args.enrich_ufcstats,
            )
            features = build_or_load_features(fights, refresh=True)
        else:
            features = load_processed_features()

        needs_train, train_reason = model_needs_retrain(force=args.force_retrain)
        if args.train or needs_train or args.force_retrain:
            if train_reason:
                logger.info("Auto-retrain: %s", train_reason)
            load_or_train_model(features, train=True, tune=args.tune)
        else:
            warn = stale_model_warning()
            if warn:
                logger.warning(warn)
            elif not _model_exists():
                raise SystemExit("No trained model. Run with --train first.")

        try:
            result = backtest_2025(
                features,
                use_dynamic_thresholds=args.dynamic_thresholds,
                compare_threshold_modes=True,
                profile=config.UFC_PROFILE,
            )
            print_backtest_2025_summary(result)
        except ValueError as exc:
            logger.error("%s", exc)
            return 1
        return 0

    # Backtest-only shortcut
    if args.backtest and not args.event and not args.output:
        if not _features_exist():
            fights = load_or_refresh_data(
                refresh=args.refresh_data,
                enrich_ufcstats=args.enrich_ufcstats,
            )
            features = build_or_load_features(
                fights,
                refresh=args.refresh_data,
            )
        else:
            features = load_processed_features()
        if args.train or not _model_exists():
            load_or_train_model(features, train=True, tune=args.tune)
        run_backtest_summary(features)
        return 0

    total_steps = 6
    try:
        _step(1, total_steps, "DATA - load historical fights")
        fights = load_or_refresh_data(
            refresh=args.refresh_data,
            enrich_ufcstats=args.enrich_ufcstats,
        )

        _step(2, total_steps, "FEATURES - build differential matrix")
        features = build_or_load_features(
            fights,
            refresh=args.refresh_data or args.enrich_ufcstats,
        )

        _step(3, total_steps, "MODEL - load or train")
        needs_train, train_reason = model_needs_retrain(force=args.force_retrain)
        if args.train or needs_train or args.force_retrain:
            if train_reason:
                logger.info("Auto-retrain: %s", train_reason)
            load_or_train_model(features, train=True, tune=args.tune)
        else:
            warn = stale_model_warning()
            if warn:
                logger.warning(warn)
            load_or_train_model(features, train=args.train, tune=args.tune)

        _step(4, total_steps, "CARD - resolve event and fetch matchups")
        event_index, event_name = resolve_event(args.event)
        card = fetch_event_card(event_index, refresh=args.refresh_data)

        _step(5, total_steps, "INFERENCE - score fights")
        if not _model_exists():
            raise SystemExit(
                "No trained model on disk. Re-run with --train or train via the dashboard."
            )
        if args.odds and not config.ODDS_API_KEY:
            logger.warning("THE_ODDS_API_KEY not set; predictions will omit market edge.")
        preds = generate_predictions(
            card,
            fights,
            use_odds=args.odds,
            refresh_odds=args.refresh_data,
            explain=args.explain,
        )

        _step(6, total_steps, "OUTPUT")
        print_predictions_table(preds, event_name)
        if args.explain:
            print_explanations(preds)

        if args.alerts or args.discord or args.telegram:
            alert_data = generate_alerts(
                preds,
                min_edge=config.profile_value("alert_min_edge"),
                event_name=event_name,
            )
            print(format_alert_text(alert_data))
            dry = args.dry_run or config.ALERT_DRY_RUN
            if args.discord or args.telegram:
                status = dispatch_alerts(
                    alert_data,
                    discord=args.discord,
                    telegram=args.telegram,
                    dry_run=dry,
                    respect_cooldown=not dry,
                )
                if status.get("skipped"):
                    logger.info("Alert not sent: %s", status.get("skip_reason"))
                elif status.get("sent"):
                    logger.info("Alerts sent (discord=%s telegram=%s)", status["discord"], status["telegram"])
                elif dry:
                    logger.info("Dry-run complete — no webhooks called.")

        if args.output:
            save_predictions(preds, args.output)

        if args.backtest:
            run_backtest_summary(features)

        logger.info("Done.")
        return 0

    except SystemExit:
        raise
    except (DataLoaderError, OddsAPIError, FileNotFoundError, ValueError) as exc:
        logger.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        logger.warning("Interrupted.")
        return 130
    except Exception as exc:
        logger.exception("Unexpected error: %s", exc)
        return 1


def run_cli(
    event_name: str | list[str] | None = None,
    *,
    profile: str | None = None,
    use_odds: bool = True,
    explain: bool = False,
    skip_preflight: bool = False,
    refresh_data: bool = False,
    apply_safety_gates: bool | None = None,
    next_two: bool = False,
    last_two: bool = False,
    include_adjacent_week: bool = True,
    use_dynamic_thresholds: bool | None = None,
) -> dict:
    """
    Weekend card analysis pipeline for ufc-predict.exe / cli_entry.

    Returns dict with predictions, alerts, preflight status, and safety gates.
    """
    if profile:
        config.UFC_PROFILE = profile.strip().lower()
    config.apply_profile_overrides()
    if use_dynamic_thresholds is not None:
        config.DYNAMIC_THRESHOLDS_ENABLED = use_dynamic_thresholds

    if apply_safety_gates is None:
        apply_safety_gates = config.is_live_profile()

    preflight_lines: list[str] = []
    preflight_code = 0
    if not skip_preflight:
        preflight_code = run_preflight(
            profile=config.UFC_PROFILE,
            printer=preflight_lines.append,
        )

    result: dict[str, Any] = {
        "event_name": "",
        "event_names": [],
        "cards": [],
        "predictions": pd.DataFrame(),
        "alerts": {},
        "preflight_code": preflight_code,
        "preflight_lines": preflight_lines,
        "preflight_skipped": skip_preflight,
        "profile": config.UFC_PROFILE,
        "safety_blocked": False,
        "safety_reason": "",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "error": None,
    }

    if preflight_code != 0 and not skip_preflight:
        result["error"] = "Preflight failed — fix issues above or use --skip-preflight."
        return result

    try:
        ensure_data_dirs()
        fights = load_or_refresh_data(refresh=refresh_data)
        next_two = next_two or last_two
        targets = resolve_event_targets(
            event_name,
            next_two=next_two,
            include_adjacent_week=include_adjacent_week and not next_two,
        )
        result["event_names"] = [name for _, name in targets]
        result["event_name"] = " + ".join(result["event_names"])

        if not _model_exists():
            result["error"] = (
                "No trained model on disk. Train via main.py --train or copy models/ beside the EXE."
            )
            return result

        all_preds: list[pd.DataFrame] = []
        card_results: list[dict[str, Any]] = []

        for event_index, resolved_name in targets:
            card = fetch_event_card(event_index, refresh=refresh_data)
            preds = generate_predictions(
                card,
                fights,
                use_odds=use_odds,
                refresh_odds=refresh_data,
                explain=explain,
            )
            if "event_name" not in preds.columns:
                preds["event_name"] = resolved_name
            else:
                preds["event_name"] = preds["event_name"].fillna(resolved_name)

            alert_data = generate_alerts(
                preds,
                event_name=resolved_name,
                use_dynamic_thresholds=use_dynamic_thresholds,
            )
            card_results.append(
                {
                    "event_index": event_index,
                    "event_name": resolved_name,
                    "predictions": preds,
                    "alerts": alert_data,
                }
            )
            all_preds.append(preds)

        combined = pd.concat(all_preds, ignore_index=True) if all_preds else pd.DataFrame()
        result["cards"] = card_results
        result["predictions"] = combined

        combined_alerts = generate_alerts(
            combined,
            event_name=result["event_name"],
            use_dynamic_thresholds=use_dynamic_thresholds,
        )
        combined_alerts["cards"] = card_results
        result["alerts"] = combined_alerts

        if apply_safety_gates:
            from src.circuit_breaker import check_alerts_allowed
            from src.risk_manager import check_bankroll_safety

            bankroll = float(combined_alerts.get("bankroll") or config.INITIAL_BANKROLL)
            allowed, reason = check_alerts_allowed(bankroll)
            if allowed:
                allowed, reason = check_bankroll_safety(bankroll)
            if not allowed:
                result["safety_blocked"] = True
                result["safety_reason"] = reason
                combined_alerts = dict(combined_alerts)
                combined_alerts["warnings"] = list(combined_alerts.get("warnings") or [])
                combined_alerts["warnings"].insert(0, f"Live safety gate: {reason}")
                result["alerts"] = combined_alerts

        return result

    except SystemExit as exc:
        result["error"] = str(exc) if str(exc) else "Analysis aborted."
        return result
    except (DataLoaderError, OddsAPIError, FileNotFoundError, ValueError) as exc:
        result["error"] = str(exc)
        return result
    except Exception as exc:
        logger.exception("run_cli failed: %s", exc)
        result["error"] = str(exc)
        return result


if __name__ == "__main__":
    raise SystemExit(main())
