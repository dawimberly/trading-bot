"""
Background UFC bot runner — scheduled full analysis + lightweight odds refresh.

Runs Next Two Cards processing at midnight or on Windows startup, caches results
for instant dashboard load. During the day, startup can skip full ML and only
refresh odds when the snapshot is fresh (<24h) but odds are stale.

CLI:
    python src/background_runner.py --mode full --trigger midnight
    python src/background_runner.py --mode auto --trigger startup
    python src/background_runner.py --mode lightweight --trigger manual
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

FULL_STALE_HOURS = 24
ODDS_STALE_MINUTES = 45
MANIFEST_VERSION = 1

# Set by main() after bootstrap
BACKGROUND_DIR: Path | None = None
MANIFEST_PATH: Path | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    s = str(ts).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        pass
    for fmt in ("%Y-%m-%d %H:%M UTC", "%Y-%m-%d %H:%M:%S UTC"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _age_hours(ts: str | None) -> float | None:
    dt = _parse_iso(ts)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0


def _age_minutes(ts: str | None) -> float | None:
    hours = _age_hours(ts)
    return None if hours is None else hours * 60.0


def _book_slug(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name).strip("_") or "book"


def _ensure_paths() -> tuple[Path, Path]:
    """Resolve cache dir from config (works when imported from dashboard/EXE)."""
    global BACKGROUND_DIR, MANIFEST_PATH
    if BACKGROUND_DIR is None or MANIFEST_PATH is None:
        try:
            import config

            BACKGROUND_DIR = config.CACHE_DIR / "background"
        except Exception:
            root = Path(__file__).resolve().parents[1]
            BACKGROUND_DIR = root / "data" / "cache" / "background"
        MANIFEST_PATH = BACKGROUND_DIR / "manifest.json"
    return BACKGROUND_DIR, MANIFEST_PATH


def _paths() -> tuple[Path, Path]:
    return _ensure_paths()


def read_manifest() -> dict[str, Any]:
    from src.safe_io import read_json_file

    _, manifest_path = _paths()
    data = read_json_file(manifest_path)
    return data if isinstance(data, dict) else {}


def is_full_snapshot_stale(manifest: dict[str, Any] | None = None) -> bool:
    """True when no snapshot exists or the last full run is older than 24 hours."""
    manifest = manifest if manifest is not None else read_manifest()
    if not manifest:
        return True
    full_at = manifest.get("full_run_at") or manifest.get("saved_at")
    age = _age_hours(full_at)
    return age is None or age > FULL_STALE_HOURS


def is_odds_snapshot_stale(manifest: dict[str, Any] | None = None) -> bool:
    manifest = manifest if manifest is not None else read_manifest()
    if not manifest:
        return True
    age = _age_minutes(manifest.get("odds_updated_at") or manifest.get("saved_at"))
    return age is None or age > ODDS_STALE_MINUTES


def save_background_snapshot(
    data: dict[str, Any],
    *,
    run_type: str,
    trigger: str,
    previous_manifest: dict[str, Any] | None = None,
) -> bool:
    """Persist dashboard analysis for instant GUI load."""
    from src.safe_io import write_json_atomic

    bg_dir, manifest_path = _paths()
    bg_dir.mkdir(parents=True, exist_ok=True)
    (bg_dir / "cards").mkdir(parents=True, exist_ok=True)
    (bg_dir / "books").mkdir(parents=True, exist_ok=True)

    combined: pd.DataFrame = data.get("combined", pd.DataFrame())
    if isinstance(combined, pd.DataFrame) and not combined.empty:
        combined.to_parquet(bg_dir / "combined.parquet", index=False)

    card_events: list[str] = []
    for i, card in enumerate(data.get("cards") or []):
        preds = card.get("predictions", pd.DataFrame())
        event_name = str(card.get("event_name", f"card_{i}"))
        card_events.append(event_name)
        if isinstance(preds, pd.DataFrame) and not preds.empty:
            preds.to_parquet(bg_dir / "cards" / f"{i}.parquet", index=False)

    book_files: dict[str, str] = {}
    for book_name, book_data in (data.get("books") or {}).items():
        if not isinstance(book_data, dict):
            continue
        slug = _book_slug(str(book_name))
        preds = book_data.get("predictions")
        if isinstance(preds, pd.DataFrame) and not preds.empty:
            rel = f"books/{slug}_predictions.parquet"
            preds.to_parquet(bg_dir / rel, index=False)
            book_files[str(book_name)] = rel
        meta = {k: v for k, v in book_data.items() if k != "predictions"}
        write_json_atomic(bg_dir / "books" / f"{slug}_meta.json", meta)

    prev = previous_manifest or read_manifest()
    now_iso = _iso_now()
    full_run_at = now_iso if run_type == "full" else (prev.get("full_run_at") or prev.get("saved_at") or now_iso)

    manifest = {
        "version": MANIFEST_VERSION,
        "run_type": run_type,
        "trigger": trigger,
        "saved_at": now_iso,
        "full_run_at": full_run_at,
        "generated_at": data.get("generated_at", _utc_now()),
        "odds_updated_at": data.get("odds_updated_at", data.get("generated_at", _utc_now())),
        "event_label": data.get("event_label", ""),
        "profile": data.get("profile", "research"),
        "from_cache": bool(data.get("from_cache", False)),
        "errors": list(data.get("errors") or []),
        "card_events": card_events,
        "book_files": book_files,
        "risk_metrics": data.get("risk_metrics") or {},
        "threshold_ctx": data.get("threshold_ctx") or {},
    }
    ok = write_json_atomic(manifest_path, manifest)
    if ok:
        logger.info(
            "Background snapshot saved (%s/%s) — %s fights, %s books",
            run_type,
            trigger,
            len(combined) if isinstance(combined, pd.DataFrame) else 0,
            len(book_files),
        )
    return ok


def load_background_snapshot(*, max_age_hours: float | None = FULL_STALE_HOURS) -> dict[str, Any] | None:
    """Load cached analysis if manifest exists and is not stale."""
    bg_dir, manifest_path = _paths()
    manifest = read_manifest()
    if not manifest or not manifest_path.is_file():
        return None

    full_at = manifest.get("full_run_at") or manifest.get("saved_at")
    age_h = _age_hours(full_at)
    if max_age_hours is not None and (age_h is None or age_h > max_age_hours):
        logger.info("Background snapshot stale (%.1fh) — skip load", age_h or -1)
        return None

    combined_path = bg_dir / "combined.parquet"
    if not combined_path.is_file():
        logger.warning("Background manifest present but combined.parquet missing")
        return None

    try:
        combined = pd.read_parquet(combined_path)
    except Exception as exc:
        logger.warning("Failed to read combined.parquet: %s", exc)
        return None

    cards: list[dict[str, Any]] = []
    for i, event_name in enumerate(manifest.get("card_events") or []):
        card_path = bg_dir / "cards" / f"{i}.parquet"
        preds = pd.DataFrame()
        if card_path.is_file():
            preds = pd.read_parquet(card_path)
        cards.append({"event_name": event_name, "predictions": preds})

    books: dict[str, dict[str, Any]] = {}
    from src.safe_io import read_json_file

    for book_name, rel in (manifest.get("book_files") or {}).items():
        pred_path = bg_dir / rel
        meta_path = bg_dir / "books" / f"{_book_slug(book_name)}_meta.json"
        preds = pd.read_parquet(pred_path) if pred_path.is_file() else combined.copy()
        meta = read_json_file(meta_path)
        books[book_name] = {**meta, "predictions": preds}

    return {
        "generated_at": manifest.get("generated_at", ""),
        "event_label": manifest.get("event_label", ""),
        "profile": manifest.get("profile", "research"),
        "cards": cards,
        "combined": combined,
        "books": books,
        "risk_metrics": manifest.get("risk_metrics") or {},
        "threshold_ctx": manifest.get("threshold_ctx") or {},
        "errors": list(manifest.get("errors") or []),
        "odds_updated_at": manifest.get("odds_updated_at", manifest.get("generated_at", "")),
        "from_cache": bool(manifest.get("from_cache", True)),
        "_manifest": manifest,
    }


def _resolve_mode(mode: str, trigger: str) -> str:
    if mode != "auto":
        return mode
    if trigger == "midnight":
        return "full"
    if is_full_snapshot_stale():
        return "full"
    if is_odds_snapshot_stale():
        return "lightweight"
    return "skip"


def run_full_job(*, trigger: str, profile: str = "research") -> int:
    from main import _model_exists
    from src.dashboard_service import run_full_analysis
    from src.heartbeat import write_heartbeat
    from src.logging_utils import log_event

    if not _model_exists():
        logger.error("No trained model — skipping full background run")
        write_heartbeat(status="error", block_reason="no_model", extra={"trigger": trigger, "mode": "full"})
        return 1

    log_event("background_full_start", trigger=trigger, profile=profile)
    logger.info("Full background run (Next Two Cards) — trigger=%s", trigger)

    try:
        from src.fighter_cache import warm_fighter_cache

        warm_fighter_cache()
        data = run_full_analysis(
            event_mode="Next Two Cards",
            profile=profile,
            force_refresh_odds=True,
            explain=False,
            use_cache=True,
        )
        if data.get("errors") and data.get("combined", pd.DataFrame()).empty:
            logger.error("Full run failed: %s", "; ".join(data["errors"]))
            write_heartbeat(
                status="error",
                block_reason="; ".join(data["errors"][:3]),
                extra={"trigger": trigger, "mode": "full"},
            )
            return 1

        if not save_background_snapshot(data, run_type="full", trigger=trigger):
            logger.error("Failed to save background snapshot")
            return 1

        event_label = data.get("event_label", "")
        write_heartbeat(
            status="ok",
            event_name=event_label,
            extra={
                "trigger": trigger,
                "mode": "full",
                "from_cache": data.get("from_cache"),
                "n_fights": len(data.get("combined", [])),
            },
        )
        log_event("background_full_done", trigger=trigger, event=event_label)
        return 0
    except Exception as exc:
        logger.error("Full background run failed: %s", exc)
        logger.debug(traceback.format_exc())
        write_heartbeat(status="error", block_reason=str(exc), extra={"trigger": trigger, "mode": "full"})
        return 1


def run_lightweight_job(*, trigger: str) -> int:
    from src.dashboard_service import run_quick_odds_refresh
    from src.heartbeat import write_heartbeat
    from src.logging_utils import log_event

    snapshot = load_background_snapshot(max_age_hours=FULL_STALE_HOURS)
    if snapshot is None:
        logger.info("No fresh snapshot for lightweight run — delegating to full")
        return run_full_job(trigger=trigger)

    combined = snapshot.get("combined", pd.DataFrame())
    if not isinstance(combined, pd.DataFrame) or combined.empty:
        logger.warning("Snapshot has no predictions — running full job")
        return run_full_job(trigger=trigger)

    event_label = snapshot.get("event_label", "")
    log_event("background_lightweight_start", trigger=trigger, event=event_label)
    logger.info("Lightweight odds refresh — trigger=%s event=%s", trigger, event_label)

    try:
        odds_result = run_quick_odds_refresh(combined, event_label=event_label)
        snapshot["books"] = odds_result.get("books", snapshot.get("books", {}))
        snapshot["threshold_ctx"] = odds_result.get("threshold_ctx", snapshot.get("threshold_ctx", {}))
        snapshot["odds_updated_at"] = odds_result.get("odds_updated_at", _utc_now())
        snapshot["generated_at"] = snapshot.get("generated_at") or _utc_now()

        if not save_background_snapshot(
            snapshot,
            run_type="lightweight",
            trigger=trigger,
            previous_manifest=snapshot.get("_manifest"),
        ):
            logger.error("Failed to save lightweight snapshot update")
            return 1

        write_heartbeat(
            status="ok",
            event_name=event_label,
            extra={"trigger": trigger, "mode": "lightweight"},
        )
        log_event("background_lightweight_done", trigger=trigger, event=event_label)
        return 0
    except Exception as exc:
        logger.error("Lightweight run failed: %s", exc)
        logger.debug(traceback.format_exc())
        write_heartbeat(status="error", block_reason=str(exc), extra={"trigger": trigger, "mode": "lightweight"})
        return 1


def run_background(*, mode: str = "auto", trigger: str = "manual", profile: str = "research") -> int:
    resolved = _resolve_mode(mode, trigger)
    if resolved == "skip":
        manifest = read_manifest()
        logger.info(
            "Background run skipped — snapshot fresh (full %.1fh ago, odds %.0fm ago)",
            _age_hours(manifest.get("full_run_at")) or -1,
            _age_minutes(manifest.get("odds_updated_at")) or -1,
        )
        from src.heartbeat import write_heartbeat

        write_heartbeat(
            status="ok",
            event_name=manifest.get("event_label", ""),
            extra={"trigger": trigger, "mode": "skip"},
        )
        return 0
    if resolved == "full":
        return run_full_job(trigger=trigger, profile=profile)
    if resolved == "lightweight":
        return run_lightweight_job(trigger=trigger)
    logger.error("Unknown mode: %s", mode)
    return 2


def _bootstrap() -> Path:
    global BACKGROUND_DIR, MANIFEST_PATH
    entry = Path(__file__).resolve()
    root = entry.parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from src.project_paths import bootstrap

    root = bootstrap(entry_file=entry)
    import config

    BACKGROUND_DIR = config.CACHE_DIR / "background"
    MANIFEST_PATH = BACKGROUND_DIR / "manifest.json"
    BACKGROUND_DIR.mkdir(parents=True, exist_ok=True)
    return root


def main(argv: list[str] | None = None) -> int:
    root = _bootstrap()
    import config
    from src.logging_utils import setup_logging
    from src.safe_io import install_safe_stdout

    install_safe_stdout()

    parser = argparse.ArgumentParser(description="UFC Predictor background runner")
    parser.add_argument(
        "--mode",
        choices=("auto", "full", "lightweight", "skip"),
        default="auto",
        help="auto: full if stale, else odds-only; full: always run ML+odds",
    )
    parser.add_argument(
        "--trigger",
        choices=("startup", "midnight", "manual", "scheduled"),
        default="manual",
        help="What invoked this run (for logging)",
    )
    parser.add_argument("--profile", default="research", choices=("research", "live"))
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args(argv)

    setup_logging(verbose=args.verbose, log_dir=config.LOG_DIR, log_name="background_runner.log")
    logger.info("Background runner start — root=%s mode=%s trigger=%s", root, args.mode, args.trigger)

    config.UFC_PROFILE = args.profile
    config.apply_profile_overrides()

    try:
        return run_background(mode=args.mode, trigger=args.trigger, profile=args.profile)
    except Exception as exc:
        logger.error("Unhandled background runner error: %s", exc)
        logger.debug(traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
