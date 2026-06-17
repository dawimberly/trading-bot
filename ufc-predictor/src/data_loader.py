"""Load, download, scrape, and clean UFC fight data."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

import config

logger = logging.getLogger(__name__)

# Pipeline aliases used by feature_engineering / model code
_PIPELINE_ALIASES = {
    "event_date": "date",
    "event_name": "event",
    "fighter_1": "fighter1",
    "fighter_2": "fighter2",
}

_CANONICAL_COLUMNS = set(config.FIGHTS_COLUMNS)

_COLUMN_ALIASES: dict[str, list[str]] = {
    "fight_id": ["fight_id", "fightid", "id", "FightId", "fight_url_id"],
    "event": ["event", "event_name", "title", "Title", "event_title", "card"],
    "date": ["date", "event_date", "Date", "fight_date"],
    "location": ["location", "Location", "event_location", "venue"],
    "fighter1": [
        "fighter1",
        "fighter_1",
        "f1",
        "red_fighter",
        "redfighter",
        "R_fighter",
        "Fighter 0",
        "fighter1_name",
    ],
    "fighter2": [
        "fighter2",
        "fighter_2",
        "f2",
        "blue_fighter",
        "bluefighter",
        "B_fighter",
        "Fighter 1",
        "fighter2_name",
    ],
    "winner": ["winner", "Winner", "event_winner", "winning_fighter"],
    "weight_class": [
        "weight_class",
        "weightclass",
        "Weight class",
        "WeightClass",
        "division",
        "Fight_type",
    ],
    "method": ["method", "Method", "win_method", "finish_method"],
    "round": ["round", "Round", "finish_round", "last_round"],
    "time": ["time", "Time", "finish_time"],
    "f1_odds": ["f1_odds", "fighter1_odds", "red_odds", "R_odds", "favourite_odds"],
    "f2_odds": ["f2_odds", "fighter2_odds", "blue_odds", "B_odds", "underdog_odds"],
}

_LOCAL_PRIORITY = tuple(config.LOCAL_KAGGLE_CANDIDATES)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class DataLoaderError(Exception):
    """Base error for data loading failures."""


class ScrapeBlockedError(DataLoaderError):
    """Remote site returned a bot challenge or unusable HTML."""


def ensure_data_dirs() -> None:
    """Create data subdirectories if missing."""
    for path in (config.RAW_DIR, config.PROCESSED_DIR, config.CACHE_DIR, config.MODELS_DIR):
        path.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------


def clean_fighter_name(name: Any) -> str:
    """Normalize fighter display names."""
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return ""
    text = str(name).strip()
    text = re.sub(r"\s+", " ", text)
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = re.sub(r"\s*\(.*?\)\s*", " ", text).strip()  # drop nicknames in parens
    return re.sub(r"\s+", " ", text)


def clean_date(value: Any) -> pd.Timestamp | pd.NaT:
    """Parse heterogeneous date strings into UTC-normalized timestamps."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return pd.NaT
    parsed = pd.to_datetime(value, errors="coerce", utc=False)
    if pd.isna(parsed):
        return pd.NaT
    if getattr(parsed, "tzinfo", None) is not None:
        parsed = parsed.tz_convert(None)
    return pd.Timestamp(parsed).normalize()


def clean_weight_class(value: Any) -> str:
    """Standardize weight-class labels."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "Unknown"
    text = str(value).strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\bBout\b", "", text, flags=re.IGNORECASE).strip(" -")
    text = re.sub(r"\bInterim\b", "Interim", text, flags=re.IGNORECASE)
    text = re.sub(r"\bTitle\b", "Title", text, flags=re.IGNORECASE)
    if re.search(r"title", text, re.IGNORECASE):
        base = re.sub(r"(?i)\s*title\s*", " ", text)
        base = re.sub(r"\s+", " ", base).strip()
        if base and "title" not in base.lower():
            return f"{base} Title"
        return text
    return text or "Unknown"


def _is_title_bout(weight_class: str) -> int:
    return int(bool(re.search(r"title", weight_class, re.IGNORECASE)))


def _parse_sig_strikes(value: Any) -> tuple[float | None, float | None]:
    """Parse '32 of 89' style strike strings."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None, None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value), None
    text = str(value).strip()
    match = re.match(r"(\d+)\s*(?:of\s*)?(\d+)?", text)
    if not match:
        return None, None
    landed = float(match.group(1))
    attempted = float(match.group(2)) if match.group(2) else None
    return landed, attempted


def _fighters_same_person(a: str, b: str) -> bool:
    """Token-based fighter name match (safer than bare substring)."""
    ka, kb = clean_fighter_name(a).lower(), clean_fighter_name(b).lower()
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    a_parts = set(ka.split())
    b_parts = set(kb.split())
    if len(a_parts.intersection(b_parts)) >= 2:
        return True
    a_last, b_last = ka.split()[-1], kb.split()[-1]
    return len(a_last) > 3 and a_last == b_last and (
        a_parts.intersection(b_parts) or ka.split()[0][0] == kb.split()[0][0]
    )


def _resolve_winner(
    row: pd.Series,
    *,
    outcome_col: str | None = None,
) -> str:
    """Map winner/outcome fields to a fighter name."""
    winner = clean_fighter_name(row.get("winner", ""))
    f1 = clean_fighter_name(row.get("fighter1", ""))
    f2 = clean_fighter_name(row.get("fighter2", ""))

    if winner:
        if winner.lower() in {"draw", "no contest", "nc", "draw/no contest"}:
            return ""
        if winner in {f1, f2}:
            return winner
        if winner.lower() in {"fighter1", "f1", "red", "r"}:
            return f1
        if winner.lower() in {"fighter2", "f2", "blue", "b"}:
            return f2

    outcome = str(row.get(outcome_col or "outcome", "")).strip().lower()
    if outcome in {"fighter1", "f1", "red"}:
        return f1
    if outcome in {"fighter2", "f2", "blue"}:
        return f2
    if outcome in {"draw", "no contest", "nc", "d", "dq"}:
        return ""
    return winner


def _make_fight_id(
    event: str,
    date: pd.Timestamp,
    fighter1: str,
    fighter2: str,
    existing: str | None = None,
) -> str:
    if existing and str(existing).strip():
        return str(existing).strip()
    key = f"{event}|{date}|{fighter1}|{fighter2}".lower()
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# HTTP / cache helpers
# ---------------------------------------------------------------------------


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": _USER_AGENT})
    return session


def _is_bot_challenge(html: str) -> bool:
    lowered = html.lower()
    return (
        "checking your browser" in lowered
        or "just a moment" in lowered
        or len(html) < 8_000 and "<table" not in lowered and "event-details" not in lowered
    )


def _request_text(url: str, *, session: requests.Session | None = None) -> str:
    sess = session or _session()
    try:
        response = sess.get(url, timeout=config.REQUEST_TIMEOUT_SEC)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise DataLoaderError(f"Request failed for {url}: {exc}") from exc
    if _is_bot_challenge(response.text):
        raise ScrapeBlockedError(f"Bot challenge detected at {url}")
    return response.text


def _cache_is_fresh(path: Path, ttl_hours: int | None = None) -> bool:
    if not path.is_file():
        return False
    ttl = ttl_hours if ttl_hours is not None else config.CACHE_TTL_HOURS
    if ttl <= 0:
        return False
    age_hours = (time.time() - path.stat().st_mtime) / 3600
    return age_hours < ttl


def _write_meta(meta_path: Path, payload: dict[str, Any]) -> None:
    ensure_data_dirs()
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**payload, "updated_at": datetime.now(timezone.utc).isoformat()}
    meta_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _read_meta(meta_path: Path) -> dict[str, Any]:
    if not meta_path.is_file():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# Column mapping / canonical fights frame
# ---------------------------------------------------------------------------


def _rename_to_canonical(df: pd.DataFrame) -> pd.DataFrame:
    """Map heterogeneous source columns onto canonical fights.csv schema."""
    rename_map: dict[str, str] = {}
    lower_lookup = {str(c).lower(): c for c in df.columns}
    for canonical, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            key = alias.lower()
            if key in lower_lookup:
                rename_map[lower_lookup[key]] = canonical
                break
    out = df.rename(columns=rename_map).copy()
    return out


