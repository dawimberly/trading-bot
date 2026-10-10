#!/usr/bin/env python3
"""PLTR long-put paper-research backtest — no orders, no .env/sleeve changes.

Fixed rule (v1):
  - Buy 1 PLTR put per signal cycle
  - 60–90 DTE; strike ~10–15% OTM (0.30 delta is not used historically —
    Alpaca option snapshots/greeks are latest-only)
  - Exit first of: premium >= 2x entry | <= 50% of entry | 21 DTE remaining
  - No pyramiding, no rolls
  - Friction: $0.05 per share per side (quoted option price units)

Data (Alpaca):
  - Equity daily bars: StockHistoricalDataClient (context + spot for strike)
  - Contract master: TradingClient.get_option_contracts (active + inactive)
  - Premium path: OptionHistoricalDataClient.get_option_bars (daily)
  - Options history exists from ~February 2024 only

If contracts or bars cannot be loaded, the script exits with a data-contract
message and does not invent a P&L curve.

Run (from stock-bot/):
  python scripts/research/pltr_long_put_backtest.py
  python scripts/research/pltr_long_put_backtest.py --years 3 --friction 0.05

Writes:
  scripts/research/pltr_long_put_trades.csv
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env", override=False)

import config  # noqa: E402

UNDERLYING = "PLTR"
OPTIONS_HISTORY_START = date(2024, 2, 1)
DTE_MIN = 60
DTE_MAX = 90
DTE_TARGET = 75
DTE_FORCE_EXIT = 21
OTM_LO = 0.10
OTM_HI = 0.15
OTM_TARGET = 0.125
OTM_HARD_LO = 0.08
OTM_HARD_HI = 0.18
MULT = 100
TP_MULT = 2.0
SL_MULT = 0.50
SENS_TP = 1.5
SENS_SL = 0.60
DEFAULT_FRICTION = 0.05  # per share, each side
OUT_CSV = Path(__file__).resolve().parent / "pltr_long_put_trades.csv"
DATA_CONTRACT = """
Minimum data contract (cannot fake fills without this):
  1. Alpaca keys in .env (APCA_* or PAPER_APCA_*) — used for *market data only*.
  2. Options market-data access (indicative or OPRA). Historical option bars
     since 2024-02-01: GET /v1beta1/options/bars for OCC symbols.
  3. Option contract master including expired/inactive puts:
     GET /v2/options/contracts?underlying_symbols=PLTR&type=put
  4. Daily equity bars for PLTR (spot for 10–15% OTM strike).
  5. At least one put with 60–90 DTE whose daily bar exists on the entry date
     and through the exit (2x / 50% / 21 DTE).

Not available from Alpaca (so this run does not use them):
  - Historical IV surface / as-of greeks (chain snapshots are latest-only)
  - Bid-ask at the historical print (bars are OHLC/VWAP aggregates)
