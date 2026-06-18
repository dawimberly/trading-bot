"""Close stale or dust Alpaca paper positions safely.

Default is dry-run (list only). Use --execute to submit closes after confirmation.

Run:
  python scripts/cleanup_stale_positions.py
  python scripts/cleanup_stale_positions.py --help
  python scripts/cleanup_stale_positions.py --tickers AAPL CVX
  python scripts/cleanup_stale_positions.py --tiny-fractionals
  python scripts/cleanup_stale_positions.py --tiny-fractionals --execute
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.env_loader import ensure_dotenv_loaded

ensure_dotenv_loaded()

import config
from modules.alpaca_client import AlpacaAuthError
from modules.alpaca_diagnostics import alpaca_env_status, format_missing_env_message
from modules.alpaca_executor import AlpacaExecutor
from modules.logging_utils import log_event, setup_logging

logger = logging.getLogger(__name__)

LOG_FILE = Path("logs") / "cleanup_stale_positions.log"
CONFIRM_TOKEN = "close"


@dataclass(frozen=True)
class PositionRow:
    symbol: str
    qty: float
    market_value: float
    unrealized_pl: float
    unrealized_plpc: float
    current_price: float

    @property
    def abs_qty(self) -> float:
        return abs(self.qty)

    @property
    def is_tiny_fractional(self) -> bool:
        return self.abs_qty < 1.0


def _require_paper_only() -> None:
    if not config.PAPER_TRADING:
        logger.error("Refusing to run: PAPER_TRADING must be true (paper account only)")
        print(
            "ERROR: This script is paper-only.\n"
            "Set PAPER_TRADING=true in .env and use Alpaca paper API keys."
        )
        sys.exit(1)


def _load_positions(executor: AlpacaExecutor) -> list[PositionRow]:
    rows: list[PositionRow] = []
    for pos in executor._get_positions():
        qty = float(pos.qty or 0)
        if abs(qty) < 1e-12:
            continue
        rows.append(
            PositionRow(
                symbol=config.normalize_symbol(pos.symbol),
                qty=qty,
                market_value=float(pos.market_value or 0),
                unrealized_pl=float(pos.unrealized_pl or 0),
                unrealized_plpc=float(pos.unrealized_plpc or 0) * 100.0,
                current_price=float(pos.current_price or pos.avg_entry_price or 0),
            )
        )
    rows.sort(key=lambda r: (r.symbol,))
    return rows


def _print_positions(rows: list[PositionRow]) -> None:
    if not rows:
        print("No open positions.")
        return
    print(f"\nOpen positions ({len(rows)}):")
    print(
        f"{'Symbol':<10} {'Qty':>14} {'Mkt value':>12} {'Unreal P/L':>12} {'P/L %':>8} {'Tiny':>5}"
    )
    print("-" * 66)
    for row in rows:
        tiny = "yes" if row.is_tiny_fractional else ""
        print(
            f"{row.symbol:<10} {row.qty:>14.8f} ${row.market_value:>10,.2f} "
            f"${row.unrealized_pl:>10,.2f} {row.unrealized_plpc:>7.2f}% {tiny:>5}"
        )


def _select_targets(
    rows: list[PositionRow],
    *,
    tickers: list[str] | None,
    tiny_fractionals: bool,
) -> list[PositionRow]:
    if not tickers and not tiny_fractionals:
        return []

    ticker_set = {config.normalize_symbol(t) for t in (tickers or [])}
    selected: list[PositionRow] = []
    for row in rows:
        if tickers and row.symbol in ticker_set:
            selected.append(row)
            continue
        if tiny_fractionals and row.is_tiny_fractional:
            selected.append(row)
    # De-dupe while preserving order
    seen: set[str] = set()
    out: list[PositionRow] = []
    for row in selected:
        if row.symbol in seen:
            continue
        seen.add(row.symbol)
        out.append(row)
    return out


def _confirm_close(targets: list[PositionRow]) -> bool:
    symbols = ", ".join(r.symbol for r in targets)
    total_mv = sum(r.market_value for r in targets)
    print(f"\nAbout to close {len(targets)} position(s): {symbols}")
    print(f"Combined market value: ${total_mv:,.2f}")
    print(f"Type '{CONFIRM_TOKEN}' to confirm (Ctrl+C to abort): ", end="", flush=True)
    try:
        answer = input().strip().lower()
    except KeyboardInterrupt:
        print("\nAborted.")
        return False
    if answer != CONFIRM_TOKEN:
        print("Confirmation failed — no orders submitted.")
        return False
    return True


def _close_positions(
    executor: AlpacaExecutor,
    targets: list[PositionRow],
    *,
    execute: bool,
    use_qty_exit: bool = False,
) -> list[dict]:
    results: list[dict] = []
    for row in targets:
        qty_exit = use_qty_exit or row.is_tiny_fractional
        action = {
            "symbol": row.symbol,
            "qty": row.qty,
            "market_value": round(row.market_value, 2),
            "execute": execute,
            "ok": False,
            "skipped": False,
            "reason": "",
            "exit_mode": "qty" if qty_exit else "notional",
        }
        if not execute:
            action["skipped"] = True
            action["reason"] = "dry_run"
            logger.info(
                "dry-run would close %s qty=%.8f mv=$%.2f mode=%s",
                row.symbol,
                row.qty,
                row.market_value,
                action["exit_mode"],
            )
            results.append(action)
            continue

        if qty_exit:
            order = executor.execute_full_exit_qty(
                row.symbol, reason="stale_cleanup_qty", sleeve="cleanup"
            )
        else:
            order = executor.execute_full_exit(
                row.symbol, reason="stale_cleanup", sleeve="cleanup"
            )
        if order is None:
            action["skipped"] = True
            action["reason"] = "below_min_notional_or_no_position"
            logger.info(
                "skip close %s qty=%.8f mv=$%.2f mode=%s (guard or no position)",
                row.symbol,
                row.qty,
                row.market_value,
                action["exit_mode"],
            )
        else:
            action["ok"] = True
            order_id = getattr(order, "id", None)
            action["order_id"] = order_id
            logger.info(
                "closed %s qty=%.8f mv=$%.2f mode=%s order_id=%s",
                row.symbol,
                row.qty,
                row.market_value,
                action["exit_mode"],
                order_id,
            )
            log_event(
                "stale_position_close",
                symbol=row.symbol,
                qty=row.qty,
                market_value=round(row.market_value, 2),
                order_id=order_id,
                exit_mode=action["exit_mode"],
            )
        results.append(action)
    return results


def _print_results(results: list[dict], *, execute: bool) -> None:
    if not results:
        return
    mode = "EXECUTE" if execute else "DRY-RUN"
    print(f"\n{mode} summary:")
    for row in results:
        sym = row["symbol"]
        if row.get("ok"):
            mode = row.get("exit_mode", "notional")
            print(f"  OK   {sym} — order submitted ({mode}, id={row.get('order_id')})")
        elif row.get("skipped"):
            print(f"  SKIP {sym} — {row.get('reason', 'skipped')}")
        else:
            print(f"  FAIL {sym}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List and optionally close stale/dust Alpaca PAPER positions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/cleanup_stale_positions.py\n"
            "  python scripts/cleanup_stale_positions.py --tickers AAPL\n"
            "  python scripts/cleanup_stale_positions.py --tiny-fractionals\n"
            "  python scripts/cleanup_stale_positions.py --tickers AAPL AMD INTC --execute\n"
            "  python scripts/cleanup_stale_positions.py --tiny-fractionals --execute\n"
        ),
    )
    parser.add_argument(
        "--qty-exit",
        action="store_true",
        help="Sell exact share qty (auto for |qty|<1; fixes notional rounding on dust)",
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        metavar="SYM",
        help="Close specific tickers (e.g. AAPL CVX)",
    )
    parser.add_argument(
        "--tiny-fractionals",
        action="store_true",
        help="Close all positions with |qty| < 1 (dust/fractional leftovers)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help=f"Submit close orders (default: dry-run). Requires typing '{CONFIRM_TOKEN}' to confirm.",
    )
    args = parser.parse_args()

    setup_logging(log_dir=Path("logs"))
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logging.getLogger().addHandler(file_handler)

    _require_paper_only()

    st = alpaca_env_status()
    if not st["credentials_ready"]:
        print(format_missing_env_message())
        sys.exit(1)

    logger.info("cleanup_stale_positions started execute=%s", args.execute)
    print(f"--- Alpaca PAPER cleanup (dry-run={'off' if args.execute else 'on'}) ---")
    print(f"Log file: {LOG_FILE.resolve()}")

    try:
        executor = AlpacaExecutor(paper=True)
    except AlpacaAuthError as exc:
        logger.error("Alpaca auth failed: %s", exc)
        print(f"ERROR: Alpaca authentication failed: {exc}")
        print("Verify APCA_API_KEY_ID / APCA_API_SECRET_KEY in .env (paper keys).")
        sys.exit(1)
    except RuntimeError as exc:
        logger.error("Alpaca init failed: %s", exc)
        print(f"ERROR: {exc}")
        sys.exit(1)

    account = executor._get_account()
    equity = float(account.equity)
    print(f"Equity: ${equity:,.2f}  Cash: ${float(account.cash):,.2f}")

    rows = _load_positions(executor)
    _print_positions(rows)

    if not args.tickers and not args.tiny_fractionals:
        print(
            "\nDry-run list only. To plan closes:\n"
            "  --tickers AAPL          close one symbol\n"
            "  --tiny-fractionals      close all |qty| < 1\n"
            f"Add --execute and type '{CONFIRM_TOKEN}' to submit orders."
        )
        return

    targets = _select_targets(
        rows,
        tickers=args.tickers,
        tiny_fractionals=args.tiny_fractionals,
    )
    if not targets:
        print("\nNo matching positions to close.")
        return

    print("\nSelected for close:")
    _print_positions(targets)

    if args.execute and not _confirm_close(targets):
        logger.info("user aborted confirmation")
        return

    results = _close_positions(
        executor, targets, execute=args.execute, use_qty_exit=args.qty_exit
    )
    _print_results(results, execute=args.execute)

    if not args.execute:
        print(f"\nDry-run only — re-run with --execute and type '{CONFIRM_TOKEN}' to submit.")
    else:
        remaining = _load_positions(executor)
        print(f"\nRemaining open positions: {len(remaining)}")
        logger.info(
            "cleanup finished closed=%s skipped=%s remaining=%s",
            sum(1 for r in results if r.get("ok")),
            sum(1 for r in results if r.get("skipped")),
            len(remaining),
        )


if __name__ == "__main__":
    main()
