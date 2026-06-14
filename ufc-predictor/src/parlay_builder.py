"""Same-card parlay builder with optional dynamic thresholds."""

from __future__ import annotations

from typing import Any

import pandas as pd

from src.strategy import ParlayCandidate, StrategyConfig, build_parlay_candidates, strategy_from_profile
from ufc_betting_bot.modules.dynamic_thresholds import (
    ThresholdResult,
    get_profile_thresholds,
    hours_to_event_from_row,
    model_confidence_from_predictions,
    recent_win_rate_from_trades,
)


def parlay_max_legs_for_profile() -> int:
    """Research allows up to 5-leg same-card parlays; live stays at 3."""
    import config as _cfg

    return 3 if _cfg.is_live_profile() else 5


def _with_profile_parlay_legs(strat: StrategyConfig) -> StrategyConfig:
    strat.parlay_max_legs = parlay_max_legs_for_profile()
    return strat


def _fighter_names_from_row(row: pd.Series) -> tuple[str, str]:
    f1 = str(row.get("fighter_1", row.get("fighter1_name", row.get("fighter1", "")))).strip()
    f2 = str(row.get("fighter_2", row.get("fighter2_name", row.get("fighter2", "")))).strip()
    return f1, f2


def build_fight_lookup(preds: pd.DataFrame) -> dict[str, pd.Series]:
    """Map fight_id → prediction row for parlay leg name enrichment."""
    lookup: dict[str, pd.Series] = {}
    if preds is None or preds.empty:
        return lookup
    for _, row in preds.iterrows():
        fid = str(row.get("fight_id", "")).strip()
        if fid:
            lookup[fid] = row
    return lookup


def enrich_parlay_leg_from_row(leg: dict[str, Any], row: pd.Series) -> dict[str, Any]:
    """Fill fighter1_name, fighter2_name, pick_name, winner_name on a parlay leg."""
    out = dict(leg)
    f1, f2 = _fighter_names_from_row(row)
    side = str(out.get("side", out.get("bet_side", ""))).strip().lower()
    winner = str(out.get("winner_name", out.get("pick_name", ""))).strip()
    if not winner and f1 and f2:
        winner = f1 if side == "f1" else f2 if side == "f2" else ""
    if not winner:
        winner = str(row.get("predicted_winner", "")).strip()
    out["fighter1_name"] = f1 or str(out.get("fighter1_name", "")).strip()
    out["fighter2_name"] = f2 or str(out.get("fighter2_name", "")).strip()
    out["pick_name"] = winner or out.get("pick_name", "")
    out["winner_name"] = out["pick_name"]
    if "odds" not in out or not out.get("odds"):
        pick = out["pick_name"]
        if pick == f1 and row.get("f1_odds") is not None:
            out["odds"] = float(row["f1_odds"])
        elif pick == f2 and row.get("f2_odds") is not None:
            out["odds"] = float(row["f2_odds"])
    return out


def enrich_parlays_for_display(
    parlays: list[dict[str, Any]],
    preds: pd.DataFrame | None,
) -> list[dict[str, Any]]:
    """Resolve fighter names on cached/stale parlay legs using live prediction rows."""
    lookup = build_fight_lookup(preds) if preds is not None else {}
    enriched: list[dict[str, Any]] = []
    for parlay in parlays:
        item = dict(parlay)
        legs_out: list[dict[str, Any]] = []
        for leg in item.get("legs", []):
            leg_d = dict(leg) if isinstance(leg, dict) else {}
            fid = str(leg_d.get("fight_id", "")).strip()
            if fid and fid in lookup:
                leg_d = enrich_parlay_leg_from_row(leg_d, lookup[fid])
            legs_out.append(leg_d)
        item["legs"] = legs_out
        item.pop("leg_labels", None)
        item.pop("leg_lines", None)
        item.pop("picks", None)
        enriched.append(item)
    return enriched


def decimal_to_american(decimal_odds: float) -> str:
    """Format decimal odds as American moneyline (e.g. +450, -110)."""
    if not decimal_odds or decimal_odds <= 1:
        return "EVEN"
    if decimal_odds >= 2.0:
        return f"+{int(round((decimal_odds - 1.0) * 100))}"
    return f"{int(round(-100.0 / (decimal_odds - 1.0)))}"


