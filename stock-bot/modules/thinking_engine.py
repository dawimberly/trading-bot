"""Local LLM market reasoning via Ollama (+ optional Kimi daily deep think).

System-wide and configurable: paper/research default ON, live default OFF
(LIVE_THINKING_ENGINE_ENABLED). Live tilts still honor manual approval +
daily-loss circuit breaker.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import re
import time
import urllib.error
from pathlib import Path
from typing import Any
import threading

import config
from modules.ollama_client import (
    clear_ollama_cache,
    format_ollama_status_line,
    model_available,
    ollama_available,
    ollama_complete,
    ollama_installed_models,
    ollama_json,
    ollama_version,
    resolve_model_chain,
)
from modules.safe_io import read_json_file, write_json_file
from modules.logging_utils import log_event, log_subsystem_error, log_subsystem_warning

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / config.THINKING_ENGINE_STATE_FILE
OUTPUT_FILE = ROOT / config.THINKING_ENGINE_OUTPUT_FILE
APPROVAL_FILE = ROOT / config.THINKING_APPROVAL_FILE
AUDIT_LOG = ROOT / "logs" / "thinking_engine.log"
THINKING_MAX_TOTAL_DELTA = 0.12
THINKING_MAX_ACTIVE_SLEEVES = 3
# Fail-fast for structured / market reasoning calls; heuristic on exhaustion.
_OLLAMA_THINKING_TIMEOUT_DEFAULT = 90
_OLLAMA_MAX_ATTEMPTS = 3
_OLLAMA_STARTUP_CHECKED = False
_OLLAMA_STARTUP_STATUS: dict[str, Any] = {}

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
def _audit_thinking(event: str, **fields: Any) -> None:
    """Append JSON audit lines for paper thinking runs (non-blocking path safe)."""
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            **fields,
        }
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, default=str) + "\n")
    except OSError:
        logger.debug("Thinking audit log write failed", exc_info=True)


def _ollama_max_attempts() -> int:
    return max(1, min(_OLLAMA_MAX_ATTEMPTS, int(getattr(config, "OLLAMA_RETRY_COUNT", 3))))


def _thinking_timeout_sec(override: int | None = None) -> int:
    if override is not None:
        return max(5, int(override))
    return max(
        5,
        int(
            getattr(
                config,
                "OLLAMA_THINKING_TIMEOUT_SEC",
                _OLLAMA_THINKING_TIMEOUT_DEFAULT,
            )
        ),
    )


def _retry_backoff_sec(attempt: int) -> float:
    """Exponential backoff after attempt N (0-based): 1s, 2s, 4s."""
    return min(8.0, 1.0 * (2**attempt))


def check_ollama_startup_status(*, force: bool = False) -> dict[str, Any]:
    """Probe Ollama once at thinking-engine startup; log host/model/reachability.

    Safe to call repeatedly — subsequent calls return the cached snapshot unless
    ``force=True``.
    """
    global _OLLAMA_STARTUP_CHECKED, _OLLAMA_STARTUP_STATUS
    if _OLLAMA_STARTUP_CHECKED and not force and _OLLAMA_STARTUP_STATUS:
        return dict(_OLLAMA_STARTUP_STATUS)

    host = str(getattr(config, "OLLAMA_HOST", "http://localhost:11434"))
    primary = str(getattr(config, "OLLAMA_MODEL", "?"))
    reachable = False
    version: str | None = None
    installed: list[str] = []
    chain: list[str] = []
    error: str | None = None
    try:
        reachable = bool(ollama_available())
        if reachable:
            version = ollama_version()
            installed = sorted(ollama_installed_models())
            chain = list(thinking_model_chain())
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        reachable = False

    status: dict[str, Any] = {
        "reachable": reachable,
        "host": host,
        "version": version,
        "primary_model": primary,
        "chain": chain[:5],
        "installed_count": len(installed),
        "installed_sample": installed[:5],
        "timeout_sec": _thinking_timeout_sec(),
        "max_attempts": _ollama_max_attempts(),
        "error": error,
        "banner": format_ollama_status_line(),
    }
    _OLLAMA_STARTUP_STATUS = status
    _OLLAMA_STARTUP_CHECKED = True

    if reachable:
        logger.info(
            "Ollama startup OK host=%s version=%s model=%s chain=%s "
            "timeout=%ss attempts=%s installed=%s",
            host,
            version or "?",
            primary,
            ",".join(chain[:3]) or primary,
            status["timeout_sec"],
            status["max_attempts"],
            len(installed),
        )
    else:
        logger.warning(
            "Ollama startup UNAVAILABLE host=%s model=%s error=%s — "
            "thinking will use heuristic fallback",
            host,
            primary,
            error or "unreachable",
        )
    _audit_thinking("ollama_startup_check", **{k: v for k, v in status.items() if k != "banner"})
    return dict(status)


def ensure_ollama_startup_check() -> dict[str, Any]:
    """Idempotent startup probe used by maybe_run_thinking / tests."""
    return check_ollama_startup_status(force=False)


def _extract_labeled_block(text: str, label: str) -> str:
    if not text:
        return ""
    m = re.search(
        rf"{label}\s*:\s*(.+?)(?=(?:NARRATIVE|ASYMMETRY|SECTOR_VIEW|AI_CYCLE_PHASE|"
        rf"REGIME_SIGNAL|TILT_SIGNAL|RISK_SIGNAL|"
        rf"RECOMMENDED_TILT|TILT_RATIONALE|CONFIDENCE|RISKS|OPPORTUNITIES)\s*:|$)",
        text,
        re.I | re.S,
    )
    if not m:
        m = re.search(rf"{label}\s*:\s*(.+)", text, re.I)
    if not m:
        return ""
    return m.group(1).strip().splitlines()[0].strip()


def _tilt_deltas_reasonable(deltas: dict[str, float]) -> tuple[bool, str]:
    material = {k: v for k, v in deltas.items() if abs(float(v)) >= 0.005}
    if len(material) > THINKING_MAX_ACTIVE_SLEEVES:
        return False, f"too many sleeves moved ({len(material)} > {THINKING_MAX_ACTIVE_SLEEVES})"
    total = sum(abs(float(v)) for v in deltas.values())
    if total > THINKING_MAX_TOTAL_DELTA:
        return False, f"total sleeve delta {total:.1%} exceeds {THINKING_MAX_TOTAL_DELTA:.0%} cap"
    return True, ""


def _material_sleeve_count(deltas: dict[str, float]) -> int:
    return len([v for v in deltas.values() if abs(float(v)) >= 0.005])


def _thinking_tilt_apply_kwargs(equity: float | None = None) -> dict[str, Any]:
    """Live-like kwargs for apply_thinking_tilt_to_caps (small account + ±6% cap)."""
    kwargs: dict[str, Any] = {}
    small = equity is not None and float(equity) < config.SMALL_ACCOUNT_EQUITY_THRESHOLD
    live_book = not config.PAPER_TRADING and not config.paper_only_sleeves_active()
    live_sim = config.live_thinking_sim_context()
    if small or live_book or live_sim:
        kwargs["allow_small_account"] = True
        kwargs["max_sleeve_delta"] = config.LIVE_THINKING_MAX_SLEEVE_DELTA
    return kwargs


def _merge_caps_from_deltas(
    base: dict[str, float],
    cap_deltas: dict[str, float],
) -> dict[str, float]:
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
    return merged


def _should_consolidate_news_deltas(
    cap_deltas: dict[str, float],
    market_summary: dict | None,
    *,
    live_like: bool = False,
) -> bool:
    """True when consolidation should run before the 3-sleeve safety guard."""
    material_n = _material_sleeve_count(cap_deltas)
    if material_n > THINKING_MAX_ACTIVE_SLEEVES:
        return True
    if not market_summary:
        return False
    has_news = bool(
        market_summary.get("news_headlines")
        or market_summary.get("news_digest")
        or market_summary.get("news_theme_summary")
    )
    if not has_news:
        return False
    impact = _news_impact(market_summary)
    if live_like or impact >= 0.15:
        return True
    return material_n > 1


def _consolidate_news_deltas(
    deltas: dict[str, float],
    market_summary: dict | None,
    *,
    max_per_sleeve: float,
) -> dict[str, float]:
    """Merge news-driven multi-sleeve deltas to <=3 before safety guard."""
    from modules.thinking_news import consolidate_news_deltas, _clamp_cap_deltas

    return consolidate_news_deltas(
        deltas,
        market_summary,
        max_per_sleeve=max_per_sleeve,
    )


def _clamp_tilt_deltas(
    deltas: dict[str, float],
    *,
    max_per_sleeve: float,
    max_total: float = THINKING_MAX_TOTAL_DELTA,
) -> dict[str, float]:
    from modules.thinking_news import _clamp_cap_deltas

    return _clamp_cap_deltas(
        deltas,
        max_per_sleeve=max_per_sleeve,
        max_total=max_total,
    )


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

_TILT_ALIASES = {
    "vti_core": "vti",
    "xle": "energy",
    "gld": "gold",
    "treasury": "bonds",
    "tlt": "bonds",
    "tech": "spy",
    "technology": "spy",
    "software": "spy",
    "semis": "spy",
    "semiconductors": "spy",
    "smh": "spy",
    "nvda": "spy",
    "ai": "spy",
    "infrastructure": "spy",
    "datacenter": "spy",
    "robotics": "spy",
    "defense": "energy",
    "financials": "spy",
    "financial": "spy",
}
_SECTOR_PROXIES = (
    ("Tech (QQQ)", "QQQ"),
    ("Semis (NVDA)", "NVDA"),
    ("AI Infra (SMCI)", "SMCI"),
    ("Broad (SPY)", "SPY"),
    ("Energy (XOM)", "XOM"),
    ("Gold (GLD)", "GLD"),
    ("Defense (RTX)", "RTX"),
)
_AI_CYCLE_KEYWORDS = (
    "ai",
    "tech",
    "semiconductor",
    "semi",
    "nvidia",
    "datacenter",
    "software",
    "robotics",
    "supercycle",
    "bubble",
    "rotation",
    "late-cycle",
    "mid-cycle",
    "exhaustion",
)

_PM_SYSTEM_PROMPT: str | None = None
_MACRO_SERIES_CACHE: dict[str, tuple[float, object]] = {}
_MACRO_CACHE_TTL_SEC = 3600.0
_MACRO_CACHE_MAX = 24
_VALIDATION_RESULT_CACHE: dict[str, tuple[float, bool, tuple[str, ...]]] = {}
_VALIDATION_CACHE_TTL_SEC = 600.0
_VALIDATION_CACHE_MAX = 32
_OLLAMA_RESPONSE_CACHE: dict[str, tuple[float, tuple[str, str]]] = {}
_OLLAMA_CACHE_TTL_SEC = 1800.0
_OLLAMA_CACHE_MAX = 4


def _pm_system_prompt() -> str:
    global _PM_SYSTEM_PROMPT
    if _PM_SYSTEM_PROMPT is not None:
        return _PM_SYSTEM_PROMPT
    _PM_SYSTEM_PROMPT = """You are an elite asymmetric-risk hedge fund PM optimizing to BEAT VTI on a risk-adjusted basis (Sharpe first, then return vs passive beta).

Primary objective: outperform buy-and-hold VTI without taking unnecessary drawdown. Every tilt must earn its risk budget.

Current context:
- Multi-year AI/Tech paradigm shift (Nvidia, datacenters, semis, software, robotics) — but leadership rotates (semis -> software -> infra -> energy).
- Crowded trades (consensus long tech, passive 60/40, meme momentum) are where edge dies — seek forced flows and mispriced hedges.
- Stat arb sleeve: mean-reverting pairs add alpha when spreads are wide and vol is moderate; reduce active tilt when spreads compress or vol spikes.
- Vol overlay sleeve: earns in elevated VIX (hedge/income); when VIX is high, DO NOT double-down on directional beta — let vol sleeve work, trim SPY/NYSE.
- Options income sleeve: harvest premium in calm vol; avoid max equity when VIX is rising.

Given the latest data:
- SPY vs MA200, VIX level & trend
- Sector leadership (Tech, Semis, Energy, Gold, Defense)
- Oil/Gold/TNX, stat-arb-friendly vol regime, vol-overlay regime
- Bot current exposure
- Major headline

Think step-by-step (internally — do NOT output your steps):
1. AI/Tech cycle phase? (Early, mid-cycle, late-cycle, rotation, exhaustion?)
2. Where is the crowd wrong or forced to adjust (asymmetry vs VTI)?
3. Stat arb + vol overlay: supportive or hostile for adding active risk today?
4. Highest conviction 3-7d edge without crowded positioning?
5. Decisive tilt that improves Sharpe vs passive VTI — not max return at any cost.

DECISIVENESS (required):
- NARRATIVE: ONE stance — risk-on, neutral, or defensive (not "bullish but cautious").
- Mixed evidence → cash/bonds over equal-weight sleeves; do not spray 10-15% across everything.
- RECOMMENDED_TILT reflects that single stance.

VTI-BEAT RULES:
- Default passive anchor is high VTI; only LOWER vti when active sleeves have clear asymmetry AND vol/stat-arb context supports it.
- Avoid crowded trades: if Tech/Semis already led 5d and VIX rising, do NOT add more growth — trim toward cash/vol hedge.
- When VIX >= 22 or rising sharply: bias cash; stat arb + vol overlay carry the hedge — do not stack directional beta on top.
- When VIX <= 16, SPY above MA200, Semis/AI leading: modest spy/tech tilt OK; keep vti >= 0.45 unless confidence >= 0.80.
- Beat VTI by selective tilts, not by going 100% active.

PRODUCTION HARD RULES (non-negotiable):
- Consistency with yesterday's tilt unless STRONG NEW EVIDENCE (VIX spike, trend break, headline, rotation).
- Per-sleeve change vs yesterday: +/-6% without new evidence; cite evidence in TILT_RATIONALE if exceeding +/-5%.
- Do NOT overweight gold when Gold 5d is negative unless asymmetry cites contrarian bounce.
- SPY below MA200 + elevated VIX → capital preservation.
- RECOMMENDED_TILT: decimal weights 0.00-1.00, sum ~1.0.
- Map sleeves: vti=broad beta, spy=tech/growth, energy=energy/defense, gold=metals, cash=cash, crypto=crypto (stat-arb pairs), bonds=bonds/cash buffer.
- tech/semis keys merge into spy/energy internally.

Output format (strict — ENTIRE reply ONLY these lines, no preamble, no markdown):
NARRATIVE: [Regime + AI cycle + VTI-beat thesis in one sentence]
ASYMMETRY: [Crowd positioning error or forced flow — why VTI passive is wrong/right today]
SECTOR_VIEW: [Tech/Semis/Energy/Defense/Gold leaders/laggards; stat-arb & vol-overlay read; 3-7d view]
REGIME_SIGNAL: [risk-on|neutral|defensive] | strength 0.00-1.00 | [one-line driver]
TILT_SIGNAL: [primary sleeve to add] / [primary sleeve to cut] | conviction 0.00-1.00
RISK_SIGNAL: [low|medium|high] | [top risk in <=12 words]
RECOMMENDED_TILT: {"vti": 0.XX, "spy": 0.XX, "tech": 0.XX, "energy": 0.XX, "gold": 0.XX, "cash": 0.XX, "crypto": 0.XX, "bonds": 0.XX}
TILT_RATIONALE: [Link asymmetry + sector/stat-arb/vol view to each sleeve >5%; mention VTI vs active trade-off]
CONFIDENCE: 0.XX
RISKS: [risk1; risk2]
OPPORTUNITIES: [opp1; opp2]

