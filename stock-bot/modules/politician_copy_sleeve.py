"""Politician copy-trading sleeve — Capitol Trades signals (paper only).

Fetches recent congressional trades (Capitol Trades HTML or cached/seed JSON),
ranks top traders by activity, and mirrors buys with strict position caps.

Live cycle places paper equity orders via the executor. Backtests replay
date-filtered trades from the seed/cache tape against daily bars.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import config
from modules.safe_io import read_json_file, write_json_file

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
_STATE_PATH = ROOT / "data" / "politician_copy_state.json"
_CACHE_PATH = ROOT / "data" / "politician_trades_cache.json"
_SEED_PATH = ROOT / "data" / "politician_trades_seed.json"
_SLEEVE = "POLITICIAN"
_UA = (
    "Mozilla/5.0 (compatible; PythonTradingBot/1.5.4; +https://github.com/local) "
    "AppleWebKit/537.36"
)


def _state_path() -> Path:
    raw = getattr(config, "POLITICIAN_COPY_STATE_FILE", None)
    if raw:
        p = Path(str(raw))
        return p if p.is_absolute() else ROOT / p
    return _STATE_PATH


def _cache_path() -> Path:
    raw = getattr(config, "POLITICIAN_TRADES_CACHE_FILE", None)
    if raw:
        p = Path(str(raw))
        return p if p.is_absolute() else ROOT / p
    return _CACHE_PATH


def _seed_path() -> Path:
    raw = getattr(config, "POLITICIAN_TRADES_SEED_FILE", None)
    if raw:
        p = Path(str(raw))
        return p if p.is_absolute() else ROOT / p
    return _SEED_PATH


def _load_state() -> dict[str, Any]:
    raw = read_json_file(_state_path()) or {}
    return raw if isinstance(raw, dict) else {}


def _save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_file(path, state)


def _cap_pct() -> float:
    return max(0.01, min(0.15, float(getattr(config, "POLITICIAN_COPY_CAP_PCT", 0.05))))


def _max_name_pct() -> float:
    return max(0.005, min(0.05, float(getattr(config, "POLITICIAN_COPY_MAX_NAME_PCT", 0.02))))


def _top_traders() -> int:
    return max(1, int(getattr(config, "POLITICIAN_COPY_TOP_TRADERS", 5)))


def _max_copies_per_day() -> int:
    return max(1, int(getattr(config, "POLITICIAN_COPY_MAX_PER_DAY", 2)))


def _lookback_days() -> int:
    return max(7, int(getattr(config, "POLITICIAN_COPY_LOOKBACK_DAYS", 60)))


def _min_amount_usd() -> float:
    return max(0.0, float(getattr(config, "POLITICIAN_COPY_MIN_AMOUNT_USD", 10000.0)))


def _fill_lag_days() -> int:
    return max(0, int(getattr(config, "POLITICIAN_COPY_FILL_LAG_DAYS", 3)))


def _trade_key(t: dict[str, Any]) -> str:
    return (
        f"{str(t.get('date') or '')[:10]}|"
        f"{str(t.get('politician') or '').strip()}|"
        f"{str(t.get('ticker') or '').strip().upper()}|"
        f"{str(t.get('side') or '').lower()}"
    )


def ensure_seed_trades(*, force: bool = False) -> list[dict[str, Any]]:
    """Create a weekday-aligned synthetic tape for backtests when seed is missing."""
    path = _seed_path()
    seed_version = 2
    if path.is_file() and not force:
        raw = read_json_file(path) or {}
        if isinstance(raw, dict) and int(raw.get("seed_version") or 0) >= seed_version:
            trades = raw.get("trades") or []
            if isinstance(trades, list) and trades:
                return [t for t in trades if isinstance(t, dict)]

    politicians = [
        ("Nancy Pelosi", "D-CA"),
        ("Tommy Tuberville", "R-AL"),
        ("Dan Crenshaw", "R-TX"),
        ("Josh Gottheimer", "D-NJ"),
        ("Michael McCaul", "R-TX"),
    ]
    # Liquid names present in the static UNIVERSE / deep-history set.
    tickers_buy = ["NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "JPM", "XOM", "CRM", "AMD"]
    tickers_sell = ["BA", "DIS", "INTC", "TSLA"]
    trades: list[dict[str, Any]] = []
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=540)
    i = 0
    weekday_i = 0
    day = start
    while day <= end:
        if day.weekday() < 5:
            if weekday_i % 4 == 0:
                pol, party = politicians[i % len(politicians)]
                buy = i % 5 != 0
                sym = (
                    tickers_buy[i % len(tickers_buy)]
                    if buy
                    else tickers_sell[i % len(tickers_sell)]
                )
                amt = 10000 + (i % 8) * 8000
                trades.append(
                    {
                        "date": day.strftime("%Y-%m-%d"),
                        "politician": pol,
                        "party": party,
                        "ticker": sym,
                        "side": "buy" if buy else "sell",
                        "amount_usd": float(amt),
                        "source": "seed",
                    }
                )
                i += 1
            weekday_i += 1
        day += timedelta(days=1)

    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_file(
        path,
        {
            "seed_version": seed_version,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "note": "Weekday-aligned synthetic seed for paper backtests",
            "trades": trades,
        },
    )
    return trades


def candidates_for_bar(
    trades: list[dict[str, Any]],
    *,
    as_of: datetime,
    top_traders: list[str] | None = None,
    filled_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    """PIT candidates: trade date on/before bar, within fill lag, not yet filled."""
    lag = _fill_lag_days()
    as_of_d = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
    if as_of_d.tzinfo is None:
        as_of_d = as_of_d.replace(tzinfo=timezone.utc)
    filled = filled_keys or set()
    cands = filter_copy_candidates(trades, as_of=as_of, top_traders=top_traders)
    out: list[dict[str, Any]] = []
    for c in cands:
        key = _trade_key(c)
        if key in filled:
            continue
        dt = _parse_date(str(c.get("date") or ""))
        if dt is None:
            continue
        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        if dt > as_of_d:
            continue
        if (as_of_d - dt).days > lag:
            continue
        out.append(c)
    return out


def _parse_capitol_trades_html(html: str) -> list[dict[str, Any]]:
    """Best-effort parse of Capitol Trades listing HTML."""
    trades: list[dict[str, Any]] = []
    # ticker links often look like /stocks/NVDA or >NVDA<
    ticker_re = re.compile(r"/stocks/([A-Z]{1,5})(?:\"|'|/|\?)")
    # dates like 2026-07-15 or Jul 15, 2026
    date_iso = re.compile(r"(20\d{2}-\d{2}-\d{2})")
    side_re = re.compile(r"\b(buy|sell|purchase|sale)\b", re.I)
    # politician name near trade rows
    name_re = re.compile(r"/politicians/([a-z0-9\-]+)", re.I)

    chunks = re.split(r"<tr[\s>]|</tr>", html, flags=re.I)
    for chunk in chunks:
        tickers = ticker_re.findall(chunk)
        if not tickers:
            continue
        dates = date_iso.findall(chunk)
        sides = side_re.findall(chunk)
        pols = name_re.findall(chunk)
        side = "buy"
        if sides:
            s = sides[0].lower()
            side = "sell" if s in ("sell", "sale") else "buy"
        pol = pols[0].replace("-", " ").title() if pols else "Unknown"
        trades.append(
            {
                "date": dates[0] if dates else datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "politician": pol,
                "party": "",
                "ticker": tickers[0].upper(),
                "side": side,
                "amount_usd": float(_min_amount_usd()),
                "source": "capitoltrades",
            }
        )
    # de-dupe
    seen: set[tuple] = set()
    out: list[dict[str, Any]] = []
    for t in trades:
        key = (t["date"], t["politician"], t["ticker"], t["side"])
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out[:200]


def fetch_capitol_trades(*, force: bool = False) -> list[dict[str, Any]]:
    """Fetch recent trades; prefer live Capitol Trades, else cache, else seed."""
    cache = _cache_path()
    max_age_h = float(getattr(config, "POLITICIAN_COPY_CACHE_HOURS", 12.0))
    if cache.is_file() and not force:
        try:
            age_h = (datetime.now(timezone.utc).timestamp() - cache.stat().st_mtime) / 3600.0
            if age_h <= max_age_h:
                raw = read_json_file(cache) or {}
                trades = raw.get("trades") if isinstance(raw, dict) else raw
                if isinstance(trades, list) and trades:
                    return [t for t in trades if isinstance(t, dict)]
        except OSError:
            pass

    url = str(
        getattr(config, "POLITICIAN_COPY_SOURCE_URL", "https://www.capitoltrades.com/trades")
        or "https://www.capitoltrades.com/trades"
    )
    live: list[dict[str, Any]] = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "text/html"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        live = _parse_capitol_trades_html(html)
        if live:
            cache.parent.mkdir(parents=True, exist_ok=True)
            write_json_file(
                cache,
                {
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "source": url,
                    "count": len(live),
                    "trades": live,
                },
            )
            logger.info("[POLITICIAN] fetched %d trades from Capitol Trades", len(live))
            return live
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.info("[POLITICIAN] Capitol Trades fetch failed (%s) — using seed/cache", exc)

    seed = ensure_seed_trades()
    if seed:
        cache.parent.mkdir(parents=True, exist_ok=True)
        write_json_file(
            cache,
            {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "source": "seed",
                "count": len(seed),
                "trades": seed,
            },
        )
    return seed


def _parse_date(s: str) -> datetime | None:
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def rank_top_traders(trades: list[dict[str, Any]], *, top_n: int | None = None) -> list[str]:
    """Rank politicians by buy activity (count * log size)."""
    top_n = top_n or _top_traders()
    scores: dict[str, float] = {}
    for t in trades:
        if str(t.get("side", "")).lower() not in ("buy", "purchase"):
            continue
        name = str(t.get("politician") or "").strip()
        if not name:
            continue
        amt = float(t.get("amount_usd") or 0)
        scores[name] = scores.get(name, 0.0) + 1.0 + (amt / 100000.0)
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    return [n for n, _ in ranked[:top_n]]


def filter_copy_candidates(
    trades: list[dict[str, Any]],
    *,
    as_of: datetime | None = None,
    top_traders: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Buys from top traders within lookback, above min size."""
    as_of = as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    cutoff = as_of - timedelta(days=_lookback_days())
    top = set(top_traders or rank_top_traders(trades))
    out: list[dict[str, Any]] = []
    for t in trades:
        if str(t.get("side", "")).lower() not in ("buy", "purchase"):
            continue
        pol = str(t.get("politician") or "").strip()
        if pol not in top:
            continue
        dt = _parse_date(str(t.get("date") or ""))
        if dt is None or dt < cutoff or dt > as_of + timedelta(days=1):
            continue
        amt = float(t.get("amount_usd") or 0)
        if amt < _min_amount_usd():
            continue
        sym = config.normalize_symbol(str(t.get("ticker") or ""))
        if not sym or config.is_crypto(sym):
            continue
        out.append({**t, "ticker": sym, "politician": pol})
    # newest first
    out.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
    return out


