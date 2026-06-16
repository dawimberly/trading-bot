"""Optional Grok narrative analysis for UFC card edges (non-blocking, Kelly adjust)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)

_GROK_CACHE_DIR = config.CACHE_DIR / "grok_analysis"


def grok_available() -> bool:
    """True when Grok integration is enabled and an API key is configured."""
    return bool(config.GROK_ENABLED and config.GROK_API_KEY)


def clamp_kelly_factor(value: Any) -> float:
    """Clamp Grok Kelly multiplier to configured bounds."""
    try:
        factor = float(value)
    except (TypeError, ValueError):
        factor = 1.0
    return round(
        max(config.GROK_KELLY_ADJ_MIN, min(config.GROK_KELLY_ADJ_MAX, factor)),
        3,
    )


def _extract_json_blob(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"picks": parsed}
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        return json.loads(fenced.group(1))

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("Grok response did not contain parseable JSON.")


def _pick_id(item: dict[str, Any]) -> str:
    for key in ("id", "fight_id", "label", "pick_line", "pick"):
        val = str(item.get(key) or "").strip()
        if val:
            return val
    return ""


def normalize_grok_result(raw: dict[str, Any], *, event_label: str = "") -> dict[str, Any]:
    """Normalize Grok JSON into dashboard-friendly structure."""
    picks_in = raw.get("picks") or raw.get("analyses") or []
    if not isinstance(picks_in, list):
        picks_in = []

    picks: list[dict[str, Any]] = []
    for row in picks_in:
        if not isinstance(row, dict):
            continue
        pid = _pick_id(row)
        risks = row.get("invalidation_risks") or row.get("risks") or []
        if isinstance(risks, str):
            risks = [r.strip() for r in risks.split(";") if r.strip()]
        picks.append(
            {
                "id": pid,
                "pick_type": str(row.get("pick_type") or row.get("type") or "moneyline"),
                "narrative_edge": str(row.get("narrative_edge") or row.get("narrative") or "").strip(),
                "crowd_positioning": str(
                    row.get("crowd_positioning") or row.get("crowd") or ""
                ).strip(),
                "invalidation_risks": [str(r).strip() for r in risks if str(r).strip()],
                "kelly_adjustment": clamp_kelly_factor(
                    row.get("kelly_adjustment") or row.get("kelly_factor") or 1.0
                ),
                "conviction": str(row.get("conviction") or row.get("confidence") or "medium").lower(),
            }
        )

    return {
        "event": str(raw.get("event") or event_label or ""),
        "summary": str(raw.get("summary") or raw.get("card_summary") or "").strip(),
        "picks": picks,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "source": "grok",
    }


def build_grok_prompt(inputs: dict[str, Any]) -> str:
    """Structured prompt for narrative edge + Kelly adjustment per pick."""
    event = inputs.get("event") or "Upcoming UFC card"
    fights = inputs.get("fights") or []
    props = inputs.get("props") or []

    fight_lines: list[str] = []
    for f in fights:
        fight_lines.append(
            f"- id={f.get('fight_id') or f.get('pick_line')} | "
            f"{f.get('pick_line') or f.get('fight')} | "
            f"type=moneyline | model_prob={f.get('prob')} | edge={f.get('edge_pct')}% | "
            f"book={f.get('book')} | odds={f.get('odds_display')} | "
            f"model_confidence={f.get('confidence')}"
        )

    prop_lines: list[str] = []
    for p in props:
        prop_lines.append(
            f"- id={p.get('id') or p.get('label')} | "
            f"{p.get('label')} | type=prop | model_prob={p.get('prob')} | "
            f"edge={p.get('edge_pct')}% | book={p.get('book')} | odds={p.get('odds')}"
        )

    picks_block = "\n".join(fight_lines + prop_lines) or "- (no ranked picks on card)"

    return f"""You are an elite UFC betting analyst. Review the model's top edges for {event}.