Do not explain your process. Start with NARRATIVE:"""
    return _PM_SYSTEM_PROMPT


def clear_thinking_runtime_caches() -> None:
    """Drop in-process macro / validation / Ollama caches."""
    global _MACRO_SERIES_CACHE, _VALIDATION_RESULT_CACHE, _OLLAMA_RESPONSE_CACHE
    _MACRO_SERIES_CACHE.clear()
    _VALIDATION_RESULT_CACHE.clear()
    _OLLAMA_RESPONSE_CACHE.clear()
    clear_ollama_cache()

_STRUCTURED_FIELD_RE = re.compile(
    r"^(?:#+\s*)?(NARRATIVE|ASYMMETRY|SECTOR_VIEW|AI_CYCLE_PHASE|REGIME_SIGNAL|TILT_SIGNAL|RISK_SIGNAL|RISKS|OPPORTUNITIES|RECOMMENDED_TILT|TILT|TILT_RATIONALE|CONFIDENCE|REASONING|PARADIGM_SHIFT|REGIME_NARRATIVE)\s*[:=\-]\s*(.*)$",
    re.I,
)
_TILT_PROSE_RE = re.compile(
    r"\b(vti|spy|tech|semis|energy|gold|cash|crypto|bonds)\s*[:=]?\s*(\d{1,3}(?:\.\d+)?)\s*%?",
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

    now = time.monotonic()
    hit = _MACRO_SERIES_CACHE.get(col)
    if hit and now - hit[0] < _MACRO_CACHE_TTL_SEC:
        return hit[1]

    series = _load_daily_close(col)
    if len(_MACRO_SERIES_CACHE) >= _MACRO_CACHE_MAX:
        oldest = min(_MACRO_SERIES_CACHE, key=lambda k: _MACRO_SERIES_CACHE[k][0])
        del _MACRO_SERIES_CACHE[oldest]
    _MACRO_SERIES_CACHE[col] = (now, series)
    return series


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
    except Exception as exc:
        logger.debug("yield-stress annotation unavailable: %s", exc)
    return ", ".join(parts) if parts else "n/a"


def _format_bot_exposure(base_caps: dict[str, float] | None = None) -> str:
    caps = base_caps or config.fund_allocation_pct()
    parts: list[str] = []
    for key in _CAP_KEYS:
        pct = float(caps.get(key, 0.0))
        if pct >= 0.005:
            parts.append(f"{_CAP_LABELS[key]} {pct:.0%}")
    return ", ".join(parts) if parts else "n/a"


def _symbol_5d_pct(data, symbol: str, macro_cache: dict) -> float | None:
    if data is not None and hasattr(data, "columns") and symbol in data.columns:
        try:
            return _pct_change(data[symbol])
        except (TypeError, ValueError, IndexError):
            pass
    series = _load_macro_close(symbol, macro_cache)
    if series is not None and not series.empty:
        return _pct_change(series)
    return None


def _build_sector_leadership(data, macro_cache: dict | None = None) -> dict[str, Any]:
    macro_cache = macro_cache or {}
    rows: list[dict[str, Any]] = []
    for label, sym in _SECTOR_PROXIES:
        ch = _symbol_5d_pct(data, sym, macro_cache)
        if ch is not None:
            rows.append({"sector": label, "symbol": sym, "change_5d_pct": ch})
    rows.sort(key=lambda r: float(r["change_5d_pct"]), reverse=True)
    leaders = rows[:3]
    laggards = list(reversed(rows[-2:])) if len(rows) >= 2 else []
    return {
        "sectors": rows,
        "leaders": leaders,
        "laggards": laggards,
        "leadership_str": ", ".join(
            f"{r['sector']} {float(r['change_5d_pct']):+.1f}%" for r in leaders
        )
        or "n/a",
    }


def _vol_overlay_regime(summary: dict | None) -> str:
    """Classify vol overlay environment for prompt + heuristic tilts."""
    if not summary:
        return "unknown"
    vix = summary.get("vix")
    vix_f = float(vix) if vix not in (None, "n/a") else 18.0
    vix_trend = str(summary.get("vix_trend", "")).lower()
    if vix_f >= 28 or (vix_f >= 22 and "rising" in vix_trend):
        return "elevated — vol overlay active; trim directional beta"
    if vix_f >= 20:
        return "elevated — hedge/income favorable; cautious on growth adds"
    if vix_f <= 14 and "falling" in vix_trend:
        return "calm — options income friendly; selective growth tilt OK"
    if vix_f <= 16:
        return "normal-low — modest active risk if asymmetry clear"
    return "normal — balance VTI anchor with selective sleeves"


def _stat_arb_regime(summary: dict | None) -> str:
    """Heuristic stat-arb environment from vol + trend."""
    if not summary:
        return "unknown"
    vix = summary.get("vix")
    vix_f = float(vix) if vix not in (None, "n/a") else 18.0
    spy_trend = str(summary.get("spy_trend", ""))
    if vix_f >= 26:
        return "hostile — spreads gappy; favor cash over new pair risk"
    if "below MA" in spy_trend and vix_f >= 20:
        return "cautious — mean-revert only on high-confidence pairs"
    if 14 <= vix_f <= 22 and "above MA" in spy_trend:
        return "supportive — pairs + crypto stat-arb can add alpha vs VTI"
    if vix_f <= 14:
        return "compressed — lower pair edge; don't over-allocate crypto for arb"
    return "mixed — small crypto/stat-arb sleeve only"


def _crowded_trade_warning(summary: dict | None) -> str:
    """Flag consensus overcrowding to avoid bad tilts."""
    if not summary:
        return ""
    leaders = summary.get("sector_leaders") or []
    tech_hot = any(
        float(r.get("change_5d_pct", 0)) > 8.0
        and any(k in str(r.get("sector", "")) for k in ("Tech", "Semis", "AI"))
        for r in leaders[:2]
    )
    vix_rising = "rising" in str(summary.get("vix_trend", "")).lower()
    if tech_hot and vix_rising:
        return "CROWDED: Tech/Semis extended 5d + VIX rising — avoid chasing; VTI+cash beats adding beta."
    if tech_hot:
        return "CROWDED: Tech leadership extended — new longs are late; favor VTI anchor + selective hedges."
    return "No extreme crowding flag — selective active tilts allowed if asymmetry clear."


def _infer_ai_cycle_phase(summary: dict) -> str:
    spy_trend = str(summary.get("spy_trend", ""))
    vix = summary.get("vix")
    vix_f = float(vix) if vix not in (None, "n/a") else 18.0
    vix_trend = str(summary.get("vix_trend", "")).lower()
    leaders = summary.get("sector_leaders") or []

    def _sector_name(row: dict) -> str:
        return str(row.get("sector", ""))

    tech_leading = any(
        any(k in _sector_name(r) for k in ("Tech", "Semis", "AI"))
        for r in leaders[:2]
    )
    energy_leading = any("Energy" in _sector_name(r) for r in leaders[:1])

    if "below MA" in spy_trend and vix_f >= 22:
        return "exhaustion / risk-off"
    if "rising" in vix_trend and tech_leading and vix_f >= 18:
        return "late-cycle / rotation risk"
    if energy_leading and not tech_leading:
        return "rotation (energy / real assets)"
    if tech_leading and "above MA" in spy_trend:
        if any("Semis" in _sector_name(r) or "AI" in _sector_name(r) for r in leaders[:1]):
            return "mid-cycle AI leadership"
        return "mid-cycle tech breadth"
    if "above MA" in spy_trend:
        return "early-cycle / broad risk-on"
    return "range-bound / unclear phase"


def _build_bubble_context(data, regime: str, vol: str) -> dict[str, Any]:
    """Buffett / bubble risk for thinking prompts."""
    try:
        from modules.bubble_risk import compute_bubble_risk, format_bubble_risk_summary

        ctx = compute_bubble_risk(data, regime, volatility=vol)
        buff = ctx.get("buffett") or {}
        ratio = buff.get("ratio_pct")
        signal = str(buff.get("signal") or "")
        score_100 = float(ctx.get("score_100") or 0.0)
        reading = format_bubble_risk_summary(ctx) or (
            f"Bubble {score_100:.0f}/100"
            + (f" | Buffett {ratio:.1f}% GDP ({signal})" if ratio is not None else "")
        )
        return {
            "bubble_score": float(ctx.get("score_normalized") or 0.0),
            "bubble_score_100": score_100,
            "buffett_signal": signal,
            "buffett_ratio_pct": ratio,
            "buffett_reading": reading,
            "bubble_technical_fraction": ctx.get("technical_fraction"),
        }
    except Exception:
        return {
            "bubble_score": 0.0,
            "bubble_score_100": 0.0,
            "buffett_signal": "",
            "buffett_ratio_pct": None,
            "buffett_reading": "bubble data unavailable",
        }


def _build_technical_context(data) -> dict[str, Any]:
    """Compact technical snapshot for LLM context."""
    from modules.pipeline_strategies import _spy_market_up_signal

    spy = config.SPY_BOT_SYMBOL
    up, mom = _spy_market_up_signal(data, spy, config.SPY_MA_WINDOW)
    lines: list[str] = []
    if up:
        lines.append(f"SPY above MA{config.SPY_MA_WINDOW} (+{mom * 100:.1f}%)")
    else:
        lines.append(f"SPY below MA{config.SPY_MA_WINDOW}")
    if config.effective_rvol_scanner_enabled():
        try:
            from modules.volume_analysis import get_high_rvol_stocks

            hot = get_high_rvol_stocks(data, min_rvol=2.0, limit=5)
            if hot:
                lines.append(
                    "High RVOL: "
                    + ", ".join(f"{r['symbol']} {r.get('rvol', 0):.1f}x" for r in hot[:5])
                )
        except Exception as exc:
            logger.debug("high-RVOL technical annotation unavailable: %s", exc)
    return {"technical_summary": "; ".join(lines) or "n/a"}


def _build_stat_arb_context(data, regime: str) -> dict[str, Any]:
    """Top stat-arb pair candidates for LLM review (scan only, no orders)."""
    if not config.effective_stat_arb_enabled():
        return {"stat_arb_candidates": [], "stat_arb_candidate_summary": "stat arb OFF"}
    try:
        from modules.stat_arb_sleeve import _nyse_stat_arb_columns, _scan_pair_candidates

        cols = _nyse_stat_arb_columns(data)
        if len(cols) < 2:
            return {"stat_arb_candidates": [], "stat_arb_candidate_summary": "universe<2"}
        raw = _scan_pair_candidates(
            data,
            cols,
            lookback=config.STAT_ARB_LOOKBACK,
            min_corr=config.effective_stat_arb_min_correlation(),
            z_entry=config.effective_stat_arb_z_entry(regime=regime),
            momentum_pick=True,
            max_leg_vol=config.effective_stat_arb_max_leg_vol(),
        )
        candidates = []
        for score, z, long_sym, short_sym, _beta, _y, _x, corr in raw[:6]:
            candidates.append(
                {
                    "pair": f"{long_sym}/{short_sym}",
                    "z": round(float(z), 2),
                    "corr": round(float(corr), 3),
                    "score": round(float(score), 3),
                }
            )
        summary = (
            ", ".join(f"{c['pair']} z={c['z']} corr={c['corr']}" for c in candidates[:4])
            or "no pairs above threshold"
        )
        return {"stat_arb_candidates": candidates, "stat_arb_candidate_summary": summary}
    except Exception as exc:
        return {"stat_arb_candidates": [], "stat_arb_candidate_summary": f"scan error: {exc}"}


_ENRICHMENT_CACHE: dict[str, tuple[float, Any]] = {}
_ENRICHMENT_CACHE_TTL = 300.0


def _cache_get(key: str) -> Any | None:
    row = _ENRICHMENT_CACHE.get(key)
    if row and time.monotonic() - row[0] < _ENRICHMENT_CACHE_TTL:
        return row[1]
    return None


def _cache_put(key: str, value: Any) -> Any:
    _ENRICHMENT_CACHE[key] = (time.monotonic(), value)
    return value


def _build_insider_thinking_context() -> dict[str, Any]:
    """Insider cluster buys + executive sells for LLM context."""
    if not config.effective_insider_monitor_enabled():
        return {"insider_summary": "monitor OFF"}
    try:
        if config.effective_insider_signal_boost_enabled():
            from modules.insider_signal_handler import get_thinking_context as _insider_ctx

            return _insider_ctx()
        from modules.insider_monitor import get_insider_context_for_thinking

        return get_insider_context_for_thinking()
    except Exception:
        return {"insider_summary": "n/a"}


def _build_news_thinking_context(news_headlines: str = "", *, news_analysis: dict | None = None) -> dict[str, Any]:
    """News headlines + themes for Ollama prompts."""
    text = str(news_headlines or "").strip()
    if not text:
        try:
            from modules.thinking_news import get_news_for_thinking

            text = get_news_for_thinking(max_items=8) or ""
        except Exception as exc:
            logger.debug("news headlines for thinking prompt unavailable: %s", exc)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()][:8]
    out: dict[str, Any] = {
        "news_headline_lines": lines,
        "news_summary": "; ".join(lines[:4])[:480] if lines else "no headlines cached",
    }
    if news_analysis:
        out["news_themes"] = news_analysis.get("themes")
        out["news_theme_summary"] = news_analysis.get("theme_summary")
        out["news_impact_score"] = news_analysis.get("news_impact_score")
        if news_analysis.get("digest_text"):
            out["news_digest"] = str(news_analysis["digest_text"])[:600]
    elif lines:
        try:
            from modules.thinking_news import analyze_news_headlines

            analysis = analyze_news_headlines("\n".join(lines))
            out["news_themes"] = analysis.get("themes")
            out["news_theme_summary"] = analysis.get("theme_summary")
            out["news_impact_score"] = analysis.get("news_impact_score")
            out["news_digest"] = analysis.get("digest_text")
        except Exception as exc:
            logger.debug("headline theme analysis unavailable: %s", exc)
    theme = str(out.get("news_theme_summary") or "").strip()
    if theme:
        out["news_catalyst_summary"] = f"{out['news_summary'][:200]} | themes: {theme[:160]}"
    else:
        out["news_catalyst_summary"] = out["news_summary"]
    return out


def _build_catalyst_thinking_context(data) -> dict[str, Any]:
    """Top catalyst scores with factors (structured + summary line)."""
    if not config.effective_catalyst_scoring_enabled():
        return {"catalyst_summary": "scanner OFF", "catalyst_top": []}
    try:
        from modules.catalyst_scoring import get_top_catalyst_stocks

        if data is None or getattr(data, "empty", True):
            from modules.pipeline_strategies import load_pipeline_data

            data = load_pipeline_data()
        top = get_top_catalyst_stocks(
            data, min_score=float(config.CATALYST_MIN_SCORE), limit=8
        )
        rows = []
        for row in top[:6]:
            fac = ", ".join((row.get("factors") or [])[:3]) or "multi-signal"
            rows.append(
                {
                    "symbol": row["symbol"],
                    "score": float(row.get("score") or 0),
                    "factors": fac,
                    "rvol": row.get("rvol"),
                    "line": f"{row['symbol']} {row['score']:.0f} ({fac})",
                }
            )
        if not rows:
            summary = f"no catalysts ≥ {config.CATALYST_MIN_SCORE:.0f} today"
        else:
            summary = "; ".join(r["line"] for r in rows[:4])
        return {"catalyst_summary": summary, "catalyst_top": rows}
    except Exception as exc:
        return {"catalyst_summary": f"catalyst scan error: {exc}", "catalyst_top": []}


def _fetch_short_interest_snapshot(tickers: list[str]) -> list[dict[str, Any]]:
    """Best-effort short interest via yfinance (cached, max 4 symbols)."""
    symbols = [config.normalize_symbol(t) for t in tickers if t][:4]
    if not symbols:
        return []
    cache_key = "si:" + ",".join(sorted(symbols))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    rows: list[dict[str, Any]] = []
    try:
        import yfinance as yf

        for sym in symbols:
            try:
                info = yf.Ticker(sym).info or {}
            except Exception:
                continue
            pct = info.get("shortPercentOfFloat") or info.get("short_percent_of_float")
            ratio = info.get("shortRatio") or info.get("short_ratio")
            if pct is None and ratio is None:
                continue
            try:
                pct_f = float(pct) * 100.0 if pct is not None and float(pct) <= 1.0 else float(pct)
            except (TypeError, ValueError):
                pct_f = None
            line_parts = [sym]
            if pct_f is not None:
                line_parts.append(f"SI {pct_f:.1f}% float")
            if ratio is not None:
                try:
                    line_parts.append(f"days-to-cover {float(ratio):.1f}")
                except (TypeError, ValueError):
                    pass
            rows.append(
                {
                    "ticker": sym,
                    "short_pct_float": pct_f,
                    "short_ratio": ratio,
                    "line": " ".join(line_parts),
                }
            )
    except ImportError:
        pass
    return _cache_put(cache_key, rows)


def _fetch_options_activity_snapshot(tickers: list[str]) -> list[dict[str, Any]]:
    """Nearest-expiry put/call volume snapshot (cached, max 2 symbols)."""
    symbols = [config.normalize_symbol(t) for t in tickers if t][:2]
    if not symbols:
        return []
    cache_key = "opt:" + ",".join(sorted(symbols))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    rows: list[dict[str, Any]] = []
    try:
        import yfinance as yf

        for sym in symbols:
            try:
                t = yf.Ticker(sym)
                expiries = list(t.options or [])
                if not expiries:
                    continue
                chain = t.option_chain(expiries[0])
                put_vol = int(chain.puts["volume"].fillna(0).sum())
                call_vol = int(chain.calls["volume"].fillna(0).sum())
                total = put_vol + call_vol
                if total < 100:
                    continue
                pc = round(put_vol / call_vol, 2) if call_vol > 0 else None
                bias = "put-heavy" if pc and pc > 1.2 else ("call-heavy" if pc and pc < 0.8 else "balanced")
                rows.append(
                    {
                        "ticker": sym,
                        "expiry": expiries[0],
                        "put_volume": put_vol,
                        "call_volume": call_vol,
                        "put_call_ratio": pc,
                        "bias": bias,
                        "line": f"{sym} P/C {pc or 'n/a'} vol {total:,} ({bias}, exp {expiries[0]})",
                    }
                )
            except Exception:
                continue
    except ImportError:
        pass
    return _cache_put(cache_key, rows)


def _build_options_flow_context(data, regime: str, vol: str) -> dict[str, Any]:
    """Unusual options activity (yfinance) + equity RVOL spikes."""
    out: dict[str, Any] = {
        "options_flow_summary": "no options flow data",
        "unusual_equity_volume": [],
    }
    opt_tickers = [config.SPY_BOT_SYMBOL]
    if config.effective_rvol_scanner_enabled() and data is not None and not getattr(data, "empty", True):
        try:
            from modules.volume_analysis import get_high_rvol_stocks

            for row in get_high_rvol_stocks(data, min_rvol=2.5, limit=3):
                sym = str(row["symbol"])
                out["unusual_equity_volume"].append(
                    {"symbol": sym, "rvol": float(row.get("rvol", 0)), "line": f"{sym} RVOL {float(row.get('rvol', 0)):.1f}x"}
                )
                if sym not in opt_tickers:
                    opt_tickers.append(sym)
        except Exception as exc:
            logger.debug("high-RVOL equity flow annotation unavailable: %s", exc)
    try:
        from modules.insider_signal_handler import get_short_candidate_tickers

        for sym in get_short_candidate_tickers()[:2]:
            if sym not in opt_tickers:
                opt_tickers.append(sym)
    except Exception as exc:
        logger.debug("short-candidate tickers for options flow unavailable: %s", exc)
    options_rows = _fetch_options_activity_snapshot(opt_tickers)
    out["unusual_options_activity"] = options_rows
    parts: list[str] = []
    if options_rows:
        parts.extend(r["line"] for r in options_rows[:3])
        out["options_flow_available"] = True
    else:
        out["options_flow_available"] = False
    rvol_lines = [r["line"] for r in out.get("unusual_equity_volume") or []]
    if rvol_lines:
        out["equity_flow_summary"] = " | ".join(rvol_lines[:4])
        parts.extend(rvol_lines[:2])
    if config.effective_options_sleeve_enabled():
        out["options_sleeve"] = "covered-call sleeve armed (calm regime)"
    out["options_flow_summary"] = " | ".join(parts) if parts else "no unusual options/volume detected"
    if parts:
        out["unusual_activity"] = parts
    return out


def _build_short_interest_context(extra_tickers: list[str] | None = None) -> dict[str, Any]:
    """Short interest from yfinance when available; insider sell watch as fallback."""
    watch: list[str] = list(extra_tickers or [])
    try:
        from modules.insider_signal_handler import get_short_candidate_tickers

        for sym in get_short_candidate_tickers():
            if sym not in watch:
                watch.append(sym)
    except Exception as exc:
        logger.debug("short-candidate tickers for short-interest watch unavailable: %s", exc)
    if config.SPY_BOT_SYMBOL not in watch:
        watch.insert(0, config.SPY_BOT_SYMBOL)
    si_rows = _fetch_short_interest_snapshot(watch[:5])
    out: dict[str, Any] = {
        "short_interest_available": bool(si_rows),
        "short_interest_rows": si_rows,
    }
    if si_rows:
        out["short_interest_summary"] = "; ".join(r["line"] for r in si_rows[:4])
    elif watch:
        out["short_interest_watch"] = watch[:6]
        out["short_interest_summary"] = (
            f"SI feed unavailable; insider sell watch: {', '.join(watch[:6])}"
        )
    else:
        out["short_interest_summary"] = "no SI feed (paper ETB-only book)"
    return out


def get_thinking_context(
    data,
    regime: str,
    vol: str,
    *,
    news_headlines: str = "",
    news_analysis: dict | None = None,
    include_core: bool = True,
) -> dict[str, Any]:
    """Enriched Ollama context: insider, bubble, news, catalyst, stat arb, options, short interest."""
    ctx: dict[str, Any] = {}
    ctx.update(_build_bubble_context(data, regime, vol))
    ctx.update(_build_technical_context(data))
    ctx.update(_build_stat_arb_context(data, regime))
    ctx.update(_build_insider_thinking_context())
    ctx.update(_build_news_thinking_context(news_headlines, news_analysis=news_analysis))
    ctx.update(_build_catalyst_thinking_context(data))
    ctx.update(_build_options_flow_context(data, regime, vol))
    insider_tickers = [
        str(r.get("ticker") or "")
        for r in (ctx.get("insider_executive_sells") or ctx.get("insider_high_score_sells") or [])[:3]
    ]
    ctx.update(_build_short_interest_context(extra_tickers=insider_tickers))
    if include_core:
        ctx["regime"] = regime
        ctx["vol"] = vol
    return ctx


def format_thinking_context_block(ctx: dict[str, Any], *, max_chars: int = 3200) -> str:
    """Compact text block for LLM prompts — low token, high signal."""
    lines: list[str] = []
    lines.append(f"REGIME: {ctx.get('regime', '?')} | vol={ctx.get('vol', '?')}")
    lines.append(
        f"SPY: {ctx.get('spy_trend', 'n/a')} | VIX: {ctx.get('vix', 'n/a')} {ctx.get('vix_trend', '')}"
    )
    lines.append(f"BUBBLE: {ctx.get('buffett_reading') or ctx.get('bubble_score_100', 'n/a')}")

    cluster_lines = ctx.get("insider_cluster_lines") or [
        r.get("line") for r in (ctx.get("insider_high_score_buys") or ctx.get("insider_cluster_buys") or [])[:3]
    ]
    sell_lines = ctx.get("insider_sell_lines") or [
        r.get("line") for r in (ctx.get("insider_high_score_sells") or ctx.get("insider_executive_sells") or [])[:3]
    ]
    if cluster_lines:
        lines.append("INSIDER_BUYS: " + "; ".join(str(x) for x in cluster_lines[:3]))
    if sell_lines:
        lines.append("INSIDER_SELLS: " + "; ".join(str(x) for x in sell_lines[:3]))
    tier_lines = ctx.get("insider_tier_lines") or []
    if tier_lines:
        lines.append("INSIDER_TIERS: " + "; ".join(str(x) for x in tier_lines[:3]))
    if not cluster_lines and not sell_lines and not tier_lines:
        lines.append(f"INSIDER: {str(ctx.get('insider_summary') or 'none')[:200]}")

    news = str(
        ctx.get("news_catalyst_summary")
        or ctx.get("news_digest")
        or ctx.get("news_summary")
        or ctx.get("top_headline")
        or "none"
    )[:280]
    impact = ctx.get("news_impact_score")
    if impact is not None:
        news += f" (impact={float(impact):.2f})"
    lines.append(f"NEWS: {news}")

    cat = ctx.get("catalyst_summary") or "n/a"
    lines.append(f"CATALYST: {cat}")

    opts = ctx.get("unusual_options_activity") or []
    if opts:
        lines.append("OPTIONS: " + "; ".join(r.get("line", "") for r in opts[:3]))
    elif ctx.get("options_flow_summary"):
        lines.append(f"OPTIONS: {ctx.get('options_flow_summary')}")

    si = ctx.get("short_interest_summary") or "n/a"
    lines.append(f"SHORT_SI: {si}")

    lines.append(f"STAT_ARB: {ctx.get('stat_arb_candidate_summary', 'n/a')}")
    eq = ctx.get("equity_flow_summary")
    if eq:
        lines.append(f"EQ_VOLUME: {eq}")
    tech = ctx.get("technical_summary")
    if tech:
        lines.append(f"TECH: {tech}")
    conv = ctx.get("conviction_score")
    if conv is not None:
        lines.append(f"CONVICTION: {conv}")
    block = "\n".join(lines)
    if len(block) > max_chars:
        block = block[: max_chars - 20] + "\n...(truncated)"
    return block


def _trading_system_prompt(capability: str) -> str:
    book = "paper" if config.PAPER_TRADING or config.paper_only_sleeves_active() else "live"
    caps = {
        "regime_analysis": (
            "Classify regime posture and conviction. "
            "Emit risk_posture, sleeve_bias, and a concrete suggested_action."
        ),
        "tilt_analysis": (
            "Propose sleeve tilts vs VTI that improve Sharpe. "
            "Prefer <=3 material sleeve moves; cite CONTEXT facts only."
        ),
        "risk_signals": (
            "Identify near-term portfolio risks (vol, bubble, crowded beta, drawdown). "
            "Be conservative on live books; prefer cash/hedge over aggression."
        ),
        "stat_arb_pairs": (
            "Score only listed pairs; never invent symbols."
        ),
        "short_validation": (
            "Validate protective shorts; defer to rule-engine when conflict."
        ),
        "weekly_review": (
            "Weekly actionable focus only — no generic advice."
        ),
    }.get(capability, "Provide a precise trading read from CONTEXT.")
    return (
        f"You are a quantitative trading assistant ({capability}) for a {book} portfolio bot.\n"
        f"Task: {caps}\n"
        "Rules:\n"
        "- Use ONLY facts in CONTEXT. Never invent tickers, prices, dates, or events.\n"
        "- Be concise: reasoning max 2 sentences. No generic advice "
        "(no 'monitor markets', 'stay diversified').\n"
        "- suggested_action must be specific and executable "
        "(e.g. 'raise cash 5%, trim SPY 3%', 'long XOM/CVX spread', 'reject SPY short').\n"
        "- signal_strength 0.0–1.0 = trade signal magnitude; confidence = certainty in your read.\n"
        "- If data is missing, say so — do not hallucinate.\n"
        "- Prefer capital preservation when VIX rising or SPY below MA200.\n"
        "Output valid JSON only matching the schema."
    )


def _normalize_structured_output(raw: dict[str, Any], capability: str) -> dict[str, Any]:
    """Ensure unified fields: signal_strength, confidence, suggested_action, reasoning."""
    out = dict(raw)
    out["capability"] = capability

    def _f(key: str, default: float = 0.0) -> float:
        try:
            return round(max(0.0, min(1.0, float(out.get(key, default)))), 3)
        except (TypeError, ValueError):
            return default

    out["signal_strength"] = _f("signal_strength", _f("conviction_score", 0.5))
    out["confidence"] = _f("confidence", 0.5)
    if not str(out.get("suggested_action") or "").strip():
        action = out.get("verdict") or out.get("action") or out.get("headline") or "hold"
        out["suggested_action"] = str(action)[:120]
    if not str(out.get("reasoning") or "").strip():
        out["reasoning"] = str(out.get("summary") or out.get("rationale") or out.get("notes") or "")[:280]
    return out


def _coerce_regime_vol(
    data=None,
    regime: str | None = None,
    vol: str | None = None,
    *,
    sentiment=None,
    infer: bool = False,
) -> tuple[str, str]:
    """Fill missing regime/vol for partial-data / smoke tests.

    Accepts None sentiment or vol without raising. Defaults: regime=unknown,
    vol=normal. Optional ``infer=True`` tries market_context helpers (may be slow).
    """
    del sentiment  # accepted for API compat; inference uses get_sentiment when infer=True
    regime_s = str(regime).strip() if regime is not None else ""
    vol_s = str(vol).strip() if vol is not None else ""

    if infer and (not regime_s or not vol_s) and data is not None and not getattr(data, "empty", True):
        try:
            from modules.market_context import (
                get_market_regime,
                get_sentiment,
                get_volatility,
            )

            sent = None
            try:
                sent = get_sentiment(data)
            except Exception:
                sent = None
            if not vol_s:
                try:
                    inferred_vol = get_volatility(data)
                    vol_s = str(inferred_vol).strip() if inferred_vol is not None else ""
                except Exception:
                    vol_s = ""
            if not regime_s:
                try:
                    inferred = get_market_regime(
                        sent if sent is not None else 0.0, vol_s or "normal"
                    )
                    regime_s = str(inferred).strip() if inferred is not None else ""
                except Exception:
                    regime_s = ""
        except Exception as exc:
            logger.debug("regime/vol inference skipped: %s", exc)

    if not vol_s or vol_s.lower() in ("none", "null", "nan"):
        vol_s = "normal"
    if not regime_s or regime_s.lower() in ("none", "null", "nan"):
        regime_s = "unknown"
    return regime_s, vol_s


def build_market_summary(
    data,
    regime: str | None = None,
    vol: str | None = None,
    *,
    wisdom: dict | None = None,
    top_headline: str | None = None,
    news_headlines: str | list | None = None,
    news_slot: str | None = None,
    base_caps: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Assemble context for the PM-style reasoning prompt.

    ``regime`` / ``vol`` may be None — coerced to unknown/normal (or inferred).
    """
    from modules.pipeline_strategies import _spy_market_up_signal
    from modules.thinking_news import normalize_news_headlines

    regime, vol = _coerce_regime_vol(data, regime, vol)
    if news_headlines is None and config.effective_historical_news_enabled():
        try:
            from modules.historical_news import backtest_headlines_for_summary

            news_headlines = backtest_headlines_for_summary()
        except Exception as exc:
            logger.debug("historical backtest headlines unavailable: %s", exc)

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
    news_text = normalize_news_headlines(news_headlines)
    if news_text:
        headline = news_text.split("\n", 1)[0][:240]
    elif headline == "n/a":
        try:
            from modules.web_sentiment_live import get_live_web_sentiment

            if get_live_web_sentiment() is not None:
                headline = "finance headline mood cached (see web sentiment)"
        except Exception as exc:
            logger.debug("live web sentiment headline fallback unavailable: %s", exc)

    sector = _build_sector_leadership(data, macro_cache)
    summary = {
        "spy_trend": spy_trend,
        "vix": round(vix_val, 1) if vix_val is not None else "n/a",
        "vix_trend": vix_trend,
        "yield_curve": yield_curve,
        "oil_change": _pct_change(oil_series) if not oil_series.empty else 0.0,
        "gold_change": _pct_change(gold_series) if not gold_series.empty else 0.0,
        "macro_sentiment": macro_sentiment,
        "top_headline": str(headline)[:240],
        "news_headlines": news_text,
        "news_slot": news_slot,
        "regime": regime,
        "vol": vol,
        "bot_exposure": bot_exposure,
        "bot_exposure_str": _format_bot_exposure(caps),
        "sector_leadership": sector["leadership_str"],
        "sector_leaders": sector["leaders"],
        "sector_laggards": sector["laggards"],
        "sector_detail": sector["sectors"],
    }
    summary["ai_cycle_phase"] = _infer_ai_cycle_phase(summary)
    summary["vol_overlay_regime"] = _vol_overlay_regime(summary)
    summary["stat_arb_regime"] = _stat_arb_regime(summary)
    summary["crowded_trade_warning"] = _crowded_trade_warning(summary)
    news_analysis_obj: dict[str, Any] | None = None
    if news_text:
        from modules.thinking_news import analyze_news_headlines

        news_analysis_obj = analyze_news_headlines(
            news_text,
            ai_cycle_phase=str(summary.get("ai_cycle_phase") or ""),
        )
        summary["news_themes"] = news_analysis_obj.get("themes")
        summary["news_theme_summary"] = news_analysis_obj.get("theme_summary")
        summary["news_impact_score"] = news_analysis_obj.get("news_impact_score")
        summary["news_ai_tech_context"] = news_analysis_obj.get("ai_tech_context")
        summary["news_digest"] = news_analysis_obj.get("digest_text")
    else:
        summary["news_impact_score"] = 0.0
    summary.update(
        get_thinking_context(
            data,
            regime,
            vol,
            news_headlines=news_text,
            news_analysis=news_analysis_obj,
            include_core=False,
        )
    )
    try:
        from modules.risk_management import compute_conviction_score

        conviction = compute_conviction_score(
            config.SPY_BOT_SYMBOL,
            data,
            regime,
            sleeve="stat_arb_equity",
        )
        summary["conviction_score"] = round(float(conviction), 3)
    except Exception:
        summary.setdefault("conviction_score", None)

    # Markov regime transition probs (prompt context only — no sizing impact).
    try:
        from modules.markov_regime import compute_markov_regime

        spy_prices = None
        if data is not None and hasattr(data, "columns") and spy_sym in data.columns:
            spy_prices = data[spy_sym]
        summary["markov_regime"] = compute_markov_regime(spy_prices)
    except Exception as exc:
        logger.debug("markov regime context skipped: %s", exc)
        summary.setdefault("markov_regime", None)
    return summary


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


