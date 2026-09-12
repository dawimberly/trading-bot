"""Collect live/paper dashboard state as JSON (read-only).

Prints to stdout and writes stock-bot/data/dashboard_export.json.

  python scripts/export_dashboard_data.py
  python scripts/export_dashboard_data.py --live
  python scripts/export_dashboard_data.py --paper
  python scripts/export_dashboard_data.py --live --paper
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import status  # noqa: E402
from modules.alpaca_client import (  # noqa: E402
    call_with_retry,
    get_trading_client,
)
from modules.cost_basis import sleeve_for_symbol  # noqa: E402

OUT_PATH = ROOT / "data" / "dashboard_export.json"
STALE_MINUTES = 90.0

_SLEEVE_DISPLAY = {
    "core": "VTI core",
    "spy": "SPY",
    "nyse": "NYSE momentum",
    "crypto": "crypto",
    "metal": "metal",
    "stat_arb": "stat arb",
    "other": "other",
}


def _book_has_keys(*, paper: bool) -> bool:
    st = config.alpaca_credentials_status(paper=paper)
    return bool(st.get("has_key") and st.get("has_secret"))


def _resolve_paper_heartbeat_path() -> Path:
    env_raw = (os.getenv("PAPER_CHASE_HEARTBEAT") or "").strip()
    if env_raw:
        p = Path(env_raw)
        return p if p.is_absolute() else ROOT / p
    candidates: list[Path] = []
    portal = ROOT / "data" / "portal" / "users"
    if portal.is_dir():
        candidates.extend(sorted(portal.glob("*/books/alpaca_paper/bot_heartbeat.json")))
        candidates.extend(sorted(portal.glob("*/books/alpaca_paper/paper_chase_heartbeat.json")))
    candidates.append(ROOT / "paper_chase_heartbeat.json")
    best: Path | None = None
    best_mtime = -1.0
    for path in candidates:
        if not path.is_file():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best = path
            best_mtime = mtime
    return best or (ROOT / status.PAPER_HEARTBEAT)


def _stat_arb_symbols() -> set[str]:
    try:
        from modules.stat_arb_sleeve import stat_arb_pair_symbols

        return {config.normalize_symbol(s) for s in stat_arb_pair_symbols(None)}
    except Exception:
        return set()


def _sleeve_label(symbol: str, *, stat_arb: set[str]) -> str:
    sym = config.normalize_symbol(symbol)
    if sym in stat_arb:
        return _SLEEVE_DISPLAY["stat_arb"]
    key = sleeve_for_symbol(sym)
    if key in _SLEEVE_DISPLAY:
        return _SLEEVE_DISPLAY[key]
    asset_class = "crypto" if config.is_crypto(sym) else "equity"
    return asset_class


def _bot_running(*, paper: bool) -> bool:
    if paper:
        return status._legacy_bot_running("run_paper_bot.py") or status._legacy_bot_running(
            "run_all.py"
        )
    return status._legacy_bot_running("run_live_bot.py") or status._legacy_bot_running(
        "run_all.py"
    )


def _fetch_account_and_positions(*, paper: bool):
    client = get_trading_client(paper=paper, allow_live=True)
    acct = call_with_retry(client.get_account, op_name="get_account")
    positions = call_with_retry(client.get_all_positions, op_name="get_all_positions")
    return acct, list(positions or [])


def collect_book(*, paper: bool) -> dict:
    book = "paper" if paper else "live"
    hb_path = _resolve_paper_heartbeat_path() if paper else status._resolve_live_heartbeat_path()
    if not hb_path.is_absolute():
        hb_path = ROOT / hb_path
    hb = status._load_json(hb_path)
    hb_age = status._heartbeat_age_minutes(hb, path=hb_path)
    age_min = float(hb_age) if hb_age is not None else 0.0
    running = _bot_running(paper=paper)
    regime = status._heartbeat_regime(hb) or "n/a"
    stale = hb_age is None or hb_age > STALE_MINUTES

    equity = status._heartbeat_equity(hb)
    cash = None
    if hb:
        try:
            cash = float(hb.get("cash"))
        except (TypeError, ValueError):
            cash = None

    pos_rows: list[dict] = []
    try:
        acct, positions = _fetch_account_and_positions(paper=paper)
        equity = float(acct.equity)
        cash = float(acct.cash)
        stat_arb = _stat_arb_symbols()
        for pos in positions:
            ticker = config.normalize_symbol(getattr(pos, "symbol", "") or "")
            if not ticker:
                continue
            pos_rows.append(
                {
                    "ticker": ticker,
                    "sleeve": _sleeve_label(ticker, stat_arb=stat_arb),
                    "qty": float(pos.qty or 0),
                    "entry": float(getattr(pos, "avg_entry_price", 0) or 0),
                    "last": float(getattr(pos, "current_price", 0) or 0),
                }
            )
        pos_rows.sort(key=lambda r: r["ticker"])
    except Exception as exc:
        print(f"warning: Alpaca {book} fetch failed: {exc}", file=sys.stderr)

    return {
        "book": book,
        "equity": float(equity or 0.0),
        "cash": float(cash or 0.0),
        "positions": pos_rows,
        "status": {
            "regime": regime,
            "heartbeat_age_min": round(age_min, 2),
            "is_stale": bool(stale),
            "bot_running": bool(running),
        },
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


def _select_books(args: argparse.Namespace) -> list[bool]:
    """Return paper flags (False=live, True=paper) to export."""
    if args.live or args.paper:
        books: list[bool] = []
        if args.live:
            books.append(False)
        if args.paper:
            books.append(True)
        return books
    books = []
    if _book_has_keys(paper=False):
        books.append(False)
    if _book_has_keys(paper=True):
        books.append(True)
    if not books:
        books = [bool(config.PAPER_TRADING)]
    return books


def main() -> int:
    parser = argparse.ArgumentParser(description="Export dashboard JSON (read-only).")
    parser.add_argument("--live", action="store_true", help="Export the live Alpaca book")
    parser.add_argument("--paper", action="store_true", help="Export the paper Alpaca book")
    args = parser.parse_args()

    payloads = [collect_book(paper=paper) for paper in _select_books(args)]
    if len(payloads) == 1:
        body = payloads[0]
    else:
        body = payloads

    text = json.dumps(body, indent=2)
    print(text)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
