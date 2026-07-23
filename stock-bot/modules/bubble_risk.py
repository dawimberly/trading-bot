"""Bubble Risk Score (0–100) with Buffett Indicator (market cap / GDP)."""

from __future__ import annotations

import io
import json
import logging
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[1]
_CACHE_PATH = _ROOT / "data" / "cache" / "buffett_indicator.json"
_CACHE_TTL_HOURS = 24

_FRED_GDP = "GDP"
_FRED_MKT_CAP = "NCBEILQ027S"

_SIGNAL_BANDS: tuple[tuple[float, str], ...] = (
    (200.0, "Strongly Overvalued"),
    (180.0, "Overvalued"),
    (150.0, "Elevated"),
    (120.0, "Fairly Valued"),
    (0.0, "Undervalued"),
)

_ratio_series_cache: Any = None
_ratio_series_loaded_at: float | None = None


def buffett_indicator_signal(ratio_pct: float) -> str:
    for threshold, label in _SIGNAL_BANDS:
        if ratio_pct >= threshold:
            return label
    return "Undervalued"


def _fetch_fred_csv_series(series_id: str):
    import pandas as pd

    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    try:
        with urllib.request.urlopen(url, timeout=45) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        df = pd.read_csv(io.StringIO(raw))
        if df.empty or len(df.columns) < 2:
            return pd.Series(dtype=float)
        date_col = df.columns[0]
        val_col = df.columns[-1]
        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
        df[val_col] = pd.to_numeric(df[val_col].replace(".", None), errors="coerce")
        series = df.dropna(subset=[date_col, val_col]).set_index(date_col)[val_col].sort_index()
        return series.astype(float)
    except Exception as exc:
        logger.warning("FRED fetch failed for %s: %s", series_id, exc)
        return pd.Series(dtype=float)


def _build_buffett_ratio_series():
    import pandas as pd

    gdp = _fetch_fred_csv_series(_FRED_GDP)
    mcap = _fetch_fred_csv_series(_FRED_MKT_CAP)
    if gdp.empty or mcap.empty:
        return pd.Series(dtype=float)
    frame = pd.DataFrame({"gdp_bil": gdp, "mcap_mil": mcap}).sort_index().ffill()
    frame = frame.dropna()
    if frame.empty:
        return pd.Series(dtype=float)
    ratio = frame["mcap_mil"] / frame["gdp_bil"] / 10.0
    ratio.name = "buffett_pct"
    return ratio.astype(float)


def _load_cache_file() -> dict[str, Any]:
    if not _CACHE_PATH.exists():
        return {}
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache_file(payload: dict[str, Any]) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.debug("Buffett cache write failed: %s", exc)


def _ratio_series(*, force_refresh: bool = False):
    import time

    global _ratio_series_cache, _ratio_series_loaded_at
    now = time.time()
    ttl = _CACHE_TTL_HOURS * 3600
    if (
        not force_refresh
        and _ratio_series_cache is not None
        and _ratio_series_loaded_at is not None
        and (now - _ratio_series_loaded_at) < ttl
    ):
        return _ratio_series_cache

    cached = _load_cache_file()
    cached_at = cached.get("fetched_at")
    if not force_refresh and cached_at and cached.get("series"):
        try:
            import pandas as pd

            age_h = (datetime.utcnow() - datetime.fromisoformat(cached_at)).total_seconds() / 3600
            if age_h < _CACHE_TTL_HOURS:
                idx = pd.to_datetime(cached["dates"])
                series = pd.Series(cached["series"], index=idx, dtype=float)
                _ratio_series_cache = series
                _ratio_series_loaded_at = now
                return series
        except (TypeError, ValueError, KeyError):
            pass

    series = _build_buffett_ratio_series()
    if not series.empty:
        payload = {
            "fetched_at": datetime.utcnow().isoformat(),
            "dates": [d.isoformat() for d in series.index],
            "series": [round(float(v), 3) for v in series.values],
            "latest_ratio_pct": round(float(series.iloc[-1]), 2),
            "latest_signal": buffett_indicator_signal(float(series.iloc[-1])),
        }
        _save_cache_file(payload)
    _ratio_series_cache = series
    _ratio_series_loaded_at = now
    return series


