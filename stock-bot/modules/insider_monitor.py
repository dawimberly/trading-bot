"""Lightweight SEC EDGAR RSS insider & filings monitor - paper bot only."""

from __future__ import annotations

import datetime
import logging
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Any

import config
from modules.safe_io import read_json_file, write_json_file

logger = logging.getLogger(__name__)

_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}

SEC_FEEDS: dict[str, str] = {
    "form4": (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4"
        "&company=&dateb=&owner=include&count=100&output=atom"
    ),
    "13d": (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+13D"
        "&company=&dateb=&owner=include&count=40&output=atom"
    ),
    "13g": (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+13G"
        "&company=&dateb=&owner=include&count=40&output=atom"
    ),
    "13f": (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=13F-HR"
        "&company=&dateb=&owner=include&count=20&output=atom"
    ),
    "shelf": (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=S-3"
        "&company=&dateb=&owner=include&count=30&output=atom"
    ),
    "dilution": (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=424B5"
        "&company=&dateb=&owner=include&count=20&output=atom"
    ),
}

_EXEC_KEYWORDS = (
    "chief",
    "ceo",
    "cfo",
    "coo",
    "president",
    "director",
    "officer",
    "executive",
    "vp ",
    "evp",
)
_CEO_KEYWORDS = ("chief executive", " ceo", "ceo ", "chief executive officer")
_BUY_WORDS = ("purchase", "acquired", "buy", " code p", "transaction code: p")
_SELL_WORDS = ("sale", "sold", "disposed", " code s", "transaction code: s")
_DILUTION_WORDS = ("at-the-market", "atm", "prospectus supplement", "offering", "dilution")

_SHELF_MIN_VALUE_USD = float(getattr(config, "INSIDER_SHELF_MIN_VALUE_USD", 50_000_000))
_DEFAULT_MIN_SCORE = int(getattr(config, "INSIDER_DEFAULT_MIN_SCORE", 60))

_CACHE: dict[str, Any] = {"signals": None, "loaded_at": None}


def _sig_type(sig: dict[str, Any]) -> str:
    return str(sig.get("signal_type") or sig.get("event_type") or "")


def _state_path():
    raw = config.INSIDER_MONITOR_STATE_FILE
    from pathlib import Path

    p = Path(raw)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / raw
    return p


def _fetch_rss(url: str) -> str | None:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": config.SEC_USER_AGENT, "Accept": "application/atom+xml"},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("SEC RSS fetch failed (%s): %s", url[:80], exc)
        return None


def _parse_atom_entries(xml_text: str) -> list[dict[str, str]]:
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("SEC RSS parse error: %s", exc)
        return []
    out: list[dict[str, str]] = []
    for entry in root.findall("a:entry", _ATOM_NS):
        title = (entry.findtext("a:title", default="", namespaces=_ATOM_NS) or "").strip()
        summary = (entry.findtext("a:summary", default="", namespaces=_ATOM_NS) or "").strip()
        updated = (entry.findtext("a:updated", default="", namespaces=_ATOM_NS) or "").strip()
        link = ""
        link_el = entry.find("a:link", _ATOM_NS)
        if link_el is not None:
            link = link_el.get("href") or ""
        out.append({"title": title, "summary": summary, "updated": updated, "link": link})
    return out


def _company_name_from_title(title: str) -> str:
    m = re.match(r"^[0-9A-Z./\s-]+\s*-\s*(.+?)\s*\(\d{10}\)", title, re.I)
    if m:
        return m.group(1).strip()
    m = re.match(r"^4\s*-\s*(.+?)\s*\(", title, re.I)
    if m:
        return m.group(1).strip()
    return title.split(" - ", 1)[-1].strip() if " - " in title else title.strip()


def _ticker_from_text(text: str) -> str | None:
    patterns = (
        r"issuer\s+trading\s+symbol[:\s]+([A-Z]{1,5})\b",
        r"trading\s+symbol[:\s]+([A-Z]{1,5})\b",
        r"\b([A-Z]{1,5})\s+common\s+stock\b",
        r"\(([A-Z]{1,5})\)\s*(?:common|class|$)",
    )
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            sym = config.normalize_symbol(m.group(1))
            if sym not in ("ISSUER", "FILER", "FILED", "CLASS", "STOCK"):
                return sym
    m = re.search(r"\(([A-Z]{1,5})\)\s*$", text.strip())
    if m:
        sym = config.normalize_symbol(m.group(1))
        if sym not in ("ISSUER", "FILER", "FILED"):
            return sym
    return None