def leg_betnow_label(leg: Any, *, sport: str = "UFC") -> str:
    """
    BetNow slip style:
    Michael Chandler (+450) (vs Mauricio Ruffy) (UFC)
    """
    if isinstance(leg, dict):
        pick = str(leg.get("winner_name", leg.get("pick_name", ""))).strip()
        f1 = str(leg.get("fighter1_name", "")).strip()
        f2 = str(leg.get("fighter2_name", "")).strip()
        side = str(leg.get("side", leg.get("bet_side", ""))).strip().lower()
        odds = float(leg.get("odds", leg.get("decimal_odds", 0)) or 0)
    else:
        pick = str(getattr(leg, "winner_name", "") or getattr(leg, "pick_name", "")).strip()
        f1 = str(getattr(leg, "fighter1_name", "")).strip()
        f2 = str(getattr(leg, "fighter2_name", "")).strip()
        side = str(getattr(leg, "bet_side", "")).strip().lower()
        odds = float(getattr(leg, "decimal_odds", 0) or 0)

    if not pick and f1 and f2:
        pick = f1 if side == "f1" else f2 if side == "f2" else ""
    if pick and f1 and f2:
        opponent = f2 if pick == f1 else f1
        if odds > 1:
            return f"{pick} ({decimal_to_american(odds)}) (vs {opponent}) ({sport})"
        return f"{pick} (vs {opponent}) ({sport})"
    return leg_pick_label(leg)


def leg_pick_label(leg: Any) -> str:
    """Human-readable pick line, e.g. 'Michael Chandler over Mauricio Ruffy'."""
    if isinstance(leg, dict):
        pick = str(leg.get("winner_name", leg.get("pick_name", ""))).strip()
        f1 = str(leg.get("fighter1_name", "")).strip()
        f2 = str(leg.get("fighter2_name", "")).strip()
        side = str(leg.get("side", leg.get("bet_side", ""))).strip().lower()
        if not pick and f1 and f2:
            pick = f1 if side == "f1" else f2 if side == "f2" else ""
        if pick and f1 and f2:
            opponent = f2 if pick == f1 else f1
            return f"{pick} over {opponent}"
        if f1 and f2:
            return f"{f1} vs {f2}"
        if pick:
            return pick
        if side in ("f1", "f2"):
            return f"Fighter {side[-1]}"
        return side.upper() or "Leg"

    pick = str(getattr(leg, "winner_name", "") or getattr(leg, "pick_name", "")).strip()
    f1 = str(getattr(leg, "fighter1_name", "")).strip()
    f2 = str(getattr(leg, "fighter2_name", "")).strip()
    side = str(getattr(leg, "bet_side", "")).strip().lower()
    if not pick and f1 and f2:
        pick = f1 if side == "f1" else f2 if side == "f2" else ""
    if pick and f1 and f2:
        opponent = f2 if pick == f1 else f1
        return f"{pick} over {opponent}"
    if f1 and f2:
        return f"{f1} vs {f2}"
    return pick or (f"Fighter {side[-1]}" if side in ("f1", "f2") else side.upper() or "Leg")


def leg_stats_suffix(leg: Any) -> str:
    if isinstance(leg, dict):
        odds = float(leg.get("odds", leg.get("decimal_odds", 0)) or 0)
        prob = float(leg.get("prob", 0) or 0)
        edge = float(leg.get("edge", 0) or 0)
    else:
        odds = float(getattr(leg, "decimal_odds", 0) or 0)
        prob = float(getattr(leg, "prob", 0) or 0)
        edge = float(getattr(leg, "edge", 0) or 0)
    return f"@ {odds:.2f} (prob {prob:.0%}, edge {edge:+.1%})"


