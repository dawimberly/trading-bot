#!/usr/bin/env python3
"""AI-burst quarterly crash-hedge backtest (QQQ / SPY) — research only.

Simulates the book-hedge bucket from docs/AI_BURST_PLAYBOOK.md:
  - Each quarter: buy ~150 DTE, 10-15% OTM long put (OTM fallback; no historical greeks)
  - Premium <= 0.75% of $100k book ($750) per quarter; 1 contract; no pyramiding
  - Exit at 2x premium or 45 DTE remaining, whichever first
  - Friction per side on option fills

No orders. No .env / sleeve / equity-bot changes. Not promote-ready.

Run (from stock-bot/):
  python scripts/research/ai_burst_hedge_backtest.py
  python scripts/research/ai_burst_hedge_backtest.py --underlying SPY

Writes:
  scripts/research/ai_burst_hedge_trades_QQQ.csv
  scripts/research/ai_burst_hedge_trades_SPY.csv
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env", override=False)

import config  # noqa: E402

OPTIONS_HISTORY_START = date(2024, 2, 1)
BOOK_EQUITY = 100_000.0
QUARTERLY_CAP_PCT = 0.0075
QUARTERLY_CAP_USD = BOOK_EQUITY * QUARTERLY_CAP_PCT
VTI_WEIGHT = 0.60
EQUITY_WEIGHT = 0.40  # remainder = underlying (QQQ or SPY)
ENTRY_SLIP_SESSIONS = 10  # sparse Alpaca option dailies — first real bar after quarter open
MULT = 100
DTE_MIN = 120
DTE_MAX = 180
DTE_TARGET = 150
DTE_FORCE_EXIT = 45
OTM_LO = 0.10
OTM_HI = 0.15
OTM_TARGET = 0.125
OTM_HARD_LO = 0.08
OTM_HARD_HI = 0.18
TP_MULT = 2.0
DEFAULT_FRICTION = 0.05
OUT_DIR = Path(__file__).resolve().parent

DATA_CONTRACT = """
Minimum data contract (cannot fake fills without this):
  1. Alpaca keys in .env (APCA_* or PAPER_APCA_*) — market data only.
  2. Options bars since ~2024-02-01: GET /v1beta1/options/bars (OCC symbols).
  3. Contract master (active + inactive puts):
     GET /v2/options/contracts?underlying_symbols=QQQ&type=put
  4. Daily equity bars for QQQ/SPY (strike selection) and VTI (overlay).
  5. At least one quarterly entry with a put in ~120-180 DTE, 10-15% OTM band,
     whose option bar exists on entry and through exit (2x or 45 DTE).

Not available (this run does not use them):
  - Historical as-of greeks / IV (0.25 delta fallback unused; OTM% only)
  - Live bid-ask (daily close +/- friction)