def _parse_dollar_amount(text: str) -> float | None:
    if not text:
        return None
    patterns = (
        r"\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?)\s*(million|billion|m|b)?",
        r"([0-9]+(?:\.[0-9]+)?)\s*(million|billion)\s+(?:dollars|usd|offering|aggregate)",
        r"aggregate\s+offering\s+price[:\s]+\$?\s*([0-9,]+(?:\.[0-9]+)?)",
        r"maximum\s+amount[:\s]+\$?\s*([0-9,]+(?:\.[0-9]+)?)",
    )
    best: float | None = None
    for pat in patterns:
        for m in re.finditer(pat, text, re.I):
            raw = m.group(1).replace(",", "")
            try:
                val = float(raw)
            except ValueError:
                continue
            mult_word = (m.group(2) or "").lower() if m.lastindex and m.lastindex >= 2 else ""
            if mult_word in ("million", "m"):
                val *= 1_000_000
            elif mult_word in ("billion", "b"):
                val *= 1_000_000_000
            if best is None or val > best:
                best = val
    return best


def _parse_stake_percent(text: str) -> float | None:
    patterns = (
        r"percent\s+of\s+class[:\s</b>]*([0-9]+(?:\.[0-9]+)?)\s*%",
        r"([0-9]+(?:\.[0-9]+)?)\s*%\s+of\s+(?:the\s+)?class",
        r"ownership[:\s]+([0-9]+(?:\.[0-9]+)?)\s*%",
    )
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return None


def _company_ticker_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    try:
        from modules.dynamic_universe import load_screener_ticker_meta

        for sym, meta in (load_screener_ticker_meta() or {}).items():
            name = str(meta.get("name") or meta.get("company") or "").strip().upper()
            if len(name) >= 4:
                mapping[name] = config.normalize_symbol(sym)
    except Exception:
        pass
    return mapping


def _resolve_ticker(title: str, summary: str) -> tuple[str | None, str]:
    blob = f"{title} {summary}"
    ticker = _ticker_from_text(blob)
    company = _company_name_from_title(title)
    if ticker:
        return ticker, company
    name_key = company.upper()
    if len(name_key) < 4:
        return None, company
    cmap = _company_ticker_map()
    if name_key in cmap:
        return cmap[name_key], company
    for key, sym in cmap.items():
        if len(key) < 6:
            continue
        if name_key == key or name_key.startswith(key) or key.startswith(name_key):
            return sym, company
    return None, company


def _accession_from_summary(summary: str) -> str:
    m = re.search(r"AccNo:</b>\s*([0-9-]+)", summary, re.I)
    return m.group(1).strip() if m else ""


def _cik_from_title(title: str) -> str:
    m = re.search(r"\((\d{10})\)", title)
    return m.group(1).lstrip("0") if m else ""


def _form4_role(title: str) -> str:
    if "(Issuer)" in title:
        return "issuer"
    if "(Reporting" in title:
        return "reporting"
    return "other"


def _detect_insider_role(xml_text: str, reporter_names: list[str]) -> str:
    low = xml_text.lower()
    blob = " ".join(reporter_names).lower()
    if any(k in low or k in blob for k in _CEO_KEYWORDS):
        return "ceo"
    if "chief financial" in low or " cfo" in low or "cfo" in blob:
        return "cfo"
    if "<isofficer>1</isofficer>" in low or any(k in low for k in _EXEC_KEYWORDS):
        return "executive"
    if "<isdirector>1</isdirector>" in low or "director" in blob:
        return "director"
    return "insider"


def _extract_form4_value_usd(xml_text: str) -> float | None:
    values: list[float] = []
    for block in re.findall(r"<nonDerivativeTransaction>.*?</nonDerivativeTransaction>", xml_text, re.S | re.I):
        shares_m = re.search(
            r"<transactionShares>.*?<value>\s*([0-9,.]+)\s*</value>",
            block,
            re.S | re.I,
        )
        price_m = re.search(
            r"<transactionPricePerShare>.*?<value>\s*([0-9,.]+)\s*</value>",
            block,
            re.S | re.I,
        )
        if shares_m and price_m:
            try:
                shares = float(shares_m.group(1).replace(",", ""))
                price = float(price_m.group(1).replace(",", ""))
                if shares > 0 and price > 0:
                    values.append(shares * price)
            except ValueError:
                pass
    if values:
        return sum(values)
    direct = re.findall(
        r"<transactionValue>.*?<value>\s*([0-9,.]+)\s*</value>",
        xml_text,
        re.S | re.I,
    )
    parsed = []
    for v in direct:
        try:
            parsed.append(float(v.replace(",", "")))
        except ValueError:
            pass
    return sum(parsed) if parsed else None


