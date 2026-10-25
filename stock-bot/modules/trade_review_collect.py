"""GET-only collector for purchase plans and 30-session sale follow-through.

Does not import the engine, portal launcher, telegram, or trading-safety modules.
Does not submit, cancel, or replace orders. Credential values stay in memory.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from modules.post_sale_review import group_fills, post_sale_report, render_markdown

NY = ZoneInfo("America/New_York")
BOOKS = ("alpaca_live", "alpaca_paper", "alpaca_paper_v2")
EXPECTED_PAPER = {
    "alpaca_live": False,
    "alpaca_paper": True,
    "alpaca_paper_v2": True,
}
DATA_HOST = "https://data.alpaca.markets"
POSITION_CHANGING = {
    "forward_splits": ("ex_date", "effective_date"),
    "reverse_splits": ("ex_date", "effective_date"),
    "unit_splits": ("ex_date", "effective_date"),
    "stock_splits": ("ex_date", "effective_date"),
    "cash_mergers": ("effective_date", "ex_date"),
    "stock_mergers": ("effective_date", "ex_date"),
    "stock_and_cash_mergers": ("effective_date", "ex_date"),
    "spin_offs": ("ex_date", "effective_date"),
    "spin_off": ("ex_date", "effective_date"),
    "redemptions": ("ex_date", "process_date"),
    "name_changes": ("ex_date", "effective_date"),
    "worthless_removals": ("ex_date", "process_date"),
    "stock_dividends": ("ex_date", "payable_date"),
}
CASH_DIVIDENDS = {"cash_dividends", "cash_dividend"}
DATE_FIELDS = ("ex_date", "effective_date", "process_date", "payable_date")


class CollectionError(RuntimeError):
    pass


def parse_env_file(path: Path) -> dict[str, str]:
    """Read one book env file without writing os.environ."""
    out: dict[str, str] = {}
    text = path.read_text(encoding="utf-8-sig")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def discover_book_env(root: Path, book: str) -> list[Path]:
    users = root / "data" / "portal" / "users"
    found: list[Path] = []
    if not users.is_dir():
        return found
    for user_dir in users.iterdir():
        if not user_dir.is_dir():
            continue
        env = user_dir / "books" / book / ".env"
        if env.is_file():
            found.append(env)
    return found


def _paper_flag(env: dict[str, str]) -> bool:
    return env.get("PAPER_TRADING", "").strip().lower() in {"1", "true", "yes"}


def trading_base(book: str, env: dict[str, str]) -> str:
    if book not in EXPECTED_PAPER:
        raise CollectionError("unknown_book")
    if _paper_flag(env) != EXPECTED_PAPER[book]:
        raise CollectionError("book_env_paper_flag_mismatch")
    if EXPECTED_PAPER[book]:
        return "https://paper-api.alpaca.markets"
    return "https://api.alpaca.markets"


def credentials(env: dict[str, str]) -> tuple[str, str]:
    key = env.get("APCA_API_KEY_ID") or env.get("PAPER_APCA_API_KEY_ID") or ""
    secret = env.get("APCA_API_SECRET_KEY") or env.get("PAPER_APCA_API_SECRET_KEY") or ""
    if not key or not secret:
        raise CollectionError("credentials_missing")
    return key, secret


def http_get_json(url: str, headers: dict[str, str], *, timeout: float = 45):
    """Perform one GET. Refuses any other method."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    if request.get_method() != "GET":
        raise CollectionError("blocked_non_get")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            body = response.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        try:
            payload = json.loads(exc.read().decode("utf-8", errors="replace") or "null")
        except json.JSONDecodeError:
            payload = None
        code = payload.get("code") if isinstance(payload, dict) else None
        return status, payload, None if code is None else str(code)
    if status != 200:
        return status, None, None
    try:
        return status, json.loads(body.decode("utf-8")), None
    except json.JSONDecodeError as exc:
        raise CollectionError("invalid_json") from exc


def _signature(row: dict) -> tuple:
    return (
        row.get("order_id"),
        row.get("symbol"),
        str(row.get("side", "")).lower(),
        str(row.get("qty")),
        str(row.get("price")),
        row.get("transaction_time"),
    )


