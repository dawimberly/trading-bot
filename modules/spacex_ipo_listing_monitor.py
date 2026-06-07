"""Monitor the real SpaceX IPO (Nasdaq SPCX): SEC milestones + Alpaca tradability."""

from __future__ import annotations

import datetime
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

import config

ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = ROOT / config.SPACEX_IPO_LISTING_CACHE_FILE
HISTORY_PATH = ROOT / config.SPACEX_IPO_LISTING_HISTORY_FILE
USER_AGENT = config.SEC_USER_AGENT

SEC_SUBMISSIONS_URL = (
    "https://data.sec.gov/submissions/CIK{cik}.json"
)
IPO_NEWS_RSS = (
    "https://news.google.com/rss/search?q=SpaceX+IPO+begins+trading+Nasdaq+SPCX&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=SpaceX+IPO+priced+SPCX+listing&hl=en-US&gl=US&ceid=US:en",
)

STAGE_ORDER = (
    "watching",
    "s1_filed",
    "s1_amended",
    "registration_effective",
    "exchange_registered",
    "final_prospectus",
    "alpaca_tradable",
    "kraken_tradable",
    "trading_live",
)

FORM_TO_STAGE = {
    "S-1": "s1_filed",
    "S-1/A": "s1_amended",
    "EFFECT": "registration_effective",
    "8-A12B": "exchange_registered",
    "424B1": "final_prospectus",
    "424B4": "final_prospectus",
    "424B5": "final_prospectus",
}


def _load_cache() -> dict | None:
    if not CACHE_PATH.exists():
        return None
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(payload: dict) -> None:
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _append_history(payload: dict) -> None:
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")


def _cache_fresh(cached: dict) -> bool:
    ts = cached.get("fetched_at")
    if not ts:
        return False
    fetched = datetime.datetime.fromisoformat(ts)
    age = datetime.datetime.now() - fetched
    return age.total_seconds() < config.SPACEX_IPO_LISTING_CACHE_HOURS * 3600


def _cik_padded() -> str:
    return str(config.SPACEX_IPO_CIK).zfill(10)


def _fetch_sec_milestones() -> dict:
    url = SEC_SUBMISSIONS_URL.format(cik=_cik_padded())
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])

    milestones = []
    stage = "watching"
    for form, fdate, acc in zip(forms, dates, accessions):
        if form not in FORM_TO_STAGE:
            continue
        entry = {
            "form": form,
            "date": fdate,
            "accession": acc,
            "stage": FORM_TO_STAGE[form],
        }
        milestones.append(entry)

    for entry in milestones:
        idx = STAGE_ORDER.index(entry["stage"]) if entry["stage"] in STAGE_ORDER else -1
        cur_idx = STAGE_ORDER.index(stage) if stage in STAGE_ORDER else -1
        if idx > cur_idx:
            stage = entry["stage"]

    return {
        "company": data.get("name"),
        "cik": data.get("cik"),
        "ticker_target": config.SPACEX_IPO_TICKER,
        "sec_stage": stage,
        "milestones": milestones[:12],
        "latest_filings": [
            {"form": f, "date": d}
            for f, d in zip(forms[:10], dates[:10])
        ],
    }


def _fetch_ipo_news() -> list[dict]:
    headlines = []
    for url in IPO_NEWS_RSS:
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
        except (requests.RequestException, ET.ParseError):
            continue
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not title:
                continue
            t = title.lower()
            if "spacex" not in t and "spcx" not in t:
                continue
            headlines.append({"title": title, "published": pub, "link": link})
    return headlines[:8]


def _days_until_listing() -> int | None:
    raw = (config.SPACEX_IPO_EXPECTED_DATE or "").strip()
    if not raw:
        return None
    try:
        target = datetime.date.fromisoformat(raw)
    except ValueError:
        return None
    return (target - datetime.date.today()).days


def check_alpaca_tradable(executor=None) -> dict:
    """Live check — call every bot cycle near listing date."""
    symbol = config.SPACEX_IPO_TICKER
    result = {
        "symbol": symbol,
        "found": False,
        "tradable": False,
        "status": None,
        "fractionable": None,
        "error": None,
    }
    try:
        if executor is None:
            from modules.alpaca_executor import AlpacaExecutor

            executor = AlpacaExecutor()
        asset = executor.client.get_asset(symbol)
        result["found"] = True
        result["tradable"] = bool(getattr(asset, "tradable", False))
        result["status"] = str(getattr(asset, "status", ""))
        result["fractionable"] = bool(getattr(asset, "fractionable", False))
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _resolve_stage(sec: dict, alpaca: dict, kraken: dict) -> str:
    if alpaca.get("tradable") or kraken.get("tradable"):
        return "trading_live"
    if alpaca.get("found"):
        return "alpaca_tradable"
    if kraken.get("found"):
        return "kraken_tradable"
    return sec.get("sec_stage", "watching")