def _fetch_form4_details(accession: str, issuer_cik: str, *, cache: dict[str, dict]) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "side": None,
        "ticker": None,
        "is_executive": False,
        "value_usd": None,
        "role": "insider",
    }
    if not accession or not issuer_cik:
        return empty
    if accession in cache:
        return dict(cache[accession])
    details = dict(empty)
    acc_flat = accession.replace("-", "")
    index_url = (
        f"https://www.sec.gov/Archives/edgar/data/{issuer_cik}/{acc_flat}/{accession}-index.htm"
    )
    try:
        req = urllib.request.Request(
            index_url,
            headers={"User-Agent": config.SEC_USER_AGENT, "Accept": "text/html"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            index_html = resp.read().decode("utf-8", errors="replace")
        xml_href = None
        for m in re.finditer(r'href="([^"]+\.xml)"', index_html, re.I):
            href = m.group(1)
            if "/xsl" in href.lower():
                continue
            xml_href = href
            break
        if not xml_href:
            m = re.search(r'href="([^"]+\.xml)"', index_html, re.I)
            xml_href = m.group(1) if m else None
        if not xml_href:
            cache[accession] = details
            return details
        if xml_href.startswith("/"):
            xml_url = f"https://www.sec.gov{xml_href}"
        else:
            xml_url = f"https://www.sec.gov/Archives/edgar/data/{issuer_cik}/{acc_flat}/{xml_href}"
        req2 = urllib.request.Request(
            xml_url,
            headers={"User-Agent": config.SEC_USER_AGENT, "Accept": "application/xml"},
        )
        with urllib.request.urlopen(req2, timeout=30) as resp2:
            xml_text = resp2.read().decode("utf-8", errors="replace")
        sym_m = re.search(
            r"<issuerTradingSymbol>\s*([A-Z0-9.-]{1,8})\s*</issuerTradingSymbol>",
            xml_text,
            re.I,
        )
        if sym_m:
            details["ticker"] = config.normalize_symbol(sym_m.group(1))
        codes = re.findall(r"<transactionCode>\s*([A-Z])\s*</transactionCode>", xml_text, re.I)
        ad_codes = re.findall(
            r"<transactionAcquiredDisposedCode>\s*([AD])\s*</transactionAcquiredDisposedCode>",
            xml_text,
            re.I,
        )
        buy_codes = {"P", "M"}
        sell_codes = {"S", "F"}
        if any(c.upper() in buy_codes for c in codes) or any(c.upper() == "A" for c in ad_codes):
            details["side"] = "buy"
        elif any(c.upper() in sell_codes for c in codes) or any(c.upper() == "D" for c in ad_codes):
            details["side"] = "sell"
        elif any(c.upper() == "A" for c in codes):
            details["side"] = "buy"
        rel = xml_text.lower()
        details["is_executive"] = any(
            tag in rel
            for tag in (
                "<isofficer>1</isofficer>",
                "<isdirector>1</isdirector>",
                "chief",
                "president",
                "ceo",
                "cfo",
            )
        )
        details["value_usd"] = _extract_form4_value_usd(xml_text)
        details["role"] = _detect_insider_role(xml_text, [])
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.debug("Form 4 detail fetch failed %s: %s", accession, exc)
    if details.get("side") or details.get("ticker"):
        cache[accession] = details
    return dict(details)


def _parse_date(raw: str) -> datetime.date | None:
    if not raw:
        return None
    try:
        return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return datetime.datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _is_executive(text: str) -> bool:
    low = text.lower()
    return any(k in low for k in _EXEC_KEYWORDS)


def _format_value(value: float | None) -> str:
    if value is None or value <= 0:
        return ""
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:,.0f}"


def _normalize_signal(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a clean signal dict for downstream consumers."""
    value = raw.get("value")
    if value is None:
        value = raw.get("value_usd")
    try:
        value_num = float(value) if value is not None else None
    except (TypeError, ValueError):
        value_num = None
    signal_type = str(raw.get("signal_type") or raw.get("event_type") or "unknown")
    ticker = raw.get("ticker")
    if ticker:
        ticker = config.normalize_symbol(str(ticker))
    return {
        "ticker": ticker,
        "signal_type": signal_type,
        "severity": str(raw.get("severity") or "medium"),
        "description": str(raw.get("description") or ""),
        "value": value_num,
        "score": int(raw.get("score") or 0),
        "filing_date": raw.get("filing_date"),
        "company": raw.get("company"),
        "insiders_count": int(raw.get("insiders_count") or 0),
        "role": raw.get("role"),
        "stake_pct": raw.get("stake_pct"),
        "link": raw.get("link") or "",
    }


def _score_signal(sig: dict[str, Any], *, shelf_repeat: bool = False) -> int:
    st = _sig_type(sig)
    score = {
        "cluster_buy": 74,
        "executive_sell": 70,
        "insider_sell": 42,
        "insider_buy": 38,
        "activist_13d": 86,
        "stake_13g": 68,
        "13f_filing": 25,
        "dilution": 64,
        "shelf_offering": 20,
    }.get(st, 35)

    insiders = int(sig.get("insiders_count") or 0)
    if st == "cluster_buy":
        score += min(20, max(0, insiders - 2) * 7)

    role = str(sig.get("role") or "").lower()
    if st == "executive_sell":
        if role == "ceo":
            score += 14
        elif role in ("cfo", "executive"):
            score += 8

    value = sig.get("value")
    try:
        val = float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        val = 0.0
    if val >= 10_000_000:
        score += 15
    elif val >= 1_000_000:
        score += 10
    elif val >= 250_000:
        score += 5

    stake = sig.get("stake_pct")
    try:
        pct = float(stake) if stake is not None else 0.0
    except (TypeError, ValueError):
        pct = 0.0
    if st in ("activist_13d", "stake_13g") and pct >= 5:
        score += min(12, int(pct))

    if st == "shelf_offering":
        if val >= _SHELF_MIN_VALUE_USD:
            score = max(score, 62)
        elif shelf_repeat:
            score = max(score, 58)
        else:
            score = min(score, 40)

    if st == "13f_filing":
        score = min(score, 35)

    return max(0, min(100, score))


def _severity_from_score(score: int, signal_type: str) -> str:
    if signal_type in ("cluster_buy", "activist_13d", "executive_sell") and score >= 70:
        return "high"
    if score >= 75:
        return "high"
    if score >= 55:
        return "medium"
    return "low"


def _parse_form4_entries(
    entries: list[dict[str, str]],
    *,
    side_cache: dict[str, dict],
    max_detail_fetches: int = 25,
) -> list[dict[str, Any]]:
    by_acc: dict[str, dict[str, Any]] = {}
    for entry in entries:
        title = entry.get("title") or ""
        if not title.upper().startswith("4"):
            continue
        acc = _accession_from_summary(entry.get("summary") or "")
        if not acc:
            continue
        role = _form4_role(title)
        bucket = by_acc.setdefault(
            acc,
            {
                "accession": acc,
                "issuer_cik": "",
                "issuer_company": "",
                "reporters": [],
                "filing_date": _parse_date(entry.get("updated") or ""),
                "link": entry.get("link") or "",
            },
        )
        if role == "issuer":
            bucket["issuer_company"] = _company_name_from_title(title)
            bucket["issuer_cik"] = _cik_from_title(title)
            if entry.get("link"):
                bucket["link"] = entry["link"]
        elif role == "reporting":
            bucket["reporters"].append(_company_name_from_title(title))

    events: list[dict[str, Any]] = []
    fetches = 0
    for acc, meta in by_acc.items():
        if not meta.get("issuer_company"):
            continue
        details: dict[str, Any] = {
            "side": None,
            "ticker": None,
            "is_executive": False,
            "value_usd": None,
            "role": "insider",
        }
        if fetches < max_detail_fetches:
            details = _fetch_form4_details(acc, meta.get("issuer_cik") or "", cache=side_cache)
            fetches += 1
        side = details.get("side")
        ticker = details.get("ticker")
        if not ticker:
            ticker, _ = _resolve_ticker(
                f"4 - {meta['issuer_company']} ({meta.get('issuer_cik', '')}) (Issuer)",
                "",
            )
        fdate = meta.get("filing_date")
        reporter_n = len(meta.get("reporters") or [])
        reporters = meta.get("reporters") or []
        exec_sell = bool(details.get("is_executive")) or any(_is_executive(r) for r in reporters)
        role = details.get("role") or "insider"
        if not details.get("role") or details.get("role") == "insider":
            role = _detect_insider_role("", reporters)
        value = details.get("value_usd")
        val_s = _format_value(value)

        if side == "buy":
            desc = f"Insider purchase - {meta['issuer_company']}"
            if val_s:
                desc += f" ({val_s})"
            events.append(
                {
                    "ticker": ticker,
                    "company": meta["issuer_company"],
                    "signal_type": "insider_buy",
                    "severity": "medium",
                    "description": desc,
                    "value": value,
                    "insiders_count": max(1, reporter_n),
                    "role": role,
                    "filing_date": fdate.isoformat() if fdate else None,
                    "link": meta.get("link") or "",
                }
            )
        elif side == "sell":
            signal_type = "executive_sell" if exec_sell else "insider_sell"
            role_label = role.upper() if role in ("ceo", "cfo") else "executive" if exec_sell else "insider"
            desc = f"{role_label} sale - {meta['issuer_company']}"
            if val_s:
                desc += f" ({val_s})"
            events.append(
                {
                    "ticker": ticker,
                    "company": meta["issuer_company"],
                    "signal_type": signal_type,
                    "severity": "high" if exec_sell else "medium",
                    "description": desc,
                    "value": value,
                    "insiders_count": max(1, reporter_n),
                    "role": role,
                    "filing_date": fdate.isoformat() if fdate else None,
                    "link": meta.get("link") or "",
                }
            )
    return events


def _classify_filing(entry: dict[str, str], *, feed: str) -> dict[str, Any] | None:
    title = entry.get("title") or ""
    summary = entry.get("summary") or ""
    blob = f"{title} {summary}"
    blob_low = blob.lower()
    ticker, company = _resolve_ticker(title, summary)
    fdate = _parse_date(entry.get("updated") or "")
    value = _parse_dollar_amount(blob)
    val_s = _format_value(value)
    stake_pct = _parse_stake_percent(blob)

    if feed == "13d":
        desc = f"Schedule 13D activist stake - {company or title[:80]}"
        if stake_pct:
            desc += f" ({stake_pct:.1f}% of class)"
        if val_s:
            desc += f" [{val_s}]"
        return {
            "ticker": ticker,
            "company": company,
            "signal_type": "activist_13d",
            "severity": "high",
            "description": desc,
            "value": value,
            "stake_pct": stake_pct,
            "insiders_count": 0,
            "filing_date": fdate.isoformat() if fdate else None,
            "link": entry.get("link") or "",
        }
    if feed == "13g":
        if stake_pct is not None and stake_pct < 5:
            return None
        desc = f"Schedule 13G large stake - {company or title[:80]}"
        if stake_pct:
            desc += f" ({stake_pct:.1f}% of class)"
        return {
            "ticker": ticker,
            "company": company,
            "signal_type": "stake_13g",
            "severity": "medium",
            "description": desc,
            "value": value,
            "stake_pct": stake_pct,
            "insiders_count": 0,
            "filing_date": fdate.isoformat() if fdate else None,
            "link": entry.get("link") or "",
        }
    if feed == "13f":
        if stake_pct is None and value is None:
            return None
        desc = f"13F institutional holdings change - {company or title[:80]}"
        if stake_pct:
            desc += f" ({stake_pct:.1f}%)"
        return {
            "ticker": ticker,
            "company": company,
            "signal_type": "13f_filing",
            "severity": "low",
            "description": desc,
            "value": value,
            "stake_pct": stake_pct,
            "insiders_count": 0,
            "filing_date": fdate.isoformat() if fdate else None,
            "link": entry.get("link") or "",
        }
    if feed == "shelf":
        desc = f"Shelf registration - {company or title[:80]}"
        if val_s:
            desc += f" ({val_s})"
        return {
            "ticker": ticker,
            "company": company,
            "signal_type": "shelf_offering",
            "severity": "low",
            "description": desc,
            "value": value,
            "insiders_count": 0,
            "filing_date": fdate.isoformat() if fdate else None,
            "link": entry.get("link") or "",
        }
    if feed == "dilution":
        sev = "high" if any(w in blob_low for w in _DILUTION_WORDS) else "medium"
        desc = f"Prospectus / offering - {company or title[:80]}"
        if val_s:
            desc += f" ({val_s})"
        return {
            "ticker": ticker,
            "company": company,
            "signal_type": "dilution",
            "severity": sev,
            "description": desc,
            "value": value,
            "insiders_count": 0,
            "filing_date": fdate.isoformat() if fdate else None,
            "link": entry.get("link") or "",
        }
    return None


def _aggregate_cluster_buys(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    min_buyers = int(config.INSIDER_CLUSTER_MIN_BUYERS)
    buys_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ev in events:
        if _sig_type(ev) != "insider_buy":
            continue
        key = ev.get("ticker") or (ev.get("company") or "").upper()
        if not key:
            continue
        buys_by_key[key].append(ev)

    clusters: list[dict[str, Any]] = []
    for key, group in buys_by_key.items():
        if len(group) < min_buyers:
            continue
        ticker = group[0].get("ticker")
        company = group[0].get("company") or key
        total_value = sum(float(g.get("value") or 0) for g in group)
        val_s = _format_value(total_value if total_value > 0 else None)
        desc = f"Cluster buy: {len(group)} insiders purchased {company}"
        if val_s:
            desc += f" ({val_s} total)"
        clusters.append(
            {
                "ticker": ticker,
                "company": company,
                "signal_type": "cluster_buy",
                "severity": "high",
                "description": desc,
                "value": total_value if total_value > 0 else None,
                "insiders_count": len(group),
                "filing_date": max((g.get("filing_date") or "") for g in group),
                "link": group[0].get("link") or "",
            }
        )
    return clusters


def _shelf_repeat_keys(events: list[dict[str, Any]]) -> set[str]:
    shelves: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ev in events:
        if _sig_type(ev) != "shelf_offering":
            continue
        key = ev.get("ticker") or (ev.get("company") or "").upper()
        if key:
            shelves[key].append(ev)
    return {k for k, group in shelves.items() if len(group) >= 2}


def _merge_signals(raw_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clusters = _aggregate_cluster_buys(raw_events)
    cluster_keys = {(c.get("ticker") or "", (c.get("company") or "").upper()) for c in clusters}
    non_cluster_buys = [
        e
        for e in raw_events
        if _sig_type(e) != "insider_buy"
        or (e.get("ticker"), (e.get("company") or "").upper()) not in cluster_keys
    ]
    merged = clusters + non_cluster_buys
    shelf_repeats = _shelf_repeat_keys(merged)

    scored: list[dict[str, Any]] = []
    for sig in merged:
        key = sig.get("ticker") or (sig.get("company") or "").upper()
        shelf_repeat = _sig_type(sig) == "shelf_offering" and key in shelf_repeats
        score = _score_signal(sig, shelf_repeat=shelf_repeat)
        st = _sig_type(sig)
        if st == "shelf_offering":
            val = float(sig.get("value") or 0)
            if val < _SHELF_MIN_VALUE_USD and not shelf_repeat:
                continue
        out = dict(sig)
        out["score"] = score
        out["severity"] = _severity_from_score(score, st)
        scored.append(_normalize_signal(out))

    scored.sort(key=lambda s: (-int(s.get("score") or 0), str(s.get("filing_date") or "")))
    return scored


def _load_state() -> dict[str, Any]:
    return read_json_file(_state_path()) or {}


def _save_state(payload: dict[str, Any]) -> None:
    write_json_file(_state_path(), payload)


def _cache_fresh(state: dict[str, Any]) -> bool:
    ts = state.get("fetched_at")
    if not ts:
        return False
    try:
        fetched = datetime.datetime.fromisoformat(str(ts))
    except ValueError:
        return False
    age_h = (datetime.datetime.now() - fetched).total_seconds() / 3600.0
    return age_h < float(config.INSIDER_MONITOR_POLL_HOURS)


def refresh_insider_signals(*, force: bool = False) -> list[dict[str, Any]]:
    """Poll SEC RSS feeds and refresh cached signals (all scores, unfiltered)."""
    if not config.effective_insider_monitor_enabled():
        return []

    state = _load_state()
    if not force and _cache_fresh(state):
        signals = state.get("signals") or []
        _CACHE["signals"] = signals
        _CACHE["loaded_at"] = state.get("fetched_at")
        return list(signals)

    raw_events: list[dict[str, Any]] = []
    errors: list[str] = []
    side_cache: dict[str, dict] = (
        {}
        if force
        else {k: v for k, v in (state.get("form4_side_cache") or {}).items() if isinstance(v, dict)}
    )

    xml4 = _fetch_rss(SEC_FEEDS["form4"])
    if xml4:
        form4_entries = _parse_atom_entries(xml4)
        raw_events.extend(_parse_form4_entries(form4_entries, side_cache=side_cache))
    else:
        errors.append("form4")

    for feed in ("13d", "13g", "13f", "shelf", "dilution"):
        xml = _fetch_rss(SEC_FEEDS[feed])
        if not xml:
            errors.append(feed)
            continue
        for entry in _parse_atom_entries(xml):
            ev = _classify_filing(entry, feed=feed)
            if ev:
                raw_events.append(ev)

    if not raw_events and state.get("signals"):
        logger.warning("Insider monitor refresh empty; keeping prior cache")
        return list(state.get("signals") or [])

    signals = _merge_signals(raw_events)
    payload = {
        "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "signals": signals,
        "raw_count": len(raw_events),
        "errors": errors,
        "form4_side_cache": side_cache,
    }
    _save_state(payload)
    _CACHE["signals"] = signals
    _CACHE["loaded_at"] = payload["fetched_at"]
    return list(signals)


def get_recent_insider_signals(
    *,
    days: int | None = None,
    min_score: int | None = None,
    max_age_days: int | None = None,
) -> list[dict[str, Any]]:
    """Return scored insider/filing signals filtered by lookback and minimum quality."""
    if not config.effective_insider_monitor_enabled():
        return []
    lookback = days if days is not None else (
        max_age_days if max_age_days is not None else config.INSIDER_MONITOR_LOOKBACK_DAYS
    )
    floor = _DEFAULT_MIN_SCORE if min_score is None else int(min_score)
    cutoff = datetime.date.today() - datetime.timedelta(days=lookback)
    signals = refresh_insider_signals()
    out: list[dict[str, Any]] = []
    for sig in signals:
        fdate = _parse_date(str(sig.get("filing_date") or ""))
        if fdate is not None and fdate < cutoff:
            continue
        if int(sig.get("score") or 0) < floor:
            continue
        out.append(_normalize_signal(sig))
    return out


def get_cluster_buy_signals(*, min_insiders: int = 3, days: int = 7) -> list[dict[str, Any]]:
    """Cluster-buy signals with ticker and minimum insider count."""
    out: list[dict[str, Any]] = []
    for sig in get_recent_insider_signals(days=days, min_score=50):
        if _sig_type(sig) != "cluster_buy":
            continue
        if int(sig.get("insiders_count") or 0) < int(min_insiders):
            continue
        if not sig.get("ticker"):
            continue
        out.append(_normalize_signal(sig))
    out.sort(key=lambda s: (-int(s.get("score") or 0), -int(s.get("insiders_count") or 0)))
    return out


def get_executive_sell_signals(*, min_value: float = 100_000, days: int = 7) -> list[dict[str, Any]]:
    """CEO/CFO/executive sell signals above a minimum dollar value when known."""
    out: list[dict[str, Any]] = []
    for sig in get_recent_insider_signals(days=days, min_score=50):
        if _sig_type(sig) != "executive_sell":
            continue
        role = str(sig.get("role") or "").lower()
        if role not in ("ceo", "cfo", "executive"):
            continue
        if not sig.get("ticker"):
            continue
        try:
            val = float(sig.get("value") or 0)
        except (TypeError, ValueError):
            val = 0.0
        if min_value > 0 and val > 0 and val < float(min_value):
            continue
        out.append(_normalize_signal(sig))
    out.sort(key=lambda s: (-int(s.get("score") or 0), -(float(s.get("value") or 0))))
    return out


def _cluster_buy_tickers(signals: list[dict[str, Any]] | None = None) -> set[str]:
    sigs = signals if signals is not None else get_recent_insider_signals(min_score=50)
    out: set[str] = set()
    for sig in sigs:
        if _sig_type(sig) != "cluster_buy":
            continue
        t = sig.get("ticker")
        if t:
            out.add(config.normalize_symbol(str(t)))
    return out


def _executive_sell_tickers(signals: list[dict[str, Any]] | None = None) -> set[str]:
    sigs = signals if signals is not None else get_recent_insider_signals(min_score=50)
    out: set[str] = set()
    for sig in sigs:
        if _sig_type(sig) not in ("executive_sell", "insider_sell"):
            continue
        t = sig.get("ticker")
        if t:
            out.add(config.normalize_symbol(str(t)))
    return out


def _ticker_in_strong_sector(ticker: str) -> bool:
    try:
        from modules.dynamic_universe import sector_for_symbol
        from modules.sector_screener import load_sector_screener_snapshot

        snap = load_sector_screener_snapshot() or {}
        active = set(snap.get("active_sectors") or [])
        if not active:
            return False
        sec = sector_for_symbol(ticker)
        return sec in active
    except Exception:
        return False


def momentum_rank_boost(symbol: str) -> float:
    """Extra sort weight for NYSE momentum (cluster buy in strong sector)."""
    if config.effective_insider_signal_boost_enabled():
        try:
            from modules.insider_signal_handler import momentum_rank_boost as handler_boost

            return handler_boost(symbol)
        except Exception:
            pass
    if not config.effective_insider_monitor_enabled():
        return 0.0
    sym = config.normalize_symbol(symbol)
    if sym not in _cluster_buy_tickers():
        return 0.0
    if _ticker_in_strong_sector(sym):
        return 0.15
    return 0.08


def stat_arb_long_boost(symbol: str) -> float:
    """Score multiplier for stat-arb long leg on cluster buys."""
    if config.effective_insider_signal_boost_enabled():
        try:
            from modules.insider_signal_handler import stat_arb_long_boost as handler_boost

            return handler_boost(symbol)
        except Exception:
            pass
    if not config.effective_insider_monitor_enabled():
        return 1.0
    sym = config.normalize_symbol(symbol)
    if sym in _cluster_buy_tickers():
        return 1.12 if _ticker_in_strong_sector(sym) else 1.06
    return 1.0


def short_candidate_boost(symbol: str, bubble_score: float) -> float:
    """Sort boost for protective shorts on insider selling + bubble."""
    if config.effective_insider_signal_boost_enabled():
        try:
            from modules.insider_signal_handler import short_candidate_boost as handler_boost

            return handler_boost(symbol, bubble_score)
        except Exception:
            pass
    if not config.effective_insider_monitor_enabled():
        return 0.0
    if float(bubble_score) < config.SHORT_BUBBLE_SCORE_MIN:
        return 0.0
    sym = config.normalize_symbol(symbol)
    sigs = get_recent_insider_signals(min_score=50)
    for sig in sigs:
        if config.normalize_symbol(str(sig.get("ticker") or "")) != sym:
            continue
        st = _sig_type(sig)
        if st in ("executive_sell", "insider_sell", "dilution", "shelf_offering"):
            if st == "executive_sell":
                return 0.25
            return 0.12
    return 0.0


def get_insider_context_for_thinking() -> dict[str, Any]:
    """Compact block for thinking_engine / Kimi prompts."""
    if config.effective_insider_signal_boost_enabled():
        try:
            from modules.insider_signal_handler import get_thinking_context

            return get_thinking_context()
        except Exception:
            pass
    signals = get_recent_insider_signals(days=7, min_score=55)[:8]
    clusters = [s for s in signals if _sig_type(s) == "cluster_buy"]
    sells = [s for s in signals if _sig_type(s) in ("executive_sell", "insider_sell")]
    stakes = [s for s in signals if _sig_type(s) in ("activist_13d", "stake_13g")]
    parts: list[str] = []
    if clusters:
        names = ", ".join(
            f"{s.get('ticker') or s.get('company', '?')} "
            f"({s.get('insiders_count', 0)}, score {s.get('score', 0)})"
            for s in clusters[:4]
        )
        parts.append(f"cluster buys: {names}")
    if sells:
        top = sells[0]
        parts.append(
            f"insider sells: {len(sells)} "
            f"(top {top.get('ticker') or top.get('company', '?')} score {top.get('score', 0)})"
        )
    if stakes:
        parts.append(f"13D/13G stakes: {len(stakes)}")
    summary = "; ".join(parts) if parts else "no high-signal filings this week"
    return {
        "insider_signals": signals,
        "insider_summary": summary,
        "insider_cluster_count": len(clusters),
    }


def format_weekly_insider_section(*, limit: int = 6) -> list[str]:
    """Markdown lines for weekly report."""
    if not config.effective_insider_monitor_enabled():
        return ["- Insider monitor: OFF"]
    signals = get_recent_insider_signals(days=7, min_score=55)[:limit]
    if not signals:
        return ["- No high-signal insider/filing events this week."]
    lines = ["**Top insider & filing signals:**"]
    for sig in signals:
        tk = sig.get("ticker") or sig.get("company") or "?"
        val_s = _format_value(sig.get("value"))
        extra = f" | {val_s}" if val_s else ""
        lines.append(
            f"- **{sig.get('signal_type', 'event')}** {tk} "
            f"(score {sig.get('score', 0)}{extra}): "
            f"{sig.get('description', '')[:100]}"
        )
    return lines


def format_insider_monitor_banner() -> str | None:
    if not config.effective_insider_monitor_enabled():
        return ">>> Insider Monitor: OFF"
    signals = get_recent_insider_signals(days=7, min_score=55)
    clusters = len([s for s in signals if _sig_type(s) == "cluster_buy"])
    quality = len(signals)
    line = f">>> Insider Monitor: ON | Clusters: {clusters} | Quality signals: {quality}"
    if config.effective_insider_signal_boost_enabled():
        try:
            from modules.insider_signal_handler import get_boost_snapshot

            state = get_boost_snapshot()
            if state.get("enabled"):
                line += (
                    f" | Boost: {int(state.get('cluster_count') or 0)} clusters, "
                    f"{int(state.get('short_count') or 0)} shorts"
                )
        except Exception:
            pass
    return line


def format_telegram_insider_lines(*, limit: int = 3) -> list[str]:
    if not config.effective_insider_monitor_enabled():
        return []
    if config.effective_insider_signal_boost_enabled():
        try:
            from modules.insider_signal_handler import format_telegram_top_signals

            return format_telegram_top_signals(limit=limit)
        except Exception:
            pass
    signals = get_recent_insider_signals(days=7, min_score=55)[:limit]
    if not signals:
        return ["Insider: none this week"]
    lines = ["Insider signals:"]
    for sig in signals:
        tk = sig.get("ticker") or sig.get("company") or "?"
        val_s = _format_value(sig.get("value"))
        val_part = f" {val_s}" if val_s else ""
        lines.append(f"  {sig.get('signal_type')}: {tk} (s{sig.get('score', 0)}{val_part})")
    return lines
