"""Monte Carlo risk analysis for UFC betting backtests and upcoming cards."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)

try:
    from ufc_betting_bot.modules.edge import compute_edge, fight_decimal_odds, market_probs, raw_kelly_fraction
except ImportError:
    from src.backtester import _fight_decimal_odds as fight_decimal_odds  # type: ignore
    from src.backtester import _market_probs as market_probs  # type: ignore

    def compute_edge(p1, p2, m1, m2):
        edge_f1 = p1 - m1
        edge_f2 = p2 - m2
        if edge_f1 >= edge_f2:
            return {"bet_side": "f1", "edge": edge_f1, "edge_f1": edge_f1, "edge_f2": edge_f2}
        return {"bet_side": "f2", "edge": edge_f2, "edge_f1": edge_f1, "edge_f2": edge_f2}

    def raw_kelly_fraction(prob: float, decimal_odds: float) -> float:
        if decimal_odds <= 1 or not np.isfinite(prob):
            return 0.0
        b = decimal_odds - 1.0
        q = 1.0 - prob
        return max(0.0, (prob * b - q) / b)


OutcomeMode = Literal["bootstrap", "parametric"]
STAKING_MODES = ("flat", "quarter_kelly", "half_kelly")


@dataclass
class MonteCarloResult:
    n_simulations: int
    outcome_mode: str
    initial_bankroll: float
    confidence_level: float
    staking_summaries: dict[str, dict[str, float]] = field(default_factory=dict)
    max_drawdown_distribution: dict[str, list[float]] = field(default_factory=dict)
    final_equity_distribution: dict[str, list[float]] = field(default_factory=dict)
    per_card_pnl: pd.DataFrame = field(default_factory=pd.DataFrame)
    rolling_card_risk: pd.DataFrame = field(default_factory=pd.DataFrame)
    sample_equity_curves: dict[str, list[list[float]]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_simulations": self.n_simulations,
            "outcome_mode": self.outcome_mode,
            "initial_bankroll": self.initial_bankroll,
            "confidence_level": self.confidence_level,
            "staking_summaries": self.staking_summaries,
            "max_drawdown_distribution": self.max_drawdown_distribution,
            "final_equity_distribution": self.final_equity_distribution,
            "per_card_pnl": self.per_card_pnl.to_dict(orient="records"),
            "rolling_card_risk": self.rolling_card_risk.to_dict(orient="records"),
            "sample_equity_curves": self.sample_equity_curves,
            "warnings": self.warnings,
        }


def _resolve_predictions(backtest_results: Any) -> pd.DataFrame:
    if isinstance(backtest_results, pd.DataFrame):
        return backtest_results
    if hasattr(backtest_results, "predictions"):
        return backtest_results.predictions
    if isinstance(backtest_results, dict) and "predictions" in backtest_results:
        return backtest_results["predictions"]
    raise TypeError("backtest_results must be a DataFrame or object with .predictions")


def build_bet_schedule(
    predictions: pd.DataFrame,
    *,
    min_edge: float | None = None,
    date_column: str = "event_date",
    target_column: str = "f1_win",
) -> pd.DataFrame:
    """Ordered value-bet schedule from backtest or live predictions."""
    min_edge = config.MIN_EDGE if min_edge is None else min_edge
    work = predictions.copy()
    if date_column in work.columns:
        work[date_column] = pd.to_datetime(work[date_column], errors="coerce")
        sort_cols = [date_column]
        if "event_name" in work.columns:
            sort_cols.append("event_name")
        work = work.sort_values(sort_cols)

    rows: list[dict[str, Any]] = []
    event_idx = 0
    last_event: str | None = None

    for _, row in work.iterrows():
        market = market_probs(row)
        decimal = fight_decimal_odds(row)
        if market is None or decimal is None:
            continue

        m1, m2 = market
        p1 = float(row.get("prob_f1_win", 0.5))
        p2 = float(row.get("prob_f2_win", 1.0 - p1))
        edge_info = compute_edge(p1, p2, m1, m2)
        if edge_info["edge"] < min_edge:
            continue

        side = edge_info["bet_side"]
        prob = p1 if side == "f1" else p2
        odds = decimal[0] if side == "f1" else decimal[1]
        event_key = str(row.get("event_name", row.get("event", "")))
        if last_event is not None and event_key != last_event:
            event_idx += 1
        last_event = event_key

        actual = row.get(target_column)
        won = np.nan
        if pd.notna(actual):
            won = int((side == "f1" and int(actual) == 1) or (side == "f2" and int(actual) == 0))

        rows.append(
            {
                "event_name": event_key,
                "event_idx": event_idx,
                date_column: row.get(date_column),
                "bet_side": side,
                "prob": prob,
                "odds": odds,
                "edge": edge_info["edge"],
                "won": won,
            }
        )

    return pd.DataFrame(rows)


def _draw_outcomes(
    bets: pd.DataFrame,
    n_simulations: int,
    *,
    mode: OutcomeMode,
    rng: np.random.Generator,
) -> np.ndarray:
    """Shape (n_simulations, n_bets) boolean win indicators."""
    n_bets = len(bets)
    if n_bets == 0:
        return np.zeros((n_simulations, 0), dtype=bool)

    probs = bets["prob"].to_numpy(dtype=float)
    if mode == "bootstrap" and bets["won"].notna().all():
        historical = bets["won"].astype(int).to_numpy()
        idx = rng.integers(0, n_bets, size=(n_simulations, n_bets))
        return historical[idx].astype(bool)

    draws = rng.random((n_simulations, n_bets))
    return draws < probs


def _kelly_fraction_vec(
    prob: float,
    odds: float,
    edge: float,
    bankroll: np.ndarray,
    *,
    kelly_mult: float,
    max_bet_fraction: float,
    min_bet_fraction: float,
    min_edge: float,
) -> np.ndarray:
    kelly = raw_kelly_fraction(prob, odds) * kelly_mult
    kelly = min(kelly, max_bet_fraction)
    stakes = np.where(
        (edge >= min_edge) & (kelly >= min_bet_fraction),
        bankroll * kelly,
        0.0,
    )
    return np.minimum(stakes, bankroll * max_bet_fraction)


def _apply_card_cap_vec(stakes: np.ndarray, bankroll: np.ndarray, max_card_fraction: float) -> np.ndarray:
    total = stakes.sum()
    cap = bankroll * max_card_fraction
    if total <= cap or total <= 0:
        return stakes
    return stakes * (cap / total)


def _simulate_staking_mode(
    bets: pd.DataFrame,
    outcomes: np.ndarray,
    *,
    staking: str,
    initial_bankroll: float,
    flat_stake: float,
    kelly_fraction: float,
    max_bet_fraction: float,
    min_bet_fraction: float,
    max_card_fraction: float,
    min_edge: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Vectorized path simulation for one staking mode.

    Returns (final_equity, max_drawdown_pct, equity_curves sample rows).
    """
    n_sims, n_bets = outcomes.shape
    if n_bets == 0:
        empty = np.full(n_sims, initial_bankroll)
        return empty, np.zeros(n_sims), empty.reshape(n_sims, 1)

    bankroll = np.full(n_sims, initial_bankroll, dtype=float)
    peak = bankroll.copy()
    max_dd = np.zeros(n_sims, dtype=float)
    equity_steps = [bankroll.copy()]

    event_groups = bets.groupby("event_idx", sort=True).groups
    for event_idx in sorted(event_groups.keys()):
        bet_indices = list(event_groups[event_idx])
        raw_stakes = np.zeros(n_sims, dtype=float)
        for bi in bet_indices:
            row = bets.iloc[bi]
            if staking == "flat":
                stake = np.full(n_sims, flat_stake)
            else:
                k_mult = 0.25 if staking == "quarter_kelly" else 0.5
                stake = _kelly_fraction_vec(
                    float(row["prob"]),
                    float(row["odds"]),
                    float(row["edge"]),
                    bankroll,
                    kelly_mult=k_mult,
                    max_bet_fraction=max_bet_fraction,
                    min_bet_fraction=min_bet_fraction,
                    min_edge=min_edge,
                )
            raw_stakes += stake

        cap = bankroll * max_card_fraction
        scale = np.where(raw_stakes > 0, np.minimum(1.0, cap / np.maximum(raw_stakes, 1e-12)), 1.0)

        for bi in bet_indices:
            row = bets.iloc[bi]
            if staking == "flat":
                stake = np.full(n_sims, flat_stake) * scale
            else:
                k_mult = 0.25 if staking == "quarter_kelly" else 0.5
                stake = _kelly_fraction_vec(
                    float(row["prob"]),
                    float(row["odds"]),
                    float(row["edge"]),
                    bankroll,
                    kelly_mult=k_mult,
                    max_bet_fraction=max_bet_fraction,
                    min_bet_fraction=min_bet_fraction,
                    min_edge=min_edge,
                ) * scale

            odds = float(row["odds"])
            won = outcomes[:, bi]
            pnl = np.where(won, stake * (odds - 1.0), -stake)
            bankroll = np.maximum(0.0, bankroll + pnl)
            peak = np.maximum(peak, bankroll)
            dd = np.where(peak > 0, (peak - bankroll) / peak, 0.0)
            max_dd = np.maximum(max_dd, dd)
            equity_steps.append(bankroll.copy())

    curves = np.column_stack(equity_steps)
    return bankroll, max_dd * 100.0, curves


