"""Local weekly monitoring report (Markdown + HTML) — no external services."""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import config
from modules import alerts
from modules.status_metrics import _merge_journal_series, fmt_pct
from modules.weekly_summary import (
    WeeklySummaryData,
    gather_weekly_summary,
    week_id,
    _load_heartbeat,
    _week_start_monday,
    _journal_paths,
)

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")
_ROOT = Path(__file__).resolve().parents[1]
_REPORTS_DIR = _ROOT / "reports" / "weekly"
_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


@dataclass
class WeeklyReportData:
    summary: WeeklySummaryData
    research_version: str
    equity_curve: list[tuple[str, float]] = field(default_factory=list)
    equity_sparkline: str = ""
    regime_counts: dict[str, int] = field(default_factory=dict)
    regime_changes: list[str] = field(default_factory=list)
    stat_arb_open: list[dict[str, Any]] = field(default_factory=list)
    stat_arb_trades: list[dict[str, Any]] = field(default_factory=list)
    short_trades: list[dict[str, Any]] = field(default_factory=list)
    tail_risk_events: list[str] = field(default_factory=list)
    tail_risk_status: dict[str, Any] = field(default_factory=dict)
    metrics_30d: dict[str, Any] = field(default_factory=dict)
    metrics_alltime: dict[str, Any] = field(default_factory=dict)
    bubble_score: float | None = None
    bubble_score_100: float | None = None
    buffett_indicator: dict[str, Any] = field(default_factory=dict)
    stat_arb_contribution_pct: float | None = None
    bot_health: dict[str, Any] = field(default_factory=dict)


def reports_dir() -> Path:
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    return _REPORTS_DIR


def weekly_report_due(*, market_open: bool | None = None, test_mode: bool = False) -> bool:
    """Same gate as Friday Telegram: Fri after 16:30 ET, market closed, once per week."""
    if test_mode:
        return True
    from modules.weekly_telegram_summary import weekly_telegram_due

    if not weekly_telegram_due(market_open=market_open, test_mode=False):
        return False
    state = alerts._load_state()
    return state.get("last_weekly_report_week") != week_id(datetime.now(_ET).date())


def _mark_report_generated(report_date: date) -> None:
    state = alerts._load_state()
    state["last_weekly_report_week"] = week_id(report_date)
    state["last_weekly_report_at"] = datetime.now().isoformat()
    alerts._save_state(state)


def _sparkline(values: list[float], width: int = 40) -> str:
    if not values:
        return "(no equity data)"
    if len(values) > width:
        step = len(values) / width
        sampled = [values[int(i * step)] for i in range(width)]
    else:
        sampled = values
    lo, hi = min(sampled), max(sampled)
    if hi <= lo:
        return _SPARK_BLOCKS[0] * len(sampled)
    out = []
    for v in sampled:
        idx = int((v - lo) / (hi - lo) * (len(_SPARK_BLOCKS) - 1))
        out.append(_SPARK_BLOCKS[idx])
    return "".join(out)


def _equity_curve_week(
    series: list[tuple[datetime, float]],
    week_start: date,
) -> tuple[list[tuple[str, float]], str]:
    points = [(ts, eq) for ts, eq in series if ts.date() >= week_start]
    if not points:
        return [], "(no data this week)"
    by_day: dict[date, float] = {}
    for ts, eq in points:
        by_day[ts.date()] = eq
    curve = [(d.isoformat(), by_day[d]) for d in sorted(by_day)]
    return curve, _sparkline([eq for _, eq in curve])


def _load_week_journal(week_start: date):
    import pandas as pd

    frames = []
    for path in _journal_paths():
        try:
            df = pd.read_csv(path)
            if not df.empty:
                frames.append(df)
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    if "timestamp" not in df.columns:
        return pd.DataFrame()
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"])
    return df.loc[df["timestamp"].dt.date >= week_start].copy()


def _regime_summary(df, hb: dict[str, Any]) -> tuple[dict[str, int], list[str]]:
    counts: dict[str, int] = {}
    changes: list[str] = []
    if df is not None and not df.empty and "regime" in df.columns:
        for reg, n in df["regime"].astype(str).value_counts().items():
            r = reg.strip()
            if r and r.lower() not in ("nan", "none"):
                counts[r] = int(n)
        if "event" in df.columns:
            cycles = df.loc[df["event"].astype(str).str.lower() == "cycle"]
            if not cycles.empty:
                last_reg = None
                for _, row in cycles.sort_values("timestamp").iterrows():
                    reg = str(row.get("regime") or "").strip()
                    if not reg:
                        continue
                    if last_reg and reg != last_reg:
                        ts = row["timestamp"].strftime("%Y-%m-%d %H:%M")
                        changes.append(f"{ts}: {last_reg} → {reg}")
                    last_reg = reg
    cur = str(hb.get("regime") or "").strip()
    if cur and cur not in counts:
        counts[cur] = counts.get(cur, 0) + 1
    return counts, changes[-8:]


