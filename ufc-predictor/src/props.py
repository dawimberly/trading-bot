"""UFC prop betting: method, rounds, decision markets."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

import numpy as np
import pandas as pd

import config
from src.strategy import BetCandidate, StrategyConfig, bet_expected_value


PROP_MARKET_LABELS: dict[str, str] = {
    "goes_to_decision": "Goes to Decision",
    "finish": "Fight Ends Inside Distance",
    "ko_tko": "KO/TKO",
    "submission": "Submission",
    "round_1_finish": "Round 1 Finish",
    "over_1_5_rounds": "Over 1.5 Rounds",
    "fighter_ko": "Fighter Wins by KO/TKO",
    "fighter_sub": "Fighter Wins by Submission",
}


def method_flags(method: Any) -> tuple[int, int, int]:
    """Return (is_ko, is_sub, is_dec) from a method string."""
    text = str(method or "").upper()
    is_ko = int("KO" in text or "TKO" in text)
    is_sub = int("SUB" in text)
    is_dec = int("DEC" in text or "DECISION" in text)
    if not is_ko and not is_sub and not is_dec and text.strip():
        is_dec = 1
    return is_ko, is_sub, is_dec


def _clip_prob(p: float, lo: float = 0.03, hi: float = 0.97) -> float:
    if not np.isfinite(p):
        return lo
    return float(np.clip(p, lo, hi))


def _fighter_rate(row: pd.Series, prefix: str, key: str, default: float) -> float:
    for col in (f"{prefix}_{key}", f"f1_{key}" if prefix == "f1" else f"f2_{key}"):
        if col in row.index and pd.notna(row.get(col)):
            return float(row[col])
    return default


def method_probs_from_row(row: pd.Series) -> dict[str, float]:
    """
    Estimate fight-level method probabilities from rolling fighter stats.

    Returns keys: ko, sub, dec, round_1_finish, over_1_5_rounds,
    fighter_ko, fighter_sub (conditional on model pick).
    """
    f1_ko = _clip_prob(_fighter_rate(row, "f1", "ko_rate", 0.18), 0.05, 0.55)
    f2_ko = _clip_prob(_fighter_rate(row, "f2", "ko_rate", 0.18), 0.05, 0.55)
    f1_sub = _clip_prob(_fighter_rate(row, "f1", "sub_avg", 0.35) / 2.5, 0.03, 0.40)
    f2_sub = _clip_prob(_fighter_rate(row, "f2", "sub_avg", 0.35) / 2.5, 0.03, 0.40)
    f1_finish = _clip_prob(_fighter_rate(row, "f1", "finish_rate", 0.45), 0.10, 0.80)
    f2_finish = _clip_prob(_fighter_rate(row, "f2", "finish_rate", 0.45), 0.10, 0.80)

    ko_diff = float(row.get("ko_rate_diff", 0) or 0)
    sub_diff = float(row.get("sub_avg_diff", 0) or 0)
    striker_diff = float(row.get("striker_score_diff", 0) or 0)

    p_ko = _clip_prob(0.5 * (f1_ko + f2_ko) + 0.15 * ko_diff + 0.08 * striker_diff, 0.08, 0.62)
    p_sub = _clip_prob(0.5 * (f1_sub + f2_sub) + 0.12 * sub_diff, 0.05, 0.45)
    p_dec = _clip_prob(1.0 - p_ko - p_sub, 0.12, 0.75)
    total = p_ko + p_sub + p_dec
    p_ko, p_sub, p_dec = p_ko / total, p_sub / total, p_dec / total

    avg_finish = 0.5 * (f1_finish + f2_finish)
    p_r1 = _clip_prob(avg_finish * 0.58 * (1.0 + 0.25 * abs(ko_diff)), 0.08, 0.55)
    p_over_15 = _clip_prob(1.0 - p_r1, 0.25, 0.92)
    p_finish = _clip_prob(p_ko + p_sub, 0.15, 0.88)

    p1 = float(row.get("prob_f1_win", row.get("predicted_prob", 0.5)) or 0.5)
    if pd.notna(row.get("prob_f2_win")):
        p2 = float(row["prob_f2_win"])
    else:
        p2 = 1.0 - p1
    pick_side = "f1" if p1 >= p2 else "f2"
    pick_prob = p1 if pick_side == "f1" else p2
    pick_ko = f1_ko if pick_side == "f1" else f2_ko
    pick_sub = f1_sub if pick_side == "f1" else f2_sub
    pick_dec = max(0.05, 1.0 - pick_ko - pick_sub)
    pick_total = pick_ko + pick_sub + pick_dec
    pick_ko /= pick_total
    pick_sub /= pick_total

    f1 = str(row.get("fighter_1", row.get("fighter1", ""))).strip()
    f2 = str(row.get("fighter_2", row.get("fighter2", ""))).strip()
    pick_name = f1 if pick_side == "f1" else f2

    return {
        "ko": p_ko,
        "sub": p_sub,
        "dec": p_dec,
        "finish": p_finish,
        "round_1_finish": p_r1,
        "over_1_5_rounds": p_over_15,
        "fighter_ko": _clip_prob(pick_prob * pick_ko, 0.03, 0.55),
        "fighter_sub": _clip_prob(pick_prob * pick_sub, 0.02, 0.40),
        "pick_side": pick_side,
        "pick_name": pick_name,
        "pick_prob": pick_prob,
    }


def prop_model_prob(prop_key: str, row: pd.Series, probs: dict[str, float] | None = None) -> float:
    """Model probability for a supported prop market."""
    p = probs or method_probs_from_row(row)
    mapping = {
        "goes_to_decision": p["dec"],
        "finish": p["finish"],
        "ko_tko": p["ko"],
        "submission": p["sub"],
        "round_1_finish": p["round_1_finish"],
        "over_1_5_rounds": p["over_1_5_rounds"],
        "fighter_ko": p["fighter_ko"],
        "fighter_sub": p["fighter_sub"],
    }
    return float(mapping.get(prop_key, 0.0))


def synthetic_market_odds(model_prob: float, *, vig: float | None = None) -> float:
    """Fair decimal odds with book vig when live prop lines are unavailable."""
    v = vig if vig is not None else config.PROP_SYNTHETIC_VIG
    implied = max(model_prob, 0.02) * (1.0 + v)
    return float(max(1.05, 1.0 / implied))


def fetch_live_prop_odds(
    book: str,
    *,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Load live prop odds for a book (DraftKings / BetNow)."""
    if not config.ENABLE_PROPS:
        from src.odds_providers.prop_odds_common import empty_prop_odds_df

        return empty_prop_odds_df()
    if book == "MyBookie" and not config.MYBOOKIE_ENABLED:
        from src.odds_providers.prop_odds_common import empty_prop_odds_df

        return empty_prop_odds_df()
    try:
        if book == "DraftKings":
            from src.odds_providers.draftkings_props import fetch_draftkings_prop_odds

            return fetch_draftkings_prop_odds(force_refresh=force_refresh)
        if book == "BetNow.eu":
            from src.odds_providers.betnow_scraper import fetch_betnow_prop_odds

            return fetch_betnow_prop_odds(force_refresh=force_refresh)
        if book == "MyBookie":
            from src.odds_providers.mybookie_scraper import fetch_mybookie_prop_odds

            return fetch_mybookie_prop_odds(force_refresh=force_refresh)
    except Exception as exc:
        logger = __import__("logging").getLogger(__name__)
        logger.warning("%s live prop odds unavailable: %s", book, exc)
    from src.odds_providers.prop_odds_common import empty_prop_odds_df

    return empty_prop_odds_df()