def _coerce_as_of(as_of: date | datetime | None) -> datetime | None:
    if as_of is None:
        return None
    if isinstance(as_of, datetime):
        return as_of
    return datetime.combine(as_of, datetime.min.time())


def _ratio_for_date(as_of: date | datetime | None = None) -> float | None:
    series = _ratio_series()
    if series.empty:
        cached = _load_cache_file()
        val = cached.get("latest_ratio_pct")
        return float(val) if val is not None else None
    ts = _coerce_as_of(as_of) or datetime.utcnow()
    import pandas as pd

    idx = series.index
    if idx.tz is not None:
        ts = pd.Timestamp(ts).tz_localize(None)
    pos = idx.searchsorted(pd.Timestamp(ts), side="right") - 1
    if pos < 0:
        return float(series.iloc[0])
    return float(series.iloc[pos])


def get_buffett_indicator(*, as_of: date | datetime | None = None) -> dict[str, Any]:
    """Latest Buffett Indicator (% of GDP) and valuation signal."""
    ratio = _ratio_for_date(as_of)
    if ratio is None:
        return {
            "enabled": config.effective_buffett_indicator_enabled(),
            "ratio_pct": None,
            "signal": "Unavailable",
            "as_of": None,
            "source": "FRED GDP + NCBEILQ027S",
        }
    return {
        "enabled": config.effective_buffett_indicator_enabled(),
        "ratio_pct": round(ratio, 2),
        "signal": buffett_indicator_signal(ratio),
        "as_of": (_coerce_as_of(as_of) or datetime.utcnow()).date().isoformat(),
        "source": "FRED GDP + NCBEILQ027S",
        "overvalued_threshold": float(config.BUFFETT_OVERVALUED_THRESHOLD),
    }


def buffett_score_points(ratio_pct: float | None) -> float:
    """0–40 contribution from Buffett Indicator."""
    if not config.effective_buffett_indicator_enabled() or ratio_pct is None:
        return 0.0
    threshold = float(config.BUFFETT_OVERVALUED_THRESHOLD)
    if ratio_pct >= threshold + 40:
        return 40.0
    if ratio_pct >= threshold:
        return 25.0 + min(15.0, (ratio_pct - threshold) / 40.0 * 15.0)
    if ratio_pct >= threshold - 20:
        return 10.0 + (ratio_pct - (threshold - 20)) / 20.0 * 15.0
    if ratio_pct >= 150:
        return 5.0 + (ratio_pct - 150) / 30.0 * 5.0
    return max(0.0, ratio_pct / 150.0 * 5.0)


def technical_bubble_fraction(
    data,
    regime: str,
    *,
    volatility: str | None = None,
    vol_score: float | None = None,
) -> float:
    """Legacy technical-only bubble score (0–1)."""
    from modules.opportunistic_short_sleeve import (
        _resolve_vix_level,
        _vix_change_pct,
        _momentum_score,
        momentum_exhaustion,
        _spy_market_down_signal,
    )

    spy = config.SPY_BOT_SYMBOL
    score = 0.0
    reg = str(regime or "")
    if "RHYME_B" in reg:
        score += 0.35
    elif "RHYME_E" in reg:
        score += 0.20
    if "RHYME_A" in reg:
        score += 0.15

    ma_window = config.effective_spy_ma_window()
    bearish, depth = _spy_market_down_signal(data, spy, ma_window)
    if bearish:
        score += min(0.25, depth * 5.0)

    exhausted, exh_score = momentum_exhaustion(data, spy)
    if exhausted:
        score += min(0.20, 0.10 + exh_score * 2.0)

    vix = _resolve_vix_level(data, volatility=volatility, vol_score=vol_score)
    chg = _vix_change_pct(data)
    if vix is not None and vix >= config.SHORT_VIX_MIN_LEVEL:
        score += 0.15
    if chg is not None and chg >= config.VOL_VIX_SPIKE_PCT:
        score += 0.15

    mom20 = _momentum_score(data, spy, 20)
    mom5 = _momentum_score(data, spy, 5)
    if mom20 is not None and mom5 is not None and mom20 > 0.03 and mom5 < -0.01:
        score += 0.10

    return round(min(1.0, max(0.0, score)), 3)


