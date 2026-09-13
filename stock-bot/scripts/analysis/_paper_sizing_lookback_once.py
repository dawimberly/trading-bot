"""One-off: paper Alpaca open-book sizing + recent fills + winners/losers."""

from __future__ import annotations

import json
import statistics as st
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / "data/portal/users/dawimberly/books/alpaca_paper/.env", override=True)

import config  # noqa: E402
from modules.alpaca_client import call_with_retry, get_trading_client  # noqa: E402
from alpaca.trading.enums import QueryOrderStatus  # noqa: E402
from alpaca.trading.requests import GetOrdersRequest  # noqa: E402

CORE = {"VTI", "ITOT", "SCHB"}
SPY = {"SPY", "VOO", "IVV", "QQQ"}
METAL = {
    "GLD",
    "IAU",
    "SLV",
    "GDX",
    "GDXJ",
    "PPLT",
    "PALL",
    "NEM",
    "GOLD",
    "FNV",
    "AEM",
    "WPM",
}


def sleeve(sym: str) -> str:
    if sym in CORE:
        return "vti_core"
    if sym in SPY:
        return "spy"
    if sym in METAL:
        return "metalish"
    if "USD" in sym:
        return "crypto"
    return "nyse"


def main() -> None:
    client = get_trading_client(paper=True)
    acct = call_with_retry(lambda: client.get_account(), op_name="get_account")
    eq = float(acct.equity)
    cash = float(acct.cash)
    print("PAPER ACCOUNT equity", eq, "cash", cash)

    positions = call_with_retry(lambda: client.get_all_positions(), op_name="positions")
    rows = []
    for p in positions:
        mv = float(p.market_value)
        rows.append(
            {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "mv": mv,
                "cost": float(p.cost_basis),
                "upnl": float(p.unrealized_pl),
                "upct": float(p.unrealized_plpc) * 100,
                "pct_eq": 100.0 * mv / eq if eq else 0.0,
                "entry": float(p.avg_entry_price),
                "px": float(p.current_price),
                "sleeve": sleeve(p.symbol),
            }
        )
    rows.sort(key=lambda x: -x["upnl"])
    print("n_pos", len(rows), "sum_mv", round(sum(r["mv"] for r in rows), 2))

    by: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "mv": 0.0, "upnl": 0.0, "pct_eq": 0.0}
    )
    for r in rows:
        b = by[r["sleeve"]]
        b["n"] += 1
        b["mv"] += r["mv"]
        b["upnl"] += r["upnl"]
        b["pct_eq"] += r["pct_eq"]

    print("SLEEVES:")
    for k, v in sorted(by.items(), key=lambda kv: -kv[1]["mv"]):
        print(
            f"  {k}: n={v['n']} mv={v['mv']:.2f} ({100 * v['mv'] / eq:.1f}%eq) "
            f"upnl={v['upnl']:+.2f}"
        )

    non_vti = [r for r in rows if r["sleeve"] != "vti_core"]
    pcts = [r["pct_eq"] for r in non_vti]
    print("NON-VTI open sizing %eq n=", len(pcts))
    if pcts:
        pcts_s = sorted(pcts)
        print(
            "  median",
            round(st.median(pcts), 3),
            "mean",
            round(st.mean(pcts), 3),
            "p25",
            round(pcts_s[max(0, int(0.25 * len(pcts_s)) - 1)], 3),
            "p75",
            round(pcts_s[min(len(pcts_s) - 1, int(0.75 * len(pcts_s)))], 3),
            "max",
            round(max(pcts), 3),
        )
        print(
            "  median_$",
            round(st.median([r["mv"] for r in non_vti]), 2),
            "mean_$",
            round(st.mean([r["mv"] for r in non_vti]), 2),
        )

    print("\nTOP 12 by unrealized PnL:")
    for r in rows[:12]:
        print(
            f"  {r['symbol']:8s} {r['sleeve']:9s} mv={r['mv']:10.2f} "
            f"({r['pct_eq']:5.2f}%eq) upnl={r['upnl']:+8.2f} ({r['upct']:+6.2f}%)"
        )
    print("\nWORST 10:")
    for r in rows[-10:]:
        print(
            f"  {r['symbol']:8s} {r['sleeve']:9s} mv={r['mv']:10.2f} "
            f"({r['pct_eq']:5.2f}%eq) upnl={r['upnl']:+8.2f} ({r['upct']:+6.2f}%)"
        )

    req = GetOrdersRequest(
        status=QueryOrderStatus.CLOSED,
        limit=500,
        after=datetime.now(timezone.utc) - timedelta(days=21),
    )
    orders = call_with_retry(lambda: client.get_orders(filter=req), op_name="orders")
    fills = []
    for o in orders:
        fq = float(o.filled_qty or 0)
        fp = float(o.filled_avg_price or 0)
        if fq <= 0 or fp <= 0:
            continue
        n = fq * fp
        fills.append(
            {
                "ts": str(o.filled_at or o.submitted_at),
                "symbol": o.symbol,
                "side": str(o.side).split(".")[-1].lower(),
                "notional": round(n, 2),
                "qty": fq,
                "price": fp,
                "pct_eq": round(100 * n / eq, 3) if eq else None,
                "sleeve": sleeve(o.symbol),
            }
        )
    buys = [f for f in fills if f["side"] == "buy"]
    sells = [f for f in fills if f["side"] == "sell"]
    print("\nfilled orders", len(fills), "buys", len(buys), "sells", len(sells))
    buy_sizing = {}
    if buys:
        b_n = [f["notional"] for f in buys]
        b_pct = [f["pct_eq"] for f in buys if f["pct_eq"] is not None]
        buy_sizing = {
            "n": len(buys),
            "median_notional": round(st.median(b_n), 2),
            "mean_notional": round(st.mean(b_n), 2),
            "median_pct_eq": round(st.median(b_pct), 3),
            "mean_pct_eq": round(st.mean(b_pct), 3),
            "p25_pct_eq": round(sorted(b_pct)[max(0, int(0.25 * len(b_pct)) - 1)], 3),
            "p75_pct_eq": round(
                sorted(b_pct)[min(len(b_pct) - 1, int(0.75 * len(b_pct)))], 3
            ),
        }
        print("BUY sizing (notional / current equity proxy):", buy_sizing)
        print("top buy symbols", Counter(f["symbol"] for f in buys).most_common(12))
        by_sl: dict[str, list] = defaultdict(list)
        for f in buys:
            by_sl[f["sleeve"]].append(f["pct_eq"])
        print("buy %eq by sleeve:")
        for k, vals in sorted(by_sl.items(), key=lambda kv: -len(kv[1])):
            print(
                f"  {k}: n={len(vals)} median%={st.median(vals):.3f} "
                f"mean%={st.mean(vals):.3f}"
            )

    # portfolio history
    hist_ret = None
    try:
        hist = call_with_retry(
            lambda: client.get_portfolio_history(period="1M", timeframe="1D"),
            op_name="hist",
        )
        eq_hist = [float(x) for x in (hist.equity or []) if x]
        if len(eq_hist) >= 2 and eq_hist[0] > 0:
            hist_ret = round(100 * (eq_hist[-1] / eq_hist[0] - 1), 3)
            print("1M equity", eq_hist[0], "->", eq_hist[-1], f"ret={hist_ret}%")
    except Exception as exc:
        print("hist err", exc)

    live_eq = 308.0
    paper_median_pct = (
        buy_sizing.get("median_pct_eq")
        or (st.median(pcts) if pcts else None)
    )
    mapped = None
    if paper_median_pct:
        mapped = {
            "paper_median_buy_pct_eq": paper_median_pct,
            "live_clip_at_same_pct": round(live_eq * paper_median_pct / 100.0, 2),
            "paper_per_name_cap_env": 0.025,
            "live_at_2_5pct": round(live_eq * 0.025, 2),
            "paper_nyse_cap_env": 0.20,
            "live_nyse_at_20pct": round(live_eq * 0.20, 2),
            "note": "Scale paper % of equity onto ~$308 live book",
        }
        print("\nMAP TO LIVE (~$308):", mapped)

    out = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "equity": eq,
        "cash": cash,
        "cash_pct": round(100 * cash / eq, 2) if eq else None,
        "sleeves": {k: dict(v) for k, v in by.items()},
        "sizing_open_non_vti": {
            "n": len(pcts),
            "median_pct_eq": round(st.median(pcts), 3) if pcts else None,
            "mean_pct_eq": round(st.mean(pcts), 3) if pcts else None,
            "p75_pct_eq": round(
                sorted(pcts)[min(len(pcts) - 1, int(0.75 * len(pcts)))], 3
            )
            if pcts
            else None,
            "max_pct_eq": round(max(pcts), 3) if pcts else None,
            "median_mv": round(st.median([r["mv"] for r in non_vti]), 2)
            if non_vti
            else None,
        },
        "top": rows[:15],
        "worst": rows[-10:],
        "buy_sizing_21d": buy_sizing,
        "recent_buys": buys[:40],
        "hist_1m_return_pct": hist_ret,
        "map_to_live": mapped,
        "paper_env": {
            "PAPER_NYSE_SLEEVE_CAP_PCT": "0.20",
            "PAPER_NYSE_PER_NAME_MAX_PCT": "0.025",
            "PAPER_NYSE_MAX_OF_SLEEVE_PCT": "0.15",
            "PAPER_VTI_CORE_PCT": "0.40",
            "DYNAMIC_VTI_DEFAULT_PCT": "0.45",
            "PAPER_MAX_EQUITY_TRADES": "3",
        },
    }
    out_path = ROOT / "data/paper_sizing_lookback_alpaca.json"
    out_path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print("wrote", out_path)


if __name__ == "__main__":
    main()
