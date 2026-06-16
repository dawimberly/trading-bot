"""Tests for optional Grok UFC analysis helpers."""

from __future__ import annotations

from src.grok_analysis import (
    apply_grok_kelly_adjustments,
    build_grok_prompt,
    clamp_kelly_factor,
    normalize_grok_result,
    _extract_json_blob,
)


def test_clamp_kelly_factor_bounds(monkeypatch):
    import config

    monkeypatch.setattr(config, "GROK_KELLY_ADJ_MIN", 0.70)
    monkeypatch.setattr(config, "GROK_KELLY_ADJ_MAX", 1.15)
    assert clamp_kelly_factor(1.0) == 1.0
    assert clamp_kelly_factor(2.0) == 1.15
    assert clamp_kelly_factor(0.1) == 0.70
    assert clamp_kelly_factor("bad") == 1.0


def test_extract_json_blob_from_fenced_block():
    raw = 'Here is the analysis:\n```json\n{"summary": "test", "picks": []}\n```'
    parsed = _extract_json_blob(raw)
    assert parsed["summary"] == "test"


def test_normalize_grok_result_picks():
    raw = {
        "summary": "Card leans grapplers",
        "picks": [
            {
                "id": "fight-1",
                "pick_type": "moneyline",
                "narrative_edge": "Wrestling edge",
                "crowd_positioning": "Public on favorite",
                "invalidation_risks": ["Bad weight cut"],
                "kelly_adjustment": 1.08,
                "conviction": "high",
            }
        ],
    }
    out = normalize_grok_result(raw, event_label="UFC 300")
    assert out["event"] == "UFC 300"
    assert len(out["picks"]) == 1
    assert out["picks"][0]["kelly_adjustment"] == 1.08
    assert out["picks"][0]["invalidation_risks"] == ["Bad weight cut"]


def test_build_grok_prompt_includes_picks():
    inputs = {
        "event": "UFC Test",
        "fights": [
            {
                "fight_id": "f1",
                "pick_line": "A over B",
                "prob": 0.62,
                "edge_pct": 4.2,
                "book": "DraftKings",
                "odds_display": "1.91",
                "confidence": "High",
            }
        ],
        "props": [],
    }
    prompt = build_grok_prompt(inputs)
    assert "UFC Test" in prompt
    assert "A over B" in prompt
    assert "kelly_adjustment" in prompt


def test_apply_grok_kelly_adjustments_scales_stakes():
    bets = [
        {
            "fight_id": "f1",
            "pick_line": "A over B",
            "kelly_stake_usd": 10.0,
            "kelly_pct": 2.0,
            "max_safe_bet_usd": 5.0,
            "suggested_stake": 8.0,
        }
    ]
    grok = {
        "ok": True,
        "picks": [
            {
                "id": "f1",
                "kelly_adjustment": 0.8,
                "narrative_edge": "Thin edge",
                "crowd_positioning": "Crowded",
                "invalidation_risks": ["Injury"],
                "conviction": "low",
            }
        ],
    }
    out = apply_grok_kelly_adjustments(bets, grok)
    assert out[0]["kelly_stake_usd"] == 8.0
    assert out[0]["grok_kelly_factor"] == 0.8
    assert out[0]["grok_narrative"] == "Thin edge"


def test_apply_grok_kelly_adjustments_noop_when_failed():
    bets = [{"fight_id": "f1", "kelly_stake_usd": 10.0}]
    out = apply_grok_kelly_adjustments(bets, {"ok": False, "picks": []})
    assert out[0]["kelly_stake_usd"] == 10.0
