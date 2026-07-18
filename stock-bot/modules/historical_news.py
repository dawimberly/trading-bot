"""Historical news proxies for backtests (catalyst + Kimi/thinking realism)."""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import re
from pathlib import Path
from typing import Any

import config

ROOT = Path(__file__).resolve().parents[1]

_DAY_CACHE: dict[str, list[dict[str, Any]]] = {}
_SOURCE_POOL: list[str] | None = None
_BACKTEST_CTX: dict[str, Any] = {}

_TICKER_RE = re.compile(r"\b[A-Z]{1,5}\b")
_TICKER_FALSE_POSITIVE = frozenset(
    {
        "USD", "ETF", "FED", "GDP", "CPI", "FOMC", "SEC", "CEO", "CFO", "IPO",
        "EPS", "PE", "TV", "AI", "US", "UK", "EU", "AM", "PM", "AD", "API",
        "CNN", "RSS", "FAQ", "PDF", "URL", "NYSE", "NASDAQ",
    }
)

_JUNK_SUBSTRINGS: tuple[str, ...] = (
    "how relevant is this ad",
    "did you encounter any technical issues",
    "video player was slow",
    "video player",
    "video content never loaded",
    "ad froze",
    "did not finish loading",
    "audio on ad was too loud",
    "ad never loaded",
    "ad prevented",
    "content moved around while ad",
    "ad was repetitive",
    "ad feedback",
    "close ad feedback",
    "cancel submit",
    "thank you! your effort",
    "terms of service",
    "privacy policy",
    "sign in to your",
    "sign in my account",
    "my account settings",
    "topics you follow",
    "newsletters",
    "subscribe sign in",
    "close icon",
    "cnn values your feedback",
    "your cnn account",
    "watch listen subscribe",
    "calculators videos more",
)

_FINANCIAL_KEYWORDS: tuple[str, ...] = (
    "earnings", "revenue", "guidance", "profit", "eps", "beat", "miss",
    "fed", "fomc", "federal reserve", "interest rate", "rate cut", "rate hike",
    "inflation", "cpi", "ppi", "gdp", "jobs report", "payrolls", "unemployment",
    "treasury", "yield", "bond", "credit spread",
    "s&p", "s&p 500", "nasdaq", "dow", "russell", "index",
    "stock", "stocks", "equity", "equities", "market", "markets", "shares",
    "ai", "artificial intelligence", "datacenter", "semiconductor", "chip",
    "nvidia", "nvda", "apple", "aapl", "microsoft", "msft", "tesla", "tsla",
    "meta", "amazon", "amzn", "alphabet", "goog", "google",
    "acquisition", "acquire", "merger", "takeover", "buyout", "m&a",
    "fda", "approval", "pdufa", "clinical trial", "phase 3",
    "contract", "partnership", "deal", "licensing",
    "oil", "crude", "opec", "energy", "gold", "commodity",
    "tariff", "trade war", "geopolitical", "middle east",
    "volatility", "vix", "hedge", "risk-off", "risk-on",
    "ipo", "listing", "offering",
    "dividend", "buyback", "capex",
    "sector", "tech", "financials", "healthcare", "biotech",
)

_COMPANY_NAMES: tuple[str, ...] = (
    "nvidia", "apple", "microsoft", "tesla", "meta", "amazon", "alphabet",
    "google", "berkshire", "jpmorgan", "exxon", "chevron", "broadcom",
    "amd", "intel", "netflix", "salesforce", "palantir",
)

