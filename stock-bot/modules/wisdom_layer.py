"""Wisdom Layer — one high-conviction recommendation per cycle for the Operating Layer."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from modules.safe_io import append_jsonl_line


@dataclass
class WisdomRecommendation:
    action: str
    core_target_delta: float
    conviction: float
    rationale: str
    regime: str = ""
    vol: str = ""
    macro_stress: bool = False
    rolling_sharpe: float | None = None
    accepted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_LAST_RECOMMENDATION: WisdomRecommendation | None = None


def wisdom_log_path() -> Path:
    raw = getattr(config, "WISDOM_LOG_FILE", "logs/wisdom_log.jsonl")
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    return path


def load_last_wisdom_recommendation() -> dict[str, Any] | None:
    path = wisdom_log_path()
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            return json.loads(line)
    except (OSError, json.JSONDecodeError):
        return None
    return None


def get_last_wisdom_recommendation() -> WisdomRecommendation | None:
    return _LAST_RECOMMENDATION


class WisdomAdvisor:
    """Produce a single actionable core-shift recommendation per cycle."""

    BEAR_MARKERS = ("RHYME_E", "RHYME_B", "Bearish", "Panic")
    BULL_MARKERS = ("RHYME_C", "RHYME_A", "Bullish")

    def recommend(
        self,
        *,
        regime: str,
        vol: str,
        macro_stress: bool = False,
        rolling_sharpe: float | None = None,
        vol_score: float | None = None,
    ) -> WisdomRecommendation:
        regime_s = str(regime or "")
        vol_s = str(vol or "")
        delta = 0.0
        conviction = 0.55
        action = "hold"
        rationale = "Neutral regime/vol — maintain operating targets"

        if macro_stress:
            action = "lower_core"
            delta = -0.06
            conviction = 0.86
            rationale = "Macro stress active — reduce core exposure"
        elif any(tag in regime_s for tag in self.BEAR_MARKERS):
            action = "lower_core"
            delta = -0.08
            conviction = 0.88
            rationale = f"Bear/panic regime ({regime_s}) — defensive core trim"
        elif vol_s.lower() in ("high", "extreme") or (
            vol_score is not None and float(vol_score) >= 0.025
        ):
            action = "lower_core"
            delta = -0.05
            conviction = 0.80
            rationale = f"Elevated cross-asset vol ({vol_s}) — trim core"
        elif any(tag in regime_s for tag in self.BULL_MARKERS):
            if rolling_sharpe is not None and rolling_sharpe >= 1.0:
                action = "raise_core"
                delta = 0.05
                conviction = 0.82
                rationale = (
                    f"Bull regime with Sharpe {rolling_sharpe:.2f} — add core exposure"
                )
            else:
                action = "raise_core"
                delta = 0.03
                conviction = 0.76
                rationale = f"Bull regime ({regime_s}) — modest core add"
        elif rolling_sharpe is not None and rolling_sharpe < 0.0:
            action = "lower_core"
            delta = -0.04
            conviction = 0.77
            rationale = f"Negative rolling Sharpe ({rolling_sharpe:.2f}) — reduce core"

        rec = WisdomRecommendation(
            action=action,
            core_target_delta=round(delta, 4),
            conviction=round(conviction, 3),
            rationale=rationale,
            regime=regime_s,
            vol=vol_s,
            macro_stress=bool(macro_stress),
            rolling_sharpe=round(rolling_sharpe, 3) if rolling_sharpe is not None else None,
        )
        rec.accepted = self.is_actionable(rec)
        self.log_recommendation(rec)
        return rec

    def is_actionable(self, rec: WisdomRecommendation) -> bool:
        if rec.action == "hold" or abs(rec.core_target_delta) < 1e-6:
            return False
        return float(rec.conviction) >= float(config.WISDOM_CONVICTION_THRESHOLD)

    def apply_core_shift(self, base_core_target: float, rec: WisdomRecommendation) -> float:
        if not self.is_actionable(rec):
            return float(base_core_target)
        max_shift = float(config.WISDOM_MAX_CORE_SHIFT_PCT)
        shift = max(-max_shift, min(max_shift, float(rec.core_target_delta)))
        return round(float(base_core_target) + shift, 4)

    def log_recommendation(self, rec: WisdomRecommendation) -> None:
        global _LAST_RECOMMENDATION
        _LAST_RECOMMENDATION = rec
        payload = rec.to_dict()
        payload["timestamp"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        payload["conviction_threshold"] = float(config.WISDOM_CONVICTION_THRESHOLD)
        append_jsonl_line(wisdom_log_path(), payload)


def format_wisdom_banner() -> str | None:
    if not config.REBALANCE_ENABLED:
        return None
    last = load_last_wisdom_recommendation()
    if last:
        action = last.get("action", "hold")
        conv = float(last.get("conviction", 0.0))
        accepted = last.get("accepted", False)
        delta = float(last.get("core_target_delta", 0.0))
        return (
            f"Operating Layer ON | last wisdom: {action} "
            f"delta {delta:+.1%} conv {conv:.2f} "
            f"({'accepted' if accepted else 'advisory-only'})"
        )
    return "Operating Layer ON | wisdom: no prior recommendation logged"
