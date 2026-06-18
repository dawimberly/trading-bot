"""Lightweight sector map + rule-based inter-sector rotation (paper-first).

Uses macro/news signals and simple proxy momentum — no ML. Complements the
Thinking Engine with explainable favored/trimmed sectors and score multipliers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import config

# Canonical sector tags
TECH = "Tech"
DEFENSE = "Defense"
AEROSPACE = "Aerospace"
SPACE = "Space"
ENERGY = "Energy"
FINANCIALS = "Financials"
CONSUMER = "Consumer"
HEALTHCARE = "Healthcare"
INDUSTRIALS = "Industrials"
AIRLINES = "Airlines"
OTHER = "Other"

TICKER_SECTOR_MAP: dict[str, str] = {
    # Tech
    "AAPL": TECH,
    "MSFT": TECH,
    "NVDA": TECH,
    "AMD": TECH,
    "GOOGL": TECH,
    "GOOG": TECH,
    "AMZN": TECH,
    "TSLA": TECH,
    "META": TECH,
    "NFLX": TECH,
    "INTC": TECH,
    "MU": TECH,
    "SMCI": TECH,
    "CRM": TECH,
    "SHOP": TECH,
    "SNOW": TECH,
    "OKTA": TECH,
    "HPE": TECH,
    "BB": TECH,
    "ARM": TECH,
    "PLTR": TECH,
    "COIN": TECH,
    "SPCX": SPACE,
    # Defense / aerospace / space
    "RTX": DEFENSE,
    "LMT": DEFENSE,
    "KTOS": DEFENSE,
    "NOC": DEFENSE,
    "GD": DEFENSE,
    "LHX": DEFENSE,
    "ATI": AEROSPACE,
    "BA": AEROSPACE,
    "LUV": AIRLINES,
    "DAL": AIRLINES,
    "UAL": AIRLINES,
    "AAL": AIRLINES,
    "RDW": SPACE,
    "ATRO": AEROSPACE,
    "RKLB": SPACE,
    "ASTS": SPACE,
    "IRDM": SPACE,
    "SPIR": SPACE,
    # Energy
    "XOM": ENERGY,
    "CVX": ENERGY,
    "LNG": ENERGY,
    "COP": ENERGY,
    "OXY": ENERGY,
    "SLB": ENERGY,
    "EOG": ENERGY,
    # Financials
    "JPM": FINANCIALS,
    "BAC": FINANCIALS,
    "GS": FINANCIALS,
    "MS": FINANCIALS,
    "WFC": FINANCIALS,
    # Healthcare
    "JNJ": HEALTHCARE,
    "UNH": HEALTHCARE,
    "PFE": HEALTHCARE,
    "LLY": HEALTHCARE,
    "MRK": HEALTHCARE,
    # Consumer
    "WMT": CONSUMER,
    "COST": CONSUMER,
    "HD": CONSUMER,
    "DIS": CONSUMER,
    "MCD": CONSUMER,
    "SBUX": CONSUMER,
    "NKE": CONSUMER,
    # Industrials
    "CAT": INDUSTRIALS,
    "DE": INDUSTRIALS,
    "GE": INDUSTRIALS,
    "HON": INDUSTRIALS,
}

SECTOR_PROXY_SYMBOLS: dict[str, str] = {
    TECH: "QQQ",
    DEFENSE: "RTX",
    AEROSPACE: "BA",
    SPACE: "RKLB",
    ENERGY: "XOM",
    FINANCIALS: "JPM",
    CONSUMER: "XLP",
    HEALTHCARE: "XLV",
    INDUSTRIALS: "XLI",
    AIRLINES: "DAL",
}

_DEFENSE_SPACE_KEYWORDS = (
    "spacex",
    "space force",
    "rocket",
    "satellite",
    "starlink",
    "launch",
    "nasa",
    "pentagon",
    "defense contract",
    "missile",
    "aerospace",
    "redwire",
    "space ipo",
    "orbit",
)
_GEO_KEYWORDS = ("iran", "middle east", "ukraine", "war", "strike", "missile", "geopolit")
_RATE_CUT_KEYWORDS = (
    "rate cut",
    "rate cuts",
    "fed cut",
    "dovish",
    "easing",
    "liquidity push",
    "policy pivot",
    "soft landing",
)

_sector_rotation_conservative_ctx: bool | None = None


def set_sector_rotation_conservative(enabled: bool | None) -> None:
    global _sector_rotation_conservative_ctx
    _sector_rotation_conservative_ctx = enabled if enabled is None else bool(enabled)


def sector_rotation_conservative_active() -> bool:
    if _sector_rotation_conservative_ctx is not None:
        return bool(_sector_rotation_conservative_ctx)
    try:
        return bool(config.sector_rotation_conservative_mode())
    except AttributeError:
        return False


@dataclass
class RotationState:
    favored: set[str] = field(default_factory=set)
    trimmed: set[str] = field(default_factory=set)
    active_rules: list[str] = field(default_factory=list)
    strength: float = 0.5
    defense_space_theme: bool = False
    narrative: str = ""
    mode: str = "rules"
    confidence: float = 0.0
    news_impact: float = 0.0
    effective_scale: float = 0.0
    sector_weights: dict[str, float] = field(default_factory=dict)
    thinking_narrative: str = ""
    validated_rules: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "favored": sorted(self.favored),
            "trimmed": sorted(self.trimmed),
            "active_rules": list(self.active_rules),
            "validated_rules": list(self.validated_rules),
            "strength": round(float(self.strength), 3),
            "defense_space_theme": self.defense_space_theme,
            "narrative": self.narrative,
            "mode": self.mode,
            "confidence": round(float(self.confidence), 3),
            "news_impact": round(float(self.news_impact), 3),
            "effective_scale": round(float(self.effective_scale), 3),
            "sector_weights": {
                k: round(float(v), 4) for k, v in self.sector_weights.items()
            },
            "thinking_narrative": self.thinking_narrative,
        }


def ticker_sector(symbol: str) -> str:
    return TICKER_SECTOR_MAP.get(config.normalize_symbol(symbol), OTHER)


def _news_theme_active(summary: dict, key: str) -> bool:
    themes = summary.get("news_themes") or {}
    block = themes.get(key) if isinstance(themes.get(key), dict) else {}
    return bool(block.get("active"))


def _headline_blob(summary: dict) -> str:
    parts = [
        summary.get("news_headlines"),
        summary.get("news_theme_summary"),
        summary.get("top_headline"),
    ]
    return " ".join(str(p or "") for p in parts).lower()


def _defense_leading(summary: dict) -> bool:
    for row in summary.get("sector_leaders") or []:
        sector = str(row.get("sector", ""))
        if "Defense" in sector and float(row.get("change_5d_pct") or 0.0) > 0:
            return True
    return False


def _spy_5d_change(summary: dict) -> float | None:
    for row in summary.get("sector_detail") or summary.get("sector_leaders") or []:
        if "Broad" in str(row.get("sector", "")) or row.get("symbol") == "SPY":
            return float(row.get("change_5d_pct") or 0.0)
    return None


def _proxy_outperforms_spy(summary: dict, proxy_label: str) -> bool:
    spy_chg = _spy_5d_change(summary)
    if spy_chg is None:
        return False
    for row in summary.get("sector_detail") or []:
        if proxy_label in str(row.get("sector", "")):
            return float(row.get("change_5d_pct") or 0.0) > spy_chg + 0.5
    return False


def _financials_strong(summary: dict) -> bool:
    return _proxy_outperforms_spy(summary, "Financials") or _proxy_outperforms_spy(
        summary, "JPM"
    )


def _tech_lagging(summary: dict) -> bool:
    for row in summary.get("sector_laggards") or []:
        if any(k in str(row.get("sector", "")) for k in ("Tech", "Semis", "QQQ")):
            return True
    return _proxy_outperforms_spy(summary, "Tech") is False and not _tech_strong(summary)


def _gold_strong(summary: dict) -> bool:
    return float(summary.get("gold_change") or 0.0) >= 0.0


def _energy_strong(summary: dict) -> bool:
    if float(summary.get("oil_change") or 0.0) >= 2.0:
        return True
    return _proxy_outperforms_spy(summary, "Energy") or _proxy_outperforms_spy(summary, "XOM")


def _defense_strong(summary: dict) -> bool:
    return (
        _defense_leading(summary)
        or _proxy_outperforms_spy(summary, "Defense")
        or _gold_strong(summary)
    )


def _tech_strong(summary: dict) -> bool:
    for row in summary.get("sector_leaders") or []:
        if any(k in str(row.get("sector", "")) for k in ("Tech", "Semis", "AI", "QQQ")):
            return float(row.get("change_5d_pct") or 0.0) > 0
    return _proxy_outperforms_spy(summary, "Tech")


def _evaluate_rule_candidates(summary: dict) -> RotationState:
    """Rules layer: propose favored/trimmed sectors (unvalidated)."""
    state = RotationState()
    oil = float(summary.get("oil_change") or 0.0)
    vix = summary.get("vix")
    vix_f = float(vix) if vix not in (None, "n/a") else 18.0
    headline = _headline_blob(summary)
    geo = (
        any(k in headline for k in _GEO_KEYWORDS)
        or _news_theme_active(summary, "geopolitics")
    )
    energy_news = _news_theme_active(summary, "sector_energy")
    tech_news = _news_theme_active(summary, "sector_tech")
    phase = str(summary.get("ai_cycle_phase") or "").lower()
    crowded = str(summary.get("crowded_trade_warning") or "").startswith("CROWDED")
    defense_space = any(k in headline for k in _DEFENSE_SPACE_KEYWORDS)

    defense_signal = (
        geo
        or defense_space
        or _defense_leading(summary)
        or _proxy_outperforms_spy(summary, "Defense")
    )
    if defense_signal:
        state.active_rules.append("defense_boom")
        state.favored.update({DEFENSE, AEROSPACE, SPACE})
        if not sector_rotation_conservative_active():
            state.trimmed.add(TECH)
        state.strength = max(state.strength, 0.65 if defense_space else 0.55)
        state.defense_space_theme = defense_space or any(
            k in headline for k in ("spacex", "space", "launch", "satellite")
        )

    rate_cut_signal = (
        _news_theme_active(summary, "liquidity")
        or any(k in headline for k in _RATE_CUT_KEYWORDS)
    )
    if rate_cut_signal:
        state.active_rules.append("rate_cuts")
        state.favored.add(FINANCIALS)
        state.strength = max(state.strength, 0.50)

    if oil >= 4.0 or energy_news or (geo and oil > 0):
        state.active_rules.append("oil_shock")
        state.favored.add(ENERGY)
        state.trimmed.update({AIRLINES, CONSUMER})
        state.strength = max(state.strength, 0.70 if oil >= 4.0 else 0.55)

    if vix_f >= 25:
        state.active_rules.append("risk_off")
        state.favored.add(HEALTHCARE)
        state.trimmed.update({CONSUMER, TECH})
        state.strength = max(state.strength, min(0.85, 0.50 + (vix_f - 25) * 0.02))

    if tech_news and ("mid-cycle" in phase or "ai" in phase) and not crowded:
        state.active_rules.append("tech_leadership")
        state.favored.add(TECH)
        state.strength = max(state.strength, 0.45)
    elif tech_news and crowded:
        state.active_rules.append("tech_crowded_trim")
        state.trimmed.add(TECH)
        state.strength = max(state.strength, 0.50)

    return state


def _validate_rules(summary: dict, candidates: RotationState) -> list[str]:
    """Rules validation — drop signals without confirming price action."""
    validated: list[str] = []
    for rule in candidates.active_rules:
        if rule == "defense_boom":
            if _defense_strong(summary) or candidates.defense_space_theme:
                validated.append(rule)
        elif rule == "oil_shock":
            if _energy_strong(summary):
                validated.append(rule)
        elif rule == "risk_off":
            vix = summary.get("vix")
            vix_f = float(vix) if vix not in (None, "n/a") else 18.0
            if vix_f >= 25 and (_gold_strong(summary) or vix_f >= 28):
                validated.append(rule)
        elif rule == "tech_leadership":
            if _tech_strong(summary):
                validated.append(rule)
        elif rule == "tech_crowded_trim":
            validated.append(rule)
        elif rule == "rate_cuts":
            if _financials_strong(summary) or _news_theme_active(summary, "liquidity"):
                validated.append(rule)
    return validated


def _apply_validated_to_state(
    summary: dict,
    candidates: RotationState,
    validated: list[str],
    *,
    trim_tech_on_defense: bool,
) -> RotationState:
    """Rebuild favored/trimmed from validated rules only."""
    state = RotationState(
        active_rules=list(candidates.active_rules),
        validated_rules=list(validated),
        strength=candidates.strength,
        defense_space_theme=candidates.defense_space_theme,
        mode="rules",
    )
    for rule in validated:
        if rule == "defense_boom":
            state.favored.update({DEFENSE, AEROSPACE, SPACE})
            if trim_tech_on_defense and _tech_lagging(summary):
                state.trimmed.add(TECH)
        elif rule == "oil_shock":
            state.favored.add(ENERGY)
            state.trimmed.update({AIRLINES, CONSUMER})
        elif rule == "risk_off":
            state.favored.add(HEALTHCARE)
            state.trimmed.update({CONSUMER, TECH})
        elif rule == "tech_leadership":
            state.favored.add(TECH)
        elif rule == "tech_crowded_trim":
            state.trimmed.add(TECH)
        elif rule == "rate_cuts":
            state.favored.add(FINANCIALS)
    return state


def _rotation_scale_params() -> tuple[float, float, float, int]:
    """boost, trim, max_sector_delta, max_sectors."""
    if sector_rotation_conservative_active():
        return (
            config.SECTOR_ROTATION_CONSERVATIVE_BOOST,
            config.SECTOR_ROTATION_CONSERVATIVE_TRIM,
            config.SECTOR_ROTATION_CONSERVATIVE_MAX_DELTA,
            config.SECTOR_ROTATION_CONSERVATIVE_MAX_SECTORS,
        )
    return (
        config.SECTOR_ROTATION_SCORE_BOOST,
        config.SECTOR_ROTATION_SCORE_TRIM,
        config.SECTOR_ROTATION_MAX_SECTOR_DELTA,
        config.SECTOR_ROTATION_MAX_ACTIVE_SECTORS,
    )


def evaluate_conservative_rotation(
    summary: dict | None,
    *,
    confidence: float = 0.7,
    suggested_tilt: dict | None = None,
    narrative: str | None = None,
) -> RotationState:
    """Validated macro rules + light thinking bias; mild boosts, max 1 sector."""
    summary = summary or {}
    conf = max(0.0, min(1.0, float(confidence)))
    news_impact = max(0.0, min(1.0, float(summary.get("news_impact_score") or 0.0)))
    effective_scale = min(0.45, conf * max(news_impact, 0.40))

    candidates = _evaluate_rule_candidates(summary)
    validated = _validate_rules(summary, candidates)
    state = _apply_validated_to_state(
        summary,
        candidates,
        validated,
        trim_tech_on_defense=False,
    )

    thinking_bias = _thinking_sector_bias(
        summary, suggested_tilt=suggested_tilt, narrative=narrative
    )
    combined = _rule_sector_scores(validated, state)
    for sector, bias in thinking_bias.items():
        combined[sector] = combined.get(sector, 0.0) + bias * conf * 0.45

    max_sec = config.SECTOR_ROTATION_CONSERVATIVE_MAX_SECTORS
    favored_w, trimmed_w = _pick_top_sectors(combined, max_sectors=max_sec)

    state.favored = set(favored_w.keys())
    state.trimmed = set(trimmed_w.keys())
    state.sector_weights = {**favored_w, **{k: -v for k, v in trimmed_w.items()}}
    state.mode = "conservative"
    state.confidence = conf
    state.news_impact = news_impact
    state.effective_scale = effective_scale
    state.strength = effective_scale
    state.thinking_narrative = (narrative or str(summary.get("narrative") or ""))[:160]
    state.narrative = build_rotation_narrative(state)
    return state


def _thinking_sector_bias(
    summary: dict,
    *,
    suggested_tilt: dict | None = None,
    narrative: str | None = None,
) -> dict[str, float]:
    """Thinking-engine sector bias from tilt weights + narrative (-1..+1 scale)."""
    bias: dict[str, float] = {}
    tilt = suggested_tilt or summary.get("suggested_tilt") or {}
    text = " ".join(
        str(x or "")
        for x in (
            narrative,
            summary.get("narrative"),
            summary.get("sector_rotation_narrative"),
            summary.get("news_theme_summary"),
            _headline_blob(summary),
        )
    ).lower()

    energy_w = float(tilt.get("energy", 0.0) or 0.0)
    spy_w = float(tilt.get("spy", 0.0) or 0.0)
    vti_w = float(tilt.get("vti", 0.0) or 0.0)
    cash_w = float(tilt.get("cash", 0.0) or 0.0)
    gold_w = float(tilt.get("gold", 0.0) or 0.0)

    if energy_w > 0.07:
        bias[ENERGY] = bias.get(ENERGY, 0.0) + min(1.0, energy_w * 4)
    if spy_w > 0.12:
        bias[TECH] = bias.get(TECH, 0.0) + min(1.0, (spy_w - 0.10) * 3)
    elif spy_w < 0.08:
        bias[TECH] = bias.get(TECH, 0.0) - 0.35
    if gold_w > 0.04 or _gold_strong(summary):
        bias[HEALTHCARE] = bias.get(HEALTHCARE, 0.0) + 0.25
    if cash_w > 0.14:
        bias[CONSUMER] = bias.get(CONSUMER, 0.0) - 0.3
        bias[TECH] = bias.get(TECH, 0.0) - 0.2
    if vti_w > 0.55:
        bias[TECH] = bias.get(TECH, 0.0) - 0.15

    if any(k in text for k in _DEFENSE_SPACE_KEYWORDS + ("defense", "pentagon", "missile")):
        for sec in (DEFENSE, AEROSPACE, SPACE):
            bias[sec] = bias.get(sec, 0.0) + 0.45
        bias[TECH] = bias.get(TECH, 0.0) - 0.25
    if _news_theme_active(summary, "sector_energy") or "oil" in text:
        bias[ENERGY] = bias.get(ENERGY, 0.0) + 0.4
        bias[AIRLINES] = bias.get(AIRLINES, 0.0) - 0.35
    if _news_theme_active(summary, "sector_tech") or "ai" in text:
        phase = str(summary.get("ai_cycle_phase") or "").lower()
        if "late" not in phase and "exhaustion" not in phase:
            bias[TECH] = bias.get(TECH, 0.0) + 0.35

    return bias


def _rule_sector_scores(validated: list[str], candidates: RotationState) -> dict[str, float]:
    scores: dict[str, float] = {}
    for rule in validated:
        if rule == "defense_boom":
            for sec in (DEFENSE, AEROSPACE, SPACE):
                scores[sec] = scores.get(sec, 0.0) + 0.55
            scores[TECH] = scores.get(TECH, 0.0) - 0.45
        elif rule == "oil_shock":
            scores[ENERGY] = scores.get(ENERGY, 0.0) + 0.6
            scores[AIRLINES] = scores.get(AIRLINES, 0.0) - 0.5
            scores[CONSUMER] = scores.get(CONSUMER, 0.0) - 0.35
        elif rule == "risk_off":
            scores[HEALTHCARE] = scores.get(HEALTHCARE, 0.0) + 0.5
            scores[CONSUMER] = scores.get(CONSUMER, 0.0) - 0.4
            scores[TECH] = scores.get(TECH, 0.0) - 0.35
        elif rule == "tech_leadership":
            scores[TECH] = scores.get(TECH, 0.0) + 0.45
        elif rule == "tech_crowded_trim":
            scores[TECH] = scores.get(TECH, 0.0) - 0.5
        elif rule == "rate_cuts":
            scores[FINANCIALS] = scores.get(FINANCIALS, 0.0) + 0.35
    return scores


def _pick_top_sectors(
    scores: dict[str, float],
    *,
    max_sectors: int,
) -> tuple[dict[str, float], dict[str, float]]:
    favored: dict[str, float] = {}
    trimmed: dict[str, float] = {}
    if not scores:
        return favored, trimmed
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    for sector, score in ranked:
        if score > 0.12 and len(favored) < max_sectors:
            favored[sector] = score
    for sector, score in sorted(scores.items(), key=lambda kv: kv[1]):
        if score < -0.12 and len(trimmed) < max_sectors:
            trimmed[sector] = abs(score)
    return favored, trimmed


def evaluate_hybrid_rotation(
    summary: dict | None,
    *,
    confidence: float = 0.7,
    suggested_tilt: dict | None = None,
    narrative: str | None = None,
) -> RotationState:
    """Hybrid adaptive: thinking bias + validated rules, scaled by conf * news impact."""
    summary = summary or {}
    conf = max(0.0, min(1.0, float(confidence)))
    news_impact = max(0.0, min(1.0, float(summary.get("news_impact_score") or 0.0)))
    impact_floor = 0.35 if news_impact > 0 else 0.50
    effective_scale = conf * max(news_impact, impact_floor)

    candidates = _evaluate_rule_candidates(summary)
    validated = _validate_rules(summary, candidates)
    thinking_bias = _thinking_sector_bias(
        summary, suggested_tilt=suggested_tilt, narrative=narrative
    )

    combined: dict[str, float] = _rule_sector_scores(validated, candidates)
    for sector, bias in thinking_bias.items():
        combined[sector] = combined.get(sector, 0.0) + bias * conf

    max_sec = config.SECTOR_ROTATION_MAX_ACTIVE_SECTORS
    favored_w, trimmed_w = _pick_top_sectors(combined, max_sectors=max_sec)

    state = RotationState(
        favored=set(favored_w.keys()),
        trimmed=set(trimmed_w.keys()),
        active_rules=list(candidates.active_rules),
        validated_rules=validated,
        strength=min(1.0, effective_scale),
        defense_space_theme=candidates.defense_space_theme,
        mode="hybrid",
        confidence=conf,
        news_impact=news_impact,
        effective_scale=effective_scale,
        sector_weights={**favored_w, **{k: -v for k, v in trimmed_w.items()}},
        thinking_narrative=(narrative or str(summary.get("narrative") or ""))[:160],
    )
    state.narrative = build_rotation_narrative(state)
    return state


def evaluate_rotation_state(
    summary: dict | None,
    *,
    confidence: float = 0.7,
    suggested_tilt: dict | None = None,
    narrative: str | None = None,
) -> RotationState:
    """Dispatch conservative, hybrid adaptive, or legacy rules-only rotation."""
    if sector_rotation_conservative_active():
        return evaluate_conservative_rotation(
            summary,
            confidence=confidence,
            suggested_tilt=suggested_tilt,
            narrative=narrative,
        )
    if config.effective_sector_rotation_hybrid():
        return evaluate_hybrid_rotation(
            summary,
            confidence=confidence,
            suggested_tilt=suggested_tilt,
            narrative=narrative,
        )
    summary = summary or {}
    candidates = _evaluate_rule_candidates(summary)
    validated = _validate_rules(summary, candidates)
    state = _apply_validated_to_state(
        summary,
        candidates,
        validated,
        trim_tech_on_defense=True,
    )
    state.narrative = build_rotation_narrative(state)
    state.mode = "rules"
    return state


def build_rotation_narrative(state: RotationState) -> str:
    if not state.favored and not state.trimmed and not state.validated_rules:
        return "Sector rotation neutral — no validated inter-sector shift."
    parts: list[str] = []
    if state.mode == "hybrid":
        parts.append(
            f"Hybrid adaptive (conf={state.confidence:.2f}, impact={state.news_impact:.2f}, "
            f"scale={state.effective_scale:.2f})"
        )
        if state.thinking_narrative:
            parts.append(f"Thinking: {state.thinking_narrative[:100]}")
        if state.validated_rules:
            parts.append(f"Validated: {', '.join(state.validated_rules)}")
    elif state.mode == "conservative":
        parts.append(
            f"Conservative macro rotation (conf={state.confidence:.2f}, "
            f"scale={state.effective_scale:.2f})"
        )
        if state.thinking_narrative:
            parts.append(f"Thinking: {state.thinking_narrative[:100]}")
        if state.validated_rules:
            parts.append(f"Regimes: {', '.join(state.validated_rules)}")
    if "defense_boom" in state.validated_rules or "defense_boom" in state.active_rules:
        if state.defense_space_theme:
            parts.append(
                "Defense/space theme — boost defense/aerospace/space when GLD/defense confirm"
            )
        else:
            parts.append("Defense leadership validated — rotate toward defense/aerospace")
    if "oil_shock" in state.validated_rules or "oil_shock" in state.active_rules:
        parts.append("Oil/energy shock validated — favor energy, trim airlines/consumer")
    if "risk_off" in state.validated_rules or "risk_off" in state.active_rules:
        parts.append("Risk-off validated — favor healthcare, trim growth")
    if "tech_leadership" in state.validated_rules:
        parts.append("Tech leadership validated — modest tech overweight")
    if "tech_crowded_trim" in state.validated_rules:
        parts.append("Crowded tech trim")
    if "rate_cuts" in state.validated_rules:
        parts.append("Rate-cut / liquidity theme — modest financials tilt")
    fav = ", ".join(sorted(state.favored)) if state.favored else "none"
    trim = ", ".join(sorted(state.trimmed)) if state.trimmed else "none"
    max_sec = (
        config.SECTOR_ROTATION_CONSERVATIVE_MAX_SECTORS
        if state.mode == "conservative"
        else config.SECTOR_ROTATION_MAX_ACTIVE_SECTORS
    )
    parts.append(f"Favor (max {max_sec}): {fav}")
    parts.append(f"Trim (max {max_sec}): {trim}")
    return " | ".join(parts)


def score_multiplier(symbol: str, state: RotationState | None) -> float:
    """Screener composite multiplier, scaled by hybrid effective_scale."""
    if state is None or (not state.favored and not state.trimmed):
        return 1.0
    sector = ticker_sector(symbol)
    boost, trim, _, _ = _rotation_scale_params()
    strength = max(0.35, min(1.0, float(state.effective_scale or state.strength)))
    if sector in state.favored:
        return 1.0 + boost * strength
    if sector in state.trimmed:
        return max(0.80, 1.0 - trim * strength)
    return 1.0


_SECTOR_SLEEVE_BOOST: dict[str, tuple[str, float]] = {
    DEFENSE: ("nyse", 1.0),
    AEROSPACE: ("nyse", 0.85),
    SPACE: ("nyse", 0.85),
    ENERGY: ("nyse", 1.0),
    HEALTHCARE: ("nyse", 0.75),
    TECH: ("spy", 1.0),
    FINANCIALS: ("nyse", 0.6),
    INDUSTRIALS: ("nyse", 0.5),
}

_SECTOR_SLEEVE_TRIM: dict[str, tuple[str, float]] = {
    TECH: ("spy", 1.0),
    CONSUMER: ("spy", 0.55),
    AIRLINES: ("nyse", 0.65),
}


def cap_deltas_from_rotation(
    summary: dict,
    confidence: float,
    *,
    suggested_tilt: dict | None = None,
    narrative: str | None = None,
) -> dict[str, float]:
    """Hybrid sleeve cap deltas — max ±8% per sector, ±6% per sleeve."""
    if not config.effective_sector_rotation_enabled():
        return {}
    state = evaluate_rotation_state(
        summary,
        confidence=confidence,
        suggested_tilt=suggested_tilt,
        narrative=narrative,
    )
    if not state.favored and not state.trimmed:
        return {}

    _, _, max_sector, _ = _rotation_scale_params()
    sleeve_cap = config.effective_thinking_max_sleeve_delta()
    scale = max(0.0, min(1.0, float(state.effective_scale or state.strength)))
    deltas = {
        "nyse": 0.0,
        "spy": 0.0,
        "metal": 0.0,
        "crypto": 0.0,
        "cash_buffer": 0.0,
        "vti_core": 0.0,
    }

    for sector, weight in state.sector_weights.items():
        if weight > 0 and sector in state.favored:
            mapping = _SECTOR_SLEEVE_BOOST.get(sector)
            if not mapping:
                continue
            sleeve, mult = mapping
            amt = min(max_sector, max_sector * min(1.0, weight)) * scale * mult
            deltas[sleeve] = deltas.get(sleeve, 0.0) + amt
        elif weight < 0 and sector in state.trimmed:
            mapping = _SECTOR_SLEEVE_TRIM.get(sector)
            if not mapping:
                continue
            sleeve, mult = mapping
            amt = min(max_sector, max_sector * min(1.0, abs(weight))) * scale * mult
            deltas[sleeve] = deltas.get(sleeve, 0.0) - amt

    if "risk_off" in state.validated_rules and _gold_strong(summary):
        deltas["metal"] += min(max_sector, 0.04 * scale)

    return {
        k: round(max(-sleeve_cap, min(sleeve_cap, float(v))), 6)
        for k, v in deltas.items()
        if abs(v) > 1e-9
    }


def apply_rotation_to_ranked(ranked: list[str], summary: dict | None) -> list[str]:
    """Reorder momentum picks: favored sectors first, trimmed sectors deferred."""
    if not ranked or not config.effective_sector_rotation_enabled():
        return ranked
    state = evaluate_rotation_state(summary or {})
    if not state.favored and not state.trimmed:
        return ranked

    def sort_key(sym: str) -> tuple[int, int]:
        sector = ticker_sector(sym)
        if sector in state.favored:
            tier = 0
        elif sector in state.trimmed:
            tier = 2
        else:
            tier = 1
        try:
            idx = ranked.index(sym)
        except ValueError:
            idx = len(ranked)
        return (tier, idx)

    return sorted(ranked, key=sort_key)


def build_screener_rotation_context() -> dict:
    """Lightweight macro snapshot for standalone screener runs."""
    macro_cache: dict = {}
    try:
        from modules.thinking_engine import (
            _build_sector_leadership,
            _load_macro_close,
            _pct_change,
        )

        oil_series = _load_macro_close("USO", macro_cache)
        if oil_series.empty:
            oil_series = _load_macro_close("XOM", macro_cache)
        gold_series = _load_macro_close("GLD", macro_cache)
        vix_series = _load_macro_close("VIX", macro_cache)
        vix_val = float(vix_series.iloc[-1]) if len(vix_series) else None
        sector = _build_sector_leadership(None, macro_cache)
        return {
            "oil_change": _pct_change(oil_series) if not oil_series.empty else 0.0,
            "gold_change": _pct_change(gold_series) if not gold_series.empty else 0.0,
            "vix": round(vix_val, 1) if vix_val is not None else "n/a",
            "sector_leaders": sector["leaders"],
            "sector_detail": sector["sectors"],
            "sector_leadership": sector["leadership_str"],
            "top_headline": "",
            "crowded_trade_warning": "",
            "ai_cycle_phase": "",
        }
    except Exception:
        return {"vix": 18.0, "oil_change": 0.0}


def build_backtest_rotation_summary(
    data,
    *,
    bar_idx: int | None = None,
    full_data=None,
) -> dict:
    """Per-bar rotation context during backtest (uses data slice)."""
    try:
        from modules.thinking_engine import _build_sector_leadership, _pct_change
        from modules.pipeline_strategies import _spy_market_up_signal

        frame = data
        if bar_idx is not None and full_data is not None:
            frame = full_data.iloc[: bar_idx + 1]
        macro_cache: dict = {}
        sector = _build_sector_leadership(frame, macro_cache)
        oil_chg = 0.0
        for sym in ("USO", "XOM"):
            if sym in frame.columns:
                try:
                    oil_chg = _pct_change(frame[sym].dropna())
                    break
                except (TypeError, ValueError, IndexError):
                    pass
        vix_f = 18.0
        if "VIX" in frame.columns:
            try:
                vix_f = float(frame["VIX"].dropna().iloc[-1])
            except (TypeError, ValueError, IndexError):
                pass
        up, _ = _spy_market_up_signal(frame, config.SPY_BOT_SYMBOL, config.SPY_MA_WINDOW)
        crowded = ""
        if sector.get("leaders"):
            top = sector["leaders"][0]
            if "Tech" in str(top.get("sector", "")) and float(top.get("change_5d_pct") or 0) > 4:
                crowded = "CROWDED — tech leadership extended"
        gold_chg = 0.0
        if "GLD" in frame.columns:
            try:
                gold_chg = _pct_change(frame["GLD"].dropna())
            except (TypeError, ValueError, IndexError):
                pass
        return {
            "oil_change": oil_chg,
            "gold_change": gold_chg,
            "vix": vix_f,
            "sector_leaders": sector["leaders"],
            "sector_laggards": sector.get("laggards") or [],
            "sector_detail": sector["sectors"],
            "spy_trend": f"above MA{config.SPY_MA_WINDOW}" if up else f"below MA{config.SPY_MA_WINDOW}",
            "crowded_trade_warning": crowded,
            "ai_cycle_phase": "",
            "top_headline": "",
        }
    except Exception:
        return {}


def enrich_summary_with_rotation(summary: dict, data=None) -> None:
    """Attach hybrid rotation state + narrative to market summary (in-place)."""
    if not config.effective_sector_rotation_enabled():
        return
    news_impact = float(summary.get("news_impact_score") or 0.0)
    conf = float(summary.get("thinking_confidence") or 0.0)
    if conf <= 0:
        conf = 0.55 + 0.35 * news_impact if news_impact > 0 else 0.60
    state = evaluate_rotation_state(
        summary,
        confidence=conf,
        suggested_tilt=summary.get("suggested_tilt"),
        narrative=str(summary.get("narrative") or ""),
    )
    summary["sector_rotation"] = state.to_dict()
    summary["sector_rotation_narrative"] = state.narrative
    summary["sector_rotation_favored"] = sorted(state.favored)
    summary["sector_rotation_trimmed"] = sorted(state.trimmed)


# Alias for callers expecting the earlier exploration name
build_lightweight_macro_summary = build_screener_rotation_context


def demo_spacex_rotation() -> RotationState:
    """Example: SpaceX IPO / launch headline triggers defense/space favor, tech trim."""
    summary = {
        "top_headline": "SpaceX valued at record high ahead of potential IPO; defense primes rally",
        "news_headlines": "SpaceX Starlink launch success boosts space sector; Pentagon awards new missile contract",
        "news_themes": {
            "geopolitics": {"active": True},
            "sector_tech": {"active": True},
        },
        "oil_change": 1.5,
        "vix": 19.0,
        "gold_change": 1.2,
        "news_impact_score": 0.62,
        "sector_leaders": [
            {"sector": "Defense (RTX)", "symbol": "RTX", "change_5d_pct": 3.2},
            {"sector": "Broad (SPY)", "symbol": "SPY", "change_5d_pct": 0.8},
            {"sector": "Tech (QQQ)", "symbol": "QQQ", "change_5d_pct": 1.1},
        ],
        "sector_detail": [
            {"sector": "Defense (RTX)", "symbol": "RTX", "change_5d_pct": 3.2},
            {"sector": "Broad (SPY)", "symbol": "SPY", "change_5d_pct": 0.8},
        ],
        "ai_cycle_phase": "mid-cycle",
        "crowded_trade_warning": "",
    }
    return evaluate_rotation_state(
        summary, confidence=0.78, narrative=summary["top_headline"]
    )