def _risk_metrics(
    final_equity: np.ndarray,
    max_drawdown_pct: np.ndarray,
    *,
    initial_bankroll: float,
    confidence_level: float,
    ruin_threshold_fraction: float,
) -> dict[str, float]:
    returns_pct = (final_equity - initial_bankroll) / initial_bankroll * 100.0
    alpha = 1.0 - confidence_level
    var_return = float(np.percentile(returns_pct, alpha * 100))
    tail = returns_pct[returns_pct <= var_return]
    cvar_return = float(tail.mean()) if len(tail) else var_return

    var_dd = float(np.percentile(max_drawdown_pct, confidence_level * 100))
    tail_dd = max_drawdown_pct[max_drawdown_pct >= var_dd]
    cvar_dd = float(tail_dd.mean()) if len(tail_dd) else var_dd

    ruin_level = initial_bankroll * ruin_threshold_fraction
    ruin_prob = float((final_equity <= ruin_level).mean())

    return {
        "expected_final_equity": float(final_equity.mean()),
        "median_final_equity": float(np.median(final_equity)),
        "expected_return_pct": float(returns_pct.mean()),
        "var_return_pct": var_return,
        "cvar_return_pct": cvar_return,
        "expected_max_drawdown_pct": float(max_drawdown_pct.mean()),
        "median_max_drawdown_pct": float(np.median(max_drawdown_pct)),
        "var_max_drawdown_pct": var_dd,
        "cvar_max_drawdown_pct": cvar_dd,
        "ruin_probability": ruin_prob,
        "prob_positive_return": float((returns_pct > 0).mean()),
    }


