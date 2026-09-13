"""Smoke test for system-wide thinking engine updates."""
from __future__ import annotations

import config
import modules.thinking_engine as te
from modules.bot_health import calculate_health_score
from modules.thinking_engine import (
    _parse_structured_reasoning,
    _pm_system_prompt,
    _trading_system_prompt,
    thinking_dashboard_snapshot,
    thinking_health_snapshot,
)


def main() -> None:
    te._PM_SYSTEM_PROMPT = None
    prompt = _pm_system_prompt()
    assert "REGIME_SIGNAL" in prompt
    assert "TILT_SIGNAL" in prompt
    assert "RISK_SIGNAL" in prompt
    sys_p = _trading_system_prompt("risk_signals")
    assert "risk" in sys_p.lower()

    sample = "\n".join(
        [
            "NARRATIVE: Risk-on mid-cycle AI leadership vs VTI.",
            "ASYMMETRY: Crowd underweights energy rotation.",
            "SECTOR_VIEW: Semis lead; vol calm; stat-arb supportive.",
            "REGIME_SIGNAL: risk-on | strength 0.78 | SPY above MA200",
            "TILT_SIGNAL: spy / cash | conviction 0.72",
            "RISK_SIGNAL: low | VIX stable sub-16",
            'RECOMMENDED_TILT: {"vti": 0.55, "spy": 0.20, "cash": 0.15, "energy": 0.10}',
            "TILT_RATIONALE: Raise SPY on AI mid-cycle; keep VTI anchor.",
            "CONFIDENCE: 0.74",
            "RISKS: Vol spike; crowded semis",
            "OPPORTUNITIES: Selective SPY tilt",
        ]
    )
    parsed = _parse_structured_reasoning(sample)
    assert parsed.get("regime_signal")
    assert parsed.get("tilt_signal")
    assert parsed.get("risk_signal")
    print("parsed", parsed["regime_signal"], "|", parsed["tilt_signal"])

    # Thinking is opt-in for paper stability (Ollama stalls); force-enable for this smoke.
    config.THINKING_ENGINE_ENABLED = True
    config.PAPER_THINKING_ENGINE_ENABLED = True
    config.LIVE_THINKING_ENGINE_ENABLED = False
    assert config.THINKING_ENGINE_ENABLED is True
    assert config.PAPER_THINKING_ENGINE_ENABLED is True
    assert config.LIVE_THINKING_ENGINE_ENABLED is False
    print("effective", config.effective_thinking_engine_enabled())
    print("safety", config.get_thinking_safety_summary())

    dash = thinking_dashboard_snapshot()
    print("dash", dash.get("status"), dash.get("pill_text"))
    print("health_t", thinking_health_snapshot())

    h = calculate_health_score(
        regime="RHYME_C",
        thinking_enabled=True,
        thinking_ollama_ok=True,
        thinking_status="ON",
        thinking_fallback=False,
        thinking_confidence=0.8,
        thinking_validation_score=80,
        thinking_age_hours=2,
        atr_sizing_ok=True,
        news_pool_clean=True,
    )
    think_notes = [n for n in h["notes"] if "hink" in n or "llama" in n]
    print("score_on", h["score"], think_notes)

    h2 = calculate_health_score(
        regime="RHYME_C",
        thinking_enabled=True,
        thinking_ollama_ok=False,
        thinking_status="FALLBACK",
        thinking_fallback=True,
        thinking_age_hours=2,
    )
    print(
        "score_fb",
        h2["score"],
        [n for n in h2["notes"] if "hink" in n or "llama" in n],
    )

    import dashboard_app

    assert hasattr(dashboard_app, "_fetch_thinking_snapshot")
    dash_src = open("dashboard_app.py", encoding="utf-8").read()
    assert "_update_thinking_pill" in dash_src
    assert "_pill_thinking" in dash_src
    print("ok")


if __name__ == "__main__":
    main()
