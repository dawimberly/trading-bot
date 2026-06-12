"""Analyze thinking engine regime detection and tilt accuracy."""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from modules.data_loader import load_close_matrix
from modules.market_context import get_market_regime, get_sentiment, get_volatility
from modules.thinking_engine import (
    _caps_to_tilt,
    _infer_asymmetry,
    _infer_narrative,
    apply_thinking_tilt_to_caps,
    build_backtest_thinking_result,
    build_heuristic_reasoning_result,
    build_market_summary,
    derive_heuristic_tilt,
)

BASELINE_TILT = derive_heuristic_tilt({"spy_trend": "above MA200", "vix": 18, "oil_change": 0, "gold_change": 0})
def _energy_col(data: pd.DataFrame) -> str | None:
    for col in ("XLE", "USO", "XOM"):
        if col in data.columns:
            return col
    return None


def _asset_map(data: pd.DataFrame) -> dict[str, str | None]:
    energy = _energy_col(data)
    return {
        "vti": "VTI" if "VTI" in data.columns else None,
        "spy": "SPY" if "SPY" in data.columns else None,
        "energy": energy,
        "gold": "GLD" if "GLD" in data.columns else None,
        "cash": None,
    }
HORIZONS = (5, 10, 20)


def _load_daily() -> pd.DataFrame:
    data = load_close_matrix(interval="1d")
    data.index = pd.to_datetime(data.index)
    return data.sort_index()


def _forward_return(
    data: pd.DataFrame,
    col: str | None,
    as_of: pd.Timestamp,
    days: int,
    *,
    min_days: int = 2,
) -> float | None:
    if not col or col not in data.columns:
        return None
    try:
        idx = data.index.get_indexer([as_of], method="pad")[0]
        if idx < 0:
            return None
        avail = len(data) - 1 - idx
        use_days = min(days, avail)
        if use_days < min_days:
            return None
        start = float(data[col].iloc[idx])
        end = float(data[col].iloc[idx + use_days])
        if start <= 0:
            return None
        return (end / start - 1.0) * 100.0
    except (TypeError, ValueError, IndexError):
        return None


def _portfolio_return(tilt: dict[str, float], fwd: dict[str, float | None]) -> float | None:
    total_w = 0.0
    ret = 0.0
    for key, w in tilt.items():
        if w <= 0:
            continue
        if key == "cash":
            total_w += w
            continue
        r = fwd.get(key)
        if r is None:
            return None
        ret += w * r
        total_w += w
    if total_w <= 0:
        return None
    return ret / total_w


def _dominant_tilt(tilt: dict[str, float]) -> tuple[str, float]:
    ranked = sorted(tilt.items(), key=lambda kv: kv[1], reverse=True)
    return ranked[0] if ranked else ("vti", 0.0)


def _tilt_vs_baseline(tilt: dict[str, float], baseline: dict[str, float]) -> dict[str, float]:
    keys = set(tilt) | set(baseline)
    return {k: float(tilt.get(k, 0)) - float(baseline.get(k, 0)) for k in keys}


def _direction_label(delta: dict[str, float]) -> str:
    """Primary directional call vs baseline heuristic."""
    eq_delta = delta.get("vti", 0) + delta.get("spy", 0) - delta.get("cash", 0)
    if eq_delta > 0.05:
        return "risk-on"
    if eq_delta < -0.05:
        return "defensive"
    top = max(delta.items(), key=lambda kv: abs(kv[1]))
    if top[0] == "energy" and top[1] > 0.03:
        return "energy-up"
    if top[0] == "gold" and top[1] > 0.03:
        return "gold-up"
    return "neutral"


def _direction_correct(
    direction: str,
    fwd: dict[str, float | None],
    *,
    horizon: int,
) -> bool | None:
    spy = fwd.get("spy")
    vti = fwd.get("vti")
    gold = fwd.get("gold")
    energy = fwd.get("energy")
    eq = None
    if spy is not None and vti is not None:
        eq = 0.5 * spy + 0.5 * vti
    elif spy is not None:
        eq = spy
    elif vti is not None:
        eq = vti

    if direction == "risk-on":
        if eq is None:
            return None
        return eq > 0
    if direction == "defensive":
        if eq is None:
            return None
        return eq < 0
    if direction == "energy-up":
        if energy is None:
            return None
        return energy > 0
    if direction == "gold-up":
        if gold is None:
            return None
        return gold > 0
    return None


def _narrative_correct(narrative: str, fwd: dict[str, float | None], *, horizon: int) -> bool | None:
    low = narrative.lower()
    spy = fwd.get("spy")
    vti = fwd.get("vti")
    eq = None
    if spy is not None and vti is not None:
        eq = 0.5 * spy + 0.5 * vti
    elif spy is not None:
        eq = spy

    if any(x in low for x in ("risk-off", "safe-haven", "pullback", "correction", "overbought", "stress")):
        if eq is None:
            return None
        return eq < 0
    if any(x in low for x in ("bullish", "momentum", "above", "strength", "outperformance")):
        if eq is None:
            return None
        return eq > 0
    if "range-bound" in low or "range bound" in low:
        if eq is None:
            return None
        return abs(eq) < 1.5 if horizon <= 10 else abs(eq) < 2.5
    return None


