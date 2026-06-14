"""BetNow.eu UFC odds scraper (requests + BeautifulSoup, optional Selenium fallback)."""

from __future__ import annotations

import json
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
from src.predictor import OddsAPIError, _implied_probs, _names_match

logger = logging.getLogger(__name__)

BETNOW_CACHE_PATH = config.CACHE_DIR / "betnow_odds.csv"
BETNOW_PROP_CACHE_PATH = config.CACHE_DIR / "betnow_prop_odds.csv"
BETNOW_UFC_URL = config.BETNOW_PROPS_URL
BETNOW_URLS = [
    BETNOW_UFC_URL,
    "https://www.betnow.eu/sportsbook-info/fighting/professional-mma/",
]
_AMERICAN_RE = re.compile(r"(?<![0-9T])([+-]\d{2,4})(?![0-9])")
_ROTATION_RE = re.compile(r"^\d{5}$")

_PROP_LABEL_MAP: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"goes?\s+to\s+decision|fight\s+goes\s+to\s+decision", re.I), "goes_to_decision", "Yes"),
    (re.compile(r"inside\s+distance|does\s+not\s+go\s+to\s+decision|finish", re.I), "finish", "Yes"),
    (re.compile(r"\bko\b|ko/tko|wins?\s+by\s+ko", re.I), "ko_tko", "Yes"),
    (re.compile(r"submission|wins?\s+by\s+sub", re.I), "submission", "Yes"),
    (re.compile(r"round\s*1\s+finish|ends?\s+in\s+round\s*1", re.I), "round_1_finish", "Yes"),
    (re.compile(r"over\s*1\.?5|over\s*1\s*½", re.I), "over_1_5_rounds", "Over"),
    (re.compile(r"under\s*1\.?5|under\s*1\s*½", re.I), "round_1_finish", "Under 1.5"),
    (re.compile(r"wins?\s+by\s+ko.*", re.I), "fighter_ko", "Yes"),
    (re.compile(r"wins?\s+by\s+sub.*", re.I), "fighter_sub", "Yes"),
]


def _cache_fresh(path: Any = None) -> bool:
    from pathlib import Path

    p = Path(path or BETNOW_CACHE_PATH)
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
    if config.BETNOW_COOKIE:
        headers["Cookie"] = config.BETNOW_COOKIE.strip()
    return headers


def _cell_odds_text(cell) -> str:
    """Extract visible odds text from a table cell, skipping login placeholders."""
    if cell is None:
        return ""
    classes = " ".join(cell.get("class", []))
    if "loginurl" in classes:
        return ""
    text = cell.get_text(" ", strip=True)
    if text in {"-", "—", ""}:
        return ""
    return text


def _decimal_from_cell_text(text: str) -> float | None:
    text = text.strip()
    if not text or text in {"-", "—"}:
        return None
    american = parse_american_odds(text)
    if american is not None:
        dec = american_to_decimal(american)
        return dec if dec > 1 else None
    try:
        val = float(text.replace(",", "."))
        return val if val > 1 else None
    except ValueError:
        return None


def _fighter_from_team_span(span_text: str) -> tuple[str, str]:
    """Return (rotation, fighter_name) from '24013 Michael Chandler'."""
    text = " ".join(span_text.split())
    parts = text.split()
    if parts and _ROTATION_RE.match(parts[0]):
        return parts[0], " ".join(parts[1:]).strip()
    return "", text.strip()


def _parse_prop_label(label: str, fighter_name: str) -> tuple[str, str] | None:
    for pattern, prop_key, selection in _PROP_LABEL_MAP:
        if pattern.search(label):
            if prop_key in ("fighter_ko", "fighter_sub") and fighter_name:
                return prop_key, f"{fighter_name} {selection}"
            return prop_key, selection
    return None