def _news_theme_active(summary: dict, key: str) -> bool:
    themes = summary.get("news_themes") or {}
    block = themes.get(key) if isinstance(themes.get(key), dict) else {}
    return bool(block.get("active"))


def _news_impact(summary: dict) -> float:
    return float(summary.get("news_impact_score") or 0.0)


def _news_text_blob(summary: dict) -> str:
    parts = [
        summary.get("news_headlines"),
        summary.get("news_theme_summary"),
        summary.get("top_headline"),
    ]
    return " ".join(str(p or "") for p in parts).lower()


def _apply_news_cap_deltas(deltas: dict[str, float], summary: dict, conf: float) -> None:
    """Headline themes scaled by news_impact_score (0-1)."""
    impact = _news_impact(summary)
    if impact < 0.25:
        return
    scale = impact * conf
    geo = _news_theme_active(summary, "geopolitics")
    energy = _news_theme_active(summary, "sector_energy")
    liq = _news_theme_active(summary, "liquidity")
    policy = _news_theme_active(summary, "policy")
    tech = _news_theme_active(summary, "sector_tech")
    phase = str(summary.get("ai_cycle_phase") or "")

    if geo or energy:
        deltas["nyse"] += 0.05 * scale
        deltas["spy"] -= 0.03 * scale
        if float(summary.get("gold_change") or 0.0) >= 0.0:
            deltas["metal"] += 0.02 * scale
    if liq and policy and geo:
        deltas["vti_core"] += 0.03 * scale
        deltas["cash_buffer"] += 0.02 * scale
    elif liq and policy and not geo:
        deltas["vti_core"] += 0.03 * scale
        deltas["cash_buffer"] -= 0.02 * scale
    if tech and ("mid-cycle" in phase or "ai" in phase.lower()):
        if policy and geo:
            deltas["vti_core"] += 0.03 * scale
            deltas["cash_buffer"] += 0.02 * scale
            deltas["spy"] -= 0.02 * scale
        elif policy and not geo:
            deltas["spy"] += 0.02 * scale
            deltas["vti_core"] -= 0.02 * scale