def resolve_prop_quote(
    row: pd.Series,
    prop_key: str,
    *,
    book: str,
    prop_odds: pd.DataFrame | None = None,
    probs: dict[str, float] | None = None,
) -> dict[str, Any]:
    """
    Resolve market quote for a prop: prefer live book line, else synthetic.

    Returns decimal_odds, implied_prob, edge, odds_source, american_odds.
    """
    from src.odds_providers.prop_odds_common import lookup_prop_odds_row

    model_p = prop_model_prob(prop_key, row, probs)
    p = probs or method_probs_from_row(row)
    f1 = str(row.get("fighter_1", row.get("fighter1", ""))).strip()
    f2 = str(row.get("fighter_2", row.get("fighter2", ""))).strip()

    live_row = None
    if prop_odds is not None and not prop_odds.empty:
        live_row = lookup_prop_odds_row(f1, f2, prop_key, prop_odds)

    if live_row is not None and float(live_row.get("decimal_odds", 0) or 0) > 1:
        decimal = float(live_row["decimal_odds"])
        implied = float(live_row.get("implied_prob") or (1.0 / decimal))
        edge = model_p - implied
        return {
            "decimal_odds": decimal,
            "implied_prob": implied,
            "edge": edge,
            "odds_source": "live",
            "american_odds": live_row.get("american_odds"),
            "selection": live_row.get("selection", ""),
            "book": book,
        }

    decimal = synthetic_market_odds(model_p)
    implied = model_p * (1.0 + config.PROP_SYNTHETIC_VIG)
    edge = model_p - implied
    return {
        "decimal_odds": decimal,
        "implied_prob": implied,
        "edge": edge,
        "odds_source": "synthetic",
        "american_odds": None,
        "selection": "",
        "book": book,
    }


