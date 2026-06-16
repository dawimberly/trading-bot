"""Shared in-process caches for inference (avoids repeated joblib loads)."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.predictor import FightPredictor

_predictor_lock = threading.Lock()
_predictor_instance: FightPredictor | None = None


def get_shared_predictor(*, force_reload: bool = False) -> FightPredictor:
    """Return a process-wide FightPredictor singleton (thread-safe lazy init)."""
    global _predictor_instance
    with _predictor_lock:
        if force_reload or _predictor_instance is None:
            from src.predictor import FightPredictor

            _predictor_instance = FightPredictor()
        return _predictor_instance


def clear_predictor_cache() -> None:
    """Drop cached predictor (e.g. after model retrain)."""
    global _predictor_instance
    with _predictor_lock:
        _predictor_instance = None