def _per_card_pnl_distribution(
    bets: pd.DataFrame,
    outcomes: np.ndarray,
    *,
    staking: str,
    initial_bankroll: float,
    flat_stake: float,
    max_card_fraction: float,
    kelly_fraction: float,
    max_bet_fraction: float,
    min_bet_fraction: float,
    min_edge: float,
) -> pd.DataFrame:
    """Per-event PnL percentiles across simulations (quarter-Kelly default sizing)."""
    if bets.empty:
        return pd.DataFrame()

    n_sims = outcomes.shape[0]
    rows: list[dict[str, Any]] = []
    for event_idx, grp in bets.groupby("event_idx", sort=True):
        idxs = grp.index.to_list()
        pos = [bets.index.get_loc(i) for i in idxs]
        sub_outcomes = outcomes[:, pos]
        sub_bets = bets.loc[idxs].reset_index(drop=True)

        _, _, curves = _simulate_staking_mode(
            sub_bets,
            sub_outcomes,
            staking=staking,
            initial_bankroll=initial_bankroll,
            flat_stake=flat_stake,
            kelly_fraction=kelly_fraction,
            max_bet_fraction=max_bet_fraction,
            min_bet_fraction=min_bet_fraction,
            max_card_fraction=max_card_fraction,
            min_edge=min_edge,
        )
        final = curves[:, -1]
        pnl = final - initial_bankroll
        rows.append(
            {
                "event_idx": int(event_idx),
                "event_name": str(grp["event_name"].iloc[0]),
                "n_bets": len(grp),
                "mean_pnl": float(pnl.mean()),
                "p5_pnl": float(np.percentile(pnl, 5)),
                "p50_pnl": float(np.percentile(pnl, 50)),
                "p95_pnl": float(np.percentile(pnl, 95)),
                "prob_loss": float((pnl < 0).mean()),
            }
        )
    return pd.DataFrame(rows)