def analyze_samples(samples_path: Path, data: pd.DataFrame) -> list[dict]:
    asset_map = _asset_map(data)
    samples = json.loads(samples_path.read_text(encoding="utf-8"))
    rows = []
    for s in samples:
        as_of = pd.Timestamp(s["as_of"])
        tilt = s.get("suggested_tilt") or {}
        baseline = derive_heuristic_tilt(s.get("market_summary") or {})
        delta = _tilt_vs_baseline(tilt, baseline)
        direction = _direction_label(delta)

        fwd_by_h: dict[int, dict[str, float | None]] = {}
        for h in HORIZONS:
            fwd_by_h[h] = {
                k: _forward_return(data, col, as_of, h) if col else 0.0
                for k, col in asset_map.items()
            }

        row = {
            "as_of": s["as_of"],
            "regime": s.get("regime"),
            "source": s.get("source", "llm"),
            "confidence": s.get("confidence"),
            "narrative": (s.get("narrative") or "")[:80],
            "top_tilt": _dominant_tilt(tilt)[0],
            "direction": direction,
            "baseline_direction": _direction_label(_tilt_vs_baseline(baseline, BASELINE_TILT)),
        }
        for h in HORIZONS:
            fwd = fwd_by_h[h]
            row[f"spy_{h}d"] = fwd.get("spy")
            row[f"vti_{h}d"] = fwd.get("vti")
            row[f"tilt_correct_{h}d"] = _direction_correct(direction, fwd, horizon=h)
            row[f"narrative_correct_{h}d"] = _narrative_correct(s.get("narrative") or "", fwd, horizon=h)
            pr = _portfolio_return(tilt, fwd)
            br = _portfolio_return(baseline, fwd)
            row[f"tilt_ret_{h}d"] = pr
            row[f"baseline_ret_{h}d"] = br
            row[f"tilt_beats_baseline_{h}d"] = (
                pr > br if pr is not None and br is not None else None
            )
        rows.append(row)
    return rows


def analyze_heuristic_backtest(data: pd.DataFrame, min_history: int = 60) -> dict:
    """Walk daily bars; score heuristic thinking proxy at regime changes."""
    asset_map = _asset_map(data)
    records = []
    prev_regime = None
    for i in range(min_history, len(data) - 2):
        window = data.iloc[: i + 1]
        sent = get_sentiment(window)
        vol = get_volatility(window)
        regime = get_market_regime(sent, vol)
        if regime == prev_regime:
            continue
        prev_regime = regime
        as_of = window.index[-1]
        thinking = build_backtest_thinking_result(window, regime, vol)
        tilt = thinking["suggested_tilt"]
        direction = _direction_label(_tilt_vs_baseline(tilt, BASELINE_TILT))
        base_caps = dict(config.fund_allocation_pct())
        merged, deltas, _ = apply_thinking_tilt_to_caps(
            base_caps,
            tilt,
            confidence=thinking["confidence"],
            market_summary=thinking["market_summary"],
        )
        cash_delta = deltas.get("cash_buffer", 0.0)
        eq_delta = deltas.get("vti_core", 0.0) + deltas.get("spy", 0.0)
        cap_direction = (
            "defensive" if cash_delta > 0.01 and eq_delta <= 0
            else "risk-on" if eq_delta > 0.01 and cash_delta <= 0
            else direction
        )
        fwd = {
            k: _forward_return(data, col, as_of, 10) if col else 0.0
            for k, col in asset_map.items()
        }
        records.append(
            {
                "date": str(as_of.date()),
                "regime": regime,
                "narrative": thinking["narrative"],
                "asymmetry": thinking["asymmetry"],
                "confidence": thinking["confidence"],
                "direction": cap_direction,
                "correct_10d": _direction_correct(cap_direction, fwd, horizon=10),
                "narrative_correct_10d": _narrative_correct(thinking["narrative"], fwd, horizon=10),
                "spy_10d": fwd.get("spy"),
                "cash_delta": cash_delta,
            }
        )

    def _pct(key: str) -> float | None:
        vals = [r[key] for r in records if r.get(key) is not None]
        if not vals:
            return None
        return 100.0 * sum(1 for v in vals if v) / len(vals)

    return {
        "regime_changes": len(records),
        "direction_accuracy_10d_pct": _pct("correct_10d"),
        "narrative_accuracy_10d_pct": _pct("narrative_correct_10d"),
        "records": records[-8:],
    }


