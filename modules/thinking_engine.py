"""Local LLM market reasoning via Ollama — paper bot only."""

from __future__ import annotations

import datetime
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import config

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / config.THINKING_ENGINE_STATE_FILE
OUTPUT_FILE = ROOT / config.THINKING_ENGINE_OUTPUT_FILE

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


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _save_json(path: Path, payload: dict) -> None:
    try:
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


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


def _load_macro_close(col: str):
    from modules.macro_regime_adaptor import _load_daily_close

    return _load_daily_close(col)


def build_market_summary(
    data,
    regime: str,
    vol: str,
    *,
    wisdom: dict | None = None,
    top_headline: str | None = None,
) -> dict[str, Any]:
    """Assemble context for the PM-style reasoning prompt."""
    from modules.pipeline_strategies import _spy_market_up_signal

    spy_sym = config.SPY_BOT_SYMBOL
    up, mom = _spy_market_up_signal(data, spy_sym, config.SPY_MA_WINDOW)
    if up:
        spy_trend = f"above MA{config.SPY_MA_WINDOW} (+{mom * 100:.1f}%)"
    else:
        spy_trend = f"below MA{config.SPY_MA_WINDOW}"

    vix_series = _load_macro_close("VIX")
    vix_val = float(vix_series.iloc[-1]) if len(vix_series) else None

    oil_series = _load_macro_close("USO")
    if oil_series.empty:
        oil_series = _load_macro_close("XOM")
    gold_series = _load_macro_close("GLD")

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
        "oil_change": _pct_change(oil_series) if not oil_series.empty else 0.0,
        "gold_change": _pct_change(gold_series) if not gold_series.empty else 0.0,
        "macro_sentiment": macro_sentiment,
        "top_headline": str(headline)[:240],
        "regime": regime,
    }


def _caps_to_tilt(caps: dict[str, float]) -> dict[str, float]:
    total = sum(float(caps.get(k, 0.0)) for k in _CAP_KEYS)
    if total <= 0:
        return {k: 0.0 for k in _TILT_KEYS}
    tilt = {k: 0.0 for k in _TILT_KEYS}
    for tkey, ckey in _TILT_TO_CAP.items():
        tilt[tkey] += float(caps.get(ckey, 0.0)) / total
    return tilt


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

    if gold >= 3.0 or vix_f >= config.MACRO_VIX_SAFE_HAVEN_MIN:
        deltas["metal"] += 0.05 * conf
        deltas["vti_core"] += 0.03 * conf
        deltas["spy"] -= 0.04 * conf
        deltas["crypto"] -= 0.03 * conf

    if geo:
        deltas["nyse"] += 0.05 * conf
        deltas["metal"] += 0.03 * conf
        deltas["spy"] -= 0.04 * conf
        deltas["cash_buffer"] += 0.03 * conf

    if "below MA" in str(summary.get("spy_trend", "")):
        deltas["vti_core"] += 0.04 * conf
        deltas["cash_buffer"] += 0.03 * conf
        deltas["spy"] -= 0.04 * conf

    max_delta = config.THINKING_MAX_SLEEVE_DELTA
    return {k: round(max(-max_delta, min(max_delta, v)), 6) for k, v in deltas.items()}


def _llm_nudge_deltas(
    base_caps: dict[str, float],
    suggested_tilt: dict[str, float],
    confidence: float,
) -> dict[str, float]:
    """Small optional nudge from LLM target weights (max 3pp per sleeve)."""
    baseline = _caps_to_tilt(base_caps)
    conf = max(0.35, min(1.0, float(confidence)))
    nudges = {k: 0.0 for k in _CAP_KEYS}
    for tkey, ckey in _TILT_TO_CAP.items():
        diff = (float(suggested_tilt.get(tkey, 0.0)) - baseline.get(tkey, 0.0)) * conf * 0.12
        diff = max(-0.03, min(0.03, diff))
        nudges[ckey] = nudges.get(ckey, 0.0) + diff
    return nudges