For EACH pick below, assess:
1) narrative_edge — why the model may be right or wrong (styles, camp news, weight cut, cardio, judging)
2) crowd_positioning — where public/recency bias likely sits vs the line
3) invalidation_risks — 2-3 concrete scenarios that break the thesis
4) kelly_adjustment — multiplier for fractional Kelly sizing in range [{config.GROK_KELLY_ADJ_MIN}, {config.GROK_KELLY_ADJ_MAX}]
   - 1.0 = neutral (trust model sizing)
   - >1.0 = increase conviction (max {config.GROK_KELLY_ADJ_MAX})
   - <1.0 = reduce conviction (min {config.GROK_KELLY_ADJ_MIN})

Picks to analyze:
{picks_block}

Reply with ONLY valid JSON (no markdown prose outside the JSON):
{{
  "event": "{event}",
  "summary": "one paragraph card-level read",
  "picks": [
    {{
      "id": "same id string from input",
      "pick_type": "moneyline|prop",
      "narrative_edge": "...",
      "crowd_positioning": "...",
      "invalidation_risks": ["...", "..."],
      "kelly_adjustment": 1.0,
      "conviction": "high|medium|low"
    }}
  ]
}}"""


def collect_card_analysis_inputs(
    books: dict[str, dict[str, Any]],
    budget_state: dict[str, Any] | None,
    *,
    event_label: str = "",
    max_fights: int | None = None,
    max_props: int | None = None,
) -> dict[str, Any]:
    """Gather top moneyline singles and prop lines for Grok."""
    from src.strategy import aggregate_top_recommended_bets

    fight_cap = max_fights if max_fights is not None else config.GROK_MAX_FIGHTS
    prop_cap = max_props if max_props is not None else config.GROK_MAX_PROPS
    bs = budget_state or {}

    fights = aggregate_top_recommended_bets(books, bs, limit=fight_cap)
    fight_payload = [
        {
            "fight_id": f.get("fight_id"),
            "fight": f.get("fight"),
            "pick_line": f.get("pick_line"),
            "pick": f.get("pick"),
            "prob": f.get("prob"),
            "edge_pct": f.get("edge_pct"),
            "book": f.get("book"),
            "odds_display": f.get("odds_display"),
            "confidence": f.get("confidence"),
        }
        for f in fights
    ]

    props: list[dict[str, Any]] = []
    if config.ENABLE_PROPS:
        for book in ("DraftKings", "BetNow.eu", "MyBookie"):
            book_data = books.get(book, {}) or {}
            singles = (book_data.get("props") or {}).get("singles") or []
            for s in singles:
                label = str(s.get("label") or "").strip()
                if not label:
                    continue
                props.append(
                    {
                        "id": label,
                        "label": label,
                        "prob": s.get("prob"),
                        "edge_pct": s.get("edge_pct"),
                        "book": book,
                        "odds": s.get("odds"),
                        "pick_type": "prop",
                    }
                )
                if len(props) >= prop_cap:
                    break
            if len(props) >= prop_cap:
                break

    return {
        "event": event_label,
        "fights": fight_payload,
        "props": props[:prop_cap],
    }


def _cache_key(inputs: dict[str, Any]) -> str:
    blob = json.dumps(inputs, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _load_cache(key: str) -> dict[str, Any] | None:
    path = _GROK_CACHE_DIR / f"{key}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cached_at = data.get("cached_at")
        if cached_at:
            ts = datetime.fromisoformat(str(cached_at).replace("Z", "+00:00"))
            age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
            if age_h > config.GROK_CACHE_TTL_HOURS:
                return None
        return data.get("result")
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _save_cache(key: str, result: dict[str, Any]) -> None:
    try:
        _GROK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "result": result,
        }
        (_GROK_CACHE_DIR / f"{key}.json").write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.debug("Grok cache write failed: %s", exc)


def query_grok(prompt: str) -> str:
    """Call xAI chat completions API. Raises on missing key or HTTP errors."""
    if not config.GROK_API_KEY:
        raise RuntimeError("GROK_API_KEY or XAI_API_KEY not set in .env")

    url = f"{config.GROK_API_BASE.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.GROK_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": config.GROK_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a concise UFC betting analyst. "
                    "Respond with valid JSON only — no commentary outside the JSON object."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.25,
    }
    resp = requests.post(
        url,
        headers=headers,
        json=body,
        timeout=config.GROK_TIMEOUT_SEC,
    )
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("Grok API returned no choices.")
    message = choices[0].get("message") or {}
    content = message.get("content") or ""
    if not str(content).strip():
        raise RuntimeError("Grok API returned empty content.")
    return str(content)


def analyze_card_with_grok(
    books: dict[str, dict[str, Any]],
    budget_state: dict[str, Any] | None,
    *,
    event_label: str = "",
    use_cache: bool = True,
) -> dict[str, Any]:
    """
    Run Grok analysis on top fights/props. Optional cache; never required for dashboard refresh.
    """
    if not grok_available():
        return {
            "ok": False,
            "error": "Grok disabled — set GROK_ENABLED=true and GROK_API_KEY in .env",
            "picks": [],
        }

    inputs = collect_card_analysis_inputs(
        books,
        budget_state,
        event_label=event_label,
    )
    if not inputs.get("fights") and not inputs.get("props"):
        return {
            "ok": False,
            "error": "No ranked fights or props to analyze — refresh card first.",
            "picks": [],
        }

    cache_key = _cache_key(inputs)
    if use_cache:
        cached = _load_cache(cache_key)
        if cached:
            out = dict(cached)
            out["from_cache"] = True
            out["ok"] = True
            return out

    prompt = build_grok_prompt(inputs)
    try:
        raw_text = query_grok(prompt)
        parsed = _extract_json_blob(raw_text)
        result = normalize_grok_result(parsed, event_label=event_label or inputs.get("event", ""))
        result["ok"] = True
        result["from_cache"] = False
        result["model"] = config.GROK_MODEL
        _save_cache(cache_key, result)
        return result
    except Exception as exc:
        logger.warning("Grok analysis failed: %s", exc)
        return {
            "ok": False,
            "error": str(exc),
            "picks": [],
            "event": event_label,
        }


def _lookup_pick(grok_picks: list[dict[str, Any]], bet: dict[str, Any]) -> dict[str, Any] | None:
    keys = [
        str(bet.get("fight_id") or "").strip(),
        str(bet.get("pick_line") or "").strip(),
        str(bet.get("fight") or "").strip(),
        str(bet.get("pick") or "").strip(),
        str(bet.get("label") or "").strip(),
    ]
    by_id = {str(p.get("id") or "").strip(): p for p in grok_picks if p.get("id")}
    for key in keys:
        if key and key in by_id:
            return by_id[key]
    return None


def apply_grok_kelly_adjustments(
    bets: list[dict[str, Any]],
    grok_result: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Apply Grok kelly_adjustment to top bet sizing fields (display + stake hints)."""
    if not grok_result or not grok_result.get("ok"):
        return bets

    grok_picks = grok_result.get("picks") or []
    if not grok_picks:
        return bets

    adjusted: list[dict[str, Any]] = []
    for bet in bets:
        row = dict(bet)
        item = _lookup_pick(grok_picks, row)
        if not item:
            adjusted.append(row)
            continue
        factor = clamp_kelly_factor(item.get("kelly_adjustment", 1.0))
        row["grok_kelly_factor"] = factor
        row["grok_narrative"] = item.get("narrative_edge", "")
        row["grok_crowd"] = item.get("crowd_positioning", "")
        row["grok_risks"] = item.get("invalidation_risks") or []
        row["grok_conviction"] = item.get("conviction", "medium")
        for field in ("kelly_stake_usd", "kelly_pct", "max_safe_bet_usd", "suggested_stake"):
            if row.get(field) is not None:
                try:
                    row[field] = round(float(row[field]) * factor, 2)
                except (TypeError, ValueError):
                    pass
        adjusted.append(row)
    return adjusted