def _rule_based_cap_deltas(summary: dict, confidence: float) -> dict[str, float]:
    """Direct sleeve cap deltas from macro signals (matches PM tilt intent)."""
    deltas = {k: 0.0 for k in _CAP_KEYS}
    conf = max(0.35, min(1.0, float(confidence)))
    oil = float(summary.get("oil_change") or 0.0)
    gold = float(summary.get("gold_change") or 0.0)
    vix = summary.get("vix")
    vix_f = float(vix) if vix not in (None, "n/a") else 18.0
    headline = _news_text_blob(summary) or str(summary.get("top_headline", "")).lower()
    geo = any(k in headline for k in _GEO_KEYWORDS) or _news_theme_active(summary, "geopolitics")

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

    vol_regime = str(summary.get("vol_overlay_regime") or _vol_overlay_regime(summary))
    stat_regime = str(summary.get("stat_arb_regime") or _stat_arb_regime(summary))
    crowded = str(summary.get("crowded_trade_warning") or "")

    if "elevated" in vol_regime.lower() or vix_f >= 22:
        deltas["spy"] -= 0.05 * conf
        deltas["nyse"] -= 0.03 * conf
        deltas["cash_buffer"] += 0.06 * conf
        deltas["vti_core"] += 0.02 * conf

    if "supportive" in stat_regime.lower() and vix_f <= 22:
        deltas["crypto"] += 0.04 * conf
        deltas["cash_buffer"] -= 0.02 * conf

    if "hostile" in stat_regime.lower() or "compressed" in stat_regime.lower():
        deltas["crypto"] -= 0.03 * conf
        deltas["vti_core"] += 0.02 * conf

    if crowded.startswith("CROWDED"):
        deltas["spy"] -= 0.04 * conf
        deltas["vti_core"] += 0.03 * conf
        deltas["cash_buffer"] += 0.02 * conf

    if "calm" in vol_regime.lower() and "above MA" in str(summary.get("spy_trend", "")):
        tech_leading = any(
            any(k in str(r.get("sector", "")) for k in ("Tech", "Semis", "AI"))
            for r in (summary.get("sector_leaders") or [])[:2]
        )
        if tech_leading and not crowded.startswith("CROWDED"):
            deltas["spy"] += 0.04 * conf
            deltas["vti_core"] -= 0.03 * conf

    _apply_news_cap_deltas(deltas, summary, conf)

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
    impact = _news_impact(summary)
    if impact >= 0.4:
        geo = _news_theme_active(summary, "geopolitics")
        liq = _news_theme_active(summary, "liquidity")
        tech = _news_theme_active(summary, "sector_tech")
        if geo and liq:
            return (
                "Oil supply shock meets policy liquidity rhetoric — crowd split on risk-on vs hedges"
            )
        if geo and tech:
            return (
                "AI beta still crowded while geopolitical headlines rise — asymmetric de-risk vs chase"
            )
    oil = float(summary.get("oil_change") or 0.0)
    gold = float(summary.get("gold_change") or 0.0)
    vix = summary.get("vix")
    vix_f = float(vix) if vix not in (None, "n/a") else 0.0
    spy_trend = str(summary.get("spy_trend", ""))
    if "below MA" in spy_trend and vix_f >= 20:
        return "Crowd still long beta while trend breaks — asymmetric downside if vol persists"
    leaders = summary.get("sector_leaders") or []
    if leaders and any("Semis" in str(r.get("sector", "")) for r in leaders[:1]):
        if "above MA" in spy_trend:
            return "Semis/AI still leading — crowd under-allocates infra capex vs datacenter demand"
    if any("Tech" in str(r.get("sector", "")) for r in leaders[:1]) and "above MA" in spy_trend:
        return "AI/Tech leadership persists — laggards forced to chase beta on dips"
    if oil >= 4.0 and "above MA" in spy_trend:
        return "Equities complacent vs energy shock — crowd under-hedged to inflation tail"
    if gold >= 3.0 and vix_f >= config.MACRO_VIX_SAFE_HAVEN_MIN:
        return "Safe-haven bid rising while equities hold — hedgers early, consensus late"
    if "rising" in str(summary.get("vix_trend", "")) and "above MA" in spy_trend:
        if gold < 0.0:
            return "Vol rising into strength with gold falling — liquidity stress, not safe-haven bid"
        return "Vol rising into strength — complacency gap; VTI passive misses vol-overlay hedge alpha"
    crowded = str(summary.get("crowded_trade_warning") or _crowded_trade_warning(summary))
    if crowded.startswith("CROWDED"):
        return crowded.replace("CROWDED: ", "")
    stat_regime = str(summary.get("stat_arb_regime") or _stat_arb_regime(summary))
    if "supportive" in stat_regime.lower():
        return "Stat-arb spreads favorable vs passive VTI — active pairs add uncorrelated alpha"
    return "Range-bound chop — edge in selective tilts vs VTI, not max beta"


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
    impact = _news_impact(summary) if summary else 0.0
    if impact >= 0.25 and summary:
        theme = str(summary.get("news_theme_summary") or "")[:100]
        narrative = _infer_narrative(summary)
        return f"news_impact={impact:.2f} | {theme} | {narrative} -> {alloc}"
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
    return _pm_system_prompt()


def get_thinking_status_snapshot() -> dict[str, object]:
    """Compact audit snapshot for status.py / monitoring / dashboard."""
    cached = read_json_file(OUTPUT_FILE) or {}
    approval = read_json_file(APPROVAL_FILE) or {}
    pending_id = cached.get("decision_id") if cached else None
    approved = bool(pending_id and is_thinking_tilt_approved(cached)) if pending_id else False
    ollama_ok = False
    try:
        ollama_ok = bool(ollama_available())
    except Exception:
        ollama_ok = False
    return {
        "master_enabled": bool(getattr(config, "THINKING_ENGINE_ENABLED", True)),
        "env_enabled": bool(config.PAPER_THINKING_ENGINE_ENABLED),
        "live_env_enabled": bool(getattr(config, "LIVE_THINKING_ENGINE_ENABLED", False)),
        "effective_enabled": bool(config.effective_thinking_engine_enabled()),
        "ollama_ok": ollama_ok,
        "last_timestamp": cached.get("timestamp"),
        "last_regime": cached.get("regime"),
        "last_confidence": cached.get("confidence"),
        "last_source": cached.get("source"),
        "last_model": cached.get("model"),
        "validation_score": cached.get("validation_score"),
        "narrative_snip": str(cached.get("narrative") or "")[:100],
        "sector_view_snip": str(cached.get("sector_view") or "")[:120],
        "regime_signal": str(cached.get("regime_signal") or "")[:80],
        "tilt_signal": str(cached.get("tilt_signal") or "")[:80],
        "risk_signal": str(cached.get("risk_signal") or "")[:80],
        "ai_cycle_phase": cached.get("ai_cycle_phase"),
        "manual_review_required": bool(cached.get("manual_review_required")),
        "pending_decision_id": pending_id,
        "approved": approved,
        "has_approval_file": bool(approval),
        "fallback": str(cached.get("source") or "").startswith("heuristic")
        or str(cached.get("source") or "") == "unavailable",
    }


def format_recommended_tilt(tilt: dict | None) -> str:
    """One-line summary of suggested_tilt weights."""
    if not tilt:
        return "n/a"
    items = sorted((tilt or {}).items(), key=lambda kv: float(kv[1]), reverse=True)
    return " | ".join(f"{k} {float(v):.0%}" for k, v in items[:6])


def evaluate_live_apply_status(
    thinking_result: dict | None = None,
    *,
    equity: float | None = None,
) -> dict[str, object]:
    """Preview whether the latest decision would apply on live under production safety rules."""
    cached = dict(thinking_result or read_json_file(OUTPUT_FILE) or {})
    conf_raw = cached.get("confidence")
    conf = float(conf_raw) if conf_raw is not None else None
    val_score = cached.get("validation_score")

    if config.thinking_manual_approval_required():
        if cached and is_thinking_tilt_approved(cached):
            approval_status = "APPROVED"
        elif cached.get("decision_id"):
            approval_status = f"PENDING (decision {cached.get('decision_id')})"
        else:
            approval_status = "PENDING (no decision_id)"
    else:
        approval_status = "Not required"

    out: dict[str, object] = {
        "timestamp": cached.get("timestamp"),
        "regime": cached.get("regime"),
        "narrative": str(cached.get("narrative") or "n/a").strip() or "n/a",
        "asymmetry": str(cached.get("asymmetry") or "n/a").strip() or "n/a",
        "recommended_tilt": format_recommended_tilt(cached.get("suggested_tilt")),
        "confidence": conf,
        "confidence_pct": f"{conf:.0%}" if conf is not None else "n/a",
        "validation_score": val_score,
        "validation_label": str(val_score) if val_score is not None else "n/a",
        "approval_status": approval_status,
        "news_slot": cached.get("news_slot"),
        "news_summary": cached.get("news_summary"),
        "news_impact_score": cached.get("news_impact_score"),
        "would_apply": False,
        "would_apply_label": "No",
        "block_reason": "",
    }

    if not cached.get("timestamp"):
        out["block_reason"] = "No thinking_engine_last.json decision yet"
        return out

    blockers: list[str] = []

    live_book = not config.PAPER_TRADING and config.ALLOW_LIVE_TRADING
    if live_book or (not config.PAPER_TRADING and not config.paper_only_sleeves_active()):
        if not getattr(config, "LIVE_THINKING_ENGINE_ENABLED", False):
            blockers.append(
                "live thinking OFF (set LIVE_THINKING_ENGINE_ENABLED=true to opt in)"
            )
        elif not config.effective_thinking_engine_enabled():
            blockers.append("thinking engine disabled (master or live flag)")

    tripped, trip_reason = thinking_daily_loss_tripped(equity)
    if tripped:
        blockers.append(trip_reason)

    if config.thinking_manual_approval_required() and not is_thinking_tilt_approved(cached):
        blockers.append("manual approval required")

    narrative = str(cached.get("narrative") or "").strip()
    asymmetry = str(cached.get("asymmetry") or "").strip()
    min_conf = 0.60 if asymmetry else 0.65
    narrative_ok = (
        len(narrative) >= 15
        and (
            (conf or 0) >= 0.75
            or bool(asymmetry)
            or (len(narrative) >= 20 and "range-bound" not in narrative.lower())
        )
    )
    if conf is None or conf < min_conf or not narrative_ok:
        blockers.append("insufficient confidence or weak narrative")

    if cached.get("validation_ok") is False and not cached.get("validation_recovered"):
        errs = cached.get("validation_errors") or []
        detail = f": {errs[0]}" if errs else ""
        blockers.append(f"validation failed{detail}")

    base_caps = dict(config.fund_allocation_pct())
    if equity is not None:
        if float(equity) < config.SMALL_ACCOUNT_EQUITY_THRESHOLD:
            base_caps["vti_core"] = config.SMALL_ACCOUNT_VTI_CORE_PCT
        else:
            base_caps["vti_core"] = config.vti_core_allocation_pct(float(equity))

    _merged, deltas, log_line = apply_thinking_tilt_to_caps(
        base_caps,
        cached.get("suggested_tilt") or {},
        confidence=float(conf or 0.65),
        market_summary=cached.get("market_summary"),
        equity=equity,
        max_sleeve_delta=config.LIVE_THINKING_MAX_SLEEVE_DELTA,
        allow_small_account=True,
    )
    material = {k: v for k, v in deltas.items() if abs(float(v)) >= 0.001}
    if not material:
        if log_line:
            blockers.append(log_line.replace("Thinking blocked: ", "").strip())
        else:
            blockers.append("tilt produced no material cap change")

    if blockers:
        out["would_apply"] = False
        out["would_apply_label"] = "No"
        out["block_reason"] = blockers[0] if len(blockers) == 1 else "; ".join(blockers[:4])
    else:
        out["would_apply"] = True
        out["would_apply_label"] = "Yes"
        out["block_reason"] = "Passes all live safety checks"

    return out