def compute_cap_deltas(
    base_caps: dict[str, float],
    suggested_tilt: dict[str, float],
    *,
    confidence: float = 0.7,
    market_summary: dict | None = None,
) -> dict[str, float]:
    """Combine rule-based macro tilts with optional LLM nudges."""
    deltas = {k: 0.0 for k in _CAP_KEYS}
    if market_summary:
        for k, v in _rule_based_cap_deltas(market_summary, confidence).items():
            deltas[k] += v
    for k, v in _llm_nudge_deltas(base_caps, suggested_tilt, confidence).items():
        deltas[k] += v
    max_delta = config.THINKING_MAX_SLEEVE_DELTA
    return {k: round(max(-max_delta, min(max_delta, v)), 6) for k, v in deltas.items()}


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
    if gold >= config.MACRO_GLD_SURGE_PCT * 100 or vix_f >= config.MACRO_VIX_SAFE_HAVEN_MIN:
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
) -> tuple[dict[str, float], dict[str, float], str]:
    """Apply LLM/heuristic tilt to sleeve caps (±THINKING_MAX_SLEEVE_DELTA per sleeve)."""
    if equity is not None and equity < config.SMALL_ACCOUNT_EQUITY_THRESHOLD:
        return dict(base_caps), {}, ""

    base = {k: float(base_caps.get(k, 0.0)) for k in _CAP_KEYS}
    conf = max(0.35, min(1.0, float(confidence)))
    cap_deltas = compute_cap_deltas(
        base,
        suggested_tilt,
        confidence=conf,
        market_summary=market_summary,
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
        "gold": 0.05,
        "cash": 0.10,
        "bonds": 0.05,
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
) -> dict:
    """Rule-based thinking proxy for historical backtests."""
    summary = build_market_summary(data, regime, vol)
    tilt = derive_heuristic_tilt(summary)
    return {
        "reasoning": f"Heuristic proxy: {_infer_narrative(summary)}",
        "suggested_tilt": tilt,
        "confidence": 0.70,
        "model": "heuristic-backtest",
        "market_summary": summary,
    }


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
    """Run at most once per day, or again on regime change."""
    state = _load_json(STATE_FILE)
    today = datetime.date.today().isoformat()
    last_date = state.get("last_date")
    last_regime = state.get("last_regime")
    if last_date != today:
        return True
    return last_regime != regime


def _record_thinking_run(regime: str, result: dict) -> None:
    now = datetime.datetime.now().isoformat()
    _save_json(
        STATE_FILE,
        {
            "last_date": datetime.date.today().isoformat(),
            "last_regime": regime,
            "last_run_at": now,
            "model": config.OLLAMA_MODEL,
        },
    )
    _save_json(
        OUTPUT_FILE,
        {
            "timestamp": now,
            "regime": regime,
            **result,
        },
    )


def ollama_available() -> bool:
    url = f"{config.OLLAMA_HOST.rstrip('/')}/api/tags"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _ollama_generate(prompt: str, *, model: str | None = None) -> str:
    model = model or config.OLLAMA_MODEL
    payload = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.4, "num_predict": 1200},
        }
    ).encode("utf-8")
    url = f"{config.OLLAMA_HOST.rstrip('/')}/api/generate"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=config.OLLAMA_TIMEOUT_SEC) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    answer = str(body.get("response") or "").strip()
    thinking = str(body.get("thinking") or "").strip()
    if answer and thinking:
        text = f"{thinking}\n\n---\n{answer}"
    elif answer:
        text = answer
    elif thinking:
        text = thinking
    else:
        raise RuntimeError("Ollama returned empty response")
    return text


def _extract_json_block(text: str) -> dict | None:
    # Look for a fenced JSON block first
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    # Fall back to scanning for any JSON-like object and prefer one that
    # contains either explicit `suggested_tilt` or any known tilt keys.
    candidate = None
    for match in re.finditer(r"\{.*?\}", text, re.S):
        try:
            obj = json.loads(match.group(0))
            if not isinstance(obj, dict):
                continue
            if "suggested_tilt" in obj:
                return obj
            if any(k in obj for k in _TILT_KEYS):
                # wrap legacy flat tilt into new schema
                return {"suggested_tilt": obj}
            # keep the first dict as a fallback
            if candidate is None:
                candidate = obj
        except json.JSONDecodeError:
            continue
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
            base[k] = float(val)
        except (TypeError, ValueError):
            continue
    total = sum(v for v in base.values() if v > 0)
    if total <= 0:
        base.update({"vti": 0.80, "cash": 0.20})
        return base
    return {k: round(v / total, 4) for k, v in base.items() if v > 0}


def get_market_reasoning(market_summary: dict) -> dict:
    """Use local LLM to think about the market like a senior PM."""
    prompt = f"""You are a senior hedge fund portfolio manager with an asymmetric risk focus.

Current market state:
SPY trend: {market_summary['spy_trend']}
VIX: {market_summary['vix']}
Oil 5d change: {market_summary['oil_change']}%
Gold 5d change: {market_summary['gold_change']}%
Recent macro: {market_summary['macro_sentiment']}
Top headline: {market_summary['top_headline']}
Regime: {market_summary.get('regime', 'unknown')}

Think step-by-step and prioritize downside protection where appropriate.

1) Provide a concise dominant narrative (1-2 sentences).
2) List the 1-2 biggest risks (bullet points) and 1-2 highest-conviction opportunities (bullet points).
3) Give an allocation tilt (target weights for: vti, spy, energy, gold, crypto, cash, bonds) for the next 1-7 days, with a 1-2 sentence justification that links the tilt to the narrative and risks.
4) Provide a clear numeric confidence score (0.0-1.0) for your tilt (how actionable you believe this is).

End your output with a single JSON object (fenced with ```json if you like) with these keys:
 - `suggested_tilt`: object of allocation weights (0-1, may sum ~1)
 - `confidence`: number between 0 and 1
 - `narrative`: short string summary
 - `risks`: array of short strings
 - `opportunities`: array of short strings
 - `justification`: short string tying tilt -> narrative

    Example:
    ```json
    {{"suggested_tilt": {{"vti":0.72, "spy":0.06, "energy":0.06, "gold":0.04, "crypto":0.06, "cash":0.04, "bonds":0.02}}, "confidence":0.72, "narrative":"Risk-off driven by rising VIX and gold bid", "risks":["oil shock","geopolitical"], "opportunities":["energy overshoot"], "justification":"Trim equities and add energy/gold as hedge given VIX+gold move"}}
    ```

Be concise, factual, and supply the JSON for machine parsing.
"""

    text = _ollama_generate(prompt)
    parsed = _extract_json_block(text) or {}

    # Normalize confidence
    confidence = parsed.get("confidence", None)
    try:
        confidence = 0.0 if confidence is None else max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.65

    # Extract suggested tilt from new or legacy formats
    suggested = parsed.get("suggested_tilt") or parsed.get("tilt") or parsed
    tilt = _normalize_tilt(suggested)

    # Pull narrative + risks/opps/justification if provided
    narrative = parsed.get("narrative") or (text.splitlines()[0] if text else "")
    risks = parsed.get("risks") or []
    opportunities = parsed.get("opportunities") or parsed.get("opps") or []
    justification = parsed.get("justification") or ""

    return {
        "reasoning": text.strip(),
        "narrative": str(narrative).strip(),
        "risks": risks if isinstance(risks, list) else [str(risks)],
        "opportunities": opportunities if isinstance(opportunities, list) else [str(opportunities)] if opportunities else [],
        "justification": str(justification).strip(),
        "suggested_tilt": tilt,
        "confidence": round(confidence, 2),
        "model": config.OLLAMA_MODEL,
        "market_summary": market_summary,
    }