def _as_of_from_data(data) -> datetime | None:
    try:
        if hasattr(data, "index") and len(data.index):
            return pd_timestamp_to_datetime(data.index[-1])
    except Exception as exc:
        logger.debug("bubble as-of timestamp parse failed: %s", exc)
    return None


def pd_timestamp_to_datetime(ts) -> datetime:
    import pandas as pd

    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_convert(None)
    return t.to_pydatetime()


def compute_bubble_risk(
    data,
    regime: str,
    *,
    volatility: str | None = None,
    vol_score: float | None = None,
    as_of: date | datetime | None = None,
) -> dict[str, Any]:
    """Unified bubble assessment: score_100 (0–100), score_normalized (0–1), Buffett context."""
    as_of_dt = as_of or _as_of_from_data(data)
    tech = technical_bubble_fraction(data, regime, volatility=volatility, vol_score=vol_score)
    tech_points = tech * 60.0
    buffett = get_buffett_indicator(as_of=as_of_dt)
    buff_pts = buffett_score_points(buffett.get("ratio_pct"))
    score_100 = round(min(100.0, tech_points + buff_pts), 1)
    return {
        "score_100": score_100,
        "score_normalized": round(score_100 / 100.0, 3),
        "technical_fraction": tech,
        "technical_points": round(tech_points, 1),
        "buffett_points": round(buff_pts, 1),
        "buffett": buffett,
    }


def bubble_risk_score(
    data,
    regime: str,
    *,
    volatility: str | None = None,
    vol_score: float | None = None,
) -> float:
    """Backward-compatible 0–1 score for protective short triggers."""
    return compute_bubble_risk(
        data, regime, volatility=volatility, vol_score=vol_score
    )["score_normalized"]


def bubble_risk_score_100(
    data,
    regime: str,
    *,
    volatility: str | None = None,
    vol_score: float | None = None,
) -> float:
    return compute_bubble_risk(
        data, regime, volatility=volatility, vol_score=vol_score
    )["score_100"]


def format_buffett_line(buffett: dict[str, Any] | None = None) -> str | None:
    if not config.effective_buffett_indicator_enabled():
        return None
    info = buffett or get_buffett_indicator()
    ratio = info.get("ratio_pct")
    if ratio is None:
        return "Buffett Indicator: unavailable"
    return (
        f"Buffett Indicator: {ratio:.1f}% of GDP ({info.get('signal', 'n/a')}) "
        f"[>{config.BUFFETT_OVERVALUED_THRESHOLD:.0f}% strongly overvalued]"
    )


def format_bubble_risk_summary(result: dict[str, Any] | None = None) -> str | None:
    """One-line bubble risk for backtest report or status."""
    if result is None:
        live = compute_bubble_risk_from_live_context()
        if not live:
            return None
        result = live
    score = result.get("score_100")
    buff = result.get("buffett") or {}
    ratio = buff.get("ratio_pct")
    if score is None:
        return None
    parts = [f"Bubble Risk: {score:.0f}/100"]
    if ratio is not None:
        parts.append(f"Buffett {ratio:.1f}% ({buff.get('signal', 'n/a')})")
    return " | ".join(parts)


def compute_bubble_risk_from_live_context(
    *,
    regime: str = "",
    hb: dict | None = None,
) -> dict[str, Any] | None:
    """Best-effort live bubble score for dashboard / weekly reports."""
    hb = hb or {}
    regime = regime or str(hb.get("regime") or "")
    try:
        import pandas as pd

        from modules.data_loader import load_close_matrix

        days = 400
        data = load_close_matrix(days=days, interval="daily")
        if data is None or data.empty:
            return None
        return compute_bubble_risk(data, regime)
    except Exception as exc:
        logger.debug("Live bubble risk skipped: %s", exc)
        buffett = get_buffett_indicator()
        if buffett.get("ratio_pct") is None:
            return None
        pts = buffett_score_points(buffett["ratio_pct"])
        return {
            "score_100": round(min(100.0, pts + 15.0), 1),
            "score_normalized": round(min(1.0, (pts + 15.0) / 100.0), 3),
            "technical_fraction": 0.0,
            "technical_points": 0.0,
            "buffett_points": round(pts, 1),
            "buffett": buffett,
        }