def _rolling_card_risk(per_card: pd.DataFrame, *, window: int = 3) -> pd.DataFrame:
    if per_card.empty or len(per_card) < window:
        return pd.DataFrame()
    work = per_card.sort_values("event_idx").copy()
    work["rolling_mean_pnl"] = work["mean_pnl"].rolling(window).mean()
    work["rolling_prob_loss"] = work["prob_loss"].rolling(window).mean()
    work["rolling_p5_pnl"] = work["p5_pnl"].rolling(window).min()
    return work.dropna()


def _collect_warnings(summaries: dict[str, dict[str, float]]) -> list[str]:
    warnings: list[str] = []
    dd_warn = config.MC_HIGH_DRAWDOWN_WARN_PCT
    ruin_warn = config.MC_HIGH_RUIN_WARN_PROB
    for mode, stats in summaries.items():
        exp_dd = stats.get("expected_max_drawdown_pct", 0)
        var_dd = stats.get("var_max_drawdown_pct", 0)
        ruin = stats.get("ruin_probability", 0)
        if exp_dd >= dd_warn:
            warnings.append(
                f"{mode}: expected max drawdown {exp_dd:.1f}% exceeds {dd_warn:.0f}% threshold."
            )
        if var_dd >= dd_warn * 1.5:
            warnings.append(
                f"{mode}: {config.MC_CONFIDENCE_LEVEL:.0%} VaR max drawdown {var_dd:.1f}% — "
                "tail risk elevated."
            )
        if ruin >= ruin_warn:
            warnings.append(
                f"{mode}: ruin probability {ruin:.1%} exceeds {ruin_warn:.0%} guardrail."
            )
    return warnings


def run_monte_carlo(
    backtest_results: Any,
    n_simulations: int | None = None,
    random_seed: int = 42,
    *,
    outcome_mode: OutcomeMode | None = None,
    initial_bankroll: float | None = None,
    confidence_level: float | None = None,
    min_edge: float | None = None,
    sample_paths: int = 25,
) -> MonteCarloResult:
    """
    Bootstrap or parametric Monte Carlo over historical value-bet paths.

    Tracks flat, quarter-Kelly, and half-Kelly equity curves.
    """
    n_simulations = n_simulations or config.MC_SIMULATIONS
    confidence_level = confidence_level or config.MC_CONFIDENCE_LEVEL
    initial_bankroll = initial_bankroll or config.INITIAL_BANKROLL
    min_edge = min_edge if min_edge is not None else config.MIN_EDGE

    predictions = _resolve_predictions(backtest_results)
    bets = build_bet_schedule(predictions, min_edge=min_edge)
    if bets.empty:
        return MonteCarloResult(
            n_simulations=n_simulations,
            outcome_mode="none",
            initial_bankroll=initial_bankroll,
            confidence_level=confidence_level,
            warnings=["No value bets with odds found for Monte Carlo."],
        )

    mode: OutcomeMode = outcome_mode or (
        "bootstrap" if bets["won"].notna().all() else "parametric"
    )
    rng = np.random.default_rng(random_seed)
    outcomes = _draw_outcomes(bets, n_simulations, mode=mode, rng=rng)

    flat_stake = config.FLAT_STAKE
    max_bet = config.MC_MAX_BET_FRACTION
    min_bet = config.MC_MIN_BET_FRACTION
    max_card = config.MC_MAX_CARD_RISK_FRACTION

    summaries: dict[str, dict[str, float]] = {}
    dd_dist: dict[str, list[float]] = {}
    eq_dist: dict[str, list[float]] = {}
    sample_curves: dict[str, list[list[float]]] = {}

    for staking in STAKING_MODES:
        kelly_frac = 0.25 if staking == "quarter_kelly" else (0.5 if staking == "half_kelly" else 0.25)
        final_eq, max_dd, curves = _simulate_staking_mode(
            bets,
            outcomes,
            staking=staking,
            initial_bankroll=initial_bankroll,
            flat_stake=flat_stake,
            kelly_fraction=kelly_frac,
            max_bet_fraction=max_bet,
            min_bet_fraction=min_bet,
            max_card_fraction=max_card,
            min_edge=min_edge,
        )
        stats = _risk_metrics(
            final_eq,
            max_dd,
            initial_bankroll=initial_bankroll,
            confidence_level=confidence_level,
            ruin_threshold_fraction=config.MC_RUIN_THRESHOLD_FRACTION,
        )
        stats["n_bets"] = float(len(bets))
        stats["n_cards"] = float(bets["event_idx"].nunique())
        summaries[staking] = stats
        dd_dist[staking] = max_dd.tolist()
        eq_dist[staking] = final_eq.tolist()

        n_sample = min(sample_paths, curves.shape[0])
        pick = rng.choice(curves.shape[0], size=n_sample, replace=False)
        sample_curves[staking] = curves[pick].tolist()

    per_card = _per_card_pnl_distribution(
        bets,
        outcomes,
        staking="quarter_kelly",
        initial_bankroll=initial_bankroll,
        flat_stake=flat_stake,
        max_card_fraction=max_card,
        kelly_fraction=0.25,
        max_bet_fraction=max_bet,
        min_bet_fraction=min_bet,
        min_edge=min_edge,
    )
    rolling = _rolling_card_risk(per_card, window=min(3, max(1, len(per_card))))

    warnings = _collect_warnings(summaries)
    return MonteCarloResult(
        n_simulations=n_simulations,
        outcome_mode=mode,
        initial_bankroll=initial_bankroll,
        confidence_level=confidence_level,
        staking_summaries=summaries,
        max_drawdown_distribution=dd_dist,
        final_equity_distribution=eq_dist,
        per_card_pnl=per_card,
        rolling_card_risk=rolling,
        sample_equity_curves=sample_curves,
        warnings=warnings,
    )