# Relevance categories: (weight, keyword phrases). Highest-signal events first.
# Weight is the base 0-100 contribution when a category matches.
_RELEVANCE_CATEGORIES: tuple[tuple[str, int, tuple[str, ...]], ...] = (
    ("earnings", 46, (
        "beats earnings", "misses earnings", "earnings beat", "earnings miss",
        "tops estimates", "quarterly results", "earnings", "eps", "revenue",
        "guidance", "profit", "outlook raised", "outlook cut", "beat", "miss",
    )),
    ("fed_macro", 44, (
        "fed holds", "fed cuts", "fed hikes", "rate cut", "rate hike",
        "federal reserve", "fomc", "powell", "interest rate", "fed",
        "cpi", "ppi", "inflation", "gdp", "jobs report", "payrolls",
        "unemployment",
    )),
    ("m_and_a", 42, (
        "to acquire", "acquisition", "acquire", "merger", "takeover",
        "buyout", "m&a", "deal to buy", "agrees to buy",
    )),
    ("contract", 38, (
        "contract win", "wins contract", "major contract", "awarded contract",
        "supply agreement", "wins deal", "landmark deal", "contract", "order book",
        "partnership", "licensing deal",
    )),
    ("insider", 36, (
        "insider cluster", "cluster buy", "insider buying", "insiders buy",
        "ceo buys", "cfo buys", "form 4", "executive buying", "insider",
    )),
    ("regulatory", 34, (
        "fda approval", "fda approves", "pdufa", "phase 3", "clinical trial",
        "antitrust", "settlement", "lawsuit", "approval",
    )),
    ("analyst", 26, (
        "upgrade", "downgrade", "price target", "initiated coverage",
        "overweight", "underweight", "buy rating", "sell rating",
    )),
    ("capital", 24, (
        "buyback", "share repurchase", "dividend increase", "special dividend",
        "capex", "ipo", "public offering", "stock split", "dividend",
    )),
    ("sector", 22, (
        "semiconductor", "chip demand", "sector rotation", "biotech",
        "healthcare", "financials", "energy sector", "sector",
    )),
    ("macro_geo", 20, (
        "opec", "tariff", "trade war", "geopolitical", "crude", "oil prices",
        "gold", "commodity", "treasury yields", "yields",
    )),
    ("market", 12, (
        "s&p 500", "s&p", "nasdaq", "dow", "russell", "record high",
        "index", "equities", "stocks", "volatility", "vix", "market",
    )),
)

# Clean, high-signal macro event pool — deterministically seeded per backtest day.
_MACRO_EVENT_POOL: tuple[str, ...] = (
    "Fed holds rates steady, signals patience on cuts",
    "Fed cuts interest rates by 25 bps",
    "CPI inflation cools more than expected",
    "Jobs report beats forecasts as unemployment holds",
    "S&P 500 hits record high on megacap strength",
    "Nvidia beats earnings on AI datacenter demand",
    "Oil prices jump on OPEC supply decision",
    "Treasury yields fall as growth data softens",
    "Semiconductor stocks rally on strong chip demand",
    "Major M&A deal reshapes technology sector",
    "Apple tops profit guidance for the quarter",
    "Microsoft cloud revenue beats on Azure growth",
    "Tech megacaps lead index higher into earnings season",
    "Gold rallies as safe-haven demand rises",
)

# Symbol event templates produce clean, meaningful, high-relevance headlines.
_SYMBOL_EVENT_TEMPLATES: tuple[str, ...] = (
    "{sym} beats earnings estimates on strong revenue",
    "{sym} tops profit guidance for the quarter",
    "Major contract win boosts {sym} outlook",
    "{sym} announces buyback and dividend increase",
    "Analysts upgrade {sym} on improving fundamentals",
    "{sym} shares climb on upbeat guidance",
    "{sym} lands strategic partnership deal",
)

_SYMBOL_TEMPLATES: dict[str, tuple[str, ...]] = {
    "NVDA": (
        "NVDA advances on AI/datacenter demand checks",
        "Nvidia GPU supply narrative supports semis leadership",
    ),
    "AAPL": (
        "AAPL services growth offsets hardware cycle concerns",
        "Apple supplier headlines mixed ahead of product cycle",
    ),
    "MSFT": (
        "MSFT cloud/Azure demand cited in enterprise software read-through",
        "Microsoft AI copilot adoption in focus for megacap earnings",
    ),
    "TSLA": (
        "TSLA delivery expectations drive EV sentiment swing",
        "Tesla margin debate lingers as price competition intensifies",
    ),
    "META": (
        "META ad rebound narrative supports communication services",
        "Meta AI capex spend in focus for megacap earnings",
    ),
}

_DEFAULT_POOL: tuple[str, ...] = (
    "Fed signals data-dependent path on interest rates",
    "NVDA leads semis as AI datacenter demand stays firm",
    "S&P 500 futures steady ahead of CPI and Fed speakers",
    "Oil volatility lifts energy vs growth stocks",
    "Tariff headlines whipsaw small-cap beta before Fed speak",
    "Safe-haven bid lifts gold as equity vol rises",
    "Treasury yields drift as growth data surprises",
    "Megacap earnings guidance in focus for tech leadership",
    "Semiconductor strength extends NVDA-led index gains",
    "Consumer discretionary softens on spending caution",
    "Credit spreads stable as risk appetite holds",
)


