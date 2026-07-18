"""Per-strategy performance tracking and ratings (Realistic Research v1.5)."""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import sqlite3
import time
from pathlib import Path
from typing import Any

import config
from modules.scanner_common import append_tag_if_boost

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
_DB_PATH = ROOT / "data" / "strategy_metrics.db"

STRATEGY_IDS: tuple[str, ...] = (
    "rvol_momentum",
    "orb_breakout",
    "catalyst_scoring",
    "insider_cluster",
    "exec_sell_short",
    "protective_short",
    "stat_arb",
    "atr_sizing",
    "nyse_momentum_base",
    "spy_trend",
)

STRATEGY_LABELS: dict[str, str] = {
    "rvol_momentum": "RVOL Boosted Momentum",
    "orb_breakout": "ORB Breakouts",
    "catalyst_scoring": "Catalyst Scoring",
    "insider_cluster": "Insider Cluster Boosts",
    "exec_sell_short": "Executive Sell Short Signals",
    "protective_short": "Protective Shorts",
    "stat_arb": "Stat Arb Pairs",
    "atr_sizing": "ATR-adjusted Sizing",
    "nyse_momentum_base": "Base NYSE Momentum",
    "spy_trend": "SPY Trend",
}

_RATING_ORDER = ("Excellent", "Good", "Fair", "Weak", "No data")