def recommended_card_risk_fraction(
    card_risk: dict[str, Any],
    base_cap: float | None = None,
) -> tuple[float, list[str]]:
    """
    Lower per-card exposure when Monte Carlo shows high variance or ruin risk.

    Returns (adjusted_cap_fraction, warnings).
    """
    base = base_cap if base_cap is not None else config.MC_MAX_CARD_RISK_FRACTION
    warnings: list[str] = []
    prob_loss = float(card_risk.get("prob_loss", 0))
    p5_pnl = float(card_risk.get("p5_pnl", 0))
    ruin = float(card_risk.get("ruin_probability", 0))
    bankroll = float(card_risk.get("bankroll", config.INITIAL_BANKROLL))

    cap = base
    if ruin > config.MC_HIGH_RUIN_WARN_PROB:
        cap *= 0.5
        warnings.append(f"Ruin risk {ruin:.1%} — halving card cap to {cap:.1%}.")
    elif prob_loss > 0.55:
        cap *= 0.75
        warnings.append(f"Loss probability {prob_loss:.1%} on card — reducing cap to {cap:.1%}.")
    elif p5_pnl < -0.05 * bankroll:
        cap *= 0.85
        warnings.append(
            f"5th percentile card PnL ${p5_pnl:,.0f} — trimming cap to {cap:.1%}."
        )

    floor = config.MC_MIN_CARD_RISK_FRACTION
    cap = float(np.clip(cap, floor, base))
    return cap, warnings


