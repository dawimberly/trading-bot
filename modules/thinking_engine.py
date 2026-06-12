"""Local LLM market reasoning via Ollama — paper bot only."""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
import threading

import config
from modules.safe_io import read_json_file, write_json_file
from modules.logging_utils import log_event

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / config.THINKING_ENGINE_STATE_FILE
OUTPUT_FILE = ROOT / config.THINKING_ENGINE_OUTPUT_FILE
APPROVAL_FILE = ROOT / config.THINKING_APPROVAL_FILE

_TILT_KEYS = ("vti", "spy", "energy", "gold", "cash", "crypto", "bonds")
_CAP_KEYS = ("vti_core", "spy", "crypto", "nyse", "metal", "cash_buffer")
_TILT_TO_CAP = {
    "vti": "vti_core",
    "spy": "spy",
    "energy": "nyse",
    "gold": "metal",
    "cash": "cash_buffer",
    "crypto": "crypto",
    "bonds": "cash_buffer",
}
_CAP_LABELS = {
    "vti_core": "VTI",
    "spy": "SPY",
    "crypto": "Crypto",
    "nyse": "Energy",
    "metal": "Gold",
    "cash_buffer": "Cash",
}
_GEO_KEYWORDS = (
    "iran",
    "israel",
    "middle east",
    "war",
    "sanctions",
    "hormuz",
    "geopolitical",
    "missile",
)

_PM_SYSTEM_PROMPT = """You are an elite asymmetric-risk hedge fund PM with real capital at risk. Your job is one decisive 3-7 day allocation call — not commentary.

CORE MANDATE:
- Be bold when asymmetry is clear. Be defensive when uncertainty dominates.
- Maintain consistency with yesterday's tilt unless STRONG NEW EVIDENCE appears (VIX spike, trend break, major headline, confirmed safe-haven bid).
- If you change any sleeve by more than 5% vs yesterday, you MUST cite the new evidence in TILT_RATIONALE.

HARD RULES (non-negotiable):
1. Do NOT overweight gold when Gold 5d change is negative (liquidity sell, not safe-haven). Default gold to 0% unless asymmetry explicitly argues a contrarian bounce.
2. When VIX is rising and there is NO strong momentum continuation narrative, bias toward cash — do not run max equity.
3. When SPY is below MA200 and VIX is elevated, prioritize capital preservation over chasing beta.
4. RECOMMENDED_TILT weights must sum to ~1.0 across sleeves (vti, spy, energy, gold, cash, crypto, bonds).
5. TILT_RATIONALE must explicitly justify EVERY sleeve you allocate above 5% with its exact percentage (e.g. "VTI 52% because...", "cash 18% because VIX rising...").

DECISION FRAMEWORK:
1. Dominant narrative — what is actually moving markets (ignore noise)?
2. Asymmetry — where is crowd positioning wrong or forced to adjust?
3. Risks (max 2) and opportunities (max 2) — highest signal only.
4. Clear RECOMMENDED_TILT — decisive, not vague 50/50 blends unless truly uncertain.

Output format (strict — ENTIRE reply ONLY these lines, no preamble, no markdown headers):
NARRATIVE: [One powerful sentence]
ASYMMETRY: [Specific edge — name winners and losers]
RISKS: [max 2 bullets, comma-separated or semicolon-separated]
OPPORTUNITIES: [max 2 bullets]
RECOMMENDED_TILT: {"vti": 0.XX, "spy": 0.XX, "energy": 0.XX, "gold": 0.XX, "cash": 0.XX, "crypto": 0.XX, "bonds": 0.XX}
TILT_RATIONALE: [Link asymmetry to EACH >5% sleeve with exact percentages — mandatory]
CONFIDENCE: 0.XX
REASONING: [Concise link from narrative to tilt]

Do not explain your process. Do not repeat the input. Start with NARRATIVE:"""

_STRUCTURED_FIELD_RE = re.compile(
    r"^(?:#+\s*)?(NARRATIVE|ASYMMETRY|RISKS|OPPORTUNITIES|RECOMMENDED_TILT|TILT|TILT_RATIONALE|CONFIDENCE|REASONING|PARADIGM_SHIFT|REGIME_NARRATIVE)\s*[:=\-]\s*(.*)$",
    re.I,
)
_TILT_PROSE_RE = re.compile(
    r"\b(vti|spy|energy|gold|cash|crypto|bonds)\s*[:=]?\s*(\d{1,3}(?:\.\d+)?)\s*%?",
    re.I,
)


def _pct_change(series, days: int = 5) -> float | None:
    if series is None or len(series) < days + 1:
        return None
    try:
        start = float(series.iloc[-days - 1])
        end = float(series.iloc[-1])
        if start <= 0:
            return None
        return round((end / start - 1.0) * 100.0, 2)
    except (TypeError, ValueError, IndexError):
        return None


def _series_trend_desc(series, *, days: int = 5) -> str:
    ch = _pct_change(series, days)
    if ch is None:
        return "n/a"
    if ch > 5.0:
        return f"rising ({ch:+.1f}% {days}d)"
    if ch < -5.0:
        return f"falling ({ch:+.1f}% {days}d)"
    return f"stable ({ch:+.1f}% {days}d)"


def _load_macro_close(col: str, cache: dict | None = None):
    from modules.macro_regime_adaptor import _load_daily_close

    if cache is not None:
        if col not in cache:
            cache[col] = _load_daily_close(col)
        return cache[col]
    return _load_daily_close(col)


def _yield_curve_summary(macro_cache: dict | None = None) -> str:
    """TLT/TNX levels and 5d moves; yield stress when TNX rising + TLT weak."""
    tlt = _load_macro_close("TLT", macro_cache)
    tnx = _load_macro_close("TNX", macro_cache)
    if tlt.empty and tnx.empty:
        return "n/a"
    parts: list[str] = []
    if not tnx.empty:
        try:
            parts.append(f"TNX {float(tnx.iloc[-1]):.2f}%")
        except (TypeError, ValueError):
            pass
    tlt_5d = _pct_change(tlt) if not tlt.empty else None
    tnx_5d = _pct_change(tnx) if not tnx.empty else None
    if tlt_5d is not None:
        parts.append(f"TLT {tlt_5d:+.1f}% 5d")
    if tnx_5d is not None:
        parts.append(f"TNX {tnx_5d:+.1f}% 5d")
    try:
        from modules.macro_regime_adaptor import _tlt_yield_stress

        if _tlt_yield_stress(tlt, tnx):
            parts.append("yield stress (TNX up + TLT weak)")
    except Exception:
        pass
    return ", ".join(parts) if parts else "n/a"


def _format_bot_exposure(base_caps: dict[str, float] | None = None) -> str:
    caps = base_caps or config.fund_allocation_pct()
    parts: list[str] = []
    for key in _CAP_KEYS:
        pct = float(caps.get(key, 0.0))
        if pct >= 0.005:
            parts.append(f"{_CAP_LABELS[key]} {pct:.0%}")
    return ", ".join(parts) if parts else "n/a"


