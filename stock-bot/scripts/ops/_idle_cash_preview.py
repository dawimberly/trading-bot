"""Read-only preview of Monday idle-cash scale-in (Lab / Medium). Never orders."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ["PYTHONTRADING_ROOT"] = str(ROOT)

BOOK = (os.environ.get("PREVIEW_BOOK") or "").strip()
if BOOK not in {"alpaca_paper", "alpaca_paper_v2"}:
    raise SystemExit("set PREVIEW_BOOK=alpaca_paper or alpaca_paper_v2")

from modules.portal_paths import bind_project_root

bind_project_root(ROOT)
from modules.portal_bot import user_bot_env

for key, val in user_bot_env("dawimberly", BOOK).items():
    if val is not None:
        os.environ[str(key)] = str(val)

import config
from modules.alpaca_client import get_trading_client, reset_trading_client_cache

book_env = Path(os.environ["PYTHONTRADING_ENV_FILE"])
config.reload_from_env(str(book_env), book_scoped=True)
config.isolate_book_alpaca_os_environ(book_env)
config.set_paper_aggressive_context(True)
reset_trading_client_cache()


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def main() -> None:
    client = get_trading_client()
    acct = client.get_account()
    equity = _f(acct.equity)
    cash = _f(acct.cash)
    cash_pct = cash / equity if equity else 0.0
    positions = list(client.get_all_positions())
    creds = config.alpaca_credentials_status()
    print(f"  keys {creds.get('key_source')} …{creds.get('key_suffix')}")
    cap_pct = config.effective_per_name_max_pct()
    chunk = round(equity * config.effective_risk_per_trade(equity), 2)
    if config.paper_deploy_aggressive(cash_pct, equity=equity, cash=cash):
        chunk = round(chunk * 1.50, 2)
    slots = config.effective_max_active_tickers()
    active = []
    for pos in positions:
        qty = _f(pos.qty)
        if qty <= 0:
            continue
        sym = config.normalize_symbol(getattr(pos, "symbol", ""))
        if sym == "VTI":
            continue
        val = _f(getattr(pos, "market_value", 0))
        entry = _f(getattr(pos, "avg_entry_price", 0))
        px = _f(getattr(pos, "current_price", 0))
        gain = (px - entry) / entry if entry > 0 and px > 0 else None
        room = config.concentration_buy_room(
            current_val=val,
            equity=equity,
            has_position=True,
            slots_full=True,
            extra_ok=config.paper_lab_extra_add_ok(gain_pct=gain),
        )
        disaster = abs(
            config.paper_lab_disaster_pct()
            if config.paper_lab_concentrated_enabled()
            else 0.10
        )
        knife = gain is not None and gain <= -disaster
        active.append(
            {
                "symbol": sym,
                "value": val,
                "gain": gain,
                "room": room,
                "knife": knife,
                "add": 0.0 if knife else min(room, chunk) if room >= 1 else 0.0,
            }
        )
    active.sort(key=lambda r: (r["add"] <= 0, -(r["gain"] or -9)))
    can_add = [r for r in active if r["add"] > 0]
    fair = round(cash * 0.95 / len(can_add), 2) if can_add else 0.0
    for row in can_add:
        row["add"] = min(row["room"], max(chunk, fair))
    label = "Lab" if BOOK == "alpaca_paper" else "Medium"
    print(f"{label} {BOOK}")
    print(
        f"  equity ${equity:,.2f}  cash ${cash:,.2f} ({cash_pct:.1%})  "
        f"names {len(active)}/{slots}  per-name cap {cap_pct:.0%}  "
        f"ticket ~${chunk:,.0f}  fair-share ~${fair:,.0f}"
    )
    print(
        f"  scale-in active: {config.paper_idle_cash_scale_in_active(equity=equity, cash=cash)}"
    )
    planned = 0.0
    cash_left = cash
    adds = []
    for row in can_add:
        size = min(row["add"], cash_left)
        if size < 1:
            continue
        row["add"] = round(size, 2)
        planned += size
        cash_left -= size
        adds.append(row)
    print(f"  would add ${planned:,.2f} across {len(adds)} held names  leftover cash ${cash_left:,.2f}")
    for row in active:
        gain_s = "n/a" if row["gain"] is None else f"{row['gain']:+.1%}"
        flag = "SKIP knife" if row["knife"] else ("ADD" if row in adds else "no room")
        print(
            f"    {row['symbol']:<6}  ${row['value']:>10,.2f}  {gain_s:>8}  "
            f"room ${row['room']:>9,.2f}  {flag}"
            + (f"  +${row['add']:,.2f}" if row in adds else "")
        )


if __name__ == "__main__":
    main()