def _load_previous_tilt_full() -> dict | None:
    cached = read_json_file(OUTPUT_FILE)
    if not cached:
        return None
    return {
        "tilt": cached.get("suggested_tilt"),
        "regime": cached.get("regime"),
        "timestamp": cached.get("timestamp"),
        "narrative": cached.get("narrative"),
        "sector_view": cached.get("sector_view"),
        "ai_cycle_phase": cached.get("ai_cycle_phase"),
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


def _validation_cache_key(result: dict[str, Any], market_summary: dict) -> str:
    prev = _load_previous_tilt()
    payload = {
        "tilt": result.get("suggested_tilt"),
        "narrative": result.get("narrative"),
        "asymmetry": result.get("asymmetry"),
        "conf": result.get("confidence"),
        "gold_chg": market_summary.get("gold_change"),
        "vix_trend": market_summary.get("vix_trend"),
        "regime": market_summary.get("regime"),
        "prev": prev,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:20]


def _store_validation_cache(key: str, valid: bool, errors: list[str]) -> None:
    if len(_VALIDATION_RESULT_CACHE) >= _VALIDATION_CACHE_MAX:
        oldest = min(_VALIDATION_RESULT_CACHE, key=lambda k: _VALIDATION_RESULT_CACHE[k][0])
        del _VALIDATION_RESULT_CACHE[oldest]
    _VALIDATION_RESULT_CACHE[key] = (time.monotonic(), valid, tuple(errors))


def _validate_thinking_quality(
    result: dict[str, Any],
    market_summary: dict,
) -> tuple[bool, list[str]]:
    """Post-process validator — reject contradictory or low-quality tilts."""
    cache_key = _validation_cache_key(result, market_summary)
    now = time.monotonic()
    cached = _VALIDATION_RESULT_CACHE.get(cache_key)
    if cached and now - cached[0] < _VALIDATION_CACHE_TTL_SEC:
        return cached[1], list(cached[2])

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

    sector_view = str(result.get("sector_view") or "").lower()
    ai_phase = str(
        result.get("ai_cycle_phase") or market_summary.get("ai_cycle_phase") or ""
    ).lower()
    combined_text = f"{narrative} {asymmetry} {sector_view} {ai_phase}"
    tech_tilt = float(tilt.get("spy", 0.0))
    risk_on = float(tilt.get("vti", 0.0)) + tech_tilt

    if not sector_view and not any(k in combined_text for k in _AI_CYCLE_KEYWORDS):
        errors.append("missing sector/AI cycle awareness (SECTOR_VIEW or AI terms)")

    if any(p in ai_phase for p in ("late-cycle", "exhaustion", "rotation risk")):
        if tech_tilt > 0.38 and float(tilt.get("cash", 0.0)) < 0.10:
            if "continuation" not in combined_text and "momentum" not in combined_text:
                errors.append("late-cycle/rotation phase but aggressive tech tilt without cash buffer")

    if "exhaustion" in ai_phase and risk_on > 0.78 and float(tilt.get("cash", 0.0)) < 0.15:
        errors.append("exhaustion phase but equity-heavy tilt without defensive cash")

    leaders = market_summary.get("sector_leaders") or []
    tech_leading = any(
        any(k in str(r.get("sector", "")) for k in ("Tech", "Semis", "AI"))
        for r in leaders[:2]
    )
    if (
        tech_leading
        and "above MA" in str(market_summary.get("spy_trend", ""))
        and "exhaustion" not in ai_phase
        and "rotation" not in ai_phase
        and tech_tilt < 0.06
        and float(tilt.get("vti", 0.0)) < 0.45
    ):
        errors.append("tech/AI sector leading but growth sleeves underweight vs regime")

    if sector_view and "underweight tech" in sector_view and tech_tilt > 0.30:
        errors.append("SECTOR_VIEW underweights tech but RECOMMENDED_TILT is tech-heavy")

    rationale = str(result.get("tilt_rationale") or "")
    rq = _rationale_quality_score(result)
    if rq < 0.45:
        errors.append("TILT_RATIONALE missing per-sleeve percentage justification")
    elif result.get("asymmetry"):
        asym_snip = str(result.get("asymmetry"))[:40].lower()
        if asym_snip and asym_snip not in rationale.lower() and "asymmetry" not in rationale.lower():
            errors.append("TILT_RATIONALE not linked to ASYMMETRY")

    valid = len(errors) == 0
    _store_validation_cache(cache_key, valid, errors)
    return valid, errors


def thinking_daily_loss_tripped(equity: float | None) -> tuple[bool, str]:
    """True when intraday loss exceeds configured limit (delegates to trading_safety)."""
    from modules.trading_safety import daily_loss_circuit_tripped

    tripped, reason, _ = daily_loss_circuit_tripped(equity)
    return tripped, reason


def _update_daily_equity_anchor(equity: float | None) -> None:
    from modules.trading_safety import update_daily_equity_anchor

    update_daily_equity_anchor(equity)


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
        from_reasoning = _extract_labeled_block(str(out.get("reasoning") or ""), "NARRATIVE")
        if from_reasoning and not _looks_like_meta_narrative(from_reasoning):
            out["narrative"] = from_reasoning
        else:
            out["narrative"] = _infer_narrative(market_summary)
    if not out.get("asymmetry"):
        out["asymmetry"] = _infer_asymmetry(market_summary)
    if not out.get("sector_view"):
        out["sector_view"] = (
            f"Leaders: {market_summary.get('sector_leadership', 'n/a')} | "
            f"phase: {market_summary.get('ai_cycle_phase', 'n/a')}"
        )
    if not out.get("ai_cycle_phase"):
        out["ai_cycle_phase"] = market_summary.get("ai_cycle_phase")
    if not out.get("regime_signal"):
        posture = "neutral"
        spy = str(market_summary.get("spy_trend") or "").lower()
        vix = float(market_summary.get("vix") or 0)
        if "below" in spy or vix >= 22:
            posture = "defensive"
        elif "above" in spy and vix and vix <= 18:
            posture = "risk-on"
        out["regime_signal"] = f"{posture} | strength {float(out.get('confidence') or 0.5):.2f} | heuristic"
    if not out.get("tilt_signal"):
        top = sorted(
            (out.get("suggested_tilt") or tilt or {}).items(),
            key=lambda kv: float(kv[1]),
            reverse=True,
        )
        add = top[0][0] if top else "vti"
        cut = "cash" if add != "cash" else "spy"
        out["tilt_signal"] = f"{add} / {cut} | conviction {float(out.get('confidence') or 0.5):.2f}"
    if not out.get("risk_signal"):
        vix = float(market_summary.get("vix") or 0)
        level = "high" if vix >= 22 else ("medium" if vix >= 16 else "low")
        out["risk_signal"] = f"{level} | VIX {vix:.1f} / trend {market_summary.get('vix_trend', 'n/a')}"
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
        log_subsystem_warning(
            "thinking_engine",
            "Thinking validation failed: " + "; ".join(val_errors),
        )
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


def _infer_narrative_from_news(summary: dict) -> str:
    impact = _news_impact(summary)
    if impact < 0.35:
        return ""
    themes = summary.get("news_themes") or {}
    phase = str(summary.get("ai_cycle_phase") or "")
    geo = _news_theme_active(summary, "geopolitics")
    liq = _news_theme_active(summary, "liquidity")
    policy = _news_theme_active(summary, "policy")
    energy = _news_theme_active(summary, "sector_energy")
    tech = _news_theme_active(summary, "sector_tech")

    if geo and (liq or policy):
        return (
            "Policy liquidity push into a geopolitical oil shock — barbell VTI/cash with energy overlay, trim beta"
        )
    if geo and energy:
        return "Middle East supply risk dominates — energy up, AI beta vulnerable until vol clears"
    if policy and tech and ("mid-cycle" in phase or "ai" in phase.lower()):
        return (
            "Policy/tariff headlines whipsaw AI boom — stay long winners but cut crowded beta size"
        )
    if liq and not geo:
        return "Liquidity-friendly policy headline — modest risk-on but respect VTI core"
    if geo:
        return "Geopolitical headline risk — raise cash/VTI, add energy/metal hedges"
    return ""


def _infer_narrative(summary: dict | None) -> str:
    if not summary:
        return "Macro reassessment"
    news_narrative = _infer_narrative_from_news(summary)
    if news_narrative:
        return news_narrative
    headline = _news_text_blob(summary) or str(summary.get("top_headline", "")).lower()
    oil = float(summary.get("oil_change") or 0.0)
    gold = float(summary.get("gold_change") or 0.0)
    vix = summary.get("vix")
    vix_f = float(vix) if vix not in (None, "n/a") else 0.0
    if any(k in headline for k in _GEO_KEYWORDS):
        return "Geopolitical tension / Middle East risk"
    if "flood" in headline and "market" in headline:
        return "Policy liquidity headline — reassess risk-on vs supply shock"
    if oil >= config.MACRO_OIL_SURGE_PCT * 100 * 0.5:
        return "Oil shock / energy stress"
    if gold >= config.MACRO_GLD_SURGE_PCT * 100:
        return "Risk-off / safe-haven bid"
    if vix_f >= config.MACRO_VIX_SAFE_HAVEN_MIN:
        if gold < 0.0:
            return "Elevated vol with gold falling — liquidity stress"
        return "Risk-off / safe-haven bid"
    if "below MA" in str(summary.get("spy_trend", "")):
        return "Equity trend weakening — AI beta vulnerable"
    phase = str(summary.get("ai_cycle_phase") or "")
    if "mid-cycle" in phase:
        return "Mid-cycle AI leadership — stay with winners, trim laggards"
    return "Range-bound chop — edge in selective sector tilts, not max risk"


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
    max_delta = (
        float(max_sleeve_delta)
        if max_sleeve_delta is not None
        else config.effective_thinking_max_sleeve_delta()
    )
    if _should_consolidate_news_deltas(
        cap_deltas,
        market_summary,
        live_like=allow_small_account or max_sleeve_delta is not None,
    ):
        before = dict(cap_deltas)
        cap_deltas = _consolidate_news_deltas(
            cap_deltas,
            market_summary,
            max_per_sleeve=max_delta,
        )
        if cap_deltas != before:
            _audit_thinking(
                "news_deltas_consolidated",
                news_impact_score=_news_impact(market_summary),
                before={k: round(v, 4) for k, v in before.items() if abs(v) > 0.001},
                after={k: round(v, 4) for k, v in cap_deltas.items() if abs(v) > 0.001},
            )

    cap_deltas = _clamp_tilt_deltas(cap_deltas, max_per_sleeve=max_delta)
    ok, reason = _tilt_deltas_reasonable(cap_deltas)
    if not ok:
        logger.info("Thinking engine: tilt rejected (%s)", reason)
        _audit_thinking("tilt_rejected", reason=reason, deltas=cap_deltas)
        return dict(base_caps), {}, f"Thinking blocked: {reason}"

    merged = _merge_caps_from_deltas(base, cap_deltas)
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
        **_thinking_tilt_apply_kwargs(equity),
    )