def build_market_summary(
    data,
    regime: str,
    vol: str,
    *,
    wisdom: dict | None = None,
    top_headline: str | None = None,
    base_caps: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Assemble context for the PM-style reasoning prompt."""
    from modules.pipeline_strategies import _spy_market_up_signal

    macro_cache: dict = {}
    spy_sym = config.SPY_BOT_SYMBOL
    up, mom = _spy_market_up_signal(data, spy_sym, config.SPY_MA_WINDOW)
    if up:
        spy_trend = f"above MA{config.SPY_MA_WINDOW} (+{mom * 100:.1f}%)"
    else:
        spy_trend = f"below MA{config.SPY_MA_WINDOW}"

    vix_series = _load_macro_close("VIX", macro_cache)
    vix_val = float(vix_series.iloc[-1]) if len(vix_series) else None
    vix_trend = _series_trend_desc(vix_series)
    yield_curve = _yield_curve_summary(macro_cache)
    caps = base_caps or config.fund_allocation_pct()
    bot_exposure = {k: round(float(caps.get(k, 0.0)), 4) for k in _CAP_KEYS}

    oil_series = _load_macro_close("USO", macro_cache)
    if oil_series.empty:
        oil_series = _load_macro_close("XOM", macro_cache)
    gold_series = _load_macro_close("GLD", macro_cache)

    web = (wisdom or {}).get("web_sentiment")
    price = (wisdom or {}).get("price_sentiment")
    macro_sentiment = f"regime={regime}, vol={vol}"
    if web is not None:
        macro_sentiment += f", web={web:+.2f}"
    if price is not None:
        macro_sentiment += f", price={price:+.2f}"

    headline = top_headline or (wisdom or {}).get("felix_video_title") or "n/a"
    if headline == "n/a":
        try:
            from modules.web_sentiment_live import get_live_web_sentiment

            if get_live_web_sentiment() is not None:
                headline = "finance headline mood cached (see web sentiment)"
        except Exception:
            pass

    return {
        "spy_trend": spy_trend,
        "vix": round(vix_val, 1) if vix_val is not None else "n/a",
        "vix_trend": vix_trend,
        "yield_curve": yield_curve,
        "oil_change": _pct_change(oil_series) if not oil_series.empty else 0.0,
        "gold_change": _pct_change(gold_series) if not gold_series.empty else 0.0,
        "macro_sentiment": macro_sentiment,
        "top_headline": str(headline)[:240],
        "regime": regime,
        "bot_exposure": bot_exposure,
        "bot_exposure_str": _format_bot_exposure(caps),
    }


def _caps_to_tilt(caps: dict[str, float]) -> dict[str, float]:
    total = sum(float(caps.get(k, 0.0)) for k in _CAP_KEYS)
    if total <= 0:
        return {k: 0.0 for k in _TILT_KEYS}
    tilt = {k: 0.0 for k in _TILT_KEYS}
    for tkey, ckey in _TILT_TO_CAP.items():
        tilt[tkey] += float(caps.get(ckey, 0.0)) / total
    return tilt


def _gold_momentum_ok(summary: dict | None) -> bool:
    """Gold overweight only when GLD 5d change is not negative."""
    if not summary:
        return False
    return float(summary.get("gold_change") or 0.0) >= 0.0


_GOLD_BOUNCE_KEYWORDS = (
    "contrarian bounce",
    "oversold gold",
    "gold reversal",
    "bounce in gold",
    "gold bounce",
)


def _gold_contrarian_allowed(asymmetry: str) -> bool:
    """Allow minimal gold only when asymmetry explicitly cites a contrarian gold case."""
    low = asymmetry.lower()
    if any(k in low for k in _GOLD_BOUNCE_KEYWORDS):
        return True
    return "contrarian" in low and "gold" in low


def _clamp_gold_in_tilt(
    summary: dict | None,
    tilt: dict[str, float],
    asymmetry: str = "",
) -> dict[str, float]:
    """Cap gold when GLD 5d is negative; max 2% only on explicit contrarian asymmetry."""
    if _gold_momentum_ok(summary):
        return tilt
    out = dict(tilt)
    gold_w = float(out.get("gold", 0.0))
    max_gold = 0.02 if _gold_contrarian_allowed(asymmetry) else 0.0
    if gold_w <= max_gold + 1e-6:
        if gold_w > max_gold:
            out["gold"] = max_gold
        return _normalize_tilt(out) if gold_w > max_gold else out
    freed = gold_w - max_gold
    out["gold"] = max_gold
    out["cash"] = out.get("cash", 0.0) + freed * 0.6
    out["vti"] = out.get("vti", 0.0) + freed * 0.4
    return _normalize_tilt(out)


def _rule_based_cap_deltas(summary: dict, confidence: float) -> dict[str, float]:
    """Direct sleeve cap deltas from macro signals (matches PM tilt intent)."""
    deltas = {k: 0.0 for k in _CAP_KEYS}
    conf = max(0.35, min(1.0, float(confidence)))
    oil = float(summary.get("oil_change") or 0.0)
    gold = float(summary.get("gold_change") or 0.0)
    vix = summary.get("vix")
    vix_f = float(vix) if vix not in (None, "n/a") else 18.0
    headline = str(summary.get("top_headline", "")).lower()
    geo = any(k in headline for k in _GEO_KEYWORDS)

    if oil >= 4.0 or (geo and oil > 0):
        deltas["nyse"] += 0.08 * conf
        deltas["spy"] -= 0.05 * conf
        deltas["cash_buffer"] -= 0.03 * conf
    elif oil <= -4.0:
        deltas["nyse"] -= 0.04 * conf
        deltas["vti_core"] += 0.04 * conf

    if gold >= 3.0:
        deltas["metal"] += 0.05 * conf
        deltas["vti_core"] += 0.03 * conf
        deltas["spy"] -= 0.04 * conf
        deltas["crypto"] -= 0.03 * conf
    elif vix_f >= config.MACRO_VIX_SAFE_HAVEN_MIN and gold >= 0.0:
        deltas["metal"] += 0.05 * conf
        deltas["vti_core"] += 0.03 * conf
        deltas["spy"] -= 0.04 * conf
        deltas["crypto"] -= 0.03 * conf

    if geo:
        deltas["nyse"] += 0.05 * conf
        if gold >= 0.0:
            deltas["metal"] += 0.03 * conf
        deltas["spy"] -= 0.04 * conf
        deltas["cash_buffer"] += 0.03 * conf

    if "below MA" in str(summary.get("spy_trend", "")):
        deltas["vti_core"] += 0.04 * conf
        deltas["cash_buffer"] += 0.03 * conf
        deltas["spy"] -= 0.04 * conf

    max_delta = config.effective_thinking_max_sleeve_delta()
    return {k: round(max(-max_delta, min(max_delta, v)), 6) for k, v in deltas.items()}


def _llm_nudge_deltas(
    base_caps: dict[str, float],
    suggested_tilt: dict[str, float],
    confidence: float,
) -> dict[str, float]:
    """Optional nudge from LLM target weights; stronger when confidence > 0.75."""
    baseline = _caps_to_tilt(base_caps)
    conf = max(0.35, min(1.0, float(confidence)))
    scale = 0.20 if conf > 0.75 else 0.12
    max_nudge = 0.05 if conf > 0.75 else 0.03
    nudges = {k: 0.0 for k in _CAP_KEYS}
    for tkey, ckey in _TILT_TO_CAP.items():
        diff = (float(suggested_tilt.get(tkey, 0.0)) - baseline.get(tkey, 0.0)) * conf * scale
        diff = max(-max_nudge, min(max_nudge, diff))
        nudges[ckey] = nudges.get(ckey, 0.0) + diff
    return nudges


def compute_cap_deltas(
    base_caps: dict[str, float],
    suggested_tilt: dict[str, float],
    *,
    confidence: float = 0.7,
    market_summary: dict | None = None,
    max_sleeve_delta: float | None = None,
) -> dict[str, float]:
    """Combine rule-based macro tilts with optional LLM nudges."""
    deltas = {k: 0.0 for k in _CAP_KEYS}
    if market_summary:
        for k, v in _rule_based_cap_deltas(market_summary, confidence).items():
            deltas[k] += v
    for k, v in _llm_nudge_deltas(base_caps, suggested_tilt, confidence).items():
        deltas[k] += v
    max_delta = (
        float(max_sleeve_delta)
        if max_sleeve_delta is not None
        else config.effective_thinking_max_sleeve_delta()
    )
    return {k: round(max(-max_delta, min(max_delta, v)), 6) for k, v in deltas.items()}


def _infer_asymmetry(summary: dict | None) -> str:
    if not summary:
        return "Macro reassessment — wait for clearer edge"
    oil = float(summary.get("oil_change") or 0.0)
    gold = float(summary.get("gold_change") or 0.0)
    vix = summary.get("vix")
    vix_f = float(vix) if vix not in (None, "n/a") else 0.0
    spy_trend = str(summary.get("spy_trend", ""))
    if "below MA" in spy_trend and vix_f >= 20:
        return "Crowd still long beta while trend breaks — asymmetric downside if vol persists"
    if oil >= 4.0 and "above MA" in spy_trend:
        return "Equities complacent vs energy shock — crowd under-hedged to inflation tail"
    if gold >= 3.0 and vix_f >= config.MACRO_VIX_SAFE_HAVEN_MIN:
        return "Safe-haven bid rising while equities hold — hedgers early, consensus late"
    if "rising" in str(summary.get("vix_trend", "")) and "above MA" in spy_trend:
        if gold < 0.0:
            return "Vol rising into strength with gold falling — liquidity stress, not safe-haven bid"
        return "Vol rising into strength — complacency gap before de-grossing"
    return "Range-bound chop — edge in selective tilts, not max risk"


def _infer_tilt_rationale(
    summary: dict | None,
    tilt: dict[str, float],
    asymmetry: str = "",
) -> str:
    """Fallback one-liner linking asymmetry to each material sleeve with percentages."""
    material = sorted(
        ((k, v) for k, v in tilt.items() if float(v) >= 0.05),
        key=lambda kv: kv[1],
        reverse=True,
    )
    parts = [f"{k} {v:.0%}" for k, v in material]
    alloc = "; ".join(parts) if parts else "balanced"
    if asymmetry:
        return f"Asymmetry ({asymmetry[:100]}) -> {alloc}"
    narrative = _infer_narrative(summary)
    return f"{narrative} -> {alloc}"


def _load_previous_tilt() -> dict[str, float] | None:
    """Last persisted tilt for prompt consistency."""
    cached = read_json_file(OUTPUT_FILE)
    if not cached:
        return None
    raw = cached.get("suggested_tilt")
    if not isinstance(raw, dict) or not raw:
        return None
    return _normalize_tilt(raw)


def persist_thinking_last(
    result: dict[str, Any],
    *,
    regime: str | None = None,
) -> None:
    """Write thinking_engine_last.json on every reasoning run for audit."""
    now = datetime.datetime.now().isoformat()
    val_score = result.get("validation_score")
    if val_score is None and "validation_ok" in result:
        val_score = 100 if result.get("validation_ok") else max(
            0, 100 - 15 * len(result.get("validation_errors") or [])
        )
    payload = {
        "timestamp": now,
        "regime": regime or (result.get("market_summary") or {}).get("regime"),
        "manual_review_required": config.thinking_manual_approval_required(),
        "safety": config.get_thinking_safety_summary(),
        "validation_score": val_score,
        "parse_quality": result.get("parse_quality"),
        **result,
    }
    write_json_file(OUTPUT_FILE, payload)


def build_regime_narrative(result: dict) -> str:
    """Short heartbeat summary from thinking result."""
    parts: list[str] = []
    narrative = str(result.get("narrative") or "").strip()
    if narrative:
        parts.append(narrative[:140])
    asymmetry = str(result.get("asymmetry") or "").strip()
    if asymmetry:
        parts.append(f"Edge: {asymmetry[:90]}")
    conf = result.get("confidence")
    if conf is not None:
        parts.append(f"conf {float(conf):.0%}")
    top = sorted((result.get("suggested_tilt") or {}).items(), key=lambda kv: kv[1], reverse=True)[:2]
    if top:
        parts.append(", ".join(f"{k} {v:.0%}" for k, v in top))
    return " | ".join(parts)[:280]


def _thinking_decision_id(regime: str, tilt: dict[str, float]) -> str:
    payload = f"{regime}|{json.dumps(tilt, sort_keys=True)}|{datetime.date.today().isoformat()}"
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def get_pm_system_prompt() -> str:
    """Return the production PM system prompt (for docs/tests)."""
    return _PM_SYSTEM_PROMPT


def _load_previous_tilt_full() -> dict | None:
    cached = read_json_file(OUTPUT_FILE)
    if not cached:
        return None
    return {
        "tilt": cached.get("suggested_tilt"),
        "regime": cached.get("regime"),
        "timestamp": cached.get("timestamp"),
        "narrative": cached.get("narrative"),
        "tilt_rationale": cached.get("tilt_rationale"),
        "asymmetry": cached.get("asymmetry"),
    }


_STRONG_EVIDENCE_KEYWORDS = (
    "vix spike",
    "vol spike",
    "trend break",
    "headline",
    "geopolitical",
    "war",
    "sanctions",
    "safe-haven",
    "safe haven",
    "oil shock",
    "regime shift",
    "paradigm",
    "breakdown",
    "liquidity stress",
    "new evidence",
    "shift",
)


def _strong_new_evidence(market_summary: dict, result: dict[str, Any]) -> bool:
    """True when macro or narrative supports large tilt changes vs prior day."""
    headline = str(market_summary.get("top_headline") or "").lower()
    asymmetry = str(result.get("asymmetry") or "").lower()
    narrative = str(result.get("narrative") or "").lower()
    combined = f"{headline} {asymmetry} {narrative}"
    if any(k in combined for k in _STRONG_EVIDENCE_KEYWORDS):
        return True
    if any(k in headline for k in _GEO_KEYWORDS):
        return True
    vix = market_summary.get("vix")
    vix_f = float(vix) if vix not in (None, "n/a") else 0.0
    if vix_f >= config.MACRO_VIX_SAFE_HAVEN_MIN and "rising" in str(
        market_summary.get("vix_trend") or ""
    ).lower():
        return True
    if "below MA" in str(market_summary.get("spy_trend") or ""):
        return True
    if float(market_summary.get("oil_change") or 0.0) >= config.MACRO_OIL_SURGE_PCT * 100 * 0.5:
        return True
    if float(market_summary.get("gold_change") or 0.0) >= config.MACRO_GLD_SURGE_PCT * 100:
        return True
    return False


def _rationale_quality_score(result: dict[str, Any]) -> float:
    """0-1 score for TILT_RATIONALE completeness and linkage."""
    rationale = str(result.get("tilt_rationale") or "").strip()
    if len(rationale) < 25:
        return 0.0
    score = 0.35
    asymmetry = str(result.get("asymmetry") or "").lower()
    if asymmetry and (asymmetry[:30] in rationale.lower() or "asymmetry" in rationale.lower()):
        score += 0.2
    tilt = dict(result.get("suggested_tilt") or {})
    material = [(k, v) for k, v in tilt.items() if float(v) >= 0.05]
    if not material:
        return min(1.0, score)
    mentioned = 0
    for key, val in material:
        pct_int = int(round(val * 100))
        pct_dec = f"{val * 100:.1f}".rstrip("0").rstrip(".")
        if (
            key.lower() in rationale.lower()
            and (
                f"{pct_int}%" in rationale
                or f"{pct_dec}%" in rationale
                or f"{val:.0%}" in rationale
                or f"{val:.2f}" in rationale
            )
        ):
            mentioned += 1
    score += 0.45 * (mentioned / max(1, len(material)))
    if len(rationale) >= 80:
        score += 0.05
    return round(min(1.0, score), 2)


def _compute_validation_score(errors: list[str], result: dict[str, Any]) -> int:
    """0-100 validation score for audit trail."""
    base = 100 - 12 * len(errors)
    base = max(0, min(100, base))
    pq = float(result.get("parse_quality") or 0.0)
    rq = _rationale_quality_score(result)
    bonus = int(10 * pq + 10 * rq)
    return max(0, min(100, base + bonus // 2))


def _validate_thinking_quality(
    result: dict[str, Any],
    market_summary: dict,
) -> tuple[bool, list[str]]:
    """Post-process validator — reject contradictory or low-quality tilts."""
    errors: list[str] = []
    tilt = dict(result.get("suggested_tilt") or {})
    narrative = str(result.get("narrative") or "").lower()
    asymmetry = str(result.get("asymmetry") or "").lower()
    conf = float(result.get("confidence") or 0.0)
    gold_chg = float(market_summary.get("gold_change") or 0.0)
    vix_trend = str(market_summary.get("vix_trend") or "").lower()

    if float(tilt.get("gold", 0.0)) > 0.02 and gold_chg < 0.0:
        errors.append("gold overweight while GLD 5d negative")

    risk_on = float(tilt.get("vti", 0.0)) + float(tilt.get("spy", 0.0))
    defensive_narrative = any(
        k in narrative or k in asymmetry
        for k in ("stress", "liquidity", "risk-off", "defensive", "cash preservation")
    )
    if defensive_narrative and risk_on > 0.72 and float(tilt.get("cash", 0.0)) < 0.12:
        errors.append("defensive narrative but equity-heavy tilt")

    if "rising" in vix_trend and risk_on > 0.80 and "continuation" not in asymmetry:
        errors.append("heavy equity tilt into rising VIX without momentum rationale")

    prev = _load_previous_tilt()
    swing_limit = max(0.05, config.effective_thinking_max_sleeve_delta())
    strong_evidence = _strong_new_evidence(market_summary, result)
    if prev:
        for key in _TILT_KEYS:
            delta = abs(float(tilt.get(key, 0.0)) - float(prev.get(key, 0.0)))
            if delta > swing_limit and not strong_evidence:
                errors.append(
                    f"tilt swing on {key} ({delta:.0%} vs prior) without strong new evidence"
                )
            elif delta > 0.35:
                errors.append(f"tilt whipsaw on {key} ({delta:.0%} vs prior)")

    if conf < 0.55:
        errors.append(f"confidence too low ({conf:.2f})")

    if _looks_like_meta_narrative(str(result.get("narrative") or "")):
        errors.append("meta/process narrative")

    rationale = str(result.get("tilt_rationale") or "")
    rq = _rationale_quality_score(result)
    if rq < 0.45:
        errors.append("TILT_RATIONALE missing per-sleeve percentage justification")
    elif result.get("asymmetry"):
        asym_snip = str(result.get("asymmetry"))[:40].lower()
        if asym_snip and asym_snip not in rationale.lower() and "asymmetry" not in rationale.lower():
            errors.append("TILT_RATIONALE not linked to ASYMMETRY")

    return len(errors) == 0, errors


def _update_daily_equity_anchor(equity: float | None) -> None:
    if equity is None or equity <= 0:
        return
    today = datetime.date.today().isoformat()
    state = read_json_file(STATE_FILE)
    if state.get("daily_equity_date") != today:
        state["daily_equity_date"] = today
        state["daily_equity_open"] = round(float(equity), 4)
        write_json_file(STATE_FILE, state)


def thinking_daily_loss_tripped(equity: float | None) -> tuple[bool, str]:
    """True when intraday loss exceeds configured limit."""
    if equity is None or equity <= 0:
        return False, ""
    _update_daily_equity_anchor(equity)
    state = read_json_file(STATE_FILE)
    open_eq = float(state.get("daily_equity_open") or equity)
    if open_eq <= 0:
        return False, ""
    loss_pct = (open_eq - float(equity)) / open_eq
    limit = config.thinking_daily_loss_limit_pct()
    if loss_pct >= limit - 1e-9:
        return True, (
            f"daily loss circuit breaker ({loss_pct:.2%} >= {limit:.2%} limit)"
        )
    return False, ""


def is_thinking_tilt_approved(result: dict) -> bool:
    if not config.thinking_manual_approval_required():
        return True
    decision_id = result.get("decision_id")
    if not decision_id:
        return False
    approval = read_json_file(APPROVAL_FILE)
    return str(approval.get("decision_id")) == str(decision_id)


def _amplify_tilt_for_confidence(
    tilt: dict[str, float],
    confidence: float,
    *,
    rationale_quality: float = 0.0,
) -> dict[str, float]:
    """Sharpen allocation when PM conviction and rationale quality are high (>=0.80, >=0.65)."""
    if not config.THINKING_CONFIDENCE_AMPLIFY_ENABLED:
        return tilt
    conf = float(confidence)
    if conf < 0.80 or float(rationale_quality) < 0.65:
        return tilt
    strength = min(1.0, (conf - 0.80) / 0.20)
    ranked = sorted(tilt.items(), key=lambda kv: kv[1], reverse=True)
    out = dict(tilt)
    boost = 0.03 + 0.05 * strength
    for key, _weight in ranked[:2]:
        out[key] = out.get(key, 0.0) + boost * 0.55
    cut_keys = ("cash", "vti") if ranked and ranked[0][0] != "vti" else ("cash",)
    for key in cut_keys:
        if key in out:
            out[key] = max(0.02, out[key] - boost * 0.45)
    return _normalize_tilt(out)


def _finalize_thinking_result(
    result: dict[str, Any],
    market_summary: dict,
    *,
    force_decision: bool = False,
) -> dict[str, Any]:
    """Ensure decisive tilt/narrative; amplify when confidence is high."""
    out = dict(result)
    conf = float(out.get("confidence") or 0.65)
    tilt = dict(out.get("suggested_tilt") or {})
    heuristic = derive_heuristic_tilt(market_summary)

    if force_decision or _is_default_tilt(tilt) or not tilt:
        for key, val in heuristic.items():
            if tilt.get(key, 0) <= 0 and val > 0:
                tilt[key] = val
        if _is_default_tilt(tilt):
            tilt = heuristic
        conf = max(conf, 0.72)
        out["source"] = out.get("source") or "force_decision"

    if not out.get("narrative") or _looks_like_meta_narrative(str(out.get("narrative"))):
        out["narrative"] = _infer_narrative(market_summary)
    if not out.get("asymmetry"):
        out["asymmetry"] = _infer_asymmetry(market_summary)
    if force_decision and not out.get("risks"):
        out["risks"] = ["Vol spike / trend break", "Macro headline shock"]
    if force_decision and not out.get("opportunities"):
        out["opportunities"] = ["Regime sleeve tilt", "Asymmetric hedge sleeve"]

    tilt = _clamp_gold_in_tilt(
        market_summary,
        _normalize_tilt(tilt),
        str(out.get("asymmetry") or ""),
    )
    rationale = str(out.get("tilt_rationale") or "").strip()
    if not rationale or len(rationale) < 20:
        out["tilt_rationale"] = _infer_tilt_rationale(
            market_summary, tilt, str(out.get("asymmetry") or "")
        )
    elif out.get("asymmetry") and str(out.get("asymmetry")).lower() not in rationale.lower():
        out["tilt_rationale"] = _infer_tilt_rationale(
            market_summary, tilt, str(out.get("asymmetry") or "")
        )
    elif not any(
        f"{v:.0%}" in rationale or f"{int(round(v * 100))}%" in rationale
        for v in tilt.values()
        if v > 0.05
    ):
        out["tilt_rationale"] = _infer_tilt_rationale(
            market_summary, tilt, str(out.get("asymmetry") or "")
        )
    rq = _rationale_quality_score(out)
    out["rationale_quality"] = rq
    tilt = _amplify_tilt_for_confidence(tilt, conf, rationale_quality=rq)
    out["suggested_tilt"] = tilt
    out["confidence"] = round(max(0.35, min(1.0, conf)), 2)
    out["regime_narrative"] = build_regime_narrative(out)
    regime = str(market_summary.get("regime") or "")
    out["decision_id"] = _thinking_decision_id(regime, tilt)
    valid, val_errors = _validate_thinking_quality(out, market_summary)
    out["validation_ok"] = valid
    out["validation_errors"] = val_errors
    out["validation_score"] = _compute_validation_score(val_errors, out)
    if not valid:
        logger.info("Thinking validation failed: %s", "; ".join(val_errors))
        safe_tilt = derive_heuristic_tilt(market_summary)
        out["suggested_tilt"] = _clamp_gold_in_tilt(
            market_summary, safe_tilt, str(out.get("asymmetry") or "")
        )
        out["validation_recovered"] = True
        out["source"] = (out.get("source") or "llm") + "+validator_fallback"
        out["decision_id"] = _thinking_decision_id(regime, out["suggested_tilt"])
        out["validation_score"] = _compute_validation_score([], out)
    persist_thinking_last(out, regime=regime or None)
    return out


def _infer_narrative(summary: dict | None) -> str:
    if not summary:
        return "Macro reassessment"
    headline = str(summary.get("top_headline", "")).lower()
    oil = float(summary.get("oil_change") or 0.0)
    gold = float(summary.get("gold_change") or 0.0)
    vix = summary.get("vix")
    vix_f = float(vix) if vix not in (None, "n/a") else 0.0
    if any(k in headline for k in _GEO_KEYWORDS):
        return "Geopolitical tension / Middle East risk"
    if oil >= config.MACRO_OIL_SURGE_PCT * 100 * 0.5:
        return "Oil shock / energy stress"
    if gold >= config.MACRO_GLD_SURGE_PCT * 100:
        return "Risk-off / safe-haven bid"
    if vix_f >= config.MACRO_VIX_SAFE_HAVEN_MIN:
        if gold < 0.0:
            return "Elevated vol with gold falling — liquidity stress"
        return "Risk-off / safe-haven bid"
    if "below MA" in str(summary.get("spy_trend", "")):
        return "Equity trend weakening"
    return "Range-bound macro"


def _format_thinking_log(narrative: str, cap_deltas: dict[str, float]) -> str:
    parts: list[str] = []
    for key in _CAP_KEYS:
        delta = cap_deltas.get(key, 0.0)
        if abs(delta) < 0.005:
            continue
        label = _CAP_LABELS.get(key, key)
        parts.append(f"{delta * 100:+.0f}% {label}")
    if not parts:
        return f"Thinking Engine: {narrative} -> no material cap change"
    return f"Thinking Engine: {narrative} -> {', '.join(parts)}"


def apply_thinking_tilt_to_caps(
    base_caps: dict[str, float],
    suggested_tilt: dict[str, float],
    *,
    confidence: float = 0.7,
    market_summary: dict | None = None,
    equity: float | None = None,
    max_sleeve_delta: float | None = None,
    allow_small_account: bool = False,
) -> tuple[dict[str, float], dict[str, float], str]:
    """Apply LLM/heuristic tilt to sleeve caps (±max_sleeve_delta per sleeve)."""
    if (
        equity is not None
        and equity < config.SMALL_ACCOUNT_EQUITY_THRESHOLD
        and not allow_small_account
    ):
        return dict(base_caps), {}, ""

    base = {k: float(base_caps.get(k, 0.0)) for k in _CAP_KEYS}
    conf = max(0.35, min(1.0, float(confidence)))
    cap_deltas = compute_cap_deltas(
        base,
        suggested_tilt,
        confidence=conf,
        market_summary=market_summary,
        max_sleeve_delta=(
            max_sleeve_delta
            if max_sleeve_delta is not None
            else config.effective_thinking_max_sleeve_delta()
        ),
    )

    merged = dict(base)
    for key, delta in cap_deltas.items():
        merged[key] = round(max(0.0, merged.get(key, 0.0) + delta), 6)

    non_cash = sum(merged[k] for k in _CAP_KEYS if k != "cash_buffer")
    merged["cash_buffer"] = round(max(0.0, 1.0 - non_cash), 6)
    if merged["cash_buffer"] < 0:
        merged["cash_buffer"] = 0.0
        scale = 1.0 / max(non_cash, 1e-9)
        for key in _CAP_KEYS:
            if key != "cash_buffer":
                merged[key] = round(merged[key] * scale, 6)
        merged["cash_buffer"] = round(
            1.0 - sum(merged[k] for k in _CAP_KEYS if k != "cash_buffer"), 6
        )

    actual_deltas = {
        k: round(merged[k] - base.get(k, 0.0), 6) for k in _CAP_KEYS
    }
    log_line = _format_thinking_log(_infer_narrative(market_summary), actual_deltas)
    if any(v != 0 for v in actual_deltas.values()):
        log_event("thinking_tilt_applied", confidence=conf, deltas=actual_deltas, log_line=log_line)
    return merged, actual_deltas, log_line


def apply_thinking_to_sleeve_caps(
    base_caps: dict[str, float],
    thinking_result: dict,
    *,
    equity: float | None = None,
) -> tuple[dict[str, float], dict[str, float], str]:
    """Apply reasoning result to sleeve caps; returns merged caps, deltas, log line."""
    return apply_thinking_tilt_to_caps(
        base_caps,
        thinking_result.get("suggested_tilt") or {},
        confidence=float(thinking_result.get("confidence", 0.65)),
        market_summary=thinking_result.get("market_summary"),
        equity=equity,
    )


def derive_heuristic_tilt(summary: dict) -> dict[str, float]:
    """Normalized target weights for LLM nudge layer (backtest/live)."""
    tilt = {
        "vti": 0.55,
        "spy": 0.12,
        "crypto": 0.08,
        "energy": 0.05,
        "gold": 0.05 if _gold_momentum_ok(summary) else 0.0,
        "cash": 0.10,
        "bonds": 0.05 if _gold_momentum_ok(summary) else 0.10,
    }
    deltas = _rule_based_cap_deltas(summary, 0.7)
    if deltas.get("nyse", 0) > 0.03:
        tilt["energy"] += 0.06
        tilt["spy"] -= 0.04
    if deltas.get("metal", 0) > 0.02:
        tilt["gold"] += 0.05
        tilt["spy"] -= 0.03
    if deltas.get("vti_core", 0) > 0.02:
        tilt["vti"] += 0.04
    return _normalize_tilt(tilt)


def build_backtest_thinking_result(
    data,
    regime: str,
    vol: str,
    *,
    force_decision: bool = True,
) -> dict:
    """Decisive thinking proxy for historical backtests (always produces a tilt)."""
    summary = build_market_summary(data, regime, vol)
    tilt = derive_heuristic_tilt(summary)
    narrative = _infer_narrative(summary)
    asymmetry = _infer_asymmetry(summary)
    conf = 0.78 if force_decision else 0.70
    base = {
        "reasoning": f"Force-decision proxy: {narrative}",
        "narrative": narrative,
        "asymmetry": asymmetry,
        "risks": ["Regime shift", "Vol spike"],
        "opportunities": ["Sleeve tilt edge", "Macro hedge"],
        "justification": asymmetry,
        "suggested_tilt": tilt,
        "confidence": conf,
        "model": "heuristic-backtest",
        "source": "force_decision",
        "market_summary": summary,
        "tilt_rationale": _infer_tilt_rationale(summary, tilt, asymmetry),
    }
    return _finalize_thinking_result(base, summary, force_decision=force_decision)


def executor_scales_from_caps(
    base_caps: dict[str, float],
    merged_caps: dict[str, float],
) -> dict[str, float]:
    """Convert absolute cap deltas to multipliers for BacktestExecutor."""
    scales: dict[str, float] = {}
    for sleeve, key in (("spy", "spy"), ("nyse", "nyse"), ("crypto", "crypto")):
        base = float(base_caps.get(key, 0.0))
        merged = float(merged_caps.get(key, base))
        scales[sleeve] = round(merged / base, 6) if base > 1e-9 else 1.0
    return scales


def should_refresh_thinking(regime: str) -> bool:
    """Refresh after THINKING_CACHE_HOURS or on regime change."""
    state = read_json_file(STATE_FILE)
    last_regime = state.get("last_regime")
    if last_regime != regime:
        return True
    last_run_at = state.get("last_run_at")
    if not last_run_at:
        return True
    try:
        last = datetime.datetime.fromisoformat(str(last_run_at))
        age_h = (datetime.datetime.now() - last).total_seconds() / 3600.0
        return age_h >= float(config.THINKING_CACHE_HOURS)
    except (TypeError, ValueError):
        return True


def build_heuristic_reasoning_result(
    market_summary: dict,
    *,
    reason: str = "heuristic-fallback",
) -> dict[str, Any]:
    """Rule-based tilt when Ollama is unavailable or all LLM attempts fail."""
    base = {
        "reasoning": f"Rule-based fallback ({reason}): {_infer_narrative(market_summary)}",
        "narrative": _infer_narrative(market_summary),
        "asymmetry": _infer_asymmetry(market_summary),
        "risks": [],
        "opportunities": [],
        "justification": reason,
        "suggested_tilt": derive_heuristic_tilt(market_summary),
        "confidence": 0.70,
        "model": reason,
        "source": "heuristic",
        "parse_quality": 0.0,
        "market_summary": market_summary,
    }
    return _finalize_thinking_result(base, market_summary, force_decision=True)


def _record_thinking_run(regime: str, result: dict) -> None:
    now = datetime.datetime.now().isoformat()
    write_json_file(
        STATE_FILE,
        {
            "last_date": datetime.date.today().isoformat(),
            "last_regime": regime,
            "last_run_at": now,
            "model": result.get("model", config.OLLAMA_MODEL),
            "source": result.get("source", "llm"),
        },
    )
    persist_thinking_last(result, regime=regime)


def ollama_available() -> bool:
    url = f"{config.OLLAMA_HOST.rstrip('/')}/api/tags"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def ollama_installed_models() -> set[str]:
    """Return model names reported by Ollama /api/tags."""
    url = f"{config.OLLAMA_HOST.rstrip('/')}/api/tags"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return set()
    names: set[str] = set()
    for item in body.get("models") or []:
        name = str(item.get("name") or "").strip()
        if name:
            names.add(name)
    return names


def _model_available(model: str, installed: set[str]) -> bool:
    if not installed:
        return True
    if model in installed:
        return True
    if ":" not in model:
        return any(n.startswith(f"{model}:") for n in installed)
    return False


def thinking_model_chain(*, fast_only: bool = False) -> list[str]:
    """Primary deepseek-r1:8b, then fast fallbacks (llama3.2:3b, deepseek-r1:1.5b)."""
    fallbacks = [
        m.strip()
        for m in config.OLLAMA_FALLBACK_MODELS.split(",")
        if m.strip()
    ]
    if fast_only:
        return fallbacks or [config.OLLAMA_MODEL]
    chain = [config.OLLAMA_MODEL]
    for model in fallbacks:
        if model not in chain:
            chain.append(model)
    return chain


def _is_fast_model(model: str) -> bool:
    low = model.lower()
    return any(tag in low for tag in ("1.5b", "3b", "llama3.2"))


def _model_num_predict(model: str) -> int:
    return 450 if _is_fast_model(model) else 900


def _ollama_generate(
    prompt: str,
    *,
    system: str | None = None,
    model: str | None = None,
    timeout_sec: int | None = None,
) -> tuple[str, str]:
    """Return (full_text_for_logs, answer_text_for_parsing)."""
    model = model or config.OLLAMA_MODEL
    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": _model_num_predict(model)},
    }
    if system:
        body["system"] = system
    payload = json.dumps(body).encode("utf-8")
    url = f"{config.OLLAMA_HOST.rstrip('/')}/api/generate"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    timeout = timeout_sec if timeout_sec is not None else config.OLLAMA_TIMEOUT_SEC
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    answer = str(body.get("response") or "").strip()
    thinking = str(body.get("thinking") or "").strip()
    if answer and thinking:
        full = f"{thinking}\n\n---\n{answer}"
        if re.search(r"NARRATIVE\s*:", answer, re.I):
            parse_text = answer
        elif re.search(r"NARRATIVE\s*:", thinking, re.I):
            parse_text = thinking
        else:
            parse_text = f"{thinking}\n{answer}" if thinking else answer
    elif answer:
        full = answer
        parse_text = answer
    elif thinking:
        full = thinking
        parse_text = thinking
    else:
        raise RuntimeError("Ollama returned empty response")
    return full, parse_text


def _extract_json_block(text: str) -> dict | None:
    fenced = re.search(r"```(?:json)?\s*(\{)", text, re.S | re.I)
    if fenced:
        span = _find_balanced_brace(text, fenced.start(1))
        if span:
            obj = _coerce_tilt_json(text[span[0] : span[1] + 1])
            if isinstance(obj, dict):
                return obj
    candidate = None
    idx = 0
    while idx < len(text):
        span = _find_balanced_brace(text, idx)
        if not span:
            break
        obj = _coerce_tilt_json(text[span[0] : span[1] + 1])
        if not isinstance(obj, dict):
            idx = span[1] + 1
            continue
        if "suggested_tilt" in obj:
            return obj
        if any(str(k).lower() in _TILT_KEYS for k in obj):
            return {"suggested_tilt": obj}
        if candidate is None:
            candidate = obj
        idx = span[1] + 1
    return candidate


def _normalize_tilt(raw: dict | None) -> dict[str, float]:
    base = {k: 0.0 for k in _TILT_KEYS}
    if not raw:
        base.update({"vti": 0.80, "cash": 0.20})
        return base
    alias = {
        "vti_core": "vti",
        "xle": "energy",
        "gld": "gold",
        "treasury": "bonds",
        "tlt": "bonds",
    }
    for key, val in raw.items():
        k = alias.get(str(key).lower(), str(key).lower())
        if k not in base:
            continue
        try:
            val = float(val)
            if val > 1.0 and val <= 100.0:
                val = val / 100.0
            base[k] = val
        except (TypeError, ValueError):
            continue
    total = sum(v for v in base.values() if v > 0)
    if total <= 0:
        base.update({"vti": 0.80, "cash": 0.20})
        return base
    return {k: round(v / total, 4) for k, v in base.items() if v > 0}


def _parse_list_value(raw: str) -> list[str]:
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            val = json.loads(raw)
            if isinstance(val, list):
                return [str(x).strip() for x in val if str(x).strip()]
        except json.JSONDecodeError:
            inner = raw.strip("[]")
            return [x.strip().strip('"\'') for x in inner.split(",") if x.strip()]
    sep = ";" if ";" in raw and "," not in raw else ","
    if sep in raw:
        return [x.strip().strip("-•*") for x in raw.split(sep) if x.strip()]
    lines = [ln.strip().strip("-•*") for ln in raw.splitlines() if ln.strip()]
    if lines:
        return lines
    return [raw]


def _find_balanced_brace(text: str, start: int = 0) -> tuple[int, int] | None:
    """Return (open_idx, close_idx) for a balanced {...} object."""
    open_idx = text.find("{", start)
    if open_idx < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    quote = ""
    for i in range(open_idx, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                in_string = False
            continue
        if ch in ('"', "'"):
            in_string = True
            quote = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return open_idx, i
    return None


def _coerce_tilt_json(raw: str) -> dict | None:
    """Parse tilt dict from LLM output; tolerate quotes, trailing commas, pct values."""
    cleaned = raw.strip()
    if not cleaned:
        return None
    cleaned = cleaned.replace("'", '"')
    cleaned = re.sub(r",\s*}", "}", cleaned)
    cleaned = re.sub(r",\s*]", "]", cleaned)
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    quoted = re.sub(r"([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:", r'\1"\2":', cleaned)
    try:
        obj = json.loads(quoted)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    return None


def _parse_tilt_value(raw: str) -> dict | None:
    raw = raw.strip()
    span = _find_balanced_brace(raw)
    if span:
        obj = _coerce_tilt_json(raw[span[0] : span[1] + 1])
        if obj:
            return obj
    return None


def _recover_partial_tilt(text: str) -> dict | None:
    """Best-effort parse when RECOMMENDED_TILT JSON is truncated mid-stream."""
    match = re.search(r"RECOMMENDED_TILT\s*:\s*\{([^}\n]*)", text, re.I)
    if not match:
        return None
    pairs = re.findall(r'"?([a-zA-Z_][a-zA-Z0-9_]*)"?\s*:\s*([\d.]+)', match.group(1))
    if not pairs:
        return None
    raw: dict[str, float] = {}
    for key, val in pairs:
        try:
            raw[key.lower()] = float(val)
        except ValueError:
            continue
    return raw if raw else None


def _extract_recommended_tilt(text: str) -> dict | None:
    """Extract RECOMMENDED_TILT dict from full LLM response (multi-strategy)."""
    for match in re.finditer(r"RECOMMENDED_TILT\s*:", text, re.I):
        span = _find_balanced_brace(text, match.end())
        if span:
            obj = _coerce_tilt_json(text[span[0] : span[1] + 1])
            if obj and any(
                str(k).lower() in _TILT_KEYS or str(k).lower() in ("vti_core", "xle", "gld")
                for k in obj
            ):
                return obj
    for match in re.finditer(r"suggested_tilt\s*:", text, re.I):
        span = _find_balanced_brace(text, match.end())
        if span:
            obj = _coerce_tilt_json(text[span[0] : span[1] + 1])
            if obj:
                return obj
    best: dict | None = None
    idx = 0
    while idx < len(text):
        span = _find_balanced_brace(text, idx)
        if not span:
            break
        obj = _coerce_tilt_json(text[span[0] : span[1] + 1])
        if obj:
            if "suggested_tilt" in obj and isinstance(obj["suggested_tilt"], dict):
                return obj["suggested_tilt"]
            if any(str(k).lower() in _TILT_KEYS for k in obj):
                best = obj
        idx = span[1] + 1
    return best or _recover_partial_tilt(text)


def _parse_confidence_value(raw: str) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    match = re.search(r"(\d+\.?\d*)", raw)
    if match:
        try:
            val = float(match.group(1))
            return val / 100.0 if val > 1.0 else val
        except ValueError:
            return None
    return None


def _parse_structured_reasoning(text: str) -> dict[str, Any]:
    """Parse NARRATIVE/ASYMMETRY/RISKS/OPPORTUNITIES/RECOMMENDED_TILT/CONFIDENCE/REASONING."""
    result: dict[str, Any] = {}
    current_field: str | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal current_field, buf
        if not current_field:
            return
        raw = "\n".join(buf).strip()
        key = current_field.lower()
        if key == "narrative":
            result["narrative"] = raw.splitlines()[0] if raw else ""
        elif key == "asymmetry":
            result["asymmetry"] = raw.splitlines()[0] if raw else ""
        elif key == "paradigm_shift":
            result["paradigm_shift"] = raw.splitlines()[0] if raw else ""
        elif key == "risks":
            result["risks"] = _parse_list_value(raw)
        elif key == "opportunities":
            result["opportunities"] = _parse_list_value(raw)
        elif key == "recommended_tilt":
            tilt = _parse_tilt_value(raw) or _extract_recommended_tilt(raw)
            if tilt:
                result["suggested_tilt"] = tilt
        elif key == "tilt_rationale":
            result["tilt_rationale"] = raw.splitlines()[0] if raw else ""
        elif key == "tilt":
            tilt = _parse_tilt_value(raw) or _extract_recommended_tilt(raw)
            if tilt:
                result["suggested_tilt"] = tilt
        elif key == "confidence":
            conf = _parse_confidence_value(raw.splitlines()[0] if raw else "")
            if conf is not None:
                result["confidence"] = conf
        elif key == "reasoning":
            result["reasoning_excerpt"] = raw
        current_field = None
        buf = []

    for line in text.splitlines():
        stripped = line.strip()
        stripped = re.sub(r"^\*\*(.+?)\*\*\s*[:=\-]?\s*", r"\1: ", stripped)
        stripped = re.sub(r"^[-*]\s+", "", stripped)
        match = _STRUCTURED_FIELD_RE.match(stripped)
        if match:
            flush()
            current_field = match.group(1).upper()
            rest = match.group(2).strip()
            buf = [rest] if rest else []
        elif current_field:
            buf.append(line)
    flush()
    return result


def _structured_parse_candidates(full_text: str, answer_text: str) -> list[str]:
    """Prefer the final structured block (often after deepseek-r1 '---' separator)."""
    candidates: list[str] = []
    if "---" in full_text:
        candidates.append(full_text.rsplit("---", 1)[-1])
    if answer_text.strip():
        candidates.append(answer_text)
    candidates.append(full_text)
    return candidates


def _best_structured_parse(full_text: str, answer_text: str) -> dict[str, Any]:
    best: dict[str, Any] = {}
    for chunk in _structured_parse_candidates(full_text, answer_text):
        parsed = _parse_structured_reasoning(chunk)
        if not parsed:
            continue
        if len(parsed) > len(best):
            best = parsed
        if parsed.get("suggested_tilt") and parsed.get("confidence") is not None:
            return parsed
    return best


def _strip_chain_of_thought(text: str) -> str:
    """Drop deepseek-r1 preamble; keep structured block when present."""
    if not text:
        return text
    if "---" in text:
        tail = text.rsplit("---", 1)[-1].strip()
        if re.search(r"NARRATIVE\s*:", tail, re.I):
            return tail
    match = re.search(r"(NARRATIVE\s*[:=\-].*)", text, re.I | re.S)
    if match and _looks_like_meta_narrative(text[: min(300, len(text))]):
        return match.group(1).strip()
    return text


def _looks_like_meta_narrative(text: str) -> bool:
    low = text.lower().strip()
    if len(text) > 220:
        return True
    return (
        low.startswith("first,")
        or "i need to" in low
        or "user provided" in low
        or "this user is" in low
        or "the user is" in low
    )


def _build_reasoning_user_prompt(market_summary: dict) -> str:
    prev = _load_previous_tilt()
    prev_full = _load_previous_tilt_full()
    prev_line = "n/a (first run)"
    prev_context = ""
    if prev:
        prev_line = ", ".join(
            f"{k} {v:.0%}" for k, v in sorted(prev.items(), key=lambda kv: kv[1], reverse=True)[:6]
        )
    if prev_full and prev_full.get("tilt"):
        prev_rationale = str(prev_full.get("tilt_rationale") or "")[:220]
        prev_context = (
            f"Previous decision ({str(prev_full.get('timestamp', 'unknown'))[:19]}):\n"
            f"  regime={prev_full.get('regime')} | narrative={str(prev_full.get('narrative') or '')[:120]}\n"
            f"  tilt={prev_line}\n"
            f"  prior TILT_RATIONALE: {prev_rationale or 'n/a'}"
        )

    gold_chg = float(market_summary.get("gold_change") or 0.0)
    gold_note = (
        "Gold 5d is NEGATIVE — default gold to 0% in RECOMMENDED_TILT unless asymmetry explicitly cites a contrarian gold bounce."
        if gold_chg < 0
        else f"Gold 5d: {gold_chg}% (safe-haven bid OK only if positive or flat)."
    )
    vix_trend = str(market_summary.get("vix_trend") or "n/a")
    vix_note = (
        "VIX is RISING — bias toward cash unless ASYMMETRY cites strong momentum continuation."
        if "rising" in vix_trend.lower()
        else f"VIX trend: {vix_trend}"
    )

    return f"""Current market snapshot:

SPY vs MA200: {market_summary['spy_trend']}
VIX: {market_summary['vix']} | {vix_note}
Oil 5d: {market_summary['oil_change']}% | Gold 5d: {market_summary['gold_change']}%
Yield curve / rates: {market_summary.get('yield_curve', 'n/a')}
Regime: {market_summary.get('regime', 'unknown')}
Macro sentiment: {market_summary['macro_sentiment']}
Top headline: {market_summary['top_headline']}
Bot exposure: {market_summary.get('bot_exposure_str', 'n/a')}

Previous day tilt: {prev_line}
{prev_context}

{gold_note}
Maintain consistency with previous day unless strong new evidence (VIX spike, trend break, headline).
Maximum sleeve change vs prior day is ±{config.effective_thinking_max_sleeve_delta():.0%} per sleeve without new evidence.
TILT_RATIONALE must justify every sleeve above 5% with its exact percentage.
Reply with ONLY the structured block (NARRATIVE through REASONING). Be decisive. Start with NARRATIVE:"""


def _is_default_tilt(tilt: dict[str, float]) -> bool:
    return (
        abs(float(tilt.get("vti", 0.0)) - 0.8) < 0.02
        and abs(float(tilt.get("cash", 0.0)) - 0.2) < 0.02
        and sum(float(tilt.get(k, 0.0)) for k in ("spy", "energy", "gold", "crypto", "bonds")) < 0.02
    )


def _llm_parse_quality(
    structured: dict[str, Any],
    tilt: dict[str, float],
    *,
    had_conf_field: bool,
) -> float:
    score = 0.0
    narrative = str(structured.get("narrative") or "")
    if narrative and not _looks_like_meta_narrative(narrative):
        score += 0.25
    if structured.get("asymmetry"):
        score += 0.15
    if structured.get("tilt_rationale"):
        score += 0.1
    if structured.get("risks"):
        score += 0.1
    if structured.get("opportunities"):
        score += 0.1
    if structured.get("suggested_tilt") or not _is_default_tilt(tilt):
        score += 0.3
    if had_conf_field:
        score += 0.1
    return round(min(1.0, score), 2)


def _get_market_reasoning_with_model(market_summary: dict, model: str) -> dict[str, Any]:
    """Single Ollama attempt; raises on transport/empty response."""
    user_prompt = _build_reasoning_user_prompt(market_summary)
    full_text, answer_text = _ollama_generate(user_prompt, system=_PM_SYSTEM_PROMPT, model=model)
    structured = _best_structured_parse(full_text, answer_text)
    parse_chunks = _structured_parse_candidates(full_text, answer_text)
    for chunk in parse_chunks:
        fb = _parse_fallback_fields(chunk)
        for key, val in fb.items():
            if key not in structured or not structured.get(key):
                structured[key] = val
    parsed: dict = {}
    for chunk in parse_chunks:
        parsed = _extract_json_block(chunk) or parsed

    had_conf_field = "confidence" in structured or "confidence" in parsed
    confidence = structured.get("confidence", parsed.get("confidence"))
    try:
        confidence = 0.0 if confidence is None else max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.65
    if confidence < 0.05 and not had_conf_field:
        confidence = 0.65

    raw_tilt = (
        structured.get("suggested_tilt")
        or next(
            (t for c in parse_chunks if (t := _extract_recommended_tilt(c))),
            None,
        )
        or next(
            (t for c in parse_chunks if (t := _extract_tilt_from_prose(c))),
            None,
        )
        or parsed.get("suggested_tilt")
        or parsed.get("recommended_tilt")
        or parsed.get("tilt")
        or parsed
    )
    tilt = _normalize_tilt(raw_tilt if isinstance(raw_tilt, dict) else None)

    narrative = structured.get("narrative") or parsed.get("narrative") or ""
    risks = structured.get("risks") or parsed.get("risks") or []
    opportunities = (
        structured.get("opportunities")
        or parsed.get("opportunities")
        or parsed.get("opps")
        or []
    )
    asymmetry = structured.get("asymmetry") or parsed.get("asymmetry") or ""
    tilt_rationale = structured.get("tilt_rationale") or parsed.get("tilt_rationale") or ""
    justification = (
        structured.get("reasoning_excerpt")
        or parsed.get("justification")
        or parsed.get("reasoning")
        or ""
    )
    quality = _llm_parse_quality(structured, tilt, had_conf_field=had_conf_field)

    result = {
        "reasoning": _strip_chain_of_thought(full_text.strip()),
        "narrative": str(narrative).strip(),
        "asymmetry": str(asymmetry).strip() if asymmetry else "",
        "tilt_rationale": str(tilt_rationale).strip() if tilt_rationale else "",
        "risks": risks if isinstance(risks, list) else [str(risks)],
        "opportunities": (
            opportunities if isinstance(opportunities, list)
            else [str(opportunities)] if opportunities else []
        ),
        "justification": str(justification).strip(),
        "suggested_tilt": tilt,
        "confidence": round(confidence, 2),
        "model": model,
        "source": "llm",
        "parse_quality": quality,
        "market_summary": market_summary,
    }
    result = _finalize_thinking_result(result, market_summary, force_decision=False)
    persist_thinking_last(result, regime=market_summary.get("regime"))
    return result


def get_market_reasoning(
    market_summary: dict,
    *,
    fast_model: bool = False,
    model: str | None = None,
) -> dict[str, Any]:
    """Use local LLM with primary -> fallback chain; heuristic if all fail."""
    installed = ollama_installed_models()
    chain = thinking_model_chain(fast_only=fast_model)
    if model:
        candidates = [model]
    else:
        candidates = [m for m in chain if _model_available(m, installed)]
        if not candidates:
            if fast_model:
                result = build_heuristic_reasoning_result(
                    market_summary,
                    reason="fast-models-not-installed",
                )
                persist_thinking_last(result, regime=market_summary.get("regime"))
                return result
            candidates = chain

    errors: list[str] = []
    best: dict[str, Any] | None = None
    for idx, candidate in enumerate(candidates):
        try:
            result = _get_market_reasoning_with_model(market_summary, candidate)
        except (urllib.error.URLError, TimeoutError, OSError, RuntimeError, json.JSONDecodeError) as exc:
            errors.append(f"{candidate}: {exc}")
            continue
        quality = float(result.get("parse_quality") or 0.0)
        if best is None or quality > float(best.get("parse_quality") or 0.0):
            best = result
        if quality >= 0.45:
            persist_thinking_last(result, regime=market_summary.get("regime"))
            return result
        if idx < len(candidates) - 1:
            continue
        persist_thinking_last(result, regime=market_summary.get("regime"))
        return result

    if best is not None:
        persist_thinking_last(best, regime=market_summary.get("regime"))
        return best

    reason = "; ".join(errors) if errors else "no-models"
    result = build_heuristic_reasoning_result(market_summary, reason=reason)
    persist_thinking_last(result, regime=market_summary.get("regime"))
    return result


def _extract_tilt_from_prose(text: str) -> dict | None:
    """Fallback: parse 'vti 55%' / 'SPY: 0.12' style allocations from prose."""
    found: dict[str, float] = {}
    for match in _TILT_PROSE_RE.finditer(text):
        key = str(match.group(1)).lower()
        val = float(match.group(2))
        if val > 1.0 and val <= 100.0:
            val /= 100.0
        found[key] = val
    return found if len(found) >= 2 else None


def _parse_fallback_fields(text: str) -> dict[str, Any]:
    """Looser field extraction when strict block parsing fails."""
    out: dict[str, Any] = {}
    for label, key in (
        (r"NARRATIVE", "narrative"),
        (r"ASYMMETRY", "asymmetry"),
        (r"TILT_RATIONALE", "tilt_rationale"),
        (r"CONFIDENCE", "confidence"),
        (r"REASONING", "reasoning_excerpt"),
    ):
        m = re.search(
            rf"(?:#+\s*)?{label}\s*[:=\-]\s*(.+?)(?=(?:#+\s*)?(?:NARRATIVE|ASYMMETRY|RISKS|OPPORTUNITIES|RECOMMENDED_TILT|TILT|CONFIDENCE|REASONING)\s*[:=\-]|$)",
            text,
            re.I | re.S,
        )
        if not m:
            m = re.search(rf"{label}\s*[:=\-]\s*(.+)", text, re.I)
        if m:
            val = m.group(1).strip().splitlines()[0]
            if key == "confidence":
                conf = _parse_confidence_value(val)
                if conf is not None:
                    out[key] = conf
            else:
                out[key] = val
    risks_m = re.search(r"RISKS\s*:\s*(.+?)(?:OPPORTUNITIES|RECOMMENDED_TILT|TILT|CONFIDENCE|$)", text, re.I | re.S)
    if risks_m:
        out["risks"] = _parse_list_value(risks_m.group(1))
    opps_m = re.search(r"OPPORTUNITIES\s*:\s*(.+?)(?:RECOMMENDED_TILT|TILT|CONFIDENCE|REASONING|$)", text, re.I | re.S)
    if opps_m:
        out["opportunities"] = _parse_list_value(opps_m.group(1))
    tilt = _extract_recommended_tilt(text) or _extract_tilt_from_prose(text)
    if tilt:
        out["suggested_tilt"] = tilt
    return out


def maybe_run_thinking(
    data,
    regime: str,
    vol: str,
    wisdom: dict | None = None,
    *,
    top_headline: str | None = None,
    force: bool = False,
) -> dict | None:
    """Paper-only hook: run LLM reasoning at most once per THINKING_CACHE_HOURS or on regime change."""
    if not config.effective_thinking_engine_enabled():
        return None
    if not force and not should_refresh_thinking(regime):
        cached = read_json_file(OUTPUT_FILE)
        if cached:
            return cached
        return None
    summary = build_market_summary(
        data, regime, vol, wisdom=wisdom, top_headline=top_headline
    )
    if not ollama_available():
        logger.info("Thinking engine: Ollama not reachable, using rule-based tilt")
        result = build_heuristic_reasoning_result(summary, reason="ollama-unreachable")
        _record_thinking_run(regime, result)
        return result
    # If caller asked for a synchronous forced run, do it now.
    if force:
        try:
            result = get_market_reasoning(summary)
        except Exception:
            logger.exception("Thinking engine: LLM failed during forced run; falling back to heuristic")
            result = build_heuristic_reasoning_result(summary, reason="llm-error-forced")
        if result.get("source") == "heuristic":
            logger.info("Thinking engine: heuristic fallback (%s)", result.get("model"))
        elif float(result.get("parse_quality") or 0.0) < 0.45:
            logger.info(
                "Thinking engine: low parse quality (%s), using best-effort LLM output",
                result.get("parse_quality"),
            )
        _record_thinking_run(regime, result)
        return result

    # Non-forced path: refresh in background to avoid blocking main loop.
    def _bg_refresh():
        try:
            logger.info("Thinking engine: background refresh started for regime=%s", regime)
            res = get_market_reasoning(summary)
            if res.get("source") == "heuristic":
                logger.info("Thinking engine background: heuristic fallback (%s)", res.get("model"))
            elif float(res.get("parse_quality") or 0.0) < 0.45:
                logger.info("Thinking engine background: low parse quality: %s", res.get("parse_quality"))
            _record_thinking_run(regime, res)
            logger.info("Thinking engine: background refresh completed for regime=%s", regime)
        except Exception:
            logger.exception("Thinking engine background refresh failed")

    t = threading.Thread(target=_bg_refresh, daemon=True)
    t.start()

    # Return cached output if available, else a heuristic immediate result.
    cached = read_json_file(OUTPUT_FILE)
    if cached:
        return cached
    result = build_heuristic_reasoning_result(summary, reason="background-refresh-started")
    _record_thinking_run(regime, result)
    return result


def log_thinking_result(thinking_result: dict) -> None:
    reasoning = thinking_result.get("reasoning", "")
    narrative = thinking_result.get("narrative") or (reasoning.splitlines()[0] if reasoning else "")
    tilt = thinking_result.get("suggested_tilt") or {}
    top_tilts = sorted(tilt.items(), key=lambda kv: kv[1], reverse=True)[:3]
    tilt_s = ", ".join(f"{k} {v:.0%}" for k, v in top_tilts)
    model = thinking_result.get("model", config.OLLAMA_MODEL)
    conf = float(thinking_result.get("confidence", 0.0))
    logger.info("Thinking engine (%s): conf %s | tilt %s", model, f"{conf:.0%}", tilt_s)
    if narrative:
        logger.info("PM view: %s", narrative)
    asymmetry = thinking_result.get("asymmetry") or ""
    if asymmetry:
        logger.info("Asymmetry: %s", asymmetry)
    tilt_rationale = thinking_result.get("tilt_rationale") or ""
    if tilt_rationale:
        logger.info("Tilt rationale: %s", tilt_rationale)
    if reasoning:
        sample_lines = [ln for ln in reasoning.splitlines() if ln.strip()][:6]
        logger.debug("Sample reasoning:\n%s", "\n".join(sample_lines))
    risks = thinking_result.get("risks") or []
    opps = thinking_result.get("opportunities") or []
    just = thinking_result.get("justification") or ""
    if risks:
        logger.info("Risks: %s", ", ".join(risks))
    if opps:
        logger.info("Opportunities: %s", ", ".join(opps))
    if just:
        logger.info("Justification: %s", just)


def maybe_apply_thinking_caps(
    base_caps: dict[str, float],
    thinking_result: dict | None,
    *,
    equity: float | None = None,
) -> tuple[dict[str, float], dict | None]:
    """Merge thinking tilt into sleeve caps; returns (caps, thinking_result with apply meta)."""
    if not thinking_result or not config.effective_thinking_engine_enabled():
        return base_caps, thinking_result

    tripped, trip_reason = thinking_daily_loss_tripped(equity)
    if tripped:
        thinking_result = dict(thinking_result)
        thinking_result["apply_log"] = f"Thinking blocked: {trip_reason}"
        thinking_result["applied_deltas"] = {}
        thinking_result["adjusted_caps"] = dict(base_caps)
        thinking_result["safety_blocked"] = "daily_loss_breaker"
        logger.warning("Thinking engine: %s", trip_reason)
        return base_caps, thinking_result

    if config.thinking_manual_approval_required() and not is_thinking_tilt_approved(
        thinking_result
    ):
        thinking_result = dict(thinking_result)
        did = thinking_result.get("decision_id", "?")
        thinking_result["apply_log"] = (
            f"Pending manual approval (decision_id={did}); "
            f"run: python scripts/approve_thinking_tilt.py"
        )
        thinking_result["applied_deltas"] = {}
        thinking_result["adjusted_caps"] = dict(base_caps)
        thinking_result["safety_blocked"] = "manual_approval"
        logger.info("Thinking engine: pending manual approval for %s", did)
        return base_caps, thinking_result

    conf = float(thinking_result.get("confidence", 0.0))
    narrative = str(thinking_result.get("narrative") or "").strip()
    asymmetry = str(thinking_result.get("asymmetry") or "").strip()
    min_conf = 0.60 if asymmetry else 0.65
    narrative_ok = (
        len(narrative) >= 15
        and (
            conf >= 0.75
            or bool(asymmetry)
            or (len(narrative) >= 20 and "range-bound" not in narrative.lower())
        )
    )
    if conf < min_conf or not narrative_ok:
        thinking_result = dict(thinking_result)
        thinking_result["apply_log"] = "Thinking skipped: insufficient confidence or weak narrative"
        thinking_result["applied_deltas"] = {}
        thinking_result["adjusted_caps"] = dict(base_caps)
        logger.info("Thinking engine: skipped apply (conf %.2f, narrative_ok=%s)", conf, narrative_ok)
        return base_caps, thinking_result

    if thinking_result.get("validation_ok") is False and not thinking_result.get(
        "validation_recovered"
    ):
        thinking_result = dict(thinking_result)
        thinking_result["apply_log"] = (
            "Thinking skipped: failed validation — "
            + "; ".join(thinking_result.get("validation_errors") or [])
        )
        thinking_result["applied_deltas"] = {}
        thinking_result["adjusted_caps"] = dict(base_caps)
        return base_caps, thinking_result

    merged, deltas, log_line = apply_thinking_to_sleeve_caps(
        base_caps, thinking_result, equity=equity
    )
    thinking_result = dict(thinking_result)
    thinking_result["applied_deltas"] = deltas
    thinking_result["adjusted_caps"] = merged
    thinking_result["apply_log"] = log_line
    if log_line:
        state = read_json_file(STATE_FILE)
        today = datetime.date.today().isoformat()
        if state.get("last_apply_log_date") != today:
            logger.info(log_line)
            state["last_apply_log_date"] = today
            write_json_file(STATE_FILE, state)
    return merged, thinking_result