def maybe_run_thinking(
    data,
    regime: str,
    vol: str,
    wisdom: dict | None = None,
    *,
    top_headline: str | None = None,
    force: bool = False,
) -> dict | None:
    """Paper-only hook: run LLM reasoning at most once/day or on regime change."""
    if not config.effective_thinking_engine_enabled():
        return None
    if not force and not should_refresh_thinking(regime):
        cached = _load_json(OUTPUT_FILE)
        if cached:
            return cached
        return None
    if not ollama_available():
        print("--- Thinking engine: Ollama not reachable (run scripts/setup_ollama.py) ---")
        return None

    summary = build_market_summary(
        data, regime, vol, wisdom=wisdom, top_headline=top_headline
    )
    try:
        result = get_market_reasoning(summary)
    except Exception as exc:
        print(f"--- Thinking engine error (non-fatal): {exc} ---")
        return None

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
    print(
        f"--- Thinking engine ({model}): "
        f"conf {conf:.0%} | tilt {tilt_s} ---"
    )
    if narrative:
        print(f"--- PM view: {narrative} ---")
    # Print brief sample of the full reasoning for human inspection
    if reasoning:
        sample_lines = [ln for ln in reasoning.splitlines() if ln.strip()][:6]
        print("--- Sample reasoning: ---")
        for ln in sample_lines:
            print(f"  {ln}")
    # Also surface extracted risks / opportunities / justification when present
    risks = thinking_result.get("risks") or []
    opps = thinking_result.get("opportunities") or []
    just = thinking_result.get("justification") or ""
    if risks:
        print(f"--- Risks: {', '.join(risks)} ---")
    if opps:
        print(f"--- Opportunities: {', '.join(opps)} ---")
    if just:
        print(f"--- Justification: {just} ---")


def maybe_apply_thinking_caps(
    base_caps: dict[str, float],
    thinking_result: dict | None,
    *,
    equity: float | None = None,
) -> tuple[dict[str, float], dict | None]:
    """Merge thinking tilt into sleeve caps; returns (caps, thinking_result with apply meta)."""
    if not thinking_result or not config.effective_thinking_engine_enabled():
        return base_caps, thinking_result
    # Only apply when LLM expresses reasonable confidence and narrative is substantive
    conf = float(thinking_result.get("confidence", 0.0))
    narrative = str(thinking_result.get("narrative") or "").strip()
    narrative_ok = len(narrative) >= 20 and "range-bound" not in narrative.lower()
    if conf < 0.65 or not narrative_ok:
        # mark as skipped for clarity
        thinking_result = dict(thinking_result)
        thinking_result["apply_log"] = "Thinking skipped: insufficient confidence or weak narrative"
        thinking_result["applied_deltas"] = {}
        thinking_result["adjusted_caps"] = dict(base_caps)
        print(f"--- Thinking engine: skipped apply (conf {conf:.2f}, narrative_ok={narrative_ok}) ---")
        return base_caps, thinking_result

    merged, deltas, log_line = apply_thinking_to_sleeve_caps(
        base_caps, thinking_result, equity=equity
    )
    thinking_result = dict(thinking_result)
    thinking_result["applied_deltas"] = deltas
    thinking_result["adjusted_caps"] = merged
    thinking_result["apply_log"] = log_line
    if log_line:
        state = _load_json(STATE_FILE)
        today = datetime.date.today().isoformat()
        if state.get("last_apply_log_date") != today:
            print(log_line)
            state["last_apply_log_date"] = today
            _save_json(STATE_FILE, state)
    return merged, thinking_result