def paginate_activities(fetch, *, page_size: int = 100, max_pages: int = 30) -> dict:
    """Walk broker activity pages. A short page ends a complete history.

    ``fetch(page_token)`` returns a list. Hitting ``max_pages`` on a full page
    is truncation, not a complete account history.
    """
    seen: dict[str, tuple] = {}
    rows: list[dict] = []
    token = None
    pages = 0
    error = None
    conflicts: list[str] = []
    truncated = False
    while pages < max_pages:
        pages += 1
        try:
            page = fetch(token)
        except Exception as exc:
            error = type(exc).__name__ if not isinstance(exc, CollectionError) else str(exc)
            break
        if not isinstance(page, list):
            error = "activity_page_not_a_list"
            break
        if not page:
            break
        for row in page:
            if not isinstance(row, dict):
                error = "activity_row_not_object"
                break
            ident = row.get("id")
            if not ident:
                error = "missing_execution_id"
                break
            ident = str(ident)
            signature = _signature(row)
            if ident in seen:
                if seen[ident] != signature:
                    conflicts.append(ident)
                    error = "conflicting_duplicate_execution"
                    break
                continue
            seen[ident] = signature
            rows.append(row)
        if error:
            break
        if len(page) < page_size:
            break
        nxt = page[-1].get("id") if isinstance(page[-1], dict) else None
        if not nxt or str(nxt) == token:
            error = "pagination_token_stuck"
            break
        token = str(nxt)
    else:
        truncated = True
    return {
        "rows": rows,
        "pages": pages,
        "truncated": truncated,
        "error": error,
        "conflicts": conflicts,
        "complete_from_start": error is None and not truncated,
    }


def merge_executions(existing: list[dict], incoming: list[dict]) -> tuple[list[dict], list[str]]:
    """Keep the first copy of an execution id. Conflicts do not overwrite it."""
    by_id: dict[str, dict] = {}
    order: list[dict] = []
    conflicts: list[str] = []
    for row in list(existing) + list(incoming):
        ident = str(row.get("id") or "")
        if not ident:
            continue
        if ident in by_id:
            if _signature(by_id[ident]) != _signature(row):
                conflicts.append(ident)
            continue
        by_id[ident] = row
        order.append(row)
    return order, conflicts


def account_fingerprint(account: dict) -> dict[str, str]:
    ident = str(account.get("id") or "")
    if not ident:
        raise CollectionError("account_id_missing")
    number = str(account.get("account_number") or "")
    return {
        "account_sha256": hashlib.sha256(ident.encode("utf-8")).hexdigest(),
        "account_last4": number[-4:] if len(number) >= 4 else "",
    }


def is_equity_symbol(symbol: str) -> bool:
    text = str(symbol or "")
    if not text or "/" in text or "-" in text:
        return False
    upper = text.upper()
    if upper.endswith("USD") and upper != "USD":
        return False
    return True


def bar_session_date(timestamp: str) -> str:
    """Map a daily bar timestamp onto an exchange session date.

    Midnight UTC timestamps keep that UTC date. Other timestamps use the
    America/New_York date, which matches Alpaca's 04:00Z/05:00Z daily bars.
    """
    dt = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("bar timestamp requires a timezone")
    utc = dt.astimezone(timezone.utc)
    if utc.hour == 0 and utc.minute == 0 and utc.second == 0:
        return utc.date().isoformat()
    return utc.astimezone(NY).date().isoformat()


def calendar_rows(payload: list) -> list[dict]:
    rows = []
    for item in payload:
        day = str(item["date"])
        close_text = str(item.get("close") or "")
        hour, minute = close_text.split(":")
        session = datetime.combine(
            datetime.fromisoformat(day).date(),
            time(int(hour), int(minute)),
            tzinfo=NY,
        )
        rows.append(
            {
                "date": day,
                "close": session.isoformat(),
                "session_close_et": close_text,
                "source": "alpaca_calendar",
            }
        )
    return rows


def _dates_in(item: dict) -> list[str]:
    found = []
    for field in DATE_FIELDS:
        value = item.get(field)
        if value:
            found.append(str(value)[:10])
    return found