def assess_upcoming_card_risk(
    predictions_df: pd.DataFrame,
    bankroll: float = 10_000,
    simulations: int | None = None,
    *,
    random_seed: int = 42,
    min_edge: float | None = None,
) -> dict[str, Any]:
    """
    Monte Carlo for a single upcoming card (parametric outcomes from model probs).

    Returns risk metrics and suggested max risk % for the card.
    """
    simulations = simulations or config.MC_CARD_SIMULATIONS
    min_edge = min_edge if min_edge is not None else config.MIN_EDGE

    bets = build_bet_schedule(predictions_df, min_edge=min_edge)
    if bets.empty:
        return {
            "available": False,
            "reason": "No value bets with odds on this card.",
            "suggested_max_risk_pct": 0.0,
            "warnings": ["No bets meet min edge — skip card exposure."],
        }

    if bets["event_idx"].nunique() > 1:
        logger.info("assess_upcoming_card_risk: multiple events in frame; analyzing all value bets.")

    rng = np.random.default_rng(random_seed)
    outcomes = _draw_outcomes(bets, simulations, mode="parametric", rng=rng)

    flat_stake = config.FLAT_STAKE
    max_bet = config.MC_MAX_BET_FRACTION
    min_bet = config.MC_MIN_BET_FRACTION
    base_cap = config.MC_MAX_CARD_RISK_FRACTION

    mode_stats: dict[str, dict[str, float]] = {}
    for staking in STAKING_MODES:
        final_eq, max_dd, _ = _simulate_staking_mode(
            bets,
            outcomes,
            staking=staking,
            initial_bankroll=bankroll,
            flat_stake=flat_stake,
            kelly_fraction=0.25,
            max_bet_fraction=max_bet,
            min_bet_fraction=min_bet,
            max_card_fraction=base_cap,
            min_edge=min_edge,
        )
        mode_stats[staking] = _risk_metrics(
            final_eq,
            max_dd,
            initial_bankroll=bankroll,
            confidence_level=config.MC_CONFIDENCE_LEVEL,
            ruin_threshold_fraction=config.MC_RUIN_THRESHOLD_FRACTION,
        )

    pnl = final_eq - bankroll
    card_risk = {
        "bankroll": bankroll,
        "n_bets": len(bets),
        "mean_pnl": float(pnl.mean()),
        "p5_pnl": float(np.percentile(pnl, 5)),
        "p50_pnl": float(np.percentile(pnl, 50)),
        "p95_pnl": float(np.percentile(pnl, 95)),
        "prob_loss": float((pnl < 0).mean()),
        "ruin_probability": mode_stats["quarter_kelly"]["ruin_probability"],
        "expected_max_drawdown_pct": mode_stats["quarter_kelly"]["expected_max_drawdown_pct"],
    }
    suggested_cap, cap_warnings = recommended_card_risk_fraction(card_risk, base_cap)

    warnings = _collect_warnings(mode_stats) + cap_warnings
    return {
        "available": True,
        "bankroll": bankroll,
        "n_simulations": simulations,
        "n_bets": len(bets),
        "bets": bets.to_dict(orient="records"),
        "staking_modes": mode_stats,
        "card_pnl": card_risk,
        "suggested_max_risk_pct": suggested_cap * 100.0,
        "suggested_max_risk_fraction": suggested_cap,
        "base_max_risk_pct": base_cap * 100.0,
        "warnings": warnings,
    }


def enrich_staking_modes_with_mc(
    staking_modes: pd.DataFrame,
    mc_result: MonteCarloResult,
) -> pd.DataFrame:
    """Merge Monte Carlo drawdown stats into staking_modes summary rows."""
    if staking_modes.empty or not mc_result.staking_summaries:
        return staking_modes

    out = staking_modes.copy()
    mc_map = {
        "flat": "flat",
        "quarter_kelly": "quarter_kelly",
        "half_kelly": "half_kelly",
    }
    for staking_key, col_prefix in mc_map.items():
        stats = mc_result.staking_summaries.get(staking_key, {})
        if not stats:
            continue
        mask = out["staking"].astype(str) == staking_key
        if not mask.any() and staking_key == "flat":
            mask = out["staking"].astype(str).str.contains("flat", case=False, na=False)
        for field_name, value in stats.items():
            out.loc[mask, f"mc_{field_name}"] = value
    return out