"""


@dataclass
class Contract:
    symbol: str
    expiration: date
    strike: float


@dataclass
class HedgeTrade:
    quarter: str
    underlying: str
    entry_date: date
    occ: str
    strike: float
    expiry: date
    contracts: int
    premium_in: float
    premium_spend_usd: float
    cap_usd: float
    exit_date: date
    exit_reason: str
    premium_out: float
    pnl_usd: float
    pnl_pct: float
    dte_at_entry: int
    spot_at_entry: float
    otm_pct: float
    fill_note: str


@dataclass
class QuarterRow:
    quarter: str
    entry_date: date | None
    spend_usd: float
    cap_usd: float
    pct_of_cap: float
    status: str
    note: str = ""


def _fail(msg: str, *, code: int = 2) -> int:
    print(msg.rstrip() + "\n" + DATA_CONTRACT, file=sys.stderr)
    return code


def _keys() -> tuple[str, str]:
    try:
        return config.get_alpaca_credentials(paper=True)
    except Exception:
        return config.get_alpaca_credentials(paper=False)


def _quarter_label(d: date) -> str:
    q = (d.month - 1) // 3 + 1
    return f"{d.year}-Q{q}"


def fetch_equity_daily(symbol: str, start: date, end: date) -> pd.DataFrame:
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    key, secret = _keys()
    client = StockHistoricalDataClient(api_key=key, secret_key=secret)
    last_err = None
    df = pd.DataFrame()
    for feed in (DataFeed.IEX, DataFeed.SIP, None):
        kwargs = dict(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=datetime(start.year, start.month, start.day, tzinfo=timezone.utc),
            end=datetime(end.year, end.month, end.day, tzinfo=timezone.utc)
            + timedelta(days=1),
        )
        if feed is not None:
            kwargs["feed"] = feed
        try:
            bars = client.get_stock_bars(StockBarsRequest(**kwargs))
            df = bars.df
            if df is not None and not df.empty:
                break
        except Exception as exc:
            last_err = exc
            df = pd.DataFrame()
    if df is None or df.empty:
        raise RuntimeError(last_err or f"empty {symbol} daily bars")
    if isinstance(df.index, pd.MultiIndex):
        df = df.reset_index()
        if "symbol" in df.columns:
            df = df[df["symbol"] == symbol]
    else:
        df = df.reset_index()
    ts_col = "timestamp" if "timestamp" in df.columns else df.columns[0]
    df["session_date"] = (
        pd.to_datetime(df[ts_col], utc=True).dt.tz_convert("America/New_York").dt.date
    )
    df = df.sort_values("session_date").drop_duplicates("session_date")
    return df.set_index("session_date")


def _page_contracts(trading, **kwargs) -> list:
    from alpaca.trading.requests import GetOptionContractsRequest

    out = []
    page = None
    while True:
        req = GetOptionContractsRequest(page_token=page, **kwargs)
        resp = trading.get_option_contracts(req)
        out.extend(list(resp.option_contracts or []))
        page = resp.next_page_token
        if not page:
            break
    return out


def list_puts(trading, underlying: str, start_exp: date, end_exp: date) -> list[Contract]:
    from alpaca.trading.enums import AssetStatus, ContractType

    found: dict[str, Contract] = {}
    cursor = start_exp
    while cursor <= end_exp:
        chunk_end = min(cursor + timedelta(days=90), end_exp)
        for status in (AssetStatus.ACTIVE, AssetStatus.INACTIVE):
            raw = _page_contracts(
                trading,
                underlying_symbols=[underlying],
                status=status,
                type=ContractType.PUT,
                expiration_date_gte=cursor.isoformat(),
                expiration_date_lte=chunk_end.isoformat(),
                limit=10000,
            )
            for c in raw:
                if c.underlying_symbol != underlying:
                    continue
                found[c.symbol] = Contract(
                    symbol=c.symbol,
                    expiration=c.expiration_date,
                    strike=float(c.strike_price),
                )
        cursor = chunk_end + timedelta(days=1)
    return sorted(found.values(), key=lambda x: (x.expiration, x.strike))


def fetch_option_daily(
    opt_client, occ: str, start: date, end: date, feed: str | None
) -> pd.DataFrame:
    from alpaca.data.models.bars import BarSet
    from alpaca.data.requests import OptionBarsRequest
    from alpaca.data.timeframe import TimeFrame

    req = OptionBarsRequest(
        symbol_or_symbols=occ,
        timeframe=TimeFrame.Day,
        start=datetime(start.year, start.month, start.day),
        end=datetime(end.year, end.month, end.day) + timedelta(days=1),
    )
    fields = req.to_request_fields()
    if feed:
        fields["feed"] = feed
        raw = opt_client._get_marketdata(  # noqa: SLF001
            path="/options/bars", params=fields, page_size=10_000
        )
        bars = BarSet(raw)
    else:
        bars = opt_client.get_option_bars(req)
    df = bars.df
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.index, pd.MultiIndex):
        df = df.reset_index()
    else:
        df = df.reset_index()
    ts_col = "timestamp" if "timestamp" in df.columns else df.columns[0]
    df["session_date"] = (
        pd.to_datetime(df[ts_col], utc=True).dt.tz_convert("America/New_York").dt.date
    )
    return df.sort_values("session_date").drop_duplicates("session_date").set_index(
        "session_date"
    )


def quarter_first_sessions(sessions: list[date]) -> list[date]:
    seen: set[tuple[int, int]] = set()
    out: list[date] = []
    for d in sessions:
        key = (d.year, (d.month - 1) // 3)
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def ranked_puts(
    puts: list[Contract], entry: date, spot: float
) -> tuple[list[Contract], str]:
    lo = entry + timedelta(days=DTE_MIN)
    hi = entry + timedelta(days=DTE_MAX)
    window = [c for c in puts if lo <= c.expiration <= hi]
    if not window:
        return [], "no_put_in_dte_window"
    target_k = spot * (1.0 - OTM_TARGET)
    band = [
        c
        for c in window
        if OTM_LO - 1e-9 <= (1.0 - c.strike / spot) <= OTM_HI + 1e-9
    ]
    pool = band if band else window
    monthlies = [
        c
        for c in pool
        if c.expiration.weekday() == 4 and 15 <= c.expiration.day <= 21
    ]
    if monthlies:
        pool = monthlies

    def score(c: Contract) -> tuple:
        dte = (c.expiration - entry).days
        otm = 1.0 - c.strike / spot
        hard = 0 if OTM_HARD_LO <= otm <= OTM_HARD_HI else 1
        return (hard, abs(dte - DTE_TARGET), abs(c.strike - target_k))

    ranked = sorted(pool, key=score)
    ranked = [
        c for c in ranked if OTM_HARD_LO <= (1.0 - c.strike / spot) <= OTM_HARD_HI
    ]
    if not ranked:
        return [], "strike_outside_8_18_otm"
    note = "otm_10_15" if band else "otm_nearest_in_8_18"
    if monthlies:
        note += ";monthly"
    note += ";no_historical_greeks"
    return ranked, note


def apply_friction(mid: float, *, buy: bool, friction: float) -> float:
    px = mid + friction if buy else max(mid - friction, 0.01)
    return max(px, 0.01)


def simulate_hedge_exit(
    bars: pd.DataFrame,
    *,
    entry: date,
    expiry: date,
    friction: float,
) -> tuple[date, str, float, str] | None:
    if entry not in bars.index:
        return None
    mid_in = float(bars.loc[entry]["close"])
    if mid_in <= 0:
        return None
    premium_in = apply_friction(mid_in, buy=True, friction=friction)
    tp = premium_in * TP_MULT
    after = bars.loc[bars.index > entry]
    for sess, row in after.iterrows():
        dte_left = (expiry - sess).days
        high = float(row["high"])
        close = float(row["close"])
        if high >= tp:
            out = apply_friction(tp, buy=False, friction=friction)
            return sess, f"take_profit_{TP_MULT:g}x", out, "high_crossed_2x"
        if dte_left <= DTE_FORCE_EXIT:
            out = apply_friction(close, buy=False, friction=friction)
            return sess, f"dte_{DTE_FORCE_EXIT}", out, "calendar_exit"
    if after.empty:
        return None
    last = after.index[-1]
    out = apply_friction(float(after.loc[last]["close"]), buy=False, friction=friction)
    return last, "last_bar", out, "no_rule_hit"


def out_csv_path(underlying: str) -> Path:
    return OUT_DIR / f"ai_burst_hedge_trades_{underlying}.csv"


def probe_option_feed(
    opt_client, puts: list[Contract], start: date, end: date
) -> tuple[bool, str | None]:
    """Return (ok, feed). feed is None when the untagged SDK path has bars."""
    sample = list(puts[:60])
    if len(puts) > 60:
        for i in (len(puts) // 4, len(puts) // 2, (3 * len(puts)) // 4, len(puts) - 1):
            sample.append(puts[i])
    for contract in sample:
        probe_end = min(end, contract.expiration)
        for try_feed in (None, "indicative", "opra"):
            try:
                df = fetch_option_daily(
                    opt_client, contract.symbol, start, probe_end, try_feed
                )
                if len(df) >= 1:
                    return True, try_feed
            except Exception:
                continue
    return False, None


def run_backtest(
    underlying: str,
    *,
    friction: float,
    years: float,
) -> tuple[int, list[HedgeTrade], list[QuarterRow], dict]:
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.trading.client import TradingClient

    today = datetime.now(timezone.utc).date()
    want_start = today - timedelta(days=int(365 * years))
    start = max(want_start, OPTIONS_HISTORY_START)
    end = today

    print("=" * 72)
    print(f"AI-burst hedge backtest — {underlying} (no orders)")
    print(f"book: ${BOOK_EQUITY:,.0f}  quarterly cap: ${QUARTERLY_CAP_USD:.2f} (0.75%)")
    print(f"window request: {years:.1f}y  effective: {start} -> {end}")
    print(f"put: ~{DTE_TARGET} DTE, 10-15% OTM | exit 2x or {DTE_FORCE_EXIT} DTE")
    print(f"friction: ${friction:.2f}/share/side")
    print()

    try:
        und_df = fetch_equity_daily(underlying, start, end)
        vti_df = fetch_equity_daily("VTI", start, end)
    except Exception as exc:
        return _fail(f"Equity bars failed: {exc}"), [], [], {}

    if und_df.empty or vti_df.empty:
        return _fail("Empty VTI or underlying equity bars."), [], [], {}

    print(f"{underlying} sessions: {und_df.index.min()} -> {und_df.index.max()} ({len(und_df)})")
    print(f"VTI sessions: {vti_df.index.min()} -> {vti_df.index.max()} ({len(vti_df)})")

    key, secret = _keys()
    trading = TradingClient(key, secret, paper=True)
    opt_client = OptionHistoricalDataClient(api_key=key, secret_key=secret)

    try:
        puts = list_puts(
            trading,
            underlying,
            start + timedelta(days=DTE_MIN),
            end + timedelta(days=DTE_MAX + 30),
        )
    except Exception as exc:
        return _fail(f"Option contract list failed: {exc}"), [], [], {}

    if not puts:
        return _fail(f"No {underlying} puts in contract master."), [], [], {}

    print(f"puts listed: {len(puts)}  expiries {puts[0].expiration} -> {puts[-1].expiration}")
    ok_feed, feed = probe_option_feed(opt_client, puts, start, end)
    if not ok_feed:
        return _fail(
            f"No {underlying} option daily bars in sample (data too sparse or no subscription)."
        ), [], [], {}

    print(f"option feed: {feed or 'sdk_default'}")
    print()

    sessions = sorted(set(und_df.index) & set(vti_df.index))
    q_entries = quarter_first_sessions(sessions)
    trades: list[HedgeTrade] = []
    quarter_rows: list[QuarterRow] = []
    bar_cache: dict[str, pd.DataFrame] = {}
    in_position_until: date | None = None

    for q_anchor in q_entries:
        qlabel = _quarter_label(q_anchor)
        if in_position_until and q_anchor <= in_position_until:
            quarter_rows.append(
                QuarterRow(
                    quarter=qlabel,
                    entry_date=None,
                    spend_usd=0.0,
                    cap_usd=QUARTERLY_CAP_USD,
                    pct_of_cap=0.0,
                    status="skipped",
                    note="prior hedge still open",
                )
            )
            continue

        # Actual entry: first session with an option bar within slip window.
        q_idx = sessions.index(q_anchor)
        entry_candidates = sessions[q_idx : q_idx + ENTRY_SLIP_SESSIONS + 1]
        filled_this_q = False

        for entry in entry_candidates:
            spot = float(und_df.loc[entry]["close"])
            ranked, pick_note = ranked_puts(puts, entry, spot)
            if not ranked:
                continue

            chosen = None
            bars = pd.DataFrame()
            for contract in ranked[:10]:
                occ = contract.symbol
                if occ not in bar_cache:
                    try:
                        bar_cache[occ] = fetch_option_daily(
                            opt_client, occ, entry, contract.expiration, feed
                        )
                    except Exception:
                        continue
                cand = bar_cache.get(occ, pd.DataFrame())
                if cand.empty or entry not in cand.index:
                    continue
                mid = float(cand.loc[entry]["close"])
                if mid <= 0:
                    continue
                premium_in = apply_friction(mid, buy=True, friction=friction)
                spend = premium_in * MULT
                if spend > QUARTERLY_CAP_USD + 1e-6:
                    continue
                chosen = contract
                bars = cand
                break

            if chosen is None:
                continue

            slip_note = ""
            if entry != q_anchor:
                slip_note = f";entry_slip={entry}_from_{q_anchor}"

            sim = simulate_hedge_exit(
                bars, entry=entry, expiry=chosen.expiration, friction=friction
            )
            if sim is None:
                continue

            exit_date, reason, premium_out, fill_note = sim
            premium_in = apply_friction(
                float(bars.loc[entry]["close"]), buy=True, friction=friction
            )
            spend = premium_in * MULT
            pnl = (premium_out - premium_in) * MULT
            trades.append(
                HedgeTrade(
                    quarter=qlabel,
                    underlying=underlying,
                    entry_date=entry,
                    occ=chosen.symbol,
                    strike=chosen.strike,
                    expiry=chosen.expiration,
                    contracts=1,
                    premium_in=premium_in,
                    premium_spend_usd=spend,
                    cap_usd=QUARTERLY_CAP_USD,
                    exit_date=exit_date,
                    exit_reason=reason,
                    premium_out=premium_out,
                    pnl_usd=pnl,
                    pnl_pct=(premium_out - premium_in) / premium_in if premium_in else 0.0,
                    dte_at_entry=(chosen.expiration - entry).days,
                    spot_at_entry=spot,
                    otm_pct=1.0 - chosen.strike / spot,
                    fill_note=f"{pick_note};{fill_note}{slip_note}",
                )
            )
            quarter_rows.append(
                QuarterRow(
                    quarter=qlabel,
                    entry_date=entry,
                    spend_usd=spend,
                    cap_usd=QUARTERLY_CAP_USD,
                    pct_of_cap=100.0 * spend / QUARTERLY_CAP_USD,
                    status="filled",
                    note=reason,
                )
            )
            in_position_until = exit_date
            filled_this_q = True
            break

        if not filled_this_q:
            quarter_rows.append(
                QuarterRow(
                    quarter=qlabel,
                    entry_date=q_anchor,
                    spend_usd=0.0,
                    cap_usd=QUARTERLY_CAP_USD,
                    pct_of_cap=0.0,
                    status="skipped",
                    note=f"no_option_bar_within_{ENTRY_SLIP_SESSIONS}d_or_over_cap",
                )
            )

    if not trades:
        sample = [f"{r.quarter}: {r.note}" for r in quarter_rows[:8]]
        return (
            _fail("Zero hedge trades — not inventing a curve.\n" + "\n".join(sample)),
            [],
            quarter_rows,
            {},
        )

    meta = {
        "underlying": underlying,
        "start": start,
        "end": end,
        "und_df": und_df,
        "vti_df": vti_df,
        "sessions": sessions,
        "bar_cache": bar_cache,
        "trades": trades,
    }
    return 0, trades, quarter_rows, meta


def _as_date(x) -> date:
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return pd.Timestamp(x).date()


def overlay_analysis(meta: dict, trades: list[HedgeTrade]) -> None:
    und_df = meta["und_df"]
    vti_df = meta["vti_df"]
    underlying = meta["underlying"]
    bar_cache = meta["bar_cache"]

    common = sorted(set(und_df.index) & set(vti_df.index))
    und = und_df.loc[common, "close"].astype(float)
    vti = vti_df.loc[common, "close"].astype(float)
    und.index = pd.Index(common)
    vti.index = pd.Index(common)

    und_ret = und.pct_change().fillna(0.0)
    vti_ret = vti.pct_change().fillna(0.0)
    port_ret = VTI_WEIGHT * vti_ret + EQUITY_WEIGHT * und_ret
    unhedged = BOOK_EQUITY * (1 + port_ret).cumprod()

    hedge_cum = pd.Series(0.0, index=unhedged.index)
    for d in common:
        total = 0.0
        for t in trades:
            if d < t.entry_date:
                continue
            if d >= t.exit_date:
                total += t.pnl_usd
            else:
                bars = bar_cache.get(t.occ, pd.DataFrame())
                if d in bars.index:
                    mark = float(bars.loc[d]["close"])
                    total += (mark - t.premium_in) * MULT
        hedge_cum.loc[d] = total

    hedged = unhedged.add(hedge_cum, fill_value=0.0)

    def max_dd(series: pd.Series) -> tuple[float, date, date]:
        peak = series.cummax()
        dd = series - peak
        trough_i = dd.idxmin()
        if pd.isna(trough_i):
            return 0.0, common[0], common[0]
        peak_i = series.loc[:trough_i].idxmax()
        return float(dd.min()), _as_date(peak_i), _as_date(trough_i)

    dd_u, peak_u, trough_u = max_dd(unhedged)
    dd_h, peak_h, trough_h = max_dd(hedged)
    lift = float(hedged.loc[trough_u] - unhedged.loc[trough_u])

    print("## Overlay (60% VTI + 40% {} book, ${:,.0f} start)".format(underlying, BOOK_EQUITY))
    print(f"dates: {common[0]} -> {common[-1]}")
    print(f"unhedged ending: ${unhedged.iloc[-1]:,.2f}  max DD: ${dd_u:,.2f}  ({peak_u} -> {trough_u})")
    print(f"hedged ending:   ${hedged.iloc[-1]:,.2f}  max DD: ${dd_h:,.2f}  ({peak_h} -> {trough_h})")
    print(f"hedge lift at unhedged trough: ${lift:,.2f}")
    print()

    # Quarterly drag when underlying up (quarter of exit vs entry spot)
    drag_rows = []
    for t in trades:
        q_ret = float(und.loc[t.exit_date]) / float(und.loc[t.entry_date]) - 1.0
        drag_pct = t.pnl_usd / BOOK_EQUITY
        drag_rows.append((t.quarter, q_ret, t.pnl_usd, drag_pct))

    up_quarters = [(q, r, p, d) for q, r, p, d in drag_rows if r > 0]
    if up_quarters:
        avg_drag = float(np.mean([d for _, _, _, d in up_quarters]))
        avg_pnl = float(np.mean([p for _, _, p, _ in up_quarters]))
        print("## Quarterly drag ({0} up quarters, n={1})".format(underlying, len(up_quarters)))
        print(f"avg hedge P&L in up quarters: ${avg_pnl:,.2f}")
        print(f"avg drag as % of book: {avg_drag:.2%}")
        print()
    else:
        print(f"## Quarterly drag: no {underlying}-up quarters with hedges in sample")
        print()


def print_quarterly_spend(rows: list[QuarterRow]) -> None:
    print("## Quarterly spend vs 0.75% cap ($750)")
    data = [
        {
            "quarter": r.quarter,
            "entry": r.entry_date.isoformat() if r.entry_date else "",
            "spend_usd": f"{r.spend_usd:.2f}",
            "cap_usd": f"{r.cap_usd:.2f}",
            "pct_cap": f"{r.pct_of_cap:.1f}%",
            "status": r.status,
            "note": r.note[:40],
        }
        for r in rows
    ]
    print(pd.DataFrame(data).to_string(index=False))
    filled = [r for r in rows if r.status == "filled"]
    if filled:
        tot = sum(r.spend_usd for r in filled)
        print(f"total premium spent (filled quarters): ${tot:,.2f}  ({len(filled)} quarters)")
    print()


def print_hedge_stats(trades: list[HedgeTrade]) -> None:
    pnls = [t.pnl_usd for t in trades]
    wins = [p for p in pnls if p > 0]
    curve = pd.Series(pnls).cumsum()
    max_dd = float((curve - curve.cummax()).min())
    print("## Hedge-only stats")
    print(f"n: {len(trades)}")
    print(f"win_rate: {len(wins) / len(trades):.1%} ({len(wins)}/{len(trades)})")
    print(f"median_trade_usd: ${float(pd.Series(pnls).median()):,.2f}")
    print(f"total_pnl_usd: ${sum(pnls):,.2f}")
    print(f"max_dd_hedge_curve_usd: ${max_dd:,.2f}")
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    print(f"exit_reasons: {', '.join(f'{k}={v}' for k, v in sorted(reasons.items()))}")
    print()


def print_trade_log(trades: list[HedgeTrade]) -> None:
    print("## Trade log")
    rows = [
        {
            "quarter": t.quarter,
            "entry": t.entry_date.isoformat(),
            "occ": t.occ,
            "strike": f"{t.strike:.2f}",
            "expiry": t.expiry.isoformat(),
            "spend": f"${t.premium_spend_usd:.2f}",
            "exit": t.exit_date.isoformat(),
            "reason": t.exit_reason,
            "pnl_usd": f"${t.pnl_usd:.2f}",
            "pnl_pct": f"{t.pnl_pct:.1%}",
        }
        for t in trades
    ]
    print(pd.DataFrame(rows).to_string(index=False))
    print()


def save_csv(trades: list[HedgeTrade], path: Path) -> None:
    rows = [
        {
            "quarter": t.quarter,
            "underlying": t.underlying,
            "entry_date": t.entry_date.isoformat(),
            "occ": t.occ,
            "strike": t.strike,
            "expiry": t.expiry.isoformat(),
            "contracts": t.contracts,
            "premium_in": t.premium_in,
            "premium_spend_usd": t.premium_spend_usd,
            "cap_usd": t.cap_usd,
            "exit_date": t.exit_date.isoformat(),
            "exit_reason": t.exit_reason,
            "premium_out": t.premium_out,
            "pnl_usd": t.pnl_usd,
            "pnl_pct": t.pnl_pct,
            "dte_at_entry": t.dte_at_entry,
            "spot_at_entry": t.spot_at_entry,
            "otm_pct": t.otm_pct,
            "fill_note": t.fill_note,
        }
        for t in trades
    ]
    pd.DataFrame(rows).to_csv(path, index=False)


def print_caveats(underlying: str, trades: list[HedgeTrade]) -> None:
    print("## Data caveats")
    print("- Research only. No orders. No promote claim.")
    print("- Alpaca options history from ~2024-02-01; not a multi-year OPRA tape.")
    print("- Historical greeks unavailable; 0.25 delta not used — 10-15% OTM fallback.")
    print("- QQQ/SPY option daily bars can be very sparse (few prints per contract);")
    print(f"  entry uses first real bar within {ENTRY_SLIP_SESSIONS} sessions of quarter open.")
    print("- Intrabar 2x on same day as adverse move: not modeled (daily bars only).")
    print("- Overlay uses 60% VTI + 40% {} buy-and-hold daily rebalance.".format(underlying))
    print("- 1 contract/quarter when premium fits cap; skip if over $750 or prior hedge open.")
    print("- Book equity fixed at $100k for cap math (not live paper mark).")
    if trades:
        print(
            f"- Sample: {trades[0].entry_date} -> {trades[-1].exit_date}, "
            f"{len(trades)} filled quarters."
        )
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="AI-burst quarterly QQQ/SPY hedge backtest")
    ap.add_argument("--underlying", default="QQQ", choices=("QQQ", "SPY"))
    ap.add_argument("--also-spy", action="store_true", help="Run SPY after QQQ if QQQ succeeds")
    ap.add_argument("--years", type=float, default=3.0)
    ap.add_argument("--friction", type=float, default=DEFAULT_FRICTION)
    args = ap.parse_args()
    if args.friction < 0 or args.friction > 0.10:
        return _fail("--friction must be between 0 and 0.10")

    underlyings = [args.underlying.upper()]
    if args.also_spy and "SPY" not in underlyings:
        underlyings.append("SPY")

    last_code = 0
    for sym in underlyings:
        code, trades, qrows, meta = run_backtest(sym, friction=args.friction, years=args.years)
        if code != 0:
            last_code = code
            if sym == "QQQ" and not args.also_spy:
                return code
            continue

        print_trade_log(trades)
        print_quarterly_spend(qrows)
        print_hedge_stats(trades)
        out = out_csv_path(sym)
        save_csv(trades, out)
        print(f"Wrote {out}")
        overlay_analysis(meta, trades)
        print_caveats(sym, trades)

        if sym == "QQQ" and args.also_spy:
            print("\n" + "=" * 72 + "\nSPY second pass\n")

    return last_code


if __name__ == "__main__":
    raise SystemExit(main())