def classify_corporate_actions(payload) -> dict | None:
    """Return dated actions, or None when the payload cannot be read."""
    if not isinstance(payload, dict):
        return None
    body = payload.get("corporate_actions", payload)
    if not isinstance(body, dict):
        return None
    changing: list[str] = []
    uncertain: list[str] = []
    dividends: list[str] = []
    for key, items in body.items():
        if not isinstance(items, list):
            continue
        if key in CASH_DIVIDENDS:
            for item in items:
                if isinstance(item, dict):
                    dividends.extend(_dates_in(item)[:1])
            continue
        if key not in POSITION_CHANGING:
            for item in items:
                if isinstance(item, dict) and _dates_in(item):
                    uncertain.extend(_dates_in(item)[:1])
            continue
        preferred = POSITION_CHANGING[key]
        for item in items:
            if not isinstance(item, dict):
                return None
            dates = _dates_in(item)
            if not dates:
                return None
            chosen = next((item.get(name) for name in preferred if item.get(name)), None)
            if chosen is None:
                uncertain.append(dates[0])
            else:
                changing.append(str(chosen)[:10])
                extras = [d for d in dates if d != str(chosen)[:10]]
                if extras:
                    uncertain.extend(extras)
    return {
        "position_changing_action_dates": sorted(set(changing)),
        "uncertain_action_dates": sorted(set(uncertain)),
        "dividend_dates_not_in_price": sorted(set(dividends)),
    }


def price_block(
    *,
    bars: list[dict],
    adjustment: str,
    pages_complete: bool,
    actions_complete: bool,
    actions_classified: dict | None,
    checked_from: str,
    checked_through: str,
    feed: str,
) -> dict:
    """Build a price contract. Flags are set only from request evidence."""
    units_ok = adjustment == "raw" and pages_complete is True
    block = {
        "basis": "raw_same_units_as_broker" if units_ok else "unverified",
        "unit_alignment_verified": units_ok,
        "unit_evidence": {
            "source": "alpaca_data_v2_stocks_bars",
            "adjustment_requested": adjustment,
            "pages_complete": pages_complete,
            "feed": feed,
            "session_date_convention": "utc_midnight_date_else_eastern_date",
        },
        "corporate_action_evidence": {
            "source": "alpaca_data_v1_corporate_actions",
            "pages_complete": actions_complete,
            "classified": actions_classified is not None,
        },
        "bars": bars,
    }
    if actions_complete and actions_classified is not None:
        block["corporate_actions_checked_from"] = checked_from
        block["corporate_actions_checked_through"] = checked_through
        block["position_changing_action_dates"] = list(
            actions_classified["position_changing_action_dates"]
        )
        block["uncertain_action_dates"] = list(actions_classified["uncertain_action_dates"])
        block["dividend_dates_not_in_price"] = list(
            actions_classified["dividend_dates_not_in_price"]
        )
        block["corporate_action_evidence"]["raw_position_changes"] = len(
            actions_classified["position_changing_action_dates"]
        )
        block["corporate_action_evidence"]["raw_dividends"] = len(
            actions_classified["dividend_dates_not_in_price"]
        )
    return block


def enforce_price_evidence(prices: dict) -> dict:
    """Drop verified flags that do not carry the request evidence."""
    cleaned = {}
    for symbol, raw in prices.items():
        block = dict(raw)
        evidence = block.get("unit_evidence") or {}
        if block.get("unit_alignment_verified") is True:
            if evidence.get("adjustment_requested") != "raw" or evidence.get("pages_complete") is not True:
                block["unit_alignment_verified"] = False
                block["basis"] = "unverified"
        action_evidence = block.get("corporate_action_evidence") or {}
        if "position_changing_action_dates" in block and action_evidence.get("pages_complete") is not True:
            block.pop("position_changing_action_dates", None)
            block.pop("corporate_actions_checked_from", None)
            block.pop("corporate_actions_checked_through", None)
            block.pop("uncertain_action_dates", None)
        cleaned[symbol] = block
    return cleaned


UNSOLD_SALE_FOLLOW_THROUGH = "not applicable—no sale yet."


def starting_inventory(fill_history_complete: bool, coverage: dict | None):
    """Share counts stay unknown until fills and corporate actions are both resolved.

    A position-changing action is not applied to the share count. Its presence
    leaves inventory unknown rather than a fill-only reconstruction.
    """
    if not fill_history_complete:
        return None, "unknown", "fill_history_incomplete"
    if (
        not coverage
        or coverage.get("complete") is not True
        or coverage.get("unscoped")
        or coverage.get("uncertain_dates")
        or coverage.get("position_changing_dates")
    ):
        detail = (
            "position_changing_corporate_action_not_applied"
            if coverage and coverage.get("position_changing_dates")
            else "corporate_actions_unresolved"
        )
        return None, "unknown", detail
    return {}, "flat_at_start_of_complete_fill_history", "no_unresolved_position_changing_actions"


