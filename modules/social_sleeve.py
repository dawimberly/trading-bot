"""Social / creator macro sleeve (Felix & shared sources) — paper-first, optional live mirror."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

import config
from modules.felix_sentiment import latest_felix_sentiment
from modules.sentiment_keywords import score_text_sentiment

ROOT = Path(__file__).resolve().parents[1]
SOCIAL_SYMBOLS = frozenset({"SPY", "GLD", "XLE"})
JOURNAL_PATH = ROOT / "sentiment" / "social_sleeve_journal.csv"


def get_social_alpaca_credentials() -> tuple[str, str] | None:
    key = (
        __import__("os").getenv("SOCIAL_APCA_API_KEY_ID")
        or __import__("os").getenv("PAPER_APCA_API_KEY_ID")
    )
    secret = (
        __import__("os").getenv("SOCIAL_APCA_API_SECRET_KEY")
        or __import__("os").getenv("PAPER_APCA_API_SECRET_KEY")
    )
    if key and secret:
        return key, secret
    return None


def social_paper_available() -> bool:
    return get_social_alpaca_credentials() is not None


def aggregate_social_score(
    wisdom: dict | None = None,
    *,
    felix_score: float | None = None,
    headline: float | None = None,
) -> dict:
    """Blend Felix transcript mood with wisdom headline web (shared social inputs)."""
    felix = None
    if felix_score is None and config.FELIX_SENTIMENT_ENABLED:
        felix = latest_felix_sentiment()
        if felix and felix.get("sentiment") is not None:
            felix_score = float(felix["sentiment"])
    if headline is None and wisdom:
        headline = wisdom.get("headline_web_sentiment")
        if headline is None and wisdom.get("web_sentiment") is not None:
            headline = wisdom.get("web_sentiment")

    parts = []
    weights = []
    if felix_score is not None:
        parts.append(felix_score)
        weights.append(config.SOCIAL_FELIX_WEIGHT)
    if headline is not None:
        parts.append(float(headline))
        weights.append(config.SOCIAL_HEADLINE_WEIGHT)

    if not parts:
        return {"score": None, "felix": felix, "sources": 0}

    wsum = sum(weights) or 1.0
    score = round(sum(p * w for p, w in zip(parts, weights)) / wsum, 4)
    return {
        "score": score,
        "felix": felix,
        "headline_web": headline,
        "sources": len(parts),
    }


def target_symbol_for_score(
    score: float | None,
    *,
    live_mirror: bool = False,
) -> str | None:
    """
    Felix-aligned macro tilt (single ETF, rotate on score change).
    Bearish → gold; mild bear → energy; bullish → SPY; neutral → cash.
    Live mirror skips SPY (main fund SPY sleeve already owns it).
    """
    if score is None:
        return None
    if score <= config.SOCIAL_BEAR_GLD_THRESHOLD:
        return "GLD"
    if score <= config.SOCIAL_BEAR_ENERGY_THRESHOLD:
        return "XLE"
    if score >= config.SOCIAL_BULL_SPY_THRESHOLD:
        return None if live_mirror else "SPY"
    return None


def _social_positions(executor) -> list:
    out = []
    for pos in executor.client.get_all_positions():
        sym = config.normalize_symbol(pos.symbol)
        base = sym.replace("-USD", "")
        if base in SOCIAL_SYMBOLS or sym in SOCIAL_SYMBOLS:
            out.append(pos)
    return out


def _position_value(pos) -> float:
    mv = getattr(pos, "market_value", None)
    if mv is not None:
        return abs(float(mv))
    return abs(float(pos.qty) * float(pos.current_price or 0))


def social_sleeve_value(executor) -> float:
    return sum(_position_value(p) for p in _social_positions(executor))


def _journal_book(account_label: str) -> dict[str, float]:
    """Notional book for an account label from the social sleeve journal."""
    book: dict[str, float] = {}
    if not JOURNAL_PATH.is_file():
        return book
    with open(JOURNAL_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("account") != account_label:
                continue
            sym = (row.get("symbol") or "").strip()
            if not sym:
                continue
            try:
                n = float(row.get("notional") or 0)
            except (TypeError, ValueError):
                continue
            if row.get("action") == "buy":
                book[sym] = round(book.get(sym, 0.0) + n, 2)
            elif row.get("action") == "sell":
                book[sym] = round(max(0.0, book.get(sym, 0.0) - n), 2)
    return {k: v for k, v in book.items() if v > 0}


def _log_action(row: dict) -> None:
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "timestamp",
        "account",
        "action",
        "symbol",
        "notional",
        "target",
        "score",
        "felix_video_id",
        "ok",
        "notes",
    ]
    write_header = not JOURNAL_PATH.is_file() or JOURNAL_PATH.stat().st_size == 0
    with open(JOURNAL_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if write_header:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fields})


def _rebalance_executor(
    executor,
    *,
    account_label: str,
    equity: float,
    target: str | None,
    agg: dict,
    market_open: bool,
    buy_only: bool = False,
) -> list[dict]:
    actions: list[dict] = []
    if not market_open:
        return actions

    cap = round(equity * config.effective_social_sleeve_cap_pct(), 2)
    min_n = config.effective_min_notional(equity)
    positions = _social_positions(executor)
    held = {config.normalize_symbol(p.symbol).replace("-USD", ""): p for p in positions}
    book = _journal_book(account_label) if buy_only else {}

    for sym, pos in list(held.items()):
        if buy_only:
            sell_n = round(book.get(sym, 0.0), 2)
            if not sell_n or target == sym:
                continue
        elif target is None or sym != target:
            sell_n = round(_position_value(pos), 2)
        else:
            continue
        if sell_n >= min_n:
            order = executor.execute_reduce_notional(config.normalize_symbol(pos.symbol), sell_n)
            act = {
                "account": account_label,
                "action": "sell",
                "symbol": sym,
                "notional": sell_n,
                "ok": order is not None,
            }
            actions.append(act)
            _log_action(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "target": target or "cash",
                    "score": agg.get("score"),
                    "felix_video_id": (agg.get("felix") or {}).get("video_id"),
                    "notes": "rotate out",
                    **act,
                }
            )
            if buy_only and act["ok"]:
                book[sym] = round(max(0.0, book.get(sym, 0.0) - sell_n), 2)

    if not target:
        return actions

    current = round(sum(book.values()), 2) if buy_only else social_sleeve_value(executor)
    room = round(cap - current, 2)
    if room < min_n:
        return actions

    buy_n = round(min(room, cap), 2)
    if buy_n < min_n:
        return actions

    order = executor.execute_order(target, "buy", notional=buy_n)
    act = {
        "account": account_label,
        "action": "buy",
        "symbol": target,
        "notional": buy_n,
        "ok": order is not None,
    }
    actions.append(act)
    _log_action(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "target": target,
            "score": agg.get("score"),
            "felix_video_id": (agg.get("felix") or {}).get("video_id"),
            "notes": "",
            **act,
        }
    )
    return actions


def run_social_sleeve_cycle(
    wisdom: dict | None,
    live_executor,
    *,
    market_open: bool,
) -> dict:
    """
    Paper social sleeve (full cap) + optional live mirror (fraction of cap).
    Does not buy IPOs; rotates GLD / XLE / SPY / cash from shared social mood.
    """
    if not config.SOCIAL_SLEEVE_ENABLED:
        return {"enabled": False}

    agg = aggregate_social_score(wisdom)
    target = target_symbol_for_score(agg.get("score"))
    mirror_target = target_symbol_for_score(agg.get("score"), live_mirror=True)
    result = {
        "enabled": True,
        "score": agg.get("score"),
        "target": target,
        "mirror_target": mirror_target,
        "cap_pct": config.effective_social_sleeve_cap_pct(),
        "paper_aggressive": config.paper_aggressive_context(),
        "felix_video_id": (agg.get("felix") or {}).get("video_id"),
        "felix_title": (agg.get("felix") or {}).get("title"),
        "paper_actions": [],
        "live_mirror_actions": [],
        "paper_equity": None,
        "paper_ok": False,
    }

    creds = get_social_alpaca_credentials()
    if creds and config.SOCIAL_SLEEVE_PAPER:
        config.set_paper_aggressive_context(True)
        try:
            from modules.alpaca_executor import AlpacaExecutor

            paper_ex = AlpacaExecutor(
                paper=True,
                credentials_fn=lambda: creds,
            )
            peq = float(paper_ex.client.get_account().equity)
            result["paper_equity"] = peq
            result["paper_ok"] = True
            result["paper_actions"] = _rebalance_executor(
                paper_ex,
                account_label="paper",
                equity=peq,
                target=target,
                agg=agg,
                market_open=market_open,
            )
        except Exception as exc:
            result["paper_error"] = str(exc)
        finally:
            config.set_paper_aggressive_context(False)

    mirror_pct = config.SOCIAL_MIRROR_TO_LIVE_PCT
    if (
        mirror_pct > 0
        and mirror_target
        and not config.PAPER_TRADING
        and live_executor is not None
    ):
        try:
            leq = float(live_executor.client.get_account().equity)
            mirror_cap = round(leq * config.SOCIAL_SLEEVE_CAP_PCT * mirror_pct, 2)
            min_n = config.effective_min_notional(leq)
            if mirror_cap >= min_n:
                result["live_mirror_actions"] = _rebalance_executor(
                    live_executor,
                    account_label="live_mirror",
                    equity=leq * mirror_pct,
                    target=mirror_target,
                    agg=agg,
                    market_open=market_open,
                    buy_only=True,
                )
        except Exception as exc:
            result["live_mirror_error"] = str(exc)

    return result
