"""Dashboard analysis: full refresh, quick odds, watch helpers."""

from __future__ import annotations

import importlib
import logging
from datetime import datetime, timezone
from typing import Any, Callable

import pandas as pd

import config
from src.alerts import generate_alerts
from src.card_cache import card_fingerprint, load_event_cache, predict_card_cached
from src.parlay_builder import threshold_context_for_alerts
from src.predictor import merge_predictions_with_odds

logger = logging.getLogger(__name__)


def _reload_config_flags() -> None:
    """Re-read .env and refresh ENABLE_PROPS / related flags before analysis."""
    try:
        from src.project_paths import reload_runtime_env

        reload_runtime_env()
    except Exception as exc:
        logger.warning("Config reload failed: %s", exc)
    logger.info("ENABLE_PROPS loaded as: %s", config.ENABLE_PROPS)


ProgressFn = Callable[[str, float | None], None]

_ODDS_OVERVIEW_COLS = (
    "f1_odds",
    "f2_odds",
    "edge_f1",
    "edge_f2",
    "edge_pct",
    "implied_prob_f1",
    "implied_prob_f2",
    "odds_matched",
)


def _pick_best_odds_overview(
    base: pd.DataFrame,
    merged_by_book: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """Vectorized best-edge overview across book prediction frames."""
    if base.empty or not merged_by_book:
        return base
    frames: list[pd.DataFrame] = []
    for mdf in merged_by_book.values():
        if mdf is None or mdf.empty:
            continue
        if "fighter_1" not in mdf.columns or "fighter_2" not in mdf.columns:
            continue
        keep = [c for c in ("fighter_1", "fighter_2", *_ODDS_OVERVIEW_COLS) if c in mdf.columns]
        frames.append(mdf[keep].copy())
    if not frames:
        return base
    stacked = pd.concat(frames, ignore_index=True)
    if "edge_pct" not in stacked.columns:
        return base
    stacked["_edge_sort"] = pd.to_numeric(stacked["edge_pct"], errors="coerce").fillna(-1e9)
    best = (
        stacked.sort_values("_edge_sort", ascending=False)
        .drop_duplicates(subset=["fighter_1", "fighter_2"], keep="first")
        .drop(columns=["_edge_sort"])
    )
    cols_drop = [c for c in _ODDS_OVERVIEW_COLS if c in base.columns]
    overview = base.drop(columns=cols_drop, errors="ignore")
    return overview.merge(best, on=["fighter_1", "fighter_2"], how="left", suffixes=("", "_book"))

BOOK_LOADERS = {
    "BetNow.eu": ("src.odds_providers.betnow_scraper", "fetch_betnow_odds"),
    "DraftKings": ("src.odds_providers.draftkings", "fetch_draftkings_odds"),
    "MyBookie": ("src.odds_providers.mybookie_scraper", "fetch_mybookie_odds"),
    "Consensus": ("src.predictor", "fetch_ufc_odds"),
}


def active_book_loaders() -> dict[str, tuple[str, str]]:
    """Book loaders respecting MYBOOKIE_ENABLED."""
    loaders = dict(BOOK_LOADERS)
    if not config.MYBOOKIE_ENABLED:
        loaders.pop("MyBookie", None)
    return loaders


def _log(progress: ProgressFn | None, msg: str, pct: float | None = None) -> None:
    if progress:
        progress(msg, pct)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _build_props_payload(
    predictions: pd.DataFrame,
    book_name: str,
    *,
    force_refresh_odds: bool,
    budget_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rank prop singles/parlays from predictions; live odds when available, else synthetic."""
    from src.odds_providers.prop_odds_common import prop_odds_summary
    from src.props import enrich_predictions_with_props, fetch_live_prop_odds, rank_prop_parlays_for_card, rank_prop_singles
    from src.strategy import bankroll_from_budget, strategy_from_profile

    if not config.ENABLE_PROPS:
        logger.debug("Props payload skipped — ENABLE_PROPS is False")
        return {}

    if predictions.empty:
        return {
            "singles": [],
            "singles_meta": {"total_found": 0, "strict_count": 0, "relaxed_count": 0},
            "parlays": [],
            "rules": config.BOOK_PROP_RULES.get(book_name, {}),
            "live_prop_lines": {"live": 0, "synthetic": 0},
            "prop_odds_rows": 0,
        }

    prop_odds = fetch_live_prop_odds(book_name, force_refresh=force_refresh_odds)
    merged = enrich_predictions_with_props(predictions, book=book_name, prop_odds=prop_odds)
    bankroll = bankroll_from_budget(budget_state)
    strategy = strategy_from_profile(bankroll=bankroll)
    singles, singles_meta = rank_prop_singles(
        merged,
        book=book_name,
        strategy=strategy,
        prop_odds=prop_odds,
        max_results=config.PROP_MAX_RESULTS,
        include_relaxed=True,
    )
    return {
        "singles": singles,
        "singles_meta": singles_meta,
        "parlays": (
            rank_prop_parlays_for_card(
                merged,
                book=book_name,
                strategy=strategy,
                prop_odds=prop_odds,
            )
            if config.BOOK_PROP_RULES.get(book_name, {}).get("allow_prop_parlays")
            else []
        ),
        "rules": config.BOOK_PROP_RULES.get(book_name, {}),
        "live_prop_lines": prop_odds_summary(prop_odds),
        "prop_odds_rows": len(prop_odds),
    }


def _load_book_odds(
    book_name: str,
    mod_path: str,
    fn_name: str,
    combined: pd.DataFrame,
    *,
    force_refresh_odds: bool,
    event_label: str,
    bankroll: float | None = None,
    budget_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load one book; BetNow falls back to The Odds API on scraper failure."""
    warning = ""
    source = book_name
    try:
        mod = importlib.import_module(mod_path)
        odds_df = getattr(mod, fn_name)(force_refresh=force_refresh_odds)
        merged = merge_predictions_with_odds(combined.copy(), odds_df, fetch_if_missing=False)
        matched = int(merged.get("odds_matched", pd.Series(False)).sum())
        if book_name == "BetNow.eu" and matched == 0:
            raise ValueError("BetNow scraper returned no matched fights")
    except Exception as exc:
        logger.warning("%s odds failed: %s", book_name, exc)
        props_payload = (
            _build_props_payload(
                combined,
                book_name,
                force_refresh_odds=force_refresh_odds,
                budget_state=budget_state,
            )
            if config.ENABLE_PROPS
            else {}
        )
        if book_name != "BetNow.eu":
            return {
                "predictions": combined.copy(),
                "alerts": {},
                "odds_matched": 0,
                "odds_total": len(combined),
                "error": str(exc),
                "warning": f"{book_name} unavailable: {exc}",
                "props": props_payload,
            }
        # BetNow → The Odds API consensus fallback
        try:
            from src.predictor import fetch_ufc_odds

            odds_df = fetch_ufc_odds(force_refresh=force_refresh_odds)
            merged = merge_predictions_with_odds(combined.copy(), odds_df, fetch_if_missing=False)
            matched = int(merged.get("odds_matched", pd.Series(False)).sum())
            source = "The Odds API (BetNow fallback)"
            warning = (
                f"BetNow.eu scraper failed ({exc}). "
                f"Showing The Odds API consensus ({matched}/{len(combined)} matched)."
            )
        except Exception as api_exc:
            logger.warning("BetNow fallback API failed: %s", api_exc)
            return {
                "predictions": combined.copy(),
                "alerts": {},
                "odds_matched": 0,
                "odds_total": len(combined),
                "error": str(exc),
                "warning": f"BetNow failed ({exc}). Odds API fallback also failed ({api_exc}).",
                "props": props_payload,
            }

    from src.strategy import bankroll_from_budget, budget_aware_alerts

    br = bankroll if bankroll is not None else bankroll_from_budget(budget_state)
    alerts = generate_alerts(
        merged,
        event_name=event_label,
        use_dynamic_thresholds=config.DYNAMIC_THRESHOLDS_ENABLED,
        bankroll=br,
    )
    alerts = budget_aware_alerts(alerts, budget_state, book_name)
    props_payload: dict[str, Any] = {}
    if config.ENABLE_PROPS:
        props_payload = _build_props_payload(
            merged,
            book_name,
            force_refresh_odds=force_refresh_odds,
            budget_state=budget_state,
        )
    return {
        "predictions": merged,
        "alerts": alerts,
        "odds_matched": matched,
        "odds_total": len(combined),
        "source": source,
        "warning": warning,
        "props": props_payload,
    }


def apply_books_to_predictions(
    combined: pd.DataFrame,
    *,
    force_refresh_odds: bool = False,
    event_label: str = "",
    progress: ProgressFn | None = None,
    books_filter: set[str] | None = None,
    budget_state: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch odds per book and build alert payloads."""
    from src.strategy import bankroll_from_budget, budget_aware_alerts

    if budget_state is not None:
        enabled = config.enabled_books_from_budget(budget_state)
        if books_filter is None:
            books_filter = enabled
        else:
            books_filter = books_filter & enabled

    bankroll = bankroll_from_budget(budget_state)
    books: dict[str, dict[str, Any]] = {}
    merged_by_book: dict[str, pd.DataFrame] = {}
    loaders = [
        (n, v) for n, v in active_book_loaders().items() if books_filter is None or n in books_filter
    ]

    for i, (book_name, (mod_path, fn_name)) in enumerate(loaders):
        pct = 0.55 + (i / max(len(loaders), 1)) * 0.35
        _log(progress, f"Odds: {book_name}…", pct)
        book_data = _load_book_odds(
            book_name,
            mod_path,
            fn_name,
            combined,
            force_refresh_odds=force_refresh_odds,
            event_label=event_label,
            bankroll=bankroll,
            budget_state=budget_state,
        )
        books[book_name] = book_data
        if book_data.get("odds_matched", 0) > 0:
            merged_by_book[book_name] = book_data["predictions"]

    overview = _pick_best_odds_overview(combined, merged_by_book)

    overview_alerts = generate_alerts(
        overview,
        event_name=event_label,
        use_dynamic_thresholds=config.DYNAMIC_THRESHOLDS_ENABLED,
        bankroll=bankroll,
    )
    books["Overview"] = {
        "predictions": overview,
        "alerts": budget_aware_alerts(overview_alerts, budget_state, "Overview"),
        "odds_matched": int(overview.get("odds_matched", pd.Series(False)).sum()),
        "odds_total": len(overview),
    }
    return books


def _ensure_book_props(
    books: dict[str, dict[str, Any]],
    combined: pd.DataFrame,
    *,
    force_refresh_odds: bool,
    budget_state: dict[str, Any] | None = None,
) -> None:
    """Build props payloads when ENABLE_PROPS is on but books lack prop data."""
    if not config.ENABLE_PROPS:
        return
    for book_name, entry in books.items():
        if book_name == "Overview":
            continue
        props = entry.get("props") or {}
        meta = props.get("singles_meta") or {}
        if props.get("singles") or int(meta.get("total_found", 0)) > 0:
            continue
        preds = entry.get("predictions", combined)
        if not isinstance(preds, pd.DataFrame) or preds.empty:
            continue
        entry["props"] = _build_props_payload(
            preds,
            book_name,
            force_refresh_odds=force_refresh_odds,
            budget_state=budget_state,
        )
        logger.info("Built props for %s (ENABLE_PROPS=True)", book_name)


def run_quick_odds_refresh(
    base_preds: pd.DataFrame,
    *,
    event_label: str = "",
    progress: ProgressFn | None = None,
    budget_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fast path: BetNow + DraftKings (+ MyBookie when enabled)."""
    _reload_config_flags()
    from src.strategy import bankroll_from_budget, budget_aware_alerts

    books_label = "BetNow + DraftKings"
    quick_filter = {"BetNow.eu", "DraftKings"}
    if config.MYBOOKIE_ENABLED:
        books_label += " + MyBookie"
        quick_filter.add("MyBookie")
    if budget_state is not None:
        quick_filter &= config.enabled_books_from_budget(budget_state)
    _log(progress, f"Quick odds refresh ({books_label})…", 0.1)
    books = apply_books_to_predictions(
        base_preds,
        force_refresh_odds=True,
        event_label=event_label,
        progress=progress,
        books_filter=quick_filter,
        budget_state=budget_state,
    )
    merged_by_book = {k: v["predictions"] for k, v in books.items() if k != "Overview" and "predictions" in v}
    overview = _pick_best_odds_overview(base_preds, merged_by_book)
    bankroll = bankroll_from_budget(budget_state)
    overview_alerts = generate_alerts(overview, event_name=event_label, bankroll=bankroll)
    books["Overview"] = {
        "predictions": overview,
        "alerts": budget_aware_alerts(overview_alerts, budget_state, "Overview"),
        "odds_matched": int(overview.get("odds_matched", pd.Series(False)).sum()),
        "odds_total": len(overview),
    }
    threshold_ctx = threshold_context_for_alerts(overview, bankroll=bankroll)
    _log(progress, "Quick odds complete.", 1.0)
    return {"books": books, "threshold_ctx": threshold_ctx, "odds_updated_at": _utc_now()}


def _fetch_card_for_analysis(event_index: int, event_name: str) -> pd.DataFrame:
    """Fetch raw card; fall back to on-disk cache when live scrape fails."""
    from main import fetch_event_card

    try:
        card = fetch_event_card(event_index, refresh=False)
        if not card.empty:
            card = card.copy()
            card["event_name"] = event_name
            logger.info(
                "Loaded %d fights for card %r (index %d, live/cache)",
                len(card),
                event_name,
                event_index,
            )
            return card
    except Exception as exc:
        logger.warning("Live card fetch failed for %r: %s", event_name, exc)

    cache_path = config.CACHE_DIR / f"upcoming_card_{event_index}.csv"
    if cache_path.is_file():
        try:
            cached = pd.read_csv(cache_path, parse_dates=["date"])
            if not cached.empty:
                cached = cached.copy()
                cached["event_name"] = event_name
                logger.info(
                    "Loaded %d fights for card %r (upcoming_card_%d.csv fallback)",
                    len(cached),
                    event_name,
                    event_index,
                )
                return cached
        except Exception as exc:
            logger.warning("Upcoming card csv fallback failed for %r: %s", event_name, exc)

    raise SystemExit(f"Could not load card {event_name!r} (index {event_index})")


def _predictions_from_background_card(event_index: int, event_name: str) -> pd.DataFrame | None:
    """Last-resort per-card predictions from background snapshot parquet."""
    from src.background_runner import _paths

    bg_dir, _ = _paths()
    card_path = bg_dir / "cards" / f"{event_index}.parquet"
    if not card_path.is_file():
        return None
    try:
        preds = pd.read_parquet(card_path)
    except Exception as exc:
        logger.warning("Background card parquet read failed for %r: %s", event_name, exc)
        return None
    if preds.empty:
        return None
    logger.info(
        "Loaded %d fights for card %r (background cards/%d.parquet fallback)",
        len(preds),
        event_name,
        event_index,
    )
    return preds


def _background_analysis_fallback(*, profile: str = "paper") -> dict[str, Any] | None:
    """Use background snapshot when live refresh fails or returns no fights."""
    from src.background_runner import load_background_snapshot

    for label, hours in (("fresh", 24), ("stale", 168), ("any", None)):
        snap = load_background_snapshot(max_age_hours=hours)
        combined = snap.get("combined") if snap else None
        if snap and isinstance(combined, pd.DataFrame) and not combined.empty:
            logger.warning(
                "Using %s background cache fallback (%d fights)",
                label,
                len(combined),
            )
            out = dict(snap)
            out["profile"] = profile
            out["errors"] = list(snap.get("errors") or []) + [
                f"Live load unavailable — showing {label} cached fights."
            ]
            return out
    return None


def _combine_card_predictions(cards: list[dict[str, Any]]) -> pd.DataFrame:
    """Merge per-card predictions; drop cross-card duplicate fight_ids."""
    frames: list[pd.DataFrame] = []
    seen_ids: set[str] = set()
    for idx, card in enumerate(cards):
        preds = card.get("predictions")
        ev = str(card.get("event_name") or f"Card {idx}")
        if not isinstance(preds, pd.DataFrame) or preds.empty:
            logger.info("Loaded Card %d: %s - 0 fights", idx, ev)
            continue
        chunk = preds.copy()
        chunk["event_name"] = ev
        if config.FIGHT_ID_COLUMN in chunk.columns:
            ids = chunk[config.FIGHT_ID_COLUMN].astype(str)
            dup_mask = ids.isin(seen_ids)
            if dup_mask.any():
                logger.warning(
                    "Dropped %d duplicate fight row(s) already on another card (card %d %r)",
                    int(dup_mask.sum()),
                    idx,
                    ev,
                )
                chunk = chunk.loc[~dup_mask]
            seen_ids.update(chunk[config.FIGHT_ID_COLUMN].astype(str))
        logger.info("Loaded Card %d: %s - %d fights", idx, ev, len(chunk))
        if not chunk.empty:
            frames.append(chunk)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def load_next_two_cards(
    *,
    explain: bool = True,
    use_cache: bool = True,
    progress: ProgressFn | None = None,
) -> tuple[list[tuple[int, str]], list[dict[str, Any]], pd.DataFrame, bool]:
    """
    Resolve and predict the two soonest upcoming cards (Refresh Next Two path).

    Logs target resolution, raw card size, and prediction row counts for debugging.
    """
    from main import load_or_refresh_data
    from src.predictor import resolve_analysis_targets

    _log(progress, "Resolving next two events…", 0.02)
    try:
        targets = resolve_analysis_targets(
            None,
            next_two=True,
            include_adjacent_week=False,
        )
    except SystemExit as exc:
        logger.warning("resolve_analysis_targets (next two) failed: %s", exc)
        fallback = _background_analysis_fallback()
        if fallback:
            cards = fallback.get("cards") or []
            combined = fallback.get("combined", pd.DataFrame())
            labels = [c.get("event_name", "") for c in cards]
            return (
                [(i, n) for i, n in enumerate(labels)],
                cards,
                combined,
                True,
            )
        raise

    labels = [name for _, name in targets]
    logger.info(
        "load_next_two_cards: resolve_analysis_targets returned %d event(s): %s",
        len(targets),
        labels,
    )
    if not targets:
        logger.warning("load_next_two_cards: no events resolved")
        fallback = _background_analysis_fallback()
        if fallback:
            cards = fallback.get("cards") or []
            combined = fallback.get("combined", pd.DataFrame())
            return ([], cards, combined, True)
        return [], [], pd.DataFrame(), False

    fights = load_or_refresh_data(refresh=False)
    logger.info("load_next_two_cards: historical fight pool rows=%d", len(fights))

    cards: list[dict[str, Any]] = []
    n = len(targets)
    all_cached = bool(use_cache)

    for idx, (event_index, event_name) in enumerate(targets):
        base_pct = 0.05 + (idx / n) * 0.5
        span = 0.5 / n
        _log(progress, f"Card {idx + 1}/{n}: {event_name}", base_pct)
        try:
            card = _fetch_card_for_analysis(event_index, event_name)
        except SystemExit as exc:
            logger.warning("Card %r unavailable: %s — trying caches", event_name, exc)
            preds = _predictions_from_background_card(event_index, event_name)
            if preds is not None:
                cards.append({"event_name": event_name, "predictions": preds})
                all_cached = True
                continue
            raise
        raw_rows = len(card)
        logger.info(
            "load_next_two_cards: card %d %r raw bouts=%d",
            idx + 1,
            event_name,
            raw_rows,
        )
        if use_cache:
            hit = load_event_cache(event_name, card)
            if not hit or hit["meta"].get("explain") != explain:
                all_cached = False
        else:
            all_cached = False
        preds = predict_card_cached(
            card,
            fights,
            event_name,
            explain=explain,
            use_cache=use_cache,
            progress=progress,
            step_pct=base_pct,
            step_span=span,
        )
        logger.info("Loaded %d fights for card %r (predictions from %d bouts)", len(preds), event_name, raw_rows)
        cards.append({"event_index": event_index, "event_name": event_name, "predictions": preds})

    combined = _combine_card_predictions(cards)
    logger.info(
        "load_next_two_cards: done — %d total predictions across %d card(s)",
        len(combined),
        len(cards),
    )
    return targets, cards, combined, all_cached


def run_full_analysis(
    *,
    event_mode: str,
    profile: str,
    force_refresh_odds: bool = False,
    explain: bool = True,
    use_cache: bool = True,
    progress: ProgressFn | None = None,
    budget_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full dashboard analysis with cached feature engineering."""
    _reload_config_flags()
    from main import fetch_event_card, resolve_event_targets, load_or_refresh_data, _model_exists
    from src.risk_manager import assess_upcoming_card_risk
    from src.strategy import bankroll_from_budget

    result: dict[str, Any] = {
        "generated_at": _utc_now(),
        "event_label": "",
        "profile": profile,
        "cards": [],
        "combined": pd.DataFrame(),
        "books": {},
        "risk_metrics": {},
        "threshold_ctx": {},
        "errors": [],
    }
    config.UFC_PROFILE = profile
    config.apply_profile_overrides()

    if not _model_exists():
        result["errors"].append("No trained model in models/.")
        return result

    next_two = event_mode in ("Next Two Cards", "Last Two Cards")
    event_query = None if event_mode in ("Next Card", "Next Two Cards", "Last Two Cards") else event_mode

    if event_mode == "Next Two Cards":
        try:
            targets, cards, combined, all_cached = load_next_two_cards(
                explain=explain,
                use_cache=use_cache,
                progress=progress,
            )
        except SystemExit as exc:
            result["errors"].append(str(exc) or "Could not resolve events.")
            fallback = _background_analysis_fallback(profile=profile)
            if fallback:
                for key in (
                    "cards",
                    "combined",
                    "books",
                    "risk_metrics",
                    "threshold_ctx",
                    "event_label",
                    "generated_at",
                    "odds_updated_at",
                    "from_cache",
                ):
                    if key in fallback:
                        result[key] = fallback[key]
                result["errors"].extend(fallback.get("errors") or [])
                return result
            return result
        result["event_label"] = " + ".join(name for _, name in targets) if targets else (
            " + ".join(c.get("event_name", "") for c in cards if c.get("event_name"))
        )
        result["cards"] = cards
        result["combined"] = combined
        result["from_cache"] = all_cached
        if combined.empty:
            fallback = _background_analysis_fallback(profile=profile)
            if fallback and not fallback.get("combined", pd.DataFrame()).empty:
                for key in ("cards", "combined", "event_label", "from_cache"):
                    if key in fallback:
                        result[key] = fallback[key]
                result["errors"].append("Live card load returned no fights — using cached snapshot.")
    else:
        _log(progress, "Resolving events…", 0.02)
        try:
            targets = resolve_event_targets(
                event_query,
                next_two=next_two,
                include_adjacent_week=not next_two and event_query is not None,
            )
        except SystemExit as exc:
            result["errors"].append(str(exc) or "Could not resolve events.")
            return result

        result["event_label"] = " + ".join(name for _, name in targets)
        fights = load_or_refresh_data(refresh=False)
        n = len(targets)
        all_cached = bool(use_cache)

        for idx, (event_index, event_name) in enumerate(targets):
            base_pct = 0.05 + (idx / n) * 0.5
            span = 0.5 / n
            _log(progress, f"Card {idx + 1}/{n}: {event_name}", base_pct)
            card = fetch_event_card(event_index, refresh=False)
            if use_cache:
                hit = load_event_cache(event_name, card)
                if not hit or hit["meta"].get("explain") != explain:
                    all_cached = False
            else:
                all_cached = False
            preds = predict_card_cached(
                card,
                fights,
                event_name,
                explain=explain,
                use_cache=use_cache,
                progress=progress,
                step_pct=base_pct,
                step_span=span,
            )
            result["cards"].append({"event_index": event_index, "event_name": event_name, "predictions": preds})

        combined = _combine_card_predictions(result["cards"])
        result["combined"] = combined
        result["from_cache"] = all_cached

    bankroll = bankroll_from_budget(budget_state)
    if budget_state is not None:
        config.apply_budget_state(budget_state)

    _log(progress, "Loading odds…", 0.58)
    result["books"] = apply_books_to_predictions(
        combined,
        force_refresh_odds=force_refresh_odds,
        event_label=result["event_label"],
        progress=progress,
        budget_state=budget_state,
    )
    _ensure_book_props(
        result["books"],
        combined,
        force_refresh_odds=force_refresh_odds,
        budget_state=budget_state,
    )

    _log(progress, "Risk analysis…", 0.92)
    try:
        dk = result["books"].get("DraftKings", {}).get("predictions", combined)
        result["risk_metrics"] = assess_upcoming_card_risk(
            dk,
            bankroll=bankroll,
            simulations=min(config.MC_CARD_SIMULATIONS, 3000),
        )
    except Exception as exc:
        result["risk_metrics"] = {"available": False, "reason": str(exc)}
        result["errors"].append(f"Risk: {exc}")

    overview = result["books"].get("Overview", {}).get("predictions", combined)
    result["threshold_ctx"] = threshold_context_for_alerts(
        overview,
        bankroll=bankroll,
    )
    if config.ENABLE_PROPS:
        try:
            from src.backtester import load_backtest_summary

            prop_bt = load_backtest_summary()
            if prop_bt:
                result["prop_backtest"] = {
                    k.replace("prop_", ""): v
                    for k, v in prop_bt.items()
                    if k.startswith("prop_") or k.startswith("prop_acc_") or k.startswith("mixed_parlay_")
                }
        except Exception:
            result["prop_backtest"] = {}
    _log(progress, "Complete.", 1.0)
    if result["combined"].empty and not result.get("books"):
        fallback = _background_analysis_fallback(profile=profile)
        if fallback:
            for key in (
                "cards",
                "combined",
                "books",
                "risk_metrics",
                "threshold_ctx",
                "event_label",
                "generated_at",
                "odds_updated_at",
                "from_cache",
            ):
                if key in fallback:
                    result[key] = fallback[key]
            result["errors"].append("Analysis produced no fights — loaded background cache.")
    return result


def detect_card_change(event_index: int = 0) -> tuple[bool, str, list[str]]:
    from main import fetch_event_card

    card = fetch_event_card(event_index, refresh=True)
    fp = card_fingerprint(card)
    event_name = fp.get("event_name") or "Upcoming"
    cached = load_event_cache(event_name, card)
    if cached is None:
        return True, event_name, fp["fight_ids"]
    changed = cached["meta"].get("fingerprint") != fp
    return changed, event_name, fp["fight_ids"]