def annotate_unsold_positions(report: dict, positions, *, positions_error: str | None = None) -> dict:
    """A current holding is not a sale. Existing sale rows are left as they are."""
    if positions_error or not isinstance(positions, list):
        report["open_positions"] = []
        report["open_position_note"] = "current positions unavailable"
        return report
    notes = []
    for row in positions:
        if not isinstance(row, dict) or not row.get("symbol"):
            continue
        try:
            qty = float(row.get("qty"))
        except (TypeError, ValueError):
            notes.append(
                {
                    "symbol": str(row.get("symbol")),
                    "quantity": None,
                    "avg_entry_price": None,
                    "sale_follow_through": "unavailable",
                    "original_plan": None,
                }
            )
            continue
        if qty == 0:
            continue
        notes.append(
            {
                "symbol": str(row["symbol"]),
                "quantity": str(row.get("qty")),
                "avg_entry_price": (
                    None if row.get("avg_entry_price") is None else str(row.get("avg_entry_price"))
                ),
                "sale_follow_through": UNSOLD_SALE_FOLLOW_THROUGH,
                "original_plan": None,
            }
        )
    notes.sort(key=lambda item: item["symbol"])
    report["open_positions"] = notes
    report["open_position_note"] = None
    return report


def latest_buy_snapshots(book: str, groups: list[dict], positions: list[dict], observed_at: str) -> list[dict]:
    """Attach a current broker average only to a symbol's latest buy with no later fill."""
    latest: dict[str, dict] = {}
    for group in groups:
        latest[group["symbol"]] = group
    held = {}
    for row in positions:
        if isinstance(row, dict) and row.get("symbol"):
            held[str(row["symbol"])] = row
    snaps = []
    for symbol, group in latest.items():
        if group["side"] != "buy" or symbol not in held:
            continue
        position = held[symbol]
        snaps.append(
            {
                "book": book,
                "symbol": symbol,
                "after_order_id": group["order_id"],
                "source": "alpaca_positions",
                "observed_at": observed_at,
                "qty": str(position.get("qty")),
                "avg_entry_price": str(position.get("avg_entry_price")),
            }
        )
    return snaps


def journal_attribution(path: Path) -> dict[str, dict]:
    """Order-id reasons from the journal. Prices in the journal are not fills."""
    if not path.is_file():
        return {}
    out: dict[str, dict] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            order_id = str(row.get("order_id") or "").strip()
            if not order_id:
                continue
            out[order_id] = {
                "source": "journal",
                "event": row.get("event") or "",
                "pair_key": row.get("pair_key") or "",
                "sleeve": row.get("sleeve") or "",
                "journal_price_ignored": True,
            }
    return out


def load_plans(directory: Path, book: str) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    if not directory.is_dir():
        return rows, skipped
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if isinstance(row, dict) and row.get("book") == book:
                rows.append(row)
    return rows, skipped


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _atomic_json(path: Path, payload) -> None:
    _atomic_text(path, json.dumps(payload, indent=2, allow_nan=False, default=str))


def _headers(key: str, secret: str) -> dict[str, str]:
    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "Accept": "application/json",
    }


def _get(transport, url: str, headers: dict[str, str]):
    status, payload, code = transport(url, headers)
    if status != 200:
        raise CollectionError(f"http_{status}" + (f"_{code}" if code else ""))
    return payload


def _paginate_token_object(transport, url: str, headers: dict[str, str], *, max_pages: int = 10):
    pages = []
    token = None
    complete = False
    for _ in range(max_pages):
        joiner = "&" if "?" in url else "?"
        page_url = url if not token else f"{url}{joiner}page_token={urllib.parse.quote(token)}"
        payload = _get(transport, page_url, headers)
        pages.append(payload)
        token = payload.get("next_page_token") if isinstance(payload, dict) else None
        if not token:
            complete = True
            break
    return pages, complete


def _bars_for_symbol(transport, headers, symbol: str, start: str, end: str):
    attempts = []
    for feed in ("iex", ""):
        query = {
            "symbols": symbol,
            "timeframe": "1Day",
            "start": start,
            "end": end,
            "adjustment": "raw",
            "limit": "10000",
        }
        if feed:
            query["feed"] = feed
        url = DATA_HOST + "/v2/stocks/bars?" + urllib.parse.urlencode(query)
        try:
            pages, complete = _paginate_token_object(transport, url, headers)
        except CollectionError as exc:
            attempts.append({"feed": feed or "default", "error": str(exc)})
            continue
        bars = []
        for page in pages:
            for row in (page.get("bars") or {}).get(symbol, []) if isinstance(page, dict) else []:
                bars.append(row)
        if bars or complete:
            return bars, complete, feed or "default", attempts
        attempts.append({"feed": feed or "default", "error": "empty"})
    return [], False, None, attempts


