"""Read-only market-data adapter for Lab Next.

GET requests are limited to https://data.alpaca.markets. This module does not
import an order executor and does not read or write a production database.
"""
from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .calendar import ET, bar_starts, first_open_after, session_bounds
from .engine import BLOCKED_ACTIONS, SUPPORTED_ACTIONS

DATA_HOST = "https://data.alpaca.markets"
FEED = "sip"
ADJUSTMENT = "raw"
ROOT = Path(__file__).resolve().parents[3]
ENV_PATH = ROOT / "data" / "portal" / "users" / "dawimberly" / "books" / "alpaca_paper" / ".env"
UNIVERSE_PATH = Path(__file__).resolve().parents[1] / "data" / "universe_forward_v1.json"
ACTION_TYPES = (
    "cash_dividend,stock_dividend,forward_split,reverse_split,unit_split,"
    "cash_merger,stock_merger,stock_and_cash_merger,spin_off,redemption,name_change,worthless_removal"
)
RAW_KIND = {
    "cash_dividends": "cash_dividend",
    "stock_dividends": "stock_dividend",
    "forward_splits": "split",
    "reverse_splits": "split",
    "unit_splits": "unit_split",
    "cash_mergers": "cash_merger",
    "stock_mergers": "stock_merger",
    "stock_and_cash_mergers": "stock_and_cash_merger",
    "spin_offs": "spin_off",
    "redemptions": "redemption",
    "name_changes": "name_change",
    "worthless_removals": "worthless_removal",
}


class DataError(RuntimeError):
    pass


def load_universe() -> dict:
    payload = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    if payload.get("label", "").lower().find("s&p membership") != -1 and "not historical" not in payload.get("label", "").lower():
        raise DataError("universe_labeled_as_historical_membership")
    if not payload.get("frozen_at") or not payload.get("tradable"):
        raise DataError("universe_not_frozen")
    return payload


def load_credentials() -> tuple[str, str]:
    if not ENV_PATH.is_file():
        raise DataError("credentials_missing")
    env: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    key = env.get("APCA_API_KEY_ID") or env.get("PAPER_APCA_API_KEY_ID") or ""
    secret = env.get("APCA_API_SECRET_KEY") or env.get("PAPER_APCA_API_SECRET_KEY") or ""
    if not key or not secret:
        raise DataError("credentials_missing")
    return key, secret


def headers(key: str, secret: str) -> dict[str, str]:
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}


def data_get(url: str, headers_map: dict[str, str], transport=None):
    if not url.startswith(DATA_HOST + "/"):
        raise DataError("data_host_only")
    retrieved = datetime.now(ET).isoformat(timespec="seconds")
    if transport is not None:
        status, payload = transport(url, headers_map)
        return int(status), payload, retrieved
    request = urllib.request.Request(url, headers=headers_map, method="GET")
    if request.get_method() != "GET":
        raise DataError("blocked_non_get")
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            body = response.read()
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        try:
            payload = json.loads(exc.read().decode("utf-8", errors="replace") or "null")
        except json.JSONDecodeError:
            payload = None
        return status, payload, retrieved
    try:
        return status, json.loads(body.decode("utf-8")), retrieved
    except json.JSONDecodeError as exc:
        raise DataError("invalid_json") from exc


def _pages(url: str, headers_map: dict[str, str], transport=None, limit: int = 20):
    pages = []
    token = None
    for _ in range(limit):
        page_url = url if not token else f"{url}&page_token={urllib.parse.quote(token)}"
        status, payload, retrieved = data_get(page_url, headers_map, transport)
        if status != 200 or not isinstance(payload, dict):
            raise DataError(f"http_{status}")
        pages.append({"retrieved_at": retrieved, "payload": payload})
        token = payload.get("next_page_token")
        if not token:
            return pages, True
    return pages, False


def fetch_bars(headers_map: dict[str, str], symbols: list[str], start: datetime, end: datetime, transport=None):
    query = urllib.parse.urlencode(
        {
            "symbols": ",".join(symbols),
            "timeframe": "5Min",
            "start": start.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z"),
            "end": end.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z"),
            "adjustment": ADJUSTMENT,
            "feed": FEED,
            "limit": "10000",
        }
    )
    return _pages(DATA_HOST + "/v2/stocks/bars?" + query, headers_map, transport)


def fetch_actions(headers_map: dict[str, str], symbols: list[str], start: str, end: str, transport=None):
    query = urllib.parse.urlencode(
        {
            "symbols": ",".join(symbols),
            "start": start,
            "end": end,
            "limit": "1000",
            "types": ACTION_TYPES,
        }
    )
    return _pages(DATA_HOST + "/v1/corporate-actions?" + query, headers_map, transport)


def merge_bars(pages: list[dict]) -> dict[str, list[dict]]:
    merged: dict[str, list[dict]] = {}
    for page in pages:
        bars = (page.get("payload") or {}).get("bars") or {}
        if not isinstance(bars, dict):
            raise DataError("bars_payload_unreadable")
        for symbol, rows in bars.items():
            if isinstance(rows, list):
                merged.setdefault(symbol, []).extend(row for row in rows if isinstance(row, dict))
    return merged