def _clean_fights_frame(df: pd.DataFrame, *, source: str) -> pd.DataFrame:
    """Normalize and validate a fights dataframe."""
    if df.empty:
        raise DataLoaderError("No fight rows to clean.")

    work = _rename_to_canonical(df)
    rows: list[dict[str, Any]] = []

    for raw in work.to_dict(orient="records"):
        event = str(raw.get("event", "") or raw.get("event_name", "") or "").strip()
        date = clean_date(raw.get("date"))
        fighter1 = clean_fighter_name(raw.get("fighter1"))
        fighter2 = clean_fighter_name(raw.get("fighter2"))
        if not fighter1 or not fighter2 or pd.isna(date):
            continue

        weight_class = clean_weight_class(raw.get("weight_class"))
        winner = _resolve_winner(pd.Series(raw))
        if winner and winner not in {fighter1, fighter2}:
            if _fighters_same_person(winner, fighter1):
                winner = fighter1
            elif _fighters_same_person(winner, fighter2):
                winner = fighter2
            else:
                logger.debug(
                    "Winner %r did not match %r or %r; clearing",
                    winner,
                    fighter1,
                    fighter2,
                )
                winner = ""

        sig_l1, sig_a1 = _parse_sig_strikes(raw.get("sig_strikes_landed_f1", raw.get("Fighter 0 Str")))
        sig_l2, sig_a2 = _parse_sig_strikes(raw.get("sig_strikes_landed_f2", raw.get("Fighter 1 Str")))
        td_l1 = raw.get("takedowns_landed_f1", raw.get("Fighter 0 Td"))
        td_l2 = raw.get("takedowns_landed_f2", raw.get("Fighter 1 Td"))

        method = str(raw.get("method", "") or "").strip()
        finish = int(
            bool(
                winner
                and method
                and not method.upper().startswith(("DEC", "S-DEC", "U-DEC", "M-DEC"))
            )
        )

        fight_id_seed = raw.get("fight_id") or raw.get("FightId")
        if not fight_id_seed and raw.get("Url"):
            fight_id_seed = str(raw["Url"]).rsplit("/", 1)[-1]

        rows.append(
            {
                "fight_id": _make_fight_id(event, date, fighter1, fighter2, fight_id_seed),
                "event": event or "Unknown Event",
                "date": date,
                "location": str(raw.get("location", "") or "").strip(),
                "fighter1": fighter1,
                "fighter2": fighter2,
                "winner": winner,
                "weight_class": weight_class,
                "method": method,
                "round": pd.to_numeric(raw.get("round"), errors="coerce"),
                "time": str(raw.get("time", "") or "").strip(),
                "is_title_fight": _is_title_bout(weight_class),
                "is_main_event": int(bool(raw.get("is_main_event", 0))),
                "sig_strikes_landed_f1": sig_l1,
                "sig_strikes_attempted_f1": sig_a1,
                "sig_strikes_landed_f2": sig_l2,
                "sig_strikes_attempted_f2": sig_a2,
                "takedowns_landed_f1": pd.to_numeric(td_l1, errors="coerce"),
                "takedowns_attempted_f1": pd.NA,
                "takedowns_landed_f2": pd.to_numeric(td_l2, errors="coerce"),
                "takedowns_attempted_f2": pd.NA,
                "finish": finish,
                "f1_odds": pd.to_numeric(raw.get("f1_odds"), errors="coerce"),
                "f2_odds": pd.to_numeric(raw.get("f2_odds"), errors="coerce"),
                "source": source,
            }
        )

    if not rows:
        raise DataLoaderError("All rows were dropped during cleaning.")

    cleaned = pd.DataFrame(rows)
    cleaned = cleaned.drop_duplicates(subset=["fight_id"], keep="last")
    cleaned = cleaned.sort_values("date").reset_index(drop=True)
    return cleaned


def _is_cleaned_fights_df(df: pd.DataFrame) -> bool:
    """True when dataframe already matches saved fights.csv schema."""
    return {"fight_id", "fighter1", "fighter2", "date"}.issubset(df.columns)


def _read_fights_csv(path: Path) -> pd.DataFrame:
    """Load fights.csv without redundant re-cleaning."""
    df = pd.read_csv(path, parse_dates=["date"], low_memory=False)
    if _is_cleaned_fights_df(df):
        logger.debug("Loaded pre-cleaned fights (%s rows)", len(df))
        return _add_pipeline_aliases(df)
    logger.debug("Re-cleaning fights from raw schema")
    return _add_pipeline_aliases(_clean_fights_frame(df, source="file:fights.csv"))


def _add_pipeline_aliases(df: pd.DataFrame) -> pd.DataFrame:
    """Attach legacy column names used by downstream modules."""
    out = df.copy()
    for legacy, canonical in _PIPELINE_ALIASES.items():
        if canonical in out.columns:
            out[legacy] = out[canonical]
    if "sig_strikes_landed_f1" in out.columns:
        out["sig_strikes_landed"] = out["sig_strikes_landed_f1"]
        out["sig_strikes_attempted"] = out["sig_strikes_attempted_f1"]
        out["takedowns_landed"] = out["takedowns_landed_f1"]
        out["takedowns_attempted"] = out["takedowns_attempted_f1"]
    return out


# ---------------------------------------------------------------------------
# Historical sources
# ---------------------------------------------------------------------------


def _load_local_candidate(path: Path) -> pd.DataFrame | None:
    if not path.is_file():
        return None
    try:
        df = pd.read_csv(path)
        logger.info("Loaded local dataset: %s (%s rows)", path.name, len(df))
        return _clean_fights_frame(df, source=f"local:{path.name}")
    except Exception as exc:
        logger.warning("Failed to load %s: %s", path, exc)
        return None


def _download_csv_url(url: str) -> pd.DataFrame:
    session = _session()
    try:
        response = session.get(url, timeout=config.REQUEST_TIMEOUT_SEC)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise DataLoaderError(f"CSV download failed: {url}") from exc

    cache_path = config.CACHE_DIR / f"download_{hashlib.md5(url.encode()).hexdigest()[:10]}.csv"
    cache_path.write_bytes(response.content)
    df = pd.read_csv(cache_path)
    return _clean_fights_frame(df, source=f"url:{url}")


def _fetch_huggingface_page(offset: int, length: int, *, retries: int = 4) -> dict[str, Any]:
    url = (
        "https://datasets-server.huggingface.co/rows"
        f"?dataset={config.HF_UFC_DATASET}"
        f"&config=default&split={config.HF_UFC_SPLIT}"
        f"&offset={offset}&length={length}"
    )
    session = _session()
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            response = session.get(url, timeout=config.REQUEST_TIMEOUT_SEC)
            if response.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))
    raise DataLoaderError(f"HuggingFace fetch failed at offset {offset}") from last_exc


def _download_huggingface_historical(*, since: pd.Timestamp | None = None) -> pd.DataFrame:
    """Download ufcstats-format history via HuggingFace datasets server."""
    ensure_data_dirs()
    cache_path = config.CACHE_DIR / "hf_ufc_fights.csv"

    first = _fetch_huggingface_page(0, 1)
    total = int(first.get("num_rows_total", 0))
    if total <= 0:
        raise DataLoaderError("HuggingFace dataset returned zero rows.")

    if cache_path.is_file() and since is None and _cache_is_fresh(cache_path):
        logger.info("Using cached HuggingFace export: %s", cache_path)
        return _clean_fights_frame(pd.read_csv(cache_path), source="huggingface:cache")

    rows: list[dict[str, Any]] = []
    page_size = max(1, min(config.HF_UFC_PAGE_SIZE, 100))
    offsets = range(0, total, page_size)

    for offset in tqdm(offsets, desc="Downloading HuggingFace UFC data"):
        try:
            payload = _fetch_huggingface_page(offset, min(page_size, total - offset))
        except DataLoaderError:
            if rows:
                logger.warning("HuggingFace rate-limited; using %s partial rows", len(rows))
                break
            if cache_path.is_file():
                logger.warning("HuggingFace unavailable; falling back to cache")
                return _clean_fights_frame(pd.read_csv(cache_path), source="huggingface:cache")
            raise

        for item in payload.get("rows", []):
            row = item.get("row", {})
            if not row:
                continue
            row_date = clean_date(row.get("Date"))
            if since is not None and not pd.isna(row_date) and row_date < since:
                continue
            rows.append(
                {
                    "fight_id": row.get("FightId"),
                    "event": row.get("Title"),
                    "date": row.get("Date"),
                    "location": row.get("Location"),
                    "fighter1": row.get("Fighter 0"),
                    "fighter2": row.get("Fighter 1"),
                    "winner": row.get("Winner"),
                    "weight_class": row.get("Weight class"),
                    "method": row.get("Method"),
                    "round": row.get("Round"),
                    "time": row.get("Time"),
                    "sig_strikes_landed_f1": row.get("Fighter 0 Str"),
                    "sig_strikes_landed_f2": row.get("Fighter 1 Str"),
                    "takedowns_landed_f1": row.get("Fighter 0 Td"),
                    "takedowns_landed_f2": row.get("Fighter 1 Td"),
                    "outcome": row.get("Outcome"),
                }
            )
        time.sleep(0.35)

    if not rows:
        if cache_path.is_file():
            return _clean_fights_frame(pd.read_csv(cache_path), source="huggingface:cache")
        if since is not None:
            return pd.DataFrame(columns=config.FIGHTS_COLUMNS)
        raise DataLoaderError("HuggingFace download produced no rows.")

    raw = pd.DataFrame(rows)
    raw.to_csv(cache_path, index=False)
    return _clean_fights_frame(raw, source="huggingface:JesterLabs/UFC_FIGHT_DATA")