def _actions_for_symbols(transport, headers, symbols: list[str], start: str, end: str):
    if not symbols:
        return {}, True
    query = {
        "symbols": ",".join(symbols),
        "start": start,
        "end": end,
        "limit": "1000",
        "types": ",".join(
            [
                "cash_dividend",
                "stock_dividend",
                "forward_split",
                "reverse_split",
                "unit_split",
                "cash_merger",
                "stock_merger",
                "stock_and_cash_merger",
                "spin_off",
                "redemption",
                "name_change",
                "worthless_removal",
            ]
        ),
    }
    url = DATA_HOST + "/v1/corporate-actions?" + urllib.parse.urlencode(query)
    try:
        pages, complete = _paginate_token_object(transport, url, headers)
    except CollectionError:
        return None, False
    merged: dict[str, list] = {}
    for page in pages:
        body = page.get("corporate_actions", page) if isinstance(page, dict) else {}
        if not isinstance(body, dict):
            return None, False
        for key, items in body.items():
            if isinstance(items, list):
                merged.setdefault(key, []).extend(items)
    return {"corporate_actions": merged}, complete


def _payload_unscoped(payload: dict) -> bool:
    body = (payload or {}).get("corporate_actions") or {}
    if not isinstance(body, dict):
        return True
    for items in body.values():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or _action_symbols(item) is None:
                return True
    return False


def inventory_action_coverage(
    transport,
    headers,
    symbols: list[str],
    start: str,
    end: str,
    *,
    batch_size: int = 40,
) -> dict:
    """Corporate-action coverage for every equity symbol in the fill history."""
    if not symbols:
        return {
            "complete": True,
            "unscoped": False,
            "position_changing_dates": [],
            "uncertain_dates": [],
        }
    changing: list[str] = []
    uncertain: list[str] = []
    for offset in range(0, len(symbols), batch_size):
        payload, complete = _actions_for_symbols(
            transport, headers, symbols[offset : offset + batch_size], start, end
        )
        if payload is None or not complete or _payload_unscoped(payload):
            return {
                "complete": False,
                "unscoped": True,
                "position_changing_dates": [],
                "uncertain_dates": [],
            }
        classified = classify_corporate_actions(payload)
        if classified is None:
            return {
                "complete": False,
                "unscoped": True,
                "position_changing_dates": [],
                "uncertain_dates": [],
            }
        changing.extend(classified["position_changing_action_dates"])
        uncertain.extend(classified["uncertain_action_dates"])
    return {
        "complete": True,
        "unscoped": False,
        "position_changing_dates": sorted(set(changing)),
        "uncertain_dates": sorted(set(uncertain)),
    }


def _execution_notes(raw_rows: list[dict]) -> dict[str, dict]:
    notes: dict[str, dict] = {}
    for row in raw_rows:
        order_id = str(row.get("order_id") or "")
        if not order_id:
            continue
        slot = notes.setdefault(
            order_id,
            {
                "execution_ids": [],
                "broker_order_status_last": None,
                "leaves_qty_last": None,
                "order_return_is_not_a_fill": True,
            },
        )
        slot["execution_ids"].append(row.get("id"))
        if row.get("order_status") is not None:
            slot["broker_order_status_last"] = str(row.get("order_status"))
        if row.get("leaves_qty") is not None:
            slot["leaves_qty_last"] = str(row.get("leaves_qty"))
    return notes


def _store_dir(root: Path, book: str) -> Path:
    return root / "data" / "trade_review" / book


def _report_paths(root: Path, book: str) -> tuple[Path, Path]:
    folder = root / "reports" / "trade_review"
    return folder / f"{book}.json", folder / f"{book}.md"


