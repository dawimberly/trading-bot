"""Betting strategy: fractional Kelly, card risk caps, same-card parlays."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

import numpy as np
import pandas as pd

from ufc_betting_bot.modules.edge import fight_decimal_odds, market_probs, raw_kelly_fraction


@dataclass
class StrategyConfig:
    """Conservative defaults: quarter-Kelly, capped per bet and per card."""

    kelly_fraction: float = 0.25
    max_bet_fraction: float = 0.02
    min_bet_fraction: float = 0.005
    max_card_risk_fraction: float = 0.08
    min_edge: float = 0.05
    flat_stake: float = 10.0
    parlay_min_edge: float = 0.07
    parlay_min_combined_prob: float = 0.35
    parlay_max_legs: int = 3
    unrealistic_roi_threshold_pct: float = 500.0


@dataclass
class BetCandidate:
    fight_id: str
    event_key: str
    bet_side: str
    prob: float
    decimal_odds: float
    edge: float
    kelly_full: float
    expected_value: float
    fighter1_name: str = ""
    fighter2_name: str = ""
    pick_name: str = ""
    winner_name: str = ""
    market_type: str = "moneyline"
    prop_key: str = ""
    display_label: str = ""
    odds_source: str = "synthetic"


@dataclass
class ParlayCandidate:
    legs: list[BetCandidate]
    combined_prob: float
    combined_odds: float
    expected_value: float
    min_leg_edge: float


def kelly_stake(
    bankroll: float,
    *,
    prob: float,
    decimal_odds: float,
    edge: float,
    config: StrategyConfig,
) -> float:
    """Fractional Kelly stake with per-bet cap."""
    if edge < config.min_edge or bankroll <= 0:
        return 0.0
    kelly = raw_kelly_fraction(prob, decimal_odds) * config.kelly_fraction
    kelly = min(kelly, config.max_bet_fraction)
    if kelly < config.min_bet_fraction:
        return 0.0
    return float(min(bankroll * kelly, bankroll * config.max_bet_fraction))


def effective_card_risk_cap(
    config: StrategyConfig,
    mc_card_risk: dict[str, Any] | None = None,
) -> tuple[float, list[str]]:
    """Resolve per-card risk cap, optionally adjusted by Monte Carlo card assessment."""
    if not mc_card_risk:
        return config.max_card_risk_fraction, []
    try:
        from src.risk_manager import recommended_card_risk_fraction

        return recommended_card_risk_fraction(mc_card_risk, config.max_card_risk_fraction)
    except ImportError:
        return config.max_card_risk_fraction, []


def apply_card_risk_cap(
    stakes: list[float],
    bankroll: float,
    *,
    max_card_fraction: float,
    mc_card_risk: dict[str, Any] | None = None,
) -> tuple[list[float], float, list[str]]:
    """
    Scale down stakes so total card exposure <= max_card_fraction * bankroll.

    When ``mc_card_risk`` is provided, may lower the cap via Monte Carlo guidance.
    Returns (capped_stakes, effective_cap_fraction, warnings).
    """
    warnings: list[str] = []
    cap_fraction = max_card_fraction
    if mc_card_risk:
        try:
            from src.risk_manager import recommended_card_risk_fraction

            cap_fraction, cap_warnings = recommended_card_risk_fraction(mc_card_risk, max_card_fraction)
            warnings.extend(cap_warnings)
        except ImportError:
            pass

    if not stakes or bankroll <= 0:
        return stakes, cap_fraction, warnings
    total = sum(stakes)
    cap = bankroll * cap_fraction
    if total <= cap or total <= 0:
        return stakes, cap_fraction, warnings
    scale = cap / total
    return [s * scale for s in stakes], cap_fraction, warnings


def bet_expected_value(prob: float, decimal_odds: float) -> float:
    """EV per $1 staked."""
    if decimal_odds <= 1 or not np.isfinite(prob):
        return 0.0
    return prob * (decimal_odds - 1.0) - (1.0 - prob)


def extract_bet_candidates(
    row: pd.Series,
    *,
    config: StrategyConfig,
) -> BetCandidate | None:
    """Single-fight value bet candidate when odds and edge exist."""
    market = market_probs(row)
    decimal = fight_decimal_odds(row)
    if market is None or decimal is None:
        return None

    m1, m2 = market
    p1 = float(row.get("prob_f1_win", 0.5))
    p2 = float(row.get("prob_f2_win", 1.0 - p1))
    edge_f1 = p1 - m1
    edge_f2 = p2 - m2

    if edge_f1 >= edge_f2 and edge_f1 >= config.min_edge:
        side, prob, odds, edge = "f1", p1, decimal[0], edge_f1
    elif edge_f2 > edge_f1 and edge_f2 >= config.min_edge:
        side, prob, odds, edge = "f2", p2, decimal[1], edge_f2
    else:
        return None

    f1 = str(row.get("fighter_1", row.get("fighter1_name", row.get("fighter1", "")))).strip()
    f2 = str(row.get("fighter_2", row.get("fighter2_name", row.get("fighter2", "")))).strip()
    pick_name = f1 if side == "f1" else f2

    return BetCandidate(
        fight_id=str(row.get("fight_id", "")),
        event_key=str(row.get("event_name", row.get("event", ""))),
        bet_side=side,
        prob=prob,
        decimal_odds=odds,
        edge=edge,
        kelly_full=raw_kelly_fraction(prob, odds),
        expected_value=bet_expected_value(prob, odds),
        fighter1_name=f1,
        fighter2_name=f2,
        pick_name=pick_name,
        winner_name=pick_name,
    )


def build_parlay_candidates(
    card_rows: pd.DataFrame,
    *,
    config: StrategyConfig,
) -> list[ParlayCandidate]:
    """
    Same-card parlays only: 2–max_legs legs, min edge 7%, combined prob > 35%.
    Sorted by expected value descending.
    """
    legs: list[BetCandidate] = []
    for _, row in card_rows.iterrows():
        cand = extract_bet_candidates(row, config=config)
        if cand is None or cand.edge < config.parlay_min_edge:
            continue
        legs.append(cand)

    if len(legs) < 2:
        return []

    parlays: list[ParlayCandidate] = []
    max_legs = min(config.parlay_max_legs, len(legs))
    for n in range(2, max_legs + 1):
        for combo in combinations(legs, n):
            combined_prob = float(np.prod([c.prob for c in combo]))
            if combined_prob < config.parlay_min_combined_prob:
                continue
            combined_odds = float(np.prod([c.decimal_odds for c in combo]))
            ev = combined_prob * (combined_odds - 1.0) - (1.0 - combined_prob)
            parlays.append(
                ParlayCandidate(
                    legs=list(combo),
                    combined_prob=combined_prob,
                    combined_odds=combined_odds,
                    expected_value=ev,
                    min_leg_edge=min(c.edge for c in combo),
                )
            )

    parlays.sort(key=lambda p: p.expected_value, reverse=True)
    return parlays


def strategy_from_profile(
    *,
    min_edge: float | None = None,
    bankroll: float | None = None,
    recent_win_rate: float | None = None,
    model_confidence: float | None = None,
    hours_to_event: float | None = None,
    use_dynamic_thresholds: bool | None = None,
) -> StrategyConfig:
    """Build StrategyConfig from active UFC_PROFILE thresholds (optionally dynamic)."""
    import config as _cfg

    enabled = (
        _cfg.DYNAMIC_THRESHOLDS_ENABLED if use_dynamic_thresholds is None else use_dynamic_thresholds
    )
    if enabled and bankroll is not None:
        from ufc_betting_bot.modules.dynamic_thresholds import get_profile_thresholds

        thresholds = get_profile_thresholds(
            bankroll,
            recent_win_rate,
            model_confidence,
            hours_to_event=hours_to_event,
            profile=_cfg.UFC_PROFILE,
        )
        edge = thresholds.alert_min_edge if min_edge is None else min_edge
        return StrategyConfig(
            kelly_fraction=_cfg.profile_value("kelly_fraction"),
            max_bet_fraction=_cfg.profile_value("max_bet_fraction"),
            max_card_risk_fraction=_cfg.effective_max_card_risk_fraction(bankroll),
            min_edge=edge,
            parlay_min_edge=thresholds.parlay_min_edge,
            parlay_min_combined_prob=thresholds.parlay_min_combined_prob,
            parlay_max_legs=_cfg.ALERT_PARLAY_MAX_LEGS,
            flat_stake=_cfg.FLAT_STAKE,
        )

    ps = _cfg.profile_settings()
    card_frac = _cfg.effective_max_card_risk_fraction(bankroll)
    edge = ps["alert_min_edge"] if min_edge is None else min_edge
    return StrategyConfig(
        kelly_fraction=ps["kelly_fraction"],
        max_bet_fraction=ps["max_bet_fraction"],
        max_card_risk_fraction=card_frac,
        min_edge=edge,
        parlay_min_edge=ps["parlay_min_edge"],
        parlay_min_combined_prob=ps["parlay_min_combined_prob"],
        parlay_max_legs=_cfg.ALERT_PARLAY_MAX_LEGS,
        flat_stake=_cfg.FLAT_STAKE,
    )


def _pick_model_prob(row: pd.Series) -> tuple[str, float, str]:
    """Return (pick, model_prob, fight_label) for a prediction row."""
    f1 = str(row.get("fighter_1", row.get("fighter1", "")))
    f2 = str(row.get("fighter_2", row.get("fighter2", "")))
    pick = str(row.get("predicted_winner", ""))
    prob = row.get("predicted_prob", row.get("prob_f1_win"))
    if pd.isna(prob):
        if pick == f2 and pd.notna(row.get("prob_f2_win")):
            prob = float(row["prob_f2_win"])
        elif pick == f1 and pd.notna(row.get("prob_f1_win")):
            prob = float(row["prob_f1_win"])
        else:
            p1 = float(row.get("prob_f1_win", 0.5))
            prob = p1 if pick == f1 else 1.0 - p1
    return pick, float(prob), f"{f1} vs {f2}"


def build_model_only_parlay_candidates(
    card_rows: pd.DataFrame,
    *,
    min_pick_prob: float = 0.52,
    parlay_max_legs: int = 3,
    parlay_min_combined_prob: float = 0.25,
) -> list[dict[str, Any]]:
    """
    Research helper: rank same-card parlays by model probability only (no odds).

    Uses highest-confidence picks per fight; does not imply +EV without market lines.
    """
    legs: list[dict[str, Any]] = []
    for _, row in card_rows.iterrows():
        pick, prob, fight = _pick_model_prob(row)
        if prob < min_pick_prob:
            continue
        legs.append(
            {
                "fight": fight,
                "pick": pick,
                "prob": prob,
                "fight_id": str(row.get("fight_id", fight)),
            }
        )
    if len(legs) < 2:
        return []

    out: list[dict[str, Any]] = []
    max_legs = min(parlay_max_legs, len(legs))
    for n in range(2, max_legs + 1):
        for combo in combinations(legs, n):
            combined_prob = float(np.prod([leg["prob"] for leg in combo]))
            if combined_prob < parlay_min_combined_prob:
                continue
            picks_txt = " + ".join(f"{leg['pick']} ({leg['prob']:.0%})" for leg in combo)
            out.append(
                {
                    "n_legs": n,
                    "legs": list(combo),
                    "combined_prob": combined_prob,
                    "picks": picks_txt,
                    "model_only": True,
                }
            )
    out.sort(key=lambda x: x["combined_prob"], reverse=True)
    return out[:5]


def compute_equity_metrics(equity: pd.Series) -> dict[str, float]:
    """Max drawdown % and longest win streak from chronological equity curve."""
    if equity.empty:
        return {"max_drawdown_pct": 0.0, "max_win_streak": 0.0}

    eq = equity.astype(float).reset_index(drop=True)
    peak = eq.cummax()
    drawdown = np.where(peak > 0, (peak - eq) / peak, 0.0)
    max_dd = float(np.max(drawdown) * 100.0) if len(drawdown) else 0.0

    return {"max_drawdown_pct": max_dd, "max_win_streak": 0.0}


def compute_trade_streaks(won: pd.Series) -> dict[str, float]:
    if won.empty:
        return {"max_win_streak": 0.0, "max_loss_streak": 0.0}
    best_win = best_loss = cur_win = cur_loss = 0
    for w in won.astype(int):
        if w:
            cur_win += 1
            cur_loss = 0
        else:
            cur_loss += 1
            cur_win = 0
        best_win = max(best_win, cur_win)
        best_loss = max(best_loss, cur_loss)
    return {"max_win_streak": float(best_win), "max_loss_streak": float(best_loss)}


def warn_unrealistic_roi(roi_pct: float, *, threshold: float = 500.0) -> str | None:
    if not np.isfinite(roi_pct) or roi_pct <= threshold:
        return None
    return (
        f"ROI {roi_pct:.1f}% exceeds {threshold:.0f}% — likely overfitting, odds leakage, "
        "or compounding artifacts. Treat as diagnostic only."
    )


def enrich_summary_with_risk_metrics(
    trades: pd.DataFrame,
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Add drawdown / streak stats to a backtest summary dict."""
    if trades.empty:
        summary.update(max_drawdown_pct=0.0, max_win_streak=0.0, max_loss_streak=0.0)
        return summary
    if "equity" in trades.columns:
        summary.update(compute_equity_metrics(trades["equity"]))
    if "won" in trades.columns:
        summary.update(compute_trade_streaks(trades["won"]))
    warning = warn_unrealistic_roi(float(summary.get("roi_pct", 0)))
    if warning:
        summary["roi_warning"] = warning
    return summary