def _scrape_ufcstats_event_fights(event_url: str, event_name: str, event_date: str) -> list[dict]:
    """Parse one ufcstats event-details page into fight rows."""
    html = _request_text(event_url)
    soup = BeautifulSoup(html, "lxml")
    rows: list[dict[str, Any]] = []

    for row in soup.select("tr.b-fight-details__table-row"):
        fighters = row.select("a.b-link.b-link_style_black")
        if len(fighters) < 2:
            continue
        f1 = clean_fighter_name(fighters[0].get_text(strip=True))
        f2 = clean_fighter_name(fighters[1].get_text(strip=True))
        status = row.select_one("p.b-fight-details__person-status")
        winner = ""
        if status:
            label = status.get_text(strip=True).upper()
            if label == "W":
                parent = status.find_parent("div", class_="b-fight-details__person")
                if parent:
                    link = parent.select_one("a.b-link")
                    if link:
                        winner = clean_fighter_name(link.get_text(strip=True))
            elif label == "D":
                winner = ""
            elif label == "NC":
                winner = ""

        weight_class = ""
        wc_el = row.select_one("i.b-fight-details__fight-title")
        if wc_el:
            weight_class = clean_weight_class(wc_el.get_text(strip=True))

        method = ""
        method_el = row.select_one("p.b-fight-details__text")
        if method_el:
            method = method_el.get_text(" ", strip=True)

        rows.append(
            {
                "fight_id": row.get("data-link", "").rsplit("/", 1)[-1],
                "event": event_name,
                "date": event_date,
                "fighter1": f1,
                "fighter2": f2,
                "winner": winner,
                "weight_class": weight_class,
                "method": method,
                "source": "ufcstats:event",
            }
        )
    return rows


def _scrape_ufcstats_completed(*, max_events: int = 25) -> pd.DataFrame:
    """Scrape recent completed events from ufcstats.com (may be blocked)."""
    html = _request_text(config.UFC_STATS_BASE_URL)
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", class_="b-statistics__table-events")
    if table is None:
        raise ScrapeBlockedError("ufcstats events table not found.")

    event_rows: list[dict[str, str]] = []
    for tr in table.find_all("tr", class_="b-statistics__table-row"):
        link = tr.find("a")
        if link is None or not link.get("href"):
            continue
        date_el = tr.find("span", class_="b-statistics__date")
        event_rows.append(
            {
                "event_url": urljoin(config.UFC_STATS_BASE_URL, link["href"]),
                "event_name": link.get_text(strip=True),
                "event_date": date_el.get_text(strip=True) if date_el else "",
            }
        )
        if len(event_rows) >= max_events:
            break

    fights: list[dict[str, Any]] = []
    for event in tqdm(event_rows, desc="Scraping ufcstats events"):
        fights.extend(
            _scrape_ufcstats_event_fights(
                event["event_url"], event["event_name"], event["event_date"]
            )
        )
        time.sleep(config.REQUEST_DELAY_SEC)

    if not fights:
        raise DataLoaderError("ufcstats scrape returned no fights.")
    return _clean_fights_frame(pd.DataFrame(fights), source="ufcstats:completed")


def _merge_fight_frames(frames: Iterable[pd.DataFrame]) -> pd.DataFrame:
    valid = [f for f in frames if f is not None and not f.empty]
    if not valid:
        raise DataLoaderError("No datasets available to merge.")
    merged = pd.concat(valid, ignore_index=True)
    merged = merged.drop_duplicates(subset=["fight_id"], keep="last")
    merged = merged.sort_values("date").reset_index(drop=True)
    return merged


# ---------------------------------------------------------------------------
# Historical betting odds (Kaggle jerzyszocik + public mirrors)
# ---------------------------------------------------------------------------

_ODDS_UNIFIED_COLUMNS = [
    "event",
    "date",
    "fighter1",
    "fighter2",
    "f1_odds",
    "f2_odds",
    "source",
]

_ODDS_COLUMN_ALIASES: dict[str, list[str]] = {
    "event": ["event", "event_name", "title", "card"],
    "date": ["date", "event_date", "fight_date"],
    "fighter1": ["fighter1", "fighter_1", "R_fighter", "red_fighter"],
    "fighter2": ["fighter2", "fighter_2", "B_fighter", "blue_fighter"],
    "f1_odds": ["f1_odds", "R_odds", "red_odds", "favourite_odds"],
    "f2_odds": ["f2_odds", "B_odds", "blue_odds", "underdog_odds"],
}


class OddsLoadError(DataLoaderError):
    """Historical odds CSV could not be loaded."""


def _normalize_event_name(name: Any) -> str:
    text = str(name or "").strip().lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _rename_odds_to_canonical(df: pd.DataFrame) -> pd.DataFrame:
    rename_map: dict[str, str] = {}
    lower_lookup = {str(c).lower(): c for c in df.columns}
    for canonical, aliases in _ODDS_COLUMN_ALIASES.items():
        for alias in aliases:
            key = alias.lower()
            if key in lower_lookup:
                rename_map[lower_lookup[key]] = canonical
                break
    return df.rename(columns=rename_map).copy()


def _odds_pair_valid(o1: Any, o2: Any) -> bool:
    try:
        a, b = float(o1), float(o2)
    except (TypeError, ValueError):
        return False
    if not np.isfinite(a) or not np.isfinite(b):
        return False
    if abs(a) > 100 or abs(b) > 100:
        return abs(a) >= 100 and abs(b) >= 100
    return a > 1 and b > 1


def _map_fav_dog_odds(
    f1: str,
    f2: str,
    fav: str,
    dog: str,
    fav_odds: Any,
    dog_odds: Any,
) -> tuple[float, float] | None:
    if not _odds_pair_valid(fav_odds, dog_odds):
        return None
    fav_f, dog_f = float(fav_odds), float(dog_odds)
    if _fighters_same_person(f1, fav) and _fighters_same_person(f2, dog):
        return fav_f, dog_f
    if _fighters_same_person(f1, dog) and _fighters_same_person(f2, fav):
        return dog_f, fav_f
    if _fighters_same_person(f1, fav):
        return fav_f, dog_f
    if _fighters_same_person(f1, dog):
        return dog_f, fav_f
    if _fighters_same_person(f2, fav):
        return dog_f, fav_f
    if _fighters_same_person(f2, dog):
        return fav_f, dog_f
    return None


def _normalize_odds_frame(df: pd.DataFrame, *, source: str) -> pd.DataFrame:
    """Map heterogeneous odds CSVs (Kaggle R_/B_ schema, fav/dog) to unified rows."""
    if df.empty:
        return pd.DataFrame(columns=_ODDS_UNIFIED_COLUMNS)

    work = _rename_odds_to_canonical(df)
    rows: list[dict[str, Any]] = []

    for raw in work.to_dict(orient="records"):
        event = str(raw.get("event", "") or raw.get("event_name", "") or "").strip()
        dt = clean_date(raw.get("date") or raw.get("event_date"))
        if pd.isna(dt):
            continue

        f1 = clean_fighter_name(raw.get("fighter1") or raw.get("R_fighter") or "")
        f2 = clean_fighter_name(raw.get("fighter2") or raw.get("B_fighter") or "")
        o1 = raw.get("f1_odds", raw.get("R_odds"))
        o2 = raw.get("f2_odds", raw.get("B_odds"))

        if f1 and f2 and _odds_pair_valid(o1, o2):
            rows.append(
                {
                    "event": event,
                    "date": dt,
                    "fighter1": f1,
                    "fighter2": f2,
                    "f1_odds": float(o1),
                    "f2_odds": float(o2),
                    "source": source,
                }
            )
            continue

        fav = clean_fighter_name(raw.get("favourite", ""))
        dog = clean_fighter_name(raw.get("underdog", ""))
        mapped = None
        if f1 and f2 and fav and dog:
            mapped = _map_fav_dog_odds(
                f1, f2, fav, dog, raw.get("favourite_odds"), raw.get("underdog_odds")
            )
        if mapped:
            rows.append(
                {
                    "event": event,
                    "date": dt,
                    "fighter1": f1,
                    "fighter2": f2,
                    "f1_odds": mapped[0],
                    "f2_odds": mapped[1],
                    "source": source,
                }
            )
            continue

        if fav and dog and _odds_pair_valid(raw.get("favourite_odds"), raw.get("underdog_odds")):
            rows.append(
                {
                    "event": event,
                    "date": dt,
                    "fighter1": fav,
                    "fighter2": dog,
                    "f1_odds": float(raw["favourite_odds"]),
                    "f2_odds": float(raw["underdog_odds"]),
                    "source": source,
                }
            )

    if not rows:
        return pd.DataFrame(columns=_ODDS_UNIFIED_COLUMNS)

    out = pd.DataFrame(rows)
    return (
        out.drop_duplicates(subset=["event", "date", "fighter1", "fighter2"], keep="last")
        .sort_values("date")
        .reset_index(drop=True)
    )


def _download_odds_csv(url: str, *, source: str) -> pd.DataFrame:
    try:
        response = requests.Session().get(url, timeout=config.REQUEST_TIMEOUT_SEC)
        response.raise_for_status()
        df = pd.read_csv(pd.io.common.BytesIO(response.content))
    except Exception as exc:
        raise OddsLoadError(f"Odds CSV download failed: {url}") from exc
    cache = config.CACHE_DIR / f"odds_{hashlib.md5(url.encode()).hexdigest()[:10]}.csv"
    cache.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(cache, index=False)
    return _normalize_odds_frame(df, source=source)


def _load_local_odds_file(path: Path) -> pd.DataFrame | None:
    if not path.is_file():
        return None
    try:
        return _normalize_odds_frame(pd.read_csv(path), source=f"local:{path.name}")
    except Exception as exc:
        logger.warning("Could not read odds file %s: %s", path, exc)
        return None