def prop_display_label(prop_key: str, row: pd.Series, probs: dict[str, float] | None = None) -> str:
    """Human-readable prop label for dashboard slips."""
    p = probs or method_probs_from_row(row)
    f1 = str(row.get("fighter_1", row.get("fighter1", ""))).strip()
    f2 = str(row.get("fighter_2", row.get("fighter2", ""))).strip()
    fight = f"{f1} vs {f2}"
    base = PROP_MARKET_LABELS.get(prop_key, prop_key)
    if prop_key == "fighter_ko":
        return f"{p['pick_name']} by KO/TKO ({fight})"
    if prop_key == "fighter_sub":
        return f"{p['pick_name']} by Submission ({fight})"
    return f"{base} ({fight})"


def settle_prop(prop_key: str, row: pd.Series) -> bool | None:
    """Whether a prop bet wins given actual fight outcome. None if outcome unknown."""
    method = row.get("method", row.get("METHOD"))
    rnd = row.get("round", row.get("ROUND"))
    if pd.isna(method) and pd.isna(rnd):
        return None

    ko, sub, dec = method_flags(method)
    try:
        round_num = int(float(rnd)) if pd.notna(rnd) else 0
    except (TypeError, ValueError):
        round_num = 0

    f1 = str(row.get("fighter_1", row.get("fighter1", ""))).strip()
    f2 = str(row.get("fighter_2", row.get("fighter2", ""))).strip()
    winner = str(row.get("winner", row.get("predicted_winner", ""))).strip()
    actual_f1_win = row.get(config.TARGET_COLUMN)
    if pd.isna(actual_f1_win) and winner:
        if winner == f1:
            actual_f1_win = 1
        elif winner == f2:
            actual_f1_win = 0
    pick_side = "f1" if float(row.get("prob_f1_win", 0.5) or 0.5) >= 0.5 else "f2"
    if winner:
        pick_side = "f1" if winner == f1 else "f2" if winner == f2 else pick_side

    if prop_key == "goes_to_decision":
        return bool(dec)
    if prop_key == "finish":
        return bool(ko or sub)
    if prop_key == "ko_tko":
        return bool(ko)
    if prop_key == "submission":
        return bool(sub)
    if prop_key == "round_1_finish":
        return round_num == 1 and bool(ko or sub)
    if prop_key == "over_1_5_rounds":
        return round_num > 1 or bool(dec)
    if prop_key == "fighter_ko":
        pick_won = (pick_side == "f1" and int(actual_f1_win or 0) == 1) or (
            pick_side == "f2" and int(actual_f1_win or 0) == 0
        )
        return bool(pick_won and ko)
    if prop_key == "fighter_sub":
        pick_won = (pick_side == "f1" and int(actual_f1_win or 0) == 1) or (
            pick_side == "f2" and int(actual_f1_win or 0) == 0
        )
        return bool(pick_won and sub)
    return None


def extract_prop_candidate(
    row: pd.Series,
    prop_key: str,
    *,
    config: StrategyConfig | None = None,
    min_edge: float | None = None,
    probs: dict[str, float] | None = None,
    book: str = "",
    prop_odds: pd.DataFrame | None = None,
    for_display: bool = False,
) -> BetCandidate | None:
    """Build a prop BetCandidate when model edge clears threshold (or synthetic display)."""
    if prop_key not in PROP_MARKET_LABELS:
        return None

    import config as _cfg

    edge_floor = min_edge if min_edge is not None else _cfg.PROP_MIN_EDGE
    strat = config or StrategyConfig(min_edge=edge_floor)
    p = probs or method_probs_from_row(row)
    quote = resolve_prop_quote(
        row,
        prop_key,
        book=book or "Consensus",
        prop_odds=prop_odds,
        probs=p,
    )
    model_p = prop_model_prob(prop_key, row, p)
    edge = float(quote["edge"])
    odds = float(quote["decimal_odds"])
    odds_source = str(quote.get("odds_source", "synthetic"))
    qualifies_live = odds_source == "live" and edge >= edge_floor
    qualifies_synth = (
        for_display
        and odds_source == "synthetic"
        and model_p >= _cfg.PROP_MIN_MODEL_PROB
    )
    if not qualifies_live and not qualifies_synth:
        return None

    f1 = str(row.get("fighter_1", row.get("fighter1", ""))).strip()
    f2 = str(row.get("fighter_2", row.get("fighter2", ""))).strip()
    label = prop_display_label(prop_key, row, p)

    cand = BetCandidate(
        fight_id=str(row.get("fight_id", "")),
        event_key=str(row.get("event_name", row.get("event", ""))),
        bet_side=p.get("pick_side", "f1"),
        prob=model_p,
        decimal_odds=odds,
        edge=edge,
        kelly_full=0.0,
        expected_value=bet_expected_value(model_p, odds),
        fighter1_name=f1,
        fighter2_name=f2,
        pick_name=label,
        winner_name=label,
        market_type="prop",
        prop_key=prop_key,
        display_label=label,
    )
    cand.odds_source = odds_source
    return cand