def _iter_fight_blocks(odds_root) -> list[dict[str, Any]]:
    """Parse #odds UFC fight blocks from sportsbook-info HTML."""
    fights: list[dict[str, Any]] = []
    if odds_root is None:
        return fights

    current: dict[str, Any] | None = None
    for child in odds_root.children:
        if getattr(child, "name", None) != "div":
            continue
        div_id = child.get("id") or ""
        classes = child.get("class") or []

        if div_id.startswith("game"):
            if current and current.get("fighters"):
                fights.append(current)
            current = {
                "event_title": child.get_text(" ", strip=True),
                "fighters": [],
                "props": [],
            }
            continue

        if "odd-info-teams" not in classes or current is None:
            continue

        cols = child.find_all("div", recursive=False)
        if len(cols) < 2:
            continue
        team_span = cols[0].find("span", class_="team-name")
        if team_span is None:
            continue
        rotation, fighter = _fighter_from_team_span(team_span.get_text(" ", strip=True))
        if not fighter:
            continue

        spread_txt = _cell_odds_text(cols[1]) if len(cols) > 1 else ""
        total_txt = _cell_odds_text(cols[2]) if len(cols) > 2 else ""
        ml_txt = _cell_odds_text(cols[3]) if len(cols) > 3 else ""

        current["fighters"].append(
            {
                "rotation": rotation,
                "name": fighter,
                "spread": spread_txt,
                "total": total_txt,
                "moneyline": ml_txt,
            }
        )

    if current and current.get("fighters"):
        fights.append(current)

    return fights


def _fight_pair(fight: dict[str, Any]) -> tuple[str, str, str, str]:
    fighters = fight.get("fighters") or []
    if len(fighters) < 2:
        return "", "", "", ""
    f1 = fighters[0]
    f2 = fighters[1]
    return (
        str(f1.get("name", "")).strip(),
        str(f2.get("name", "")).strip(),
        str(f1.get("rotation", "")).strip(),
        str(f2.get("rotation", "")).strip(),
    )


