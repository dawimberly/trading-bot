"""MyBookie.ag UFC odds scraper (requests + BeautifulSoup)."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import pandas as pd
import requests

import config
from src.data_loader import ensure_data_dirs
from src.odds_providers.prop_odds_common import (
    american_to_decimal,
    empty_prop_odds_df,
    parse_american_odds,
    prop_row,
)
from src.predictor import OddsAPIError, _implied_probs

logger = logging.getLogger(__name__)

MYBOOKIE_CACHE_PATH = config.CACHE_DIR / "mybookie_odds.csv"
MYBOOKIE_PROP_CACHE_PATH = config.CACHE_DIR / "mybookie_prop_odds.csv"
MYBOOKIE_UFC_URL = config.MYBOOKIE_UFC_URL
MYBOOKIE_PROPS_URL = config.MYBOOKIE_PROPS_URL
MYBOOKIE_URLS = [MYBOOKIE_UFC_URL, "https://www.mybookie.ag/sportsbook/mma/"]
_ODDS_API_BOOK_KEY = "mybookieag"

_METHOD_PROP_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(.+?)\s+by\s+ko(?:/tko)?$", re.I), "fighter_ko"),
    (re.compile(r"^(.+?)\s+by\s+submission$", re.I), "fighter_sub"),
]
_SKIP_PROP_RE = re.compile(r"^draw$|by\s+decision$|&\s*\d|^\{\{\{", re.I)

_PROP_LABEL_MAP: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"goes?\s+to\s+decision|fight\s+goes\s+to\s+decision", re.I), "goes_to_decision", "Yes"),
    (re.compile(r"inside\s+distance|does\s+not\s+go\s+to\s+decision", re.I), "finish", "Yes"),
    (re.compile(r"\bko\b|ko/tko|wins?\s+by\s+ko", re.I), "ko_tko", "Yes"),
    (re.compile(r"submission|wins?\s+by\s+sub", re.I), "submission", "Yes"),
    (re.compile(r"round\s*1\s+finish|ends?\s+in\s+round\s*1", re.I), "round_1_finish", "Yes"),
    (re.compile(r"over\s*1\.?5|over\s*1\s*½", re.I), "over_1_5_rounds", "Over 1.5"),
    (re.compile(r"under\s*1\.?5|under\s*1\s*½", re.I), "round_1_finish", "Under 1.5"),
    (re.compile(r"wins?\s+by\s+ko", re.I), "fighter_ko", "Yes"),
    (re.compile(r"wins?\s+by\s+sub", re.I), "fighter_sub", "Yes"),
]


def _cache_fresh(path: Any = None) -> bool:
    from pathlib import Path

    p = Path(path or MYBOOKIE_CACHE_PATH)
    if not p.is_file() or config.ODDS_CACHE_TTL_HOURS <= 0:
        return False
    age_h = (time.time() - p.stat().st_mtime) / 3600
    return age_h < config.ODDS_CACHE_TTL_HOURS


def _request_headers() -> dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if config.MYBOOKIE_COOKIE:
        headers["Cookie"] = config.MYBOOKIE_COOKIE.strip()
    return headers


def _normalize_fighter_name(raw: str) -> str:
    """Convert MyBookie 'Last, First' to 'First Last' when applicable."""
    text = " ".join(str(raw or "").split()).strip()
    if "," in text:
        parts = [p.strip() for p in text.split(",", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            return f"{parts[1]} {parts[0]}".strip()
    return text


def _decimal_from_odd_attr(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or "{{" in text:
        return None
    american = parse_american_odds(text)
    if american is not None:
        dec = american_to_decimal(american)
        return dec if dec > 1 else None
    try:
        val = float(text)
        return val if val > 1 else None
    except ValueError:
        return None


def _fighter_names_from_line(container) -> tuple[str, str]:
    home_el = container.select_one(".game-line__home-team__name")
    away_el = container.select_one(".game-line__visitor-team__name")
    home_raw = (home_el.get("title") or home_el.get_text(" ", strip=True)) if home_el else ""
    away_raw = (away_el.get("title") or away_el.get_text(" ", strip=True)) if away_el else ""
    return _normalize_fighter_name(home_raw), _normalize_fighter_name(away_raw)


def _parse_moneyline_rows(soup) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for game_line in soup.select(".game-line"):
        container = game_line.select_one(".container-fluid")
        if container is None:
            continue
        f1_name, f2_name = _fighter_names_from_line(container)
        if not f1_name or not f2_name:
            continue

        home_ml = None
        away_ml = None
        game_id = ""
        for btn in container.select("button.lines-odds[data-markettype='ml']"):
            team = _normalize_fighter_name(btn.get("data-team", ""))
            odd = _decimal_from_odd_attr(btn.get("data-odd"))
            if odd is None:
                continue
            if team == f1_name:
                home_ml = odd
            elif team == f2_name:
                away_ml = odd
            if not game_id:
                game_id = str(btn.get("data-gameid", "")).strip()

        if not home_ml or not away_ml:
            continue
        imp1, imp2 = _implied_probs(home_ml, away_ml)
        rows.append(
            {
                "fighter_1": f1_name,
                "fighter_2": f2_name,
                "f1_odds": round(home_ml, 3),
                "f2_odds": round(away_ml, 3),
                "implied_prob_f1": imp1,
                "implied_prob_f2": imp2,
                "bookmaker": "MyBookie",
                "bookmaker_count": 1,
                "source_url": MYBOOKIE_UFC_URL,
                "game_id": game_id,
            }
        )
    return rows


def _parse_totals_props(soup) -> list[dict[str, Any]]:
    props: list[dict[str, Any]] = []
    for game_line in soup.select(".game-line"):
        container = game_line.select_one(".container-fluid")
        if container is None:
            continue
        f1_name, f2_name = _fighter_names_from_line(container)
        if not f1_name or not f2_name:
            continue
        game_id = ""
        for btn in container.select("button.lines-odds[data-markettype='to']"):
            text = btn.get_text(" ", strip=True)
            odd = _decimal_from_odd_attr(btn.get("data-odd"))
            point_raw = btn.get("data-points")
            if odd is None:
                continue
            try:
                point = float(point_raw) if point_raw else 1.5
            except (TypeError, ValueError):
                point = 1.5
            if not game_id:
                game_id = str(btn.get("data-gameid", "")).strip()
            american = parse_american_odds(str(btn.get("data-odd", "")))
            if re.search(r"\bO\b|over", text, re.I):
                props.append(
                    prop_row(
                        fighter_1=f1_name,
                        fighter_2=f2_name,
                        prop_key="over_1_5_rounds",
                        selection="Over 1.5",
                        decimal_odds=odd,
                        bookmaker="MyBookie",
                        odds_source="live",
                        market_key="totals",
                        point=point,
                        rotation=game_id,
                        american_odds=american,
                    )
                )
            elif re.search(r"\bU\b|under", text, re.I):
                props.append(
                    prop_row(
                        fighter_1=f1_name,
                        fighter_2=f2_name,
                        prop_key="round_1_finish",
                        selection="Under 1.5",
                        decimal_odds=odd,
                        bookmaker="MyBookie",
                        odds_source="live",
                        market_key="totals",
                        point=point,
                        rotation=game_id,
                        american_odds=american,
                    )
                )
    return props


def _map_method_prop_description(desc: str) -> tuple[str, str] | None:
    """Map MyBookie fight prop text like 'Lopes, Diego by ko' to prop_key + fighter."""
    text = " ".join(str(desc or "").split()).strip()
    if not text or _SKIP_PROP_RE.search(text):
        return None
    for pattern, prop_key in _METHOD_PROP_PATTERNS:
        m = pattern.match(text)
        if not m:
            continue
        fighter = _normalize_fighter_name(m.group(1))
        if fighter:
            return prop_key, fighter
    for pattern, prop_key, selection in _PROP_LABEL_MAP:
        if pattern.search(text):
            return prop_key, selection
    return None


def _fight_names_from_prop_page(soup) -> tuple[str, str, str]:
    """Best-effort fighter names + game id from a per-fight prop page."""
    game_id = ""
    for btn in soup.select("button.lines-odds[data-gameid]"):
        gid = str(btn.get("data-gameid", "")).strip()
        if gid and "{{" not in gid:
            game_id = gid
            break

    home = soup.select_one(".game-line__home-team__name")
    away = soup.select_one(".game-line__visitor-team__name")
    if home and away:
        f1 = _normalize_fighter_name(home.get("title") or home.get_text(" ", strip=True))
        f2 = _normalize_fighter_name(away.get("title") or away.get_text(" ", strip=True))
        if f1 and f2:
            return f1, f2, game_id

    for btn in soup.select("button.lines-odds[data-team][data-team-vs]"):
        t1 = _normalize_fighter_name(btn.get("data-team", ""))
        t2 = _normalize_fighter_name(btn.get("data-team-vs", ""))
        if t1 and t2 and "{{" not in t1:
            return t1, t2, game_id or str(btn.get("data-gameid", "")).strip()
    return "", "", game_id


def _parse_prop_buttons(soup, *, f1_default: str = "", f2_default: str = "", game_id: str = "") -> list[dict[str, Any]]:
    """Parse prop buttons from a MyBookie UFC or per-fight prop page."""
    props: list[dict[str, Any]] = []
    page_f1, page_f2, page_gid = _fight_names_from_prop_page(soup)
    f1_name = f1_default or page_f1
    f2_name = f2_default or page_f2
    rotation = game_id or page_gid

    for btn in soup.select("button.lines-odds"):
        odd = _decimal_from_odd_attr(btn.get("data-odd"))
        if odd is None:
            continue

        desc = str(btn.get("data-description") or btn.get_text(" ", strip=True)).strip()
        if not desc or "{{" in desc:
            continue

        team = _normalize_fighter_name(btn.get("data-team", ""))
        opponent = _normalize_fighter_name(btn.get("data-team-vs", ""))
        if team and opponent and not f1_name:
            f1_name, f2_name = team, opponent

        market_type = str(btn.get("data-markettype", "")).lower()
        wager_type = str(btn.get("data-wager-type", "")).lower()
        text = btn.get_text(" ", strip=True)

        # Round totals on main card rows
        if market_type == "to" or wager_type == "to":
            fight_f1 = f1_name or team
            fight_f2 = f2_name or opponent
            if not fight_f1 or not fight_f2:
                continue
            point_raw = btn.get("data-points")
            try:
                point = float(point_raw) if point_raw else 1.5
            except (TypeError, ValueError):
                point = 1.5
            american = parse_american_odds(str(btn.get("data-odd", "")))
            if re.search(r"\bO\b|over", text, re.I):
                props.append(
                    prop_row(
                        fighter_1=fight_f1,
                        fighter_2=fight_f2,
                        prop_key="over_1_5_rounds",
                        selection="Over 1.5",
                        decimal_odds=odd,
                        bookmaker="MyBookie",
                        odds_source="live",
                        market_key="totals",
                        point=point,
                        rotation=rotation,
                        american_odds=american,
                    )
                )
            elif re.search(r"\bU\b|under", text, re.I):
                props.append(
                    prop_row(
                        fighter_1=fight_f1,
                        fighter_2=fight_f2,
                        prop_key="round_1_finish",
                        selection="Under 1.5",
                        decimal_odds=odd,
                        bookmaker="MyBookie",
                        odds_source="live",
                        market_key="totals",
                        point=point,
                        rotation=rotation,
                        american_odds=american,
                    )
                )
            continue

        mapped = _map_method_prop_description(desc)
        if not mapped:
            continue
        prop_key, selection_or_fighter = mapped
        fight_f1 = f1_name or team
        fight_f2 = f2_name or opponent
        if not fight_f1 or not fight_f2:
            continue

        if prop_key in ("fighter_ko", "fighter_sub"):
            fighter = selection_or_fighter if isinstance(selection_or_fighter, str) else team
            selection = f"{fighter} Yes"
        else:
            selection = str(selection_or_fighter)

        american = parse_american_odds(str(btn.get("data-odd", "")))
        props.append(
            prop_row(
                fighter_1=fight_f1,
                fighter_2=fight_f2,
                prop_key=prop_key,
                selection=selection,
                decimal_odds=odd,
                bookmaker="MyBookie",
                odds_source="live",
                market_key="prop",
                rotation=rotation,
                american_odds=american,
            )
        )
    return props


def _prop_urls_from_soup(soup) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for a in soup.select("a[data-props-count], .game-line__props a[href*='prop=']"):
        href = str(a.get("href", "")).strip()
        if not href or "prop=" not in href:
            continue
        if href.startswith("http"):
            url = href
        elif href.startswith("/"):
            url = f"https://www.mybookie.ag{href}"
        else:
            url = f"{MYBOOKIE_UFC_URL.rstrip('/')}/{href.lstrip('/')}"
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _scrape_fight_prop_pages(main_soup) -> list[dict[str, Any]]:
    """Fetch each fight's ?prop=GAMEID page for method-of-victory markets."""
    from bs4 import BeautifulSoup

    props: list[dict[str, Any]] = []
    urls = _prop_urls_from_soup(main_soup)
    for url in urls:
        try:
            resp = requests.get(url, headers=_request_headers(), timeout=config.REQUEST_TIMEOUT_SEC)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.debug("MyBookie fight prop page failed %s: %s", url, exc)
            continue
        soup = BeautifulSoup(resp.text, "lxml")
        props.extend(_parse_prop_buttons(soup))
        time.sleep(0.15)
    return props