def extract_prop_candidates_for_row(
    row: pd.Series,
    *,
    strategy: StrategyConfig | None = None,
    markets: list[str] | None = None,
    book: str = "",
    prop_odds: pd.DataFrame | None = None,
    for_display: bool = False,
) -> list[BetCandidate]:
    """All qualifying prop singles for one fight row."""
    if not config.ENABLE_PROPS:
        return []
    keys = markets or config.PROP_MARKETS
    probs = method_probs_from_row(row)
    out: list[BetCandidate] = []
    for key in keys:
        cand = extract_prop_candidate(
            row,
            key,
            config=strategy,
            probs=probs,
            book=book,
            prop_odds=prop_odds,
            for_display=for_display,
        )
        if cand is not None:
            out.append(cand)
    return out


def enrich_predictions_with_props(
    df: pd.DataFrame,
    *,
    book: str = "",
    prop_odds: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Attach prop probability and live/synthetic quote columns for dashboard tables."""
    if not config.ENABLE_PROPS or df.empty:
        return df
    from src.odds_providers.prop_odds_common import attach_prop_odds_to_predictions

    odds_df = prop_odds if prop_odds is not None else pd.DataFrame()
    out = attach_prop_odds_to_predictions(df, odds_df)
    for key in config.PROP_MARKETS:
        col = f"prop_prob_{key}"
        out[col] = out.apply(lambda r: prop_model_prob(key, r), axis=1)
        quote_col = f"prop_odds_source_{key}"
        if quote_col not in out.columns:
            out[quote_col] = ""
        for idx, row in out.iterrows():
            if str(out.at[idx, quote_col]).strip():
                continue
            quote = resolve_prop_quote(row, key, book=book, prop_odds=prop_odds)
            out.at[idx, f"prop_odds_{key}"] = quote["decimal_odds"]
            out.at[idx, f"prop_edge_{key}"] = quote["edge"]
            out.at[idx, quote_col] = quote["odds_source"]
    return out


def _prop_fighter_name(prop_key: str, row: pd.Series, probs: dict[str, float] | None = None) -> str:
    """Fighter column for prop table — pick name for fighter-specific markets."""
    if prop_key in ("fighter_ko", "fighter_sub", "fighter_decision"):
        p = probs or method_probs_from_row(row)
        return str(p.get("pick_name", "—"))
    return "—"


def _prop_row_dict(
    cand: BetCandidate,
    *,
    rank: int,
    book: str,
    row: pd.Series,
    probs: dict[str, float],
    strict_qualified: bool,
) -> dict[str, Any]:
    odds_source = getattr(cand, "odds_source", "synthetic")
    edge_pct = cand.edge * 100.0 if odds_source == "live" else None
    prop_key = cand.prop_key
    return {
        "rank": rank,
        "book": book,
        "fight_id": cand.fight_id,
        "fight": f"{cand.fighter1_name} vs {cand.fighter2_name}",
        "prop_key": prop_key,
        "prop_type": PROP_MARKET_LABELS.get(prop_key, prop_key),
        "fighter": _prop_fighter_name(prop_key, row, probs),
        "label": cand.display_label or cand.pick_name,
        "prob": cand.prob,
        "odds": cand.decimal_odds,
        "edge": cand.edge,
        "edge_pct": edge_pct,
        "ev": cand.expected_value,
        "market_type": "prop",
        "odds_source": odds_source,
        "strict_qualified": strict_qualified,
        "parlay_allowed": config.BOOK_PROP_RULES.get(book, {}).get("allow_prop_parlays", False),
    }


def _collect_prop_candidates(
    rows: pd.DataFrame,
    *,
    book: str,
    strategy: StrategyConfig | None,
    prop_odds: pd.DataFrame | None,
    include_relaxed: bool,
) -> tuple[list[tuple[BetCandidate, pd.Series, dict[str, float], bool]], set[str]]:
    """Return (candidate, row, probs, strict) tuples and seen keys for dedupe."""
    collected: list[tuple[BetCandidate, pd.Series, dict[str, float], bool]] = []
    seen: set[str] = set()

    for _, row in rows.iterrows():
        probs = method_probs_from_row(row)
        for key in config.PROP_MARKETS:
            cand = extract_prop_candidate(
                row,
                key,
                config=strategy,
                probs=probs,
                book=book,
                prop_odds=prop_odds,
                for_display=True,
            )
            if cand is not None:
                dedupe = f"{cand.fight_id}|{key}"
                if dedupe not in seen:
                    seen.add(dedupe)
                    collected.append((cand, row, probs, True))
                continue
            if not include_relaxed:
                continue
            quote = resolve_prop_quote(row, key, book=book, prop_odds=prop_odds, probs=probs)
            model_p = prop_model_prob(key, row, probs)
            if str(quote.get("odds_source", "synthetic")) != "synthetic":
                continue
            if model_p < config.PROP_SHOW_ALL_MIN_PROB:
                continue
            dedupe = f"{row.get('fight_id', '')}|{key}"
            if dedupe in seen:
                continue
            seen.add(dedupe)
            f1 = str(row.get("fighter_1", row.get("fighter1", ""))).strip()
            f2 = str(row.get("fighter_2", row.get("fighter2", ""))).strip()
            label = prop_display_label(key, row, probs)
            relaxed = BetCandidate(
                fight_id=str(row.get("fight_id", "")),
                event_key=str(row.get("event_name", row.get("event", ""))),
                bet_side=probs.get("pick_side", "f1"),
                prob=model_p,
                decimal_odds=float(quote["decimal_odds"]),
                edge=float(quote["edge"]),
                kelly_full=0.0,
                expected_value=bet_expected_value(model_p, float(quote["decimal_odds"])),
                fighter1_name=f1,
                fighter2_name=f2,
                pick_name=label,
                winner_name=label,
                market_type="prop",
                prop_key=key,
                display_label=label,
            )
            relaxed.odds_source = "synthetic"
            collected.append((relaxed, row, probs, False))

    return collected, seen


def rank_prop_singles(
    rows: pd.DataFrame,
    *,
    book: str,
    strategy: StrategyConfig | None = None,
    max_results: int | None = None,
    prop_odds: pd.DataFrame | None = None,
    include_relaxed: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Top prop singles by edge for a book.

    Returns (ranked rows, meta) where meta has total_found and strict_count.
    """
    empty_meta = {"total_found": 0, "strict_count": 0, "relaxed_count": 0}
    if not config.ENABLE_PROPS or rows.empty:
        return [], empty_meta

    cap = max_results if max_results is not None else config.PROP_MAX_RESULTS
    collected, _ = _collect_prop_candidates(
        rows,
        book=book,
        strategy=strategy,
        prop_odds=prop_odds,
        include_relaxed=include_relaxed,
    )

    def _rank_key(item: tuple[BetCandidate, pd.Series, dict, bool]) -> tuple[int, float]:
        cand, _, _, strict = item
        source = getattr(cand, "odds_source", "synthetic")
        score = cand.edge if source == "live" else cand.prob
        return (2 if strict and source == "live" else 1 if strict else 0, score)

    collected.sort(key=_rank_key, reverse=True)
    strict_count = sum(1 for *_, strict in collected if strict)
    total_found = len(collected)

    ranked: list[dict[str, Any]] = []
    for i, (cand, row, probs, strict) in enumerate(collected[:cap], 1):
        ranked.append(
            _prop_row_dict(cand, rank=i, book=book, row=row, probs=probs, strict_qualified=strict)
        )

    meta = {
        "total_found": total_found,
        "strict_count": strict_count,
        "relaxed_count": max(0, total_found - strict_count),
        "shown": len(ranked),
        "cap": cap,
    }
    return ranked, meta


@dataclass
class PropParlayCandidate:
    legs: list[BetCandidate]
    combined_prob: float
    combined_odds: float
    expected_value: float
    min_leg_edge: float
    correlation_adjusted: bool = False


def _same_fight_groups(legs: list[BetCandidate]) -> dict[str, list[BetCandidate]]:
    groups: dict[str, list[BetCandidate]] = {}
    for leg in legs:
        groups.setdefault(leg.fight_id, []).append(leg)
    return groups


def correlated_combined_prob(legs: list[BetCandidate], row_lookup: dict[str, pd.Series]) -> float:
    """
    Adjust combined probability when legs share a fight or mix ML + prop.

    Uses joint estimates for same-fight ML+prop; independence across fights.
    """
    groups = _same_fight_groups(legs)
    joint_parts: list[float] = []

    for fight_id, group in groups.items():
        if len(group) == 1:
            joint_parts.append(group[0].prob)
            continue

        row = row_lookup.get(fight_id)
        if row is None:
            naive = float(np.prod([g.prob for g in group]))
            joint_parts.append(naive * (1.0 - config.PROP_CORRELATION_DISCOUNT))
            continue

        probs = method_probs_from_row(row)
        ml_legs = [g for g in group if g.market_type == "moneyline"]
        prop_legs = [g for g in group if g.market_type == "prop"]

        if ml_legs and prop_legs:
            ml = ml_legs[0]
            prop = prop_legs[0]
            p_win = ml.prob
            if prop.prop_key == "fighter_ko":
                joint = probs["fighter_ko"]
            elif prop.prop_key == "fighter_sub":
                joint = probs["fighter_sub"]
            elif prop.prop_key == "goes_to_decision":
                joint = p_win * probs["dec"] / max(probs["pick_prob"], 0.01)
            elif prop.prop_key == "finish":
                joint = p_win * (probs["ko"] + probs["sub"]) / max(probs["pick_prob"], 0.01)
            elif prop.prop_key in ("ko_tko", "submission", "round_1_finish", "over_1_5_rounds"):
                joint = prop.prob * 0.85
            else:
                joint = min(p_win, prop.prob)
            joint_parts.append(_clip_prob(joint))
        elif len(prop_legs) > 1:
            joint = float(np.prod([g.prob for g in prop_legs]))
            joint_parts.append(joint * (1.0 - config.PROP_CORRELATION_DISCOUNT))
        else:
            joint_parts.append(float(np.prod([g.prob for g in group])))

    return float(np.prod(joint_parts))


def build_prop_parlay_candidates(
    card_rows: pd.DataFrame,
    *,
    book: str = "DraftKings",
    strategy: StrategyConfig | None = None,
    include_moneyline: bool = True,
    prop_odds: pd.DataFrame | None = None,
) -> list[PropParlayCandidate]:
    """Same-card prop / mixed parlays for books that allow them (DraftKings)."""
    if not config.ENABLE_PROPS:
        return []

    rules = config.BOOK_PROP_RULES.get(book, {})
    if not rules.get("allow_prop_parlays"):
        return []

    from src.strategy import extract_bet_candidates

    strat = strategy or StrategyConfig(min_edge=config.PROP_MIN_EDGE)
    legs: list[BetCandidate] = []
    row_lookup: dict[str, pd.Series] = {}

    for _, row in card_rows.iterrows():
        fid = str(row.get("fight_id", ""))
        if fid:
            row_lookup[fid] = row
        legs.extend(
            extract_prop_candidates_for_row(
                row,
                strategy=strat,
                book=book,
                prop_odds=prop_odds,
                for_display=True,
            )
        )
        if include_moneyline and rules.get("allow_mixed_parlays"):
            ml = extract_bet_candidates(row, config=strat)
            if ml is not None and ml.edge >= strat.parlay_min_edge:
                legs.append(ml)

    if len(legs) < 2:
        return []

    max_legs = min(int(rules.get("max_prop_parlay_legs", 3)), len(legs))
    parlays: list[PropParlayCandidate] = []
    for n in range(2, max_legs + 1):
        for combo in combinations(legs, n):
            fight_ids = [c.fight_id for c in combo]
            if len(fight_ids) != len(set(fight_ids)):
                if not rules.get("allow_mixed_parlays"):
                    continue
            combined_prob = correlated_combined_prob(list(combo), row_lookup)
            if combined_prob < config.PROP_PARLAY_MIN_COMBINED_PROB:
                continue
            combined_odds = float(np.prod([c.decimal_odds for c in combo]))
            ev = combined_prob * (combined_odds - 1.0) - (1.0 - combined_prob)
            if ev < config.PROP_PARLAY_MIN_EV:
                continue
            parlays.append(
                PropParlayCandidate(
                    legs=list(combo),
                    combined_prob=combined_prob,
                    combined_odds=combined_odds,
                    expected_value=ev,
                    min_leg_edge=min(c.edge for c in combo),
                    correlation_adjusted=len(set(fight_ids)) < len(fight_ids),
                )
            )

    parlays.sort(key=lambda p: p.expected_value, reverse=True)
    return parlays


def prop_parlay_to_display_dict(parlay: PropParlayCandidate, *, rank: int, book: str) -> dict[str, Any]:
    """Serialize prop parlay for dashboard."""
    from src.parlay_builder import decimal_to_american, leg_betnow_label

    leg_rows = []
    for i, leg in enumerate(parlay.legs, 1):
        if leg.market_type == "prop":
            label = leg.display_label or leg.pick_name
            am_odds = decimal_to_american(leg.decimal_odds)
            leg_rows.append(f"{i}. {label} ({am_odds}) (UFC)")
        else:
            leg_rows.append(f"{i}. {leg_betnow_label(leg)}")

    return {
        "rank": rank,
        "book": book,
        "n_legs": len(parlay.legs),
        "combined_prob": parlay.combined_prob,
        "combined_odds": parlay.combined_odds,
        "expected_value": parlay.expected_value,
        "min_leg_edge": parlay.min_leg_edge,
        "correlation_adjusted": parlay.correlation_adjusted,
        "legs": [
            {
                "fight_id": c.fight_id,
                "market_type": c.market_type,
                "prop_key": c.prop_key,
                "label": c.display_label or c.pick_name,
                "prob": c.prob,
                "odds": c.decimal_odds,
                "edge": c.edge,
            }
            for c in parlay.legs
        ],
        "_leg_rows": leg_rows,
    }


def rank_prop_parlays_for_card(
    card_rows: pd.DataFrame,
    *,
    book: str = "DraftKings",
    strategy: StrategyConfig | None = None,
    max_results: int = 5,
    prop_odds: pd.DataFrame | None = None,
) -> list[dict[str, Any]]:
    parlays = build_prop_parlay_candidates(
        card_rows,
        book=book,
        strategy=strategy,
        prop_odds=prop_odds,
    )
    return [
        prop_parlay_to_display_dict(p, rank=i + 1, book=book)
        for i, p in enumerate(parlays[:max_results])
    ]


def evaluate_prop_accuracy(predictions: pd.DataFrame) -> dict[str, float]:
    """Historical prop model accuracy on labeled fights."""
    if predictions.empty:
        return {"n_fights": 0.0}

    rows: list[dict[str, float]] = []
    for key in config.PROP_MARKETS:
        hits = 0
        total = 0
        for _, row in predictions.iterrows():
            actual = settle_prop(key, row)
            if actual is None:
                continue
            pred_p = prop_model_prob(key, row)
            pred_yes = pred_p >= 0.5
            if pred_yes == actual:
                hits += 1
            total += 1
        if total:
            rows.append({"prop_key": key, "accuracy": hits / total, "n": total})

    if not rows:
        return {"n_fights": 0.0}

    df = pd.DataFrame(rows)
    return {
        "n_fights": float(len(predictions)),
        "mean_prop_accuracy": float(df["accuracy"].mean()),
        "props_scored": float(len(df)),
    }


def simulate_prop_bets(
    predictions: pd.DataFrame,
    *,
    min_edge: float | None = None,
    initial_bankroll: float = 1000.0,
    flat_stake: float = 10.0,
) -> tuple[pd.DataFrame, dict[str, float]]:
    """Flat-stake prop value betting with synthetic market lines."""
    if not config.ENABLE_PROPS:
        empty = pd.DataFrame()
        return empty, {"trades": 0.0, "roi_pct": 0.0, "hit_rate": 0.0}

    edge_floor = min_edge if min_edge is not None else config.PROP_MIN_EDGE
    bankroll = initial_bankroll
    rows: list[dict] = []

    for _, row in predictions.iterrows():
        for cand in extract_prop_candidates_for_row(row, strategy=StrategyConfig(min_edge=edge_floor)):
            if cand.edge < edge_floor:
                continue
            won = settle_prop(cand.prop_key, row)
            if won is None:
                continue
            pnl = flat_stake * (cand.decimal_odds - 1) if won else -flat_stake
            bankroll += pnl
            rows.append(
                {
                    config.FIGHT_ID_COLUMN: row.get(config.FIGHT_ID_COLUMN),
                    config.DATE_COLUMN: row.get(config.DATE_COLUMN),
                    "fighter_1": row.get("fighter_1", row.get("fighter1")),
                    "fighter_2": row.get("fighter_2", row.get("fighter2")),
                    "market_type": "prop",
                    "prop_key": cand.prop_key,
                    "label": cand.display_label,
                    "edge": cand.edge,
                    "edge_pct": cand.edge * 100.0,
                    "stake": flat_stake,
                    "odds": cand.decimal_odds,
                    "won": int(won),
                    "pnl": pnl,
                    "equity": bankroll,
                }
            )

    trades = pd.DataFrame(rows)
    if trades.empty:
        return trades, {
            "trades": 0.0,
            "hit_rate": 0.0,
            "total_pnl": 0.0,
            "final_equity": initial_bankroll,
            "roi_pct": 0.0,
        }

    summary = {
        "trades": float(len(trades)),
        "hit_rate": float(trades["won"].mean()),
        "total_pnl": float(trades["pnl"].sum()),
        "final_equity": float(trades["equity"].iloc[-1]),
        "roi_pct": float((trades["equity"].iloc[-1] - initial_bankroll) / initial_bankroll * 100),
    }
    return trades, summary


def simulate_mixed_parlays(
    predictions: pd.DataFrame,
    *,
    book: str = "DraftKings",
    initial_bankroll: float = 1000.0,
    flat_stake: float = 10.0,
    max_parlays_per_card: int = 3,
) -> tuple[pd.DataFrame, dict[str, float]]:
    """Simulate top prop/mixed parlays per event card (DraftKings rules)."""
    if not config.ENABLE_PROPS:
        return pd.DataFrame(), {"trades": 0.0, "roi_pct": 0.0}

    rules = config.BOOK_PROP_RULES.get(book, {})
    if not rules.get("allow_prop_parlays"):
        return pd.DataFrame(), {"trades": 0.0, "roi_pct": 0.0, "note": 0.0}

    bankroll = initial_bankroll
    rows: list[dict] = []
    event_col = "event_name" if "event_name" in predictions.columns else "event"
    if event_col not in predictions.columns:
        groups = [("card", predictions)]
    else:
        groups = list(predictions.groupby(event_col, dropna=False))

    for event_key, card_df in groups:
        parlays = build_prop_parlay_candidates(card_df, book=book)[:max_parlays_per_card]
        for parlay in parlays:
            leg_results: list[bool] = []
            for leg in parlay.legs:
                if leg.market_type == "moneyline":
                    actual = row_for_leg = None
                    for _, r in card_df.iterrows():
                        if str(r.get("fight_id", "")) == leg.fight_id:
                            row_for_leg = r
                            break
                    if row_for_leg is None:
                        leg_results.append(False)
                        continue
                    actual_f1 = row_for_leg.get(config.TARGET_COLUMN)
                    if pd.isna(actual_f1):
                        leg_results.append(False)
                        continue
                    leg_results.append(
                        (leg.bet_side == "f1" and int(actual_f1) == 1)
                        or (leg.bet_side == "f2" and int(actual_f1) == 0)
                    )
                else:
                    for _, r in card_df.iterrows():
                        if str(r.get("fight_id", "")) == leg.fight_id:
                            settled = settle_prop(leg.prop_key, r)
                            leg_results.append(bool(settled))
                            break
                    else:
                        leg_results.append(False)

            won = all(leg_results) and len(leg_results) == len(parlay.legs)
            pnl = flat_stake * (parlay.combined_odds - 1) if won else -flat_stake
            bankroll += pnl
            rows.append(
                {
                    "event": event_key,
                    "book": book,
                    "market_type": "parlay",
                    "n_legs": len(parlay.legs),
                    "combined_odds": parlay.combined_odds,
                    "combined_prob": parlay.combined_prob,
                    "correlation_adjusted": int(parlay.correlation_adjusted),
                    "won": int(won),
                    "stake": flat_stake,
                    "pnl": pnl,
                    "equity": bankroll,
                }
            )

    trades = pd.DataFrame(rows)
    if trades.empty:
        return trades, {"trades": 0.0, "roi_pct": 0.0, "hit_rate": 0.0}

    return trades, {
        "trades": float(len(trades)),
        "hit_rate": float(trades["won"].mean()),
        "total_pnl": float(trades["pnl"].sum()),
        "final_equity": float(trades["equity"].iloc[-1]),
        "roi_pct": float((trades["equity"].iloc[-1] - initial_bankroll) / initial_bankroll * 100),
    }