def _size_notional(equity: float, amount_usd: float) -> float:
    """Scale copy size: fraction of disclosed amount, capped."""
    scale = float(getattr(config, "POLITICIAN_COPY_SIZE_SCALE", 0.025))
    raw = max(0.0, float(amount_usd) * scale)
    cap_total = equity * _cap_pct()
    cap_name = equity * _max_name_pct()
    return round(min(raw, cap_total, cap_name), 2)


def run_politician_copy_backtest_day(
    portfolio,
    prices,
    *,
    bar_i: int,
    state: dict,
    ts=None,
    trades: list[dict[str, Any]] | None = None,
    market_open: bool = True,
) -> tuple[list[dict], dict]:
    """Replay Capitol-style buys on matching daily bars."""
    meta = {"active": False, "copies": 0, "notional": 0.0, "skipped": 0}
    actions: list[dict] = []
    if not config.effective_politician_copy_enabled() or not market_open:
        return actions, meta

    if trades is None:
        trades = ensure_seed_trades()
    as_of = datetime.now(timezone.utc)
    if ts is not None:
        try:
            as_of = datetime.fromisoformat(str(ts)[:19])
            if as_of.tzinfo is None:
                as_of = as_of.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            try:
                import pandas as pd

                as_of = pd.Timestamp(ts).to_pydatetime()
                if as_of.tzinfo is None:
                    as_of = as_of.replace(tzinfo=timezone.utc)
            except Exception:
                pass

    day_key = as_of.strftime("%Y-%m-%d")
    if state.get("last_copy_day") == day_key and int(state.get("copies_today", 0)) >= _max_copies_per_day():
        return actions, meta

    top = rank_top_traders(trades)
    state["top_traders"] = top
    filled = set(state.get("filled_keys") or [])
    day_cands = candidates_for_bar(
        trades, as_of=as_of, top_traders=top, filled_keys=filled
    )
    if not day_cands:
        return actions, meta

    eq = float(portfolio.equity(prices))
    used = 0.0
    for sym, qty in list(portfolio.positions.items()):
        if qty > 0 and sym in getattr(prices, "index", []):
            px = float(prices.get(sym) or 0)
            if px > 0:
                used += qty * px
    room = max(0.0, eq * _cap_pct() - used)
    copies_today = int(state.get("copies_today", 0)) if state.get("last_copy_day") == day_key else 0

    for c in day_cands:
        if copies_today >= _max_copies_per_day() or room < 25:
            break
        sym = str(c["ticker"])
        if sym not in prices.index:
            meta["skipped"] += 1
            continue
        spot = float(prices[sym])
        if spot <= 0:
            continue
        notion = min(_size_notional(eq, float(c.get("amount_usd") or 0)), room)
        min_n = float(getattr(config, "PAPER_MIN_NOTIONAL", 2.0) or 2.0)
        if notion < min_n or float(portfolio.cash) < notion:
            meta["skipped"] += 1
            continue
        qty = notion / spot
        portfolio.cash = round(float(portfolio.cash) - notion, 2)
        portfolio.positions[sym] = float(portfolio.positions.get(sym, 0) or 0) + qty
        room = max(0.0, room - notion)
        copies_today += 1
        meta["copies"] += 1
        meta["notional"] = round(meta["notional"] + notion, 2)
        meta["active"] = True
        filled.add(_trade_key(c))
        actions.append(
            {
                "action": "copy_buy",
                "symbol": sym,
                "notional": notion,
                "politician": c.get("politician"),
                "source_date": c.get("date"),
            }
        )

    state["filled_keys"] = list(filled)[-2000:]
    state["last_copy_day"] = day_key
    state["copies_today"] = copies_today
    state["total_copies"] = int(state.get("total_copies", 0)) + meta["copies"]
    state["total_notional"] = round(float(state.get("total_notional", 0)) + meta["notional"], 2)
    return actions, meta