def _stat_arb_open_book() -> list[dict[str, Any]]:
    path = Path(getattr(config, "STAT_ARB_BOOK_FILE", "stat_arb_open_book.json"))
    if not path.is_file():
        path = _ROOT / "stat_arb_open_book.json"
    if not path.is_file():
        return []
    try:
        book = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    if not isinstance(book, dict):
        return []
    rows = []
    for pair_key, pos in book.items():
        if not isinstance(pos, dict):
            continue
        rows.append(
            {
                "pair": pair_key,
                "long": pos.get("long_symbol", ""),
                "short": pos.get("short_symbol", ""),
                "entry_z": pos.get("entry_z"),
                "leg_notional": pos.get("leg_notional"),
                "entry_bar": pos.get("entry_bar"),
            }
        )
    return rows


def _stat_arb_week_trades(df) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        pk = str(row.get("pair_key") or "")
        sym = str(row.get("symbol") or row.get("ticker") or "")
        if "/MA" in pk.upper():
            continue
        is_pair = "/" in pk or ("/" in sym and "MA" not in sym.upper())
        sleeve = str(row.get("sleeve") or "").lower()
        if not is_pair and sleeve not in ("stat_arb", "pair", ""):
            if not (pk and "/" in pk):
                continue
        if not is_pair and not pk:
            continue
        rows.append(
            {
                "time": row["timestamp"].strftime("%Y-%m-%d %H:%M"),
                "event": str(row.get("event", "")),
                "pair": pk or sym,
                "side": str(row.get("side", "")),
                "notional": float(row["notional"])
                if "notional" in row and str(row["notional"]).strip()
                else None,
                "z": row.get("z_score"),
            }
        )
    rows.sort(key=lambda r: r.get("notional") or 0, reverse=True)
    return rows[:15]


def _short_week_trades(df) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        sleeve = str(row.get("sleeve") or "").upper()
        pk = str(row.get("pair_key") or row.get("reason") or "")
        strategy = str(row.get("strategy") or "")
        if sleeve != "SHORT" and strategy != "opportunistic_short" and "/SHORT/" not in pk.upper():
            continue
        sym = str(row.get("symbol") or row.get("ticker") or "")
        rows.append(
            {
                "time": row["timestamp"].strftime("%Y-%m-%d %H:%M"),
                "event": str(row.get("event", "")),
                "symbol": sym or pk.split("/")[0],
                "side": str(row.get("side", "")),
                "notional": float(row["notional"])
                if "notional" in row and str(row["notional"]).strip()
                else None,
                "reason": pk,
            }
        )
    rows.sort(key=lambda r: r.get("notional") or 0, reverse=True)
    return rows[:10]


def _tail_risk_section(hb: dict[str, Any], df) -> tuple[list[str], dict[str, Any]]:
    status: dict[str, Any] = {
        "tail_risk_controls": config.effective_tail_risk_controls(),
        "research_version": getattr(config, "REALISTIC_RESEARCH_VERSION", "n/a"),
        "vol_ceiling_pct": getattr(config, "PAPER_VOL_CEILING_PCT", None),
        "portfolio_vol_ceiling_pct": getattr(config, "PORTFOLIO_VOL_CEILING_PCT", None),
        "dynamic_vol_score": hb.get("dynamic_vol_score"),
        "halted": hb.get("halted"),
    }
    wisdom = hb.get("wisdom") or {}
    if wisdom:
        status["wisdom_paused"] = wisdom.get("paused")
        status["governor_stress"] = wisdom.get("governor_stress")
        status["sizing_multiplier"] = wisdom.get("sizing_multiplier")
        status["gap_tier"] = wisdom.get("gap_tier")

    events: list[str] = []
    if hb.get("halted"):
        events.append("Trading halt flag active in heartbeat")
    if wisdom.get("governor_stress"):
        events.append("Wisdom governor stress active")
    if wisdom.get("paused"):
        events.append("Wisdom layer paused entries")
    sm = wisdom.get("sizing_multiplier")
    if sm is not None and float(sm) < 0.99:
        events.append(f"Wisdom sizing multiplier reduced to {float(sm):.2f}×")

    dvs = hb.get("dynamic_vol_score")
    if dvs is not None:
        try:
            ann = float(dvs) * (252**0.5)
            ceiling = float(config.effective_vol_ceiling_pct())
            if ann > ceiling:
                events.append(
                    f"Vol ceiling breach: ann vol ~{ann:.1%} > {ceiling:.0%} cap"
                )
        except (TypeError, ValueError):
            pass

    skip_daily = hb.get("entry_skip_daily")
    if isinstance(skip_daily, dict) and skip_daily:
        top = sorted(skip_daily.items(), key=lambda x: -int(x[1]))[:5]
        events.append(
            "Entry skips (cycle): " + ", ".join(f"{k}={v}" for k, v in top)
        )

    if df is not None and not df.empty and "notes" in df.columns:
        note_text = " ".join(df["notes"].astype(str).tolist()).lower()
        for token in ("rhyme_b", "dd_risk", "vol_ceiling", "soft_pause", "no_room"):
            if token in note_text:
                events.append(f"Journal mentions `{token}` this week")

    esr = hb.get("entry_skip_reason")
    if esr:
        events.append(f"Latest entry skip: {esr}")

    if not events:
        events.append("No tail-risk activations flagged this week.")
    return events, status