def derive_heuristic_tilt(summary: dict) -> dict[str, float]:
    """Normalized target weights for LLM nudge layer (backtest/live). VTI-beat aware."""
    phase = str(summary.get("ai_cycle_phase") or "")
    vol_regime = str(summary.get("vol_overlay_regime") or _vol_overlay_regime(summary))
    stat_regime = str(summary.get("stat_arb_regime") or _stat_arb_regime(summary))
    crowded = str(summary.get("crowded_trade_warning") or "")
    tech_leading = any(
        any(k in str(r.get("sector", "")) for k in ("Tech", "Semis", "AI"))
        for r in (summary.get("sector_leaders") or [])[:2]
    )
    vix = summary.get("vix")
    vix_f = float(vix) if vix not in (None, "n/a") else 18.0

    vti_w = 0.52
    if crowded.startswith("CROWDED") or "elevated" in vol_regime.lower() or vix_f >= 22:
        vti_w = 0.58
    elif tech_leading and "rotation" not in phase and vix_f <= 18:
        if "supportive" in stat_regime.lower() and "calm" in vol_regime.lower():
            vti_w = 0.46
        else:
            vti_w = 0.50

    spy_w = 0.16 if tech_leading and not crowded.startswith("CROWDED") else 0.10
    if "late-cycle" in phase or "exhaustion" in phase:
        spy_w = max(0.08, spy_w - 0.04)

    cash_w = 0.12 if vix_f >= 20 or "elevated" in vol_regime.lower() else 0.08
    crypto_w = 0.10 if "supportive" in stat_regime.lower() and vix_f <= 22 else 0.06

    tilt = {
        "vti": vti_w,
        "spy": spy_w,
        "crypto": crypto_w,
        "energy": 0.06 if "rotation" in phase else 0.05,
        "gold": 0.05 if _gold_momentum_ok(summary) else 0.0,
        "cash": cash_w,
        "bonds": 0.05 if _gold_momentum_ok(summary) else 0.08,
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
    if _news_impact(summary) >= 0.35:
        tilt["cash"] += 0.02
        if _news_theme_active(summary, "geopolitics"):
            tilt["energy"] += 0.03
            tilt["spy"] = max(0.06, float(tilt.get("spy", 0.0)) - 0.03)
    return _normalize_tilt(tilt)


def build_backtest_thinking_result(
    data,
    regime: str,
    vol: str,
    *,
    news_headlines: str | list | None = None,
    news_slot: str | None = None,
    force_decision: bool = True,
) -> dict:
    """Decisive thinking proxy for historical backtests (always produces a tilt)."""
    summary = build_market_summary(
        data,
        regime,
        vol,
        news_headlines=news_headlines,
        news_slot=news_slot,
    )
    tilt = derive_heuristic_tilt(summary)
    narrative = _infer_narrative(summary)
    asymmetry = _infer_asymmetry(summary)
    news_impact = _news_impact(summary)
    conf = 0.78 if force_decision else 0.70
    if news_impact >= 0.35:
        conf = round(min(0.88, conf + 0.10 * news_impact), 2)
    base = {
        "reasoning": f"Force-decision proxy: {narrative}",
        "narrative": narrative,
        "asymmetry": asymmetry,
        "sector_view": (
            f"Leaders: {summary.get('sector_leadership', 'n/a')} | "
            f"phase: {summary.get('ai_cycle_phase', 'n/a')} | "
            f"vol: {summary.get('vol_overlay_regime', 'n/a')} | "
            f"stat-arb: {summary.get('stat_arb_regime', 'n/a')}"
        ),
        "ai_cycle_phase": summary.get("ai_cycle_phase"),
        "risks": ["Regime shift", "Vol spike"],
        "opportunities": ["Sleeve tilt edge", "Macro hedge"],
        "justification": asymmetry,
        "suggested_tilt": tilt,
        "confidence": conf,
        "model": "heuristic-backtest",
        "source": "force_decision",
        "market_summary": summary,
        "tilt_rationale": _infer_tilt_rationale(summary, tilt, asymmetry),
        "news_impact_score": news_impact,
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
    news_impact = _news_impact(market_summary)
    confidence = round(min(0.88, 0.70 + 0.18 * news_impact), 2) if news_impact >= 0.35 else 0.70
    base = {
        "reasoning": f"Rule-based fallback ({reason}): {_infer_narrative(market_summary)}",
        "narrative": _infer_narrative(market_summary),
        "asymmetry": _infer_asymmetry(market_summary),
        "risks": [],
        "opportunities": [],
        "justification": reason,
        "suggested_tilt": derive_heuristic_tilt(market_summary),
        "confidence": confidence,
        "model": reason,
        "source": "heuristic",
        "parse_quality": 0.0,
        "market_summary": market_summary,
        "news_impact_score": news_impact,
    }
    return _finalize_thinking_result(base, market_summary, force_decision=True)


def _record_thinking_run(
    regime: str,
    result: dict,
    *,
    news_summary: str | None = None,
    news_slot: str | None = None,
) -> None:
    now = datetime.datetime.now().isoformat()
    write_json_file(
        STATE_FILE,
        {
            "last_date": datetime.date.today().isoformat(),
            "last_regime": regime,
            "last_run_at": now,
            "model": result.get("model", config.OLLAMA_MODEL),
            "source": result.get("source", "llm"),
            "last_news_slot": news_slot,
        },
    )
    persist_thinking_last(result, regime=regime)
    audit_fields: dict[str, Any] = {
        "regime": regime,
        "source": result.get("source"),
        "model": result.get("model"),
        "confidence": result.get("confidence"),
        "validation_ok": result.get("validation_ok"),
        "validation_score": result.get("validation_score"),
        "narrative": (str(result.get("narrative") or "")[:160]),
        "suggested_tilt": result.get("suggested_tilt"),
    }
    if news_slot:
        audit_fields["news_slot"] = news_slot
    if news_summary:
        audit_fields["news_summary"] = news_summary[:800]
    if result.get("news_impact_score") is not None:
        audit_fields["news_impact_score"] = result.get("news_impact_score")
    _audit_thinking("reasoning_complete", **audit_fields)


def thinking_model_chain(*, fast_only: bool = False) -> list[str]:
    """Primary model + fallbacks resolved against installed Ollama tags."""
    return resolve_model_chain(fast_only=fast_only)


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
    retries: int | None = None,
) -> tuple[str, str]:
    """Return (full_text_for_logs, answer_text_for_parsing)."""
    return ollama_complete(
        prompt,
        system=system,
        model=model,
        timeout_sec=_thinking_timeout_sec(timeout_sec),
        json_mode=False,
        # Thinking-engine owns the retry loop; avoid nested 3x3 attempts.
        retries=1 if retries is None else retries,
    )


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
    alias = dict(_TILT_ALIASES)
    for key, val in raw.items():
        k = alias.get(str(key).lower(), str(key).lower())
        if k not in base:
            continue
        try:
            val = float(val)
            if val > 1.0 and val <= 100.0:
                val = val / 100.0
            base[k] += val
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
                str(k).lower() in _TILT_KEYS or str(k).lower() in _TILT_ALIASES
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
        elif key == "sector_view":
            result["sector_view"] = raw.splitlines()[0] if raw else ""
        elif key == "ai_cycle_phase":
            result["ai_cycle_phase"] = raw.splitlines()[0] if raw else ""
        elif key == "regime_signal":
            result["regime_signal"] = raw.splitlines()[0] if raw else ""
        elif key == "tilt_signal":
            result["tilt_signal"] = raw.splitlines()[0] if raw else ""
        elif key == "risk_signal":
            result["risk_signal"] = raw.splitlines()[0] if raw else ""
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


def _format_markov_prompt_line(market_summary: dict) -> str:
    """One-line Markov regime context for the thinking prompt."""
    markov = market_summary.get("markov_regime") or {}
    if not isinstance(markov, dict) or not markov.get("current_state"):
        return "n/a"
    state = str(markov.get("current_state") or "sideways")
    p_bull = float(markov.get("p_bull_tomorrow") or 0.0)
    p_bear = float(markov.get("p_bear_tomorrow") or 0.0)
    line = (
        f"currently {state}, "
        f"P(bull tomorrow)={p_bull:.0%}, "
        f"P(bear tomorrow)={p_bear:.0%}"
    )
    if str(markov.get("confidence") or "").lower() == "low":
        line += " — regime transition uncertain — reduce tilt magnitude"
    return line


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
        prev_sector = str(prev_full.get("sector_view") or "")[:120]
        prev_rationale = str(prev_full.get("tilt_rationale") or "")[:220]
        prev_context = (
            f"Previous decision ({str(prev_full.get('timestamp', 'unknown'))[:19]}):\n"
            f"  regime={prev_full.get('regime')} | phase={prev_full.get('ai_cycle_phase', 'n/a')}\n"
            f"  narrative={str(prev_full.get('narrative') or '')[:120]}\n"
            f"  sector_view={prev_sector or 'n/a'}\n"
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
    news_text = str(
        market_summary.get("news_digest") or market_summary.get("news_headlines") or ""
    )
    if len(news_text) > 1200:
        news_text = news_text[:1200] + "\n...(truncated for prompt size)"

    return f"""Current market snapshot:

Objective: beat VTI on risk-adjusted basis (Sharpe first). Passive VTI is the benchmark — every active sleeve must justify its risk.

AI/Tech cycle phase (heuristic): {market_summary.get('ai_cycle_phase', 'unknown')}
Sector leadership (5d): {market_summary.get('sector_leadership', 'n/a')}
Sector laggards: {', '.join(f"{r['sector']} {float(r['change_5d_pct']):+.1f}%" for r in (market_summary.get('sector_laggards') or [])) or 'n/a'}

Vol overlay regime: {market_summary.get('vol_overlay_regime', _vol_overlay_regime(market_summary))}
Stat arb regime: {market_summary.get('stat_arb_regime', _stat_arb_regime(market_summary))}
Stat arb candidates (top): {market_summary.get('stat_arb_candidate_summary', 'n/a')}
Bubble risk: {market_summary.get('bubble_score_100', 'n/a')}/100 | Buffett: {market_summary.get('buffett_signal', 'n/a')}
Technicals: {market_summary.get('technical_summary', 'n/a')}
Conviction score (SPY/stat-arb): {market_summary.get('conviction_score', 'n/a')}
Crowding check: {market_summary.get('crowded_trade_warning', _crowded_trade_warning(market_summary))}

SPY vs MA200: {market_summary['spy_trend']}
VIX: {market_summary['vix']} | {vix_note}
Oil 5d: {market_summary['oil_change']}% | Gold 5d: {market_summary['gold_change']}%
Yield curve / rates: {market_summary.get('yield_curve', 'n/a')}
Regime: {market_summary.get('regime', 'unknown')}
Markov regime: {_format_markov_prompt_line(market_summary)}
Macro sentiment: {market_summary['macro_sentiment']}
Top headline: {market_summary['top_headline']}
Insider / filings (SEC RSS): {market_summary.get('insider_summary', 'n/a')}
Options / unusual activity: {market_summary.get('options_flow_summary', 'n/a')}
Short interest: {market_summary.get('short_interest_summary', 'n/a')}
Catalyst watchlist (RVOL/ORB/news): {market_summary.get('catalyst_summary', 'n/a')}
{(
    "Scheduled news digest ("
    + str(market_summary.get("news_slot") or "scheduled")
    + "):\n"
    + news_text
    + "\nUse news_impact_score="
    + f"{float(market_summary.get('news_impact_score') or 0.0):.2f}"
    + " to scale tilt conviction (0=ignore headlines, 1=strong evidence).\n"
    + (
        "HIGH-IMPACT NEWS: move at most 3 sleeves vs prior day (strict live cap). "
        "Rank by conviction: keep VTI/cash barbell, SPY liquidity, NYSE energy as custodians; "
        "merge smaller sleeve nudges into those three (e.g. gold->cash, crypto->SPY, metal->cash).\n"
        if float(market_summary.get("news_impact_score") or 0.0) >= 0.35
        else (
            "When news_impact_score >= 0.25, prefer <=3 sleeve moves — consolidate minor tilts.\n"
            if float(market_summary.get("news_impact_score") or 0.0) >= 0.25
            else ""
        )
    )
    + "AI/tech boom context: "
    + str(market_summary.get("news_ai_tech_context") or market_summary.get("ai_cycle_phase", "n/a"))
    + "\n"
    if market_summary.get("news_headlines")
    else ""
)}
Bot exposure: {market_summary.get('bot_exposure_str', 'n/a')}

Previous day tilt: {prev_line}
{prev_context}

{gold_note}
Maintain consistency with previous day unless strong new evidence (VIX spike, trend break, headline, sector rotation).
Hard production cap: +/-{config.effective_thinking_max_sleeve_delta():.0%} per sleeve vs prior day without new evidence.
Max 3 sleeves may move per refresh (strict on live); when news_impact_score >= 0.35 prefer exactly 3 custodian sleeves: VTI/cash barbell, SPY liquidity, or NYSE energy.
When vol overlay is elevated, trim SPY/NYSE — do not stack beta on top of vol sleeve.
When stat arb is supportive, modest crypto tilt OK; when hostile, raise VTI/cash.
Use decimal weights in RECOMMENDED_TILT (e.g. 0.52 not "52%"); weights must sum to ~1.0.
SECTOR_VIEW must include stat-arb + vol-overlay read and 3-7d sector leaders/laggards.
REGIME_SIGNAL / TILT_SIGNAL / RISK_SIGNAL must be filled (structured one-liners).
TILT_RATIONALE must link ASYMMETRY + SECTOR_VIEW to each sleeve above 5% and state VTI vs active trade-off.
Reply with ONLY the structured block (NARRATIVE through OPPORTUNITIES). Be decisive. Start with NARRATIVE:"""


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
    if structured.get("sector_view"):
        score += 0.12
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


def _reasoning_result_from_llm_text(
    full_text: str,
    answer_text: str,
    market_summary: dict,
    *,
    model: str,
    source: str,
) -> dict[str, Any]:
    """Parse structured PM block from LLM output (Ollama or Kimi)."""
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
    sector_view = structured.get("sector_view") or parsed.get("sector_view") or ""
    ai_cycle_phase = (
        structured.get("ai_cycle_phase")
        or parsed.get("ai_cycle_phase")
        or market_summary.get("ai_cycle_phase")
        or ""
    )
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
        "sector_view": str(sector_view).strip() if sector_view else "",
        "ai_cycle_phase": str(ai_cycle_phase).strip() if ai_cycle_phase else "",
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
        "source": source,
        "parse_quality": quality,
        "market_summary": market_summary,
    }
    return _finalize_thinking_result(result, market_summary, force_decision=False)


def _get_market_reasoning_with_model(
    market_summary: dict,
    model: str,
    *,
    timeout_sec: int | None = None,
) -> dict[str, Any]:
    """Single-model Ollama attempt with retries + exponential backoff; raises on exhaustion."""
    user_prompt = _build_reasoning_user_prompt(market_summary)
    prompt_len = len(user_prompt) + len(_pm_system_prompt() or "")
    timeout = _thinking_timeout_sec(timeout_sec)
    attempts = _ollama_max_attempts()
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            full_text, answer_text = _ollama_generate(
                user_prompt,
                system=_pm_system_prompt(),
                model=model,
                timeout_sec=timeout,
            )
            result = _reasoning_result_from_llm_text(
                full_text,
                answer_text,
                market_summary,
                model=model,
                source="llm",
            )
            persist_thinking_last(result, regime=market_summary.get("regime"))
            return result
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            RuntimeError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            last_exc = exc
            logger.warning(
                "Ollama market reasoning failed attempt %s/%s model=%s "
                "prompt_len=%s timeout=%ss error_type=%s err=%s",
                attempt + 1,
                attempts,
                model,
                prompt_len,
                timeout,
                type(exc).__name__,
                exc,
            )
            _audit_thinking(
                "ollama_retry",
                capability="market_reasoning",
                model=model,
                attempt=attempt + 1,
                max_attempts=attempts,
                prompt_len=prompt_len,
                timeout_sec=timeout,
                error_type=type(exc).__name__,
                error=str(exc)[:240],
            )
            if attempt + 1 < attempts:
                time.sleep(_retry_backoff_sec(attempt))
                continue
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.error(
                "Ollama market reasoning unexpected error model=%s prompt_len=%s "
                "error_type=%s err=%s",
                model,
                prompt_len,
                type(exc).__name__,
                exc,
            )
            break
    assert last_exc is not None
    raise last_exc


def get_market_reasoning_via_kimi(market_summary: dict) -> dict[str, Any]:
    """Daily deep reasoning via Moonshot/Kimi (cloud API — not for 5m loop)."""
    from modules.kimi_client import deep_think, record_kimi_daily_run

    user_prompt = _build_reasoning_user_prompt(market_summary)
    full_text = deep_think(user_prompt, system=_pm_system_prompt())
    result = _reasoning_result_from_llm_text(
        full_text,
        full_text,
        market_summary,
        model=config.KIMI_MODEL,
        source="kimi",
    )
    record_kimi_daily_run(
        regime=str(market_summary.get("regime") or ""),
        model=config.KIMI_MODEL,
        source="kimi",
    )
    persist_thinking_last(result, regime=market_summary.get("regime"))
    return result


def get_market_reasoning(
    market_summary: dict,
    *,
    fast_model: bool = False,
    model: str | None = None,
    timeout_sec: int | None = None,
) -> dict[str, Any]:
    """Use local LLM with primary -> fallback chain; heuristic if all fail."""
    ensure_ollama_startup_check()
    if not ollama_available():
        result = build_heuristic_reasoning_result(
            market_summary, reason="ollama-unreachable"
        )
        persist_thinking_last(result, regime=market_summary.get("regime"))
        return result

    installed = ollama_installed_models()
    chain = thinking_model_chain(fast_only=fast_model)
    if model:
        candidates = [model]
    else:
        candidates = [m for m in chain if model_available(m, installed)]
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
            result = _get_market_reasoning_with_model(
                market_summary, candidate, timeout_sec=timeout_sec
            )
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            RuntimeError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            errors.append(f"{candidate}: {type(exc).__name__}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{candidate}: {type(exc).__name__}: {exc}")
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
    if errors:
        clear_thinking_runtime_caches()
        logger.warning(
            "Ollama market reasoning exhausted models=%s errors=%s — heuristic fallback",
            candidates,
            errors[:3],
        )
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


def build_demo_reasoning_samples() -> list[dict[str, Any]]:
    """Three illustrative PM outputs (heuristic) for docs — VTI-beat tuned scenarios."""
    scenarios: list[dict[str, Any]] = []

    def _scenario(
        label: str,
        summary: dict[str, Any],
        *,
        conf: float = 0.82,
    ) -> dict[str, Any]:
        base = {
            "label": label,
            "reasoning": f"Demo scenario: {label}",
            "narrative": _infer_narrative(summary),
            "asymmetry": _infer_asymmetry(summary),
            "sector_view": (
                f"Leaders: {summary.get('sector_leadership', 'n/a')} | "
                f"vol: {summary.get('vol_overlay_regime')} | "
                f"stat-arb: {summary.get('stat_arb_regime')}"
            ),
            "ai_cycle_phase": summary.get("ai_cycle_phase"),
            "suggested_tilt": derive_heuristic_tilt(summary),
            "confidence": conf,
            "model": "heuristic-demo",
            "source": "demo",
            "market_summary": summary,
        }
        tilt = base["suggested_tilt"]
        base["tilt_rationale"] = _infer_tilt_rationale(
            summary, tilt, str(base["asymmetry"])
        )
        return _finalize_thinking_result(base, summary, force_decision=True)

    mid_cycle = {
        "spy_trend": "above MA200 (+4.2%)",
        "vix": 15.2,
        "vix_trend": "stable (-1.2% 5d)",
        "oil_change": 1.5,
        "gold_change": 0.8,
        "regime": "risk_on",
        "sector_leaders": [
            {"sector": "Semis (NVDA)", "change_5d_pct": 6.2},
            {"sector": "Tech (QQQ)", "change_5d_pct": 4.1},
        ],
        "sector_laggards": [{"sector": "Gold (GLD)", "change_5d_pct": -0.5}],
        "sector_leadership": "Semis (NVDA) +6.2%, Tech (QQQ) +4.1%",
        "ai_cycle_phase": "mid-cycle AI leadership",
        "vol_overlay_regime": _vol_overlay_regime({"vix": 15.2, "vix_trend": "stable"}),
        "stat_arb_regime": _stat_arb_regime(
            {"vix": 15.2, "spy_trend": "above MA200", "vix_trend": "stable"}
        ),
        "crowded_trade_warning": _crowded_trade_warning(
            {"sector_leaders": [{"sector": "Semis (NVDA)", "change_5d_pct": 6.2}]}
        ),
    }
    scenarios.append(_scenario("Mid-cycle AI + supportive stat arb", mid_cycle, conf=0.84))

    crowded_vol = {
        "spy_trend": "above MA200 (+1.1%)",
        "vix": 21.5,
        "vix_trend": "rising (+12.3% 5d)",
        "oil_change": 2.0,
        "gold_change": -1.2,
        "regime": "neutral",
        "sector_leaders": [
            {"sector": "Tech (QQQ)", "change_5d_pct": 9.5},
            {"sector": "Semis (NVDA)", "change_5d_pct": 11.2},
        ],
        "sector_laggards": [{"sector": "Energy (XOM)", "change_5d_pct": -2.1}],
        "sector_leadership": "Semis (NVDA) +11.2%, Tech (QQQ) +9.5%",
        "ai_cycle_phase": "late-cycle / rotation risk",
        "vol_overlay_regime": _vol_overlay_regime({"vix": 21.5, "vix_trend": "rising (+12.3% 5d)"}),
        "stat_arb_regime": _stat_arb_regime(
            {"vix": 21.5, "spy_trend": "above MA200", "vix_trend": "rising"}
        ),
        "crowded_trade_warning": _crowded_trade_warning(
            {
                "sector_leaders": [
                    {"sector": "Tech (QQQ)", "change_5d_pct": 9.5},
                    {"sector": "Semis (NVDA)", "change_5d_pct": 11.2},
                ],
                "vix_trend": "rising (+12.3% 5d)",
            }
        ),
    }
    crowded_result = _scenario("Crowded tech + rising VIX", crowded_vol, conf=0.79)
    crowded_result["narrative"] = (
        "Late-cycle AI chase into rising vol — defensive neutral; VTI anchor beats adding beta"
    )
    crowded_result["asymmetry"] = crowded_vol["crowded_trade_warning"].replace("CROWDED: ", "")
    crowded_result["tilt_rationale"] = _infer_tilt_rationale(
        crowded_vol,
        crowded_result["suggested_tilt"],
        crowded_result["asymmetry"],
    )
    scenarios.append(crowded_result)

    defensive = {
        "spy_trend": "below MA200",
        "vix": 24.8,
        "vix_trend": "rising (+8.0% 5d)",
        "oil_change": 5.5,
        "gold_change": 2.5,
        "regime": "risk_off",
        "top_headline": "Geopolitical tension in Middle East",
        "sector_leaders": [
            {"sector": "Energy (XOM)", "change_5d_pct": 4.8},
            {"sector": "Gold (GLD)", "change_5d_pct": 3.2},
        ],
        "sector_laggards": [{"sector": "Tech (QQQ)", "change_5d_pct": -3.5}],
        "sector_leadership": "Energy (XOM) +4.8%, Gold (GLD) +3.2%",
        "ai_cycle_phase": "rotation (energy / real assets)",
        "vol_overlay_regime": _vol_overlay_regime({"vix": 24.8, "vix_trend": "rising"}),
        "stat_arb_regime": _stat_arb_regime(
            {"vix": 24.8, "spy_trend": "below MA200", "vix_trend": "rising"}
        ),
        "crowded_trade_warning": _crowded_trade_warning({"sector_leaders": []}),
    }
    scenarios.append(_scenario("Risk-off rotation + vol overlay", defensive, conf=0.86))
    return scenarios


def run_thinking_with_news(
    data,
    regime: str,
    vol: str,
    wisdom: dict | None = None,
    *,
    news_headlines: str | list,
    slot: str | None = None,
    news_digest: dict | None = None,
    background: bool = False,
) -> dict | None:
    """Forced thinking refresh with scheduled news digest (paper only)."""
    if not config.effective_thinking_engine_enabled():
        return None
    from modules.thinking_news import format_news_digest, format_news_summary, normalize_news_headlines

    news_text = normalize_news_headlines(news_headlines)
    if news_digest:
        news_summary = format_news_digest(news_digest)
        news_impact = float(news_digest.get("news_impact_score") or 0.0)
    else:
        news_summary = format_news_summary(news_text, slot=slot)
        news_impact = 0.0
    logger.info(
        "Thinking engine: scheduled news run slot=%s headlines=%d impact=%.2f",
        slot or "manual",
        len(news_text.splitlines()) if news_text else 0,
        news_impact,
    )
    _audit_thinking(
        "scheduled_news_start",
        news_slot=slot,
        news_summary=news_summary[:800],
        news_impact_score=news_impact,
    )

    summary = build_market_summary(
        data,
        regime,
        vol,
        wisdom=wisdom,
        news_headlines=news_text,
        news_slot=slot,
    )

    def _execute() -> dict:
        if ollama_available():
            try:
                result = get_market_reasoning(summary)
            except Exception as exc:
                log_subsystem_error(
                    "thinking_engine",
                    "Scheduled news LLM failed; heuristic fallback",
                    exc,
                )
                result = build_heuristic_reasoning_result(summary, reason=f"news-{slot or 'manual'}-llm-error")
        else:
            result = build_heuristic_reasoning_result(summary, reason=f"news-{slot or 'manual'}-no-ollama")
        result["news_slot"] = slot
        result["news_summary"] = news_summary
        result["news_impact_score"] = float(
            news_digest.get("news_impact_score") if news_digest else summary.get("news_impact_score") or 0.0
        )
        _record_thinking_run(
            regime,
            result,
            news_summary=news_summary,
            news_slot=slot,
        )
        log_thinking_result(result)
        _audit_thinking(
            "scheduled_news_complete",
            news_slot=slot,
            source=result.get("source"),
            confidence=result.get("confidence"),
            narrative=(str(result.get("narrative") or "")[:160]),
            suggested_tilt=result.get("suggested_tilt"),
            news_impact_score=result.get("news_impact_score"),
        )
        return result

    if background:
        threading.Thread(
            target=_execute,
            name=f"thinking-news-sync-{slot or 'manual'}",
            daemon=True,
        ).start()
        cached = read_json_file(OUTPUT_FILE)
        return cached

    return _execute()


def _run_thinking_refresh(regime: str, summary: dict) -> dict[str, Any]:
    """Hybrid refresh: Ollama for quick/regime-change; Kimi once daily when enabled."""
    from modules.kimi_client import should_run_kimi_daily

    use_kimi = (
        config.effective_kimi_deep_thinker_enabled()
        and config.KIMI_DAILY_THINK
        and should_run_kimi_daily()
    )
    if use_kimi:
        logger.info("Thinking engine: Kimi daily deep think started (background-safe path)")
        _audit_thinking("kimi_daily_start", regime=regime)
        try:
            result = get_market_reasoning_via_kimi(summary)
            _audit_thinking(
                "kimi_daily_done",
                regime=regime,
                confidence=result.get("confidence"),
                parse_quality=result.get("parse_quality"),
            )
            return result
        except Exception as exc:
            log_subsystem_error(
                "thinking_engine",
                "Kimi daily deep think failed; falling back to local Ollama",
                exc,
            )
            _audit_thinking("kimi_daily_failed", regime=regime, error=str(exc)[:200])

    if not ollama_available():
        return build_heuristic_reasoning_result(summary, reason="ollama-unreachable")

    # Regime-change or post-Kimi: fast local Ollama (no cloud cost)
    fast = bool(config.effective_kimi_deep_thinker_enabled() and not use_kimi)
    return get_market_reasoning(summary, fast_model=fast)


def maybe_run_thinking(
    data,
    regime: str,
    vol: str,
    wisdom: dict | None = None,
    *,
    top_headline: str | None = None,
    news_headlines: str | list | None = None,
    news_slot: str | None = None,
    force: bool = False,
) -> dict | None:
    """Run LLM reasoning at most once per THINKING_CACHE_HOURS or on regime change.

    Available on paper (default ON) and live (default OFF / opt-in). Failures fall
    back to heuristic tilts; live still requires manual approval when configured.
    """
    if not config.effective_thinking_engine_enabled():
        return None
    if not force and not should_refresh_thinking(regime):
        cached = read_json_file(OUTPUT_FILE)
        if cached:
            return cached
        return None
    ensure_ollama_startup_check()
    summary = build_market_summary(
        data,
        regime,
        vol,
        wisdom=wisdom,
        top_headline=top_headline,
        news_headlines=news_headlines,
        news_slot=news_slot,
    )
    news_blob = (
        news_headlines
        if isinstance(news_headlines, str)
        else "\n".join(str(x) for x in news_headlines)
        if isinstance(news_headlines, list)
        else None
    )
    from modules.kimi_client import should_run_kimi_daily

    kimi_daily_ok = (
        config.effective_kimi_deep_thinker_enabled() and should_run_kimi_daily()
    )
    if not ollama_available() and not kimi_daily_ok:
        logger.info("Thinking engine: Ollama not reachable, using rule-based tilt")
        result = build_heuristic_reasoning_result(summary, reason="ollama-unreachable")
        result["fallback_reason"] = "ollama-unreachable"
        _record_thinking_run(regime, result, news_summary=news_blob, news_slot=news_slot)
        return result
    # If caller asked for a synchronous forced run, do it now.
    if force:
        try:
            result = _run_thinking_refresh(regime, summary)
        except Exception as exc:
            log_subsystem_error(
                "thinking_engine",
                "LLM failed during forced run; falling back to heuristic",
                exc,
            )
            result = build_heuristic_reasoning_result(summary, reason="llm-error-forced")
            result["fallback_reason"] = f"llm-error:{type(exc).__name__}"
        if result.get("source") == "heuristic":
            logger.info("Thinking engine: heuristic fallback (%s)", result.get("model"))
        elif float(result.get("parse_quality") or 0.0) < 0.45:
            logger.info(
                "Thinking engine: low parse quality (%s), using best-effort LLM output",
                result.get("parse_quality"),
            )
        _record_thinking_run(
            regime,
            result,
            news_summary=news_blob,
            news_slot=news_slot,
        )
        return result

    # Non-forced path: refresh in background to avoid blocking main loop.
    def _bg_refresh():
        try:
            logger.info("Thinking engine: background refresh started for regime=%s", regime)
            _audit_thinking("background_refresh_start", regime=regime)
            res = _run_thinking_refresh(regime, summary)
            if res.get("source") == "heuristic":
                logger.info("Thinking engine background: heuristic fallback (%s)", res.get("model"))
            elif float(res.get("parse_quality") or 0.0) < 0.45:
                logger.info("Thinking engine background: low parse quality: %s", res.get("parse_quality"))
            _record_thinking_run(
                regime,
                res,
                news_summary=news_blob,
                news_slot=news_slot,
            )
            logger.info("Thinking engine: background refresh completed for regime=%s", regime)
            _audit_thinking(
                "background_refresh_done",
                regime=regime,
                source=res.get("source"),
                confidence=res.get("confidence"),
            )
        except Exception as exc:
            log_subsystem_error(
                "thinking_engine",
                "Background refresh failed",
                exc,
            )
            try:
                # Persist a heuristic snapshot so health/dashboard still have a signal.
                fb = build_heuristic_reasoning_result(
                    summary, reason=f"bg-error:{type(exc).__name__}"
                )
                fb["fallback_reason"] = f"bg-error:{type(exc).__name__}"
                _record_thinking_run(regime, fb, news_summary=news_blob, news_slot=news_slot)
            except Exception:
                cached_err = read_json_file(OUTPUT_FILE)
                if cached_err:
                    logger.info(
                        "Thinking engine: kept last cached snapshot after refresh failure"
                    )
                    _audit_thinking(
                        "background_refresh_failed_kept_cache", regime=regime
                    )

    t = threading.Thread(target=_bg_refresh, daemon=True)
    t.start()

    # Return cached output if available, else a heuristic immediate result.
    cached = read_json_file(OUTPUT_FILE)
    if cached:
        return cached
    result = build_heuristic_reasoning_result(summary, reason="background-refresh-started")
    result["fallback_reason"] = "background-refresh-started"
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
        _audit_thinking(
            "tilt_applied",
            log_line=log_line,
            deltas=deltas,
            confidence=thinking_result.get("confidence"),
        )
        state = read_json_file(STATE_FILE)
        today = datetime.date.today().isoformat()
        if state.get("last_apply_log_date") != today:
            logger.info(log_line)
            state["last_apply_log_date"] = today
            write_json_file(STATE_FILE, state)
    return merged, thinking_result


# --- Structured AI capabilities (Ollama JSON) --------------------------------


def _structured_ai_call(
    capability: str,
    system: str,
    user_prompt: str,
    *,
    model: str | None = None,
    fast_model: bool = False,
    fallback: dict[str, Any] | None = None,
    timeout_sec: int | None = None,
    force_fallback: bool = False,
) -> dict[str, Any]:
    """Run a JSON-mode Ollama call across the model chain; heuristic fallback on failure."""
    base = _normalize_structured_output(dict(fallback or {}), capability)
    prompt_len = len(system or "") + len(user_prompt or "")
    timeout = _thinking_timeout_sec(timeout_sec)

    if force_fallback:
        base.update(
            source="heuristic",
            model=None,
            error="force_fallback",
            fallback_reason="force_fallback",
        )
        logger.info(
            "structured_%s: force_fallback — skipping Ollama (prompt_len=%s)",
            capability,
            prompt_len,
        )
        return base

    ensure_ollama_startup_check()
    if not ollama_available():
        base.update(
            source="unavailable",
            model=None,
            error="ollama_unreachable",
            fallback_reason="ollama_unreachable",
        )
        log_subsystem_warning(
            "thinking_engine",
            f"structured_{capability}: Ollama unreachable — using heuristic fallback",
        )
        _audit_thinking(
            f"structured_{capability}_fallback",
            reason="ollama_unreachable",
            prompt_len=prompt_len,
            timeout_sec=timeout,
        )
        return base

    installed = ollama_installed_models()
    chain = [model] if model else thinking_model_chain(fast_only=fast_model)
    candidates = [m for m in chain if model_available(m, installed)] or chain
    errors: list[str] = []
    retries = _ollama_max_attempts()
    for candidate in candidates:
        for attempt in range(retries):
            try:
                parsed = ollama_json(
                    user_prompt,
                    system=system,
                    model=candidate,
                    timeout_sec=timeout,
                    retries=1,  # outer loop owns exponential backoff
                )
                if parsed.get("parse_error"):
                    errors.append(f"{candidate}: json_parse")
                    logger.warning(
                        "structured_%s: JSON parse failed model=%s prompt_len=%s "
                        "attempt=%s/%s",
                        capability,
                        candidate,
                        prompt_len,
                        attempt + 1,
                        retries,
                    )
                    break  # try next model
                parsed["model"] = candidate
                parsed["source"] = "llm"
                parsed["capability"] = capability
                _audit_thinking(
                    f"structured_{capability}",
                    model=candidate,
                    signal_strength=parsed.get("signal_strength"),
                    confidence=parsed.get("confidence"),
                    attempt=attempt + 1,
                    prompt_len=prompt_len,
                    timeout_sec=timeout,
                )
                return _normalize_structured_output(parsed, capability)
            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
                RuntimeError,
                json.JSONDecodeError,
            ) as exc:
                err_type = type(exc).__name__
                errors.append(f"{candidate}@{attempt + 1}: {err_type}: {exc}")
                logger.warning(
                    "structured_%s failed attempt %s/%s model=%s prompt_len=%s "
                    "timeout=%ss error_type=%s err=%s",
                    capability,
                    attempt + 1,
                    retries,
                    candidate,
                    prompt_len,
                    timeout,
                    err_type,
                    exc,
                )
                _audit_thinking(
                    "ollama_retry",
                    capability=capability,
                    model=candidate,
                    attempt=attempt + 1,
                    max_attempts=retries,
                    prompt_len=prompt_len,
                    timeout_sec=timeout,
                    error_type=err_type,
                    error=str(exc)[:240],
                )
                if attempt + 1 < retries:
                    time.sleep(_retry_backoff_sec(attempt))
                    continue
            except Exception as exc:  # noqa: BLE001
                err_type = type(exc).__name__
                errors.append(f"{candidate}@{attempt + 1}: {err_type}: {exc}")
                logger.error(
                    "structured_%s unexpected error model=%s prompt_len=%s "
                    "error_type=%s err=%s",
                    capability,
                    candidate,
                    prompt_len,
                    err_type,
                    exc,
                )
                break
    base.update(
        source="heuristic",
        model=None,
        errors=errors[:6],
        fallback_reason="; ".join(errors[:2]) if errors else "all-models-failed",
    )
    log_subsystem_warning(
        "thinking_engine",
        f"structured_{capability}: all models failed — heuristic fallback",
    )
    _audit_thinking(
        f"structured_{capability}_fallback",
        reason=base.get("fallback_reason"),
        prompt_len=prompt_len,
        timeout_sec=timeout,
        errors=errors[:4],
    )
    return base