# --- Budget manager (per-book card allocation) --------------------------------


def resolve_budget_for_calculations(
    budget_state: dict[str, Any] | None,
    *,
    profile: str | None = None,
) -> dict[str, Any]:
    """
    Normalize budget and apply profile card cap for stake / allocation math.

    Live mode clamps card_budget to the live USD cap (default $12).
    """
    import config as _cfg

    state = _cfg.normalize_budget_state(budget_state)
    card_eff, _ = effective_card_budget_usd(state, profile=profile)
    resolved = dict(state)
    resolved["card_budget"] = card_eff
    return resolved


def effective_card_budget_usd(
    budget_state: dict[str, Any],
    *,
    profile: str | None = None,
) -> tuple[float, list[str]]:
    """
    Resolve user card budget capped by profile safe limits.

    Live mode hard-caps at LIVE_MAX_CARD_STAKE_USD (default $12).
    """
    import config as _cfg

    warnings: list[str] = []
    br = max(float(budget_state.get("total_bankroll") or _cfg.DEFAULT_TOTAL_BANKROLL), 1.0)
    raw = float(budget_state.get("card_budget") or 0.0)
    if raw <= 0:
        raw = _cfg.max_card_stake_cap(br)

    safe_cap = _cfg.max_card_stake_cap(br)
    capped = min(raw, safe_cap)

    live = _cfg.is_live_profile() if profile is None else _cfg.normalize_profile(profile) == "live"
    if live:
        live_usd = float(
            _cfg.profile_settings().get("max_card_stake_usd") or _cfg.LIVE_MAX_CARD_BUDGET_USD
        )
        if raw > live_usd:
            warnings.append(
                f"Card budget ${raw:,.2f} exceeds Live cap ${live_usd:,.2f}; "
                f"allocations use ${min(capped, live_usd):,.2f}."
            )
        capped = min(capped, live_usd)
    elif raw > safe_cap:
        warnings.append(
            f"Card budget ${raw:,.2f} exceeds profile safe cap ${safe_cap:,.2f}; "
            f"allocations use ${capped:,.2f}."
        )

    return max(capped, 0.0), warnings


