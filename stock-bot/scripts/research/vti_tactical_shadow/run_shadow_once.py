"""$5k VTI tactical overlay — SHADOW ONLY (freeze-safe).

Does NOT place orders or break paper freeze.

Critical provenance rules:
  - Every row has data_source + bar_date + data_fresh + promote_eligible
  - Stale / fallback rows are NEVER mixed into promote stats by default
  - Prefer SQLite; yfinance fallback is logged and non-eligible unless --allow-stale

Usage (from stock-bot/):
  python scripts/research/vti_tactical_shadow/run_shadow_once.py
  python scripts/research/vti_tactical_shadow/run_shadow_once.py --symbol VTI
  python scripts/research/vti_tactical_shadow/summarize_shadow.py
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SHADOW_DIR = HERE.parents[0] / "sneaky pivot"
OUT_DIR = HERE / "output"
EVENTS_PATH = OUT_DIR / "shadow_events.jsonl"
STALE_PATH = OUT_DIR / "shadow_events_stale.jsonl"
SUMMARY_PATH = OUT_DIR / "shadow_summary_latest.md"
POISON_ARCHIVE = OUT_DIR / "archive_pre_provenance_events.jsonl"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SHADOW_DIR))

import config  # noqa: E402
from modules import market_context  # noqa: E402
from modules.data_loader import load_close_matrix  # noqa: E402
from modules.garch_vol import update_garch_vol  # noqa: E402

NOTIONAL = 5000.0
GARCH_MULT_FLOOR = 0.55
GARCH_MULT_CEIL = 0.85
RIP_PCT = 0.015
DIP_PCT = 0.005
LOOKBACK_SESSIONS = 5
# After RTH close ET, last daily bar should be today (weekday) or prior session.
MAX_BAR_LAG_CALENDAR_DAYS = 1


@dataclass
class PanelLoad:
    panel: pd.DataFrame
    data_source: str  # sqlite | yfinance_fallback
    load_error: str = ""


@dataclass
class ShadowEvent:
    ts_utc: str
    symbol: str
    bar_date: str
    last_close: float
    data_source: str  # sqlite | yfinance_fallback
    data_fresh: bool
    promote_eligible: bool
    stale_reason: str
    ret_1d_pct: float | None
    ret_2d_pct: float | None
    from_local_high_pct: float | None
    action: str
    reason: str
    notional_cap: float
    size_mult: float
    sized_notional: float
    rhyme_prod: str
    rhyme_shadow_equity: str
    rhyme_prod_letter: str
    rhyme_shadow_letter: str
    vol_score_prod: float
    vol_score_equity_shadow: float
    vol_score_fixed_shadow: float
    garch_ok: bool
    garch_ratio: float | None
    garch_size_mult_raw: float | None
    arima_enabled_for_decision: bool
    arima_direction: str
    arima_mult: float | None
    bh_equity_5k: float
    shadow_mark_5k: float
    notes: str


def _rhyme_letter(regime: str) -> str:
    r = str(regime or "")
    for letter in ("A", "B", "C", "D", "E"):
        if f"RHYME_{letter}" in r:
            return letter
    return "?"


def _classify_rhyme_from_vol_score(
    data: pd.DataFrame, vol_score: float, *, interval: str
) -> str:
    thresh = market_context.regime_vol_threshold(interval)
    volatility = "High" if vol_score > thresh else "Low"
    sentiment = market_context.get_price_sentiment(data)
    return market_context.get_market_regime(sentiment, volatility)


def _yf_fallback_panel(days: int = 400) -> pd.DataFrame:
    import yfinance as yf

    tickers = ["VTI", "SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "BRK-B"]
    period = "2y" if days >= 400 else "1y"
    raw = yf.download(
        tickers,
        period=period,
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    if raw is None or raw.empty:
        raise RuntimeError("yfinance fallback empty")
    if isinstance(raw.columns, pd.MultiIndex):
        closes = raw["Close"].copy()
    else:
        closes = raw[["Close"]].copy()
        closes.columns = [tickers[0]]
    closes = closes.rename(columns={"BRK-B": "BRK.B"})
    closes = closes.dropna(how="all").ffill()
    if closes.empty:
        raise RuntimeError("yfinance fallback produced empty panel")
    return closes


def _load_panel(days: int = 400) -> PanelLoad:
    try:
        daily = load_close_matrix(interval="1d", days=days)
        if daily is not None and not daily.empty:
            return PanelLoad(panel=daily, data_source="sqlite")
        err = "sqlite empty"
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    try:
        return PanelLoad(
            panel=_yf_fallback_panel(days=days),
            data_source="yfinance_fallback",
            load_error=err,
        )
    except Exception as yf_exc:
        raise SystemExit(
            f"No usable panel. sqlite={err}; yfinance={yf_exc}"
        ) from yf_exc


def _series(panel: pd.DataFrame, symbol: str) -> pd.Series:
    sym = symbol.upper()
    if sym not in panel.columns:
        for alt in (sym, "VTI", "SPY"):
            if alt in panel.columns:
                sym = alt
                break
        else:
            raise SystemExit(f"{symbol} not in close matrix columns")
    s = panel[sym].dropna().astype(float)
    if len(s) < LOOKBACK_SESSIONS + 5:
        raise SystemExit(f"Insufficient history for {sym}")
    return s


def _bar_date(series: pd.Series) -> date:
    idx = series.index[-1]
    ts = pd.Timestamp(idx)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("America/New_York")
    return ts.date()


def _expected_last_session(today: date | None = None) -> date:
    """Last completed US cash session date (naive weekday backstep; no holiday calendar)."""
    d = today or datetime.now(timezone.utc).astimezone().date()
    # Before 16:00 ET, prior session is "complete"; after close, today can be complete.
    try:
        from zoneinfo import ZoneInfo

        now_et = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now_et = datetime.now()
    if now_et.hour < 16:
        d = (now_et - timedelta(days=1)).date()
    else:
        d = now_et.date()
    while d.weekday() >= 5:  # Sat/Sun → Friday
        d -= timedelta(days=1)
    return d


def _freshness(
    *,
    data_source: str,
    bar_date: date,
    last_close: float,
) -> tuple[bool, str]:
    """Return (data_fresh, stale_reason). Fallback is never promote-fresh."""
    if data_source != "sqlite":
        return False, f"source_{data_source}_not_sqlite"
    expected = _expected_last_session()
    lag = (expected - bar_date).days
    if lag > MAX_BAR_LAG_CALENDAR_DAYS:
        return False, f"bar_lag_{lag}d_bar={bar_date}_expected={expected}"
    if lag < -1:
        return False, f"bar_in_future_bar={bar_date}_expected={expected}"
    # Sanity: known Aug-2026 VTI regime after rip is well above pre-rip ~368
    if bar_date >= date(2026, 8, 3) and last_close < 372.0:
        return False, f"price_sanity_fail_close={last_close}_on_{bar_date}"
    return True, ""


def _arima_advisory(panel: pd.DataFrame, symbol: str) -> tuple[str, float | None, str]:
    try:
        from modules.arima_forecast import update_arima_forecast, get_arima_forecast_state

        saved = bool(getattr(config, "ARIMA_ENABLED", False))
        config.ARIMA_ENABLED = True
        try:
            update_arima_forecast(panel, symbol=symbol)
            st = get_arima_forecast_state()
        finally:
            config.ARIMA_ENABLED = saved
        if not getattr(st, "ok", False):
            return "n/a", None, str(getattr(st, "reason", "not_ok"))
        mult = float(getattr(st, "size_mult", 1.0) or 1.0)
        if mult > 1.01:
            direction = "up"
        elif mult < 0.99:
            direction = "down"
        else:
            direction = "flat"
        return direction, mult, "advisory_only"
    except Exception as exc:
        return "error", None, str(exc)[:160]


def _decide_action(
    *,
    rhyme_prod: str,
    ret_2d: float | None,
    from_high: float | None,
) -> tuple[str, str]:
    letter = _rhyme_letter(rhyme_prod)
    if letter in ("A", "B"):
        return "blocked", f"prod_RHYME_{letter}_block_chase"
    if letter in ("E",):
        return "blocked", "prod_RHYME_E_no_add_bias"
    if ret_2d is not None and ret_2d >= RIP_PCT:
        if letter in ("C", "D", "?"):
            return "would_trim", f"2d_rip_{ret_2d:.2%}_gate_{letter}"
    if from_high is not None and from_high <= -DIP_PCT:
        if letter == "D":
            return "would_add", f"dip_from_high_{from_high:.2%}_prefer_D"
        if letter == "C":
            return "would_add", f"dip_from_high_{from_high:.2%}_allow_C"
    return "hold", f"no_trigger_gate_{letter}"


def quarantine_pre_provenance_logs() -> str:
    """Move unlabeled early rows out of the promote stream."""
    if not EVENTS_PATH.exists():
        return "no_events_file"
    raw = EVENTS_PATH.read_text(encoding="utf-8").strip()
    if not raw:
        return "empty"
    keep: list[str] = []
    poison: list[str] = []
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            poison.append(line)
            continue
        if "data_source" not in row or "promote_eligible" not in row:
            poison.append(line)
        else:
            keep.append(line)
    if not poison:
        return "clean"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with POISON_ARCHIVE.open("a", encoding="utf-8") as f:
        for line in poison:
            f.write(line + "\n")
    EVENTS_PATH.write_text(("\n".join(keep) + ("\n" if keep else "")), encoding="utf-8")
    return f"quarantined_{len(poison)}_kept_{len(keep)}"


def build_event(*, symbol: str = "VTI") -> ShadowEvent:
    config.set_paper_aggressive_context(True)
    config.set_backtest_paper_sleeves_context(True)

    loaded = _load_panel()
    panel = loaded.panel
    series = _series(panel, symbol)
    bar_d = _bar_date(series)
    px = float(series.iloc[-1])
    fresh, stale_reason = _freshness(
        data_source=loaded.data_source, bar_date=bar_d, last_close=px
    )
    promote_eligible = bool(fresh and loaded.data_source == "sqlite")

    ret_1d = float(series.iloc[-1] / series.iloc[-2] - 1.0) if len(series) >= 2 else None
    ret_2d = float(series.iloc[-1] / series.iloc[-3] - 1.0) if len(series) >= 3 else None
    local = series.iloc[-LOOKBACK_SESSIONS:]
    local_high = float(local.max())
    from_high = (px / local_high - 1.0) if local_high > 0 else None

    regime_data, interval = market_context.regime_dataframe(panel)
    inputs = market_context.get_regime_inputs(panel)
    rhyme_prod = market_context.get_market_regime(
        inputs["price_sentiment"], inputs["volatility"]
    )
    vol_prod = float(inputs.get("vol_score") or 0.0)

    vol_eq = 0.0
    vol_fixed = 0.0
    rhyme_shadow = "n/a"
    try:
        from cross_asset_vol_score_shadow import (  # type: ignore
            equity_vol_score,
            cross_asset_vol_score_FIXED,
        )

        vol_eq = float(equity_vol_score(regime_data))
        vol_fixed = float(cross_asset_vol_score_FIXED(regime_data))
        rhyme_shadow = _classify_rhyme_from_vol_score(
            regime_data, vol_eq, interval=interval
        )
    except Exception as exc:
        rhyme_shadow = f"shadow_err:{exc.__class__.__name__}"

    garch_st = update_garch_vol(panel, symbol=symbol)
    raw_mult = float(getattr(garch_st, "size_mult", 1.0) or 1.0)
    size_mult = max(GARCH_MULT_FLOOR, min(GARCH_MULT_CEIL, raw_mult))
    sized = round(NOTIONAL * size_mult, 2)

    action, reason = _decide_action(
        rhyme_prod=rhyme_prod, ret_2d=ret_2d, from_high=from_high
    )
    arima_dir, arima_mult, arima_note = _arima_advisory(panel, symbol)

    notes = (
        f"ARIMA={arima_note}; load_err={loaded.load_error or 'none'}; "
        f"RHYME equity-split SHADOW-only"
    )
    if not promote_eligible:
        notes = f"NOT_PROMOTE_ELIGIBLE ({stale_reason}); " + notes

    return ShadowEvent(
        ts_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        symbol=symbol.upper(),
        bar_date=str(bar_d),
        last_close=round(px, 4),
        data_source=loaded.data_source,
        data_fresh=fresh,
        promote_eligible=promote_eligible,
        stale_reason=stale_reason,
        ret_1d_pct=None if ret_1d is None else round(ret_1d * 100, 4),
        ret_2d_pct=None if ret_2d is None else round(ret_2d * 100, 4),
        from_local_high_pct=None if from_high is None else round(from_high * 100, 4),
        action=action,
        reason=reason,
        notional_cap=NOTIONAL,
        size_mult=size_mult,
        sized_notional=sized,
        rhyme_prod=rhyme_prod,
        rhyme_shadow_equity=rhyme_shadow,
        rhyme_prod_letter=_rhyme_letter(rhyme_prod),
        rhyme_shadow_letter=_rhyme_letter(rhyme_shadow),
        vol_score_prod=round(vol_prod, 6),
        vol_score_equity_shadow=round(vol_eq, 6),
        vol_score_fixed_shadow=round(vol_fixed, 6),
        garch_ok=bool(getattr(garch_st, "ok", False)),
        garch_ratio=(
            None
            if getattr(garch_st, "ratio", None) is None
            else round(float(garch_st.ratio), 4)
        ),
        garch_size_mult_raw=round(raw_mult, 4),
        arima_enabled_for_decision=False,
        arima_direction=arima_dir,
        arima_mult=None if arima_mult is None else round(float(arima_mult), 4),
        bh_equity_5k=NOTIONAL,
        shadow_mark_5k=NOTIONAL,
        notes=notes,
    )


def append_event(ev: ShadowEvent, *, allow_stale: bool) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(ev), default=str)
    if ev.promote_eligible:
        with EVENTS_PATH.open("a", encoding="utf-8") as f:
            f.write(payload + "\n")
        return EVENTS_PATH
    with STALE_PATH.open("a", encoding="utf-8") as f:
        f.write(payload + "\n")
    if allow_stale:
        with EVENTS_PATH.open("a", encoding="utf-8") as f:
            f.write(payload + "\n")
        return EVENTS_PATH
    return STALE_PATH


def load_events(*, eligible_only: bool = False) -> list[dict[str, Any]]:
    if not EVENTS_PATH.exists():
        return []
    rows = []
    for line in EVENTS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if eligible_only and not row.get("promote_eligible"):
            continue
        rows.append(row)
    return rows


def write_summary(rows: list[dict[str, Any]]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    eligible = [r for r in rows if r.get("promote_eligible")]
    stale_in_main = [r for r in rows if not r.get("promote_eligible")]
    stale_file_n = 0
    if STALE_PATH.exists():
        stale_file_n = sum(1 for line in STALE_PATH.read_text(encoding="utf-8").splitlines() if line.strip())

    def _counts(rs: list[dict]) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in rs:
            out[r.get("action", "?")] = out.get(r.get("action", "?"), 0) + 1
        return out

    rhyme_mismatch = 0
    for r in eligible:
        a = str(r.get("rhyme_prod_letter") or _rhyme_letter(str(r.get("rhyme_prod", ""))))
        b = str(r.get("rhyme_shadow_letter") or _rhyme_letter(str(r.get("rhyme_shadow_equity", ""))))
        if a in "ABCDE" and b in "ABCDE" and a != b:
            rhyme_mismatch += 1

    # Known-event probe: Aug 3–4 2026 rip
    rip_rows = [
        r
        for r in eligible
        if str(r.get("bar_date", "")) in ("2026-08-03", "2026-08-04")
        or (
            float(r.get("ret_2d_pct") or 0) >= RIP_PCT * 100
            and str(r.get("bar_date", "")).startswith("2026-08")
        )
    ]
    rip_trim = [r for r in rip_rows if r.get("action") == "would_trim"]

    sources: dict[str, int] = {}
    for r in rows:
        sources[str(r.get("data_source", "?"))] = sources.get(str(r.get("data_source", "?")), 0) + 1

    latest_el = eligible[-1] if eligible else {}
    latest_any = rows[-1] if rows else {}

    lines = [
        "# VTI tactical $5k — shadow summary",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## Provenance",
        "",
        f"- Rows in main log: {len(rows)}",
        f"- Promote-eligible (fresh sqlite): {len(eligible)}",
        f"- Stale/non-eligible inside main log: {len(stale_in_main)} (should be 0)",
        f"- Rows in stale sidecar: {stale_file_n}",
        f"- data_source counts (main): {sources}",
        f"- Staleness rate vs main: "
        f"{(len(stale_in_main) / len(rows) * 100) if rows else 0:.1f}%",
        "",
        "## Actions (eligible only)",
        "",
        f"{_counts(eligible)}",
        f"Prod vs shadow-equity RHYME mismatches (eligible): {rhyme_mismatch}/{len(eligible)}",
        "",
        "## Known-event check (Aug 2026 rip)",
        "",
        f"- Eligible rows touching rip window: {len(rip_rows)}",
        f"- Of those with action=would_trim: {len(rip_trim)}",
        f"- Pass hint: need ≥1 fresh `would_trim` on/after the rip with gate D/C",
        "",
        "## Pre-registered promote criteria",
        "",
        "See `PROMOTE_CRITERIA.md` — do not invent thresholds after looking at results.",
        "",
        "## Latest eligible event",
        "",
        "```json",
        json.dumps(latest_el or {"none": "no eligible rows yet"}, indent=2),
        "```",
        "",
        "## Latest any (may be stale — do not use for promote)",
        "",
        "```json",
        json.dumps(latest_any or {}, indent=2),
        "```",
        "",
    ]
    SUMMARY_PATH.write_text("\n".join(lines), encoding="utf-8")
    return SUMMARY_PATH


def main() -> int:
    ap = argparse.ArgumentParser(description="VTI tactical $5k shadow once")
    ap.add_argument("--symbol", default="VTI")
    ap.add_argument(
        "--allow-stale",
        action="store_true",
        help="Also append non-eligible rows into main jsonl (still flagged)",
    )
    ap.add_argument(
        "--require-fresh",
        action="store_true",
        help="Exit non-zero if this snapshot is not promote-eligible",
    )
    args = ap.parse_args()

    q = quarantine_pre_provenance_logs()
    print(f"quarantine: {q}", flush=True)

    ev = build_event(symbol=args.symbol)
    path = append_event(ev, allow_stale=bool(args.allow_stale))
    rows = load_events(eligible_only=False)
    summary = write_summary(rows)

    print(
        f"source={ev.data_source} fresh={ev.data_fresh} "
        f"eligible={ev.promote_eligible} bar={ev.bar_date} close={ev.last_close}",
        flush=True,
    )
    if ev.stale_reason:
        print(f"stale_reason={ev.stale_reason}", flush=True)
    print(f"action={ev.action} reason={ev.reason}", flush=True)
    print(
        f"prod={ev.rhyme_prod_letter} shadow_eq={ev.rhyme_shadow_letter} "
        f"garch_mult={ev.size_mult:.2f} sized=${ev.sized_notional:,.0f} "
        f"ret_2d={ev.ret_2d_pct}",
        flush=True,
    )
    print(f"ARIMA advisory={ev.arima_direction} (decision OFF)", flush=True)
    print(f"Wrote {path}", flush=True)
    print(f"Wrote {summary}", flush=True)

    if args.require_fresh and not ev.promote_eligible:
        print("FAIL: --require-fresh but snapshot not promote-eligible", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