def _stat_arb_week_pnl(df) -> float | None:
    if df is None or df.empty:
        return None
    total = 0.0
    found = False
    for _, row in df.iterrows():
        sleeve = str(row.get("sleeve") or "").upper()
        pk = str(row.get("pair_key") or row.get("reason") or "")
        if sleeve != "STAT_ARB" and "STAT_ARB" not in pk.upper() and "/PAIR/" not in pk.upper():
            continue
        if str(row.get("event", "")).lower() not in ("exit", "close", "cover"):
            continue
        for col in ("pnl_usd", "pnl", "realized_pnl"):
            if col in row and str(row[col]).strip():
                try:
                    total += float(row[col])
                    found = True
                except (TypeError, ValueError):
                    pass
                break
    return total if found else None


def gather_weekly_report(
    *,
    equity: float | None = None,
    cash: float | None = None,
    regime: str = "",
    wisdom: dict | None = None,
    sleeves: dict | None = None,
    paper: bool | None = None,
) -> WeeklyReportData:
    summary = gather_weekly_summary(
        equity=equity,
        cash=cash,
        regime=regime,
        wisdom=wisdom,
        sleeves=sleeves,
        paper=paper,
    )
    hb = _load_heartbeat()
    week_start = _week_start_monday(summary.as_of.date())
    series = _merge_journal_series(
        paper_chase=config.paper_chase_mode_enabled(),
        live_only=not (paper if paper is not None else config.PAPER_TRADING),
    )
    curve, spark = _equity_curve_week(series, week_start)
    df = _load_week_journal(week_start)
    regime_counts, regime_changes = _regime_summary(df, hb)
    tail_events, tail_status = _tail_risk_section(hb, df)

    from modules.status_metrics import metrics_30d
    from modules.bot_health_score import compute_bot_health_score

    paper_chase = config.paper_chase_mode_enabled()
    m30 = metrics_30d(paper_chase=paper_chase)
    mall: dict[str, Any] = {"return_pct": None, "sharpe": None, "max_drawdown_pct": None}
    try:
        from modules.wisdom_evaluator import live_metrics

        book = "paper" if paper_chase else "live"
        raw_all = live_metrics(3650, book_type=book, live_only=not paper_chase)
        if raw_all:
            mall = {
                "return_pct": float(raw_all.get("return_pct") or 0),
                "sharpe": float(raw_all.get("sharpe") or 0),
                "max_drawdown_pct": float(raw_all.get("max_drawdown_pct") or 0),
            }
    except Exception:
        pass

    bubble_score: float | None = None
    bubble_score_100: float | None = None
    buffett: dict[str, Any] | None = None
    try:
        from modules.bubble_risk import compute_bubble_risk_from_live_context

        regime_str = str(hb.get("regime") or summary.regime or "")
        bubble_ctx = compute_bubble_risk_from_live_context(regime=regime_str, hb=hb)
        if bubble_ctx:
            bubble_score_100 = float(bubble_ctx["score_100"])
            bubble_score = float(bubble_ctx["score_normalized"])
            buffett = dict(bubble_ctx.get("buffett") or {})
    except Exception:
        bubble_score = None
        bubble_score_100 = None
        buffett = None

    stat_arb_pnl = _stat_arb_week_pnl(df)
    stat_arb_contrib: float | None = None
    if stat_arb_pnl is not None and summary.week_pnl_usd and summary.week_pnl_usd != 0:
        stat_arb_contrib = stat_arb_pnl / float(summary.week_pnl_usd) * 100.0

    health = compute_bot_health_score(
        hb=hb,
        metrics_30d=m30,
        metrics_alltime=mall,
        bubble_score=bubble_score_100 if bubble_score_100 is not None else bubble_score,
        stat_arb_pnl_week=stat_arb_pnl,
        short_trade_count=len(_short_week_trades(df)),
        heartbeat_age_min=summary.heartbeat_age_min,
    )

    return WeeklyReportData(
        summary=summary,
        research_version=str(getattr(config, "REALISTIC_RESEARCH_VERSION", "1.5")),
        equity_curve=curve,
        equity_sparkline=spark,
        regime_counts=regime_counts,
        regime_changes=regime_changes,
        stat_arb_open=_stat_arb_open_book(),
        stat_arb_trades=_stat_arb_week_trades(df),
        short_trades=_short_week_trades(df),
        tail_risk_events=tail_events,
        tail_risk_status=tail_status,
        metrics_30d=m30,
        metrics_alltime=mall,
        bubble_score=bubble_score,
        bubble_score_100=bubble_score_100,
        buffett_indicator=buffett or {},
        stat_arb_contribution_pct=stat_arb_contrib,
        bot_health=health,
    )


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_No data._\n"
    line = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = "\n".join("| " + " | ".join(cells) + " |" for cells in rows)
    return f"{line}\n{sep}\n{body}\n"


