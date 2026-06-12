"""Smoke-test thinking engine with real Ollama on recent market windows."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from modules.data_loader import load_close_matrix
from modules.market_context import get_market_regime, get_sentiment, get_volatility
from modules.thinking_engine import (
    build_market_summary,
    get_market_reasoning,
    ollama_available,
    ollama_installed_models,
    persist_thinking_last,
    thinking_model_chain,
)


def _pick_windows(data, max_examples: int = 3) -> list[tuple[int, str, str]]:
    """Pick recent bar offsets with distinct regimes when possible."""
    chosen: list[tuple[int, str, str]] = []
    seen_regimes: set[str] = set()
    min_bars = 25
    for offset in range(2, min(60, len(data) - min_bars)):
        window = data.iloc[: len(data) - offset]
        if len(window) < min_bars:
            continue
        sent = get_sentiment(window)
        vol = get_volatility(window)
        regime = get_market_regime(sent, vol)
        if regime in seen_regimes and len(chosen) >= max_examples:
            continue
        if regime not in seen_regimes or len(chosen) < max_examples:
            chosen.append((offset, regime, vol))
            seen_regimes.add(regime)
        if len(chosen) >= max_examples:
            break
    return chosen


def main() -> int:
    parser = argparse.ArgumentParser(description="Test thinking engine with Ollama")
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser.add_argument("--max-examples", type=int, default=2, help="Number of windows to test")
    parser.add_argument(
        "--fast-model",
        action="store_true",
        help="Skip primary model; use fast fallbacks only (llama3.2:3b / deepseek-r1:1.5b)",
    )
    args = parser.parse_args()

    if not ollama_available():
        print("Ollama not reachable — start Ollama and pull a model first.")
        return 1

    installed = sorted(ollama_installed_models())
    models = thinking_model_chain(fast_only=args.fast_model)
    print(f"Timeout: {config.OLLAMA_TIMEOUT_SEC}s | Cache: {config.THINKING_CACHE_HOURS}h")
    print(f"Model chain: {models}")
    if installed:
        print(f"Installed: {', '.join(installed[:6])}{'...' if len(installed) > 6 else ''}")

    data = load_close_matrix(interval="1d")
    if data is None or data.empty:
        print("No daily data available.")
        return 1

    windows = _pick_windows(data, max_examples=max(1, args.max_examples))
    if not windows:
        print("Could not build test windows from daily data.")
        return 1

    mode = "fast fallback" if args.fast_model else "primary+fallback"
    print(f"Testing thinking engine ({mode}) on {len(windows)} window(s)...")
    results = []
    for i, (offset, regime, vol) in enumerate(windows, 1):
        window = data.iloc[: len(data) - offset]
        as_of = str(window.index[-1].date())
        summary = build_market_summary(window, regime, vol)
        print(f"\n--- Example {i} ({as_of}, {regime}, {vol}) ---")
        print(json.dumps(summary, indent=2))
        print("Calling Ollama...")
        result = get_market_reasoning(summary, fast_model=args.fast_model)
        persist_thinking_last(result, regime=regime)
        results.append({"as_of": as_of, "regime": regime, **result})
        print(f"MODEL: {result.get('model')} | SOURCE: {result.get('source')} | QUALITY: {result.get('parse_quality')}")
        print(f"NARRATIVE: {result.get('narrative')}")
        if result.get("asymmetry"):
            print(f"ASYMMETRY: {result.get('asymmetry')}")
        if result.get("tilt_rationale"):
            print(f"TILT_RATIONALE: {result.get('tilt_rationale')}", flush=True)
        print(f"RISKS: {result.get('risks')}")
        print(f"OPPORTUNITIES: {result.get('opportunities')}")
        print(f"RECOMMENDED_TILT: {json.dumps(result.get('suggested_tilt'))}")
        print(f"CONFIDENCE: {result.get('confidence')}")
        print(f"REGIME_NARRATIVE: {result.get('regime_narrative')}")
        excerpt = (result.get("justification") or result.get("reasoning", ""))[:320]
        print(f"REASONING: {excerpt}")

    out_path = ROOT / "thinking_engine_test_samples.json"
    out_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nSaved samples to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