def _discover_kaggle_odds_csvs() -> list[Path]:
    """Return CSV paths from a manual or CLI Kaggle drop."""
    paths: list[Path] = []
    kaggle_dir = config.KAGGLE_ODDS_DIR
    if kaggle_dir.is_dir():
        paths.extend(sorted(kaggle_dir.glob("*.csv")))
        paths.extend(sorted(kaggle_dir.glob("**/*.csv")))
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in paths:
        resolved = p.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(p)
    return unique


def _download_kaggle_ufc_betting_odds() -> list[Path]:
    """
    Fetch jerzyszocik/ufc-betting-odds-daily-dataset when Kaggle credentials exist.

    Requires ``~/.kaggle/kaggle.json`` or ``KAGGLE_USERNAME`` / ``KAGGLE_KEY`` env vars.
    """
    dest = config.KAGGLE_ODDS_DIR
    dest.mkdir(parents=True, exist_ok=True)
    existing = _discover_kaggle_odds_csvs()
    if existing:
        return existing

    slug = config.KAGGLE_UFC_BETTING_ODDS_SLUG
    try:
        from kaggle.api.kaggle_api_extended import KaggleApi

        api = KaggleApi()
        api.authenticate()
        api.dataset_download_files(slug, path=str(dest), unzip=True)
        logger.info("Downloaded Kaggle odds dataset: %s", slug)
        return _discover_kaggle_odds_csvs()
    except Exception as exc:
        logger.debug("Kaggle API download skipped (%s): %s", slug, exc)

    import shutil
    import subprocess

    kaggle_bin = shutil.which("kaggle")
    if kaggle_bin:
        try:
            subprocess.run(
                [kaggle_bin, "datasets", "download", "-d", slug, "-p", str(dest), "--unzip"],
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
            logger.info("Downloaded Kaggle odds dataset via CLI: %s", slug)
            return _discover_kaggle_odds_csvs()
        except Exception as exc:
            logger.debug("Kaggle CLI download skipped: %s", exc)

    return []


def _load_historical_odds_frames() -> list[pd.DataFrame]:
    frames: list[pd.DataFrame] = []

    for url, label in [
        (config.JANSEN_COMPLETE_URL, "kaggle:jansen_complete"),
        (config.JANSEN_CLEANED_ODDS_URL, "kaggle:jansen_cleaned_odds"),
        (config.ULTIMATE_UFC_DATASET_URL, "kaggle:ultimate_ufc_dataset"),
    ]:
        try:
            frames.append(_download_odds_csv(url, source=label))
            logger.info("Loaded %s odds rows from %s", len(frames[-1]), label)
        except OddsLoadError as exc:
            logger.debug("Skipped %s: %s", label, exc)

    for path in config.LOCAL_ODDS_CANDIDATES:
        local = _load_local_odds_file(path)
        if local is not None and not local.empty:
            frames.append(local)
            logger.info("Loaded %s odds rows from %s", len(local), path.name)

    for path in _download_kaggle_ufc_betting_odds():
        local = _load_local_odds_file(path)
        if local is not None and not local.empty:
            frames.append(local)
            logger.info(
                "Loaded %s odds rows from Kaggle jerzyszocik (%s)",
                len(local),
                path.name,
            )

    return [f for f in frames if f is not None and not f.empty]


def _supplement_from_odds_api_cache(frames: list[pd.DataFrame]) -> list[pd.DataFrame]:
    cache_path = config.ODDS_API_CACHE_PATH
    if not cache_path.is_file():
        return frames
    try:
        cached = pd.read_csv(cache_path, parse_dates=["commence_time"])
    except Exception:
        return frames
    rows: list[dict[str, Any]] = []
    for raw in cached.to_dict(orient="records"):
        f1 = clean_fighter_name(raw.get("fighter_1", ""))
        f2 = clean_fighter_name(raw.get("fighter_2", ""))
        if not f1 or not f2 or not _odds_pair_valid(raw.get("f1_odds"), raw.get("f2_odds")):
            continue
        rows.append(
            {
                "event": "",
                "date": clean_date(raw.get("commence_time")),
                "fighter1": f1,
                "fighter2": f2,
                "f1_odds": float(raw["f1_odds"]),
                "f2_odds": float(raw["f2_odds"]),
                "source": "odds_api:cache",
            }
        )
    if rows:
        frames.append(pd.DataFrame(rows))
    return frames


def build_unified_odds_table() -> pd.DataFrame:
    """Combine all odds sources; later sources win on duplicate fights."""
    ensure_data_dirs()
    frames = _supplement_from_odds_api_cache(_load_historical_odds_frames())
    if not frames:
        return pd.DataFrame(columns=_ODDS_UNIFIED_COLUMNS)

    unified = pd.concat(frames, ignore_index=True)
    unified["date"] = pd.to_datetime(unified["date"], errors="coerce")
    unified = unified.dropna(subset=["date", "fighter1", "fighter2"])
    unified = (
        unified.drop_duplicates(subset=["event", "date", "fighter1", "fighter2"], keep="last")
        .sort_values("date")
        .reset_index(drop=True)
    )
    unified.to_csv(config.HISTORICAL_ODDS_CACHE, index=False)
    return unified


def _lookup_odds_for_fight(
    odds_table: pd.DataFrame,
    *,
    event: str,
    fight_date: pd.Timestamp,
    fighter1: str,
    fighter2: str,
) -> tuple[float, float] | None:
    """Match odds by date + fighters; fuzzy fallback on event/date."""
    if odds_table.empty:
        return None

    f1 = clean_fighter_name(fighter1)
    f2 = clean_fighter_name(fighter2)
    ev_norm = _normalize_event_name(event)
    dt = pd.Timestamp(fight_date).normalize()

    exact = odds_table[
        (odds_table["date"].dt.normalize() == dt)
        & (odds_table["fighter1"] == f1)
        & (odds_table["fighter2"] == f2)
    ]
    if not exact.empty:
        row = exact.iloc[-1]
        return float(row["f1_odds"]), float(row["f2_odds"])

    swapped = odds_table[
        (odds_table["date"].dt.normalize() == dt)
        & (odds_table["fighter1"] == f2)
        & (odds_table["fighter2"] == f1)
    ]
    if not swapped.empty:
        row = swapped.iloc[-1]
        return float(row["f2_odds"]), float(row["f1_odds"])

    if ev_norm:
        event_rows = odds_table[
            (odds_table["date"].dt.normalize() == dt)
            & (odds_table["event"].map(_normalize_event_name) == ev_norm)
        ]
        for _, row in event_rows.iterrows():
            if _fighters_same_person(f1, row["fighter1"]) and _fighters_same_person(
                f2, row["fighter2"]
            ):
                return float(row["f1_odds"]), float(row["f2_odds"])
            if _fighters_same_person(f1, row["fighter2"]) and _fighters_same_person(
                f2, row["fighter1"]
            ):
                return float(row["f2_odds"]), float(row["f1_odds"])

    for _, row in odds_table[odds_table["date"].dt.normalize() == dt].iterrows():
        if _fighters_same_person(f1, row["fighter1"]) and _fighters_same_person(f2, row["fighter2"]):
            return float(row["f1_odds"]), float(row["f2_odds"])
        if _fighters_same_person(f1, row["fighter2"]) and _fighters_same_person(f2, row["fighter1"]):
            return float(row["f2_odds"]), float(row["f1_odds"])

    return None


def merge_historical_odds(fights: pd.DataFrame) -> pd.DataFrame:
    """
    Left-join historical odds on event_date + fighter names (fuzzy fallback).

    Unmatched fights keep NaN ``f1_odds`` / ``f2_odds``; no rows are dropped.
    """
    if fights.empty:
        return fights

    out = fights.copy()
    if "f1_odds" not in out.columns:
        out["f1_odds"] = np.nan
    if "f2_odds" not in out.columns:
        out["f2_odds"] = np.nan

    odds_table = build_unified_odds_table()
    if odds_table.empty:
        logger.warning("No historical odds sources loaded")
        return out

    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    filled = 0
    for idx, row in out.iterrows():
        if pd.notna(row.get("f1_odds")) and pd.notna(row.get("f2_odds")):
            if _odds_pair_valid(row["f1_odds"], row["f2_odds"]):
                continue
        if pd.isna(row.get("date")):
            continue

        matched = _lookup_odds_for_fight(
            odds_table,
            event=str(row.get("event", "") or ""),
            fight_date=row["date"],
            fighter1=str(row.get("fighter1", "")),
            fighter2=str(row.get("fighter2", "")),
        )
        if matched is None:
            continue
        out.at[idx, "f1_odds"] = matched[0]
        out.at[idx, "f2_odds"] = matched[1]
        filled += 1

    logger.info(
        "Odds merge: filled %s fights; %s/%s have odds",
        filled,
        int(out["f1_odds"].notna().sum()),
        len(out),
    )
    return out


# ---------------------------------------------------------------------------
# Upcoming card sources
# ---------------------------------------------------------------------------


def _fighter_name_from_ufc_corner(corner: BeautifulSoup) -> str:
    given = corner.select_one(".c-listing-fight__corner-given-name")
    family = corner.select_one(".c-listing-fight__corner-family-name")
    if given and family:
        return clean_fighter_name(f"{given.get_text(strip=True)} {family.get_text(strip=True)}")
    link = corner.select_one("a")
    return clean_fighter_name(link.get_text(strip=True) if link else "")


def _scrape_ufc_event_card(event_path: str) -> pd.DataFrame:
    """Scrape a single UFC.com event page fight card."""
    url = urljoin("https://www.ufc.com", event_path.split("#")[0])
    html = _request_text(url)
    soup = BeautifulSoup(html, "lxml")

    event_name = ""
    title_el = soup.select_one(
        ".field--name-node-title, h1.hero-content__title, h1.c-hero__header"
    )
    if title_el:
        event_name = title_el.get_text(" ", strip=True)

    event_date = pd.NaT
    time_el = soup.select_one("time[datetime]")
    if time_el and time_el.get("datetime"):
        event_date = clean_date(time_el["datetime"])
    if pd.isna(event_date):
        date_el = soup.select_one(".c-hero__headline-suffix, .c-event-fight__date")
        if date_el:
            event_date = clean_date(date_el.get_text(strip=True))

    location = ""
    loc_el = soup.select_one(
        ".field--name-node-title + .c-hero__headline, .c-event-fight__location, .e-tile-hero__location"
    )
    if loc_el:
        location = loc_el.get_text(" ", strip=True)

    rows: list[dict[str, Any]] = []
    fights = soup.select(".c-listing-fight")
    for order, fight in enumerate(fights, start=1):
        red = fight.select_one(".c-listing-fight__corner-name--red")
        blue = fight.select_one(".c-listing-fight__corner-name--blue")
        if not red or not blue:
            continue
        wc_el = fight.select_one(".c-listing-fight__class-text")
        weight_class = clean_weight_class(wc_el.get_text(strip=True) if wc_el else "")
        rows.append(
            {
                "event": event_name or "Upcoming UFC Event",
                "date": event_date,
                "location": location,
                "fighter1": _fighter_name_from_ufc_corner(red),
                "fighter2": _fighter_name_from_ufc_corner(blue),
                "winner": "",
                "weight_class": weight_class,
                "is_title_fight": _is_title_bout(weight_class),
                "is_main_event": int(order == 1),
                "bout_order": order,
                "event_url": url,
                "source": "ufc.com:event",
            }
        )

    if not rows:
        raise DataLoaderError(f"No fights found on UFC event page: {url}")
    return pd.DataFrame(rows)


def _discover_ufc_upcoming_event_paths() -> list[dict[str, str]]:
    html = _request_text(config.UFC_EVENTS_URL)
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    events: list[dict[str, str]] = []

    for card in soup.select(".c-event-tile, .l-listing__item"):
        link = card.select_one("a[href*='/event/']")
        if link is None:
            continue
        href = link["href"].split("#")[0]
        if href in seen:
            continue
        seen.add(href)
        name_el = card.select_one(".c-event-tile__title, .c-card-event__title")
        date_el = card.select_one(".c-event-tile__date, .c-card-event__date")
        events.append(
            {
                "event_path": href,
                "event_name": name_el.get_text(strip=True) if name_el else "",
                "event_date": date_el.get_text(strip=True) if date_el else "",
            }
        )

    if not events:
        for link in soup.select("a[href^='/event/']"):
            href = link["href"].split("#")[0]
            if href in seen:
                continue
            seen.add(href)
            events.append({"event_path": href, "event_name": link.get_text(strip=True), "event_date": ""})

    return events


def _parse_espn_scoreboard_payload(
    data: dict[str, Any],
    *,
    completed_only: bool = False,
) -> list[dict[str, Any]]:
    """Turn ESPN scoreboard JSON into fight row dicts."""
    rows: list[dict[str, Any]] = []

    for event in data.get("events", []):
        event_name = event.get("name", "")
        event_date = clean_date(event.get("date"))
        location = ""
        venues = event.get("venues") or []
        if venues:
            venue = venues[0]
            city = venue.get("address", {}).get("city", "")
            state = venue.get("address", {}).get("state", "")
            location = ", ".join(part for part in (city, state) if part)

        for idx, comp in enumerate(event.get("competitions", []), start=1):
            competitors = comp.get("competitors", [])
            if len(competitors) < 2:
                continue

            status = comp.get("status", {}).get("type", {})
            is_completed = bool(status.get("completed"))
            if completed_only and not is_completed:
                continue

            f1 = clean_fighter_name(competitors[0].get("athlete", {}).get("displayName", ""))
            f2 = clean_fighter_name(competitors[1].get("athlete", {}).get("displayName", ""))
            comp_type = comp.get("type", {})
            weight_class = clean_weight_class(
                comp_type.get("text") or comp_type.get("abbreviation") or ""
            )

            winner = ""
            for competitor in competitors:
                if competitor.get("winner"):
                    winner = clean_fighter_name(
                        competitor.get("athlete", {}).get("displayName", "")
                    )
                    break

            if completed_only and not winner:
                continue

            rows.append(
                {
                    "fight_id": str(comp.get("id", "") or ""),
                    "event": event_name,
                    "date": event_date,
                    "location": location,
                    "fighter1": f1,
                    "fighter2": f2,
                    "winner": winner,
                    "weight_class": weight_class,
                    "round": comp.get("status", {}).get("period"),
                    "is_title_fight": _is_title_bout(weight_class),
                    "is_main_event": int(idx == 1),
                    "bout_order": idx,
                    "source": "espn:scoreboard",
                }
            )

    return rows


def _fetch_espn_scoreboard_url(url: str, *, completed_only: bool = False) -> pd.DataFrame:
    payload = _session().get(url, timeout=config.REQUEST_TIMEOUT_SEC)
    payload.raise_for_status()
    rows = _parse_espn_scoreboard_payload(payload.json(), completed_only=completed_only)
    if not rows:
        raise DataLoaderError(f"ESPN scoreboard returned no fights: {url}")
    return pd.DataFrame(rows)


def _fetch_espn_scoreboard(*, event_index: int = 0) -> pd.DataFrame:
    payload = _session().get(config.ESPN_UFC_SCOREBOARD_URL, timeout=config.REQUEST_TIMEOUT_SEC)
    payload.raise_for_status()
    data = payload.json()
    events = data.get("events") or []
    if not events:
        raise DataLoaderError("ESPN scoreboard returned no events")
    idx = max(0, min(event_index, len(events) - 1))
    rows = _parse_espn_scoreboard_payload({"events": [events[idx]]}, completed_only=False)
    if not rows:
        raise DataLoaderError(f"ESPN scoreboard event_index={event_index} returned no fights")
    return pd.DataFrame(rows)


def _fetch_espn_historical(*, since: pd.Timestamp | None = None) -> pd.DataFrame:
    """Pull completed UFC fights from ESPN scoreboard API (2024+ coverage)."""
    current_year = datetime.now(timezone.utc).year
    start_year = int(since.year) if since is not None and not pd.isna(since) else 1993
    frames: list[pd.DataFrame] = []

    for year in range(start_year, current_year + 1):
        url = (
            f"{config.ESPN_UFC_SCOREBOARD_URL}"
            f"?dates={year}0101-{year}1231"
        )
        try:
            df = _fetch_espn_scoreboard_url(url, completed_only=True)
        except DataLoaderError as exc:
            logger.debug("ESPN year %s skipped: %s", year, exc)
            continue
        if since is not None:
            df = df[df["date"] > since]
        if not df.empty:
            frames.append(df)
        time.sleep(0.25)

    if not frames:
        raise DataLoaderError("ESPN historical fetch returned no completed fights.")
    return _clean_fights_frame(pd.concat(frames, ignore_index=True), source="espn:historical")


def _scrape_ufcstats_upcoming(*, event_index: int = 0) -> pd.DataFrame:
    html = _request_text(config.UFC_STATS_UPCOMING_URL)
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", class_="b-statistics__table-events")
    if table is None:
        raise ScrapeBlockedError("ufcstats upcoming table not found.")

    events: list[dict[str, str]] = []
    for tr in table.find_all("tr", class_="b-statistics__table-row"):
        link = tr.find("a")
        if link is None:
            continue
        date_el = tr.find("span", class_="b-statistics__date")
        events.append(
            {
                "event_url": urljoin(config.UFC_STATS_UPCOMING_URL, link["href"]),
                "event_name": link.get_text(strip=True),
                "event_date": date_el.get_text(strip=True) if date_el else "",
            }
        )

    if not events:
        raise DataLoaderError("No upcoming events on ufcstats.")

    # Upcoming event at requested index (0 = soonest)
    pick = min(max(0, event_index), len(events) - 1)
    event = events[pick]
    fights = _scrape_ufcstats_event_fights(event["event_url"], event["event_name"], event["event_date"])
    df = pd.DataFrame(fights)
    df["winner"] = ""
    return df


# ---------------------------------------------------------------------------
# UFCstats enrichment (Greco1899 CSVs + live scrape for 2025)
# ---------------------------------------------------------------------------

_ENRICH_F1_COLS = {
    "height": "fighter1_height",
    "reach": "fighter1_reach",
    "dob": "fighter1_dob",
    "stance": "fighter1_stance",
    "sig_strikes_per_min": "fighter1_sig_strikes_landed_pm",
    "sig_strike_acc": "fighter1_sig_strikes_accuracy",
    "td_acc": "fighter1_takedown_accuracy",
    "td_defense": "fighter1_takedown_defence",
    "sub_avg": "fighter1_submission_avg_attempted_per15m",
}
_ENRICH_F2_COLS = {k: v.replace("fighter1", "fighter2") for k, v in _ENRICH_F1_COLS.items()}


def _parse_inches_measurement(value: Any) -> float | None:
    """Parse height/reach strings like ``5' 11\"`` or ``72\"`` into inches."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip().replace("\u2033", '"').replace("\u201d", '"')
    feet_inch = re.match(r"(\d+)\s*'\s*(\d+)", text)
    if feet_inch:
        return float(int(feet_inch.group(1)) * 12 + int(feet_inch.group(2)))
    inches_only = re.match(r"(\d+(?:\.\d+)?)\s*\"?", text)
    if inches_only:
        return float(inches_only.group(1))
    return None


def _parse_of_fraction(value: Any) -> tuple[float | None, float | None]:
    """Parse ``9 of 9`` style counts."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None, None
    text = str(value).strip()
    match = re.match(r"(\d+)\s+of\s+(\d+)", text, flags=re.IGNORECASE)
    if not match:
        return None, None
    return float(match.group(1)), float(match.group(2))


def _parse_pct_value(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip().replace("%", "")
    if text in {"", "---", "nan"}:
        return None
    try:
        val = float(text)
    except ValueError:
        return None
    return val / 100.0 if val > 1.0 else val


def _parse_ctrl_seconds(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if text in {"", "---"}:
        return None
    parts = text.split(":")
    try:
        if len(parts) == 2:
            return float(int(parts[0]) * 60 + int(parts[1]))
        if len(parts) == 3:
            return float(int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2]))
    except ValueError:
        return None
    return None


def _parse_bout_fighters(bout: str) -> tuple[str, str]:
    """Split ``Fighter A vs. Fighter B`` into two cleaned names."""
    text = str(bout or "").strip()
    for sep in (" vs. ", " vs ", " Vs. ", " VS "):
        if sep in text:
            left, right = text.split(sep, 1)
            return clean_fighter_name(left), clean_fighter_name(right)
    return "", ""


def _download_greco_csv(filename: str, *, force_refresh: bool = False) -> pd.DataFrame:
    """Download and cache a Greco1899/scrape_ufc_stats CSV."""
    ensure_data_dirs()
    cache_dir = config.UFCSTATS_GRECO_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / filename
    url = f"{config.GRECO_UFCSTATS_BASE_URL.rstrip('/')}/{filename}"

    if not force_refresh and _cache_is_fresh(cache_path, config.UFCSTATS_ENRICH_TTL_HOURS):
        return pd.read_csv(cache_path)

    try:
        response = _session().get(url, timeout=max(config.REQUEST_TIMEOUT_SEC, 60))
        response.raise_for_status()
        cache_path.write_bytes(response.content)
        return pd.read_csv(cache_path)
    except Exception as exc:
        if cache_path.is_file():
            logger.warning("Using stale Greco cache for %s (%s)", filename, exc)
            return pd.read_csv(cache_path)
        raise DataLoaderError(f"Failed to download {url}") from exc


def _build_greco_fighter_profiles(force_refresh: bool = False) -> pd.DataFrame:
    """Fighter tale-of-the-tape + career aggregates from Greco CSVs."""
    tott = _download_greco_csv("ufc_fighter_tott.csv", force_refresh=force_refresh)
    tott = tott.rename(columns=str.upper)
    profiles = pd.DataFrame(
        {
            "fighter": tott["FIGHTER"].map(clean_fighter_name),
            "height_in": tott["HEIGHT"].map(_parse_inches_measurement),
            "reach_in": tott["REACH"].map(_parse_inches_measurement),
            "stance": tott["STANCE"].astype(str).str.strip(),
            "dob": pd.to_datetime(tott["DOB"], errors="coerce"),
            "url": tott.get("URL", ""),
        }
    )
    profiles = profiles[profiles["fighter"].astype(bool)].drop_duplicates("fighter", keep="last")

    try:
        stats = _download_greco_csv("ufc_fight_stats.csv", force_refresh=force_refresh)
    except DataLoaderError:
        logger.warning("ufc_fight_stats.csv unavailable; profiles only")
        profiles["fighter_key"] = profiles["fighter"]
        return profiles

    stats = stats.rename(columns=str.upper)
    landed, attempted = zip(*stats["SIG.STR."].map(_parse_of_fraction))
    stats["sig_landed"] = landed
    stats["sig_attempted"] = attempted
    td_landed, td_attempted = zip(*stats["TD"].map(_parse_of_fraction))
    stats["td_landed"] = td_landed
    stats["td_attempted"] = td_attempted
    stats["sig_acc_pct"] = stats["SIG.STR. %"].map(_parse_pct_value)
    stats["ctrl_sec"] = stats["CTRL"].map(_parse_ctrl_seconds)
    stats["sub_att"] = pd.to_numeric(stats["SUB.ATT"], errors="coerce")
    stats["round_num"] = stats["ROUND"].astype(str).str.extract(r"(\d+)").astype(float)

    bout = stats.groupby(["EVENT", "BOUT", "FIGHTER"], as_index=False).agg(
        sig_landed=("sig_landed", "sum"),
        sig_attempted=("sig_attempted", "sum"),
        td_landed=("td_landed", "sum"),
        td_attempted=("td_attempted", "sum"),
        sub_att=("sub_att", "sum"),
        ctrl_sec=("ctrl_sec", "sum"),
        max_round=("round_num", "max"),
        sig_acc_pct=("sig_acc_pct", "mean"),
    )
    bout["fighter"] = bout["FIGHTER"].map(clean_fighter_name)
    bout["fight_minutes"] = bout["max_round"].fillna(3) * 5.0
    bout["sig_strikes_per_min"] = bout["sig_landed"] / bout["fight_minutes"].replace(0, np.nan)

    career = bout.groupby("fighter", as_index=False).agg(
        sig_landed=("sig_landed", "sum"),
        sig_attempted=("sig_attempted", "sum"),
        td_landed=("td_landed", "sum"),
        td_attempted=("td_attempted", "sum"),
        sub_att=("sub_att", "sum"),
        ctrl_sec=("ctrl_sec", "sum"),
        sig_strikes_per_min=("sig_strikes_per_min", "mean"),
        sig_acc_pct=("sig_acc_pct", "mean"),
        n_fights=("sig_landed", "count"),
    )
    career["sig_strike_acc"] = np.where(
        career["sig_attempted"] > 0,
        career["sig_landed"] / career["sig_attempted"],
        career["sig_acc_pct"],
    )
    career["td_acc"] = np.where(
        career["td_attempted"] > 0,
        career["td_landed"] / career["td_attempted"],
        np.nan,
    )
    career["sub_avg"] = career["sub_att"] / career["n_fights"].replace(0, np.nan)
    career["control_time_per_min"] = career["ctrl_sec"] / (
        career["n_fights"] * 15.0
    ).replace(0, np.nan)
    career["td_defense"] = np.nan  # filled via opponent merge below

    opp = bout[["EVENT", "BOUT", "fighter", "td_landed", "td_attempted"]].rename(
        columns={
            "fighter": "opponent",
            "td_landed": "opp_td_landed",
            "td_attempted": "opp_td_attempted",
        }
    )
    bout_opp = bout.merge(
        opp,
        on=["EVENT", "BOUT"],
        how="left",
        suffixes=("", "_drop"),
    )
    bout_opp = bout_opp[bout_opp["opponent"] != bout_opp["fighter"]]
    bout_opp["td_def_fight"] = np.where(
        bout_opp["opp_td_attempted"] > 0,
        1.0 - bout_opp["opp_td_landed"] / bout_opp["opp_td_attempted"],
        np.nan,
    )
    td_def = bout_opp.groupby("fighter", as_index=False)["td_def_fight"].mean()
    career = career.merge(td_def, on="fighter", how="left")
    career["td_defense"] = career["td_def_fight"].fillna(career["td_defense"])

    profiles = profiles.merge(
        career[
            [
                "fighter",
                "sig_strike_acc",
                "td_acc",
                "td_defense",
                "sig_strikes_per_min",
                "sub_avg",
                "control_time_per_min",
            ]
        ],
        on="fighter",
        how="left",
    )
    profiles["fighter_key"] = profiles["fighter"]
    return profiles


def _build_fighter_lookup(profiles: pd.DataFrame) -> dict[str, pd.Series]:
    """Exact-name index; fuzzy resolution uses ``_fighters_same_person``."""
    lookup: dict[str, pd.Series] = {}
    for _, row in profiles.iterrows():
        key = clean_fighter_name(row["fighter"])
        if key:
            lookup[key] = row
    return lookup


def _lookup_fighter_profile(
    name: str,
    lookup: dict[str, pd.Series],
    profiles: pd.DataFrame,
) -> pd.Series | None:
    clean = clean_fighter_name(name)
    if not clean:
        return None
    if clean in lookup:
        return lookup[clean]
    last = clean.split()[-1] if clean.split() else ""
    candidates = profiles[profiles["fighter"].str.split().str[-1] == last]
    for _, row in candidates.iterrows():
        if _fighters_same_person(clean, row["fighter"]):
            return row
    for _, row in profiles.iterrows():
        if _fighters_same_person(clean, row["fighter"]):
            return row
    return None


def _apply_profile_to_side(
    out: pd.DataFrame,
    idx: int,
    profile: pd.Series,
    *,
    side: int,
) -> None:
    col_map = _ENRICH_F1_COLS if side == 1 else _ENRICH_F2_COLS
    mapping = {
        col_map["height"]: profile.get("height_in"),
        col_map["reach"]: profile.get("reach_in"),
        col_map["dob"]: profile.get("dob"),
        col_map["stance"]: profile.get("stance"),
        col_map["sig_strikes_per_min"]: profile.get("sig_strikes_per_min"),
        col_map["sig_strike_acc"]: profile.get("sig_strike_acc"),
        col_map["td_acc"]: profile.get("td_acc"),
        col_map["td_defense"]: profile.get("td_defense"),
        col_map["sub_avg"]: profile.get("sub_avg"),
    }
    for col, val in mapping.items():
        if col not in out.columns:
            out[col] = np.nan
        if pd.isna(out.at[idx, col]) and pd.notna(val):
            if "dob" in col:
                if out[col].dtype == object or str(out[col].dtype).startswith("string"):
                    out[col] = pd.to_datetime(out[col], errors="coerce")
                val = pd.to_datetime(val, errors="coerce")
            out.at[idx, col] = val


def _merge_greco_fight_results(
    fights: pd.DataFrame,
    *,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Supplement fight rows from Greco ufc_fight_results + event_details."""
    try:
        results = _download_greco_csv("ufc_fight_results.csv", force_refresh=force_refresh)
        events = _download_greco_csv("ufc_event_details.csv", force_refresh=force_refresh)
    except DataLoaderError as exc:
        logger.debug("Greco fight results skipped: %s", exc)
        return fights

    results = results.rename(columns=str.upper)
    events = events.rename(columns=str.upper)
    events["date"] = pd.to_datetime(events["DATE"], errors="coerce")
    event_dates = events.set_index("EVENT")["date"].to_dict()

    extra_rows: list[dict[str, Any]] = []
    for raw in results.to_dict(orient="records"):
        event = str(raw.get("EVENT", "")).strip()
        bout = str(raw.get("BOUT", "")).strip()
        f1, f2 = _parse_bout_fighters(bout)
        if not f1 or not f2:
            continue
        dt = clean_date(event_dates.get(event))
        if pd.isna(dt):
            continue

        outcome = str(raw.get("OUTCOME", "")).strip()
        winner = ""
        if "/" in outcome:
            w_side, l_side = outcome.split("/", 1)
            if w_side.strip().upper() == "W":
                winner = f1
            elif w_side.strip().upper() == "L":
                winner = f2
            elif l_side.strip().upper() == "W":
                winner = f2
            else:
                winner = f1

        fight_url = str(raw.get("URL", "") or "")
        fight_id = fight_url.rsplit("/", 1)[-1] if fight_url else None
        extra_rows.append(
            {
                "fight_id": _make_fight_id(event, dt, f1, f2, fight_id),
                "event": event,
                "date": dt,
                "fighter1": f1,
                "fighter2": f2,
                "winner": winner,
                "weight_class": clean_weight_class(raw.get("WEIGHTCLASS", "")),
                "method": str(raw.get("METHOD", "") or "").strip(),
                "round": pd.to_numeric(raw.get("ROUND"), errors="coerce"),
                "time": str(raw.get("TIME", "") or "").strip(),
                "source": "greco:ufc_fight_results",
            }
        )

    if not extra_rows:
        return fights

    greco = _clean_fights_frame(pd.DataFrame(extra_rows), source="greco:ufc_fight_results")
    return _merge_fight_frames([fights, greco])


def _scrape_ufcstats_recent(*, year_min: int = 2024, max_events: int = 60) -> pd.DataFrame:
    """Scrape completed ufcstats events (newest first) down to ``year_min``."""
    html = _request_text(config.UFC_STATS_BASE_URL)
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", class_="b-statistics__table-events")
    if table is None:
        raise ScrapeBlockedError("ufcstats events table not found.")

    event_rows: list[dict[str, str]] = []
    for tr in table.find_all("tr", class_="b-statistics__table-row"):
        link = tr.find("a")
        if link is None or not link.get("href"):
            continue
        date_el = tr.find("span", class_="b-statistics__date")
        event_date = date_el.get_text(strip=True) if date_el else ""
        dt = clean_date(event_date)
        if pd.notna(dt) and dt.year < year_min:
            break
        event_rows.append(
            {
                "event_url": urljoin(config.UFC_STATS_BASE_URL, link["href"]),
                "event_name": link.get_text(strip=True),
                "event_date": event_date,
            }
        )
        if len(event_rows) >= max_events:
            break

    fights: list[dict[str, Any]] = []
    for event in tqdm(event_rows, desc=f"Scraping ufcstats ({year_min}+)"):
        fights.extend(
            _scrape_ufcstats_event_fights(
                event["event_url"], event["event_name"], event["event_date"]
            )
        )
        time.sleep(config.REQUEST_DELAY_SEC)

    if not fights:
        raise DataLoaderError("ufcstats recent scrape returned no fights.")
    return _clean_fights_frame(pd.DataFrame(fights), source="ufcstats:recent")


def enrich_fights_with_ufcstats(
    fights: pd.DataFrame,
    *,
    force_refresh: bool = False,
    scrape_recent: bool = True,
    year_min: int = 2024,
) -> pd.DataFrame:
    """
    Enrich fight rows with ufcstats/Greco fighter profiles and career stats.

    Joins on fighter names (fuzzy token match) and supplements 2024+ cards from
    Greco CSVs and optional live ufcstats scrape.
    """
    if fights.empty:
        return fights

    out = fights.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")

    out = _merge_greco_fight_results(out, force_refresh=force_refresh)

    if scrape_recent:
        try:
            scraped = _scrape_ufcstats_recent(year_min=year_min)
            out = _merge_fight_frames([out, scraped])
        except (DataLoaderError, ScrapeBlockedError) as exc:
            logger.info("ufcstats live scrape skipped: %s", exc)

    profiles = _build_greco_fighter_profiles(force_refresh=force_refresh)
    lookup = _build_fighter_lookup(profiles)

    _enrich_dtypes = {
        "fighter1_dob": "datetime64[ns]",
        "fighter2_dob": "datetime64[ns]",
        "fighter1_stance": object,
        "fighter2_stance": object,
    }
    for col in config.FIGHTS_ENRICHMENT_COLUMNS:
        if col not in out.columns:
            dtype = _enrich_dtypes.get(col, float)
            out[col] = pd.Series(dtype=dtype)

    for dob_col in ("fighter1_dob", "fighter2_dob"):
        if dob_col in out.columns:
            out[dob_col] = pd.to_datetime(out[dob_col], errors="coerce")

    filled_cells = 0
    target_mask = out["date"].dt.year >= year_min
    for idx in out.index[target_mask]:
        for side, fighter_col in ((1, "fighter1"), (2, "fighter2")):
            name = str(out.at[idx, fighter_col] or "")
            profile = _lookup_fighter_profile(name, lookup, profiles)
            if profile is None:
                continue
            before = sum(
                1
                for c in (_ENRICH_F1_COLS if side == 1 else _ENRICH_F2_COLS).values()
                if pd.notna(out.at[idx, c])
            )
            _apply_profile_to_side(out, idx, profile, side=side)
            after = sum(
                1
                for c in (_ENRICH_F1_COLS if side == 1 else _ENRICH_F2_COLS).values()
                if pd.notna(out.at[idx, c])
            )
            filled_cells += max(0, after - before)

    n_target = int(target_mask.sum())
    has_height = out.loc[target_mask, "fighter1_height"].notna() & out.loc[
        target_mask, "fighter2_height"
    ].notna()
    has_reach = out.loc[target_mask, "fighter1_reach"].notna() & out.loc[
        target_mask, "fighter2_reach"
    ].notna()
    has_acc = out.loc[target_mask, "fighter1_sig_strikes_accuracy"].notna() & out.loc[
        target_mask, "fighter2_sig_strikes_accuracy"
    ].notna()
    logger.info(
        "UFCstats enrich (%s+): %s fights | height %s | reach %s | sig_acc %s | +%s cells",
        year_min,
        n_target,
        int(has_height.sum()),
        int(has_reach.sum()),
        int(has_acc.sum()),
        filled_cells,
    )

    _write_meta(
        config.UFCSTATS_ENRICH_META_PATH,
        {
            "year_min": year_min,
            "target_fights": n_target,
            "filled_cells": filled_cells,
            "profiles": len(profiles),
        },
    )
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def save_fights(df: pd.DataFrame, path: Path | str | None = None) -> Path:
    """Persist cleaned fights to CSV."""
    ensure_data_dirs()
    out = Path(path) if path else config.RAW_FIGHTS_CSV
    out.parent.mkdir(parents=True, exist_ok=True)
    export_cols = [c for c in config.FIGHTS_SAVE_COLUMNS if c in df.columns]
    df[export_cols].to_csv(out, index=False)
    return out


def load_historical_data(
    *,
    force_refresh: bool = False,
    source: str = "auto",
    incremental: bool = True,
    enrich_ufcstats: bool = False,
) -> pd.DataFrame:
    """
    Download or load the latest UFC historical dataset into data/raw/fights.csv.

    Source priority (``source='auto'``):
    1. Fresh local ``data/raw/fights.csv`` cache (TTL from ``CACHE_TTL_HOURS``)
    2. Local Kaggle-style CSV dropped in ``data/raw/``
    3. HuggingFace ``JesterLabs/UFC_FIGHT_DATA`` (ufcstats format, ~8k fights)
    4. Fallback CSV URL (``HISTORICAL_DATA_URL``, jansen88 1994+ dataset)
    5. Optional incremental ufcstats scrape for the newest events
    6. ESPN scoreboard API for recent completed cards (2024+ when ufcstats blocked)

    Returns cleaned fights with canonical columns (event, date, fighter1, ...).
    """
    ensure_data_dirs()
    output_path = config.RAW_FIGHTS_CSV

    if not force_refresh and _cache_is_fresh(output_path):
        logger.info("Using cached fights.csv")
        return _read_fights_csv(output_path)

    existing = None
    since: pd.Timestamp | None = None
    if incremental and output_path.is_file() and not force_refresh:
        try:
            existing = _read_fights_csv(output_path)
            if not existing.empty:
                since = pd.to_datetime(existing["date"], errors="coerce").max()
                logger.info("Incremental update since %s", since.date())
        except Exception as exc:
            logger.warning("Could not load existing fights.csv for merge: %s", exc)

    frames: list[pd.DataFrame] = []
    errors: list[str] = []

    if source in {"auto", "local"}:
        for candidate in _LOCAL_PRIORITY:
            local = _load_local_candidate(candidate)
            if local is not None:
                frames.append(local)
                break

    if source in {"auto", "huggingface", "hf"}:
        try:
            frames.append(_download_huggingface_historical(since=since if incremental else None))
        except DataLoaderError as exc:
            errors.append(f"huggingface: {exc}")

    if source in {"auto", "url", "github"}:
        try:
            url_df = _download_csv_url(config.HISTORICAL_DATA_URL)
            if since is not None:
                url_df = url_df[url_df["date"] > since]
            if not url_df.empty:
                frames.append(url_df)
        except DataLoaderError as exc:
            errors.append(f"url: {exc}")

    if source in {"auto", "ufcstats"}:
        try:
            scraped = _scrape_ufcstats_completed(max_events=15)
            if since is not None:
                scraped = scraped[scraped["date"] > since]
            if not scraped.empty:
                frames.append(scraped)
        except (DataLoaderError, ScrapeBlockedError) as exc:
            errors.append(f"ufcstats: {exc}")

    if source in {"auto", "espn"}:
        try:
            espn_df = _fetch_espn_historical(since=since if incremental else None)
            if not espn_df.empty:
                frames.append(espn_df)
        except DataLoaderError as exc:
            errors.append(f"espn: {exc}")

    if not frames:
        raise DataLoaderError(
            "Failed to load historical UFC data. Attempts: " + "; ".join(errors or ["none"])
        )

    refreshed = _merge_fight_frames(frames)
    if existing is not None and not existing.empty:
        if _is_cleaned_fights_df(existing):
            existing_clean = existing
        else:
            existing_clean = _clean_fights_frame(existing, source="existing:cache")
        merged = _merge_fight_frames([existing_clean, refreshed])
    else:
        merged = refreshed

    merged = merge_historical_odds(merged)

    if enrich_ufcstats:
        merged = enrich_fights_with_ufcstats(merged, force_refresh=force_refresh)

    save_fights(merged, output_path)
    _write_meta(
        config.HISTORICAL_META_PATH,
        {
            "rows": len(merged),
            "min_date": str(merged["date"].min().date()),
            "max_date": str(merged["date"].max().date()),
            "sources": sorted(merged["source"].dropna().unique().tolist()),
            "errors": errors,
        },
    )
    logger.info("Saved %s fights to %s", len(merged), output_path)
    return _add_pipeline_aliases(merged)


def _events_from_card_cache() -> list[dict[str, str]]:
    """Build event list from per-index upcoming_card_*.csv when live discovery fails."""
    events: list[dict[str, str]] = []
    for path in sorted(config.CACHE_DIR.glob("upcoming_card_*.csv")):
        try:
            idx = int(path.stem.replace("upcoming_card_", ""))
        except ValueError:
            continue
        try:
            card = pd.read_csv(path, parse_dates=["date"])
        except Exception:
            continue
        if card.empty:
            continue
        row = card.iloc[0]
        event_date = ""
        if "date" in card.columns and card["date"].notna().any():
            event_date = str(card["date"].dropna().iloc[0].date())
        events.append(
            {
                "event_name": str(row.get("event", f"Cached card {idx}")),
                "event_path": str(row.get("event_url", f"cached:{idx}")),
                "event_date": event_date,
                "event_index": idx,
            }
        )
    return sorted(events, key=lambda e: (e.get("event_date") or "9999-12-31", int(e.get("event_index", 0))))


def list_upcoming_events() -> list[dict[str, str]]:
    """Return upcoming UFC.com events for dashboard selection."""
    try:
        events = _discover_ufc_upcoming_event_paths()
        if events:
            return events
    except DataLoaderError:
        pass
    cached = _events_from_card_cache()
    if cached:
        logger.info("Using %s cached upcoming card(s) (live discovery unavailable)", len(cached))
    return cached


def get_upcoming_card(
    *,
    event_index: int = 0,
    force_refresh: bool = False,
    source: str = "auto",
) -> pd.DataFrame:
    """
    Fetch the upcoming UFC fight card.

    Tries UFC.com event pages first, then ESPN scoreboard, then ufcstats upcoming.
    Caches result to ``data/cache/upcoming_card.csv``.
    """
    ensure_data_dirs()
    cache_path = config.CACHE_DIR / f"upcoming_card_{event_index}.csv"

    if not force_refresh and _cache_is_fresh(cache_path, ttl_hours=6):
        cached = pd.read_csv(cache_path, parse_dates=["date"])
        if not cached.empty:
            logger.debug("Using cached card for event_index=%s", event_index)
            return cached

    errors: list[str] = []
    card: pd.DataFrame | None = None

    if source in {"auto", "ufc", "ufc.com"}:
        try:
            events = _discover_ufc_upcoming_event_paths()
            if not events:
                raise DataLoaderError("No upcoming events found on UFC.com")
            idx = max(0, min(event_index, len(events) - 1))
            card = _scrape_ufc_event_card(events[idx]["event_path"])
        except DataLoaderError as exc:
            errors.append(f"ufc.com: {exc}")

    if card is None and source in {"auto", "espn"}:
        try:
            card = _fetch_espn_scoreboard(event_index=event_index)
        except DataLoaderError as exc:
            errors.append(f"espn: {exc}")

    if card is None and source in {"auto", "ufcstats"}:
        try:
            card = _scrape_ufcstats_upcoming(event_index=event_index)
        except (DataLoaderError, ScrapeBlockedError) as exc:
            errors.append(f"ufcstats: {exc}")

    if card is None or card.empty:
        if cache_path.is_file():
            try:
                cached = pd.read_csv(cache_path, parse_dates=["date"])
                if not cached.empty:
                    logger.warning(
                        "Using stale cached card for event_index=%s (fetch failed)",
                        event_index,
                    )
                    return cached
            except Exception:
                pass
        raise DataLoaderError(
            "Could not fetch upcoming card. Attempts: " + "; ".join(errors or ["none"])
        )

    card = card.reset_index(drop=True)
    card.to_csv(cache_path, index=False)
    return card


def load_fights(path: Path | str | None = None) -> pd.DataFrame:
    """
    Load cleaned fights from CSV and attach pipeline alias columns.

    If ``data/raw/fights.csv`` is missing, calls ``load_historical_data()`` first.
    """
    csv_path = Path(path) if path else config.RAW_FIGHTS_CSV
    if not csv_path.is_file():
        return load_historical_data()

    return _read_fights_csv(csv_path)


def load_processed_features(path: Path | str | None = None) -> pd.DataFrame:
    """Load engineered feature matrix."""
    csv_path = Path(path) if path else config.PROCESSED_FEATURES_CSV
    if not csv_path.is_file():
        raise FileNotFoundError(
            f"Processed features not found: {csv_path}. Run feature engineering first."
        )
    return pd.read_csv(csv_path, parse_dates=[config.DATE_COLUMN], low_memory=True)


def validate_columns(columns: Iterable[str]) -> None:
    """Raise if canonical fight columns are absent after aliasing."""
    mapped = _rename_to_canonical(pd.DataFrame(columns=list(columns))).columns
    required = {"fight_id", "event", "date", "fighter1", "fighter2", "weight_class"}
    missing = required - set(mapped)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")