def _db_path() -> Path:
    path = Path(getattr(config, "STRATEGY_METRICS_DB", str(_DB_PATH)))
    if not path.is_absolute():
        path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS closed_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id TEXT NOT NULL,
            symbol TEXT,
            entry_ts TEXT,
            exit_ts TEXT NOT NULL,
            pnl REAL NOT NULL,
            pnl_pct REAL,
            hold_hours REAL,
            notional REAL,
            source TEXT DEFAULT 'paper',
            tags TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_closed_trades_exit ON closed_trades(exit_ts);
        CREATE INDEX IF NOT EXISTS idx_closed_trades_strategy ON closed_trades(strategy_id);
        CREATE TABLE IF NOT EXISTS daily_snapshots (
            snap_date TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            PRIMARY KEY (snap_date, strategy_id)
        );
        """
    )
    conn.commit()


def rating_from_score(score: float) -> str:
    if score >= 85:
        return "Excellent"
    if score >= 70:
        return "Good"
    if score >= 50:
        return "Fair"
    if score > 0:
        return "Weak"
    return "No data"


def rating_short_label(rating: str) -> str:
    return {
        "Excellent": "Strong",
        "Good": "Good",
        "Fair": "Neutral",
        "Weak": "Weak",
        "No data": "N/A",
    }.get(rating, rating)


def classify_strategy(
    *,
    sleeve: str | None = None,
    reason: str | None = None,
    symbol: str | None = None,
    side: str | None = None,
    strategy: str | None = None,
    trigger_reason: str | None = None,
    tags: list[str] | None = None,
    atr_applied: bool = False,
) -> str:
    """Map fill context to primary strategy id."""
    tags = [t.lower() for t in (tags or [])]
    reason_s = str(reason or "")
    if "|" in reason_s:
        _, tag_blob = reason_s.split("|", 1)
        tags.extend(t.strip().lower() for t in tag_blob.replace("+", ",").split(",") if t.strip())

    sleeve_u = str(sleeve or "").upper()
    sym = config.normalize_symbol(symbol or "")
    backtest = str(strategy or "").lower()

    if atr_applied and "atr" not in tags:
        tags.append("atr")

    if backtest == "spy" or sleeve_u == "SPY" or sym == config.SPY_BOT_SYMBOL:
        return "spy_trend"
    if backtest == "stat_arb" or ("/" in reason_s and "MA" not in reason_s.upper().split("/")[-1]):
        if sleeve_u != "NYSE" and (backtest == "stat_arb" or "pair" in reason_s.lower()):
            return "stat_arb"
    if backtest == "opportunistic_short" or side == "sell" and "short" in str(trigger_reason or "").lower():
        tr = str(trigger_reason or reason_s).lower()
        if "exec" in tr or "ceo" in tr or "cfo" in tr or "insider" in tr:
            return "exec_sell_short"
        return "protective_short"
    if backtest == "opportunistic_short":
        return "protective_short"

    if "catalyst" in tags:
        return "catalyst_scoring"
    if "orb" in tags:
        return "orb_breakout"
    if "rvol" in tags:
        return "rvol_momentum"
    if "insider" in tags:
        return "insider_cluster"
    if "mtf" in tags:
        return "nyse_momentum_base"
    if "atr" in tags and len(tags) == 1:
        return "atr_sizing"

    if sleeve_u == "NYSE" or backtest == "ma50_momentum" or "/MA" in reason_s.upper():
        return "nyse_momentum_base"
    if backtest == "stat_arb":
        return "stat_arb"
    return "nyse_momentum_base"


def classify_nyse_entry_tags(symbol: str, data=None) -> list[str]:
    """Active boost tags at NYSE momentum entry (paper)."""
    tags: list[str] = []
    sym = config.normalize_symbol(symbol)
    if config.effective_rvol_scanner_enabled():
        from modules.volume_analysis import rvol_momentum_rank_boost

        append_tag_if_boost(
            tags, "rvol", enabled=True, boost_fn=rvol_momentum_rank_boost, symbol=sym, data=data
        )
    if config.effective_orb_enabled():
        from modules.orb_strategy import orb_momentum_rank_boost

        append_tag_if_boost(
            tags, "orb", enabled=True, boost_fn=orb_momentum_rank_boost, symbol=sym, data=data
        )
    if config.effective_catalyst_scoring_enabled():
        from modules.catalyst_scoring import catalyst_momentum_rank_boost

        append_tag_if_boost(
            tags,
            "catalyst",
            enabled=True,
            boost_fn=catalyst_momentum_rank_boost,
            symbol=sym,
            data=data,
        )
    if config.effective_insider_signal_boost_enabled():
        from modules.insider_signal_handler import momentum_rank_boost

        append_tag_if_boost(
            tags,
            "insider",
            enabled=True,
            boost_fn=momentum_rank_boost,
            symbol=sym,
            with_data=False,
        )
    if config.effective_multi_timeframe_enabled():
        from modules.multi_timeframe import multi_timeframe_entry_boost

        append_tag_if_boost(
            tags, "mtf", enabled=True, boost_fn=multi_timeframe_entry_boost, symbol=sym, data=data
        )
    return tags


def format_reason_with_tags(pair_key: str, tags: list[str]) -> str:
    if not tags:
        return pair_key
    return f"{pair_key}|{'+'.join(tags)}"


def record_closed_trade(
    strategy_id: str,
    *,
    symbol: str = "",
    entry_ts: str | dt.datetime | None = None,
    exit_ts: str | dt.datetime | None = None,
    pnl: float,
    pnl_pct: float | None = None,
    hold_hours: float | None = None,
    notional: float | None = None,
    source: str = "paper",
    tags: list[str] | None = None,
) -> None:
    if strategy_id not in STRATEGY_IDS:
        return
    if exit_ts is None:
        exit_ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    elif isinstance(exit_ts, dt.datetime):
        exit_ts = exit_ts.isoformat(timespec="seconds")
    if entry_ts is not None and isinstance(entry_ts, dt.datetime):
        entry_ts = entry_ts.isoformat(timespec="seconds")

    with _connect() as conn:
        _init_db(conn)
        conn.execute(
            """
            INSERT INTO closed_trades
            (strategy_id, symbol, entry_ts, exit_ts, pnl, pnl_pct, hold_hours, notional, source, tags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy_id,
                config.normalize_symbol(symbol) if symbol else "",
                entry_ts,
                str(exit_ts),
                float(pnl),
                pnl_pct,
                hold_hours,
                notional,
                source,
                json.dumps(tags or []),
            ),
        )
        conn.commit()


def record_from_round_trip(trip: dict[str, Any], *, source: str = "backtest") -> None:
    """Ingest a backtest attribution round-trip dict."""
    strategy_id = classify_strategy(
        strategy=str(trip.get("strategy") or ""),
        symbol=str(trip.get("symbol") or ""),
        side=str(trip.get("side") or ""),
        trigger_reason=str(trip.get("trigger_reason") or ""),
        tags=list(trip.get("boost_tags") or []),
    )
    hold_bars = trip.get("hold_bars")
    hold_hours = float(hold_bars) * 6.5 if hold_bars is not None else None
    pnl = float(trip.get("pnl_usd") or 0.0)
    record_closed_trade(
        strategy_id,
        symbol=str(trip.get("symbol") or ""),
        pnl=pnl,
        pnl_pct=float(trip["return_pct"]) if trip.get("return_pct") is not None else None,
        hold_hours=hold_hours,
        source=source,
        tags=list(trip.get("boost_tags") or []),
    )


def ingest_backtest_attribution(attribution: dict[str, Any] | None) -> int:
    """Bulk-import round trips from backtest attribution finalize."""
    if not attribution:
        return 0
    count = 0
    for trip in attribution.get("round_trips") or []:
        if not isinstance(trip, dict):
            continue
        record_from_round_trip(trip, source="backtest")
        count += 1
    return count