"""


@dataclass
class Contract:
    symbol: str
    expiration: date
    strike: float


@dataclass
class Trade:
    entry_date: date
    occ: str
    strike: float
    expiry: date
    premium_in: float
    exit_date: date
    exit_reason: str
    premium_out: float
    pnl_usd: float
    pnl_pct: float
    dte_at_entry: int
    spot_at_entry: float
    otm_pct: float
    fill_note: str


def _fail(msg: str, *, code: int = 2) -> int:
    print(msg.rstrip() + "\n" + DATA_CONTRACT, file=sys.stderr)
    return code


def _keys() -> tuple[str, str]:
    try:
        return config.get_alpaca_credentials(paper=True)
    except Exception:
        return config.get_alpaca_credentials(paper=False)


def _as_date(ts) -> date:
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_convert("America/New_York")
    return t.date()


def fetch_pltr_daily(start: date, end: date) -> pd.DataFrame:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    key, secret = _keys()
    client = StockHistoricalDataClient(api_key=key, secret_key=secret)
    from alpaca.data.enums import DataFeed

    last_err = None
    df = pd.DataFrame()
    for feed in (DataFeed.IEX, DataFeed.SIP, None):
        kwargs = dict(
            symbol_or_symbols=UNDERLYING,
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
        raise RuntimeError(last_err or "empty PLTR daily bars")
    if isinstance(df.index, pd.MultiIndex):
        df = df.reset_index()
        if "symbol" in df.columns:
            df = df[df["symbol"] == UNDERLYING]
    else:
        df = df.reset_index()
    ts_col = "timestamp" if "timestamp" in df.columns else df.columns[0]
    df["session"] = pd.to_datetime(df[ts_col], utc=True).dt.tz_convert(
        "America/New_York"
    )
    df["session_date"] = df["session"].dt.date
    df = df.sort_values("session_date").drop_duplicates("session_date")
    return df.set_index("session_date")


def _page_contracts(trading, **kwargs) -> list:
    from alpaca.trading.requests import GetOptionContractsRequest

    out = []
    page = None
    while True:
        req = GetOptionContractsRequest(page_token=page, **kwargs)
        resp = trading.get_option_contracts(req)
        batch = list(resp.option_contracts or [])
        out.extend(batch)
        page = resp.next_page_token
        if not page:
            break
    return out


def list_pltr_puts(trading, start_exp: date, end_exp: date) -> list[Contract]:
    from alpaca.trading.enums import AssetStatus, ContractType

    found: dict[str, Contract] = {}
    cursor = start_exp
    while cursor <= end_exp:
        chunk_end = min(cursor + timedelta(days=90), end_exp)
        for status in (AssetStatus.ACTIVE, AssetStatus.INACTIVE):
            raw = _page_contracts(
                trading,
                underlying_symbols=[UNDERLYING],
                status=status,
                type=ContractType.PUT,
                expiration_date_gte=cursor.isoformat(),
                expiration_date_lte=chunk_end.isoformat(),
                limit=10000,
            )
            for c in raw:
                if c.underlying_symbol != UNDERLYING:
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
        raw = opt_client._get_marketdata(  # noqa: SLF001 — feed not on OptionBarsRequest
            path="/options/bars",
            params=fields,
            page_size=10_000,
        )
        from alpaca.data.models.bars import BarSet

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
    df["session"] = pd.to_datetime(df[ts_col], utc=True).dt.tz_convert(
        "America/New_York"
    )
    df["session_date"] = df["session"].dt.date
    df = df.sort_values("session_date").drop_duplicates("session_date")
    return df.set_index("session_date")


def ranked_puts(puts: list[Contract], entry: date, spot: float) -> tuple[list[Contract], str]:
    lo = entry + timedelta(days=DTE_MIN)
    hi = entry + timedelta(days=DTE_MAX)
    window = [c for c in puts if lo <= c.expiration <= hi]
    if not window:
        return [], "no_put_in_60_90_dte"
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
        hard = 0 if (OTM_HARD_LO <= otm <= OTM_HARD_HI) else 1
        return (hard, abs(dte - DTE_TARGET), abs(c.strike - target_k))

    ranked = sorted(pool, key=score)
    ranked = [
        c for c in ranked if OTM_HARD_LO <= (1.0 - c.strike / spot) <= OTM_HARD_HI
    ]
    if not ranked:
        return [], "best_strike_outside_8-18%_band"
    note = "otm_band_10_15" if band else "otm_nearest_outside_10_15_inside_8_18"
    if monthlies:
        note += ";monthly"
    return ranked, note


def apply_friction(mid: float, *, buy: bool, friction: float) -> float:
    px = mid + friction if buy else mid - friction
    return max(px, 0.01)


def simulate_trade(
    bars: pd.DataFrame,
    *,
    entry: date,
    expiry: date,
    friction: float,
    tp_mult: float,
    sl_mult: float,
) -> tuple[date, str, float, str] | None:
    if entry not in bars.index:
        return None
    mid_in = float(bars.loc[entry]["close"])
    if mid_in <= 0:
        return None
    premium_in = apply_friction(mid_in, buy=True, friction=friction)
    tp = premium_in * tp_mult
    sl = premium_in * sl_mult
    after = bars.loc[bars.index > entry]
    for sess, row in after.iterrows():
        dte_left = (expiry - sess).days
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        hit_tp = high >= tp
        hit_sl = low <= sl
        hit_dte = dte_left <= DTE_FORCE_EXIT
        if hit_tp and hit_sl:
            # Daily bar: path unknown. Conservative: stop (worse for long put).
            out = apply_friction(sl, buy=False, friction=friction)
            return sess, "stop_50pct_ambiguous_intrabar", out, "intrabar_tp_and_sl"
        if hit_tp:
            # Fill at the TP threshold (not the session close spike).
            out = apply_friction(tp, buy=False, friction=friction)
            return sess, f"take_profit_{tp_mult:g}x", out, "high_crossed_tp"
        if hit_sl:
            out = apply_friction(sl, buy=False, friction=friction)
            return sess, f"stop_{sl_mult:.0%}", out, "low_crossed_sl"
        if hit_dte:
            out = apply_friction(close, buy=False, friction=friction)
            return sess, "dte_21", out, "calendar_21dte"
    # Last available bar before expiry
    if after.empty:
        return None
    last = after.index[-1]
    out = apply_friction(float(after.loc[last]["close"]), buy=False, friction=friction)
    return last, "last_bar_before_expiry", out, "no_explicit_exit_rule_hit"


def _histogram(pcts: list[float], bins: int = 8) -> str:
    if not pcts:
        return "(no trades)"
    s = pd.Series(pcts)
    cats = pd.cut(s, bins=bins)
    counts = cats.value_counts().sort_index()
    m = int(counts.max()) if len(counts) else 1
    lines = []
    for interval, n in counts.items():
        bar = "#" * max(1, int(round(20 * n / m))) if n else ""
        lines.append(f"  {interval}: {int(n):3d}  {bar}")
    return "\n".join(lines)


def _summary_table(trades: list[Trade], *, label: str) -> str:
    if not trades:
        return f"## {label}\n(no trades)\n"
    pnls = [t.pnl_usd for t in trades]
    pcts = [t.pnl_pct for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    curve = pd.Series(pnls).cumsum()
    peak = curve.cummax()
    dd = curve - peak
    max_dd = float(dd.min()) if len(dd) else 0.0
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    reason_s = ", ".join(f"{k}={v}" for k, v in sorted(reasons.items()))
    return "\n".join(
        [
            f"## {label}",
            f"trades: {len(trades)}",
            f"win_rate: {len(wins) / len(trades):.1%}  ({len(wins)} / {len(trades)})",
            f"avg_win_usd: {sum(wins) / len(wins):.2f}" if wins else "avg_win_usd: n/a",
            f"avg_loss_usd: {sum(losses) / len(losses):.2f}" if losses else "avg_loss_usd: n/a",
            f"total_pnl_usd: {sum(pnls):.2f}",
            f"median_trade_pnl_usd: {float(pd.Series(pnls).median()):.2f}",
            f"median_trade_pnl_pct: {float(pd.Series(pcts).median()):.1%}",
            f"max_dd_options_curve_usd: {max_dd:.2f}",
            f"exit_reasons: {reason_s}",
            "",
        ]
    )


def _print_trade_log(trades: list[Trade]) -> None:
    cols = [
        "entry_date",
        "occ",
        "strike",
        "expiry",
        "premium_in",
        "exit_date",
        "exit_reason",
        "premium_out",
        "pnl_usd",
        "pnl_pct",
    ]
    rows = [
        {
            "entry_date": t.entry_date.isoformat(),
            "occ": t.occ,
            "strike": f"{t.strike:.2f}",
            "expiry": t.expiry.isoformat(),
            "premium_in": f"{t.premium_in:.4f}",
            "exit_date": t.exit_date.isoformat(),
            "exit_reason": t.exit_reason,
            "premium_out": f"{t.premium_out:.4f}",
            "pnl_usd": f"{t.pnl_usd:.2f}",
            "pnl_pct": f"{t.pnl_pct:.1%}",
        }
        for t in trades
    ]
    print(pd.DataFrame(rows, columns=cols).to_string(index=False))


def _save_csv(trades: list[Trade]) -> None:
    rows = [
        {
            "entry_date": t.entry_date.isoformat(),
            "strike": t.strike,
            "expiry": t.expiry.isoformat(),
            "occ": t.occ,
            "premium_in": t.premium_in,
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
    pd.DataFrame(rows).to_csv(OUT_CSV, index=False)


def run(years: float, friction: float) -> int:
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.trading.client import TradingClient

    today = datetime.now(timezone.utc).date()
    want_start = today - timedelta(days=int(365 * years))
    start = max(want_start, OPTIONS_HISTORY_START)
    end = today

    print("PLTR long-put research backtest (no orders)")
    print(f"requested_window: {years:.1f}y  effective: {start} -> {end}")
    print(
        "strike_rule: 10-15% OTM (0.30 delta unavailable historically - "
        "Alpaca greeks are latest snapshots only)"
    )
    print(f"exits: {TP_MULT:g}x / {SL_MULT:.0%} / {DTE_FORCE_EXIT} DTE  friction=${friction:.2f}/share/side")
    print()

    try:
        equity = fetch_pltr_daily(start, end)
    except Exception as exc:
        return _fail(f"Failed to load PLTR equity daily bars: {exc}")
    if equity.empty or "close" not in equity.columns:
        return _fail("PLTR equity daily bars empty.")

    eq_start = min(equity.index)
    eq_end = max(equity.index)
    print(f"PLTR equity bars: {eq_start} -> {eq_end}  ({len(equity)} sessions)")

    key, secret = _keys()
    try:
        trading = TradingClient(key, secret, paper=True)
        puts = list_pltr_puts(
            trading,
            start + timedelta(days=DTE_MIN),
            end + timedelta(days=DTE_MAX + 5),
        )
    except Exception as exc:
        return _fail(f"Failed to list PLTR option contracts: {exc}")
    if not puts:
        return _fail("No PLTR put contracts returned (active+inactive).")
    print(f"put contracts listed: {len(puts)}  expiries {puts[0].expiration} -> {puts[-1].expiration}")

    opt_client = OptionHistoricalDataClient(api_key=key, secret_key=secret)
    feed: str | None = None
    # Probe one nearby monthly-style symbol from the list.
    probe_occ = puts[min(len(puts) // 2, len(puts) - 1)].symbol
    probe_df = pd.DataFrame()
    last_err = None
    for try_feed in (None, "indicative", "opra"):
        try:
            probe_df = fetch_option_daily(
                opt_client,
                probe_occ,
                start,
                min(end, puts[min(len(puts) // 2, len(puts) - 1)].expiration),
                try_feed,
            )
            if not probe_df.empty:
                feed = try_feed
                break
        except Exception as exc:
            last_err = exc
            probe_df = pd.DataFrame()
    if probe_df.empty:
        extra = f" last_error={last_err}" if last_err else ""
        return _fail(
            f"Option daily bars empty for probe {probe_occ} (tried default/"
            f"indicative/opra).{extra}"
        )
    print(f"option bars probe ok: {probe_occ}  feed={feed or 'sdk_default'}  bars={len(probe_df)}")
    print()

    sessions = list(equity.index)
    trades: list[Trade] = []
    gaps: list[str] = []
    bar_cache: dict[str, pd.DataFrame] = {}
    i = 0
    while i < len(sessions):
        entry = sessions[i]
        if entry < start:
            i += 1
            continue
        # Need room for 60 DTE contract + 21 DTE exit.
        if entry > end - timedelta(days=DTE_FORCE_EXIT + 5):
            break
        spot = float(equity.loc[entry]["close"])
        if spot <= 0:
            i += 1
            continue
        ranked, pick_note = ranked_puts(puts, entry, spot)
        if not ranked:
            gaps.append(f"{entry}: {pick_note}")
            i += 1
            continue
        chosen = None
        bars = pd.DataFrame()
        for contract in ranked[:8]:
            occ = contract.symbol
            if occ not in bar_cache:
                try:
                    bar_cache[occ] = fetch_option_daily(
                        opt_client, occ, entry, contract.expiration, feed
                    )
                except Exception as exc:
                    gaps.append(f"{entry}: bars_error {occ} {exc}")
                    continue
            cand = bar_cache[occ]
            if cand.empty or entry not in cand.index:
                continue
            chosen = contract
            bars = cand
            break
        if chosen is None:
            gaps.append(f"{entry}: no_entry_bar {ranked[0].symbol} (+{min(7, len(ranked)-1)} alts)")
            i += 1
            continue
        contract = chosen
        occ = contract.symbol
        sim = simulate_trade(
            bars,
            entry=entry,
            expiry=contract.expiration,
            friction=friction,
            tp_mult=TP_MULT,
            sl_mult=SL_MULT,
        )
        if sim is None:
            gaps.append(f"{entry}: no_entry_bar_or_path {occ}")
            i += 1
            continue
        exit_date, reason, premium_out, fill_note = sim
        premium_in = apply_friction(
            float(bars.loc[entry]["close"]), buy=True, friction=friction
        )
        pnl = (premium_out - premium_in) * MULT
        trades.append(
            Trade(
                entry_date=entry,
                occ=occ,
                strike=contract.strike,
                expiry=contract.expiration,
                premium_in=premium_in,
                exit_date=exit_date,
                exit_reason=reason,
                premium_out=premium_out,
                pnl_usd=pnl,
                pnl_pct=(premium_out - premium_in) / premium_in,
                dte_at_entry=(contract.expiration - entry).days,
                spot_at_entry=spot,
                otm_pct=1.0 - contract.strike / spot,
                fill_note=f"{pick_note};{fill_note}",
            )
        )
        # Next cycle: first session strictly after exit.
        nxt = next((j for j, d in enumerate(sessions) if d > exit_date), None)
        if nxt is None:
            break
        i = nxt

    if not trades:
        sample = "\n  ".join(gaps[:12]) or "(no gap log)"
        return _fail(
            "Zero completed trades - not inventing a curve.\n"
            f"Gap sample:\n  {sample}"
        )

    used_start = trades[0].entry_date
    used_end = trades[-1].exit_date
    print(f"## Window used (first entry -> last exit): {used_start} -> {used_end}")
    print(f"skipped_cycles: {len(gaps)}")
    print()
    print("## Trade log")
    _print_trade_log(trades)
    print()
    print(_summary_table(trades, label="Summary (2.0x / 50% / 21 DTE)"))
    print("## Trade return distribution (pnl_pct)")
    print(_histogram([t.pnl_pct for t in trades]))
    print()

    # Sensitivity: same entries / same contracts, different TP/SL.
    sens: list[Trade] = []
    for t in trades:
        bars = bar_cache.get(t.occ)
        if bars is None or t.entry_date not in bars.index:
            continue
        sim = simulate_trade(
            bars,
            entry=t.entry_date,
            expiry=t.expiry,
            friction=friction,
            tp_mult=SENS_TP,
            sl_mult=SENS_SL,
        )
        if sim is None:
            continue
        exit_date, reason, premium_out, fill_note = sim
        premium_in = apply_friction(
            float(bars.loc[t.entry_date]["close"]), buy=True, friction=friction
        )
        pnl = (premium_out - premium_in) * MULT
        sens.append(
            Trade(
                entry_date=t.entry_date,
                occ=t.occ,
                strike=t.strike,
                expiry=t.expiry,
                premium_in=premium_in,
                exit_date=exit_date,
                exit_reason=reason,
                premium_out=premium_out,
                pnl_usd=pnl,
                pnl_pct=(premium_out - premium_in) / premium_in,
                dte_at_entry=t.dte_at_entry,
                spot_at_entry=t.spot_at_entry,
                otm_pct=t.otm_pct,
                fill_note=fill_note,
            )
        )
    print(_summary_table(sens, label="Sensitivity (1.5x / 60% / 21 DTE, same entries)"))

    print("## Caveats")
    print("- Not promote-ready. Research only; does not touch equity bot / .env / sleeves.")
    print("- Alpaca options history starts ~2024-02-01 - not a full 3-year OPRA tape.")
    print("- No historical 0.30 delta: strikes are 10-15% OTM (8-18% hard band).")
    print("- Fills use daily option close +/- friction; no bid-ask. Intrabar 2x and 50%")
    print("  on the same day is counted as the stop (conservative).")
    print("- Missing session bars are not interpolated; cycle is skipped or exits on")
    print("  last real bar.")
    print("- Survivorship: PLTR listed throughout the window; expired contracts come")
    print("  from Alpaca's inactive master (gaps possible).")
    print("- One contract, sequential cycles only.")
    if gaps:
        print(f"- {len(gaps)} skipped entry dates (see first 8):")
        for g in gaps[:8]:
            print(f"    {g}")

    _save_csv(trades)
    print()
    print(f"Wrote {OUT_CSV}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="PLTR long-put research backtest (no orders)")
    ap.add_argument("--years", type=float, default=3.0, help="Lookback years (capped at Alpaca options start)")
    ap.add_argument("--friction", type=float, default=DEFAULT_FRICTION, help="Per-share penalty each side")
    args = ap.parse_args()
    if args.friction < 0:
        return _fail("--friction must be >= 0")
    return run(years=args.years, friction=args.friction)


if __name__ == "__main__":
    raise SystemExit(main())