def refresh_book(
    *,
    book: str,
    env: dict[str, str],
    root: Path,
    transport,
    journal_path: Path | None = None,
    as_of: datetime | None = None,
    max_pages: int = 30,
    page_size: int = 100,
    symbol_cap: int = 40,
) -> dict:
    """Refresh one isolated book. ``transport(url, headers) -> (status, payload, code)``."""
    if book not in BOOKS:
        raise CollectionError("unknown_book")
    key, secret = credentials(env)
    headers = _headers(key, secret)
    base = trading_base(book, env)
    stamp = as_of or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        raise CollectionError("as_of_requires_timezone")
    store = _store_dir(root, book)
    evidence = store / "evidence"
    fills_path = evidence / "fills.jsonl"
    state_path = store / "state.json"
    previous_state = {}
    if state_path.is_file():
        try:
            previous_state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous_state = {"error": "state_unreadable"}

    account = _get(transport, base + "/v2/account", headers)
    fingerprint = account_fingerprint(account if isinstance(account, dict) else {})
    if previous_state.get("account_sha256") and previous_state["account_sha256"] != fingerprint["account_sha256"]:
        summary = {
            "book": book,
            "error": "account_isolation_mismatch",
            "account_last4": fingerprint["account_last4"],
            "fills_added": 0,
        }
        _atomic_json(state_path, {**previous_state, "last_error": "account_isolation_mismatch"})
        _write_error_report(root, book, stamp, summary)
        return summary

    def fetch(token):
        query = {
            "activity_types": "FILL",
            "page_size": str(page_size),
            "direction": "desc",
        }
        if token:
            query["page_token"] = token
        url = base + "/v2/account/activities?" + urllib.parse.urlencode(query)
        payload = _get(transport, url, headers)
        if not isinstance(payload, list):
            raise CollectionError("activity_page_not_a_list")
        return payload

    page = paginate_activities(fetch, page_size=page_size, max_pages=max_pages)
    existing = _read_jsonl(fills_path)
    merged, merge_conflicts = merge_executions(existing, page["rows"])
    conflicts = list(page["conflicts"]) + merge_conflicts
    complete = page["complete_from_start"] and not conflicts
    added = len(merged) - len(existing)
    if added > 0 and not conflicts:
        evidence.mkdir(parents=True, exist_ok=True)
        known = {str(row.get("id")) for row in existing}
        with fills_path.open("a", encoding="utf-8") as handle:
            for row in merged:
                if str(row.get("id")) not in known:
                    handle.write(json.dumps(row, default=str, separators=(",", ":")) + "\n")
    elif not fills_path.exists() and merged and not conflicts:
        evidence.mkdir(parents=True, exist_ok=True)
        fills_path.write_text(
            "".join(json.dumps(row, default=str, separators=(",", ":")) + "\n" for row in merged),
            encoding="utf-8",
        )

    equity_rows = []
    non_equity = 0
    unclassified = 0
    for row in merged:
        if str(row.get("activity_type", "FILL")) != "FILL":
            continue
        side = str(row.get("side", "")).lower()
        if side not in {"buy", "sell"}:
            unclassified += 1
            continue
        if not is_equity_symbol(str(row.get("symbol") or "")):
            non_equity += 1
            continue
        equity_rows.append(row)

    sale_symbols: list[str] = []
    seen_symbols: set[str] = set()
    for row in sorted(equity_rows, key=lambda item: str(item.get("transaction_time") or ""), reverse=True):
        if str(row.get("side", "")).lower() != "sell":
            continue
        symbol = str(row["symbol"])
        if symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        sale_symbols.append(symbol)
    deferred = sale_symbols[symbol_cap:]
    sale_symbols = sale_symbols[:symbol_cap]

    positions_payload = []
    positions_error = None
    try:
        raw_positions = _get(transport, base + "/v2/positions", headers)
        if isinstance(raw_positions, list):
            positions_payload = raw_positions
        else:
            positions_error = "positions_not_a_list"
    except CollectionError as exc:
        positions_error = str(exc)

    prices: dict = {}
    price_errors: dict[str, str] = {}
    calendar_error = None
    calendar = []
    fill_days = []
    for row in equity_rows:
        try:
            when = datetime.fromisoformat(str(row["transaction_time"]).replace("Z", "+00:00"))
            fill_days.append(when.astimezone(NY).date())
        except (TypeError, ValueError):
            continue
    if fill_days:
        start_day = min(fill_days) - timedelta(days=7)
        end_day = stamp.astimezone(NY).date() + timedelta(days=90)
        try:
            payload = _get(
                transport,
                base
                + "/v2/calendar?"
                + urllib.parse.urlencode(
                    {"start": start_day.isoformat(), "end": end_day.isoformat()}
                ),
                headers,
            )
            calendar = calendar_rows(payload if isinstance(payload, list) else [])
        except (CollectionError, KeyError, ValueError) as exc:
            calendar_error = type(exc).__name__ if not isinstance(exc, CollectionError) else str(exc)
            calendar = []

    checked_from = (min(fill_days).isoformat() if fill_days else stamp.date().isoformat())
    checked_through = (stamp.astimezone(NY).date() + timedelta(days=90)).isoformat()
    actions_payload, actions_complete = _actions_for_symbols(
        transport, headers, sale_symbols, checked_from, checked_through
    )
    classified = classify_corporate_actions(actions_payload) if actions_payload else None
    if actions_payload is None:
        actions_complete = False
        classified = None

    for symbol in sale_symbols:
        try:
            raw_bars, bars_complete, feed, attempts = _bars_for_symbol(
                transport,
                headers,
                symbol,
                checked_from,
                stamp.astimezone(NY).date().isoformat(),
            )
        except CollectionError as exc:
            price_errors[symbol] = str(exc)
            continue
        if feed is None:
            price_errors[symbol] = "price_source_unavailable:" + ",".join(
                item.get("error", "") for item in attempts
            )
            continue
        normalized = []
        sessions = set()
        duplicate = False
        for row in raw_bars:
            try:
                session = bar_session_date(row["t"])
                close = row["c"]
                high = row.get("h", close)
                low = row.get("l", close)
            except (KeyError, TypeError, ValueError):
                duplicate = True
                break
            if session in sessions:
                duplicate = True
                break
            sessions.add(session)
            normalized.append({"date": session, "close": close, "high": high, "low": low})
        symbol_actions = classified
        if classified is not None:
            symbol_actions = _actions_for_symbol(actions_payload, symbol)
        if duplicate or not bars_complete:
            prices[symbol] = price_block(
                bars=[],
                adjustment="raw" if bars_complete and not duplicate else "unverified",
                pages_complete=False,
                actions_complete=False,
                actions_classified=None,
                checked_from=checked_from,
                checked_through=checked_through,
                feed=feed or "unverified",
            )
            prices[symbol]["unit_evidence"]["duplicate_or_incomplete"] = True
            prices[symbol]["unit_evidence"]["attempts"] = attempts
        else:
            prices[symbol] = price_block(
                bars=normalized,
                adjustment="raw",
                pages_complete=True,
                actions_complete=actions_complete and symbol_actions is not None,
                actions_classified=symbol_actions,
                checked_from=checked_from,
                checked_through=checked_through,
                feed=feed,
            )
            prices[symbol]["unit_evidence"]["attempts"] = attempts

    prices = enforce_price_evidence(prices)
    groups = []
    group_error = None
    try:
        groups = group_fills(equity_rows)
    except ValueError as exc:
        group_error = f"group_fills:{exc}"
        page["error"] = page["error"] or group_error
        complete = False

    observed_at = stamp.isoformat()
    snapshots = latest_buy_snapshots(book, groups, positions_payload, observed_at) if groups else []
    plans, plans_skipped = load_plans(root / "data" / "trade_plans" / book, book)
    action_coverage = None
    if complete:
        held_symbols = sorted(
            {str(row.get("symbol")) for row in equity_rows if row.get("symbol")}
        )
        action_coverage = inventory_action_coverage(
            transport, headers, held_symbols, checked_from, checked_through
        )
    inventory, inventory_label, inventory_detail = starting_inventory(complete, action_coverage)
    report_error = None
    if group_error:
        report = {
            "schema": 1,
            "book": book,
            "as_of": observed_at,
            "purchases": [],
            "sales": [],
            "error": group_error,
        }
        report_error = group_error
    elif calendar_error or not calendar:
        report = {
            "schema": 1,
            "book": book,
            "as_of": observed_at,
            "purchases": [],
            "sales": [],
            "error": calendar_error or "calendar_unavailable",
        }
        report_error = report["error"]
    else:
        try:
            report = post_sale_report(
                book=book,
                fills=equity_rows,
                calendar=[{k: row[k] for k in ("date", "close")} for row in calendar],
                prices=prices,
                as_of=observed_at,
                plans=plans,
                initial_inventory=inventory,
                position_snapshots=snapshots,
            )
        except (ValueError, TypeError) as exc:
            report = {
                "schema": 1,
                "book": book,
                "as_of": observed_at,
                "purchases": [],
                "sales": [],
                "error": type(exc).__name__,
            }
            report_error = report["error"]
    report = annotate_unsold_positions(
        report, positions_payload, positions_error=positions_error
    )

    notes = _execution_notes(equity_rows)
    links = journal_attribution(journal_path) if journal_path else {}
    used_links = {}
    for section in ("purchases", "sales"):
        for row in report.get(section, []):
            order_id = row.get("order_id")
            if order_id in links:
                used_links[order_id] = links[order_id]
    report["journal_attribution"] = used_links
    report["execution_notes"] = {
        row.get("order_id"): notes.get(row.get("order_id"), {})
        for section in ("purchases", "sales")
        for row in report.get(section, [])
    }
    report["coverage"] = {
        "complete_from_start": complete,
        "truncated": page["truncated"],
        "pagination_error": page["error"],
        "pages": page["pages"],
        "conflicts": conflicts,
        "fills_stored": len(merged),
        "fills_added": max(0, added),
        "non_equity_excluded": non_equity,
        "unclassified_side_excluded": unclassified,
        "sale_symbols_priced": list(prices),
        "sale_symbols_deferred": deferred,
        "price_errors": price_errors,
        "positions_error": positions_error,
        "calendar_error": calendar_error,
        "plans_skipped": plans_skipped,
        "plans_loaded": len(plans),
        "initial_inventory": inventory_label,
        "initial_inventory_detail": inventory_detail,
        "account_last4": fingerprint["account_last4"],
        "account_sha256": fingerprint["account_sha256"],
        "price_adjustment": "raw broker fills compared only with adjustment=raw bars",
        "dividends_fees_redeployment": "not included in price follow-through",
        "expected_drawdown_rule": "not_activated",
    }
    report["generated_at"] = observed_at
    json_path, md_path = _report_paths(root, book)
    _atomic_json(json_path, report)
    _atomic_text(md_path, render_markdown(report) if report.get("sales") or report.get("purchases") else _error_markdown(report))
    state = {
        "book": book,
        "account_sha256": fingerprint["account_sha256"],
        "account_last4": fingerprint["account_last4"],
        "complete_from_start": complete,
        "truncated": page["truncated"],
        "error": page["error"] or report_error,
        "fills_stored": len(merged),
        "generated_at": observed_at,
    }
    _atomic_json(state_path, state)
    if calendar:
        _atomic_json(evidence / "calendar.json", calendar)
    return {
        "book": book,
        "error": state["error"],
        "complete_from_start": complete,
        "truncated": page["truncated"],
        "fills_stored": len(merged),
        "fills_added": max(0, added),
        "purchases": len(report.get("purchases") or []),
        "sales": len(report.get("sales") or []),
        "account_last4": fingerprint["account_last4"],
        "price_errors": price_errors,
        "calendar_error": calendar_error,
        "positions_error": positions_error,
        "report_json": str(json_path),
        "report_md": str(md_path),
    }