def _scrape_pages() -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    from bs4 import BeautifulSoup

    rows: list[dict[str, Any]] = []
    props: list[dict[str, Any]] = []
    last_url = MYBOOKIE_UFC_URL
    main_soup = None

    for url in MYBOOKIE_URLS:
        try:
            resp = requests.get(url, headers=_request_headers(), timeout=config.REQUEST_TIMEOUT_SEC)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.debug("MyBookie fetch failed %s: %s", url, exc)
            continue

        soup = BeautifulSoup(resp.text, "lxml")
        parsed_rows = _parse_moneyline_rows(soup)
        parsed_props = _parse_totals_props(soup)
        parsed_props.extend(_parse_prop_buttons(soup))
        if parsed_rows or parsed_props:
            last_url = url
            rows = parsed_rows
            props = parsed_props
            main_soup = soup
            break

    if main_soup is not None:
        props.extend(_scrape_fight_prop_pages(main_soup))

    # Global props hub (often empty off-season)
    try:
        resp = requests.get(MYBOOKIE_PROPS_URL, headers=_request_headers(), timeout=config.REQUEST_TIMEOUT_SEC)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        props.extend(_parse_prop_buttons(soup))
    except requests.RequestException as exc:
        logger.debug("MyBookie props hub fetch failed: %s", exc)

    return rows, props, last_url