def analyze_tilt_with_ai(
    data,
    regime: str | None = None,
    vol: str | None = None,
    *,
    model: str | None = None,
    fast_model: bool = False,
    sentiment=None,
    timeout_sec: int = 90,
    heuristic_only: bool = False,
    force_fallback: bool = False,
) -> dict[str, Any]:
    """Structured sleeve-tilt recommendation (JSON).

    ``regime`` / ``vol`` / ``sentiment`` may be None — defaults are applied.
    """
    if force_fallback:
        heuristic_only = True
    regime_s, vol_s = _coerce_regime_vol(data, regime, vol, sentiment=sentiment)
    data_empty = data is None or getattr(data, "empty", True)
    try:
        if data_empty or heuristic_only:
            raise RuntimeError("empty_data" if data_empty else "heuristic_only")
        summary, ctx_block = _thinking_context_for_ai(data, regime_s, vol_s)
    except Exception as exc:
        if str(exc) in ("empty_data", "heuristic_only"):
            logger.debug("analyze_tilt_with_ai using partial context: %s", exc)
        else:
            logger.warning("analyze_tilt_with_ai context failed: %s", exc)
        summary = {
            "regime": regime_s,
            "vol": vol_s,
            "spy_trend": "n/a",
            "vix": "n/a",
            "sector_leadership": "n/a",
            "asymmetry": "",
        }
        ctx_block = f"REGIME: {regime_s} | vol={vol_s}\n(partial context — build failed)"

    try:
        heuristic = derive_heuristic_tilt(summary)
    except Exception as exc:
        logger.debug("heuristic tilt failed: %s", exc)
        heuristic = {"vti": 0.70, "cash": 0.20, "spy": 0.10, "energy": 0.0, "gold": 0.0, "crypto": 0.0, "bonds": 0.0}

    top = sorted(heuristic.items(), key=lambda kv: float(kv[1]), reverse=True)
    add = top[0][0] if top else "vti"
    cut = "cash" if add != "cash" else "spy"
    try:
        rationale = _infer_tilt_rationale(summary, heuristic, _infer_asymmetry(summary))[:200]
    except Exception:
        rationale = f"Partial-data tilt toward {add}; trim {cut} (regime={regime_s}, vol={vol_s})"
    fallback = _normalize_structured_output(
        {
            "signal_strength": 0.45,
            "confidence": 0.4,
            "suggested_action": f"tilt toward {add}, trim {cut}",
            "reasoning": rationale,
            "tilt_signal": f"{add} / {cut} | conviction 0.40",
            "recommended_tilt": heuristic,
            "max_sleeve_moves": 3,
        },
        "tilt_analysis",
    )
    if data_empty or heuristic_only:
        fallback["regime"] = regime_s
        fallback["vol"] = vol_s
        fallback["source"] = "heuristic"
        if force_fallback:
            fallback["fallback_reason"] = "force_fallback"
        else:
            fallback["fallback_reason"] = "empty_data" if data_empty else "heuristic_only"
        return fallback
    prompt = f"""CONTEXT:
{ctx_block}

Heuristic tilt seed: {json.dumps(heuristic, default=str)}
Prior day consistency: prefer <=3 sleeve moves, +/-{config.effective_thinking_max_sleeve_delta():.0%} each.

JSON schema:
{{
  "signal_strength": 0.0-1.0,
  "confidence": 0.0-1.0,
  "suggested_action": "e.g. 'raise cash 5%, trim SPY 3%'",
  "reasoning": "max 2 sentences citing CONTEXT",
  "tilt_signal": "add_sleeve / cut_sleeve | conviction 0.00-1.00",
  "recommended_tilt": {{"vti": 0.XX, "spy": 0.XX, "energy": 0.XX, "gold": 0.XX, "cash": 0.XX, "crypto": 0.XX, "bonds": 0.XX}},
  "max_sleeve_moves": 3
}}"""
    try:
        result = _structured_ai_call(
            "tilt_analysis",
            _trading_system_prompt("tilt_analysis"),
            prompt,
            model=model,
            fast_model=fast_model,
            fallback=fallback,
            timeout_sec=timeout_sec,
            force_fallback=force_fallback,
        )
    except Exception as exc:
        logger.warning("analyze_tilt_with_ai LLM path failed: %s", exc)
        result = dict(fallback)
        result["fallback_reason"] = f"exception:{type(exc).__name__}"
    result["regime"] = regime_s
    result["vol"] = vol_s
    return result


def analyze_risk_signals_with_ai(
    data,
    regime: str | None = None,
    vol: str | None = None,
    *,
    model: str | None = None,
    fast_model: bool = False,
    sentiment=None,
) -> dict[str, Any]:
    """Structured near-term risk read (JSON)."""
    regime_s, vol_s = _coerce_regime_vol(data, regime, vol, sentiment=sentiment)
    try:
        summary, ctx_block = _thinking_context_for_ai(data, regime_s, vol_s)
    except Exception as exc:
        logger.warning("analyze_risk_signals_with_ai context failed: %s", exc)
        summary = {"regime": regime_s, "vol": vol_s, "vix": 0, "bubble_score_100": 0, "spy_trend": "n/a"}
        ctx_block = f"REGIME: {regime_s} | vol={vol_s}\n(partial context)"
    vix = float(summary.get("vix") or 0) if summary.get("vix") not in (None, "n/a") else 0.0
    try:
        bubble = float(summary.get("bubble_score_100") or 0)
    except (TypeError, ValueError):
        bubble = 0.0
    level = "high" if vix >= 22 or bubble >= 70 else ("medium" if vix >= 16 or bubble >= 55 else "low")
    fallback = _normalize_structured_output(
        {
            "signal_strength": 0.7 if level == "high" else (0.45 if level == "medium" else 0.25),
            "confidence": 0.4,
            "suggested_action": (
                "raise cash / cut beta" if level == "high" else "hold risk budget"
            ),
            "reasoning": (
                f"VIX {vix:.1f} ({summary.get('vix_trend')}); "
                f"bubble {bubble:.0f}/100; SPY {summary.get('spy_trend')}"
            )[:200],
            "risk_signal": f"{level} | VIX/bubble/trend",
            "risk_level": level,
            "top_risks": [
                f"VIX {vix:.1f}",
                f"bubble {bubble:.0f}/100",
                str(summary.get("crowded_trade_warning") or "crowding n/a")[:60],
            ],
        },
        "risk_signals",
    )
    prompt = f"""CONTEXT:
{ctx_block}

JSON schema:
{{
  "signal_strength": 0.0-1.0,
  "confidence": 0.0-1.0,
  "suggested_action": "raise cash / cut beta | hold | selective add",
  "reasoning": "max 2 sentences",
  "risk_signal": "low|medium|high | top risk in <=12 words",
  "risk_level": "low|medium|high",
  "top_risks": ["risk1", "risk2", "risk3"]
}}"""
    return _structured_ai_call(
        "risk_signals",
        _trading_system_prompt("risk_signals"),
        prompt,
        model=model,
        fast_model=fast_model,
        fallback=fallback,
        timeout_sec=90,
    )


def thinking_dashboard_snapshot() -> dict[str, Any]:
    """Dashboard-friendly thinking status (works for paper + live books)."""
    snap = get_thinking_status_snapshot()
    enabled = bool(snap.get("effective_enabled"))
    source = str(snap.get("last_source") or "")
    conf = snap.get("last_confidence")
    conf_s = f"{float(conf):.0%}" if conf is not None else "—"
    if not enabled:
        status = "OFF"
        detail = (
            "paper ON / live OFF by default"
            if not getattr(config, "LIVE_THINKING_ENGINE_ENABLED", False)
            else "disabled"
        )
    elif not snap.get("ollama_ok") and (not source or source.startswith("heuristic")):
        status = "FALLBACK"
        detail = "Ollama down — heuristic tilts"
    elif source.startswith("heuristic") or source == "unavailable":
        status = "FALLBACK"
        detail = f"heuristic · conf {conf_s}"
    elif snap.get("last_timestamp"):
        status = "ON"
        detail = f"{snap.get('last_model') or 'llm'} · conf {conf_s}"
    else:
        status = "ON"
        detail = "awaiting first run"
    return {
        **snap,
        "status": status,
        "detail": detail,
        "pill_text": f"Think: {status}" + (f" · {detail}" if detail else ""),
        "narrative": snap.get("narrative_snip") or "",
        "regime_signal": snap.get("regime_signal") or "",
        "tilt_signal": snap.get("tilt_signal") or "",
        "risk_signal": snap.get("risk_signal") or "",
    }


def thinking_health_snapshot() -> dict[str, Any]:
    """Inputs for Bot Health Score (thinking engine contribution)."""
    dash = thinking_dashboard_snapshot()
    age_hours: float | None = None
    ts = dash.get("last_timestamp")
    if ts:
        try:
            parsed = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            age_hours = (
                datetime.datetime.now(datetime.timezone.utc) - parsed.astimezone(datetime.timezone.utc)
            ).total_seconds() / 3600.0
        except (TypeError, ValueError):
            age_hours = None
    return {
        "enabled": bool(dash.get("effective_enabled")),
        "ollama_ok": bool(dash.get("ollama_ok")),
        "status": dash.get("status"),
        "source": dash.get("last_source"),
        "confidence": dash.get("last_confidence"),
        "age_hours": age_hours,
        "fallback": bool(dash.get("fallback")),
        "validation_score": dash.get("validation_score"),
        "live_opt_in": bool(dash.get("live_env_enabled")),
    }