def _action_symbols(item: dict) -> set[str] | None:
    """Symbols named by one action. None means the row cannot be scoped."""
    if item.get("symbol"):
        return {str(item["symbol"])}
    names = {str(item[key]) for key in ("old_symbol", "new_symbol") if item.get(key)}
    return names or None


def _actions_for_symbol(payload: dict, symbol: str) -> dict | None:
    """Keep actions that name this symbol. An unscoped row leaves coverage unverified."""
    body = (payload or {}).get("corporate_actions") or {}
    subset: dict[str, list] = {}
    for key, items in body.items():
        if not isinstance(items, list):
            continue
        named = []
        for item in items:
            if not isinstance(item, dict):
                return None
            names = _action_symbols(item)
            if names is None:
                return None
            if symbol in names:
                named.append(item)
        if named:
            subset[key] = named
    return classify_corporate_actions({"corporate_actions": subset})


def _error_markdown(report: dict) -> str:
    return (
        f"# Purchase plans and sale follow-through — {report.get('book')}\n\n"
        f"As of: {report.get('as_of')}\n\n"
        f"Report incomplete: {report.get('error')}. No price follow-through was invented.\n"
    )


def _write_error_report(root: Path, book: str, stamp: datetime, summary: dict) -> None:
    report = {
        "schema": 1,
        "book": book,
        "as_of": stamp.isoformat(),
        "purchases": [],
        "sales": [],
        "error": summary.get("error"),
        "coverage": {"account_last4": summary.get("account_last4")},
    }
    json_path, md_path = _report_paths(root, book)
    _atomic_json(json_path, report)
    _atomic_text(md_path, _error_markdown(report))


def urllib_transport(timeout: float = 45):
    def transport(url: str, headers: dict[str, str]):
        return http_get_json(url, headers, timeout=timeout)

    return transport