def clean_headline(text: str) -> str:
    """Strip HTML, ad/UI noise, and collapse whitespace."""
    if not text:
        return ""
    t = html.unescape(str(text))
    t = re.sub(r"&#x[0-9a-fA-F]+;?", " ", t)
    t = re.sub(r"&[a-zA-Z]+;", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    low = t.lower()
    for junk in _JUNK_SUBSTRINGS:
        if junk in low:
            idx = low.find(junk)
            if idx <= 40:
                t = t[idx + len(junk) :].strip(" -|:")
                low = t.lower()
            else:
                t = t[:idx].strip(" -|:")
                low = t.lower()
    t = re.sub(r"\s+", " ", t).strip(" -|:")
    return t[:280]


def is_junk_headline(text: str) -> bool:
    raw_low = str(text).lower()
    if any(junk in raw_low for junk in _JUNK_SUBSTRINGS):
        return True
    low = clean_headline(text).lower()
    if not low:
        return True
    if any(junk in low for junk in _JUNK_SUBSTRINGS):
        return True
    if low.count("?") >= 2 and "earnings" not in low and "fed" not in low:
        return True
    if len(low.split()) > 45:
        return True
    return False


def _headline_tickers(text: str) -> list[str]:
    found: list[str] = []
    for match in _TICKER_RE.findall(text.upper()):
        sym = config.normalize_symbol(match)
        if len(sym) < 2 or sym in _TICKER_FALSE_POSITIVE:
            continue
        if sym not in found:
            found.append(sym)
    return found


def financial_headline_score(text: str) -> int:
    """Higher = more useful financial headline."""
    cleaned = clean_headline(text)
    if not cleaned or is_junk_headline(cleaned):
        return 0
    low = cleaned.lower()
    score = 0
    for kw in _FINANCIAL_KEYWORDS:
        if kw in low:
            score += 2
    for name in _COMPANY_NAMES:
        if name in low:
            score += 3
    score += min(6, len(_headline_tickers(cleaned)) * 3)
    if 30 <= len(cleaned) <= 160:
        score += 2
    return score


def is_financial_headline(text: str) -> bool:
    """True when headline is clean and market-relevant."""
    cleaned = clean_headline(text)
    if len(cleaned) < 18:
        return False
    if is_junk_headline(cleaned):
        return False
    return financial_headline_score(cleaned) >= 4


def _best_category(low: str) -> tuple[str, int, int]:
    """Return (category, base_weight, secondary_matches) for a lowercased headline."""
    matches: list[tuple[int, str]] = []
    for cat, weight, phrases in _RELEVANCE_CATEGORIES:
        if any(phrase in low for phrase in phrases):
            matches.append((weight, cat))
    if not matches:
        return "generic", 0, 0
    matches.sort(reverse=True)
    return matches[0][1], matches[0][0], len(matches) - 1


def relevance_score(text: str, symbol: str | None = None) -> int:
    """0-100 relevance: category weight + ticker/company/length signal, minus vagueness."""
    cleaned = clean_headline(text)
    if not cleaned or is_junk_headline(cleaned):
        return 0
    low = cleaned.lower()
    category, base, secondary = _best_category(low)
    if base == 0:
        return 0
    score = base + min(12, secondary * 6)

    tickers = _headline_tickers(cleaned)
    score += min(12, len(tickers) * 5)
    if symbol:
        sym = config.normalize_symbol(symbol)
        if sym and (sym in tickers or sym.lower() in low):
            score += 14
    if any(name in low for name in _COMPANY_NAMES):
        score += 6

    n = len(cleaned)
    if 24 <= n <= 120:
        score += 8
    elif n > 200:
        score -= 8
    words = len(low.split())
    if words < 4:
        score -= 12
    if low.count("?") >= 1:
        score -= 6
    return max(0, min(100, score))


def clean_and_score_headline(
    title: str,
    date: str | dt.date | dt.datetime | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Clean a headline and attach a 0-100 relevance score + category."""
    cleaned = clean_headline(title)
    low = cleaned.lower()
    category, _, _ = _best_category(low) if cleaned else ("junk", 0, 0)
    score = relevance_score(cleaned, symbol=symbol)
    if score == 0:
        category = "junk" if (not cleaned or is_junk_headline(cleaned)) else "generic"
    result: dict[str, Any] = {
        "title": cleaned,
        "score": score,
        "category": category,
        "symbol": config.normalize_symbol(symbol) if symbol else None,
    }
    if date is not None:
        result["date"] = _parse_date(date).isoformat()
    return result


def _min_relevance(explicit: int | None = None) -> int:
    if explicit is not None:
        return int(explicit)
    return int(getattr(config, "HISTORICAL_NEWS_MIN_RELEVANCE", 50))


def _filter_titles(titles: list[str], *, limit: int | None = None) -> list[str]:
    seen: set[str] = set()
    ranked: list[tuple[int, str]] = []
    for raw in titles:
        cleaned = clean_headline(raw)
        if not is_financial_headline(cleaned):
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        ranked.append((financial_headline_score(cleaned), cleaned))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    out = [t for _, t in ranked]
    if limit is not None:
        return out[:limit]
    return out


def _rows_from_titles(
    titles: list[str],
    as_of: dt.date,
    *,
    source: str,
    symbol: str | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for title in _filter_titles(titles):
        scored = clean_and_score_headline(title, as_of, symbol=symbol)
        rows.append(
            {
                "title": scored["title"],
                "date": as_of.isoformat(),
                "source": source,
                "symbol": symbol,
                "score": scored["score"],
                "category": scored["category"],
            }
        )
    return rows


def cache_dir() -> Path:
    path = ROOT / config.HISTORICAL_NEWS_CACHE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _parse_date(value: str | dt.date | dt.datetime) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def _pool_index(seed: str, pool_len: int) -> int:
    if pool_len <= 0:
        return 0
    digest = hashlib.md5(seed.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % pool_len


def _extract_financial_from_blob(blob: str) -> list[str]:
    """Pull market-relevant phrases from scraped page text."""
    text = clean_headline(blob)
    if not text:
        return []
    candidates: list[str] = []
    for chunk in re.split(r"[|•\n]+", text):
        chunk = clean_headline(chunk)
        if is_financial_headline(chunk):
            candidates.append(chunk)
    if not candidates:
        for m in re.finditer(
            r"[^.!?]{12,180}\b(?:earnings|Fed|stock|market|NVDA|AI|acquisition|FDA|contract|revenue|guidance|S&P|Nasdaq)\b[^.!?]*[.!?]?",
            text,
            re.I,
        ):
            chunk = clean_headline(m.group(0))
            if is_financial_headline(chunk):
                candidates.append(chunk)
    return candidates


def _load_source_headline_pool() -> list[str]:
    global _SOURCE_POOL
    if _SOURCE_POOL is not None:
        return _SOURCE_POOL

    tier1: list[str] = []
    tier2: list[str] = []
    tier3: list[str] = []

    cache_path = ROOT / "thinking_news_cache.json"
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            for item in cached.get("headlines") or []:
                if isinstance(item, dict):
                    tier1.append(str(item.get("title") or ""))
                else:
                    tier1.append(str(item))
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    manual = ROOT / "thinking_news_manual.txt"
    if manual.is_file():
        try:
            for line in manual.read_text(encoding="utf-8").splitlines():
                if line.strip() and not line.startswith("#"):
                    tier2.append(line)
        except OSError:
            pass

    try:
        web_path = ROOT / config.WEB_SENTIMENT_CACHE_FILE
        if web_path.is_file():
            web = json.loads(web_path.read_text(encoding="utf-8"))
            blob = str(web.get("headline_text") or "")
            tier3.extend(_extract_financial_from_blob(blob))
    except (OSError, json.JSONDecodeError, TypeError):
        pass

    ordered = (
        _filter_titles(tier1)
        + _filter_titles(tier2)
        + _filter_titles(tier3)
        + list(_MACRO_EVENT_POOL)
        + list(_DEFAULT_POOL)
    )
    seen: set[str] = set()
    pool: list[str] = []
    for title in ordered:
        cleaned = clean_headline(title)
        if not is_financial_headline(cleaned):
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        pool.append(cleaned)

    _SOURCE_POOL = pool or list(_DEFAULT_POOL)
    return _SOURCE_POOL


def preload_headline_pool() -> int:
    """Warm headline pool once per backtest (cheap)."""
    return len(_load_source_headline_pool())


def _disk_cache_path(as_of: dt.date) -> Path:
    return cache_dir() / f"{as_of.isoformat()}.json"


def _load_disk_cache(as_of: dt.date) -> list[dict[str, Any]] | None:
    path = _disk_cache_path(as_of)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("headlines")
        if not isinstance(rows, list):
            return None
        cleaned_rows = _filter_headline_rows(rows)
        if cleaned_rows and len(cleaned_rows) != len(rows):
            _save_disk_cache(as_of, cleaned_rows)
        return cleaned_rows or None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _filter_headline_rows(
    rows: list[dict[str, Any]],
    *,
    min_score: int | None = None,
) -> list[dict[str, Any]]:
    threshold = _min_relevance(min_score)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        title = clean_headline(str(row.get("title") or ""))
        if not is_financial_headline(title):
            continue
        key = title.lower()
        if key in seen:
            continue
        sym = row.get("symbol")
        score = relevance_score(title, symbol=sym)
        if score < threshold:
            continue
        seen.add(key)
        category, _, _ = _best_category(key)
        out.append({**row, "title": title, "score": score, "category": category})
    out.sort(key=lambda r: (int(r.get("score") or 0), str(r.get("title") or "")), reverse=True)
    return out


def _save_disk_cache(as_of: dt.date, headlines: list[dict[str, Any]]) -> None:
    path = _disk_cache_path(as_of)
    try:
        filtered = _filter_headline_rows(headlines)
        path.write_text(
            json.dumps({"date": as_of.isoformat(), "headlines": filtered}, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


_QUARTER_BY_MONTH: dict[int, str] = {1: "Q4", 4: "Q1", 7: "Q2", 10: "Q3"}


def _date_context_headlines(as_of: dt.date, *, symbol: str | None = None) -> list[str]:
    """Date-natural macro/earnings framing so each backtest day feels distinct."""
    sym = config.normalize_symbol(symbol) if symbol else ""
    lines: list[str] = []
    weekday = as_of.weekday()
    quarter = _QUARTER_BY_MONTH.get(as_of.month)
    if quarter:
        lines.append(f"{quarter} earnings season ramps up as megacaps report")
    if weekday == 0:
        lines.append("Week ahead: Fed speakers and earnings steer risk appetite")
    elif weekday == 2:
        lines.append("Markets await Fed policy signal at midweek")
    elif weekday == 4:
        lines.append("Jobs data and month-end positioning drive index direction")
    if sym:
        tpl = _SYMBOL_EVENT_TEMPLATES[
            _pool_index(f"{as_of.isoformat()}:{sym}:evt", len(_SYMBOL_EVENT_TEMPLATES))
        ]
        lines.append(tpl.format(sym=sym))
    return lines


def _shifted_titles(as_of: dt.date, *, count: int = 5, symbol: str | None = None) -> list[str]:
    pool = _load_source_headline_pool()
    if not pool:
        return _filter_titles(_date_context_headlines(as_of, symbol=symbol), limit=count)
    sym = config.normalize_symbol(symbol) if symbol else ""
    seed = f"{as_of.isoformat()}:{sym}"

    # Lead with date-natural context so headlines feel current for each day.
    out: list[str] = list(_date_context_headlines(as_of, symbol=sym))

    start = _pool_index(seed, len(pool))
    scanned = 0
    idx = start
    target = max(count, count + 2)
    while len(out) < target and scanned < len(pool) * 2:
        title = pool[idx % len(pool)]
        if sym and sym not in title.upper() and sym in _SYMBOL_TEMPLATES:
            tpl = _SYMBOL_TEMPLATES[sym]
            alt = tpl[_pool_index(f"{seed}:tpl:{len(out)}", len(tpl))]
            if len(out) % 2 == 0:
                title = alt
        if is_financial_headline(title):
            if sym and sym not in title.upper():
                if sym in _SYMBOL_TEMPLATES and len(out) % 2 == 0:
                    out.append(clean_headline(title))
            else:
                out.append(clean_headline(title))
        idx += 1
        scanned += 1
    # Rank by relevance so the highest-signal headlines survive the count cap.
    ranked = sorted(
        {clean_headline(t) for t in out if is_financial_headline(t)},
        key=lambda t: relevance_score(t, symbol=sym),
        reverse=True,
    )
    return ranked[:count]


def _synthetic_regime_headlines(
    as_of: dt.date,
    *,
    symbol: str | None = None,
    regime: str | None = None,
    vol: str | None = None,
) -> list[str]:
    sym = config.normalize_symbol(symbol) if symbol else ""
    if sym and sym in _SYMBOL_TEMPLATES:
        return _filter_titles(list(_SYMBOL_TEMPLATES[sym]), limit=4)
    reg = str(regime or "RHYME_C")
    vol_s = str(vol or "Medium")
    lines = [
        f"Fed and macro data in focus as equities trade {reg} regime",
        "S&P 500 sector rotation continues amid earnings season",
    ]
    if "RHYME_E" in reg or "RHYME_B" in reg:
        lines.append("Risk-off headlines dominate — defensives bid, growth sold")
    elif "RHYME_A" in reg or "RHYME_C" in reg:
        lines.append("Risk-on narrative: cyclicals and tech leadership intact")
    if vol_s.lower() not in ("low", ""):
        lines.append("VIX volatility spike stories drive hedging demand")
    lines.extend(_shifted_titles(as_of, count=2, symbol=sym))
    return _filter_titles(lines, limit=6)


def get_historical_headlines(
    date: str | dt.date | dt.datetime,
    symbol: str | None = None,
    days_back: int = 7,
) -> list[dict[str, Any]]:
    """Simulated or cached headlines for a backtest date (no network)."""
    as_of = _parse_date(date)
    sym = config.normalize_symbol(symbol) if symbol else None
    cache_key = f"{as_of.isoformat()}:{sym or '*'}:{days_back}"
    if cache_key in _DAY_CACHE:
        return list(_DAY_CACHE[cache_key])

    rows: list[dict[str, Any]] = []
    for offset in range(max(0, int(days_back)), -1, -1):
        day = as_of - dt.timedelta(days=offset)
        day_key = f"{day.isoformat()}:{sym or '*'}"
        if day_key in _DAY_CACHE:
            day_rows = _DAY_CACHE[day_key]
        else:
            disk = _load_disk_cache(day)
            if disk:
                day_rows = disk
            else:
                titles = _shifted_titles(day, count=4, symbol=sym)
                if not titles:
                    titles = _synthetic_regime_headlines(day, symbol=sym)
                day_rows = _rows_from_titles(
                    titles,
                    day,
                    source="shifted_cache" if titles else "synthetic",
                    symbol=sym,
                )
                if day_rows:
                    _save_disk_cache(day, day_rows)
            _DAY_CACHE[day_key] = day_rows
        if sym:
            for row in day_rows:
                title = str(row.get("title") or "")
                if sym in title.upper() or _symbol_in_headline_corpus(sym, title):
                    rows.append(dict(row))
        else:
            rows.extend(dict(r) for r in day_rows)

    if sym and not rows:
        day_rows = _rows_from_titles(
            _synthetic_regime_headlines(as_of, symbol=sym),
            as_of,
            source="synthetic",
            symbol=sym,
        )
        rows.extend(day_rows)

    rows = _filter_headline_rows(rows)
    _DAY_CACHE[cache_key] = rows
    return list(rows)


def get_high_relevance_headlines(
    date: str | dt.date | dt.datetime,
    symbol: str | None = None,
    days_back: int = 5,
    min_score: int = 55,
) -> list[dict[str, Any]]:
    """High-signal scored headlines for a backtest window, filtered aggressively.

    Returns rows sorted by descending relevance score, each with title/date/
    source/symbol/score/category. Uses per-date disk + memory caching for speed.
    """
    as_of = _parse_date(date)
    sym = config.normalize_symbol(symbol) if symbol else None
    threshold = max(int(min_score), _min_relevance())
    cache_key = f"hi:{as_of.isoformat()}:{sym or '*'}:{days_back}:{threshold}"
    if cache_key in _DAY_CACHE:
        return list(_DAY_CACHE[cache_key])

    rows = get_historical_headlines(as_of, symbol=sym, days_back=days_back)
    high = _filter_headline_rows(rows, min_score=threshold)
    _DAY_CACHE[cache_key] = high
    return list(high)


def _symbol_in_headline_corpus(symbol: str, text: str) -> bool:
    sym = config.normalize_symbol(symbol)
    if not sym or not text:
        return False
    return bool(re.search(rf"\b{re.escape(sym)}\b", text.upper()))


def headlines_to_text(headlines: list[dict[str, Any]] | list[str] | None) -> str:
    if not headlines:
        return ""
    lines: list[str] = []
    for item in headlines:
        if isinstance(item, dict):
            title = clean_headline(str(item.get("title") or ""))
        else:
            title = clean_headline(str(item))
        if title and is_financial_headline(title):
            lines.append(title)
    return "\n".join(lines[:12])


def build_backtest_news_digest(
    data,
    regime: str,
    vol: str,
    bar_date: str | dt.date | dt.datetime,
) -> dict[str, Any]:
    """Headline digest for one simulation bar (thinking + catalyst)."""
    as_of = _parse_date(bar_date)
    rows = get_historical_headlines(as_of, days_back=7)
    titles = [str(r.get("title") or "") for r in rows if r.get("title")]
    if not titles:
        from modules.thinking_news import synthesize_backtest_news

        return synthesize_backtest_news(data, regime, vol, slot="premarket")
    from modules.thinking_news import build_news_digest

    return build_news_digest(titles[:8], slot="premarket")


def set_backtest_news_context(
    as_of_date: str | dt.date | dt.datetime,
    digest: dict[str, Any] | None = None,
    *,
    regime: str | None = None,
    vol: str | None = None,
) -> None:
    as_of = _parse_date(as_of_date)
    headlines = get_historical_headlines(as_of, days_back=7)
    _BACKTEST_CTX.clear()
    _BACKTEST_CTX.update(
        {
            "as_of_date": as_of.isoformat(),
            "headlines": headlines,
            "digest": digest or {},
            "regime": regime,
            "vol": vol,
        }
    )


def clear_backtest_news_context() -> None:
    _BACKTEST_CTX.clear()


def get_backtest_news_context() -> dict[str, Any]:
    return dict(_BACKTEST_CTX)


def backtest_headlines_for_summary() -> str | list[str] | None:
    ctx = get_backtest_news_context()
    digest = ctx.get("digest") or {}
    if digest.get("headlines"):
        return _filter_titles([str(x) for x in digest["headlines"]])
    text = headlines_to_text(ctx.get("headlines") or [])
    return text or None


def get_backtest_headline_corpus() -> str:
    ctx = get_backtest_news_context()
    parts: list[str] = []
    digest = ctx.get("digest") or {}
    if digest.get("digest_text"):
        parts.append(str(digest["digest_text"]))
    text = headlines_to_text(ctx.get("headlines") or [])
    if text:
        parts.append(text)
    return "\n".join(parts).lower()


def headline_momentum_boosts(*, max_boost: float = 0.12) -> dict[str, float]:
    """Map tickers mentioned in backtest headlines to small momentum boosts."""
    ctx = get_backtest_news_context()
    boosts: dict[str, float] = {}
    corpus_parts: list[str] = []
    for row in ctx.get("headlines") or []:
        corpus_parts.append(str(row.get("title") or ""))
    digest = ctx.get("digest") or {}
    for line in digest.get("headlines") or []:
        corpus_parts.append(str(line))
    corpus = " ".join(corpus_parts).upper()
    if not corpus.strip():
        return boosts
    for match in _TICKER_RE.findall(corpus):
        sym = config.normalize_symbol(match)
        if len(sym) < 2 or sym in _TICKER_FALSE_POSITIVE:
            continue
        boosts[sym] = round(min(max_boost, boosts.get(sym, 0.0) + 0.04), 4)
    return boosts


def sample_headline_titles(*, limit: int = 6) -> list[str]:
    ctx = get_backtest_news_context()
    raw: list[str] = []
    for row in ctx.get("headlines") or []:
        raw.append(str(row.get("title") or ""))
    digest = ctx.get("digest") or {}
    for line in digest.get("headlines") or []:
        raw.append(str(line))
    return _filter_titles(raw, limit=limit)