def parlay_to_display_dict(
    parlay: ParlayCandidate,
    *,
    rank: int,
    event_name: str = "",
) -> dict[str, Any]:
    """Serialize a ranked parlay for dashboard / alert display."""
    leg_labels: list[str] = []
    leg_lines: list[str] = []
    for leg in parlay.legs:
        label = leg_pick_label(leg)
        betnow = leg_betnow_label(leg)
        leg_labels.append(label)
        leg_lines.append(betnow if betnow != label else f"{label} {leg_stats_suffix(leg)}")
    return {
        "rank": rank,
        "event_name": event_name,
        "legs": [
            {
                "fight_id": c.fight_id,
                "side": c.bet_side,
                "edge": c.edge,
                "prob": c.prob,
                "odds": c.decimal_odds,
                "fighter1_name": c.fighter1_name,
                "fighter2_name": c.fighter2_name,
                "pick_name": c.pick_name,
                "winner_name": c.winner_name or c.pick_name,
            }
            for c in parlay.legs
        ],
        "n_legs": len(parlay.legs),
        "combined_prob": parlay.combined_prob,
        "combined_odds": parlay.combined_odds,
        "expected_value": parlay.expected_value,
        "min_leg_edge": parlay.min_leg_edge,
        "avg_leg_edge": sum(c.edge for c in parlay.legs) / len(parlay.legs),
        "leg_labels": leg_labels,
        "leg_lines": leg_lines,
        "picks": " + ".join(leg_labels),
    }


def rank_parlays_by_ev(
    parlays: list[ParlayCandidate],
    *,
    min_ev: float,
    event_name: str = "",
    max_results: int | None = None,
) -> list[dict[str, Any]]:
    """Filter by min EV, sort descending, assign rank #1, #2, …"""
    import config as _cfg

    qualified = [p for p in parlays if p.expected_value >= min_ev]
    qualified.sort(key=lambda p: p.expected_value, reverse=True)
    cap = max_results if max_results is not None else _cfg.ALERT_MAX_PARLAYS
    return [
        parlay_to_display_dict(p, rank=i + 1, event_name=event_name)
        for i, p in enumerate(qualified[:cap])
    ]


def format_recommended_parlay_header(p: dict[str, Any]) -> str:
    rank = p.get("rank", 0)
    combined_dec = float(p.get("combined_odds", 0) or 0)
    odds_txt = (
        f"{decimal_to_american(combined_dec)} ({combined_dec:.2f})"
        if combined_dec > 1
        else f"{combined_dec:.2f}"
    )
    return (
        f"Recommended Parlay #{rank}  |  {p.get('n_legs', 0)}-Team  |  "
        f"prob {p.get('combined_prob', 0):.0%}  |  "
        f"odds {odds_txt}  |  "
        f"EV {p.get('expected_value', 0):+.0%}  |  "
        f"min leg edge {p.get('min_leg_edge', 0):.1%}"
    )


def format_recommended_parlay_legs(p: dict[str, Any]) -> list[str]:
    """Numbered BetNow-style leg lines for the dashboard."""
    legs = p.get("legs") or []
    return [f"{i}. {leg_betnow_label(leg)}" for i, leg in enumerate(legs, 1)]


def strategy_from_thresholds(thresholds: ThresholdResult) -> StrategyConfig:
    """Map dynamic threshold result onto StrategyConfig."""
    base = strategy_from_profile()
    strat = StrategyConfig(
        kelly_fraction=base.kelly_fraction,
        max_bet_fraction=base.max_bet_fraction,
        min_bet_fraction=base.min_bet_fraction,
        max_card_risk_fraction=base.max_card_risk_fraction,
        min_edge=thresholds.alert_min_edge,
        flat_stake=base.flat_stake,
        parlay_min_edge=thresholds.parlay_min_edge,
        parlay_min_combined_prob=thresholds.parlay_min_combined_prob,
        parlay_max_legs=base.parlay_max_legs,
    )
    return _with_profile_parlay_legs(strat)


def resolve_parlay_thresholds(
    card_rows: pd.DataFrame,
    *,
    bankroll: float,
    recent_win_rate: float | None = None,
    recent_wins: list[bool] | None = None,
    hours_to_event: float | None = None,
    use_dynamic: bool | None = None,
) -> tuple[StrategyConfig, ThresholdResult | None, float]:
    """
    Return (strategy config, threshold detail, min_parlay_ev).

    When dynamic thresholds are disabled, uses static profile settings.
    """
    import config as _cfg

    enabled = _cfg.DYNAMIC_THRESHOLDS_ENABLED if use_dynamic is None else use_dynamic
    if not enabled:
        strat = strategy_from_profile()
        return _with_profile_parlay_legs(strat), None, _cfg.profile_value("parlay_min_ev")

    wr = recent_win_rate
    if wr is None and recent_wins:
        wr = recent_win_rate_from_trades(recent_wins)

    hte = hours_to_event
    if hte is None and not card_rows.empty:
        hte = hours_to_event_from_row(card_rows.iloc[0])

    conf = model_confidence_from_predictions(card_rows)
    thresholds = get_profile_thresholds(
        bankroll,
        wr,
        conf,
        hours_to_event=hte,
        profile=_cfg.UFC_PROFILE,
    )
    strat = strategy_from_thresholds(thresholds)
    return _with_profile_parlay_legs(strat), thresholds, thresholds.parlay_min_ev