def render_markdown(data: WeeklyReportData) -> str:
    s = data.summary
    iso = s.as_of.isocalendar()
    pnl_s = f"${s.week_pnl_usd:+,.2f}" if s.week_pnl_usd is not None else "n/a"
    lines = [
        f"# Weekly Monitoring Report — {s.account_label}",
        "",
        f"**Week {iso.week} ({iso.year})** · Generated {s.as_of:%Y-%m-%d %H:%M} ET",
        f"**Profile:** Realistic Research v{data.research_version}",
        "",
        "## Summary",
        "",
        f"- **Equity:** ${s.equity:,.2f}",
        f"- **Weekly P&L:** {pnl_s} ({fmt_pct(s.week_return_pct)})",
        f"- **vs {config.VTI_CORE_SYMBOL}:** {fmt_pct(s.vs_vti_pct)} "
        f"(benchmark {fmt_pct(s.vti_week_return_pct)})",
        f"- **Max drawdown (week):** "
        f"{s.week_max_dd_pct:.2f}%" if s.week_max_dd_pct is not None else "- **Max drawdown (week):** n/a",
        (
            f"- **Cash:** ${s.cash:,.2f} · **Invested:** {s.invested_pct:.0f}%"
            if s.invested_pct is not None
            else f"- **Cash:** ${s.cash:,.2f}"
        ),
        "",
        "## Bot Health Score",
        "",
        f"- **Score:** {data.bot_health.get('score', 'n/a')}/100 — "
        f"**{data.bot_health.get('grade', 'n/a')}**",
    ]
    for note in data.bot_health.get("notes") or []:
        lines.append(f"- {note}")
    lines.append("")
    m30 = data.metrics_30d or {}
    mall = data.metrics_alltime or {}
    lines.extend(
        [
            "## Performance metrics",
            "",
            _md_table(
                ["Window", "Return", "Sharpe", "Max DD"],
                [
                    [
                        "30d",
                        fmt_pct(m30.get("return_pct")) or "n/a",
                        f"{float(m30['sharpe']):.2f}" if m30.get("sharpe") is not None else "n/a",
                        f"{float(m30['max_drawdown_pct']):.2f}%"
                        if m30.get("max_drawdown_pct") is not None
                        else "n/a",
                    ],
                    [
                        "All-time",
                        fmt_pct(mall.get("return_pct")) or "n/a",
                        f"{float(mall['sharpe']):.2f}" if mall.get("sharpe") is not None else "n/a",
                        f"{float(mall['max_drawdown_pct']):.2f}%"
                        if mall.get("max_drawdown_pct") is not None
                        else "n/a",
                    ],
                ],
            ),
            "",
        ]
    )
    if data.bubble_score_100 is not None:
        lines.append(f"- **Bubble Risk Score:** {data.bubble_score_100:.0f}/100")
        if data.buffett_indicator.get("ratio_pct") is not None:
            bi = data.buffett_indicator
            lines.append(
                f"- **Buffett Indicator:** {bi['ratio_pct']:.1f}% of GDP — "
                f"**{bi.get('signal', 'n/a')}** "
                f"(threshold {bi.get('overvalued_threshold', config.BUFFETT_OVERVALUED_THRESHOLD):.0f}%)"
            )
        lines.append("")
    elif data.bubble_score is not None:
        lines.append(f"- **Bubble Risk Score:** {data.bubble_score:.3f}")
        lines.append("")
    if data.stat_arb_contribution_pct is not None:
        lines.append(
            f"- **Stat Arb week contribution:** {data.stat_arb_contribution_pct:+.1f}% of weekly P&L"
        )
        lines.append("")
    lines.extend(
        [
            "## Equity curve (this week)",
            "",
            f"```\n{data.equity_sparkline}\n```",
            "",
        ]
    )
    if data.equity_curve:
        rows = [
            [d, f"${eq:,.2f}"] for d, eq in data.equity_curve[-10:]
        ]
        lines.append(_md_table(["Date", "Equity"], rows))
        lines.append("")

    lines.extend(
        [
            "## Weekly P&L vs VTI",
            "",
            _md_table(
                ["Metric", "Account", "VTI"],
                [
                    [
                        "Week return",
                        fmt_pct(s.week_return_pct) or "n/a",
                        fmt_pct(s.vti_week_return_pct) or "n/a",
                    ],
                    [
                        "Excess vs VTI",
                        fmt_pct(s.vs_vti_pct) or "n/a",
                        "—",
                    ],
                ],
            ),
            "",
            "## Regime summary",
            "",
        ]
    )
    lines.append(f"**Current:** {s.regime}")
    lines.append(f"**Wisdom:** {s.wisdom_mode} · **Vol tier:** {s.volatility}")
    if data.regime_counts:
        lines.append("")
        lines.append("**Regime observations (journal):**")
        for reg, n in sorted(data.regime_counts.items(), key=lambda x: -x[1])[:8]:
            lines.append(f"- {reg}: {n} cycle(s)")
    if data.regime_changes:
        lines.append("")
        lines.append("**Regime changes:**")
        for ch in data.regime_changes:
            lines.append(f"- {ch}")
    lines.append("")

    try:
        from modules.markov_regime import format_weekly_hmm_section

        lines.extend(format_weekly_hmm_section())
    except Exception:
        lines.extend(["## Markov HMM regime", "", "- Markov HMM: unavailable", ""])

    lines.extend(["## Sector screener", "", f"- {s.sector_activity}", f"- {s.screener_meta}", ""])

    lines.extend(["## Insider & filings", ""])
    try:
        from modules.insider_monitor import format_weekly_insider_section

        lines.extend(format_weekly_insider_section())
    except Exception:
        lines.append("- Insider monitor: unavailable")
    try:
        from modules.insider_signal_handler import get_weekly_impact_summary

        lines.extend(get_weekly_impact_summary())
    except Exception:
        pass
    lines.append("")

    lines.extend(["## Stat Arb attribution", ""])
    stat_on = config.PAPER_STAT_ARB_ENABLED or config.effective_stat_arb_enabled()
    if stat_on:
        cap = config.effective_stat_arb_cap()
        cap_note = (
            f"Dedicated sleeve: {config.STAT_ARB_SLEEVE_CAP_PCT:.0%} "
            f"(effective {cap:.1%})"
            if config.effective_stat_arb_sleeve_cap_enabled()
            else "Shared NYSE sleeve cap"
        )
        lines.append(f"- {cap_note}")
        lines.append(
            f"- Vol scaling: "
            f"{'ON' if config.effective_stat_arb_vol_scaling_enabled() else 'OFF'}"
        )
    else:
        lines.append("- Stat arb sleeve disabled.")
    lines.append("")
    if data.stat_arb_open:
        lines.append("**Open pairs:**")
        lines.append(
            _md_table(
                ["Pair", "Long", "Short", "Entry Z", "Leg $"],
                [
                    [
                        r["pair"],
                        str(r.get("long", "")),
                        str(r.get("short", "")),
                        f"{float(r['entry_z']):.2f}" if r.get("entry_z") is not None else "—",
                        f"${float(r['leg_notional']):,.0f}"
                        if r.get("leg_notional") is not None
                        else "—",
                    ]
                    for r in data.stat_arb_open
                ],
            )
        )
    else:
        lines.append("_No open stat-arb pairs._")
    lines.append("")
    if data.stat_arb_trades:
        lines.append("**Key stat-arb activity:**")
        lines.append(
            _md_table(
                ["Time", "Event", "Pair", "Side", "Notional"],
                [
                    [
                        t["time"],
                        t.get("event", ""),
                        t.get("pair", ""),
                        t.get("side", ""),
                        f"${t['notional']:,.0f}" if t.get("notional") else "—",
                    ]
                    for t in data.stat_arb_trades[:10]
                ],
            )
        )
    else:
        lines.append("_No stat-arb journal entries this week._")
    lines.append("")

    lines.extend(["## Protective shorts", ""])
    lines.append(f"- {config.format_opportunistic_short_banner()}")
    lines.append(
        "- Triggers: RHYME_B + VIX≥22 rising + exhaustion + depth≥2%; "
        f"RHYME_E + VIX≥22 rising + bubble≥{config.effective_short_bubble_min_for_rhyme_e():.0%} "
        f"+ depth≥{config.SHORT_DEEP_BEAR_MIN_DEPTH:.0%}"
        + (" + exhaustion" if config.effective_short_rhyme_e_exhaustion_required() else " (exhaustion waived)")
    )
    if config.effective_sector_short_enabled():
        lines.append(
            f"- Sector shorts: weak sectors (score ≤ {config.SECTOR_SHORT_MAX_SCORE:.2f}, "
            f"RS ≤ {config.SECTOR_SHORT_MIN_RS_VS_SPY:.2f}) | max "
            f"{config.SECTOR_SHORT_MAX_PCT:.0%}/sector | "
            f"{config.SECTOR_SHORT_MAX_POSITIONS} slots"
        )
    lines.append(
        f"- Sizing: {config.effective_protective_short_min_pct():.0%}-"
        f"RHYME_E {config.SHORT_RHYME_E_MAX_PCT:.0%} / RHYME_B {config.SHORT_RHYME_B_MAX_PCT:.0%} gross | "
        f"partial 50% @ 1:1 | trail arm {config.SHORT_TRAILING_ARM_FRAC:.0%} / pull {config.SHORT_TRAILING_PULLBACK_FRAC:.0%} | "
        f"RR {config.SHORT_PROFIT_TARGET_PCT/config.SHORT_STOP_LOSS_PCT:.1f}:1 + trail | "
        f"max hold {config.SHORT_MAX_HOLD_BARS}b"
    )
    if config.SHORT_LONG_HEDGE_ENABLED:
        lines.append(
            f"- Long hedge: ON (floor {config.SHORT_LONG_HEDGE_FLOOR:.0%} sizing when shorts active)"
        )
    lines.append("")
    if data.short_trades:
        lines.append("**Short activity:**")
        lines.append(
            _md_table(
                ["Time", "Symbol", "Side", "Notional", "Reason"],
                [
                    [
                        t["time"],
                        t.get("symbol", ""),
                        t.get("side", ""),
                        f"${t['notional']:,.0f}" if t.get("notional") else "—",
                        t.get("reason", ""),
                    ]
                    for t in data.short_trades
                ],
            )
        )
    else:
        lines.append("_No opportunistic short activity this week._")
    lines.append("")

    lines.extend(["## Tail-risk activations", ""])
    tr = data.tail_risk_status
    lines.append(
        f"- Controls: **{'ON' if tr.get('tail_risk_controls') else 'OFF'}** · "
        f"Vol ceiling {tr.get('vol_ceiling_pct', 0):.0%} · "
        f"Portfolio vol cap {tr.get('portfolio_vol_ceiling_pct', 0):.0%}"
    )
    if tr.get("dynamic_vol_score") is not None:
        lines.append(f"- Dynamic vol score: {float(tr['dynamic_vol_score']):.4f}")
    if tr.get("sizing_multiplier") is not None:
        lines.append(f"- Sizing multiplier: {float(tr['sizing_multiplier']):.2f}×")
    lines.append("")
    for ev in data.tail_risk_events:
        lines.append(f"- {ev}")
    lines.append("")

    lines.extend(["## Key trades", ""])
    if s.key_trades:
        lines.append(
            _md_table(
                ["Time", "Side", "Symbol", "Notional"],
                [
                    [
                        t["time"],
                        t.get("side", ""),
                        t.get("symbol", ""),
                        f"${t['notional']:,.0f}" if t.get("notional") else "—",
                    ]
                    for t in s.key_trades[:12]
                ],
            )
        )
    else:
        lines.append("_No key trades this week._")
    lines.append("")

    lines.extend(["## Open positions", ""])
    if s.open_positions:
        lines.append(
            _md_table(
                ["Ticker", "Sleeve", "Qty", "Mkt value", "P&L %", "P&L $"],
                [
                    [
                        p.get("ticker", ""),
                        p.get("sleeve", ""),
                        f"{float(p.get('qty', 0)):.2f}",
                        f"${float(p.get('mv', 0)):,.0f}",
                        f"{float(p.get('pnl_pct', 0)):+.1f}%",
                        f"${float(p.get('pnl_usd', 0)):+,.0f}",
                    ]
                    for p in s.open_positions[:20]
                ],
            )
        )
    else:
        lines.append("_No open positions (or journal unavailable)._")
    lines.append("")

    if s.sleeve_line:
        lines.extend(["## Sleeve exposure", "", f"- {s.sleeve_line}", ""])

    # Paper research sleeves — sector rotation + ATR vol breakout
    paper_notes: list[str] = []
    try:
        from modules.sector_rotation import format_weekly_sector_rotation_note

        note = format_weekly_sector_rotation_note()
        if note:
            paper_notes.append(note)
    except Exception:
        pass
    try:
        from modules.vol_breakout_sleeve import format_weekly_vol_breakout_note

        note = format_weekly_vol_breakout_note()
        if note:
            paper_notes.append(note)
    except Exception:
        pass
    if paper_notes:
        lines.extend(["## Paper research sleeves", ""])
        for note in paper_notes:
            lines.append(f"- {note}")
        lines.append("")

    if s.heartbeat_age_min is not None:
        lines.append(f"_Heartbeat age: {s.heartbeat_age_min:.0f} min_")
    return "\n".join(lines)