def run_politician_copy_cycle(
    executor,
    data=None,
    *,
    market_open: bool = True,
) -> dict:
    """Paper cycle: fetch trades and mirror top-trader buys with size caps."""
    result: dict[str, Any] = {"enabled": False, "actions": [], "copies": 0}
    if not config.effective_politician_copy_enabled() or not market_open:
        return result
    result["enabled"] = True

    trades = fetch_capitol_trades()
    top = rank_top_traders(trades)
    cands = filter_copy_candidates(trades, top_traders=top)
    state = _load_state()
    day_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    copies_today = int(state.get("copies_today", 0)) if state.get("last_copy_day") == day_key else 0

    try:
        acct = executor._get_account()
        equity = float(acct.equity)
    except Exception:
        equity = 0.0

    actions: list[dict] = []
    for c in cands:
        if copies_today >= _max_copies_per_day():
            break
        sym = str(c["ticker"])
        # Skip if already held meaningfully
        try:
            pos = executor._find_position(sym)
            if pos is not None and float(executor._position_market_value(pos)) > equity * _max_name_pct() * 0.5:
                continue
        except Exception:
            pass
        notion = _size_notional(equity, float(c.get("amount_usd") or 0))
        min_n = config.effective_min_notional(equity)
        if notion < min_n:
            continue
        try:
            order = executor.execute_order(sym, "buy", notional=notion)
        except Exception as exc:
            logger.debug("[POLITICIAN] order failed %s: %s", sym, exc)
            continue
        if not order:
            continue
        copies_today += 1
        actions.append(
            {
                "action": "copy_buy",
                "symbol": sym,
                "notional": notion,
                "politician": c.get("politician"),
                "date": c.get("date"),
            }
        )

    state["last_copy_day"] = day_key
    state["copies_today"] = copies_today
    state["top_traders"] = top
    state["last_actions"] = actions
    state["last_fetch_count"] = len(trades)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)

    result["actions"] = actions
    result["copies"] = len(actions)
    result["top_traders"] = top
    result["fetched"] = len(trades)
    if actions:
        print(
            f"--- Politician copy: {len(actions)} buy(s) mirroring "
            f"{', '.join(top[:3]) or 'top traders'} ---"
        )
        logger.info("[POLITICIAN] copied %d trades; top=%s", len(actions), top[:3])
    else:
        logger.info(
            "[POLITICIAN] no copies (fetched=%d top=%s candidates=%d)",
            len(trades),
            top[:3],
            len(cands),
        )
    return result


def format_politician_copy_banner() -> str | None:
    if not config.effective_politician_copy_enabled():
        return ">>> Politician Copy: OFF (paper opt-in)"
    return (
        f">>> Politician Copy: ON (Capitol Trades, top {_top_traders()} traders, "
        f"cap {_cap_pct():.0%}, max/name {_max_name_pct():.0%}, "
        f"≤{_max_copies_per_day()}/day, paper-only) <<<"
    )


def format_weekly_politician_copy_note() -> str:
    if not config.effective_politician_copy_enabled():
        return ""
    state = _load_state()
    top = state.get("top_traders") or []
    copies = int(state.get("total_copies", 0) or 0)
    notion = float(state.get("total_notional", 0) or 0)
    tops = ", ".join(str(t) for t in top[:3]) or "n/a"
    return (
        f"Politician copy: ON | top {tops} | "
        f"cap {_cap_pct():.0%} | copies {copies} | notional ${notion:.0f}"
    )


def format_telegram_weekly_politician_copy_block() -> str:
    note = format_weekly_politician_copy_note()
    if not note:
        return ""
    return f"\n\n{note}"