def _parse_ts(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def sync_from_journal(*, journal_path: Path | None = None) -> int:
    """Rebuild closed trades from paper journal (FIFO per symbol). Paper only."""
    if not config.PAPER_TRADING and not config.paper_aggressive_context():
        return 0
    try:
        from modules.paper_journal import normalize_journal_df, read_journal
        import pandas as pd
    except ImportError:
        return 0

    if journal_path:
        df = read_journal(path=journal_path)
    else:
        df = read_journal()
    df = normalize_journal_df(df)
    if df is None or df.empty:
        return 0

    added = 0
    lots: dict[str, list[dict[str, Any]]] = {}

    for _, row in df.sort_values("timestamp").iterrows():
        sym = config.normalize_symbol(str(row.get("ticker") or row.get("symbol") or ""))
        if not sym:
            continue
        event = str(row.get("event") or "").lower()
        side = str(row.get("side") or "").lower()
        sleeve = str(row.get("sleeve") or "")
        reason = str(row.get("notes") or row.get("pair_key") or "")
        ts = row.get("timestamp")
        price = float(row.get("price") or 0) or 0.0
        qty = float(row.get("qty") or 0) or 0.0
        notional = float(row.get("notional") or 0) or (qty * price if qty and price else 0.0)

        if event in ("fill", "signal", "entry", "buy") and side == "buy":
            tags = []
            if "|" in reason:
                _, blob = reason.split("|", 1)
                tags = [t.strip() for t in blob.replace("+", ",").split(",") if t.strip()]
            lots.setdefault(sym, []).append(
                {
                    "ts": ts,
                    "price": price,
                    "qty": qty or (notional / price if price else 0),
                    "notional": notional,
                    "sleeve": sleeve,
                    "reason": reason,
                    "tags": tags,
                }
            )
        elif event in ("exit", "sell", "close") or side == "sell":
            if sym not in lots or not lots[sym]:
                continue
            entry = lots[sym].pop(0)
            entry_px = float(entry.get("price") or 0)
            sell_px = price or entry_px
            sell_qty = qty or float(entry.get("qty") or 0)
            if sell_px <= 0 or sell_qty <= 0:
                continue
            pnl = (sell_px - entry_px) * sell_qty
            pnl_pct = 100.0 * (sell_px / entry_px - 1.0) if entry_px > 0 else 0.0
            entry_ts = entry.get("ts")
            hold_hours = None
            if pd.notna(ts) and pd.notna(entry_ts):
                hold_hours = (ts - entry_ts).total_seconds() / 3600.0
            strategy_id = classify_strategy(
                sleeve=entry.get("sleeve") or sleeve,
                reason=entry.get("reason") or reason,
                symbol=sym,
                side="sell",
                tags=entry.get("tags"),
            )
            exit_iso = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            with _connect() as conn:
                _init_db(conn)
                dup = conn.execute(
                    "SELECT 1 FROM closed_trades WHERE strategy_id=? AND symbol=? AND exit_ts=? LIMIT 1",
                    (strategy_id, sym, exit_iso),
                ).fetchone()
            if dup:
                continue
            record_closed_trade(
                strategy_id,
                symbol=sym,
                entry_ts=entry_ts.isoformat() if hasattr(entry_ts, "isoformat") else None,
                exit_ts=exit_iso,
                pnl=pnl,
                pnl_pct=pnl_pct,
                hold_hours=hold_hours,
                notional=float(entry.get("notional") or notional),
                source="paper",
                tags=entry.get("tags"),
            )
            added += 1
    return added


def _fetch_trades(days: int | None) -> list[sqlite3.Row]:
    with _connect() as conn:
        _init_db(conn)
        if days is None or days <= 0:
            rows = conn.execute(
                "SELECT * FROM closed_trades ORDER BY exit_ts"
            ).fetchall()
        else:
            cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(days))).isoformat()
            rows = conn.execute(
                "SELECT * FROM closed_trades WHERE exit_ts >= ? ORDER BY exit_ts",
                (cutoff,),
            ).fetchall()
    return list(rows)