def _fetch_odds_api_fallback() -> pd.DataFrame:
    """Fallback to The Odds API mybookieag bookmaker when scrape fails."""
    import json

    from src.predictor import _names_match, _to_decimal_odds

    if not config.ODDS_API_KEY:
        raise OddsAPIError("Missing THE_ODDS_API_KEY for MyBookie fallback.")

    url = f"{config.ODDS_API_BASE_URL}/sports/{config.ODDS_API_SPORT}/odds"
    params = {
        "apiKey": config.ODDS_API_KEY,
        "regions": config.ODDS_API_REGIONS,
        "markets": config.ODDS_API_MARKETS,
        "oddsFormat": config.ODDS_API_ODDS_FORMAT,
        "bookmakers": _ODDS_API_BOOK_KEY,
    }
    try:
        response = requests.get(url, params=params, timeout=config.REQUEST_TIMEOUT_SEC)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise OddsAPIError(f"MyBookie Odds API fallback failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise OddsAPIError("MyBookie Odds API fallback returned invalid JSON") from exc

    if not isinstance(payload, list):
        raise OddsAPIError(f"Unexpected MyBookie API response: {type(payload)}")

    rows: list[dict[str, Any]] = []
    for event in payload:
        home = str(event.get("home_team", "")).strip()
        away = str(event.get("away_team", "")).strip()
        if not home or not away:
            continue
        f1_px: float | None = None
        f2_px: float | None = None
        for book in event.get("bookmakers", []):
            if str(book.get("key", "")).lower() != _ODDS_API_BOOK_KEY:
                continue
            for market in book.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                prices: dict[str, float] = {}
                for outcome in market.get("outcomes", []):
                    name = str(outcome.get("name", "")).strip()
                    price = outcome.get("price")
                    if name and price is not None:
                        prices[name] = _to_decimal_odds(float(price), config.ODDS_API_ODDS_FORMAT)
                f1_px = next((px for nm, px in prices.items() if _names_match(nm, home)), None)
                f2_px = next((px for nm, px in prices.items() if _names_match(nm, away)), None)
                break
        if not f1_px or not f2_px or f1_px <= 1 or f2_px <= 1:
            continue
        imp1, imp2 = _implied_probs(f1_px, f2_px)
        rows.append(
            {
                "fighter_1": home,
                "fighter_2": away,
                "f1_odds": round(f1_px, 3),
                "f2_odds": round(f2_px, 3),
                "implied_prob_f1": imp1,
                "implied_prob_f2": imp2,
                "bookmaker": "MyBookie",
                "bookmaker_count": 1,
                "source_url": "The Odds API (MyBookie)",
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        raise OddsAPIError("No MyBookie UFC h2h odds from Odds API fallback.")
    return df


def fetch_mybookie_odds(*, force_refresh: bool = False) -> pd.DataFrame:
    """Scrape MyBookie.ag UFC moneyline lines; cache to data/cache/mybookie_odds.csv."""
    if not config.MYBOOKIE_ENABLED:
        raise OddsAPIError("MyBookie is disabled (MYBOOKIE_ENABLED=false).")

    ensure_data_dirs()
    if not force_refresh and _cache_fresh(MYBOOKIE_CACHE_PATH):
        cached = pd.read_csv(MYBOOKIE_CACHE_PATH)
        if not cached.empty:
            logger.info("Using cached MyBookie odds (%s rows)", len(cached))
            return cached

    rows, _, source_url = _scrape_pages()
    if not rows:
        try:
            df = _fetch_odds_api_fallback()
            df.to_csv(MYBOOKIE_CACHE_PATH, index=False)
            logger.info("MyBookie Odds API fallback returned %s rows", len(df))
            return df
        except OddsAPIError:
            raise OddsAPIError(
                "Could not scrape MyBookie UFC odds and Odds API fallback unavailable."
            ) from None

    df = pd.DataFrame(rows).drop_duplicates(subset=["fighter_1", "fighter_2"], keep="first")
    if "source_url" not in df.columns:
        df["source_url"] = source_url
    df.to_csv(MYBOOKIE_CACHE_PATH, index=False)
    logger.info("Scraped %s MyBookie moneyline rows", len(df))
    return df


def fetch_mybookie_prop_odds(*, force_refresh: bool = False) -> pd.DataFrame:
    """Scrape MyBookie UFC prop lines (round totals + method props when listed)."""
    ensure_data_dirs()
    if not config.ENABLE_PROPS or not config.MYBOOKIE_ENABLED:
        return empty_prop_odds_df()

    if not force_refresh and _cache_fresh(MYBOOKIE_PROP_CACHE_PATH):
        cached = pd.read_csv(MYBOOKIE_PROP_CACHE_PATH)
        if not cached.empty:
            logger.info("Using cached MyBookie prop odds (%s rows)", len(cached))
            return cached

    _, props, _ = _scrape_pages()
    df = pd.DataFrame(props)
    if df.empty:
        logger.info("MyBookie prop scrape returned no live lines.")
        return empty_prop_odds_df()

    df = df.drop_duplicates(
        subset=["fighter_1", "fighter_2", "prop_key", "selection"],
        keep="first",
    )
    df.to_csv(MYBOOKIE_PROP_CACHE_PATH, index=False)
    logger.info("Scraped %s MyBookie prop lines", len(df))
    return df
