"""Conservative dynamic sector screener — expand momentum/stat-arb pools in strong sectors."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

import config
from modules.safe_io import append_jsonl_line, read_json_file, write_json_file

logger = logging.getLogger(__name__)

# Sector SPDR ETFs -> short banner label + internal sector tags for ticker pools
SECTOR_ETF_DEFS: dict[str, tuple[str, tuple[str, ...]]] = {
    "XLK": ("Tech", ("Tech",)),
    "XLE": ("Energy", ("Energy",)),
    "XLI": ("Industrials", ("Defense",)),
    "XLF": ("Financials", ("Financials",)),
    "XLV": ("Healthcare", ("Healthcare",)),
    "XLY": ("Consumer Disc", ("Consumer",)),
    "XLP": ("Staples", ("Consumer",)),
    "XLB": ("Materials", ("Materials",)),
    "XLU": ("Utilities", ("Utilities",)),
    "XLRE": ("Real Estate", ("Real Estate",)),
    "XLC": ("Comm", ("Tech",)),
}

# Extra liquid names per sector tag (beyond static maps) for controlled expansion
SECTOR_EXTRA_BY_TAG: dict[str, tuple[str, ...]] = {
    "Tech": (
        "ORCL",
        "ADBE",
        "QCOM",
        "AVGO",
        "NOW",
        "PANW",
        "CSCO",
        "TXN",
        "AMAT",
        "LRCX",
        "KLAC",
        "SNPS",
        "CDNS",
        "MRVL",
        "FTNT",
        "CRWD",
        "DDOG",
        "NET",
        "UBER",
        "ABNB",
    ),
    "Energy": (
        "EOG",
        "SLB",
        "MPC",
        "PSX",
        "VLO",
        "OXY",
        "HAL",
        "DVN",
        "HES",
        "BKR",
        "FANG",
        "PXD",
        "WMB",
        "KMI",
        "OKE",
    ),
    "Financials": (
        "WFC",
        "C",
        "USB",
        "PNC",
        "TFC",
        "SCHW",
        "BLK",
        "AXP",
        "COF",
        "CB",
        "MMC",
        "ICE",
        "CME",
        "SPGI",
        "MCO",
    ),
    "Healthcare": (
        "ABBV",
        "MRK",
        "TMO",
        "ABT",
        "DHR",
        "BMY",
        "AMGN",
        "GILD",
        "VRTX",
        "REGN",
        "ISRG",
        "SYK",
        "MDT",
        "CI",
        "ELV",
    ),
    "Defense": (
        "BA",
        "GE",
        "CAT",
        "DE",
        "HON",
        "UPS",
        "UNP",
        "CSX",
        "NSC",
        "FDX",
        "EMR",
        "ETN",
        "ITW",
        "PH",
        "ROK",
    ),
    "Consumer": (
        "NKE",
        "SBUX",
        "MCD",
        "LOW",
        "TGT",
        "BKNG",
        "CMG",
        "MAR",
        "YUM",
        "ROST",
        "TJX",
        "ORLY",
        "AZO",
        "DG",
        "DLTR",
    ),
    "Materials": (
        "LIN",
        "APD",
        "SHW",
        "ECL",
        "FCX",
        "NEM",
        "NUE",
        "DOW",
        "DD",
        "VMC",
        "MLM",
        "PPG",
        "ALB",
        "CF",
        "MOS",
    ),
    "Utilities": (
        "NEE",
        "DUK",
        "SO",
        "D",
        "AEP",
        "EXC",
        "SRE",
        "XEL",
        "ED",
        "WEC",
        "PEG",
        "ES",
        "AWK",
        "ETR",
        "FE",
    ),
    "Real Estate": (
        "PLD",
        "AMT",
        "EQIX",
        "SPG",
        "O",
        "PSA",
        "WELL",
        "DLR",
        "AVB",
        "EQR",
        "VTR",
        "ARE",
        "MAA",
        "UDR",
        "ESS",
    ),
}

# Broader liquid NYSE names used for momentum fallback and backtest prefetch
LIQUID_NYSE_FALLBACK: tuple[str, ...] = (
    "JPM",
    "BAC",
    "WMT",
    "PG",
    "KO",
    "PEP",
    "DIS",
    "V",
    "MA",
    "HD",
    "COST",
    "CVX",
    "IBM",
    "GS",
    "MS",
    "RTX",
    "LMT",
    "NOC",
    "GD",
    "MO",
    "PM",
    "CL",
    "MDLZ",
    "KHC",
    "GIS",
    "HSY",
    "HIG",
    "MET",
    "PRU",
    "ALL",
    "AIG",
    "TRV",
    "CINF",
    "AFL",
    "NDAQ",
    "CBOE",
    "BK",
    "STT",
    "RF",
    "KEY",
    "FITB",
    "HBAN",
    "CFG",
    "MTB",
    "ZION",
    "SYY",
    "KR",
    "CVS",
    "HUM",
    "CNC",
    "MOH",
    "WM",
    "RSG",
    "FAST",
    "CTAS",
    "PCAR",
    "URI",
    "GWW",
    "CMI",
    "EMN",
    "IP",
    "PKG",
    "WRK",
)

STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "sector_screener_state.json"
MOMENTUM_LOOKBACK = 63
_log_day: str | None = None
_sector_pools_cache: dict[str, list[str]] | None = None


def sector_etf_symbols() -> tuple[str, ...]:
    return tuple(SECTOR_ETF_DEFS.keys())


def sector_expansion_prefetch_tickers() -> list[str]:
    """All candidate expansion tickers for backtest data prefetch."""
    pools = _sector_ticker_pools()
    out: set[str] = set(LIQUID_NYSE_FALLBACK)
    for tickers in pools.values():
        out.update(tickers)
    return sorted(out)


def _sector_ticker_pools() -> dict[str, list[str]]:
    global _sector_pools_cache
    if _sector_pools_cache is not None:
        return _sector_pools_cache

    from modules.dynamic_universe import EQUITY_SECTOR_MAP, sector_for_symbol
    from modules.pipeline_strategies import NYSE_SECTOR_MAP

    pools: dict[str, set[str]] = {}
    for sym, sector in {**EQUITY_SECTOR_MAP, **NYSE_SECTOR_MAP}.items():
        pools.setdefault(sector, set()).add(sym)
    for tag, extras in SECTOR_EXTRA_BY_TAG.items():
        pools.setdefault(tag, set()).update(extras)
    try:
        screener = config.load_screener_universe_tickers() or []
    except Exception as exc:
        logger.debug("screener universe tickers unavailable for sector pools: %s", exc)
        screener = []
    for sym in screener:
        sec = sector_for_symbol(sym)
        if sec:
            pools.setdefault(sec, set()).add(sym)

    _sector_pools_cache = {k: sorted(v) for k, v in pools.items()}
    return _sector_pools_cache


def _momentum_return(prices: pd.Series, lookback: int = MOMENTUM_LOOKBACK) -> float:
    if len(prices) < 2:
        return 0.0
    lb = min(lookback, len(prices) - 1)
    if lb < 5:
        return 0.0
    start = float(prices.iloc[-lb - 1])
    end = float(prices.iloc[-1])
    if start <= 0:
        return 0.0
    return (end / start) - 1.0


def _above_ma(prices: pd.Series, window: int) -> bool:
    if len(prices) < max(20, window // 4):
        return False
    w = min(window, len(prices))
    ma = float(prices.rolling(window=w).mean().iloc[-1])
    current = float(prices.iloc[-1])
    return ma > 0 and current > ma


def _sector_qualifies(row: dict[str, Any]) -> bool:
    """Conservative but usable: trend + configurable RS/score floors."""
    rs_min = float(getattr(config, "SECTOR_RS_MIN", 0.0))
    score_min = float(getattr(config, "SECTOR_STRENGTH_THRESHOLD", 0.0))
    if row["rs_vs_spy"] < rs_min or row["score"] < score_min:
        return False
    if row["above_ma200"]:
        return True
    # Soft trend: shorter MA + non-negative momentum (no dramatic SPY beat required)
    if row.get("above_ma_short") and row.get("momentum", 0.0) >= -0.02:
        return True
    return False


def compute_sector_strengths(data: pd.DataFrame) -> list[dict[str, Any]]:
    """Rank sector ETFs by momentum + relative strength vs SPY."""
    if data is None or data.empty:
        return []

    spy_sym = config.SPY_BOT_SYMBOL
    if spy_sym not in data.columns:
        return []

    spy_prices = data[spy_sym].dropna()
    if len(spy_prices) < 20:
        return []

    ma_window = max(20, int(config.SECTOR_STRENGTH_MA_WINDOW))
    short_ma = min(100, ma_window)
    rows: list[dict[str, Any]] = []

    for etf, (label, tags) in SECTOR_ETF_DEFS.items():
        if etf not in data.columns:
            continue
        etf_prices = data[etf].dropna()
        if len(etf_prices) < 20:
            continue
        mom = _momentum_return(etf_prices)
        spy_mom = _momentum_return(spy_prices)
        rs = mom - spy_mom
        above_ma = _above_ma(etf_prices, ma_window)
        above_short = _above_ma(etf_prices, short_ma)
        score = mom + rs
        rows.append(
            {
                "etf": etf,
                "label": label,
                "sector_tags": list(tags),
                "momentum": float(mom),
                "rs_vs_spy": float(rs),
                "score": float(score),
                "above_ma200": bool(above_ma),
                "above_ma_short": bool(above_short),
            }
        )

    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def compute_sector_regime_score(data: pd.DataFrame, strengths: list[dict[str, Any]] | None = None) -> float:
    """Blend top-sector momentum/RS with cross-sector breadth into a single 0-1 regime score.

    0.0 = broad sector weakness / narrow leadership, 0.5 = neutral, 1.0 = broad strong rotation.
    Used by portfolio_constructor + dynamic_vti_allocator as a sector-rotation signal — separate
    from get_active_sectors()'s trend/RS *qualification* filter, which decides universe expansion.
    """
    rows = strengths if strengths is not None else compute_sector_strengths(data)
    if not rows:
        return 0.5

    qualifying = [r for r in rows if _sector_qualifies(r)]
    breadth = len(qualifying) / len(rows)

    top_n = rows[: min(3, len(rows))]
    avg_top_score = sum(r["score"] for r in top_n) / len(top_n)
    avg_top_rs = sum(r["rs_vs_spy"] for r in top_n) / len(top_n)

    # Scale momentum/RS components relative to the existing "strong sector" threshold so the
    # score stays consistent with SECTOR_STRONG_SCORE_MIN as strength/weakness fine-tunes.
    scale_ref = max(1e-6, 2.0 * float(getattr(config, "SECTOR_STRONG_SCORE_MIN", 0.06)))
    score_component = _clamp01(0.5 + (avg_top_score / scale_ref) * 0.5)
    rs_component = _clamp01(0.5 + (avg_top_rs / scale_ref) * 0.5)

    regime_score = 0.45 * score_component + 0.25 * rs_component + 0.30 * breadth
    return round(_clamp01(regime_score), 4)


def _max_active_sectors(data: pd.DataFrame | None) -> int:
    """Up to MAX_ACTIVE_SECTORS_STRONG (4) when top sector score is elevated."""
    base = int(config.MAX_ACTIVE_SECTORS)
    strong_cap = int(getattr(config, "MAX_ACTIVE_SECTORS_STRONG", 4))
    if data is None or getattr(data, "empty", True):
        return base
    strengths = compute_sector_strengths(data)
    if not strengths:
        return base
    top_score = float(strengths[0].get("score", 0.0))
    threshold = float(getattr(config, "SECTOR_STRONG_SCORE_MIN", 0.06))
    if top_score >= threshold:
        return max(base, strong_cap)
    return base


def get_active_sectors(data: pd.DataFrame) -> list[dict[str, Any]]:
    """Top sectors passing relaxed trend + RS/score filters (dynamic sector cap)."""
    cap = _max_active_sectors(data)
    strengths = compute_sector_strengths(data)
    active: list[dict[str, Any]] = []
    for row in strengths:
        if not _sector_qualifies(row):
            continue
        active.append(row)
        if len(active) >= cap:
            break
    if active:
        return active
    # Secondary: top names by score with soft trend only (choppy / neutral RS periods)
    for row in strengths:
        if not row.get("above_ma_short"):
            continue
        if row.get("momentum", 0.0) < 0:
            continue
        active.append(row)
        if len(active) >= cap:
            break
    return active


def sector_regime_snapshot(data: pd.DataFrame) -> dict[str, Any]:
    """Single-pass sector strength + regime score for allocation callers (portfolio_constructor).

    Computes compute_sector_strengths() once and derives both active sectors and the regime
    score from it, avoiding the redundant recompute that get_active_sectors()/_max_active_sectors()
    each trigger independently.
    """
    strengths = compute_sector_strengths(data)
    cap = _max_active_sectors(data) if strengths else int(config.MAX_ACTIVE_SECTORS)
    active: list[dict[str, Any]] = [r for r in strengths if _sector_qualifies(r)][:cap]
    if not active:
        active = [
            r
            for r in strengths
            if r.get("above_ma_short") and r.get("momentum", 0.0) >= 0
        ][:cap]
    return {
        "strengths": strengths,
        "active_sectors": active,
        "sector_regime_score": compute_sector_regime_score(data, strengths=strengths),
    }


def _rank_by_momentum(symbols: list[str], data: pd.DataFrame) -> list[str]:
    scored: list[tuple[float, str]] = []
    for sym in symbols:
        if sym not in data.columns:
            continue
        prices = data[sym].dropna()
        if len(prices) < 10:
            continue
        scored.append((_momentum_return(prices, lookback=20), sym))
    scored.sort(reverse=True)
    ordered = [s for _, s in scored]
    if config.effective_rvol_scanner_enabled():
        try:
            from modules.volume_analysis import rvol_momentum_rank_boost

            boosted: list[tuple[float, str]] = []
            for score, sym in scored:
                boosted.append((score + rvol_momentum_rank_boost(sym, data), sym))
            boosted.sort(reverse=True)
            ordered = [s for _, s in boosted]
        except Exception as exc:
            logger.debug("RVOL sector rank boost skipped: %s", exc)
    for sym in symbols:
        if sym not in ordered:
            ordered.append(sym)
    return ordered


def _expansion_candidates(
    sector_tags: tuple[str, ...] | list[str],
    data_columns,
    data: pd.DataFrame,
) -> list[str]:
    pools = _sector_ticker_pools()
    candidates: list[str] = []
    seen: set[str] = set()
    col_set = set(data_columns)
    for tag in sector_tags:
        for sym in pools.get(tag, []):
            if sym in seen or sym not in col_set:
                continue
            if not config._nyse_eligible_symbol(sym):
                continue
            seen.add(sym)
            candidates.append(sym)
    if config.effective_rvol_scanner_enabled():
        try:
            from modules.volume_analysis import filter_symbols_by_rvol

            candidates = filter_symbols_by_rvol(candidates, data)
        except Exception as exc:
            logger.debug("RVOL sector candidate filter skipped: %s", exc)
    return _rank_by_momentum(candidates, data)


def _eligible_momentum_universe(data_columns) -> list[str]:
    return [
        str(c)
        for c in data_columns
        if config._nyse_eligible_symbol(c) and str(c) != config.SPY_BOT_SYMBOL
    ]


def _add_momentum_fallback(
    pool: set[str],
    added_tickers: list[str],
    *,
    data_columns,
    data: pd.DataFrame,
    max_total: int,
    count: int,
) -> int:
    """Add top-momentum names when sector expansion adds nothing."""
    if count <= 0 or len(pool) >= max_total:
        return 0
    col_set = set(data_columns)
    fallback_pool = [
        s
        for s in LIQUID_NYSE_FALLBACK
        if s in col_set and s not in pool and config._nyse_eligible_symbol(s)
    ]
    ranked = _rank_by_momentum(fallback_pool, data)
    candidates = ranked + [
        s
        for s in _eligible_momentum_universe(data_columns)
        if s not in pool and s not in ranked
    ]
    added = 0
    for sym in candidates[:count]:
        if len(pool) >= max_total:
            break
        pool.add(sym)
        added_tickers.append(sym)
        added += 1
    return added


def _reference_date(data: pd.DataFrame | None) -> str:
    if data is not None and not data.empty:
        idx = data.index[-1]
        if hasattr(idx, "strftime"):
            return idx.strftime("%Y-%m-%d")
        return str(idx)[:10]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _state_path() -> Path:
    raw = getattr(config, "SECTOR_SCREENER_STATE_FILE", "") or str(STATE_PATH)
    p = Path(raw)
    if not p.is_absolute():
        p = Path(__file__).resolve().parents[1] / p
    return p


def _log_path() -> Path:
    raw = getattr(config, "SECTOR_SCREENER_LOG_FILE", "logs/sector_screener.jsonl")
    p = Path(raw)
    if not p.is_absolute():
        p = Path(__file__).resolve().parents[1] / p
    return p


def load_sector_screener_snapshot() -> dict[str, Any]:
    snap = read_json_file(_state_path())
    return snap if isinstance(snap, dict) else {}


def _persist_snapshot(payload: dict[str, Any]) -> None:
    write_json_file(_state_path(), payload)


def _maybe_log_daily(
    *,
    ref_date: str,
    active: list[dict[str, Any]],
    base_count: int,
    total_count: int,
    expansion_count: int,
    expanded_sectors: list[str],
    added_tickers: list[str],
    fallback_count: int = 0,
) -> None:
    global _log_day
    if _log_day == ref_date:
        return
    _log_day = ref_date

    payload = {
        "date": ref_date,
        "active_sectors": [s["label"] for s in active],
        "expanded_sectors": expanded_sectors,
        "base_count": base_count,
        "total_count": total_count,
        "expansion_count": expansion_count,
        "fallback_momentum_count": fallback_count,
        "added_tickers": added_tickers,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    append_jsonl_line(_log_path(), payload)
    _persist_snapshot(payload)
    logger.info(
        "Sector screener %s: active=%s +%d tickers (total %d%s)",
        ref_date,
        " + ".join(payload["active_sectors"]) or "none",
        expansion_count,
        total_count,
        f", fallback={fallback_count}" if fallback_count else "",
    )


def _market_vol_annualized(data: pd.DataFrame, *, window: int = 20) -> float | None:
    spy = config.SPY_BOT_SYMBOL
    if data is None or getattr(data, "empty", True) or spy not in data.columns:
        return None
    prices = data[spy].dropna()
    if len(prices) < window + 5:
        return None
    rets = prices.pct_change().dropna().tail(window)
    if len(rets) < 5:
        return None
    daily = float(rets.std())
    if daily <= 0:
        return None
    return daily * (252**0.5)


def _high_vol_market(data: pd.DataFrame | None) -> bool:
    if not config.effective_tail_risk_controls():
        return False
    ann = _market_vol_annualized(data)
    if ann is None:
        return False
    return ann > float(getattr(config, "SECTOR_HIGH_VOL_CEILING_PCT", 0.18))


def _expansion_limits(data: pd.DataFrame | None) -> tuple[int, int, int]:
    """Per-sector expansion, max sectors, max total — tighter when market vol is high."""
    per_sector = max(1, int(config.SECTOR_EXPANSION_SIZE))
    max_sectors = _max_active_sectors(data)
    max_total = max(int(config.BASE_UNIVERSE_SIZE), int(config.SECTOR_MAX_TOTAL_TICKERS))
    if _high_vol_market(data):
        per_sector = min(per_sector, int(getattr(config, "SECTOR_HIGH_VOL_EXPANSION_CAP", 10)))
        max_sectors = min(max_sectors, int(getattr(config, "SECTOR_HIGH_VOL_MAX_ACTIVE_SECTORS", 1)))
    return per_sector, max_sectors, max_total


def _expanded_base_universe(col_list, data: pd.DataFrame) -> list[str]:
    """Screener-led base, topped up with liquid NYSE names present in price data."""
    primary = config.nyse_momentum_universe(col_list)
    col_set = set(col_list)
    top_up = [
        s
        for s in LIQUID_NYSE_FALLBACK
        if s in col_set and s not in primary and config._nyse_eligible_symbol(s)
    ]
    ranked_top_up = _rank_by_momentum(top_up, data)
    seen = set(primary)
    ordered = list(primary)
    for sym in ranked_top_up:
        if sym not in seen:
            ordered.append(sym)
            seen.add(sym)
    if len(ordered) < int(config.BASE_UNIVERSE_SIZE):
        wide = [s for s in _eligible_momentum_universe(col_list) if s not in seen]
        for sym in _rank_by_momentum(wide, data):
            if sym not in seen:
                ordered.append(sym)
                seen.add(sym)
    return ordered


def get_expanded_universe(data_columns, data: pd.DataFrame | None = None) -> list[str]:
    """Base NYSE momentum pool capped at BASE_UNIVERSE_SIZE; expand top strong sectors."""
    if not config.effective_dynamic_sector_screener():
        return config.nyse_momentum_universe(data_columns)

    col_list = list(data_columns)
    if data is None or getattr(data, "empty", True):
        base_cap = max(1, int(config.BASE_UNIVERSE_SIZE))
        base_full = config.nyse_momentum_universe(col_list)
        return sorted(base_full[:base_cap])

    base_full = _expanded_base_universe(col_list, data)
    base_cap = max(1, int(config.BASE_UNIVERSE_SIZE))
    per_sector, max_active_sectors, max_total = _expansion_limits(data)

    base = base_full[:base_cap]
    pool: set[str] = set(base)

    active: list[dict[str, Any]] = []
    added_tickers: list[str] = []
    expanded_sector_labels: list[str] = []
    fallback_count = 0

    if data is not None and not getattr(data, "empty", True):
        active = get_active_sectors(data)[:max_active_sectors]
        for sector in active:
            if len(pool) >= max_total:
                break
            candidates = _expansion_candidates(sector["sector_tags"], col_list, data)
            sector_added = 0
            for sym in candidates:
                if sym in pool:
                    continue
                if len(pool) >= max_total:
                    break
                pool.add(sym)
                added_tickers.append(sym)
                sector_added += 1
                if sector_added >= per_sector:
                    break
            if sector_added:
                expanded_sector_labels.append(sector["label"])

        if not added_tickers:
            fallback_count = _add_momentum_fallback(
                pool,
                added_tickers,
                data_columns=col_list,
                data=data,
                max_total=max_total,
                count=min(
                    int(getattr(config, "SECTOR_FALLBACK_MOMENTUM_COUNT", 12)),
                    6 if _high_vol_market(data) else int(getattr(config, "SECTOR_FALLBACK_MOMENTUM_COUNT", 12)),
                ),
            )

    result = sorted(pool)
    ref_date = _reference_date(data)
    _maybe_log_daily(
        ref_date=ref_date,
        active=active,
        base_count=len(base),
        total_count=len(result),
        expansion_count=len(added_tickers),
        expanded_sectors=expanded_sector_labels,
        added_tickers=added_tickers,
        fallback_count=fallback_count,
    )
    return result


def format_sector_screener_banner() -> str | None:
    """Startup line: Sector Screener ON/OFF and last active sectors."""
    enabled = config.effective_dynamic_sector_screener() or (
        config.DYNAMIC_SECTOR_SCREENER_ENABLED
        and config.PAPER_TRADING
        and config.PAPER_AGGRESSIVE_ENABLED
    )
    if not enabled:
        return ">>> Sector Screener: OFF"

    snap = load_sector_screener_snapshot()
    sectors = snap.get("active_sectors") or []
    extra = int(snap.get("expansion_count") or 0)
    fallback = int(snap.get("fallback_momentum_count") or 0)
    if not sectors and fallback:
        return f">>> Sector Screener: ON | Momentum fallback (+{fallback} tickers, total pool)"
    if not sectors:
        return ">>> Sector Screener: ON | Active sectors: none (base universe only)"
    names = " + ".join(str(s) for s in sectors)
    return f">>> Sector Screener: ON | Active sectors: {names} (+{extra} tickers)"
