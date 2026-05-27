"""Kraken portfolio cleanup (playbook rules): leverage, duplicate core, trim small names."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import config
from modules.kraken_advisor import (
    CASH_ASSETS,
    DUPLICATE_US_CORE,
    LEVERAGED_TICKERS,
    fetch_kraken_balances,
    load_manual_positions,
)
from modules.kraken_execute import execute_kraken_trade
from modules.kraken_pairs import (
    equity_pair_likely_unsupported,
    kraken_pair_for_symbol,
    ticker_from_balance_display,
)

STATE_FILE = Path(__file__).resolve().parents[1] / "kraken_autopilot_state.json"
MAX_POSITIONS = 5
CRYPTO_TICKERS = frozenset({"BTC", "ETH", "SOL", "RENDER", "ADA", "AVAX", "LINK", "XBT", "XETH"})


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _manual_tickers(manual: list[dict]) -> set[str]:
    return {(p.get("ticker") or "").upper() for p in manual if p.get("ticker")}


def _is_tradable_intent(intent: dict) -> bool:
    sym = intent.get("symbol", "")
    pair = kraken_pair_for_symbol(sym)
    if not pair or equity_pair_likely_unsupported(pair):
        return False
    vol = intent.get("volume")
    return vol is not None and float(vol) > 0


def _equity_positions(balances: list[dict], manual: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for b in balances:
        disp = b.get("display", "").upper()
        if disp in CASH_ASSETS or disp in CRYPTO_TICKERS:
            continue
        if not disp.endswith(".EQ"):
            continue
        ticker = ticker_from_balance_display(disp)
        if not ticker:
            continue
        rows.append(
            {
                "ticker": ticker,
                "volume": float(b.get("amount", 0)),
                "usd": None,
                "source": "api",
            }
        )

    by_ticker = {r["ticker"]: r for r in rows}
    for p in manual:
        t = (p.get("ticker") or "").upper()
        if not t:
            continue
        usd = float(p.get("usd") or 0)
        if t in by_ticker:
            by_ticker[t]["usd"] = usd
        else:
            by_ticker[t] = {"ticker": t, "volume": None, "usd": usd, "source": "manual"}
    return list(by_ticker.values())


def _skip_symbol(sym: str, pos: dict, manual_set: set[str], done: dict) -> bool:
    if done.get(f"{sym}:duplicate_core_sold_manual"):
        return True
    vol = pos.get("volume")
    if sym not in manual_set and vol is not None and float(vol) < config.KRAKEN_DUST_VOLUME:
        return True
    return False


def build_cleanup_intents(
    *,
    max_actions: int,
    wisdom_stress: bool,
) -> list[dict[str, Any]]:
    """All cleanup intents (execute layer splits auto vs manual)."""
    bal = fetch_kraken_balances()
    manual = load_manual_positions()
    manual_set = _manual_tickers(manual)
    state = _load_state()
    done = state.get("cleanup_done", {})

    positions = _equity_positions(bal.get("balances") or [], manual)
    tickers = {p["ticker"] for p in positions}
    intents: list[dict[str, Any]] = []

    def _append_sell(phase: str, pos: dict, reason: str) -> None:
        sym = pos["ticker"]
        if _skip_symbol(sym, pos, manual_set, done):
            return
        vol = pos.get("volume")
        if vol is not None and float(vol) <= 0:
            return
        intents.append(
            {
                "phase": phase,
                "symbol": sym,
                "side": "sell",
                "volume": float(vol) if vol is not None else None,
                "reason": reason,
            }
        )

    for lev in sorted(LEVERAGED_TICKERS & tickers):
        pos = next((p for p in positions if p["ticker"] == lev), None)
        if pos:
            _append_sell("cleanup_leverage", pos, "leveraged ETF risk")

    core = [p for p in positions if p["ticker"] in DUPLICATE_US_CORE]
    manual_core = [t for t in manual_set if t in DUPLICATE_US_CORE]
    if len(manual_core) >= 2 or len(core) >= 2:
        pool = core if len(core) >= 2 else [p for p in positions if p["ticker"] in manual_core]
        if len(pool) >= 2:
            pool.sort(key=lambda p: float(p.get("usd") or 0))
            loser = pool[0]
            others = [c["ticker"] for c in pool if c != loser]
            _append_sell("cleanup_duplicate_core", loser, f"overlap with {others}")

    if len(positions) > MAX_POSITIONS:
        ranked = sorted(
            [p for p in positions if p["ticker"] not in LEVERAGED_TICKERS],
            key=lambda p: float(p.get("usd") or 0),
        )
        excess = len(positions) - MAX_POSITIONS
        for p in ranked[:excess]:
            _append_sell("cleanup_trim_small", p, f"simplify toward {MAX_POSITIONS} names")

    if wisdom_stress and bal.get("ok"):
        for b in bal.get("balances") or []:
            if b.get("display", "").upper() != "RENDER":
                continue
            amt = float(b.get("amount", 0))
            if amt <= config.KRAKEN_DUST_VOLUME:
                break
            trim = round(amt * 0.25, 8)
            pair = kraken_pair_for_symbol("RENDER")
            if pair:
                try:
                    from kraken.spot import Market

                    ticker = Market().get_ticker(pair=pair)
                    if ticker:
                        price = float(next(iter(ticker.values()))["c"][0])
                        min_vol = config.MIN_NOTIONAL / price if price > 0 else 0
                        if trim * price < config.MIN_NOTIONAL:
                            trim = min(amt, round(min_vol * 1.05, 8))
                except Exception:
                    pass
            if trim > 0 and trim <= amt:
                intents.append(
                    {
                        "phase": "cleanup_speculative",
                        "symbol": "RENDER",
                        "side": "sell",
                        "volume": trim,
                        "reason": "stress + speculative trim (API min size)",
                    }
                )
            break

    auto = [i for i in intents if _is_tradable_intent(i)]
    manual = [i for i in intents if not _is_tradable_intent(i)]
    return auto[:max_actions] + manual[:max_actions]


def execute_cleanup_intents(intents: list[dict]) -> list[dict]:
    """Execute sells via API where allowed (crypto/xStock); no manual Telegram."""
    results: list[dict] = []
    state = _load_state()
    done = state.setdefault("cleanup_done", {})
    auto_done = 0
    max_auto = config.KRAKEN_CLEANUP_MAX_ACTIONS

    tradable = [i for i in intents if _is_tradable_intent(i)]
    manual_intents = [i for i in intents if not _is_tradable_intent(i)]

    for intent in tradable + manual_intents:
        if auto_done >= max_auto:
            break
        sym = intent.get("symbol", "")
        key = f"{sym}:{intent.get('phase')}"
        if done.get(key):
            results.append({**intent, "ok": False, "skipped": "already done"})
            continue
        trade = {
            "symbol": sym,
            "side": "sell",
            "usd": intent.get("usd") or 0,
            "volume": intent.get("volume"),
            "reason": intent.get("reason"),
        }
        out = execute_kraken_trade(trade)
        out["intent"] = intent
        results.append(out)
        if out.get("ok") and not out.get("dry_run") and not out.get("validate"):
            done[key] = date.today().isoformat()
            auto_done += 1

    state["cleanup_done"] = done
    _save_state(state)
    return results