def get_spacex_ipo_listing_status(
    *,
    executor=None,
    force_refresh: bool = False,
) -> dict | None:
    """
    Full IPO listing snapshot: SEC milestones, Alpaca + Kraken Pro tradability.
    SEC/news cached; Alpaca and Kraken checked every call.
    """
    if not config.SPACEX_IPO_LISTING_MONITOR_ENABLED:
        return None

    from modules.kraken_equity_monitor import check_kraken_spcx_tradable

    cached = _load_cache() if not force_refresh else None
    sec_block = None
    news = []

    if cached and _cache_fresh(cached):
        sec_block = cached.get("sec")
        news = cached.get("news") or []
    else:
        try:
            sec_block = _fetch_sec_milestones()
            news = _fetch_ipo_news()
        except requests.RequestException as exc:
            if cached:
                sec_block = cached.get("sec")
                news = cached.get("news") or []
            else:
                sec_block = {"sec_stage": "watching", "error": str(exc), "milestones": []}

    alpaca = check_alpaca_tradable(executor)
    kraken = check_kraken_spcx_tradable()
    days = _days_until_listing()
    stage = _resolve_stage(sec_block or {}, alpaca, kraken)

    payload = {
        "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "ticker": config.SPACEX_IPO_TICKER,
        "expected_listing_date": config.SPACEX_IPO_EXPECTED_DATE,
        "days_until_expected": days,
        "stage": stage,
        "sec": sec_block,
        "alpaca": alpaca,
        "kraken": kraken,
        "news": news,
        "ready_to_buy_alpaca": bool(alpaca.get("tradable")),
        "ready_to_buy_kraken": bool(kraken.get("tradable")),
        "ready_to_buy": bool(alpaca.get("tradable") or kraken.get("tradable")),
        "auto_buy_alpaca": config.SPACEX_IPO_AUTO_BUY
        and (config.PAPER_TRADING or config.ALLOW_LIVE_TRADING),
        "auto_buy_kraken": config.KRAKEN_SPCX_BUY_ENABLED,
    }

    prev = cached or {}
    stage_changed = prev.get("stage") != stage
    became_tradable_alpaca = not (prev.get("alpaca") or {}).get("tradable") and alpaca.get(
        "tradable"
    )
    became_tradable_kraken = not (prev.get("kraken") or {}).get("tradable") and kraken.get(
        "tradable"
    )

    if not cached or not _cache_fresh(cached) or stage_changed or became_tradable_alpaca or became_tradable_kraken:
        _save_cache(payload)
    if stage_changed or became_tradable_alpaca or became_tradable_kraken:
        _append_history(
            {
                "at": payload["fetched_at"],
                "stage": stage,
                "alpaca_tradable": alpaca.get("tradable"),
                "kraken_tradable": kraken.get("tradable"),
                "kraken_pair": kraken.get("pair"),
                "days_until": days,
            }
        )

    payload["stage_changed"] = stage_changed
    payload["became_tradable_alpaca"] = became_tradable_alpaca
    payload["became_tradable_kraken"] = became_tradable_kraken
    payload["became_tradable"] = became_tradable_alpaca or became_tradable_kraken
    return payload


def format_listing_line(snapshot: dict | None) -> str:
    if not snapshot:
        return "SpaceX IPO listing monitor: off"
    days = snapshot.get("days_until_expected")
    days_s = f"{days}d" if days is not None else "n/a"
    alpaca = snapshot.get("alpaca") or {}
    kraken = snapshot.get("kraken") or {}
    a = "YES" if alpaca.get("tradable") else "not yet"
    k = kraken.get("wsname") or kraken.get("pair") or "not yet"
    if kraken.get("tradable"):
        k = f"YES ({k})"
    return (
        f"SpaceX IPO ({snapshot.get('ticker')}): stage={snapshot.get('stage')} | "
        f"expected {snapshot.get('expected_listing_date')} ({days_s}) | "
        f"Alpaca: {a} | Kraken: {k}"
    )