def _thinking_context_for_ai(
    data,
    regime: str | None = None,
    vol: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Build summary + compact context block for structured LLM calls."""
    regime_s, vol_s = _coerce_regime_vol(data, regime, vol)
    summary = build_market_summary(data, regime_s, vol_s)
    block = format_thinking_context_block(summary)
    return summary, block


def analyze_regime_with_ai(
    data,
    regime: str | None = None,
    vol: str | None = None,
    *,
    model: str | None = None,
    fast_model: bool = False,
    sentiment=None,
    timeout_sec: int = 90,
    heuristic_only: bool = False,
    force_fallback: bool = False,
) -> dict[str, Any]:
    """Regime + conviction with unified signal_strength / suggested_action.

    ``regime`` / ``vol`` / ``sentiment`` may be None — defaults are applied.
    """
    if force_fallback:
        heuristic_only = True
    regime_s, vol_s = _coerce_regime_vol(data, regime, vol, sentiment=sentiment)
    data_empty = data is None or getattr(data, "empty", True)
    try:
        if data_empty or heuristic_only:
            raise RuntimeError("empty_data" if data_empty else "heuristic_only")
        summary, ctx_block = _thinking_context_for_ai(data, regime_s, vol_s)
    except Exception as exc:
        if str(exc) in ("empty_data", "heuristic_only"):
            logger.debug("analyze_regime_with_ai using partial context: %s", exc)
        else:
            logger.warning("analyze_regime_with_ai context failed: %s", exc)
        summary = {
            "regime": regime_s,
            "vol": vol_s,
            "spy_trend": "n/a",
            "bubble_score_100": "n/a",
            "sector_leadership": "n/a",
            "crowded_trade_warning": "",
            "conviction_score": 0.5,
            "stat_arb_regime": "n/a",
        }
        ctx_block = f"REGIME: {regime_s} | vol={vol_s}\n(partial context — build failed)"

    heuristic_conv = 0.5
    try:
        heuristic_conv = float(summary.get("conviction_score") or 0.5)
    except (TypeError, ValueError):
        heuristic_conv = 0.5

    fallback = _normalize_structured_output(
        {
            "regime_label": regime_s,
            "conviction_score": round(heuristic_conv, 3),
            "signal_strength": round(heuristic_conv, 3),
            "risk_posture": "neutral",
            "suggested_action": "hold current sleeve weights",
            "reasoning": (
                f"{regime_s} vol={vol_s}; bubble {summary.get('bubble_score_100', 'n/a')}/100; "
                f"SPY {summary.get('spy_trend', 'n/a')}"
            ),
            "key_drivers": [
                str(summary.get("sector_leadership") or "n/a")[:80],
                str(summary.get("crowded_trade_warning") or "")[:80],
            ],
            "sleeve_bias": {"vti": "neutral", "spy": "neutral", "cash": "neutral"},
            "confidence": 0.35,
        },
        "regime_analysis",
    )
    if data_empty or heuristic_only:
        fallback["regime"] = regime_s
        fallback["vol"] = vol_s
        fallback["source"] = "heuristic"
        if force_fallback:
            fallback["fallback_reason"] = "force_fallback"
        else:
            fallback["fallback_reason"] = "empty_data" if data_empty else "heuristic_only"
        return fallback
    prompt = f"""CONTEXT:
{ctx_block}

Sector leaders: {summary.get('sector_leadership', 'n/a')}
Stat-arb regime: {summary.get('stat_arb_regime', 'n/a')}

JSON schema:
{{
  "signal_strength": 0.0-1.0,
  "confidence": 0.0-1.0,
  "suggested_action": "specific sleeve tilt (e.g. 'raise cash 5%, trim SPY 3%')",
  "reasoning": "max 2 sentences, cite CONTEXT facts only",
  "regime_label": "{regime_s}",
  "conviction_score": 0.0-1.0,
  "risk_posture": "risk-on|risk-off|neutral",
  "key_drivers": ["fact1", "fact2"],
  "sleeve_bias": {{"vti": "over|under|neutral", "spy": "...", "cash": "...", "crypto": "...", "nyse": "..."}}
}}"""
    try:
        result = _structured_ai_call(
            "regime_analysis",
            _trading_system_prompt("regime_analysis"),
            prompt,
            model=model,
            fast_model=fast_model,
            fallback=fallback,
            timeout_sec=timeout_sec,
            force_fallback=force_fallback,
        )
    except Exception as exc:
        logger.warning("analyze_regime_with_ai LLM path failed: %s", exc)
        result = dict(fallback)
        result["fallback_reason"] = f"exception:{type(exc).__name__}"
    try:
        conv = float(result.get("conviction_score", result.get("signal_strength", heuristic_conv)))
        result["conviction_score"] = round(max(0.0, min(1.0, conv)), 3)
    except (TypeError, ValueError):
        result["conviction_score"] = round(heuristic_conv, 3)
    result["regime"] = regime_s
    result["vol"] = vol_s
    return result


def suggest_stat_arb_pairs(
    data,
    regime: str | None = None,
    vol: str | None = None,
    *,
    model: str | None = None,
    fast_model: bool = False,
    max_pairs: int = 5,
) -> dict[str, Any]:
    """AI quality scoring for stat-arb candidates — only pairs listed in CONTEXT."""
    regime_s, vol_s = _coerce_regime_vol(data, regime, vol)
    summary, ctx_block = _thinking_context_for_ai(data, regime_s, vol_s)
    candidates = list(summary.get("stat_arb_candidates") or [])[:8]
    allowed_pairs = {str(c.get("pair")) for c in candidates if c.get("pair")}
    fallback_pairs = []
    for row in candidates[:max_pairs]:
        score = float(row.get("score") or 0.0)
        z = abs(float(row.get("z") or 0.0))
        quality = round(min(100.0, score * 40 + z * 8 + float(row.get("corr") or 0) * 20), 1)
        action = "long_spread" if float(row.get("z") or 0) < 0 else "short_spread"
        fallback_pairs.append(
            {
                "pair": row.get("pair"),
                "signal_strength": round(quality / 100.0, 3),
                "quality_score": quality,
                "suggested_action": action,
                "action": action,
                "reasoning": f"z={row.get('z')} corr={row.get('corr')} rule scan",
            }
        )
    top_action = fallback_pairs[0]["suggested_action"] if fallback_pairs else "no trade"
    fallback = _normalize_structured_output(
        {
            "signal_strength": fallback_pairs[0]["signal_strength"] if fallback_pairs else 0.0,
            "confidence": 0.4 if fallback_pairs else 0.2,
            "suggested_action": top_action,
            "reasoning": str(summary.get("stat_arb_candidate_summary") or "no pairs")[:200],
            "pairs": fallback_pairs,
            "market_spread_regime": str(summary.get("stat_arb_regime") or "unknown"),
        },
        "stat_arb_pairs",
    )
    prompt = f"""CONTEXT:
{ctx_block}

Candidate pairs (ONLY score these — do not invent symbols):
{json.dumps(candidates, default=str)}

JSON schema:
{{
  "signal_strength": 0.0-1.0,
  "confidence": 0.0-1.0,
  "suggested_action": "top pair trade or 'stand down'",
  "reasoning": "max 2 sentences",
  "market_spread_regime": "mean_reverting|trending|hostile",
  "pairs": [
    {{
      "pair": "LONG/SHORT",
      "signal_strength": 0.0-1.0,
      "quality_score": 0-100,
      "suggested_action": "long_spread|short_spread|avoid",
      "reasoning": "one line citing z/corr from CONTEXT"
    }}
  ]
}}
Max {max_pairs} pairs. Reject pairs not in the candidate list."""
    result = _structured_ai_call(
        "stat_arb_pairs",
        _trading_system_prompt("stat_arb_pairs"),
        prompt,
        model=model,
        fast_model=fast_model,
        fallback=fallback,
    )
    pairs = result.get("pairs")
    if not isinstance(pairs, list):
        result["pairs"] = fallback_pairs
    else:
        filtered = [p for p in pairs if str(p.get("pair") or "") in allowed_pairs or not allowed_pairs]
        result["pairs"] = filtered[:max_pairs] if filtered else fallback_pairs
    return result


def validate_short_signal(
    data,
    regime: str,
    *,
    volatility: str | None = None,
    vol_score: float | None = None,
    model: str | None = None,
    fast_model: bool = False,
) -> dict[str, Any]:
    """Validate protective short triggers — conservative, fact-bound."""
    from modules.pipeline_strategies import evaluate_short_entry_triggers

    vol_label = volatility or "normal"
    triggers = evaluate_short_entry_triggers(
        data, regime, volatility=volatility, vol_score=vol_score
    )
    summary, ctx_block = _thinking_context_for_ai(data, regime, vol_label)
    allowed = bool(triggers.get("allowed"))
    bubble = float(triggers.get("bubble_score_100") or summary.get("bubble_score_100") or 0.0)
    verdict = "approve" if allowed else "reject"
    fallback = _normalize_structured_output(
        {
            "signal_strength": 0.7 if allowed else 0.2,
            "confidence": 0.5 if allowed else 0.4,
            "suggested_action": "open protective SPY short" if allowed else "no short — stand down",
            "reasoning": (
                f"Rules: allowed={allowed} reject={triggers.get('reject')} "
                f"bubble={bubble:.0f}/100 path={triggers.get('regime_path')}"
            ),
            "valid": allowed,
            "verdict": verdict,
            "trigger_alignment": 0.75 if allowed else 0.25,
            "risks": ["VIX reversal", "squeeze if exhaustion fades"][:2],
        },
        "short_validation",
    )
    prompt = f"""CONTEXT:
{ctx_block}

Rule-engine (authoritative):
{json.dumps({k: triggers.get(k) for k in ('allowed', 'reject', 'regime_path', 'bubble_score_100', 'vix_reason', 'exhaustion_reason', 'bear_streak')}, default=str)}

JSON schema:
{{
  "signal_strength": 0.0-1.0,
  "confidence": 0.0-1.0,
  "suggested_action": "open protective SPY short | reduce short size | no short — stand down",
  "reasoning": "max 2 sentences; defer to rule-engine when conflict",
  "valid": true|false,
  "verdict": "approve|reject|wait",
  "trigger_alignment": 0.0-1.0,
  "risks": ["max 2 specific risks"]
}}"""
    result = _structured_ai_call(
        "short_validation",
        _trading_system_prompt("short_validation"),
        prompt,
        model=model,
        fast_model=fast_model,
        fallback=fallback,
    )
    result["triggers"] = triggers
    return result


def weekly_strategy_review(
    context: dict[str, Any],
    *,
    model: str | None = None,
    fast_model: bool = False,
) -> dict[str, Any]:
    """Weekly review — actionable focus items only."""
    ctx_block = format_thinking_context_block(context) if context.get("regime") else ""
    compact = {
        k: context.get(k)
        for k in (
            "regime", "vol", "return_30d_pct", "sharpe_30d", "health_score",
            "best_sleeve", "worst_sleeve", "bubble_score_100", "insider_summary",
            "stat_arb_candidate_summary", "news_summary",
        )
        if context.get(k) is not None
    }
    fallback = _normalize_structured_output(
        {
            "signal_strength": 0.4,
            "confidence": 0.35,
            "suggested_action": "hold core VTI; review stat-arb pair filters",
            "reasoning": f"30d return {context.get('return_30d_pct')}% sharpe {context.get('sharpe_30d')}",
            "headline": "Weekly review (heuristic fallback)",
            "what_worked": [str(context.get("best_sleeve") or "VTI core")],
            "what_failed": [str(context.get("worst_sleeve") or "n/a")],
            "next_week_actions": [
                "tighten stat-arb z-entry if vol stays high",
                "watch insider cluster buys for NYSE entries",
            ],
            "regime_outlook": str(context.get("regime") or "unchanged"),
        },
        "weekly_review",
    )
    prompt = f"""CONTEXT:
{ctx_block or json.dumps(compact, default=str)}

Performance: {json.dumps(compact, default=str)}

JSON schema:
{{
  "signal_strength": 0.0-1.0,
  "confidence": 0.0-1.0,
  "suggested_action": "one specific portfolio adjustment",
  "reasoning": "max 2 sentences",
  "headline": "max 12 words",
  "what_worked": ["max 2 items"],
  "what_failed": ["max 2 items"],
  "next_week_actions": ["3 specific trading tasks — no generic advice"],
  "regime_outlook": "one sentence"
}}
Benchmark: beat VTI Sharpe. Max sleeve delta ±6%."""
    return _structured_ai_call(
        "weekly_review",
        _trading_system_prompt("weekly_review"),
        prompt,
        model=model,
        fast_model=fast_model,
        fallback=fallback,
    )


def run_full_thinking_cycle(
    data,
    regime: str,
    vol: str,
    *,
    model: str | None = None,
    fast_model: bool = False,
    include_market_reasoning: bool = False,
) -> dict[str, Any]:
    """Run all structured capabilities + optional sleeve tilt reasoning."""
    summary = build_market_summary(data, regime, vol)
    ctx_block = format_thinking_context_block(summary)
    cycle: dict[str, Any] = {
        "regime": regime,
        "vol": vol,
        "context_block": ctx_block,
        "enriched_context": {
            k: summary.get(k)
            for k in (
                "insider_cluster_buys", "insider_executive_sells", "insider_summary",
                "bubble_score_100", "buffett_signal", "buffett_ratio_pct",
                "news_summary", "news_headline_lines", "stat_arb_candidates",
                "stat_arb_candidate_summary", "options_flow_summary", "unusual_activity",
                "short_interest_summary", "short_interest_watch",
            )
            if summary.get(k) is not None
        },
        "regime_analysis": analyze_regime_with_ai(
            data, regime, vol, model=model, fast_model=fast_model
        ),
        "stat_arb": suggest_stat_arb_pairs(
            data, regime, vol, model=model, fast_model=fast_model, max_pairs=4
        ),
        "short_validation": validate_short_signal(
            data, regime, volatility=vol, model=model, fast_model=fast_model
        ),
        "weekly_review": weekly_strategy_review(
            {
                **summary,
                "return_30d_pct": summary.get("return_30d_pct"),
                "sharpe_30d": summary.get("sharpe_30d"),
                "health_score": summary.get("health_score"),
            },
            model=model,
            fast_model=fast_model,
        ),
    }
    if include_market_reasoning:
        cycle["market_reasoning"] = get_market_reasoning(
            summary, model=model, fast_model=fast_model
        )
    return cycle


def test_ollama_thinking(
    data=None,
    *,
    regime: str | None = None,
    vol: str | None = None,
    sentiment=None,
    fast_model: bool = True,
    timeout_sec: int = 90,
    heuristic_only: bool = False,
    force_fallback: bool = False,
) -> dict[str, Any]:
    """Smoke-test regime + tilt analyzers with partial data (None-safe).

    Usage::

        from modules.thinking_engine import test_ollama_thinking
        test_ollama_thinking()

    Or::

        data = load_pipeline_data()
        print(analyze_regime_with_ai(data))
        print(analyze_tilt_with_ai(data))

    Pass ``heuristic_only=True`` or ``force_fallback=True`` to skip Ollama
    (fast CI / forced heuristic path).
    """
    status = check_ollama_startup_status(force=True)
    print("ollama_startup:", status.get("banner") or status)

    if force_fallback:
        heuristic_only = True

    if data is None and not heuristic_only:
        try:
            from modules.pipeline_strategies import load_pipeline_data

            data = load_pipeline_data()
        except Exception as exc:
            print(f"test_ollama_thinking: load_pipeline_data failed ({exc})")
            data = None
    if data is None or getattr(data, "empty", True):
        print("test_ollama_thinking: no pipeline data — using empty frame (heuristic)")
        import pandas as pd

        data = pd.DataFrame()
        if not force_fallback:
            heuristic_only = True

    # Explicit None sentiment/vol path (must not raise).
    regime_out = analyze_regime_with_ai(
        data,
        regime,
        vol,
        sentiment=sentiment,
        fast_model=fast_model,
        timeout_sec=timeout_sec,
        heuristic_only=heuristic_only,
        force_fallback=force_fallback,
    )
    tilt_out = analyze_tilt_with_ai(
        data,
        regime,
        vol,
        sentiment=sentiment,
        fast_model=fast_model,
        timeout_sec=timeout_sec,
        heuristic_only=heuristic_only,
        force_fallback=force_fallback,
    )
    print(regime_out)
    print(tilt_out)
    return {
        "regime_analysis": regime_out,
        "tilt_analysis": tilt_out,
        "ollama_startup": status,
    }


if __name__ == "__main__":
    import sys

    # Default CLI: fast partial-data check.
    #   --llm            hit Ollama
    #   --force-fallback  force heuristic path (no Ollama calls)
    use_llm = "--llm" in sys.argv
    force_fb = "--force-fallback" in sys.argv
    test_ollama_thinking(
        heuristic_only=not use_llm and not force_fb,
        force_fallback=force_fb,
    )