def build_parlays_for_card(
    card_rows: pd.DataFrame,
    *,
    bankroll: float,
    recent_win_rate: float | None = None,
    recent_wins: list[bool] | None = None,
    hours_to_event: float | None = None,
    use_dynamic: bool | None = None,
) -> tuple[list[ParlayCandidate], StrategyConfig, ThresholdResult | None, float]:
    """Build qualified same-card parlays using static or dynamic thresholds."""
    strat, thresholds, min_ev = resolve_parlay_thresholds(
        card_rows,
        bankroll=bankroll,
        recent_win_rate=recent_win_rate,
        recent_wins=recent_wins,
        hours_to_event=hours_to_event,
        use_dynamic=use_dynamic,
    )
    candidates = build_parlay_candidates(card_rows, config=strat)
    qualified = [p for p in candidates if p.expected_value >= min_ev]
    qualified.sort(key=lambda p: p.expected_value, reverse=True)
    return qualified, strat, thresholds, min_ev


def ranked_parlays_for_card(
    card_rows: pd.DataFrame,
    *,
    bankroll: float,
    recent_win_rate: float | None = None,
    recent_wins: list[bool] | None = None,
    hours_to_event: float | None = None,
    use_dynamic: bool | None = None,
    event_name: str = "",
) -> list[dict[str, Any]]:
    """Same-card parlays ranked by EV (#1 = highest) for dashboard display."""
    qualified, _, _, min_ev = build_parlays_for_card(
        card_rows,
        bankroll=bankroll,
        recent_win_rate=recent_win_rate,
        recent_wins=recent_wins,
        hours_to_event=hours_to_event,
        use_dynamic=use_dynamic,
    )
    ev_name = event_name
    if not ev_name and "event_name" in card_rows.columns and card_rows["event_name"].notna().any():
        ev_name = str(card_rows["event_name"].dropna().iloc[0])
    return rank_parlays_by_ev(qualified, min_ev=min_ev, event_name=ev_name)


def threshold_context_for_alerts(
    predictions_df: pd.DataFrame,
    *,
    bankroll: float,
    recent_wins: list[bool] | None = None,
    use_dynamic: bool | None = None,
) -> dict[str, Any]:
    """Shared threshold resolution for singles + parlays in alerts."""
    import config as _cfg

    enabled = _cfg.DYNAMIC_THRESHOLDS_ENABLED if use_dynamic is None else use_dynamic
    if not enabled:
        ps = _cfg.profile_settings()
        strat = _with_profile_parlay_legs(strategy_from_profile())
        return {
            "use_dynamic": False,
            "min_edge": ps["alert_min_edge"],
            "min_parlay_ev": ps["parlay_min_ev"],
            "strategy": strat,
            "thresholds": None,
            "parlay_max_legs": parlay_max_legs_for_profile(),
        }

    wr = recent_win_rate_from_trades(recent_wins) if recent_wins else None
    hte = None
    if not predictions_df.empty:
        hte = hours_to_event_from_row(predictions_df.iloc[0])
    conf = model_confidence_from_predictions(predictions_df)
    thresholds = get_profile_thresholds(
        bankroll,
        wr,
        conf,
        hours_to_event=hte,
        profile=_cfg.UFC_PROFILE,
    )
    strat = strategy_from_thresholds(thresholds)
    return {
        "use_dynamic": True,
        "min_edge": thresholds.alert_min_edge,
        "min_parlay_ev": thresholds.parlay_min_ev,
        "strategy": _with_profile_parlay_legs(strat),
        "thresholds": thresholds.as_dict(),
        "model_confidence": conf,
        "recent_win_rate": wr,
        "hours_to_event": hte,
        "parlay_max_legs": parlay_max_legs_for_profile(),
    }