def save_monte_carlo_report(
    mc_result: MonteCarloResult,
    report_dir: Any,
    *,
    year: int | None = None,
) -> None:
    """Persist Monte Carlo JSON + per-card CSV."""
    from pathlib import Path

    out_dir = Path(report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"_{year}" if year else ""
    summary_path = out_dir / f"monte_carlo{suffix}_summary.json"
    payload = {k: v for k, v in mc_result.to_dict().items() if k != "sample_equity_curves"}
    summary_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    if not mc_result.per_card_pnl.empty:
        mc_result.per_card_pnl.to_csv(out_dir / f"monte_carlo{suffix}_per_card.csv", index=False)
    if mc_result.sample_equity_curves:
        sample_rows = []
        for mode, curves in mc_result.sample_equity_curves.items():
            for i, curve in enumerate(curves):
                for step, eq in enumerate(curve):
                    sample_rows.append({"staking": mode, "path_id": i, "step": step, "equity": eq})
        pd.DataFrame(sample_rows).to_csv(
            out_dir / f"monte_carlo{suffix}_equity_samples.csv",
            index=False,
        )


class DrawdownHalt:
    """
    Peak bankroll drawdown monitor — blocks new alerts when breach exceeds profile limit.
    Persisted state survives watch-loop restarts.
    """

    def __init__(self) -> None:
        self._load()

    def _load(self) -> None:
        from src.safe_io import read_json_file

        data = read_json_file(config.DRAWDOWN_STATE_PATH)
        self.peak_bankroll: float | None = data.get("peak_bankroll")
        self.halted: bool = bool(data.get("halted"))
        self.halt_events: int = int(data.get("halt_events", 0))
        self.resume_events: int = int(data.get("resume_events", 0))

    def _save(self) -> None:
        from src.safe_io import write_json_atomic

        write_json_atomic(
            config.DRAWDOWN_STATE_PATH,
            {
                "peak_bankroll": self.peak_bankroll,
                "halted": self.halted,
                "halt_events": self.halt_events,
                "resume_events": self.resume_events,
            },
        )

    def _max_dd(self) -> float:
        return config.profile_value("max_drawdown_fraction")

    def _resume_dd(self) -> float:
        return config.profile_value("resume_drawdown_fraction")

    def current_drawdown(self, bankroll: float) -> float:
        if self.peak_bankroll is None or self.peak_bankroll <= 0:
            return 0.0
        return (self.peak_bankroll - bankroll) / self.peak_bankroll

    def update_peak(self, bankroll: float) -> None:
        if bankroll <= 0:
            return
        if self.peak_bankroll is None or bankroll > self.peak_bankroll:
            self.peak_bankroll = bankroll
            self._save()

    def check(self, bankroll: float) -> tuple[bool, str]:
        """
        Returns (alerts_allowed, reason_if_blocked).
        """
        if not config.DRAWDOWN_HALT_ENABLED:
            self.update_peak(bankroll)
            return True, ""

        self.update_peak(bankroll)
        dd = self.current_drawdown(bankroll)
        max_dd = self._max_dd()
        resume_dd = self._resume_dd()

        if not self.halted:
            if dd >= max_dd:
                self.halted = True
                self.halt_events += 1
                self._save()
                reason = f"peak drawdown halt ({dd:.1%} >= {max_dd:.1%})"
                logger.warning(reason)
                return False, reason
            return True, ""

        if dd < resume_dd:
            self.halted = False
            self.resume_events += 1
            self._save()
            logger.info("Drawdown resume: %.1f%% below %.0f%% threshold", dd * 100, resume_dd * 100)
            return True, ""

        return False, f"drawdown halt active ({dd:.1%} vs resume {resume_dd:.1%})"

    def status(self, bankroll: float | None = None) -> dict[str, float | bool | None]:
        dd = self.current_drawdown(bankroll) if bankroll is not None else None
        return {
            "halted": self.halted,
            "peak_bankroll": self.peak_bankroll,
            "current_drawdown_pct": round(dd * 100, 2) if dd is not None else None,
            "max_drawdown_pct": round(self._max_dd() * 100, 1),
            "halt_events": self.halt_events,
        }


_drawdown_halt: DrawdownHalt | None = None


def get_drawdown_halt() -> DrawdownHalt:
    global _drawdown_halt
    if _drawdown_halt is None:
        _drawdown_halt = DrawdownHalt()
    return _drawdown_halt


def check_bankroll_safety(bankroll: float) -> tuple[bool, str]:
    """Drawdown halt + delegates to circuit breaker."""
    from src.circuit_breaker import check_alerts_allowed

    halt = get_drawdown_halt()
    allowed, reason = halt.check(bankroll)
    if not allowed:
        return False, reason
    return check_alerts_allowed(bankroll, drawdown_halted=False)

