"""Live (dry-run) signal generation with bankroll guardrails."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from ufc_betting_bot.config.settings import LIVE_SIGNALS_CSV, get_settings
from ufc_betting_bot.modules.bankroll import BankrollManager
from ufc_betting_bot.modules.edge import compute_edge, fight_decimal_odds, market_probs
from ufc_betting_bot.modules.model_bridge import _ensure_predictor_path, get_predictor

logger = logging.getLogger(__name__)


@dataclass
class LiveSignal:
    event: str
    fighter_1: str
    fighter_2: str
    bet_side: str
    model_prob: float
    market_prob: float
    edge: float
    recommended_stake: float
    decimal_odds: float
    blocked_reason: str = ""


class LiveRunner:
    """Generate bet signals for the next card without placing real wagers."""

    def __init__(self, *, dry_run: bool = True):
        self.settings = get_settings()
        self.dry_run = dry_run
        self.manager = BankrollManager.load(self.settings.bankroll)
        _ensure_predictor_path()

    def fetch_upcoming_predictions(self) -> pd.DataFrame:
        from src.data_loader import get_upcoming_card

        card = get_upcoming_card(force_refresh=True)
        if card.empty:
            raise ValueError("No upcoming card available.")

        predictor = get_predictor()
        from src.predictor import merge_predictions_with_odds

        preds = predictor.predict_card(card)
        try:
            preds = merge_predictions_with_odds(preds, force_refresh=True)
        except Exception as exc:
            logger.warning("Odds API unavailable: %s", exc)
        return preds

    def build_signals(self, predictions: pd.DataFrame) -> list[LiveSignal]:
        signals: list[LiveSignal] = []
        bet_day = datetime.now(timezone.utc).date()

        for _, row in predictions.iterrows():
            market = market_probs(row)
            if market is None:
                continue

            m1, m2 = market
            p1 = float(row["prob_f1_win"])
            p2 = float(row.get("prob_f2_win", 1.0 - p1))
            edges = compute_edge(p1, p2, m1, m2)
            decimal = fight_decimal_odds(row)
            if decimal is None:
                continue

            dec_f1, dec_f2 = decimal
            prob = p1 if edges["bet_side"] == "f1" else p2
            odds = dec_f1 if edges["bet_side"] == "f1" else dec_f2

            stake = self.manager.compute_stake(
                prob=prob,
                decimal_odds=odds,
                edge=edges["edge"],
                bet_day=bet_day,
            )
            blocked = ""
            if stake <= 0:
                if edges["edge"] < self.settings.bankroll.min_edge:
                    blocked = "edge_below_threshold"
                elif not self.manager.can_bet(bet_day):
                    blocked = "daily_loss_limit_or_halted"
                else:
                    blocked = "stake_below_min_fraction"

            signals.append(
                LiveSignal(
                    event=str(row.get("event", "")),
                    fighter_1=str(row.get("fighter_1", row.get("fighter1", ""))),
                    fighter_2=str(row.get("fighter_2", row.get("fighter2", ""))),
                    bet_side=edges["bet_side"],
                    model_prob=prob,
                    market_prob=m1 if edges["bet_side"] == "f1" else m2,
                    edge=edges["edge"],
                    recommended_stake=stake,
                    decimal_odds=odds,
                    blocked_reason=blocked,
                )
            )

        return signals

    def save_signals(self, signals: list[LiveSignal], path: Path | None = None) -> Path:
        out = path or LIVE_SIGNALS_CSV
        rows = [
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "dry_run": self.dry_run,
                **s.__dict__,
            }
            for s in signals
        ]
        pd.DataFrame(rows).to_csv(out, index=False)
        self.manager.save()
        return out


def run_live_dry_run() -> pd.DataFrame:
    runner = LiveRunner(dry_run=True)
    preds = runner.fetch_upcoming_predictions()
    signals = runner.build_signals(preds)
    path = runner.save_signals(signals)
    logger.info("Wrote %s signals -> %s", len(signals), path)
    return pd.DataFrame([s.__dict__ for s in signals])
