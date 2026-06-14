"""Fractional Kelly sizing with strict per-bet and daily loss limits."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ufc_betting_bot.config.settings import BankrollSettings, BANKROLL_STATE_PATH
from ufc_betting_bot.modules.edge import (
    compute_edge,
    fight_decimal_odds,
    market_probs,
    raw_kelly_fraction,
)


@dataclass
class BankrollState:
    bankroll: float
    day_start_bankroll: float
    current_day: str
    daily_pnl: float
    halted_today: bool
    total_bets: int = 0
    total_wins: int = 0
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BankrollState:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class BankrollManager:
    """
    Bankroll guardrails:
    - Fractional Kelly (default quarter-Kelly)
    - Hard cap: max 1–2% of bankroll per wager
    - Daily loss limit: stop betting after X% drawdown from day open
    """

    def __init__(self, settings: BankrollSettings, *, state: BankrollState | None = None):
        self.settings = settings
        if state is None:
            self.state = BankrollState(
                bankroll=settings.initial_bankroll,
                day_start_bankroll=settings.initial_bankroll,
                current_day="",
                daily_pnl=0.0,
                halted_today=False,
            )
        else:
            self.state = state

    @property
    def bankroll(self) -> float:
        return self.state.bankroll

    def _sync_day(self, bet_day: date | pd.Timestamp | str) -> None:
        day_key = pd.Timestamp(bet_day).date().isoformat()
        if self.state.current_day != day_key:
            self.state.current_day = day_key
            self.state.day_start_bankroll = self.state.bankroll
            self.state.daily_pnl = 0.0
            self.state.halted_today = False

    def daily_loss_limit_amount(self) -> float:
        return self.state.day_start_bankroll * self.settings.daily_loss_limit_fraction

    def can_bet(self, bet_day: date | pd.Timestamp | str | None = None) -> bool:
        if bet_day is not None:
            self._sync_day(bet_day)
        if self.state.halted_today or self.state.bankroll <= 0:
            return False
        if self.state.daily_pnl <= -self.daily_loss_limit_amount():
            self.state.halted_today = True
            return False
        return True

    def compute_stake(
        self,
        *,
        prob: float,
        decimal_odds: float,
        edge: float,
        bet_day: date | pd.Timestamp | str | None = None,
    ) -> float:
        """Stake in currency units; 0 when rules block the bet."""
        if edge < self.settings.min_edge:
            return 0.0
        if bet_day is not None and not self.can_bet(bet_day):
            return 0.0
        elif bet_day is None and not self.can_bet():
            return 0.0

        kelly = raw_kelly_fraction(prob, decimal_odds)
        fraction = kelly * self.settings.kelly_fraction
        fraction = min(fraction, self.settings.max_bet_fraction)
        if fraction < self.settings.min_bet_fraction:
            return 0.0

        stake = self.state.bankroll * fraction
        max_stake = self.state.bankroll * self.settings.max_bet_fraction
        return float(min(stake, max_stake))

    def record_bet(self, stake: float, won: bool, decimal_odds: float) -> float:
        """Apply PnL and return profit for this wager."""
        if stake <= 0:
            return 0.0
        pnl = stake * (decimal_odds - 1.0) if won else -stake
        self.state.bankroll = max(0.0, self.state.bankroll + pnl)
        self.state.daily_pnl += pnl
        self.state.total_bets += 1
        if won:
            self.state.total_wins += 1
        self.state.updated_at = datetime.now(timezone.utc).isoformat()
        if self.state.daily_pnl <= -self.daily_loss_limit_amount():
            self.state.halted_today = True
        return pnl

    def save(self, path: Path | None = None) -> Path:
        out = path or BANKROLL_STATE_PATH
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(self.state.to_dict(), indent=2), encoding="utf-8")
        return out

    @classmethod
    def load(
        cls,
        settings: BankrollSettings,
        path: Path | None = None,
    ) -> BankrollManager:
        state_path = path or BANKROLL_STATE_PATH
        if state_path.is_file():
            data = json.loads(state_path.read_text(encoding="utf-8"))
            return cls(settings, state=BankrollState.from_dict(data))
        return cls(settings)


def simulate_flat_bets(
    predictions: pd.DataFrame,
    settings: BankrollSettings,
    *,
    date_column: str = "event_date",
    target_column: str = "f1_win",
) -> tuple[pd.DataFrame, dict[str, float]]:
    """Flat-stake value bets when edge clears min_edge."""
    bankroll = settings.initial_bankroll
    rows: list[dict[str, Any]] = []
    pending_event: str | None = None
    pending_stakes: list[tuple[int, float, dict]] = []

    work = predictions.copy()
    if date_column in work.columns:
        work[date_column] = pd.to_datetime(work[date_column], errors="coerce")
        work = work.sort_values([date_column, "event_name"] if "event_name" in work.columns else [date_column])

    def _flush_event() -> None:
        nonlocal bankroll, pending_event, pending_stakes
        if not pending_stakes:
            return
        stakes = [s for _, s, _ in pending_stakes]
        capped, _ = _apply_card_cap(stakes, bankroll, settings.max_card_risk_fraction)
        for (i, (_, _stake, meta)), stake in zip(enumerate(pending_stakes), capped):
            if stake <= 0:
                continue
            won = meta["won"]
            odds = meta["odds"]
            pnl = stake * (odds - 1.0) if won else -stake
            bankroll += pnl
            rows.append({**meta, "stake": stake, "pnl": pnl, "equity": bankroll, "staking": "flat"})
        pending_stakes = []
        pending_event = None

    for _, row in work.iterrows():
        market = market_probs(row)
        if market is None:
            continue
        decimal = fight_decimal_odds(row)
        if decimal is None:
            continue
        market_p1, market_p2 = market
        model_p1 = float(row["prob_f1_win"])
        model_p2 = float(row.get("prob_f2_win", 1.0 - model_p1))
        edge_info = compute_edge(model_p1, model_p2, market_p1, market_p2)
        if edge_info["edge"] < settings.min_edge:
            continue

        event_key = str(row.get("event_name", row.get("event", "")))
        if pending_event and event_key != pending_event:
            _flush_event()
        pending_event = event_key

        if edge_info["bet_side"] == "f1":
            odds = decimal[0]
        else:
            odds = decimal[1]
        actual = row.get(target_column)
        if pd.isna(actual):
            continue
        won = (edge_info["bet_side"] == "f1" and int(actual) == 1) or (
            edge_info["bet_side"] == "f2" and int(actual) == 0
        )
        pending_stakes.append(
            (
                len(rows),
                settings.flat_stake,
                {
                    date_column: row.get(date_column),
                    "event_name": event_key,
                    "bet_side": edge_info["bet_side"],
                    "edge": edge_info["edge"],
                    "edge_pct": edge_info["edge"] * 100.0,
                    "odds": odds,
                    "won": int(won),
                },
            )
        )
    _flush_event()
    trades = pd.DataFrame(rows)
    return trades, _summarize_trades(trades, settings, label="flat")


def _apply_card_cap(
    stakes: list[float],
    bankroll: float,
    max_card_fraction: float,
    *,
    mc_card_risk: dict[str, Any] | None = None,
) -> tuple[list[float], float]:
    """Apply card cap; optionally MC-adjusted when risk metrics supplied."""
    if mc_card_risk:
        try:
            import sys
            from pathlib import Path

            repo = Path(__file__).resolve().parents[2]
            pred = repo / "ufc-predictor"
            if str(pred) not in sys.path:
                sys.path.insert(0, str(pred))
            from src.risk_manager import recommended_card_risk_fraction

            cap_frac, _ = recommended_card_risk_fraction(
                {**mc_card_risk, "bankroll": bankroll},
                max_card_fraction,
            )
            max_card_fraction = cap_frac
        except ImportError:
            pass
    if not stakes or bankroll <= 0:
        return stakes, max_card_fraction
    total = sum(stakes)
    cap = bankroll * max_card_fraction
    if total <= cap:
        return stakes, max_card_fraction
    scale = cap / total
    return [s * scale for s in stakes], max_card_fraction


def _summarize_trades(
    trades: pd.DataFrame,
    settings: BankrollSettings,
    *,
    label: str,
) -> dict[str, float]:
    initial = settings.initial_bankroll
    if trades.empty:
        return {
            "staking": label,
            "trades": 0,
            "hit_rate": 0.0,
            "total_pnl": 0.0,
            "final_equity": initial,
            "roi_pct": 0.0,
            "kelly_fraction": settings.kelly_fraction,
            "min_edge": settings.min_edge,
            "max_card_risk_pct": settings.max_card_risk_fraction * 100,
        }

    final = float(trades["equity"].iloc[-1]) if "equity" in trades.columns else initial + trades["pnl"].sum()
    summary = {
        "staking": label,
        "trades": len(trades),
        "hit_rate": float(trades["won"].mean()),
        "total_pnl": float(trades["pnl"].sum()),
        "final_equity": final,
        "roi_pct": float((final - initial) / initial * 100),
        "avg_stake": float(trades["stake"].mean()),
        "max_stake": float(trades["stake"].max()),
        "kelly_fraction": settings.kelly_fraction,
        "min_edge": settings.min_edge,
        "max_card_risk_pct": settings.max_card_risk_fraction * 100,
    }
    if "equity" in trades.columns:
        peak = trades["equity"].cummax()
        dd = (peak - trades["equity"]) / peak.replace(0, np.nan)
        summary["max_drawdown_pct"] = float(dd.max() * 100) if dd.notna().any() else 0.0
    if "won" in trades.columns:
        best = cur = 0
        for w in trades["won"].astype(int):
            cur = cur + 1 if w else 0
            best = max(best, cur)
        summary["max_win_streak"] = float(best)
    if summary["roi_pct"] > 500:
        summary["roi_warning"] = (
            f"ROI {summary['roi_pct']:.1f}% looks unrealistically high — verify odds and sizing."
        )
    return summary


def simulate_bankroll_bets(
    predictions: pd.DataFrame,
    settings: BankrollSettings,
    *,
    date_column: str = "event_date",
    target_column: str = "f1_win",
) -> tuple[pd.DataFrame, dict[str, float]]:
    """
    Chronological backtest with fractional Kelly + bankroll guardrails.
    Only bets when closing odds exist and edge clears min_edge.
    """
    manager = BankrollManager(settings)
    rows: list[dict[str, Any]] = []
    pending_event: str | None = None
    pending_bets: list[dict[str, Any]] = []

    work = predictions.copy()
    if date_column in work.columns:
        work[date_column] = pd.to_datetime(work[date_column], errors="coerce")
        sort_cols = [date_column]
        if "event_name" in work.columns:
            sort_cols.append("event_name")
        work = work.sort_values(sort_cols)

    def _flush_event() -> None:
        nonlocal pending_event, pending_bets
        if not pending_bets:
            return
        stakes = [b["stake"] for b in pending_bets]
        capped, _ = _apply_card_cap(stakes, manager.bankroll, settings.max_card_risk_fraction)
        for bet, stake in zip(pending_bets, capped):
            if stake <= 0:
                continue
            pnl = manager.record_bet(stake, bet["won"], bet["odds"])
            rows.append({**bet, "stake": stake, "pnl": pnl, "equity": manager.bankroll, "staking": "kelly"})
        pending_bets = []
        pending_event = None

    for _, row in work.iterrows():
        market = market_probs(row)
        if market is None:
            continue

        market_p1, market_p2 = market
        model_p1 = float(row["prob_f1_win"])
        model_p2 = float(row.get("prob_f2_win", 1.0 - model_p1))
        decimal = fight_decimal_odds(row)
        if decimal is None:
            continue
        dec_f1, dec_f2 = decimal

        edge_info = compute_edge(model_p1, model_p2, market_p1, market_p2)
        if edge_info["edge"] < settings.min_edge:
            continue

        bet_day = row.get(date_column)
        manager._sync_day(bet_day)

        if edge_info["bet_side"] == "f1":
            prob, odds = model_p1, dec_f1
        else:
            prob, odds = model_p2, dec_f2

        stake = manager.compute_stake(
            prob=prob,
            decimal_odds=odds,
            edge=edge_info["edge"],
            bet_day=bet_day,
        )
        if stake <= 0:
            continue

        actual = row.get(target_column)
        if pd.isna(actual):
            continue
        won = (edge_info["bet_side"] == "f1" and int(actual) == 1) or (
            edge_info["bet_side"] == "f2" and int(actual) == 0
        )

        event_key = str(row.get("event_name", row.get("event", "")))
        if pending_event and event_key != pending_event:
            _flush_event()
        pending_event = event_key

        pending_bets.append(
            {
                date_column: bet_day,
                "event_name": event_key,
                "bet_side": edge_info["bet_side"],
                "edge": edge_info["edge"],
                "edge_pct": edge_info["edge"] * 100.0,
                "stake": stake,
                "stake_pct_bankroll": stake / max(manager.state.day_start_bankroll, 1) * 100,
                "kelly_fraction_used": settings.kelly_fraction,
                "odds": odds,
                "won": won,
                "daily_pnl": manager.state.daily_pnl,
                "halted_today": int(manager.state.halted_today),
            }
        )

    _flush_event()
    trades = pd.DataFrame(rows)
    label = f"kelly_{settings.kelly_fraction:.2f}"
    summary = _summarize_trades(trades, settings, label=label)
    return trades, summary


def simulate_bankroll_bets_dynamic(
    predictions: pd.DataFrame,
    settings: BankrollSettings,
    *,
    date_column: str = "event_date",
    target_column: str = "f1_win",
    profile: str | None = None,
) -> tuple[pd.DataFrame, dict[str, float]]:
    """
    Chronological backtest with per-bet dynamic min_edge (bankroll, form, confidence, time).
    """
    from ufc_betting_bot.modules.dynamic_thresholds import (
        get_profile_thresholds,
        model_confidence_from_prob,
        recent_win_rate_from_trades,
    )

    manager = BankrollManager(settings)
    rows: list[dict[str, Any]] = []
    pending_event: str | None = None
    pending_bets: list[dict[str, Any]] = []
    recent_outcomes: list[bool] = []

    work = predictions.copy()
    if date_column in work.columns:
        work[date_column] = pd.to_datetime(work[date_column], errors="coerce")
        sort_cols = [date_column]
        if "event_name" in work.columns:
            sort_cols.append("event_name")
        work = work.sort_values(sort_cols)

    def _flush_event() -> None:
        nonlocal pending_event, pending_bets
        if not pending_bets:
            return
        stakes = [b["stake"] for b in pending_bets]
        capped, _ = _apply_card_cap(stakes, manager.bankroll, settings.max_card_risk_fraction)
        for bet, stake in zip(pending_bets, capped):
            if stake <= 0:
                continue
            pnl = manager.record_bet(stake, bet["won"], bet["odds"])
            recent_outcomes.append(bool(bet["won"]))
            rows.append(
                {
                    **bet,
                    "stake": stake,
                    "pnl": pnl,
                    "equity": manager.bankroll,
                    "staking": "kelly_dynamic",
                }
            )
        pending_bets = []
        pending_event = None

    for _, row in work.iterrows():
        market = market_probs(row)
        if market is None:
            continue

        market_p1, market_p2 = market
        model_p1 = float(row["prob_f1_win"])
        model_p2 = float(row.get("prob_f2_win", 1.0 - model_p1))
        decimal = fight_decimal_odds(row)
        if decimal is None:
            continue
        dec_f1, dec_f2 = decimal

        edge_info = compute_edge(model_p1, model_p2, market_p1, market_p2)
        wr = recent_win_rate_from_trades(recent_outcomes)
        pick_prob = model_p1 if edge_info["bet_side"] == "f1" else model_p2
        conf = model_confidence_from_prob(pick_prob)
        thresholds = get_profile_thresholds(
            manager.bankroll,
            wr,
            conf,
            hours_to_event=12.0,
            profile=profile,
        )
        dyn_min_edge = thresholds.alert_min_edge
        if edge_info["edge"] < dyn_min_edge:
            continue

        bet_day = row.get(date_column)
        manager._sync_day(bet_day)

        if edge_info["bet_side"] == "f1":
            prob, odds = model_p1, dec_f1
        else:
            prob, odds = model_p2, dec_f2

        saved_min_edge = manager.settings.min_edge
        manager.settings.min_edge = dyn_min_edge
        try:
            stake = manager.compute_stake(
                prob=prob,
                decimal_odds=odds,
                edge=edge_info["edge"],
                bet_day=bet_day,
            )
        finally:
            manager.settings.min_edge = saved_min_edge
        if stake <= 0:
            continue

        actual = row.get(target_column)
        if pd.isna(actual):
            continue
        won = (edge_info["bet_side"] == "f1" and int(actual) == 1) or (
            edge_info["bet_side"] == "f2" and int(actual) == 0
        )

        event_key = str(row.get("event_name", row.get("event", "")))
        if pending_event and event_key != pending_event:
            _flush_event()
        pending_event = event_key

        pending_bets.append(
            {
                date_column: bet_day,
                "event_name": event_key,
                "bet_side": edge_info["bet_side"],
                "edge": edge_info["edge"],
                "edge_pct": edge_info["edge"] * 100.0,
                "min_edge_used": thresholds.alert_min_edge,
                "stake": stake,
                "stake_pct_bankroll": stake / max(manager.state.day_start_bankroll, 1) * 100,
                "kelly_fraction_used": settings.kelly_fraction,
                "odds": odds,
                "won": won,
                "daily_pnl": manager.state.daily_pnl,
                "halted_today": int(manager.state.halted_today),
            }
        )

    _flush_event()
    trades = pd.DataFrame(rows)
    summary = _summarize_trades(trades, settings, label="kelly_dynamic")
    summary["mode"] = "dynamic"
    return trades, summary


def sweep_bankroll_thresholds(
    predictions: pd.DataFrame,
    base_settings: BankrollSettings,
    thresholds: list[float],
) -> pd.DataFrame:
    rows: list[dict[str, float]] = []
    for edge in thresholds:
        cfg = BankrollSettings(
            initial_bankroll=base_settings.initial_bankroll,
            kelly_fraction=base_settings.kelly_fraction,
            max_bet_fraction=base_settings.max_bet_fraction,
            min_bet_fraction=base_settings.min_bet_fraction,
            max_card_risk_fraction=base_settings.max_card_risk_fraction,
            daily_loss_limit_fraction=base_settings.daily_loss_limit_fraction,
            min_edge=edge,
            flat_stake=base_settings.flat_stake,
        )
        _, summary = simulate_bankroll_bets(predictions, cfg)
        summary["min_edge"] = edge
        rows.append(summary)
    return pd.DataFrame(rows)


def simulate_all_staking_modes(
    predictions: pd.DataFrame,
    base_settings: BankrollSettings,
) -> pd.DataFrame:
    """Flat, quarter-Kelly, and half-Kelly summaries side-by-side."""
    modes: list[dict[str, float]] = []
    _, flat = simulate_flat_bets(predictions, base_settings)
    modes.append(flat)

    for label, frac in (("quarter_kelly", 0.25), ("half_kelly", 0.5)):
        cfg = BankrollSettings(
            initial_bankroll=base_settings.initial_bankroll,
            kelly_fraction=frac,
            max_bet_fraction=base_settings.max_bet_fraction,
            min_bet_fraction=base_settings.min_bet_fraction,
            max_card_risk_fraction=base_settings.max_card_risk_fraction,
            daily_loss_limit_fraction=base_settings.daily_loss_limit_fraction,
            min_edge=base_settings.min_edge,
            flat_stake=base_settings.flat_stake,
        )
        _, summary = simulate_bankroll_bets(predictions, cfg)
        summary["staking"] = label
        modes.append(summary)
    return pd.DataFrame(modes)
