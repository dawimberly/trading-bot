"""Local LLM market reasoning via Ollama — paper bot only."""

from __future__ import annotations

import datetime
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

_PM_SYSTEM_PROMPT = """You are a battle-hardened hedge fund portfolio manager specializing in asymmetric risk and paradigm shifts.
You think like Eric Weinstein combined with top macro traders — you hunt for situations where upside is significantly larger than downside.

Given current market data:
- SPY vs MA200 trend
- VIX level & trend
- Oil & Gold 5-day moves
- TNX / yield curve
- Current regime (RHYME_*)
- Bot current exposure
- Any major headline or geopolitical event

Think step-by-step like a senior PM with skin in the game:

1. What is the dominant narrative right now? (What story is the market telling itself?)
2. Where is the asymmetry? (Where is the crowd wrong or forced to change?)
3. What are the 1-2 biggest risks and the highest-conviction opportunities?
4. Recommend a clear allocation tilt for the next 3-7 days. Be decisive. Use percentages for VTI, SPY, Energy, Gold, Cash, etc.

Output format (strict — your ENTIRE reply must be ONLY these lines, no preamble):
NARRATIVE: [One powerful sentence]
ASYMMETRY: [Where the edge is]
RISKS: [bullet list, max 2]
OPPORTUNITIES: [bullet list, max 2]
RECOMMENDED_TILT: {"vti": 0.XX, "spy": 0.XX, "energy": 0.XX, "gold": 0.XX, "cash": 0.XX, ...}
CONFIDENCE: 0.XX
REASONING: [Concise but high-signal explanation]

Do not explain your process. Do not repeat the input. Start with NARRATIVE:"""

_STRUCTURED_FIELD_RE = re.compile(
    r"^(NARRATIVE|ASYMMETRY|RISKS|OPPORTUNITIES|RECOMMENDED_TILT|CONFIDENCE|REASONING|PARADIGM_SHIFT)\s*:\s*(.*)$",
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
    tilt = derive_heuristic_tilt(market_summary)
    narrative = _infer_narrative(market_summary)
    return {
        "reasoning": f"Rule-based fallback ({reason}): {narrative}",
        "narrative": narrative,
        "asymmetry": "",
        "risks": [],
        "opportunities": [],
        "justification": reason,
        "suggested_tilt": tilt,
        "confidence": 0.70,
        "model": reason,
        "source": "heuristic",
        "parse_quality": 0.0,
        "market_summary": market_summary,
    }


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
    write_json_file(
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
        elif key == "confidence":
            conf = _parse_confidence_value(raw.splitlines()[0] if raw else "")
            if conf is not None:
                result["confidence"] = conf
        elif key == "reasoning":
            result["reasoning_excerpt"] = raw
        current_field = None
        buf = []

    for line in text.splitlines():
        match = _STRUCTURED_FIELD_RE.match(line.strip())
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


def _looks_like_meta_narrative(text: str) -> bool:
    low = text.lower().strip()
    return low.startswith("first,") or "i need to" in low or "user provided" in low


def _build_reasoning_user_prompt(market_summary: dict) -> str:
    return f"""Market snapshot:

SPY vs MA200: {market_summary['spy_trend']}
VIX: {market_summary['vix']} ({market_summary.get('vix_trend', 'n/a')})
Oil 5d: {market_summary['oil_change']}% | Gold 5d: {market_summary['gold_change']}%
Rates: {market_summary.get('yield_curve', 'n/a')}
Regime: {market_summary.get('regime', 'unknown')} | {market_summary['macro_sentiment']}
Headline: {market_summary['top_headline']}
Bot exposure: {market_summary.get('bot_exposure_str', 'n/a')}

Reply with ONLY the structured block. Start with NARRATIVE:"""


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
        or parsed.get("suggested_tilt")
        or parsed.get("recommended_tilt")
        or parsed.get("tilt")
        or parsed
    )
    tilt = _normalize_tilt(raw_tilt if isinstance(raw_tilt, dict) else None)

    narrative = structured.get("narrative") or parsed.get("narrative") or ""
    if not narrative or _looks_like_meta_narrative(narrative):
        narrative = _infer_narrative(market_summary)

    risks = structured.get("risks") or parsed.get("risks") or []
    opportunities = (
        structured.get("opportunities")
        or parsed.get("opportunities")
        or parsed.get("opps")
        or []
    )
    asymmetry = structured.get("asymmetry") or parsed.get("asymmetry") or ""
    justification = (
        structured.get("reasoning_excerpt")
        or parsed.get("justification")
        or parsed.get("reasoning")
        or ""
    )
    quality = _llm_parse_quality(structured, tilt, had_conf_field=had_conf_field)

    return {
        "reasoning": full_text.strip(),
        "narrative": str(narrative).strip(),
        "asymmetry": str(asymmetry).strip() if asymmetry else "",
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
                return build_heuristic_reasoning_result(
                    market_summary,
                    reason="fast-models-not-installed",
                )
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
            return result
        if idx < len(candidates) - 1:
            continue
        return result

    if best is not None:
        return best

    reason = "; ".join(errors) if errors else "no-models"
    return build_heuristic_reasoning_result(market_summary, reason=reason)


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
    conf = float(thinking_result.get("confidence", 0.0))
    narrative = str(thinking_result.get("narrative") or "").strip()
    narrative_ok = len(narrative) >= 20 and "range-bound" not in narrative.lower()
    if conf < 0.65 or not narrative_ok:
        thinking_result = dict(thinking_result)
        thinking_result["apply_log"] = "Thinking skipped: insufficient confidence or weak narrative"
        thinking_result["applied_deltas"] = {}
        thinking_result["adjusted_caps"] = dict(base_caps)
        logger.info("Thinking engine: skipped apply (conf %.2f, narrative_ok=%s)", conf, narrative_ok)
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
