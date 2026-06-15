"""Demo scheduled news input for Thinking Engine (paper only).

Run:
  python scripts/test_thinking_news.py
  python scripts/test_thinking_news.py --sample-trump
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from modules.data_loader import load_close_matrix
from modules.market_context import get_market_regime, get_price_sentiment, get_volatility
from modules.thinking_engine import (
    apply_thinking_tilt_to_caps,
    build_heuristic_reasoning_result,
    build_market_summary,
    format_recommended_tilt,
    get_market_reasoning,
    ollama_available,
)
from modules.thinking_news import analyze_news_headlines, build_news_digest, format_news_digest

SAMPLE_HEADLINES = [
    "Trump to flood the market with strategic oil reserves amid Middle East tensions",
    "Analysts warn tariff headlines may whipsaw small-cap beta before Fed speak",
    "Oil jumps 4% on Hormuz shipping risk; gold firm",
]


def _latest_window(data):
    window = data.iloc[:]
    sentiment = get_price_sentiment(window)
    vol = get_volatility(window)
    regime = get_market_regime(sentiment, vol)
    return window, regime, vol


def _print_news_analysis(digest: dict) -> None:
    print(f"news_impact_score: {float(digest.get('news_impact_score') or 0.0):.2f}")
    print(f"Themes:         {digest.get('theme_summary', 'n/a')}")
    print(f"AI/tech lens:   {digest.get('ai_tech_context', 'n/a')}")


def _print_tilt_comparison(label: str, base_caps: dict, result: dict) -> None:
    merged, deltas, log_line = apply_thinking_tilt_to_caps(
        base_caps,
        result.get("suggested_tilt") or {},
        confidence=float(result.get("confidence") or 0.7),
        market_summary=result.get("market_summary"),
    )
    material = {k: round(v, 4) for k, v in deltas.items() if abs(v) > 0.001}
    print(f"\n--- {label} ---")
    print(f"Narrative:       {result.get('narrative', 'n/a')}")
    print(f"Asymmetry:       {result.get('asymmetry', 'n/a')}")
    print(f"Tilt rationale:  {result.get('tilt_rationale', 'n/a')}")
    print(f"Tilt:            {format_recommended_tilt(result.get('suggested_tilt'))}")
    print(f"Confidence:      {float(result.get('confidence', 0)):.0%}")
    impact = result.get("news_impact_score")
    if impact is not None:
        print(f"News impact:     {float(impact):.2f}")
    if material:
        print(f"Cap deltas:      {material}")
    else:
        print(f"Apply log:       {log_line or 'no material change'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Test thinking news integration")
    parser.add_argument(
        "--sample-trump",
        action="store_true",
        help="Use sample Trump/oil headlines instead of live RSS",
    )
    parser.add_argument(
        "--live-rss",
        action="store_true",
        help="Fetch live Google News RSS instead of sample headlines",
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        help="Also call Ollama when reachable (may take 1-2 min)",
    )
    args = parser.parse_args()

    config.set_paper_aggressive_context(True)
    config.set_backtest_paper_sleeves_context(True)
    config.PAPER_THINKING_ENGINE_ENABLED = True

    data = load_close_matrix(interval="1d")
    if data is None or data.empty:
        print("No daily data — run fetch_data.py first.")
        return 1

    window, regime, vol = _latest_window(data)
    base_caps = dict(config.fund_allocation_pct())

    if args.live_rss:
        from modules.thinking_news import get_news_digest_for_thinking

        digest = get_news_digest_for_thinking(slot="premarket")
    else:
        digest = build_news_digest(SAMPLE_HEADLINES, slot="premarket")

    print("=== THINKING NEWS TEST (paper only) ===", flush=True)
    print(format_news_digest(digest))
    _print_news_analysis(digest)
    print(f"Regime: {regime} | Vol: {vol}")

    baseline_summary = build_market_summary(window, regime, vol)
    baseline = build_heuristic_reasoning_result(baseline_summary, reason="baseline-no-news")
    _print_tilt_comparison("Baseline (no news digest)", base_caps, baseline)

    news_summary_obj = build_market_summary(
        window,
        regime,
        vol,
        news_headlines=digest.get("headlines") or SAMPLE_HEADLINES,
        news_slot="premarket",
    )
    with_news = build_heuristic_reasoning_result(news_summary_obj, reason="news-heuristic")
    _print_tilt_comparison("With news digest (heuristic)", base_caps, with_news)

    if args.llm and ollama_available():
        print("\nOllama reachable — running LLM with news digest...")
        try:
            llm = get_market_reasoning(news_summary_obj)
            _print_tilt_comparison("With news digest (LLM)", base_caps, llm)
            print("\nLLM reasoning preview:")
            print((llm.get("reasoning") or "")[:600])
        except Exception as exc:
            print(f"LLM run failed: {exc}")
    else:
        print("\nSkip LLM (pass --llm to call Ollama when running).")

    print("\nScheduled slots: 8:00 AM ET (premarket) | 6:00 PM ET (postmarket) | max 2/day")
    print("Audit log: logs/thinking_engine.log")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
