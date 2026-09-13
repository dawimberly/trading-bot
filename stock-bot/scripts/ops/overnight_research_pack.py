#!/usr/bin/env python3
"""Overnight paper research pack — morning Cursor brief (read-only).

Never places orders. Does not modify strategy defaults or bot entrypoints.

Run from stock-bot/:
  python scripts/ops/overnight_research_pack.py
  python scripts/ops/overnight_research_pack.py --date 2026-07-22
  python scripts/ops/overnight_research_pack.py --no-telegram
  python scripts/ops/overnight_research_pack.py --force

Telegram (Section 1 + 6 only) requires OVERNIGHT_PACK_ENABLED=true and Telegram config.

Scheduling (do not wire into run_paper_bot.py until stable):
  Task Scheduler → Research_Pack.bat around 06:00–07:00 local, or after the close.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import warnings

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("PYTHONTRADING_ROOT", str(ROOT))

# Prefer paper portal env for Alpaca paper position pulls (read-only).
def _resolve_paper_env_file() -> Path | None:
    try:
        from modules.portal_paths import (
            bind_project_root,
            book_env_path,
            get_last_username,
            has_alpaca_config,
        )

        bind_project_root(ROOT)
        username = (get_last_username() or "").strip().lower()
        portal = ROOT / "data" / "portal" / "users"
        candidates: list[Path] = []
        if username and has_alpaca_config(username, "alpaca_paper"):
            candidates.append(book_env_path(username, "alpaca_paper"))
        if portal.is_dir():
            candidates.extend(portal.glob("*/books/alpaca_paper/.env"))
        existing = [p for p in candidates if p.is_file()]
        if existing:
            return max(existing, key=lambda p: p.stat().st_mtime)
    except Exception:
        pass
    stock = ROOT / ".env"
    return stock if stock.is_file() else None


_paper_env = _resolve_paper_env_file()
if _paper_env is not None:
    os.environ.setdefault("PYTHONTRADING_ENV_FILE", str(_paper_env))
os.environ.setdefault("PAPER_TRADING", "true")

import config  # noqa: E402

from modules.health_check import (  # noqa: E402
    resolve_live_heartbeat_path,
    resolve_paper_heartbeat_path,
)
from modules.paper_journal import (  # noqa: E402
    ENTRY_EVENTS,
    EXIT_EVENTS,
    fetch_alpaca_positions,
    normalize_journal_df,
    prefer_fill_rows,
    read_journal,
    row_is_entry,
    row_is_exit,
)

GAP_ALERT_PCT = 3.0
STOP_NEAR_PCT = 1.0
HIGH_STOP_RATE = 0.50
MIN_EXITS_FOR_STOP_RATE = 4

PACK_DIR = ROOT / "data"
REPORT_DIR = ROOT / "reports" / "research"
STATE_PATH = PACK_DIR / "overnight_pack_state.json"
CURSOR_PROMPT_PATH = REPORT_DIR / "morning_cursor_prompt.txt"

SLEEVE_ORDER = ("core", "nyse", "spy", "metal", "crypto", "stat_arb", "other")

# Passive-market / core passive ETFs the bot treats as VTI-core (not active NYSE).
_CORE_PASSIVE = frozenset({"VTI", "VOO", "ITOT"})


@dataclass
class PosMark:
    ticker: str
    sleeve: str
    qty: float
    price: float
    entry: float
    upl: float
    upl_pct: float
    mv: float
    stop_price: float | None = None
    sleeve_reason: str = ""


@dataclass
class PackContext:
    pack_date: date
    asof: datetime
    paper_hb_path: Path | None = None
    paper_hb: dict = field(default_factory=dict)
    live_hb_path: Path | None = None
    live_hb: dict = field(default_factory=dict)
    prior_state: dict = field(default_factory=dict)
    positions: list[PosMark] = field(default_factory=list)
    journal_source: str = "none"
    journal_df: Any = None
    lines: list[str] = field(default_factory=list)
    summary_lines: list[str] = field(default_factory=list)
    recommendation: str = ""
    cursor_idea: str = ""
    high_stop_rate: bool = False


def _log(msg: str = "", *, ctx: PackContext | None = None) -> None:
    print(msg, flush=True)
    if ctx is not None:
        ctx.lines.append(msg)


def _section(title: str, ctx: PackContext) -> None:
    _log("", ctx=ctx)
    _log("=" * 72, ctx=ctx)
    _log(f"  {title}", ctx=ctx)
    _log("=" * 72, ctx=ctx)


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _parse_ts(raw: Any) -> datetime | None:
    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        text = str(raw).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            local_tz = datetime.now().astimezone().tzinfo or timezone.utc
            dt = dt.replace(tzinfo=local_tz)
        return dt
    except (TypeError, ValueError, OSError):
        return None


def _env_bool(key: str, default: str = "false") -> bool:
    return os.getenv(key, default).strip().lower() in ("1", "true", "yes", "on")


def _regime_key(raw: str | None) -> str:
    text = (raw or "").strip().upper()
    for tag in ("RHYME_A", "RHYME_B", "RHYME_C", "RHYME_D", "RHYME_E"):
        if tag in text:
            return tag
    return (raw or "unknown").strip() or "unknown"


def _core_symbols() -> set[str]:
    """VTI core symbol plus common total-market passive aliases."""
    out = set(_CORE_PASSIVE)
    try:
        out.add(config.normalize_symbol(getattr(config, "VTI_CORE_SYMBOL", "VTI")))
    except Exception:
        out.add("VTI")
    return {config.normalize_symbol(s) for s in out if s}


def _classify_sleeve(
    ticker: str, journal_sleeve: str = "", pair_key: str = ""
) -> tuple[str, str]:
    """Return (sleeve, reason) using bot conventions (ops reporting only).

    SPY_BOT_SYMBOL → spy (trend sleeve, not merely a benchmark label).
    VTI_CORE_SYMBOL / VOO / ITOT → core (passive).
    Metals / crypto via config helpers; remaining equities → nyse.
    """
    sym = config.normalize_symbol(ticker)
    js = (journal_sleeve or "").strip().lower()
    pk = (pair_key or "").strip()

    if config.is_crypto(sym):
        return "crypto", "is_crypto"
    if config.is_metal_symbol(sym):
        return "metal", "is_metal_symbol"

    core_syms = _core_symbols()
    if sym in core_syms:
        vti = config.normalize_symbol(getattr(config, "VTI_CORE_SYMBOL", "VTI"))
        if sym == vti:
            return "core", f"VTI_CORE_SYMBOL={vti}"
        return "core", f"core_passive_etf ({sym})"

    if sym == config.normalize_symbol(getattr(config, "SPY_BOT_SYMBOL", "SPY")):
        return "spy", "SPY_BOT_SYMBOL (trend sleeve)"

    # Stat-arb: journal sleeve or pair_key like AAA/BBB (not MA50 momentum tags).
    if "stat" in js or js in ("pair", "pairs", "stat_arb", "statarb"):
        return "stat_arb", f"journal_sleeve={journal_sleeve!r}"
    if pk and "/" in pk:
        pku = pk.upper()
        if "MA50" not in pku and not pku.endswith("/MA50"):
            return "stat_arb", f"pair_key={pk!r}"

    if js in ("nyse", "equity", "momentum"):
        return "nyse", f"journal_sleeve={journal_sleeve!r}"
    if js == "spy":
        return "spy", f"journal_sleeve={journal_sleeve!r}"
    if js in ("metal", "metals"):
        return "metal", f"journal_sleeve={journal_sleeve!r}"
    if js in ("crypto",):
        return "crypto", f"journal_sleeve={journal_sleeve!r}"
    if js in ("vti", "core", "vti_core"):
        return "core", f"journal_sleeve={journal_sleeve!r}"

    # Default active equity book (matches cost_basis.sleeve_for_symbol residual).
    return "nyse", "NYSE default (active equity)"


def _debug_sleeves(positions: list[PosMark], ctx: PackContext) -> None:
    _section("DEBUG — sleeve classification", ctx)
    if not positions:
        _log("  (no positions)", ctx=ctx)
        return
    _log(
        f"  {'Symbol':<10} {'Sleeve':<10} {'MV $':>12}  Reason",
        ctx=ctx,
    )
    for p in sorted(positions, key=lambda x: (-x.mv, x.ticker)):
        _log(
            f"  {p.ticker:<10} {p.sleeve:<10} {p.mv:>12,.2f}  {p.sleeve_reason}",
            ctx=ctx,
        )
    counts: dict[str, int] = {}
    for p in positions:
        counts[p.sleeve] = counts.get(p.sleeve, 0) + 1
    _log(
        "  Totals: "
        + ", ".join(f"{k}={counts[k]}" for k in SLEEVE_ORDER if k in counts),
        ctx=ctx,
    )


def _resolve_paper_heartbeat() -> tuple[Path | None, dict]:
    # Prefer portal alpaca_paper heartbeat (freshest among portal users).
    portal = ROOT / "data" / "portal" / "users"
    portal_hits: list[Path] = []
    if portal.is_dir():
        portal_hits = [
            p
            for p in portal.glob("*/books/alpaca_paper/bot_heartbeat.json")
            if p.is_file()
        ]
    if portal_hits:
        best = max(portal_hits, key=lambda p: p.stat().st_mtime)
        hb = _load_json(best)
        if hb:
            return best, hb

    path = resolve_paper_heartbeat_path(ROOT)
    hb = _load_json(path) if path.is_file() else {}
    # Avoid stale dist/ copies when a fresher root/portal file exists.
    if hb and "dist" not in str(path).replace("\\", "/").lower():
        return path, hb

    for candidate in (
        ROOT / "paper_chase_heartbeat.json",
        ROOT / "bot_heartbeat.json",
        ROOT / "data" / "bot_heartbeat.json",
    ):
        data = _load_json(candidate)
        if data and data.get("paper") is not False:
            return candidate, data

    if hb:
        return path, hb
    return (path if path.is_file() else None), {}


def _resolve_live_heartbeat() -> tuple[Path | None, dict]:
    path = resolve_live_heartbeat_path(ROOT)
    hb = _load_json(path) if path.is_file() else {}
    if hb and hb.get("paper") is not True:
        return path, hb
    return (path if path.is_file() else None), {}


def _paper_journal_candidates() -> list[Path]:
    paths: list[Path] = []
    portal = ROOT / "data" / "portal" / "users"
    if portal.is_dir():
        paths.extend(portal.glob("*/books/alpaca_paper/paper_journal.csv"))
    paths.extend(
        [
            ROOT / "paper_chase_journal.csv",
            ROOT / "paper_journal.csv",
            ROOT / Path(getattr(config, "PAPER_JOURNAL_CSV", "paper_journal.csv")),
        ]
    )
    env_j = os.getenv("PAPER_JOURNAL_CSV", "").strip()
    if env_j:
        paths.append(Path(env_j) if Path(env_j).is_absolute() else ROOT / env_j)
    out: list[Path] = []
    seen: set[Path] = set()
    for p in paths:
        try:
            rp = p.resolve()
        except OSError:
            continue
        if rp in seen:
            continue
        seen.add(rp)
        if p.is_file():
            out.append(p)
    return out


def _load_paper_journal():
    import pandas as pd

    for path in _paper_journal_candidates():
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                df = read_journal(path=path)
                df = normalize_journal_df(df)
            if df is not None and not df.empty:
                return df, str(path)
        except Exception:
            continue
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            df = normalize_journal_df(read_journal())
        if df is not None and not df.empty:
            return df, "paper_journal.default"
    except Exception:
        pass
    return pd.DataFrame(), "none"


def _ensure_journal(ctx: PackContext):
    if ctx.journal_df is not None:
        return ctx.journal_df, ctx.journal_source
    df, source = _load_paper_journal()
    ctx.journal_df = df
    ctx.journal_source = source
    return df, source


def _equity_from_hb(hb: dict) -> float | None:
    try:
        if hb.get("equity") is None:
            return None
        return float(hb["equity"])
    except (TypeError, ValueError):
        return None


def _prior_equity(state: dict, book: str) -> float | None:
    block = (state.get("books") or {}).get(book) or {}
    try:
        if block.get("equity") is None:
            return None
        return float(block["equity"])
    except (TypeError, ValueError):
        return None


def _fmt_equity_delta(now: float | None, prior: float | None, label: str) -> str:
    if now is None:
        return f"{label}: n/a"
    if prior is None or prior <= 0:
        return f"{label}: ${now:,.2f} (no prior snapshot)"
    delta = now - prior
    pct = delta / prior * 100.0
    return f"{label}: ${now:,.2f} ({delta:+,.2f} / {pct:+.2f}% vs prior)"


def _load_positions(ctx: PackContext) -> list[PosMark]:
    rows, err = fetch_alpaca_positions(paper=True)
    if err or not rows:
        # Fallback: empty — sleeve section still uses heartbeat sleeve_pnl.
        if err:
            _log(f"  (paper positions via Alpaca unavailable: {err})", ctx=ctx)
        return []
    out: list[PosMark] = []
    for r in rows:
        sleeve, reason = _classify_sleeve(r.ticker, r.sleeve)
        out.append(
            PosMark(
                ticker=r.ticker,
                sleeve=sleeve,
                qty=float(r.qty),
                price=float(r.current_price),
                entry=float(r.entry_price),
                upl=float(r.unrealized_pnl),
                upl_pct=float(r.unrealized_pnl_pct),
                mv=float(r.market_value),
                sleeve_reason=reason,
            )
        )
    return out


def _attach_stops_if_any(positions: list[PosMark], hb: dict) -> None:
    """Best-effort stop marks from heartbeat / known state files; skip if absent."""
    stops: dict[str, float] = {}
    for key in ("position_stops", "stops", "smart_stops"):
        block = hb.get(key)
        if isinstance(block, dict):
            for sym, val in block.items():
                try:
                    if isinstance(val, dict):
                        px = val.get("stop_price") or val.get("smart_stop_price")
                    else:
                        px = val
                    if px is not None:
                        stops[config.normalize_symbol(sym)] = float(px)
                except (TypeError, ValueError):
                    continue
    state_candidates = [
        ROOT / "data" / "smart_stops_state.json",
        ROOT / "smart_stops_state.json",
    ]
    for path in state_candidates:
        data = _load_json(path)
        for sym, val in (data.get("positions") or data or {}).items():
            if not isinstance(val, dict):
                continue
            px = val.get("stop_price") or val.get("smart_stop_price")
            try:
                if px is not None:
                    stops[config.normalize_symbol(str(sym))] = float(px)
            except (TypeError, ValueError):
                continue
    for pos in positions:
        pos.stop_price = stops.get(pos.ticker)


def section_overnight_summary(ctx: PackContext) -> None:
    _section("1 — Overnight summary", ctx)
    paper_eq = _equity_from_hb(ctx.paper_hb)
    live_eq = _equity_from_hb(ctx.live_hb)
    prior_paper = _prior_equity(ctx.prior_state, "paper")
    prior_live = _prior_equity(ctx.prior_state, "live")

    # Journal fallback for prior paper equity (last equity before pack_date)
    if prior_paper is None:
        try:
            import pandas as pd

            df, _ = _ensure_journal(ctx)
            if (
                df is not None
                and not df.empty
                and "equity" in df.columns
                and "timestamp" in df.columns
            ):
                day_end = datetime.combine(ctx.pack_date, datetime.min.time())
                local_tz = datetime.now().astimezone().tzinfo
                if local_tz:
                    day_end = day_end.replace(tzinfo=local_tz)
                sub = df[df["timestamp"] < pd.Timestamp(day_end)]
                if not sub.empty:
                    prior_paper = float(sub["equity"].dropna().iloc[-1])
        except Exception:
            pass

    regime_now = str(ctx.paper_hb.get("regime") or "unknown")
    regime_prior = str(
        ((ctx.prior_state.get("books") or {}).get("paper") or {}).get("regime") or ""
    )
    rk_now = _regime_key(regime_now)
    rk_prior = _regime_key(regime_prior) if regime_prior else ""
    if rk_prior and rk_prior != rk_now:
        regime_line = f"Regime: {regime_now} (changed from {regime_prior})"
    elif rk_prior:
        regime_line = f"Regime: {regime_now} (unchanged)"
    else:
        regime_line = f"Regime: {regime_now} (no prior to compare)"

    line1 = _fmt_equity_delta(paper_eq, prior_paper, "Paper")
    line2 = _fmt_equity_delta(live_eq, prior_live, "Live")
    line3 = regime_line
    for line in (line1, line2, line3):
        _log(f"  {line}", ctx=ctx)
        ctx.summary_lines.append(line)


def section_open_positions(ctx: PackContext) -> None:
    _section("2 — Open paper positions by unrealized P&L %", ctx)
    if not ctx.positions:
        _log("  (no open paper positions)", ctx=ctx)
        return

    ranked = sorted(ctx.positions, key=lambda p: p.upl_pct, reverse=True)
    top = ranked[:3]
    bottom = list(reversed(ranked[-3:])) if len(ranked) >= 1 else []

    _log("  Top contributors:", ctx=ctx)
    for p in top:
        _log(
            f"    {p.ticker:<8} {p.upl_pct:+.2f}%  UPnL ${p.upl:+,.2f}  "
            f"sleeve={p.sleeve}  mv=${p.mv:,.2f}",
            ctx=ctx,
        )
    _log("  Bottom detractors:", ctx=ctx)
    for p in bottom:
        _log(
            f"    {p.ticker:<8} {p.upl_pct:+.2f}%  UPnL ${p.upl:+,.2f}  "
            f"sleeve={p.sleeve}  mv=${p.mv:,.2f}",
            ctx=ctx,
        )

    prior_marks = ((ctx.prior_state.get("books") or {}).get("paper") or {}).get(
        "marks"
    ) or {}
    gaps: list[str] = []
    for p in ctx.positions:
        try:
            prior_px = float(prior_marks.get(p.ticker))
        except (TypeError, ValueError):
            continue
        if prior_px <= 0 or p.price <= 0:
            continue
        move = (p.price / prior_px - 1.0) * 100.0
        if abs(move) > GAP_ALERT_PCT:
            gaps.append(f"{p.ticker} {move:+.1f}% vs prior mark")
    if gaps:
        _log("  GAP_ALERT:", ctx=ctx)
        for g in gaps:
            _log(f"    ⚠️ {g}", ctx=ctx)
    else:
        _log("  GAP_ALERT: none (or no prior marks)", ctx=ctx)


def section_sleeves(ctx: PackContext) -> None:
    _section("3 — Sleeve breakdown", ctx)
    equity = _equity_from_hb(ctx.paper_hb) or 0.0
    sleeve_pnl = ctx.paper_hb.get("sleeve_pnl") or {}
    exposure = ctx.paper_hb.get("sleeve_exposure") or {}

    stats: dict[str, dict[str, float]] = {
        k: {"count": 0.0, "upl": 0.0, "mv": 0.0} for k in SLEEVE_ORDER
    }

    # Primary: aggregate open positions with corrected labels (core ≠ other).
    for p in ctx.positions:
        bucket = p.sleeve if p.sleeve in stats else "other"
        stats[bucket]["count"] += 1
        stats[bucket]["upl"] += p.upl
        stats[bucket]["mv"] += p.mv

    # Heartbeat exposure fills gaps when a sleeve has marks but no classified rows
    # (e.g. core from vti_core_value if VTI qty dust / missing from Alpaca pull).
    try:
        vti_mv = float(exposure.get("vti_core_value") or 0)
    except (TypeError, ValueError):
        vti_mv = 0.0
    if vti_mv > 0 and stats["core"]["mv"] <= 0:
        stats["core"]["mv"] = vti_mv
        stats["core"]["count"] = max(stats["core"]["count"], 1.0)

    for key in ("spy", "crypto", "nyse", "metal"):
        if stats[key]["count"] > 0 or stats[key]["mv"] > 0:
            continue
        block = sleeve_pnl.get(key) or {}
        try:
            stats[key]["count"] = float(block.get("positions") or 0)
            stats[key]["upl"] = float(block.get("unrealized_pnl") or 0)
        except (TypeError, ValueError):
            pass
        exp_key = f"{key}_value"
        try:
            if exposure.get(exp_key) is not None:
                stats[key]["mv"] = float(exposure.get(exp_key) or 0)
            else:
                stats[key]["mv"] = float(block.get("value") or 0)
        except (TypeError, ValueError):
            pass

    # Note: SPY_BOT_SYMBOL is the trend sleeve (not a pure benchmark) when held.
    _log(
        "  Note: core = VTI_CORE_SYMBOL (+ VOO/ITOT); spy = SPY trend sleeve when held.",
        ctx=ctx,
    )

    leading = dragging = None
    best_upl = float("-inf")
    worst_upl = float("inf")
    _log(
        f"  {'Sleeve':<10} {'Count':>6} {'UPnL $':>12} {'% book':>8}",
        ctx=ctx,
    )
    for name in SLEEVE_ORDER:
        s = stats[name]
        pct = (s["mv"] / equity * 100.0) if equity > 0 else 0.0
        _log(
            f"  {name:<10} {int(s['count']):>6} {s['upl']:>+12,.2f} {pct:>7.1f}%",
            ctx=ctx,
        )
        if s["count"] > 0 or abs(s["upl"]) > 1e-6 or s["mv"] > 0:
            if s["upl"] > best_upl:
                best_upl = s["upl"]
                leading = name
            if s["upl"] < worst_upl:
                worst_upl = s["upl"]
                dragging = name
    if leading or dragging:
        _log(
            f"  Leading: {leading or 'n/a'}  |  Dragging: {dragging or 'n/a'}",
            ctx=ctx,
        )


def section_journal_yesterday(ctx: PackContext) -> None:
    _section("4 — Yesterday’s journal activity", ctx)
    yday = ctx.pack_date - timedelta(days=1)
    df, source = _ensure_journal(ctx)
    _log(f"  Journal: {source}  |  day={yday.isoformat()}", ctx=ctx)
    if df is None or df.empty or "timestamp" not in df.columns:
        _log("  (no journal rows)", ctx=ctx)
        return

    import pandas as pd

    ts = pd.to_datetime(df["timestamp"], errors="coerce")
    day_mask = ts.dt.date == yday
    day = df.loc[day_mask].copy()
    if day.empty:
        _log("  Entries: 0  |  Exits: 0", ctx=ctx)
        return

    events = day["event"].astype(str).str.lower() if "event" in day.columns else None
    sides = day["side"].astype(str).str.lower() if "side" in day.columns else None

    trade = prefer_fill_rows(day)
    if trade is not None and not getattr(trade, "empty", True):
        in_mask = trade.apply(
            lambda r: row_is_entry(r.get("event"), r.get("side")), axis=1
        )
        out_mask = trade.apply(
            lambda r: row_is_exit(r.get("event"), r.get("side")), axis=1
        )
        entries = trade.loc[in_mask]
        exits = trade.loc[out_mask]
    elif events is not None:
        entries = day[events.isin(ENTRY_EVENTS)]
        exits = day[events.isin(EXIT_EVENTS)]
    else:
        entries = day[sides.isin(["buy", "long"])] if sides is not None else day.iloc[0:0]
        exits = day[sides.isin(["sell", "exit"])] if sides is not None else day.iloc[0:0]

    n_in = int(len(entries))
    n_out = int(len(exits))
    _log(f"  Entries: {n_in}  |  Exits: {n_out}", ctx=ctx)

    stop_n = profit_n = 0
    pnls: list[float] = []
    reason_col = None
    for cand in ("exit_reason", "reason", "notes"):
        if cand in exits.columns:
            reason_col = cand
            break
    if reason_col and not exits.empty:
        for _, row in exits.iterrows():
            reason = str(row.get(reason_col) or "").lower()
            if "stop" in reason:
                stop_n += 1
            elif any(x in reason for x in ("profit", "target", "trail", "take")):
                profit_n += 1

    pnl_col = None
    for cand in ("realized_pnl", "pnl"):
        if cand in exits.columns:
            pnl_col = cand
            break
    if pnl_col and not exits.empty:
        pnls = [
            float(x)
            for x in pd.to_numeric(exits[pnl_col], errors="coerce").dropna().tolist()
        ]

    if reason_col:
        _log(
            f"  Exit mix: stop_loss≈{stop_n}  profit/trail≈{profit_n}  "
            f"(reason col={reason_col})",
            ctx=ctx,
        )
    else:
        _log("  Exit reasons: (no exit_reason/reason field)", ctx=ctx)

    if pnls:
        avg = sum(pnls) / len(pnls)
        _log(f"  Avg PnL on exits: ${avg:+,.2f}  (n={len(pnls)})", ctx=ctx)
    else:
        _log("  Avg PnL on exits: n/a", ctx=ctx)

    if n_out >= MIN_EXITS_FOR_STOP_RATE and stop_n / max(n_out, 1) > HIGH_STOP_RATE:
        _log(
            f"  ⚠️ HIGH STOP RATE — stops {stop_n}/{n_out} "
            f"({stop_n / n_out:.0%} > 50%)",
            ctx=ctx,
        )


def section_watchlist(ctx: PackContext) -> None:
    _section("5 — Watchlist", ctx)
    near: list[str] = []
    for p in ctx.positions:
        if p.stop_price is None or p.price <= 0:
            continue
        dist = abs(p.price - p.stop_price) / p.price * 100.0
        if dist <= STOP_NEAR_PCT:
            near.append(
                f"{p.ticker} px={p.price:.2f} stop={p.stop_price:.2f} ({dist:.2f}% away)"
            )
    if near:
        _log("  Near stop (~1%):", ctx=ctx)
        for line in near:
            _log(f"    {line}", ctx=ctx)
    else:
        _log("  Near-stop watch: skipped (no stop data) or none within 1%", ctx=ctx)

    # Optional: top screener names not held
    uni_path = ROOT / "data" / "screener_universe.json"
    if not uni_path.is_file():
        raw = getattr(config, "SCREENER_UNIVERSE_PATH", "")
        if raw:
            uni_path = Path(raw) if Path(raw).is_absolute() else ROOT / raw
    uni = _load_json(uni_path)
    held = {p.ticker for p in ctx.positions}
    score_table = uni.get("score_table") or []
    scored: list[tuple[float, str]] = []
    if isinstance(score_table, list):
        for row in score_table:
            if not isinstance(row, dict):
                continue
            sym = config.normalize_symbol(str(row.get("ticker") or ""))
            if not sym or sym in held:
                continue
            try:
                score = float(row.get("composite") or row.get("momentum") or 0)
            except (TypeError, ValueError):
                continue
            scored.append((score, sym))
    if scored:
        scored.sort(reverse=True)
        top = scored[:5]
        _log("  Screener momentum not held (top 5):", ctx=ctx)
        for score, sym in top:
            _log(f"    {sym:<8} score={score:.3f}", ctx=ctx)
    else:
        _log("  Screener watch: skipped (no scores) or empty", ctx=ctx)


def section_recommendation(ctx: PackContext) -> None:
    _section("6 — One-line recommendation", ctx)
    regime = _regime_key(str(ctx.paper_hb.get("regime") or ""))
    sleeve_pnl = ctx.paper_hb.get("sleeve_pnl") or {}
    total_upl = 0.0
    for block in sleeve_pnl.values():
        if isinstance(block, dict):
            try:
                total_upl += float(block.get("unrealized_pnl") or 0)
            except (TypeError, ValueError):
                pass

    # Stop-rate hint from yesterday (reuse cached journal).
    high_stops = False
    try:
        df, _ = _ensure_journal(ctx)
        if df is not None and not df.empty and "timestamp" in df.columns:
            import pandas as pd

            yday = ctx.pack_date - timedelta(days=1)
            ts = pd.to_datetime(df["timestamp"], errors="coerce")
            day = df.loc[ts.dt.date == yday]
            if not day.empty and "event" in day.columns:
                exits = day[day["event"].astype(str).str.lower().isin(EXIT_EVENTS)]
                reason_col = next(
                    (c for c in ("exit_reason", "reason", "notes") if c in exits.columns),
                    None,
                )
                if reason_col is not None and len(exits) >= MIN_EXITS_FOR_STOP_RATE:
                    stops = (
                        exits[reason_col]
                        .astype(str)
                        .str.lower()
                        .str.contains("stop")
                        .sum()
                    )
                    high_stops = stops / len(exits) > HIGH_STOP_RATE
    except Exception:
        high_stops = False
    ctx.high_stop_rate = high_stops

    if regime in ("RHYME_B", "RHYME_E") or (regime in ("RHYME_B", "RHYME_E") and high_stops):
        rec = "Reduce exposure; let positions breathe"
        if high_stops:
            rec = "Reduce exposure; let positions breathe (high stop rate overnight)"
    elif regime == "RHYME_C" and total_upl >= 0:
        rec = "Normal ops; adds only on hygiene rules"
    elif regime == "RHYME_D":
        rec = "Hold; few or no new entries until regime clarifies"
    elif regime == "RHYME_A":
        rec = "Normal ops but watch size; prefer hygiene cuts over chasing"
    elif high_stops:
        rec = "Tighten risk; review stop clustering before new entries"
    elif total_upl < 0:
        rec = "Hold and triage losers; no force-deploy"
    else:
        rec = "Normal ops; keep paper hygiene (max adds / same-day block) on"

    # Prefix with regime tag for clarity
    ctx.recommendation = f"{regime}: {rec}"
    _log(f"  {ctx.recommendation}", ctx=ctx)

    # Morning Cursor experiment idea (paper-only)
    if high_stops or regime in ("RHYME_B", "RHYME_E"):
        idea = (
            "Paper-only: tighten NYSE same-day reentry block / max-adds hygiene "
            "for one week; do not raise conviction floors; compare stop rate vs prior week."
        )
    elif regime == "RHYME_D":
        idea = (
            "Paper-only: gate new NYSE entries when regime is RHYME_D "
            "(allow manages/exits only); measure fill count and DD vs baseline."
        )
    else:
        idea = (
            "Paper-only: keep NYSE entry hygiene on; A/B a slightly higher "
            "min-notional floor ($25→$40) to cut dust adds — no new indicators."
        )
    ctx.cursor_idea = idea


def _write_outputs(ctx: PackContext) -> tuple[Path, Path]:
    PACK_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    txt_path = PACK_DIR / f"overnight_pack_{ctx.pack_date.isoformat()}.txt"
    md_path = REPORT_DIR / f"{ctx.pack_date.isoformat()}.md"

    body = "\n".join(ctx.lines) + "\n"
    header = (
        f"Overnight research pack — {ctx.pack_date.isoformat()}\n"
        f"Generated: {ctx.asof.isoformat(timespec='seconds')}\n"
        f"Paper HB: {ctx.paper_hb_path or 'n/a'}\n"
        f"Live HB:  {ctx.live_hb_path or 'n/a'}\n"
        f"READ-ONLY — no orders\n"
    )
    txt_path.write_text(header + "\n" + body, encoding="utf-8")
    md_path.write_text(
        f"# Overnight research pack — {ctx.pack_date.isoformat()}\n\n"
        f"_Generated {ctx.asof.isoformat(timespec='seconds')} · read-only_\n\n"
        f"```\n{body}\n```\n\n"
        f"## Morning Cursor idea (paper only)\n\n{ctx.cursor_idea}\n",
        encoding="utf-8",
    )
    CURSOR_PROMPT_PATH.write_text(
        "Paper-only experiment for this morning (do not touch live):\n"
        f"{ctx.cursor_idea}\n",
        encoding="utf-8",
    )
    return txt_path, md_path


def _save_state(ctx: PackContext) -> None:
    marks = {p.ticker: p.price for p in ctx.positions if p.price > 0}
    state = {
        "date": ctx.pack_date.isoformat(),
        "saved_at": ctx.asof.isoformat(timespec="seconds"),
        "books": {
            "paper": {
                "equity": _equity_from_hb(ctx.paper_hb),
                "regime": ctx.paper_hb.get("regime"),
                "marks": marks,
                "heartbeat": str(ctx.paper_hb_path) if ctx.paper_hb_path else None,
            },
            "live": {
                "equity": _equity_from_hb(ctx.live_hb),
                "regime": ctx.live_hb.get("regime"),
                "heartbeat": str(ctx.live_hb_path) if ctx.live_hb_path else None,
            },
        },
    }
    PACK_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _open_report(path: Path) -> None:
    """Open markdown via os.startfile (Windows). Never crash — warn on failure."""
    abs_path = path.resolve()
    try:
        if sys.platform == "win32":
            os.startfile(str(abs_path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            import subprocess

            subprocess.run(["open", str(abs_path)], check=False)
        else:
            import subprocess

            subprocess.run(["xdg-open", str(abs_path)], check=False)
        print(f"[overnight_pack] Opened {abs_path}", flush=True)
    except Exception as exc:
        print(
            f"[overnight_pack] WARNING: could not open report ({abs_path}): {exc}",
            flush=True,
        )


def _editor_open_enabled(*, cli_open: bool) -> bool:
    """Honor CLI --open or OPEN_RESEARCH_PACK_IN_EDITOR from env / stock-bot .env."""
    if cli_open:
        return True
    if _env_bool("OPEN_RESEARCH_PACK_IN_EDITOR", "false"):
        return True
    # Paper book .env may be primary; still honor stock-bot/.env for this ops flag.
    try:
        from dotenv import dotenv_values

        vals = dotenv_values(ROOT / ".env") or {}
        raw = str(vals.get("OPEN_RESEARCH_PACK_IN_EDITOR") or "").strip().lower()
        return raw in ("1", "true", "yes", "on")
    except Exception:
        return False


def _maybe_telegram(ctx: PackContext, *, enabled: bool) -> None:
    if not enabled:
        _log("Telegram: skipped (--no-telegram or OVERNIGHT_PACK_ENABLED≠true)")
        return
    try:
        from modules import alerts
    except Exception as exc:
        _log(f"Telegram: unavailable ({exc})")
        return
    if not config.get_telegram_config():
        _log("Telegram: not configured")
        return
    text = (
        f"Overnight pack {ctx.pack_date.isoformat()}\n"
        + "\n".join(ctx.summary_lines[:3])
        + f"\n\n{ctx.recommendation}"
    )
    ok = alerts.send_telegram(text)
    _log(f"Telegram: {'sent' if ok else 'failed'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Overnight paper research pack")
    parser.add_argument(
        "--date",
        help="Pack calendar date YYYY-MM-DD (default: today local)",
    )
    parser.add_argument(
        "--no-telegram",
        action="store_true",
        help="Do not send Telegram even if OVERNIGHT_PACK_ENABLED=true",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild even if today's pack already exists",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the markdown report when finished (same as OPEN_RESEARCH_PACK_IN_EDITOR=true)",
    )
    parser.add_argument(
        "--debug-sleeves",
        action="store_true",
        help="Print each open symbol → sleeve + classification reason",
    )
    args = parser.parse_args(argv)

    if args.date:
        pack_date = date.fromisoformat(args.date)
    else:
        pack_date = datetime.now().astimezone().date()

    txt_path = PACK_DIR / f"overnight_pack_{pack_date.isoformat()}.txt"
    if txt_path.is_file() and not args.force:
        abs_txt = txt_path.resolve()
        abs_md = (REPORT_DIR / f"{pack_date.isoformat()}.md").resolve()
        print(f"Pack already exists for {pack_date.isoformat()}: {abs_txt}")
        print(f"Markdown: {abs_md}")
        print("Use --force to rebuild. Exit 0.")
        return 0

    ctx = PackContext(pack_date=pack_date, asof=datetime.now().astimezone())
    ctx.prior_state = _load_json(STATE_PATH)
    ctx.paper_hb_path, ctx.paper_hb = _resolve_paper_heartbeat()
    ctx.live_hb_path, ctx.live_hb = _resolve_live_heartbeat()

    _log(
        f"Overnight research pack — {pack_date.isoformat()} (read-only)",
        ctx=ctx,
    )
    _log(f"Paper HB: {ctx.paper_hb_path or 'n/a'}", ctx=ctx)
    _log(f"Live HB:  {ctx.live_hb_path or 'n/a'}", ctx=ctx)

    ctx.positions = _load_positions(ctx)
    _attach_stops_if_any(ctx.positions, ctx.paper_hb)
    if args.debug_sleeves:
        _debug_sleeves(ctx.positions, ctx)

    section_overnight_summary(ctx)
    section_open_positions(ctx)
    section_sleeves(ctx)
    section_journal_yesterday(ctx)
    section_watchlist(ctx)
    section_recommendation(ctx)

    txt_out, md_out = _write_outputs(ctx)
    _save_state(ctx)

    abs_txt = txt_out.resolve()
    abs_md = md_out.resolve()
    abs_prompt = CURSOR_PROMPT_PATH.resolve()

    _log("", ctx=ctx)
    _log(f"Wrote {abs_txt}", ctx=ctx)
    _log(f"Wrote {abs_md}", ctx=ctx)
    _log(f"Wrote {abs_prompt}", ctx=ctx)
    # Explicit absolute path for PyCharm / Cursor (weekly-style UX)
    print(f"[overnight_pack] Full report: {abs_md}", flush=True)

    tg_on = _env_bool("OVERNIGHT_PACK_ENABLED", "false") and not args.no_telegram
    _maybe_telegram(ctx, enabled=tg_on)

    if _editor_open_enabled(cli_open=args.open):
        _open_report(md_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