def compare_llm_heuristic_same_window(data: pd.DataFrame, samples: list[dict]) -> list[dict]:
    asset_map = _asset_map(data)
    rows = []
    for s in samples:
        as_of = pd.Timestamp(s["as_of"])
        idx = data.index.get_indexer([as_of], method="pad")[0]
        if idx < 25:
            continue
        window = data.iloc[: idx + 1]
        sent = get_sentiment(window)
        vol = get_volatility(window)
        regime = get_market_regime(sent, vol)
        summary = build_market_summary(window, regime, vol)
        heur = build_heuristic_reasoning_result(summary)
        llm_tilt = s.get("suggested_tilt") or {}
        heur_tilt = heur["suggested_tilt"]
        fwd10 = {
            k: _forward_return(data, col, as_of, 10, min_days=2) if col else 0.0
            for k, col in asset_map.items()
        }
        llm_dir = _direction_label(_tilt_vs_baseline(llm_tilt, heur_tilt))
        heur_dir = _direction_label(_tilt_vs_baseline(heur_tilt, BASELINE_TILT))
        rows.append(
            {
                "as_of": s["as_of"],
                "llm_direction": llm_dir,
                "heur_direction": heur_dir,
                "llm_correct_10d": _direction_correct(llm_dir, fwd10, horizon=10),
                "heur_correct_10d": _direction_correct(heur_dir, fwd10, horizon=10),
                "llm_narrative": (s.get("narrative") or "")[:60],
                "heur_narrative": heur["narrative"],
                "llm_asymmetry": (s.get("asymmetry") or "")[:60],
                "heur_asymmetry": heur["asymmetry"],
            }
        )
    return rows


def confidence_calibration(rows: list[dict], conf_key: str = "confidence") -> dict:
    buckets: dict[str, list[bool]] = {"high": [], "mid": [], "low": []}
    for r in rows:
        c = r.get(conf_key)
        if c is None:
            continue
        ok = r.get("correct_10d")
        if ok is None:
            continue
        if c >= 0.75:
            buckets["high"].append(ok)
        elif c >= 0.65:
            buckets["mid"].append(ok)
        else:
            buckets["low"].append(ok)
    return {
        k: (100.0 * sum(v) / len(v) if v else None, len(v))
        for k, v in buckets.items()
    }


def main() -> int:
    data = _load_daily()
    print(f"Daily data: {data.index[0].date()} -> {data.index[-1].date()} ({len(data)} bars)")
    print(f"Columns sample: {', '.join(list(data.columns)[:8])}...")

    samples_path = ROOT / "thinking_engine_test_samples.json"
    sample_rows = analyze_samples(samples_path, data)
    print("\n=== LLM TEST SAMPLES (3 live Ollama runs) ===")
    for r in sample_rows:
        print(json.dumps(r, default=str))

    last_path = ROOT / config.THINKING_ENGINE_OUTPUT_FILE
    print(f"\nthinking_engine_last.json exists: {last_path.is_file()}")

    bt = analyze_heuristic_backtest(data.iloc[-400:] if len(data) > 400 else data)
    print("\n=== HEURISTIC BACKTEST PROXY (regime-change dates, last ~400 bars) ===")
    da = bt["direction_accuracy_10d_pct"]
    na = bt["narrative_accuracy_10d_pct"]
    print(
        f"Regime changes: {bt['regime_changes']} | "
        f"Direction accuracy 10d: {da:.1f}% | "
        f"Narrative accuracy 10d: {na:.1f}%"
        if da is not None and na is not None
        else f"Regime changes: {bt['regime_changes']} | insufficient scored outcomes"
    )

    samples_raw = json.loads(samples_path.read_text(encoding="utf-8"))
    cmp_rows = compare_llm_heuristic_same_window(data, samples_raw)
    print("\n=== LLM vs HEURISTIC (same windows) ===")
    for r in cmp_rows:
        print(json.dumps(r, default=str))

    # Full 365d heuristic if enough data
    slice365 = data.iloc[-(365 + 60):] if len(data) > 425 else data
    bt365 = analyze_heuristic_backtest(slice365)
    print("\n=== HEURISTIC 365d WINDOW ===")
    da365 = bt365["direction_accuracy_10d_pct"]
    na365 = bt365["narrative_accuracy_10d_pct"]
    print(
        f"Regime changes: {bt365['regime_changes']} | "
        f"Direction accuracy 10d: {da365:.1f}% | "
        f"Narrative accuracy 10d: {na365:.1f}%"
        if da365 is not None and na365 is not None
        else f"Regime changes: {bt365['regime_changes']} | insufficient scored outcomes"
    )

    out = {
        "sample_rows": sample_rows,
        "heuristic_400d": {k: v for k, v in bt.items() if k != "records"},
        "heuristic_365d": {k: v for k, v in bt365.items() if k != "records"},
        "llm_vs_heuristic": cmp_rows,
    }
    out_path = ROOT / "scripts" / "thinking_engine_analysis_output.json"
    out_path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