def _compute_metrics(rows: list[sqlite3.Row]) -> dict[str, dict[str, Any]]:
    by_strategy: dict[str, list[sqlite3.Row]] = {sid: [] for sid in STRATEGY_IDS}
    for row in rows:
        sid = str(row["strategy_id"])
        if sid in by_strategy:
            by_strategy[sid].append(row)

    out: dict[str, dict[str, Any]] = {}
    for sid in STRATEGY_IDS:
        trades = by_strategy[sid]
        if not trades:
            out[sid] = {
                "strategy_id": sid,
                "label": STRATEGY_LABELS[sid],
                "trade_count": 0,
                "return_pct": 0.0,
                "sharpe": 0.0,
                "win_rate_pct": 0.0,
                "avg_hold_hours": 0.0,
                "pnl_contribution": 0.0,
                "risk_adjusted_score": 0.0,
                "rating": "No data",
            }
            continue

        pnls = [float(r["pnl"]) for r in trades]
        pnl_pcts = [float(r["pnl_pct"]) for r in trades if r["pnl_pct"] is not None]
        holds = [float(r["hold_hours"]) for r in trades if r["hold_hours"] is not None]
        wins = sum(1 for p in pnls if p > 0)
        total_pnl = sum(pnls)
        notional_sum = sum(float(r["notional"] or 0) for r in trades) or 1.0
        return_pct = 100.0 * total_pnl / notional_sum

        if len(pnl_pcts) >= 2:
            mean = sum(pnl_pcts) / len(pnl_pcts)
            var = sum((x - mean) ** 2 for x in pnl_pcts) / (len(pnl_pcts) - 1)
            std = math.sqrt(var) if var > 0 else 0.0
            sharpe = (mean / std) * math.sqrt(min(len(pnl_pcts), 252)) if std > 1e-9 else 0.0
        else:
            sharpe = 0.0

        win_rate = 100.0 * wins / len(trades)
        avg_hold = sum(holds) / len(holds) if holds else 0.0

        score = 50.0
        score += min(25.0, max(-15.0, return_pct * 2.0))
        score += min(20.0, max(-10.0, sharpe * 8.0))
        score += min(15.0, (win_rate - 50.0) * 0.3)
        score = max(0.0, min(100.0, score))

        out[sid] = {
            "strategy_id": sid,
            "label": STRATEGY_LABELS[sid],
            "trade_count": len(trades),
            "return_pct": round(return_pct, 2),
            "sharpe": round(sharpe, 2),
            "win_rate_pct": round(win_rate, 1),
            "avg_hold_hours": round(avg_hold, 1),
            "pnl_contribution": round(total_pnl, 2),
            "risk_adjusted_score": round(score, 1),
            "rating": rating_from_score(score),
        }
    return out


def get_strategy_ratings(days: int = 30) -> dict[str, Any]:
    """Rolling strategy metrics for paper research."""
    if config.PAPER_TRADING or config.paper_aggressive_context():
        try:
            sync_from_journal()
        except Exception as exc:
            logger.debug("strategy metrics journal sync skipped: %s", exc)

    windows = {
        "30d": _compute_metrics(_fetch_trades(30)),
        "90d": _compute_metrics(_fetch_trades(90)),
        "all_time": _compute_metrics(_fetch_trades(None)),
    }
    primary = windows.get(f"{days}d") if days in (30, 90) else windows["30d"]
    if days == 0:
        primary = windows["all_time"]

    ranked = sorted(
        [v for v in primary.values() if v.get("trade_count", 0) > 0],
        key=lambda x: float(x.get("risk_adjusted_score") or 0),
        reverse=True,
    )
    return {
        "as_of": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "window_days": days,
        "strategies": primary,
        "ranked": ranked,
        "top_3": ranked[:3],
        "bottom_3": list(reversed(ranked[-3:])) if len(ranked) >= 3 else [],
        "windows": windows,
    }


def format_strategy_health_banner() -> str | None:
    """Brief startup line: Strategy Health: RVOL Strong, Shorts Neutral."""
    if not config.PAPER_TRADING and not config.backtest_paper_sleeves_context():
        if not config.paper_aggressive_context():
            return None
    try:
        ratings = get_strategy_ratings(days=30)
        ranked = ratings.get("ranked") or []
        if not ranked:
            return ">>> Strategy Health: collecting data (no closed trades yet) <<<"
        parts: list[str] = []
        for row in ranked[:4]:
            short = row["label"].split()[0]
            if "RVOL" in row["label"]:
                short = "RVOL"
            elif "ORB" in row["label"]:
                short = "ORB"
            elif "Catalyst" in row["label"]:
                short = "Catalyst"
            elif "Insider" in row["label"]:
                short = "Insider"
            elif "Executive" in row["label"]:
                short = "ExecShort"
            elif "Protective" in row["label"]:
                short = "Shorts"
            elif "Stat" in row["label"]:
                short = "StatArb"
            elif "ATR" in row["label"]:
                short = "ATR"
            elif "NYSE" in row["label"]:
                short = "NYSE"
            elif "SPY" in row["label"]:
                short = "SPY"
            parts.append(f"{short} {rating_short_label(row['rating'])}")
        return f">>> Strategy Health: {', '.join(parts)} <<<"
    except Exception as exc:
        logger.debug("strategy health banner unavailable: %s", exc)
        return None


