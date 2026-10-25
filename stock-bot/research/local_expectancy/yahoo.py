"""Free Yahoo daily bars. No key, no paid SIP, no broker."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
USER_AGENT = "local-expectancy-research/1.0"


def chart_url(symbol: str, period1: int, period2: int) -> str:
    return (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{symbol}?interval=1d&period1={period1}&period2={period2}"
    )


def parse_chart(payload: dict) -> dict[str, tuple[float, float]]:
    """Adjusted open and close by session date. One series, no unadjusted mix."""
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        raise ValueError("empty chart")
    block = result[0]
    stamps = block.get("timestamp") or []
    indicators = block.get("indicators") or {}
    quote = (indicators.get("quote") or [{}])[0]
    adj_block = (indicators.get("adjclose") or [{}])[0]
    adj = adj_block.get("adjclose")
    opens = quote.get("open") or []
    closes = quote.get("close") or []
    if not adj or len(adj) != len(stamps):
        raise ValueError("adjusted close missing")
    bars: dict[str, tuple[float, float]] = {}
    for stamp, opened, close, adjusted in zip(stamps, opens, closes, adj):
        if opened is None or close is None or adjusted is None:
            continue
        if opened <= 0 or close <= 0 or adjusted <= 0:
            continue
        day = datetime.fromtimestamp(int(stamp), tz=timezone.utc).astimezone(ET).date()
        if day.weekday() >= 5:
            continue
        scale = adjusted / close
        bars[day.isoformat()] = (opened * scale, adjusted)
    if not bars:
        raise ValueError("no sessions")
    return bars


def fetch_symbol(symbol: str, period1: int, period2: int, *, pause: float = 0.15) -> dict[str, tuple[float, float]]:
    request = urllib.request.Request(chart_url(symbol, period1, period2), headers={"User-Agent": USER_AGENT})
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
            bars = parse_chart(payload)
            if pause:
                time.sleep(pause)
            return bars
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"{symbol} download failed") from last_error
