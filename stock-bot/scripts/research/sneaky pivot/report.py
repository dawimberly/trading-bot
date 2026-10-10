"""
Reporting for Sneaky Pivot v2 trade output.
Phase 2/3 hooks: rhyme breakdown, TOD x rhyme x sleeve matrix.
Generic enough to reuse for other strategies later (Phase 4).
"""

from __future__ import annotations

import pandas as pd

from harness import label_rhyme_for_universe, BarCache


def summary_stats(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"n_trades": 0}

    wins = trades["pnl_bps"] > 0
    return {
        "n_trades": len(trades),
        "win_rate": wins.mean(),
        "avg_r_multiple": trades["r_multiple"].mean(),
        "avg_pnl_bps": trades["pnl_bps"].mean(),
        "total_pnl_bps": trades["pnl_bps"].sum(),
        "exit_reason_counts": trades["exit_reason"].value_counts().to_dict(),
    }


def rhyme_breakdown(trades: pd.DataFrame, cache: BarCache | None = None) -> pd.DataFrame:
    """
    Attach market-wide RHYME A-E (per day) to each trade and summarize by regime.
    `cache` is unused (kept for call-site compatibility with Phase 0+1 runner).
    """
    if trades.empty:
        return pd.DataFrame()
    del cache

    days = sorted(trades["session_date"].unique().tolist())
    rhyme_map = label_rhyme_for_universe(days)

    merged = trades.merge(
        rhyme_map, left_on="session_date", right_on="day", how="left",
    )

    grouped = merged.groupby("rhyme").agg(
        n_trades=("pnl_bps", "count"),
        win_rate=("pnl_bps", lambda s: (s > 0).mean()),
        avg_r_multiple=("r_multiple", "mean"),
        avg_pnl_bps=("pnl_bps", "mean"),
        total_pnl_bps=("pnl_bps", "sum"),
    ).reset_index()

    return grouped


def tod_x_rhyme_matrix(
    trades: pd.DataFrame, cache: BarCache | None = None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Phase 3: TOD bucket x RHYME label matrix.
    `cache` unused — rhyme is market-wide by day.
    """
    if trades.empty:
        return pd.DataFrame(), pd.DataFrame()
    del cache

    days = sorted(trades["session_date"].unique().tolist())
    rhyme_map = label_rhyme_for_universe(days)

    merged = trades.merge(
        rhyme_map, left_on="session_date", right_on="day", how="left",
    )

    pivot_pnl = merged.pivot_table(
        index="tod_bucket_at_entry", columns="rhyme",
        values="pnl_bps", aggfunc="mean",
    )
    pivot_win = merged.pivot_table(
        index="tod_bucket_at_entry", columns="rhyme",
        values="pnl_bps", aggfunc=lambda s: (s > 0).mean(),
    )

    return pivot_pnl, pivot_win


def per_sleeve_breakdown(trades: pd.DataFrame, sleeve_map: dict[str, list[str]]) -> pd.DataFrame:
    """
    Phase 4 prep: tag each trade with its sleeve so the same harness can
    later report across nyse_momentum / crypto_vol / etc side by side.
    """
    if trades.empty:
        return pd.DataFrame()

    symbol_to_sleeve = {}
    for sleeve, symbols in sleeve_map.items():
        for sym in symbols:
            symbol_to_sleeve[sym] = sleeve

    trades = trades.copy()
    trades["sleeve"] = trades["symbol"].map(symbol_to_sleeve).fillna("unassigned")

    return trades.groupby("sleeve").agg(
        n_trades=("pnl_bps", "count"),
        win_rate=("pnl_bps", lambda s: (s > 0).mean()),
        avg_pnl_bps=("pnl_bps", "mean"),
        total_pnl_bps=("pnl_bps", "sum"),
    ).reset_index()