def _bar_start(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise DataError("bar_timestamp_needs_timezone")
    parsed = parsed.astimezone(ET).replace(second=0, microsecond=0)
    if parsed.minute % 5:
        raise DataError("bar_not_on_five_minute_grid")
    return parsed


def regular_bars(merged: dict[str, list[dict]], day: str, symbols: list[str]):
    """Keep completed regular-session bars. The 16:00 stamp is not a session bar."""
    expected = bar_starts(day)
    expected_set = set(expected)
    found = {symbol: {} for symbol in symbols}
    extras = []
    for symbol, rows in merged.items():
        if symbol not in found:
            continue
        for row in rows:
            start = _bar_start(row["t"])
            if start.date().isoformat() != day:
                continue
            clock = start.strftime("%H:%M")
            if clock not in expected_set:
                extras.append({"symbol": symbol, "clock": clock})
                continue
            open_, high, low, close = (row.get("o"), row.get("h"), row.get("l"), row.get("c"))
            values = (open_, high, low, close)
            if not all(isinstance(value, (int, float)) and value > 0 for value in values):
                raise DataError(f"invalid_ohlc_{symbol}")
            if high < max(open_, low, close) or low > min(open_, high, close):
                raise DataError(f"invalid_ohlc_range_{symbol}")
            found[symbol][clock] = {
                "bar_start": start.isoformat(),
                "bar_end": (start + timedelta(minutes=5)).isoformat(),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": row.get("v") if isinstance(row.get("v"), (int, float)) else 0,
            }
    gaps = {symbol: [clock for clock in expected if clock not in rows] for symbol, rows in found.items()}
    gaps = {symbol: missing for symbol, missing in gaps.items() if missing}
    return found, gaps, extras


def execution_class(bar_start: datetime, received_at: datetime, newest: datetime) -> str:
    """A closed-market backfill is collection only.

    During the session, only the newest completed bar can be executable, and
    only when its first eligible open is still after receipt.
    """
    if bar_start != newest:
        return "collect_only"
    _start, end = session_bounds(bar_start.date().isoformat())
    if received_at >= end:
        return "collect_only"
    if received_at >= first_open_after(received_at):
        return "collect_only"
    return "executable"


def _symbol(item: dict) -> str | None:
    for key in ("symbol", "acquiree_symbol"):
        if item.get(key):
            return str(item[key])
    return None


def _ratio(item: dict):
    old, new = item.get("old_rate"), item.get("new_rate")
    if isinstance(old, (int, float)) and isinstance(new, (int, float)) and old > 0 and new > 0:
        return new / old
    return None


def normalize_actions(pages: list[dict]) -> tuple[list[dict], bool]:
    events = []
    unscoped = False
    for page in pages:
        body = (page.get("payload") or {}).get("corporate_actions") or {}
        if not isinstance(body, dict):
            return [], True
        for key, items in body.items():
            if not isinstance(items, list):
                unscoped = True
                continue
            kind = RAW_KIND.get(key)
            if kind is None:
                unscoped = True
                continue
            for item in items:
                if not isinstance(item, dict):
                    unscoped = True
                    continue
                symbol = _symbol(item)
                ex_date = str(item.get("ex_date") or item.get("effective_date") or "")[:10]
                if not symbol or len(ex_date) != 10:
                    unscoped = True
                    continue
                event = {
                    "type": kind,
                    "symbol": symbol,
                    "ex_date": ex_date,
                    "payable_date": str(item.get("payable_date") or "")[:10] or None,
                }
                if kind == "cash_dividend":
                    rate = item.get("rate")
                    if not isinstance(rate, (int, float)) or rate <= 0:
                        event["type"] = "cash_dividend_unparsed"
                    else:
                        event["amount"] = rate
                elif kind in {"split", "stock_dividend"}:
                    ratio = _ratio(item)
                    if ratio is None:
                        event["type"] = "split_unparsed" if kind == "split" else "stock_dividend_unparsed"
                    else:
                        event["ratio"] = ratio
                events.append(event)
    return events, unscoped


def session_audit(events: list[dict], symbols: list[str], day: str, pages_complete: bool, unscoped: bool) -> dict:
    by_symbol: dict[str, dict] = {}
    for event in events:
        if event["symbol"] not in symbols or event["ex_date"] != day:
            continue
        if event["symbol"] in by_symbol or (
            event["type"] not in SUPPORTED_ACTIONS and event["type"] not in BLOCKED_ACTIONS
        ):
            by_symbol[event["symbol"]] = {"type": "split_unparsed", "symbol": event["symbol"]}
            continue
        if event["type"] == "cash_dividend":
            by_symbol[event["symbol"]] = {
                "type": "cash_dividend",
                "symbol": event["symbol"],
                "amount": event["amount"],
                "payable_date": event.get("payable_date"),
            }
        elif event["type"] in {"split", "stock_dividend"}:
            by_symbol[event["symbol"]] = {
                "type": event["type"],
                "symbol": event["symbol"],
                "ratio": event["ratio"],
            }
        else:
            by_symbol[event["symbol"]] = {"type": event["type"], "symbol": event["symbol"]}
    return {
        "source": "alpaca_data_v1_corporate_actions",
        "independent": True,
        "coverage_from": day,
        "coverage_through": day,
        "symbols": list(symbols),
        "events": list(by_symbol.values()),
        "pages_complete": bool(pages_complete) and not unscoped,
    }


def digest(payload) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
