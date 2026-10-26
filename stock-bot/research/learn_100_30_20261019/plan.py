"""Frozen window and candidate geometry. No simulation."""

from __future__ import annotations

WARMUP = 115
TRAIN = 100
EVAL = 30
CUTOFF = "2026-10-14"
INITIAL_CASH = 100_000.0
MIN_COMPLETED = 30
MAX_DRAWDOWN = 0.25
MA_GRID = (50, 70, 100)
HOLD_GRID = (15, 30, 45)
ATR_GRID = (1.5, 2.0, 2.5)
BASELINE = {"ma_win": 70, "max_hold": 30, "atr_mult": 2.0}


def candidate_id(row: dict) -> str:
    return f"ma{int(row['ma_win'])}_hold{int(row['max_hold'])}_atr{float(row['atr_mult']):.1f}"


def candidates() -> list[dict]:
    rows = []
    order = 0
    for ma in MA_GRID:
        for hold in HOLD_GRID:
            for mult in ATR_GRID:
                row = {
                    "order": order,
                    "ma_win": int(ma),
                    "max_hold": int(hold),
                    "atr_mult": float(mult),
                    "candidate_id": candidate_id({"ma_win": ma, "max_hold": hold, "atr_mult": mult}),
                }
                rows.append(row)
                order += 1
    if len(rows) != 27:
        raise RuntimeError(f"grid has {len(rows)} candidates")
    if not any(candidate_id(row) == candidate_id(BASELINE) for row in rows):
        raise RuntimeError("baseline missing from grid")
    return rows


def baseline_spec() -> dict:
    row = next(row for row in candidates() if candidate_id(row) == candidate_id(BASELINE))
    return dict(row)


def build_plan(calendar: list[str]) -> dict:
    """Anchor the last complete 30-session folds on the verified calendar.

    The first fold keeps 115 indicator sessions strictly before its 100
    training sessions. Later folds advance 30 sessions. Training may overlap.
    Evaluation dates do not.
    """
    need = WARMUP + TRAIN
    if len(calendar) <= need:
        raise RuntimeError(f"calendar {len(calendar)} cannot hold one fold")
    folds_n = (len(calendar) - need) // EVAL
    if folds_n < 1:
        raise RuntimeError("no complete evaluation fold")
    eval_sessions = folds_n * EVAL
    span_start = len(calendar) - (need + eval_sessions)
    folds = []
    seen = set()
    for i in range(folds_n):
        ev0 = span_start + need + i * EVAL
        ev = calendar[ev0 : ev0 + EVAL]
        scored = calendar[ev0 - TRAIN : ev0]
        hist = calendar[ev0 - TRAIN - WARMUP : ev0]
        if len(ev) != EVAL or len(scored) != TRAIN or len(hist) != need:
            raise RuntimeError("fold length drifted")
        if scored[-1] >= ev[0] or hist[-1] != scored[-1]:
            raise RuntimeError("training overlaps its evaluation fold")
        if any(day >= ev[0] for day in hist):
            raise RuntimeError("training reaches evaluation")
        overlap = seen.intersection(ev)
        if overlap:
            raise RuntimeError("evaluation windows overlap")
        seen.update(ev)
        folds.append(
            {
                "fold": i + 1,
                "warmup_start": hist[0],
                "train_start": scored[0],
                "train_end": scored[-1],
                "cutoff": scored[-1],
                "eval_start": ev[0],
                "eval_end": ev[-1],
                "train_dates": hist,
                "train_scored": scored,
                "eval_dates": ev,
            }
        )
    first_i = calendar.index(folds[0]["eval_start"])
    last_i = calendar.index(folds[-1]["eval_end"])
    start_i = first_i - 1 - WARMUP
    if start_i < 0:
        raise RuntimeError("evaluation book is missing indicator warmup")
    eval_history = calendar[start_i : last_i + 1]
    if eval_history[WARMUP] != calendar[first_i - 1]:
        raise RuntimeError("decision index is not the training cutoff")
    if eval_history[WARMUP + 1] != folds[0]["eval_start"]:
        raise RuntimeError("first evaluation session is not the fill after the cutoff")
    return {
        "warmup_sessions": WARMUP,
        "train_sessions": TRAIN,
        "eval_sessions": EVAL,
        "folds": folds_n,
        "evaluation_sessions": eval_sessions,
        "calendar_sessions": len(calendar),
        "unused_prefix_sessions": span_start,
        "unused_prefix_dates": calendar[:span_start],
        "first_warmup": folds[0]["warmup_start"],
        "first_train": folds[0]["train_start"],
        "first_eval": folds[0]["eval_start"],
        "last_eval": folds[-1]["eval_end"],
        "decision_start": WARMUP,
        "eval_history": eval_history,
        "fold_bounds": folds,
        "is_1000_session_study": False,
    }