def _parse_moneyline_rows(fights: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fight in fights:
        f1_name, f2_name, _, _ = _fight_pair(fight)
        if not f1_name or not f2_name:
            continue
        fighters = fight["fighters"]
        o1 = _decimal_from_cell_text(fighters[0].get("moneyline", ""))
        o2 = _decimal_from_cell_text(fighters[1].get("moneyline", ""))
        if not o1 or not o2:
            continue
        imp1, imp2 = _implied_probs(o1, o2)
        rows.append(
            {
                "fighter_1": f1_name,
                "fighter_2": f2_name,
                "f1_odds": round(o1, 3),
                "f2_odds": round(o2, 3),
                "implied_prob_f1": imp1,
                "implied_prob_f2": imp2,
                "bookmaker": "BetNow.eu",
                "bookmaker_count": 1,
                "source_url": BETNOW_UFC_URL,
            }
        )
    return rows


def _parse_totals_props(fights: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse fight totals column (e.g. O1.5 / U1.5) when BetNow exposes text odds."""
    props: list[dict[str, Any]] = []
    over_re = re.compile(r"o(?:ver)?\s*1\.?5", re.I)
    under_re = re.compile(r"u(?:nder)?\s*1\.?5", re.I)

    for fight in fights:
        f1_name, f2_name, rot1, rot2 = _fight_pair(fight)
        if not f1_name or not f2_name:
            continue
        rotation = rot1 or rot2
        for fighter in fight.get("fighters", []):
            total_txt = str(fighter.get("total", "")).strip()
            if not total_txt:
                continue
            # Combined cell like "O1.5 -110" or separate over/under tokens
            american = parse_american_odds(total_txt)
            if american is None:
                continue
            decimal = american_to_decimal(american)
            if decimal <= 1:
                continue
            if over_re.search(total_txt):
                props.append(
                    prop_row(
                        fighter_1=f1_name,
                        fighter_2=f2_name,
                        prop_key="over_1_5_rounds",
                        selection="Over 1.5",
                        decimal_odds=decimal,
                        bookmaker="BetNow.eu",
                        odds_source="live",
                        market_key="totals",
                        point=1.5,
                        rotation=rotation,
                        american_odds=american,
                    )
                )
            elif under_re.search(total_txt):
                props.append(
                    prop_row(
                        fighter_1=f1_name,
                        fighter_2=f2_name,
                        prop_key="round_1_finish",
                        selection="Under 1.5",
                        decimal_odds=decimal,
                        bookmaker="BetNow.eu",
                        odds_source="live",
                        market_key="totals",
                        point=1.5,
                        rotation=rotation,
                        american_odds=american,
                    )
                )
    return props


def _parse_prop_sections(html_text: str, fights: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Parse BetNow prop rows when present in HTML (authenticated sessions).

    Looks for label + american odds patterns near known fight names.
    """
    props: list[dict[str, Any]] = []
    for fight in fights:
        f1_name, f2_name, rot1, _ = _fight_pair(fight)
        if not f1_name or not f2_name:
            continue
        rotation = rot1
        for fighter in fight.get("fighters", []):
            name = str(fighter.get("name", ""))
            for field in ("spread", "total", "moneyline"):
                txt = str(fighter.get(field, ""))
                for label, prop_key, selection in _PROP_LABEL_MAP:
                    if label.search(txt):
                        american = parse_american_odds(txt)
                        if american is None:
                            continue
                        decimal = american_to_decimal(american)
                        if decimal <= 1:
                            continue
                        props.append(
                            prop_row(
                                fighter_1=f1_name,
                                fighter_2=f2_name,
                                prop_key=prop_key,
                                selection=selection,
                                decimal_odds=decimal,
                                bookmaker="BetNow.eu",
                                odds_source="live",
                                market_key="prop",
                                rotation=rotation,
                                american_odds=american,
                            )
                        )

        # Global search for fighter-specific method props in page text chunks
        chunk_pat = re.compile(
            rf"{re.escape(f1_name)}.+?({_AMERICAN_RE.pattern})|"
            rf"{re.escape(f2_name)}.+?({_AMERICAN_RE.pattern})",
            re.I | re.S,
        )
        for m in chunk_pat.finditer(html_text):
            snippet = m.group(0)[:240]
            american = parse_american_odds(snippet)
            if american is None:
                continue
            mapped = None
            for label, prop_key, selection in _PROP_LABEL_MAP:
                if label.search(snippet):
                    mapped = (prop_key, selection)
                    break
            if not mapped:
                continue
            prop_key, selection = mapped
            decimal = american_to_decimal(american)
            if decimal <= 1:
                continue
            props.append(
                prop_row(
                    fighter_1=f1_name,
                    fighter_2=f2_name,
                    prop_key=prop_key,
                    selection=selection,
                    decimal_odds=decimal,
                    bookmaker="BetNow.eu",
                    odds_source="live",
                    market_key="prop",
                    rotation=rotation,
                    american_odds=american,
                )
            )
    return props


def _scrape_sportsbook_info() -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    from bs4 import BeautifulSoup

    rows: list[dict[str, Any]] = []
    props: list[dict[str, Any]] = []
    last_url = BETNOW_UFC_URL

    for url in BETNOW_URLS:
        try:
            resp = requests.get(url, headers=_request_headers(), timeout=config.REQUEST_TIMEOUT_SEC)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.debug("BetNow fetch failed %s: %s", url, exc)
            continue

        soup = BeautifulSoup(resp.text, "lxml")
        odds_root = soup.select_one("#odds")
        fights = _iter_fight_blocks(odds_root)
        if not fights:
            continue

        last_url = url
        rows = _parse_moneyline_rows(fights)
        props = _parse_totals_props(fights)
        props.extend(_parse_prop_sections(resp.text, fights))
        if rows or props:
            break

    return rows, props, last_url


def _scrape_with_selenium() -> list[dict[str, Any]]:
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError:
        logger.debug("Selenium not installed — skipping BetNow browser fallback.")
        return []

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    rows: list[dict[str, Any]] = []
    driver = None
    try:
        driver = webdriver.Chrome(options=options)
        for url in BETNOW_URLS:
            driver.get(url)
            time.sleep(4)
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(driver.page_source, "lxml")
            fights = _iter_fight_blocks(soup.select_one("#odds"))
            parsed = _parse_moneyline_rows(fights)
            if parsed:
                for item in parsed:
                    item["source_url"] = url
                rows.extend(parsed)
                break
    except Exception as exc:
        logger.warning("BetNow Selenium fallback failed: %s", exc)
    finally:
        if driver is not None:
            driver.quit()
    return rows


def fetch_betnow_odds(*, force_refresh: bool = False) -> pd.DataFrame:
    """Scrape BetNow.eu UFC moneyline lines; cache to data/cache/betnow_odds.csv."""
    ensure_data_dirs()
    if not force_refresh and _cache_fresh(BETNOW_CACHE_PATH):
        cached = pd.read_csv(BETNOW_CACHE_PATH)
        if not cached.empty:
            logger.info("Using cached BetNow odds (%s rows)", len(cached))
            return cached

    rows, _, source_url = _scrape_sportsbook_info()
    if not rows:
        rows = _scrape_with_selenium()

    if not rows:
        raise OddsAPIError(
            "Could not scrape BetNow.eu UFC odds. Lines may require login — set BETNOW_COOKIE in .env "
            "or use DraftKings props tab for live round totals."
        )

    df = pd.DataFrame(rows).drop_duplicates(subset=["fighter_1", "fighter_2"], keep="first")
    if "source_url" not in df.columns:
        df["source_url"] = source_url
    df.to_csv(BETNOW_CACHE_PATH, index=False)
    logger.info("Scraped %s BetNow moneyline rows", len(df))
    return df


def fetch_betnow_prop_odds(*, force_refresh: bool = False) -> pd.DataFrame:
    """Scrape BetNow.eu UFC prop lines (method, totals, decision) when exposed in HTML."""
    ensure_data_dirs()
    if not config.ENABLE_PROPS:
        return empty_prop_odds_df()

    if not force_refresh and _cache_fresh(BETNOW_PROP_CACHE_PATH):
        cached = pd.read_csv(BETNOW_PROP_CACHE_PATH)
        if not cached.empty:
            logger.info("Using cached BetNow prop odds (%s rows)", len(cached))
            return cached

    _, props, _ = _scrape_sportsbook_info()
    df = pd.DataFrame(props)
    if df.empty:
        logger.info(
            "BetNow prop scrape returned no live lines (guest pages often hide odds behind login)."
        )
        return empty_prop_odds_df()

    df = df.drop_duplicates(
        subset=["fighter_1", "fighter_2", "prop_key", "selection"],
        keep="first",
    )
    df.to_csv(BETNOW_PROP_CACHE_PATH, index=False)
    logger.info("Scraped %s BetNow prop lines", len(df))
    return df


def match_betnow_row(fighter_1: str, fighter_2: str, odds: pd.DataFrame) -> pd.Series | None:
    """Lookup helper for tests."""
    for _, row in odds.iterrows():
        if _names_match(fighter_1, row["fighter_1"]) and _names_match(fighter_2, row["fighter_2"]):
            return row
        if _names_match(fighter_1, row["fighter_2"]) and _names_match(fighter_2, row["fighter_1"]):
            swapped = row.copy()
            swapped["fighter_1"], swapped["fighter_2"] = row["fighter_2"], row["fighter_1"]
            swapped["f1_odds"], swapped["f2_odds"] = row["f2_odds"], row["f1_odds"]
            swapped["implied_prob_f1"], swapped["implied_prob_f2"] = (
                row["implied_prob_f2"],
                row["implied_prob_f1"],
            )
            return swapped
    return None