def allocate_card_budget_per_book(
    budget_state: dict[str, Any],
    *,
    profile: str | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Split effective card budget across enabled books.

    Proportional to positive balances when any exist; otherwise equal split.
    Each book allocation is capped at that book's balance.
    """
    import config as _cfg

    card_budget, _ = effective_card_budget_usd(budget_state, profile=profile)
    enabled: list[tuple[str, float]] = []
    for book in _cfg.BUDGET_BOOKS:
        use_key = _cfg.BUDGET_USE_KEYS[book]
        bal_key = _cfg.BUDGET_BALANCE_KEYS[book]
        if not budget_state.get(use_key, True):
            continue
        enabled.append((book, max(float(budget_state.get(bal_key) or 0.0), 0.0)))

    result: dict[str, dict[str, Any]] = {}
    for book in _cfg.BUDGET_BOOKS:
        use_key = _cfg.BUDGET_USE_KEYS[book]
        bal_key = _cfg.BUDGET_BALANCE_KEYS[book]
        balance = max(float(budget_state.get(bal_key) or 0.0), 0.0)
        enabled_flag = bool(budget_state.get(use_key, True))
        result[book] = {
            "balance": balance,
            "enabled": enabled_flag,
            "allocation": 0.0,
            "share_pct": 0.0,
        }

    if not enabled or card_budget <= 0:
        return result

    with_balance = [(b, bal) for b, bal in enabled if bal > 0]
    if with_balance:
        total_bal = sum(bal for _, bal in with_balance)
        shares = {book: card_budget * (bal / total_bal) for book, bal in with_balance}
    else:
        share = card_budget / len(enabled)
        shares = {book: share for book, _ in enabled}

    for book, _ in enabled:
        raw_alloc = shares.get(book, 0.0)
        balance = result[book]["balance"]
        alloc = min(raw_alloc, balance) if balance > 0 else raw_alloc
        result[book]["allocation"] = float(alloc)
        result[book]["share_pct"] = (alloc / card_budget * 100.0) if card_budget > 0 else 0.0

    return result


def distribute_stakes_to_pool(singles: list[dict[str, Any]], pool: float) -> list[float]:
    """Scale suggested singles stakes to fit within a book's card allocation pool."""
    if not singles or pool <= 0:
        return [0.0] * len(singles)
    raw = [max(float(s.get("suggested_stake") or 0.0), 0.0) for s in singles]
    total = sum(raw)
    if total <= 0:
        even = pool / len(singles)
        return [even] * len(singles)
    if total <= pool:
        return raw
    scale = pool / total
    return [r * scale for r in raw]


def attach_prop_stakes(
    singles: list[dict[str, Any]],
    budget_state: dict[str, Any] | None,
    book: str,
    *,
    profile: str | None = None,
) -> list[dict[str, Any]]:
    """Scale suggested prop stakes to the book's card allocation pool."""
    if not singles:
        return []
    if not budget_state:
        return [{**s, "suggested_stake": 0.0} for s in singles]

    resolved = resolve_budget_for_calculations(budget_state, profile=profile)
    plan = allocate_card_budget_per_book(resolved, profile=profile)
    info = plan.get(book, {})
    if not info.get("enabled"):
        return [{**s, "suggested_stake": 0.0, "book_disabled": True} for s in singles]

    pool = float(info.get("allocation") or 0)
    if pool <= 0 and info.get("enabled"):
        pool = available_card_budget_usd(resolved, profile=profile) / max(
            1,
            sum(1 for b in plan.values() if b.get("enabled")),
        )

    weighted = [
        {"suggested_stake": max(float(s.get("edge") or 0), 0.005) * max(pool, 1.0)}
        for s in singles
    ]
    stakes = distribute_stakes_to_pool(weighted, pool)
    return [{**s, "suggested_stake": round(float(st), 2)} for s, st in zip(singles, stakes)]


def budget_summary_text(budget_state: dict[str, Any]) -> str:
    """One-line budget summary for the dashboard."""
    import config as _cfg

    card, _ = effective_card_budget_usd(budget_state)
    parts = [
        f"Total Budget: ${float(budget_state.get('total_bankroll') or _cfg.DEFAULT_TOTAL_BANKROLL):,.0f}",
        f"Card Budget: ${card:,.0f}",
    ]
    alloc = allocate_card_budget_per_book(budget_state)
    for book in _cfg.BUDGET_BOOKS:
        info = alloc.get(book, {})
        if not info.get("enabled"):
            continue
        short = book.replace(".eu", "")
        parts.append(f"{short}: ${float(info.get('allocation') or 0):,.2f}")
    return " | ".join(parts)


def live_card_budget_warning(budget_state: dict[str, Any]) -> str | None:
    """Live-only warning when user card budget exceeds recommended safe limits."""
    import config as _cfg

    if not _cfg.is_live_profile():
        return None
    br = max(float(budget_state.get("total_bankroll") or _cfg.DEFAULT_TOTAL_BANKROLL), 1.0)
    raw = float(budget_state.get("card_budget") or 0.0)
    safe_cap = _cfg.max_card_stake_cap(br)
    live_usd = float(_cfg.profile_settings().get("max_card_stake_usd") or _cfg.DEFAULT_CARD_BUDGET)
    cap = min(safe_cap, live_usd)
    if raw <= cap:
        return None
    pct = raw / br * 100.0
    safe_pct = cap / br * 100.0
    return (
        f"Card budget ${raw:,.2f} ({pct:.0f}% of bankroll) exceeds recommended safe limit "
        f"${cap:,.2f} ({safe_pct:.0f}%) for Live mode."
    )


def book_display_name(book: str) -> str:
    return book.replace(".eu", "")


def available_card_budget_usd(
    budget_state: dict[str, Any],
    *,
    profile: str | None = None,
) -> float:
    """Total dollars allocatable this card across enabled books."""
    plan = allocate_card_budget_per_book(budget_state, profile=profile)
    return float(
        sum(float(info.get("allocation") or 0) for info in plan.values() if info.get("enabled"))
    )


def available_card_budget_text(
    budget_state: dict[str, Any],
    *,
    profile: str | None = None,
) -> str:
    """Human label: 'Available this card: $12.00 across selected books'."""
    total = available_card_budget_usd(budget_state, profile=profile)
    import config as _cfg

    enabled = sum(
        1 for book in _cfg.BUDGET_BOOKS if budget_state.get(_cfg.BUDGET_USE_KEYS[book], True)
    )
    if enabled == 0:
        return "Available this card: $0.00 (no books selected)"
    return f"Available this card: ${total:,.2f} across selected books"


MAX_SAFE_BANKROLL_FRACTION = 0.005  # 0.5% hard cap for "max safe" single bet


def budget_availability_badge_style(
    total_usd: float,
    *,
    books_enabled: bool,
) -> tuple[str, str]:
    """Background and text colors for the Available-this-card badge."""
    if not books_enabled:
        return "#451a1a", "#fca5a5"
    if total_usd > 50:
        return "#14532d", "#86efac"
    if total_usd >= 20:
        return "#713f12", "#fde047"
    return "#451a1a", "#fca5a5"


def bet_sizing_metrics(
    bankroll: float,
    *,
    prob: float | None,
    decimal_odds: float | None,
    edge: float,
    config: StrategyConfig,
) -> dict[str, float]:
    """Kelly stake, Kelly % of bankroll, and max-safe bet (min 0.5% bankroll vs Kelly)."""
    kelly_usd = 0.0
    kelly_pct = 0.0
    half_pct_cap = max(bankroll * MAX_SAFE_BANKROLL_FRACTION, 0.0)
    if (
        prob is not None
        and decimal_odds is not None
        and bankroll > 0
        and float(edge) >= config.min_edge
    ):
        kelly_usd = kelly_stake(
            bankroll,
            prob=float(prob),
            decimal_odds=float(decimal_odds),
            edge=float(edge),
            config=config,
        )
        if kelly_usd > 0:
            kelly_pct = kelly_usd / bankroll * 100.0
    max_safe = min(half_pct_cap, kelly_usd) if kelly_usd > 0 else half_pct_cap
    return {
        "kelly_stake_usd": round(kelly_usd, 2),
        "kelly_pct": round(kelly_pct, 2),
        "max_safe_bet_usd": round(max_safe, 2),
    }


def bankroll_from_budget(budget_state: dict[str, Any] | None) -> float:
    import config as _cfg

    if not budget_state:
        return float(_cfg.INITIAL_BANKROLL)
    return float(budget_state.get("total_bankroll") or _cfg.INITIAL_BANKROLL)


def scale_alerts_to_book_pool(
    alerts: dict[str, Any],
    budget_state: dict[str, Any] | None,
    book: str,
    *,
    profile: str | None = None,
) -> dict[str, Any]:
    """Scale singles/parlay suggested stakes to the book's card allocation pool."""
    if not alerts or not budget_state:
        return alerts

    resolved = resolve_budget_for_calculations(budget_state, profile=profile)

    if book == "Overview":
        pool = available_card_budget_usd(resolved, profile=profile)
    else:
        plan = allocate_card_budget_per_book(resolved, profile=profile)
        info = plan.get(book, {})
        if not info.get("enabled"):
            return {**alerts, "singles": [], "parlays": [], "book_disabled": True}
        pool = float(info.get("allocation") or 0)
    out = dict(alerts)
    singles = list(alerts.get("singles") or [])
    if singles and pool > 0:
        stakes = distribute_stakes_to_pool(singles, pool)
        out["singles"] = [
            {**s, "suggested_stake": round(float(st), 2), "book_pool_usd": pool}
            for s, st in zip(singles, stakes)
        ]
    elif singles:
        out["singles"] = [{**s, "suggested_stake": 0.0, "book_pool_usd": 0.0} for s in singles]

    parlays = list(alerts.get("parlays") or [])
    if parlays and pool > 0:
        parlay_pool = pool * 0.25
        stakes = distribute_stakes_to_pool(parlays, parlay_pool)
        out["parlays"] = [
            {**p, "suggested_stake": round(float(st), 2), "book_pool_usd": pool}
            for p, st in zip(parlays, stakes)
        ]
    return out


def budget_aware_alerts(
    alerts: dict[str, Any],
    budget_state: dict[str, Any] | None,
    book: str,
    *,
    profile: str | None = None,
) -> dict[str, Any]:
    """Apply book enablement and card-budget stake scaling to an alert payload."""
    if not budget_state:
        return alerts
    return scale_alerts_to_book_pool(alerts, budget_state, book, profile=profile)


def collect_dashboard_risk_warnings(
    alerts: dict[str, Any] | None,
    budget_state: dict[str, Any] | None,
    *,
    bankroll: float | None = None,
) -> list[tuple[str, str]]:
    """
    Unified risk warnings for dashboard tabs.

    Returns list of (severity, message) where severity is critical | warn | info.
    """
    import config as _cfg

    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(severity: str, msg: str) -> None:
        key = msg.strip()
        if not key or key in seen:
            return
        seen.add(key)
        out.append((severity, key))

    br = float(
        (budget_state or {}).get("total_bankroll")
        or (alerts or {}).get("bankroll")
        or bankroll
        or _cfg.INITIAL_BANKROLL
    )

    if budget_state:
        budget_warn = live_card_budget_warning(budget_state)
        if budget_warn:
            _add("critical", budget_warn)
        card_eff, cap_warnings = effective_card_budget_usd(budget_state)
        for w in cap_warnings:
            _add("warn", w)
        raw_card = float(budget_state.get("card_budget") or 0)
        if _cfg.is_live_profile():
            live_cap = _cfg.live_card_budget_cap_usd(br)
            if raw_card > live_cap:
                _add(
                    "critical",
                    f"Card budget ${raw_card:,.2f} exceeds Live hard cap ${live_cap:,.2f}.",
                )
        elif card_eff < raw_card:
            _add("warn", f"Card budget trimmed to safe cap ${card_eff:,.2f} for current profile.")

    for w in _cfg.live_small_bankroll_warnings(br):
        _add("critical", w)

    if alerts:
        live_warn = _cfg.live_card_risk_warning(alerts, bankroll=br)
        if live_warn:
            _add("critical", live_warn)
        stake = _cfg.estimated_card_stake_usd(alerts)
        cap = _cfg.max_card_stake_cap(br)
        if _cfg.is_live_profile() and stake > 0 and stake > cap:
            _add(
                "critical",
                f"Suggested stakes ${stake:,.2f} exceed your ${cap:,.2f} live card cap.",
            )
        for w in alerts.get("warnings") or []:
            _add("warn", str(w))

    return out


def format_risk_warnings(
    warnings: list[tuple[str, str]],
    *,
    max_lines: int = 4,
    separator: str = "  |  ",
) -> tuple[str, str]:
    """Return (display_text, color_hex) for a warning label."""
    if not warnings:
        return "", "#9ca3af"
    critical = [m for s, m in warnings if s == "critical"]
    shown = (critical or [m for s, m in warnings])[:max_lines]
    text = separator.join(shown)
    if critical:
        return f"⚠ {text}", "#f87171"
    return f"⚠ {text}", "#fbbf24"


def _format_american_odds(decimal: float | None) -> str:
    if decimal is None or decimal <= 1:
        return "—"
    if decimal >= 2.0:
        return f"+{int(round((decimal - 1.0) * 100))}"
    return str(int(round(-100.0 / (decimal - 1.0))))


def format_pick_over_opponent(fight: str, pick: str) -> str:
    """e.g. 'Michael Chandler over Mauricio Ruffy'."""
    if " vs " not in fight or not pick:
        return pick or fight
    f1, f2 = [x.strip() for x in fight.split(" vs ", 1)]
    if pick == f1:
        return f"{pick} over {f2}"
    if pick == f2:
        return f"{pick} over {f1}"
    return pick


def _find_prediction_row(preds: pd.DataFrame, single: dict[str, Any]) -> pd.Series | None:
    if preds is None or preds.empty:
        return None
    fid = str(single.get("fight_id") or "")
    fight = str(single.get("fight") or "")
    for _, row in preds.iterrows():
        row_fid = str(row.get("fight_id", ""))
        if fid and row_fid and row_fid == fid:
            return row
        f1 = str(row.get("fighter_1", row.get("fighter1", ""))).strip()
        f2 = str(row.get("fighter_2", row.get("fighter2", ""))).strip()
        if fight and f"{f1} vs {f2}" == fight:
            return row
    return None


def decimal_odds_for_pick(row: pd.Series, pick: str) -> float | None:
    f1 = str(row.get("fighter_1", row.get("fighter1", ""))).strip()
    if pick == f1:
        val = row.get("f1_odds")
    else:
        val = row.get("f2_odds")
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        dec = float(val)
        return dec if dec > 1.0 else None
    except (TypeError, ValueError):
        return None


def aggregate_top_recommended_bets(
    books: dict[str, dict[str, Any]],
    budget_state: dict[str, Any],
    *,
    limit: int = 5,
    per_book_cap: int = 2,
    profile: str | None = None,
) -> list[dict[str, Any]]:
    """
    Best singles across enabled books, deduped by fight, stakes scaled to card budget.

    Takes up to ``per_book_cap`` edges from each enabled book before global dedupe so
    Overview is not dominated by a single sportsbook.
    """
    import config as _cfg

    resolved = resolve_budget_for_calculations(budget_state, profile=profile)
    enabled = [
        book
        for book in _cfg.BUDGET_BOOKS
        if resolved.get(_cfg.BUDGET_USE_KEYS[book], True)
    ]
    bankroll = bankroll_from_budget(resolved)
    strategy = strategy_from_profile(bankroll=bankroll)
    pool_candidates: list[dict[str, Any]] = []

    for book in enabled:
        book_data = books.get(book, {})
        alerts = book_data.get("alerts") or {}
        preds = book_data.get("predictions")
        if not isinstance(preds, pd.DataFrame):
            preds = pd.DataFrame()
        book_singles = sorted(
            alerts.get("singles") or [],
            key=lambda x: float(x.get("edge") or 0),
            reverse=True,
        )[: max(1, per_book_cap)]
        for single in book_singles:
            edge = float(single.get("edge") or 0)
            fid = str(single.get("fight_id") or single.get("fight") or "")
            if not fid:
                continue
            row = _find_prediction_row(preds, single)
            pick = str(single.get("pick") or "")
            fight = str(single.get("fight") or "")
            dec = decimal_odds_for_pick(row, pick) if row is not None else None
            prob_val = single.get("prob")
            prob_f = float(prob_val) if prob_val is not None else None
            confidence = str(single.get("confidence") or "").strip()
            if not confidence and row is not None:
                confidence = str(row.get("confidence_label") or "").strip()
            sizing = bet_sizing_metrics(
                bankroll,
                prob=prob_f,
                decimal_odds=dec,
                edge=edge,
                config=strategy,
            )
            pool_candidates.append(
                {
                    "fight_id": fid,
                    "fight": fight,
                    "pick": pick,
                    "pick_line": format_pick_over_opponent(fight, pick),
                    "bet_type": "Moneyline Single",
                    "description": str(single.get("brief") or single.get("reasoning") or "").strip(),
                    "prob": prob_f,
                    "edge": edge,
                    "edge_pct": float(single.get("edge_pct") or edge * 100),
                    "confidence": confidence or "—",
                    "kelly_pct": sizing["kelly_pct"],
                    "kelly_stake_usd": sizing["kelly_stake_usd"],
                    "max_safe_bet_usd": sizing["max_safe_bet_usd"],
                    "book": book_display_name(book),
                    "book_key": book,
                    "decimal_odds": dec,
                    "odds_display": f"{dec:.2f}" if dec else "—",
                    "american_odds": _format_american_odds(dec),
                    "raw_stake": float(single.get("suggested_stake") or 0),
                }
            )

    best_by_fight: dict[str, dict[str, Any]] = {}
    for bet in pool_candidates:
        fid = bet["fight_id"]
        prev = best_by_fight.get(fid)
        if prev is None or float(bet.get("edge") or 0) > float(prev.get("edge") or 0):
            best_by_fight[fid] = bet

    ranked = sorted(best_by_fight.values(), key=lambda x: float(x.get("edge") or 0), reverse=True)[:limit]
    pool = available_card_budget_usd(resolved, profile=profile)
    stakes = distribute_stakes_to_pool(ranked, pool)
    for i, (bet, stake) in enumerate(zip(ranked, stakes), start=1):
        bet["rank"] = i
        bet["suggested_stake"] = round(float(stake), 2)
        bet["card_pool_usd"] = pool
    return ranked


def aggregate_overview_recommendations(
    books: dict[str, dict[str, Any]],
    budget_state: dict[str, Any],
    *,
    limit: int = 5,
    profile: str | None = None,
) -> list[dict[str, Any]]:
    """
    Up to 5 actionable Overview picks: top singles plus best parlay when available.

    Singles are deduped by fight, ranked by edge, and stakes scaled to card budget.
    """
    cap = max(3, min(5, int(limit)))
    parlay = aggregate_best_parlay(books, budget_state, profile=profile)
    if parlay is not None:
        edge_val = parlay.get("edge_pct")
        if edge_val is None:
            edge_val = parlay.get("expected_value")
        if edge_val is None or float(edge_val) <= 0:
            parlay = None
    singles_limit = cap - 1 if parlay else cap
    singles = aggregate_top_recommended_bets(
        books, budget_state, limit=singles_limit, per_book_cap=2, profile=profile
    )
    # Fill remaining slots from Overview alerts if cross-book dedupe left gaps
    if len(singles) < singles_limit:
        overview_singles = (books.get("Overview", {}).get("alerts") or {}).get("singles") or []
        seen = {s.get("fight_id") for s in singles}
        for s in sorted(overview_singles, key=lambda x: float(x.get("edge") or 0), reverse=True):
            fid = str(s.get("fight_id") or s.get("fight") or "")
            if not fid or fid in seen:
                continue
            singles.append(
                {
                    "fight_id": fid,
                    "fight": s.get("fight"),
                    "pick": s.get("pick"),
                    "pick_line": format_pick_over_opponent(str(s.get("fight") or ""), str(s.get("pick") or "")),
                    "bet_type": "Moneyline Single",
                    "edge": float(s.get("edge") or 0),
                    "edge_pct": float(s.get("edge_pct") or float(s.get("edge") or 0) * 100),
                    "book": "Overview",
                    "book_key": "Overview",
                    "suggested_stake": float(s.get("suggested_stake") or 0),
                    "american_odds": "—",
                    "odds_display": "—",
                }
            )
            seen.add(fid)
            if len(singles) >= singles_limit:
                break
    for single in singles:
        pick = str(single.get("pick") or "").strip()
        single["display_label"] = f"{pick} ML" if pick else str(single.get("pick_line") or "—")

    items: list[dict[str, Any]] = list(singles)
    if parlay:
        legs = str(parlay.get("pick_line") or "").strip()
        bt = str(parlay.get("bet_type") or "Parlay")
        leg_n = bt.split("-Leg")[0].strip() if "-Leg" in bt else ""
        if legs and leg_n.isdigit():
            parlay["display_label"] = f"{legs} {leg_n}-leg"
        else:
            parlay["display_label"] = legs or bt
        parlay["is_parlay"] = True
        items.append(parlay)

    for i, bet in enumerate(items[:cap], start=1):
        bet["rank"] = i
    return items[:cap]


def aggregate_best_parlay(
    books: dict[str, dict[str, Any]],
    budget_state: dict[str, Any],
    *,
    profile: str | None = None,
) -> dict[str, Any] | None:
    """Highest-EV parlay across enabled books (for Overview highlight)."""
    import config as _cfg
    from src.parlay_builder import enrich_parlays_for_display, format_recommended_parlay_header

    resolved = resolve_budget_for_calculations(budget_state, profile=profile)
    enabled = [
        book
        for book in _cfg.BUDGET_BOOKS
        if resolved.get(_cfg.BUDGET_USE_KEYS[book], True)
    ]
    best: dict[str, Any] | None = None
    best_ev = -1.0

    for book in enabled:
        book_data = books.get(book, {})
        alerts = book_data.get("alerts") or {}
        preds = book_data.get("predictions")
        if not isinstance(preds, pd.DataFrame):
            preds = pd.DataFrame()
        parlays = alerts.get("parlays") or []
        if not parlays:
            continue
        top = sorted(parlays, key=lambda x: float(x.get("expected_value", 0)), reverse=True)[0]
        ev = float(top.get("expected_value", 0))
        if ev <= best_ev:
            continue
        enriched = enrich_parlays_for_display([top], preds)
        p = enriched[0] if enriched else dict(top)
        combined_dec = float(p.get("combined_odds", 0) or 0)
        legs_txt = str(p.get("picks") or "")
        if not legs_txt and p.get("legs"):
            from src.parlay_builder import leg_pick_label

            legs_txt = " + ".join(leg_pick_label(leg) for leg in p["legs"])
        pool = available_card_budget_usd(resolved, profile=profile)
        stake = round(min(float(p.get("suggested_stake") or 0), pool * 0.25), 2)
        best_ev = ev
        best = {
            "bet_type": f"{int(p.get('n_legs', 2))}-Leg Parlay",
            "pick_line": legs_txt or format_recommended_parlay_header({**p, "rank": 1}),
            "description": format_recommended_parlay_header({**p, "rank": 1}),
            "prob": p.get("combined_prob"),
            "edge_pct": float(p.get("min_leg_edge", 0) or 0) * 100,
            "book": book_display_name(book),
            "book_key": book,
            "american_odds": _format_american_odds(combined_dec if combined_dec > 1 else None),
            "odds_display": f"{combined_dec:.2f}" if combined_dec > 1 else "—",
            "suggested_stake": stake,
            "expected_value": ev,
            "card_pool_usd": pool,
        }
    return best