def format_weekly_strategy_contribution_note(*, days: int = 30) -> str:
    """One-line active strategy PnL contribution for the weekly report."""
    try:
        ratings = get_strategy_ratings(days=days)
    except Exception as exc:
        logger.debug("strategy ratings unavailable for weekly note: %s", exc)
        return ""
    ranked = ratings.get("ranked") or []
    active = [r for r in ranked if int(r.get("trade_count") or 0) > 0]
    if not active:
        return "Active strategy contribution: no closed trades this window."
    total = sum(float(r.get("pnl_contribution") or 0.0) for r in active)
    parts: list[str] = []
    for row in active[:4]:
        pnl = float(row.get("pnl_contribution") or 0.0)
        share = (pnl / total * 100.0) if abs(total) > 1e-9 else 0.0
        label = str(row.get("label") or "?").split(" (")[0]
        parts.append(f"{label} ${pnl:+.0f} ({share:+.0f}%)")
    return (
        f"Active strategy contribution ({days}d, net ${total:+.0f}): "
        + ", ".join(parts)
    )


def format_telegram_weekly_strategy_block(*, days: int = 30) -> str:
    ratings = get_strategy_ratings(days=days)
    top = ratings.get("top_3") or []
    bottom = ratings.get("bottom_3") or []
    if not top:
        return "\n\nStrategy ratings: no closed trades in window."
    lines = ["\n\nStrategy performance (30d):"]
    lines.append("Top:")
    for row in top:
        lines.append(
            f"  {row['label']}: {row['rating']} "
            f"({row['risk_adjusted_score']:.0f}/100, {row['trade_count']} trades, "
            f"{row['return_pct']:+.1f}%)"
        )
    if bottom and bottom != top:
        lines.append("Watch:")
        for row in bottom[:3]:
            if row in top:
                continue
            lines.append(
                f"  {row['label']}: {row['rating']} ({row['risk_adjusted_score']:.0f}/100)"
            )
    return "\n".join(lines)


def dashboard_rows(*, days: int = 30) -> list[dict[str, str]]:
    ratings = get_strategy_ratings(days=days)
    rows: list[dict[str, str]] = []
    for sid in STRATEGY_IDS:
        m = ratings["strategies"].get(sid) or {}
        rows.append(
            {
                "Strategy": m.get("label", STRATEGY_LABELS[sid]),
                "Rating": str(m.get("rating", "No data")),
                "Score": f"{m.get('risk_adjusted_score', 0):.0f}",
                "Return%": f"{m.get('return_pct', 0):+.1f}",
                "Sharpe": f"{m.get('sharpe', 0):.2f}",
                "Win%": f"{m.get('win_rate_pct', 0):.0f}",
                "Trades": str(m.get("trade_count", 0)),
                "PnL": f"${m.get('pnl_contribution', 0):,.0f}",
                "AvgHold": f"{m.get('avg_hold_hours', 0):.0f}h",
            }
        )
    rows.sort(key=lambda r: float(r.get("Score") or 0), reverse=True)
    return rows


def strategy_health_score_bonus() -> tuple[float, str | None]:
    """Optional Bot Health adjustment from strategy ratings."""
    ratings = get_strategy_ratings(days=30)
    ranked = ratings.get("ranked") or []
    if not ranked:
        return 0.0, None
    scores = [float(r.get("risk_adjusted_score") or 0) for r in ranked]
    avg = sum(scores) / len(scores)
    weak = sum(1 for s in scores if s < 50)
    if avg >= 80 and weak == 0:
        return 4.0, f"Strategy stack strong (avg {avg:.0f}/100)"
    if avg >= 75:
        return 2.0, f"Strategy ratings healthy (avg {avg:.0f}/100)"
    if weak >= 3:
        return -3.0, f"{weak} strategies weak (<50)"
    return 0.0, None


def save_daily_snapshot() -> None:
    """Persist today's per-strategy metrics (lightweight)."""
    today = dt.date.today().isoformat()
    metrics = _compute_metrics(_fetch_trades(30))
    with _connect() as conn:
        _init_db(conn)
        for sid, payload in metrics.items():
            conn.execute(
                """
                INSERT OR REPLACE INTO daily_snapshots (snap_date, strategy_id, metrics_json)
                VALUES (?, ?, ?)
                """,
                (today, sid, json.dumps(payload)),
            )
        conn.commit()