def _md_to_simple_html(md: str) -> str:
    """Lightweight Markdown → HTML (headings, tables, lists, code)."""
    out: list[str] = []
    in_code = False
    table_buf: list[str] = []

    def flush_table() -> None:
        nonlocal table_buf
        if not table_buf:
            return
        rows = []
        for line in table_buf:
            if re.match(r"^\|?\s*---", line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells:
                rows.append(cells)
        if rows:
            out.append("<table>")
            out.append("<thead><tr>" + "".join(f"<th>{html.escape(c)}</th>" for c in rows[0]) + "</tr></thead>")
            out.append("<tbody>")
            for row in rows[1:]:
                out.append("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in row) + "</tr>")
            out.append("</tbody></table>")
        table_buf = []

    for line in md.splitlines():
        if line.startswith("```"):
            if in_code:
                out.append("</pre>")
                in_code = False
            else:
                flush_table()
                out.append('<pre class="sparkline">')
                in_code = True
            continue
        if in_code:
            out.append(html.escape(line))
            continue
        if line.startswith("|"):
            table_buf.append(line)
            continue
        flush_table()
        if line.startswith("# "):
            out.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.startswith("## "):
            out.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("- "):
            body = line[2:]
            body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", body)
            out.append(f"<li>{body}</li>")
        elif line.strip() == "":
            out.append("<br/>")
        elif line.startswith("_") and line.endswith("_"):
            out.append(f"<p><em>{html.escape(line.strip('_'))}</em></p>")
        else:
            body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html.escape(line))
            out.append(f"<p>{body}</p>")

    flush_table()
    body = "\n".join(out)
    body = re.sub(r"(<li>.*?</li>\n?)+", lambda m: "<ul>" + m.group(0) + "</ul>", body)
    return body


def render_html(data: WeeklyReportData, markdown: str) -> str:
    s = data.summary
    body = _md_to_simple_html(markdown)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Weekly Report {s.as_of:%Y-%m-%d} — {html.escape(s.account_label)}</title>
  <style>
    body {{ font-family: system-ui, Segoe UI, sans-serif; max-width: 920px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; line-height: 1.5; }}
    h1 {{ border-bottom: 2px solid #2563eb; padding-bottom: 0.3rem; }}
    h2 {{ color: #1e40af; margin-top: 1.5rem; }}
    table {{ border-collapse: collapse; width: 100%; margin: 0.75rem 0; font-size: 0.92rem; }}
    th, td {{ border: 1px solid #cbd5e1; padding: 0.4rem 0.6rem; text-align: left; }}
    th {{ background: #f1f5f9; }}
    pre.sparkline {{ font-size: 1.1rem; letter-spacing: 1px; background: #f8fafc; padding: 0.75rem; border-radius: 6px; }}
    ul {{ margin: 0.25rem 0 0.75rem 1.2rem; }}
    p {{ margin: 0.35rem 0; }}
  </style>
</head>
<body>
{body}
</body>
</html>
"""


def generate_weekly_report(
    *,
    test_mode: bool = False,
    dry_run: bool = False,
    equity: float | None = None,
    cash: float | None = None,
    regime: str = "",
    wisdom: dict | None = None,
    sleeves: dict | None = None,
    market_open: bool | None = None,
) -> tuple[Path, Path] | None:
    """Write reports/weekly/YYYY-MM-DD.md and .html. Returns paths or None if not due."""
    if not weekly_report_due(market_open=market_open, test_mode=test_mode):
        return None

    data = gather_weekly_report(
        equity=equity,
        cash=cash,
        regime=regime,
        wisdom=wisdom,
        sleeves=sleeves,
    )
    report_date = data.summary.as_of.date()
    out_dir = reports_dir()
    stem = report_date.isoformat()
    md_path = out_dir / f"{stem}.md"
    html_path = out_dir / f"{stem}.html"
    md_text = render_markdown(data)
    html_text = render_html(data, md_text)

    if dry_run:
        print(f"DRY RUN — would write:\n  {md_path}\n  {html_path}")
        print("\n--- preview (first 40 lines) ---")
        print("\n".join(md_text.splitlines()[:40]))
        return md_path, html_path

    md_path.write_text(md_text, encoding="utf-8")
    html_path.write_text(html_text, encoding="utf-8")
    if not test_mode:
        _mark_report_generated(report_date)
    print(f"Weekly report saved:\n  {md_path}\n  {html_path}")
    return md_path, html_path


def format_telegram_research_addon(data: WeeklyReportData) -> str:
    """High-signal research metrics for Friday Telegram (paper bot)."""
    m30 = data.metrics_30d or {}
    mall = data.metrics_alltime or {}
    health = data.bot_health or {}
    lines = [
        f"\n— Realistic Research v{data.research_version} —",
        f"Health: {health.get('score', 'n/a')}/100 ({health.get('grade', 'n/a')})",
    ]
    if m30.get("sharpe") is not None:
        lines.append(
            f"30d: {fmt_pct(m30.get('return_pct'))} | Sharpe {float(m30['sharpe']):.2f} | "
            f"DD {float(m30.get('max_drawdown_pct') or 0):.1f}%"
        )
    if mall.get("sharpe") is not None:
        lines.append(
            f"All-time: {fmt_pct(mall.get('return_pct'))} | Sharpe {float(mall['sharpe']):.2f}"
        )
    if data.bubble_score_100 is not None:
        lines.append(f"Bubble Risk: {data.bubble_score_100:.0f}/100")
        bi = data.buffett_indicator or {}
        if bi.get("ratio_pct") is not None:
            lines.append(
                f"Buffett: {bi['ratio_pct']:.1f}% GDP ({bi.get('signal', 'n/a')})"
            )
    elif data.bubble_score is not None:
        lines.append(f"Bubble Risk: {data.bubble_score:.2f}")
    short_n = len(data.short_trades or [])
    try:
        from modules.short_activity import format_weekly_shorts_telegram_block

        lines.append(
            format_weekly_shorts_telegram_block(short_trades=data.short_trades)
        )
    except Exception:
        lines.append(f"Short activity: {short_n} trade(s) this week")
        lines.append(config.format_opportunistic_short_banner())
    try:
        from modules.risk_management import format_weekly_atr_sizing_note

        atr_note = format_weekly_atr_sizing_note()
        if atr_note:
            lines.append(atr_note)
    except Exception:
        pass
    try:
        from modules.vol_breakout_sleeve import format_weekly_vol_breakout_note

        vol_bo_note = format_weekly_vol_breakout_note()
        if vol_bo_note:
            lines.append(vol_bo_note)
    except Exception:
        pass
    try:
        from modules.risk_management import format_weekly_conviction_note

        conv_note = format_weekly_conviction_note()
        if conv_note:
            lines.append(conv_note)
    except Exception:
        pass
    try:
        from modules.strategy_performance import format_weekly_strategy_contribution_note

        contrib_note = format_weekly_strategy_contribution_note()
        if contrib_note:
            lines.append(contrib_note)
    except Exception:
        pass
    try:
        from modules.multi_timeframe import format_weekly_multi_timeframe_note

        mtf_note = format_weekly_multi_timeframe_note()
        if mtf_note:
            lines.append(mtf_note)
    except Exception:
        pass
    try:
        from modules.exit_management import format_weekly_exit_note

        exit_note = format_weekly_exit_note()
        if exit_note:
            lines.append(exit_note)
    except Exception:
        pass
    try:
        from modules.risk_management import format_weekly_correlation_note

        corr_note = format_weekly_correlation_note()
        if corr_note:
            lines.append(corr_note)
    except Exception:
        pass
    if config.effective_thinking_engine_enabled():
        try:
            from modules.thinking_engine import weekly_strategy_review

            review = weekly_strategy_review(
                {
                    "regime": data.regime or "",
                    "return_30d_pct": (data.metrics_30d or {}).get("return_pct"),
                    "sharpe_30d": (data.metrics_30d or {}).get("sharpe"),
                    "health_score": (data.bot_health or {}).get("score"),
                    "bubble_score_100": data.bubble_score_100,
                },
                fast_model=True,
            )
            headline = str(review.get("headline") or "").strip()
            if headline and review.get("source") == "llm":
                lines.append(f"AI weekly: {headline[:120]}")
        except Exception:
            pass
    return "\n".join(lines)


def maybe_generate_weekly_report(
    *,
    equity: float,
    cash: float,
    regime: str = "",
    wisdom: dict | None = None,
    sleeves: dict | None = None,
    market_open: bool | None = None,
    force: bool = False,
) -> tuple[Path, Path] | None:
    """Called from run_all.py after the weekly Telegram hook."""
    try:
        from modules.sharpe_history import update_sharpe_history

        update_sharpe_history(equity, force=force, market_open=market_open)
    except Exception as exc:
        logger.debug("sharpe history update from weekly report skipped: %s", exc)
    return generate_weekly_report(
        test_mode=force,
        equity=equity,
        cash=cash,
        regime=regime,
        wisdom=wisdom,
        sleeves=sleeves,
        market_open=market_open,
    )
