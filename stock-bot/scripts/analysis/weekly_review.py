# NEVER apply parameter changes automatically
# NEVER modify .env directly
# NEVER touch live bot parameters
# Owner approval required before any change is promoted
"""Paper-book weekly research note (institutional / academic cadence).

Saturday pipeline:
  1) Collect 7d paper performance from cleaned journals (jump-filtered)
  2) Score vs explicit mandate (success / failure)
  3) Form exactly ONE single-factor hypothesis (scientific method)
  4) Run controlled 90d A/B backtest (baseline vs env override)
  5) Write IC-style markdown + notify owner (email/Telegram)
  6) Optionally open the report (--open)

Never writes .env. Never touches live. Owner must promote APPROVE'd lines manually.

Run:
  python scripts/analysis/weekly_review.py
  python scripts/analysis/weekly_review.py --open
  python scripts/analysis/weekly_review.py --skip-backtest   # smoke only
  python scripts/analysis/weekly_review.py --test            # any day; [TEST] email
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import find_dotenv, load_dotenv  # noqa: E402

LOOKBACK_DAYS = 7
BACKTEST_DAYS = 90
BACKTEST_TIMEOUT_SEC = int(os.getenv("WEEKLY_REVIEW_BACKTEST_TIMEOUT_SEC", "3600"))
TARGET_SHARPE = float(os.getenv("WEEKLY_REVIEW_TARGET_SHARPE", "1.0"))
MAX_DD_GOAL_PCT = float(os.getenv("WEEKLY_REVIEW_MAX_DD_PCT", "15"))
TARGET_7D_RETURN_PCT = float(os.getenv("WEEKLY_REVIEW_TARGET_RETURN_PCT", "0.5"))
MIN_TRADES_GOAL = int(os.getenv("WEEKLY_REVIEW_MIN_TRADES", "5"))
MIN_DAILY_OBS = int(os.getenv("WEEKLY_REVIEW_MIN_DAILY_OBS", "4"))
EQUITY_JUMP_PCT = float(os.getenv("WEEKLY_REVIEW_EQUITY_JUMP_PCT", "0.25"))
EQUITY_JUMP_RATIO = float(os.getenv("WEEKLY_REVIEW_EQUITY_JUMP_RATIO", "5.0"))
SHARPE_SCALE = math.sqrt(252)

PAPER_BOOK = ROOT / "data" / "portal" / "users" / "dawimberly" / "books" / "alpaca_paper"

# sleeve -> (env var, default, direction when weak)
SLEEVE_PARAM: dict[str, tuple[str, str, str]] = {
    "nyse": ("PAPER_NYSE_SLEEVE_CAP_PCT", "0.15", "tighten"),
    "spy": ("PAPER_SPY_MAX_EXPOSURE_PCT", "0.46", "tighten"),
    "crypto": ("PAPER_CRYPTO_MAX_EXPOSURE_PCT", "0.12", "tighten"),
    "metal": ("METAL_SLEEVE_CAP_PCT", "0.10", "tighten"),
    "vti": ("PAPER_VTI_CORE_PCT", "0.80", "raise"),
    "vti_core": ("PAPER_VTI_CORE_PCT", "0.80", "raise"),
    "overall": ("PAPER_RISK_PER_TRADE", "0.015", "tighten"),
}

TRADE_EVENTS = {"exit", "sell", "close"}
BUY_EVENTS = {"buy", "entry", "open"}
DataGrade = Literal["A", "B", "C"]


@dataclass
class SleeveStats:
    name: str
    realized_trades: int = 0
    realized_wins: int = 0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    positions: int = 0
    source: str = "none"  # realized | unrealized | mixed

    @property
    def contribution(self) -> float:
        return self.realized_pnl + self.unrealized_pnl

    @property
    def win_rate(self) -> float | None:
        if self.realized_trades <= 0:
            return None
        return 100.0 * self.realized_wins / self.realized_trades

    @property
    def avg_realized(self) -> float | None:
        if self.realized_trades <= 0:
            return None
        return self.realized_pnl / self.realized_trades


@dataclass
class RiskMetrics:
    period_return_pct: float | None = None
    ann_vol_pct: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    calmar: float | None = None
    max_dd_pct: float | None = None  # magnitude (positive)
    daily_win_rate_pct: float | None = None
    profit_factor: float | None = None
    skew: float | None = None
    kurtosis: float | None = None
    n_days: int = 0
    start_equity: float | None = None
    end_equity: float | None = None


@dataclass
class DataQuality:
    grade: DataGrade = "C"
    equity_source: str = "none"
    jumps_removed: int = 0
    rows_raw: int = 0
    rows_clean: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def usable_for_mandate(self) -> bool:
        return self.grade in ("A", "B")


@dataclass
class PerfSummary:
    closed_trades: int = 0
    closed_wins: int = 0
    closed_win_rate: float | None = None
    expectancy: float | None = None
    profit_factor_trades: float | None = None
    trades_per_day: float = 0.0
    best_sleeve: str = "n/a"
    best_sleeve_pnl: float = 0.0
    worst_sleeve: str = "n/a"
    worst_sleeve_pnl: float = 0.0
    sleeves: dict[str, SleeveStats] = field(default_factory=dict)
    risk: RiskMetrics = field(default_factory=RiskMetrics)
    quality: DataQuality = field(default_factory=DataQuality)
    regime: str = "unknown"
    notes: list[str] = field(default_factory=list)

    # Compat aliases used by score_goals / older callers
    @property
    def trades(self) -> int:
        return self.closed_trades

    @property
    def win_rate(self) -> float | None:
        return self.closed_win_rate

    @property
    def avg_pnl(self) -> float | None:
        return self.expectancy

    @property
    def sharpe_7d(self) -> float | None:
        return self.risk.sharpe

    @property
    def max_dd_pct(self) -> float | None:
        return self.risk.max_dd_pct

    @property
    def period_return_pct(self) -> float | None:
        return self.risk.period_return_pct


@dataclass
class Hypothesis:
    what: str
    why: str
    mechanism: str
    env_key: str
    current_value: str
    proposed_value: str
    expected_outcome: str
    falsification: str
    confounders: list[str] = field(default_factory=list)

    @property
    def env_line(self) -> str:
        return f"{self.env_key}={self.proposed_value}"


@dataclass
class BacktestMetrics:
    return_pct: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    calmar: float | None = None
    max_dd_pct: float | None = None
    win_rate_pct: float | None = None
    ok: bool = False
    error: str = ""
    raw_tail: str = ""

    @property
    def dd_magnitude(self) -> float | None:
        if self.max_dd_pct is None:
            return None
        return abs(float(self.max_dd_pct))


@dataclass
class GoalsStatus:
    target_sharpe: float
    max_dd_pct: float
    target_return_pct: float
    min_trades: int
    actual_sharpe: float | None
    actual_dd_pct: float | None
    actual_return_pct: float | None
    actual_trades: int
    data_grade: DataGrade = "C"
    toward_success: list[str] = field(default_factory=list)
    toward_failure: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.data_grade == "C":
            return "UNRELIABLE (data quality C)"
        if len(self.toward_failure) >= 2:
            return "FAILING"
        if self.toward_success and not self.toward_failure:
            return "ON TRACK"
        if self.toward_failure:
            return "MIXED"
        return "NEEDS MORE DATA"


def _review_saturday(today: date | None = None) -> date:
    d = today or date.today()
    return d - timedelta(days=(d.weekday() - 5) % 7)


def _parse_ts(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce", utc=True).dt.tz_localize(None)


def _load_csv(path: Path) -> pd.DataFrame:
    if not path.is_file():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
    except Exception:
        return pd.DataFrame()
    if df.empty or "timestamp" not in df.columns:
        return df
    df = df.copy()
    df["timestamp"] = _parse_ts(df["timestamp"])
    return df.dropna(subset=["timestamp"]).sort_values("timestamp")


def _load_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _paper_journal_candidates() -> list[Path]:
    return [
        PAPER_BOOK / "paper_journal.csv",
        ROOT / "paper_chase_journal.csv",
        Path(os.getenv("PAPER_JOURNAL_CSV", "")),
        ROOT / "paper_journal.csv",
    ]


def _load_best_paper_journal() -> tuple[pd.DataFrame, str]:
    """Prefer portal paper journal; segment via wisdom jump filter when possible."""
    for path in _paper_journal_candidates():
        if not path or not str(path):
            continue
        p = path if path.is_absolute() else ROOT / path
        df = _load_csv(p)
        if df.empty or "equity" not in df.columns:
            continue
        df = df.copy()
        df["equity"] = pd.to_numeric(df["equity"], errors="coerce")
        df = df.dropna(subset=["equity"])
        meta: dict[str, Any] = {}
        try:
            from modules.wisdom_evaluator import filter_journal

            seg, meta = filter_journal(df, book_type="paper")
            if not seg.empty:
                return seg, f"{p.name}+wisdom_filter({meta.get('split_reason') or meta.get('fallback') or 'ok'})"
        except Exception:
            pass
        return df, p.name
    return pd.DataFrame(), "none"


def _clean_daily_equity(daily: pd.Series) -> tuple[pd.Series, int, list[str]]:
    """Drop account-reset discontinuities; keep latest contiguous clean segment."""
    notes: list[str] = []
    if daily is None or len(daily) < 2:
        return daily.astype(float) if daily is not None else pd.Series(dtype=float), 0, notes

    daily = daily.astype(float).sort_index()
    segments: list[list[tuple[Any, float]]] = []
    cur: list[tuple[Any, float]] = []
    prev: float | None = None
    jumps = 0
    for idx, val in daily.items():
        v = float(val)
        if prev is not None and prev > 0 and v > 0:
            pct = abs(v - prev) / prev
            ratio = max(prev / v, v / prev)
            if pct >= EQUITY_JUMP_PCT or ratio >= EQUITY_JUMP_RATIO:
                jumps += 1
                if cur:
                    segments.append(cur)
                cur = [(idx, v)]
                prev = v
                continue
        cur.append((idx, v))
        prev = v
    if cur:
        segments.append(cur)

    if not segments:
        return pd.Series(dtype=float), jumps, notes

    # Prefer segment containing the most recent date (current book state).
    best = segments[-1]
    if jumps:
        notes.append(
            f"Removed {jumps} equity discontinuity(ies) "
            f"(jump≥{EQUITY_JUMP_PCT:.0%} or ratio≥{EQUITY_JUMP_RATIO:g}); "
            f"using latest contiguous segment ({len(best)} day(s))."
        )
    out = pd.Series({d: v for d, v in best}).astype(float)
    return out, jumps, notes


def _risk_from_curve(curve: pd.Series) -> RiskMetrics:
    rm = RiskMetrics(n_days=int(len(curve)))
    if curve is None or len(curve) < 2:
        return rm
    curve = curve.astype(float)
    rm.start_equity = float(curve.iloc[0])
    rm.end_equity = float(curve.iloc[-1])
    if rm.start_equity > 0:
        rm.period_return_pct = (rm.end_equity / rm.start_equity - 1.0) * 100.0

    rets = curve.pct_change().dropna()
    if rets.empty:
        return rm
    std = float(rets.std())
    mean = float(rets.mean())
    rm.ann_vol_pct = std * SHARPE_SCALE * 100.0 if std > 0 else 0.0
    rm.sharpe = (mean / std) * SHARPE_SCALE if std > 1e-12 else 0.0
    downside = rets[rets < 0]
    dstd = float(downside.std()) if len(downside) else 0.0
    rm.sortino = (mean / dstd) * SHARPE_SCALE if dstd > 1e-12 else None
    dd = curve / curve.cummax() - 1.0
    rm.max_dd_pct = abs(float(dd.min()) * 100.0)
    if rm.max_dd_pct > 1e-9 and rm.period_return_pct is not None:
        # Period Calmar (not annualized) — disclosed in report
        rm.calmar = rm.period_return_pct / rm.max_dd_pct
    rm.daily_win_rate_pct = float((rets > 0).mean() * 100.0)
    gains = float(rets[rets > 0].sum())
    losses = float(abs(rets[rets < 0].sum()))
    if losses > 1e-12:
        rm.profit_factor = gains / losses
    elif gains > 0:
        rm.profit_factor = None  # no losing days — PF undefined / infinite
    else:
        rm.profit_factor = 0.0
    if len(rets) >= 3:
        rm.skew = float(rets.skew())
        rm.kurtosis = float(rets.kurtosis())  # excess kurtosis
    # Annualized ratios need a minimum sample or they mislead the IC.
    if len(rets) < max(2, MIN_DAILY_OBS - 1):
        rm.sharpe = None
        rm.sortino = None
        rm.ann_vol_pct = None
        rm.calmar = None
    return rm


def _extract_pnl(note: str) -> float | None:
    if not note:
        return None
    m = re.search(r"pnl[=:]?\s*([+-]?\d+\.?\d*)", note, re.I)
    if m:
        return float(m.group(1))
    # stop_loss -6.89% style: treat as return mark, not USD — still directional
    m = re.search(r"([+-]?\d+\.?\d+)\s*%", note)
    if m:
        return float(m.group(1))
    return None


def _closed_trade_stats(journal: pd.DataFrame, cutoff: datetime) -> tuple[list[float], dict[str, SleeveStats]]:
    sleeves: dict[str, SleeveStats] = {}
    pnls: list[float] = []
    if journal.empty or "event" not in journal.columns:
        return pnls, sleeves
    df = journal[journal["timestamp"] >= cutoff].copy()
    if df.empty:
        return pnls, sleeves
    ev = df["event"].astype(str).str.lower()
    exits = df[ev.isin(TRADE_EVENTS)]
    for _, row in exits.iterrows():
        sleeve = str(row.get("sleeve") or "").strip().lower() or "unknown"
        if sleeve in ("", "nan", "none"):
            sleeve = "unknown"
        st = sleeves.setdefault(sleeve, SleeveStats(name=sleeve, source="realized"))
        st.source = "realized" if st.source == "none" else ("mixed" if st.source != "realized" else "realized")
        st.realized_trades += 1
        note = str(row.get("notes") or "")
        pnl = _extract_pnl(note)
        if pnl is not None:
            st.realized_pnl += pnl
            pnls.append(pnl)
            if pnl > 0:
                st.realized_wins += 1
    return pnls, sleeves


def _merge_crypto_realized(sleeves: dict[str, SleeveStats], cutoff: datetime) -> None:
    df = _load_csv(ROOT / "crypto_vol_journal.csv")
    if df.empty:
        return
    recent = df[df["timestamp"] >= cutoff]
    if recent.empty or "action" not in recent.columns:
        return
    actions = recent["action"].astype(str).str.lower()
    sells = recent[actions.isin(["sell", "exit", "close"])]
    if sells.empty:
        return
    st = sleeves.setdefault("crypto", SleeveStats(name="crypto", source="realized"))
    st.source = "mixed" if st.source == "unrealized" else "realized"
    for _, row in sells.iterrows():
        st.realized_trades += 1
        pnl = _extract_pnl(str(row.get("notes") or row.get("exit_reason") or ""))
        if pnl is not None:
            st.realized_pnl += pnl
            if pnl > 0:
                st.realized_wins += 1


def _attach_unrealized(sleeves: dict[str, SleeveStats], hb: dict | None) -> None:
    if not hb:
        return
    for name, block in (hb.get("sleeve_pnl") or {}).items():
        if not isinstance(block, dict):
            continue
        key = str(name).lower()
        st = sleeves.setdefault(key, SleeveStats(name=key, source="unrealized"))
        if st.source == "none":
            st.source = "unrealized"
        elif st.source == "realized":
            st.source = "mixed"
        st.unrealized_pnl = float(block.get("unrealized_pnl") or 0.0)
        st.positions = int(block.get("positions") or 0)


def _wisdom_daily_scores(cutoff: datetime) -> list[dict[str, Any]]:
    scores: list[dict[str, Any]] = []
    scorecard = _load_json(ROOT / "wisdom_scorecard.json") or _load_json(PAPER_BOOK / "wisdom_scorecard.json")
    if isinstance(scorecard, dict):
        live = scorecard.get("live") or {}
        ret = live.get("return_pct")
        # Contaminated split windows produce absurd returns — exclude from corroboration.
        if live and ret is not None and abs(float(ret)) < 100.0:
            scores.append(
                {
                    "date": live.get("to_date") or scorecard.get("evaluated_at"),
                    "return_pct": live.get("return_pct"),
                    "sharpe": live.get("sharpe"),
                    "max_drawdown_pct": live.get("max_drawdown_pct"),
                    "mode": live.get("mode"),
                    "source": "wisdom_scorecard.live",
                }
            )
    for path in (PAPER_BOOK / "wisdom_journal.csv", ROOT / "wisdom_journal.csv"):
        df = _load_csv(path)
        if df.empty or "equity" not in df.columns:
            continue
        recent = df[df["timestamp"] >= cutoff].copy()
        if recent.empty:
            continue
        recent["equity"] = pd.to_numeric(recent["equity"], errors="coerce")
        daily = recent.groupby(recent["timestamp"].dt.date)["equity"].last().dropna()
        daily, jumps, _ = _clean_daily_equity(daily)
        rets = daily.pct_change() * 100.0
        for d in daily.tail(LOOKBACK_DAYS).index:
            ret = rets.get(d)
            scores.append(
                {
                    "date": str(d),
                    "return_pct": None
                    if ret is None or (isinstance(ret, float) and math.isnan(ret))
                    else float(ret),
                    "equity": float(daily.loc[d]),
                    "source": path.name,
                    "jumps_filtered": jumps,
                }
            )
        break
    return scores[-LOOKBACK_DAYS:]


def _grade_quality(
    *,
    n_days: int,
    jumps: int,
    closed_trades: int,
    equity_source: str,
) -> DataQuality:
    q = DataQuality(equity_source=equity_source, jumps_removed=jumps)
    if equity_source == "none" or n_days < 2:
        q.grade = "C"
        q.notes.append("Insufficient clean equity observations.")
        return q
    if n_days >= MIN_DAILY_OBS and jumps == 0 and closed_trades >= MIN_TRADES_GOAL:
        q.grade = "A"
        q.notes.append("Clean curve, no discontinuities, adequate closed-trade sample.")
    elif n_days >= MIN_DAILY_OBS and jumps <= 2:
        q.grade = "B"
        q.notes.append("Usable after jump filter; treat trade counts cautiously if sparse.")
    else:
        q.grade = "C"
        q.notes.append("Sparse/noisy sample — mandate scoring and sleeve trade stats unreliable.")
    return q


def collect_performance() -> tuple[PerfSummary, list[dict], dict | None]:
    now = datetime.now()
    cutoff = now - timedelta(days=LOOKBACK_DAYS)
    journal, equity_source = _load_best_paper_journal()

    hb = _load_json(PAPER_BOOK / "bot_heartbeat.json") or _load_json(ROOT / "bot_heartbeat.json")
    wisdom_scores = _wisdom_daily_scores(cutoff)

    summary = PerfSummary()
    summary.regime = str((hb or {}).get("regime") or "unknown")

    # Equity curve: cycle rows preferred, then any row with equity
    if not journal.empty and "equity" in journal.columns:
        cycles = journal
        if "event" in journal.columns:
            cyc = journal[journal["event"].astype(str).str.lower() == "cycle"]
            if not cyc.empty:
                cycles = cyc
        recent = cycles[cycles["timestamp"] >= cutoff]
        if recent.empty:
            recent = cycles.tail(500)
            summary.notes.append("Lookback had few rows; extended history used then jump-filtered.")
        daily_raw = recent.groupby(recent["timestamp"].dt.date)["equity"].last().dropna()
        daily, jumps, jump_notes = _clean_daily_equity(daily_raw)
        summary.notes.extend(jump_notes)
        # Keep only lookback on cleaned segment
        if not daily.empty:
            keep_from = (daily.index[-1] - timedelta(days=LOOKBACK_DAYS)) if hasattr(daily.index[-1], "year") else daily.index[0]
            try:
                daily = daily[daily.index >= keep_from]
            except Exception:
                daily = daily.tail(LOOKBACK_DAYS)
        summary.risk = _risk_from_curve(daily)
        summary.quality = _grade_quality(
            n_days=summary.risk.n_days,
            jumps=jumps,
            closed_trades=0,  # filled after trade parse
            equity_source=equity_source,
        )
        summary.quality.rows_raw = int(len(daily_raw))
        summary.quality.rows_clean = int(len(daily))
    else:
        summary.quality = DataQuality(grade="C", equity_source="none", notes=["No paper journal equity."])

    pnls, sleeves = _closed_trade_stats(journal, cutoff)
    _merge_crypto_realized(sleeves, cutoff)
    _attach_unrealized(sleeves, hb)

    summary.sleeves = sleeves
    summary.closed_trades = sum(s.realized_trades for s in sleeves.values())
    summary.closed_wins = sum(s.realized_wins for s in sleeves.values())
    if summary.closed_trades:
        summary.closed_win_rate = 100.0 * summary.closed_wins / summary.closed_trades
    if pnls:
        summary.expectancy = float(np.mean(pnls))
        gains = sum(p for p in pnls if p > 0)
        losses = abs(sum(p for p in pnls if p < 0))
        summary.profit_factor_trades = (gains / losses) if losses > 1e-12 else (gains if gains > 0 else 0.0)
    summary.trades_per_day = summary.closed_trades / float(LOOKBACK_DAYS)

    # Re-grade with trade sample
    summary.quality = _grade_quality(
        n_days=summary.risk.n_days,
        jumps=summary.quality.jumps_removed,
        closed_trades=summary.closed_trades,
        equity_source=summary.quality.equity_source or equity_source,
    )
    summary.quality.rows_raw = summary.quality.rows_raw
    summary.quality.rows_clean = summary.risk.n_days
    if summary.closed_trades == 0 and any(s.unrealized_pnl != 0 for s in sleeves.values()):
        summary.notes.append(
            "No closed trades in window — sleeve ranking uses mark-to-market (unrealized) only."
        )

    ranked = [
        (name, st.contribution)
        for name, st in sleeves.items()
        if name != "unknown" and (st.realized_trades > 0 or abs(st.contribution) > 1e-9)
    ]
    if ranked:
        ranked.sort(key=lambda x: x[1], reverse=True)
        summary.best_sleeve, summary.best_sleeve_pnl = ranked[0]
        summary.worst_sleeve, summary.worst_sleeve_pnl = ranked[-1]
    else:
        summary.notes.append("Insufficient sleeve contribution for ranking.")

    if wisdom_scores:
        summary.notes.append(f"Wisdom corroboration series: {len(wisdom_scores)} point(s).")

    return summary, wisdom_scores, hb


def score_goals(summary: PerfSummary) -> GoalsStatus:
    gs = GoalsStatus(
        target_sharpe=TARGET_SHARPE,
        max_dd_pct=MAX_DD_GOAL_PCT,
        target_return_pct=TARGET_7D_RETURN_PCT,
        min_trades=MIN_TRADES_GOAL,
        actual_sharpe=summary.risk.sharpe,
        actual_dd_pct=summary.risk.max_dd_pct,
        actual_return_pct=summary.risk.period_return_pct,
        actual_trades=summary.closed_trades,
        data_grade=summary.quality.grade,
    )
    if summary.quality.grade == "C":
        gs.toward_failure.append("Data grade C — do not trust mandate score this week")
        return gs

    if summary.risk.sharpe is not None:
        if summary.risk.sharpe >= TARGET_SHARPE:
            gs.toward_success.append(f"ann. Sharpe {summary.risk.sharpe:.2f} ≥ {TARGET_SHARPE}")
        else:
            gs.toward_failure.append(f"ann. Sharpe {summary.risk.sharpe:.2f} < {TARGET_SHARPE}")
    if summary.risk.max_dd_pct is not None:
        if summary.risk.max_dd_pct <= MAX_DD_GOAL_PCT:
            gs.toward_success.append(f"max DD {summary.risk.max_dd_pct:.2f}% ≤ {MAX_DD_GOAL_PCT}%")
        else:
            gs.toward_failure.append(f"max DD {summary.risk.max_dd_pct:.2f}% > {MAX_DD_GOAL_PCT}%")
    if summary.risk.period_return_pct is not None:
        if summary.risk.period_return_pct >= TARGET_7D_RETURN_PCT:
            gs.toward_success.append(
                f"7d return {summary.risk.period_return_pct:+.2f}% ≥ {TARGET_7D_RETURN_PCT}%"
            )
        else:
            gs.toward_failure.append(
                f"7d return {summary.risk.period_return_pct:+.2f}% < {TARGET_7D_RETURN_PCT}%"
            )
    if summary.closed_trades >= MIN_TRADES_GOAL:
        gs.toward_success.append(f"closed trades {summary.closed_trades} ≥ {MIN_TRADES_GOAL}")
    else:
        gs.toward_failure.append(
            f"closed trades {summary.closed_trades} < {MIN_TRADES_GOAL} (underpowered sample)"
        )
    return gs


def _current_env_value(key: str, default: str) -> str:
    return str(os.getenv(key, default))


def _nudge_value(raw: str, direction: str) -> str:
    try:
        val = float(raw)
    except ValueError:
        return raw
    if direction == "raise":
        new = min(0.95, round(val + 0.05, 4))
    else:
        new = max(1.0, round(val * 0.75, 4)) if val >= 1.0 else max(0.02, round(val * 0.75, 4))
    if "." not in raw and abs(new - round(new)) < 1e-9:
        return str(int(round(new)))
    return f"{new:.4f}".rstrip("0").rstrip(".")


def form_hypothesis(summary: PerfSummary, hb: dict | None) -> Hypothesis:
    worst = (summary.worst_sleeve or "overall").lower()
    if worst in ("n/a", "", "unknown"):
        worst = "overall"

    # If realized edge is empty, use most negative unrealized sleeve by $ drag / equity
    equity = float((hb or {}).get("equity") or summary.risk.end_equity or 0.0)
    if summary.closed_trades < 3 and hb:
        underwater = []
        for name, block in (hb.get("sleeve_pnl") or {}).items():
            if not isinstance(block, dict):
                continue
            upnl = float(block.get("unrealized_pnl") or 0.0)
            if upnl < 0 or block.get("underwater"):
                underwater.append((str(name).lower(), upnl))
        if underwater:
            underwater.sort(key=lambda x: x[1])
            worst = underwater[0][0]

    key, default, direction = SLEEVE_PARAM.get(worst, SLEEVE_PARAM["overall"])
    current = _current_env_value(key, default)
    proposed = _nudge_value(current, direction)
    st = summary.sleeves.get(worst)
    contrib = summary.worst_sleeve_pnl
    contrib_bps = (contrib / equity * 1e4) if equity > 0 else None

    what = (
        f"Single factor under investigation: {worst} sleeve is the weakest "
        f"{LOOKBACK_DAYS}d contributor (contrib={contrib:+.2f}"
        + (f", {contrib_bps:.1f} bps of book" if contrib_bps is not None else "")
        + ")."
    )
    why = (
        f"Regime={summary.regime}. Data grade={summary.quality.grade}. "
        f"Closed trades={summary.closed_trades}. "
        f"Sleeve source={st.source if st else 'n/a'}. "
        "Under a single-factor discipline we adjust only the exposure/risk lever "
        "mapped to this sleeve — not an omnibus retune."
    )
    mechanism = (
        f"{'Raise' if direction == 'raise' else 'Tighten'} `{key}` "
        f"from {current} → {proposed} to change that sleeve's capital allocation "
        "while holding all other policy constants fixed."
    )
    expected = (
        f"On a {BACKTEST_DAYS}d paper-aggressive backtest, proposed config should improve "
        "Sharpe and not worsen max-DD magnitude materially (ΔSharpe>+0.05, ΔReturn≥−0.5pp, "
        "Δ|DD|≤+0.5pp)."
    )
    falsification = (
        "Reject if proposed Sharpe worsens by >0.05, or return drops >1pp with worse |DD|, "
        "or data grade remains C with no closed-trade corroboration."
    )
    confounders = [
        "7d sample is short vs parameter half-life — A/B is 90d to compensate",
        "Mark-to-market sleeve PnL ≠ closed PnL when exits are sparse",
        "Regime conditioning (RHYME) may dominate sleeve caps this week",
        "Backtest path risk: one seed/path; not a monte-carlo IC memo",
    ]
    return Hypothesis(
        what=what,
        why=why,
        mechanism=mechanism,
        env_key=key,
        current_value=current,
        proposed_value=proposed,
        expected_outcome=expected,
        falsification=falsification,
        confounders=confounders,
    )


def _parse_backtest_output(text: str) -> BacktestMetrics:
    metrics = BacktestMetrics(raw_tail=text[-4000:] if text else "")
    pats = {
        "return_pct": r"Total Return:\s*([+-]?\d+\.?\d*)%",
        "sharpe": r"Sharpe Ratio:\s*([+-]?\d+\.?\d*)",
        "sortino": r"Sortino Ratio:\s*([+-]?\d+\.?\d*)",
        "calmar": r"Calmar Ratio:\s*([+-]?\d+\.?\d*)",
        "max_dd_pct": r"Max Drawdown:\s*([+-]?\d+\.?\d*)%",
        "win_rate_pct": r"Win rate \(daily\):\s*([+-]?\d+\.?\d*)%",
    }
    for attr, pat in pats.items():
        m = re.search(pat, text)
        if m:
            setattr(metrics, attr, float(m.group(1)))
    metrics.ok = metrics.return_pct is not None and metrics.sharpe is not None
    if not metrics.ok:
        metrics.error = "Could not parse Total Return / Sharpe from backtester output"
    return metrics


def run_backtest(label: str, env_overrides: dict[str, str] | None = None) -> BacktestMetrics:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    cmd = [
        sys.executable,
        str(ROOT / "backtester.py"),
        "--days",
        str(BACKTEST_DAYS),
        "--paper-aggressive",
    ]
    print(f"[weekly_review] Starting backtest ({label}): {' '.join(cmd)}", flush=True)
    if env_overrides:
        print(f"[weekly_review] Env overrides: {env_overrides}", flush=True)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=BACKTEST_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return BacktestMetrics(ok=False, error=f"timeout after {BACKTEST_TIMEOUT_SEC}s: {exc}")
    except Exception as exc:
        return BacktestMetrics(ok=False, error=str(exc))

    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    metrics = _parse_backtest_output(text)
    if proc.returncode != 0 and not metrics.ok:
        metrics.error = f"exit {proc.returncode}: {metrics.error or (proc.stderr or '')[:500]}"
        metrics.ok = False
    return metrics


def decide_recommendation(
    summary: PerfSummary,
    baseline: BacktestMetrics,
    proposed: BacktestMetrics,
    goals: GoalsStatus,
) -> tuple[str, str]:
    if not baseline.ok or not proposed.ok:
        return (
            "NEEDS MORE DATA",
            "Controlled experiment incomplete — one or both 90d backtests failed to parse "
            f"(baseline_ok={baseline.ok}, proposed_ok={proposed.ok}; "
            f"{baseline.error or '—'} | {proposed.error or '—'}). "
            "No parameter change without a valid A/B table.",
        )

    ret_delta = (proposed.return_pct or 0) - (baseline.return_pct or 0)
    sharpe_delta = (proposed.sharpe or 0) - (baseline.sharpe or 0)
    base_dd = baseline.dd_magnitude or 0.0
    prop_dd = proposed.dd_magnitude or 0.0
    dd_mag_delta = prop_dd - base_dd  # positive = worse drawdown

    improved = sharpe_delta > 0.05 and ret_delta >= -0.5 and dd_mag_delta <= 0.5
    worsened = sharpe_delta < -0.05 or (ret_delta < -1.0 and dd_mag_delta > 0.25)

    caveats = []
    if summary.quality.grade == "C":
        caveats.append("live 7d data grade C — lean on A/B, not mandate score")
    if summary.closed_trades < MIN_TRADES_GOAL:
        caveats.append("thin closed-trade sample in live window")
    if goals.label.startswith("UNRELIABLE"):
        caveats.append("mandate score unreliable this week")
    caveat_txt = (" Caveats: " + "; ".join(caveats) + ".") if caveats else ""

    if improved:
        return (
            "APPROVE",
            "Investment Committee decision support: proposed single-factor change improves "
            f"{BACKTEST_DAYS}d Sharpe with non-worse return/|DD| "
            f"(ΔReturn={ret_delta:+.2f}pp, ΔSharpe={sharpe_delta:+.2f}, Δ|DD|={dd_mag_delta:+.2f}pp). "
            "Paper-only promotion; owner must edit .env manually. Never auto-apply."
            + caveat_txt,
        )
    if worsened:
        return (
            "REJECT",
            "Falsified vs success criteria on controlled backtest "
            f"(ΔReturn={ret_delta:+.2f}pp, ΔSharpe={sharpe_delta:+.2f}, Δ|DD|={dd_mag_delta:+.2f}pp). "
            "Retain current baseline."
            + caveat_txt,
        )
    return (
        "NEEDS MORE DATA",
        "Effect size within noise / mixed "
        f"(ΔReturn={ret_delta:+.2f}pp, ΔSharpe={sharpe_delta:+.2f}, Δ|DD|={dd_mag_delta:+.2f}pp). "
        "Hold parameters; gather another week of clean closed trades."
        + caveat_txt,
    )


def _fmt_pct(val: float | None, digits: int = 2) -> str:
    if val is None:
        return "n/a"
    return f"{val:.{digits}f}%"


def _fmt_num(val: float | None, digits: int = 2) -> str:
    if val is None:
        return "n/a"
    return f"{val:.{digits}f}"


def build_markdown(
    review_date: date,
    summary: PerfSummary,
    hypothesis: Hypothesis,
    baseline: BacktestMetrics,
    proposed: BacktestMetrics,
    recommendation: str,
    reasoning: str,
    wisdom_scores: list[dict],
    hb: dict | None,
    goals: GoalsStatus,
) -> str:
    r = summary.risk
    q = summary.quality
    lines: list[str] = [
        f"# Paper Book Weekly Research Note — {review_date.isoformat()}",
        "",
        "> Classification: Internal research / decision support. **Not** an order. "
        "Parameter changes require owner approval. This artifact never mutates `.env` or live books.",
        "",
        "## 0. Executive Decision",
        f"**Recommendation: {recommendation}**",
        f"{reasoning}",
        "",
        f"Proposed line (manual only): `{hypothesis.env_line}`",
        "",
        "## 1. Mandate & Success Criteria",
        (
            f"| Metric | Success | Failure / risk bound |\n"
            f"|--------|---------|----------------------|\n"
            f"| Ann. Sharpe (7d daily) | ≥ {goals.target_sharpe} | < {goals.target_sharpe} |\n"
            f"| Max DD magnitude (7d) | ≤ {goals.max_dd_pct}% | > {goals.max_dd_pct}% |\n"
            f"| 7d total return | ≥ {goals.target_return_pct}% | < {goals.target_return_pct}% |\n"
            f"| Closed trades (7d) | ≥ {goals.min_trades} | < {goals.min_trades} (underpowered) |"
        ),
        "",
        f"**Mandate score: {goals.label}**",
    ]
    for item in goals.toward_success:
        lines.append(f"- Toward success: {item}")
    for item in goals.toward_failure:
        lines.append(f"- Toward failure: {item}")

    lines.extend(
        [
            "",
            "## 2. Data Quality & Methodology",
            f"- Grade: **{q.grade}** | Equity source: `{q.equity_source}`",
            f"- Clean daily obs: {q.rows_clean} (raw daily points considered: {q.rows_raw})",
            f"- Discontinuities removed: {q.jumps_removed} "
            f"(threshold jump≥{EQUITY_JUMP_PCT:.0%} or ratio≥{EQUITY_JUMP_RATIO:g})",
            "- Returns: end-of-day equity from cycle marks; rf≈0 for short-horizon Sharpe",
            f"- Sharpe scale: √252; lookback: {LOOKBACK_DAYS}d live / {BACKTEST_DAYS}d experiment",
            "- Scientific rule: **one** exogenous parameter change per review",
        ]
    )
    for n in q.notes:
        lines.append(f"- QC: {n}")

    lines.extend(
        [
            "",
            "## 3. Performance (cleaned 7d)",
            (
                f"| Metric | Value |\n|--------|-------|\n"
                f"| Start equity | {_fmt_num(r.start_equity, 2)} |\n"
                f"| End equity | {_fmt_num(r.end_equity, 2)} |\n"
                f"| Period return | {_fmt_pct(r.period_return_pct)} |\n"
                f"| Ann. volatility | {_fmt_pct(r.ann_vol_pct)} |\n"
                f"| Sharpe (ann.) | {_fmt_num(r.sharpe)} |\n"
                f"| Sortino (ann.) | {_fmt_num(r.sortino)} |\n"
                f"| Calmar (period ret / |DD|) | {_fmt_num(r.calmar)} |\n"
                f"| Max DD (magnitude) | {_fmt_pct(r.max_dd_pct)} |\n"
                f"| Daily hit rate | {_fmt_pct(r.daily_win_rate_pct, 1)} |\n"
                f"| Profit factor (daily) | {_fmt_num(r.profit_factor)} |\n"
                f"| Skew / excess kurtosis | {_fmt_num(r.skew)} / {_fmt_num(r.kurtosis)} |\n"
                f"| Closed trades | {summary.closed_trades} "
                f"(WR {_fmt_pct(summary.closed_win_rate, 1)}, "
                f"expectancy {_fmt_num(summary.expectancy)}, "
                f"PF {_fmt_num(summary.profit_factor_trades)}) |\n"
                f"| Trades / day | {summary.trades_per_day:.2f} |\n"
                f"| Regime | {summary.regime} |"
            ),
        ]
    )
    if hb:
        lines.append(
            f"- Heartbeat: equity={hb.get('equity')} halted={hb.get('halted')} "
            f"crypto_vol_only={hb.get('crypto_vol_only')}"
        )

    lines.extend(
        [
            "",
            "## 4. Sleeve Attribution",
            (
                f"Best: **{summary.best_sleeve}** ({summary.best_sleeve_pnl:+.2f}) | "
                f"Worst: **{summary.worst_sleeve}** ({summary.worst_sleeve_pnl:+.2f})"
            ),
            "",
            "| Sleeve | Source | Closed | Win% | Realized | Unrealized | Positions | Contrib |",
            "|--------|--------|--------|------|----------|------------|-----------|---------|",
        ]
    )
    for name, st in sorted(summary.sleeves.items(), key=lambda kv: kv[1].contribution):
        lines.append(
            f"| {name} | {st.source} | {st.realized_trades} | "
            f"{_fmt_pct(st.win_rate, 1)} | {st.realized_pnl:+.2f} | "
            f"{st.unrealized_pnl:+.2f} | {st.positions} | {st.contribution:+.2f} |"
        )

    lines.extend(
        [
            "",
            "## 5. Single-Factor Hypothesis (Scientific Method)",
            f"**H1 (what):** {hypothesis.what}",
            f"**Rationale (why):** {hypothesis.why}",
            f"**Mechanism (treatment):** {hypothesis.mechanism}",
            f"**Success definition:** {hypothesis.expected_outcome}",
            f"**Falsification:** {hypothesis.falsification}",
            "**Confounders / threats to validity:**",
        ]
    )
    for c in hypothesis.confounders:
        lines.append(f"- {c}")

    lines.extend(
        [
            "",
            "## 6. Controlled Experiment (90d paper-aggressive A/B)",
            "| Metric | Baseline (current) | Treatment (proposed) | Δ |",
            "|--------|--------------------|----------------------|---|",
            (
                f"| Return | {_fmt_pct(baseline.return_pct)} | {_fmt_pct(proposed.return_pct)} | "
                f"{_fmt_num((proposed.return_pct or 0) - (baseline.return_pct or 0) if baseline.ok and proposed.ok else None)} pp |"
            ),
            (
                f"| Sharpe | {_fmt_num(baseline.sharpe)} | {_fmt_num(proposed.sharpe)} | "
                f"{_fmt_num((proposed.sharpe or 0) - (baseline.sharpe or 0) if baseline.ok and proposed.ok else None)} |"
            ),
            (
                f"| Sortino | {_fmt_num(baseline.sortino)} | {_fmt_num(proposed.sortino)} | "
                f"{_fmt_num((proposed.sortino or 0) - (baseline.sortino or 0) if baseline.sortino is not None and proposed.sortino is not None else None)} |"
            ),
            (
                f"| Calmar | {_fmt_num(baseline.calmar)} | {_fmt_num(proposed.calmar)} | "
                f"{_fmt_num((proposed.calmar or 0) - (baseline.calmar or 0) if baseline.calmar is not None and proposed.calmar is not None else None)} |"
            ),
            (
                f"| Max DD | {_fmt_pct(baseline.max_dd_pct)} | {_fmt_pct(proposed.max_dd_pct)} | "
                f"|DD| Δ {_fmt_num((proposed.dd_magnitude or 0) - (baseline.dd_magnitude or 0) if baseline.ok and proposed.ok else None)} pp |"
            ),
            (
                f"| Daily win rate | {_fmt_pct(baseline.win_rate_pct, 1)} | "
                f"{_fmt_pct(proposed.win_rate_pct, 1)} | — |"
            ),
        ]
    )
    if not baseline.ok:
        lines.append(f"- Baseline error: {baseline.error}")
    if not proposed.ok:
        lines.append(f"- Treatment error: {proposed.error}")

    lines.extend(
        [
            "",
            "## 7. Investment Committee Recommendation",
            f"**{recommendation}**",
            reasoning,
            "",
            "## 8. Implementation (owner-only)",
            "If APPROVE, add/change **paper** `.env` only:",
            f"```",
            f"{hypothesis.env_line}",
            f"```",
            "Do **not** apply to live. Restart paper bot after edit. Re-evaluate next Saturday.",
            "",
            "## Appendix",
        ]
    )
    if wisdom_scores:
        lines.append("### Wisdom corroboration")
        for row in wisdom_scores[-5:]:
            lines.append(
                f"- {row.get('date')}: ret={row.get('return_pct')} "
                f"sharpe={row.get('sharpe')} src={row.get('source')}"
            )
    if summary.notes:
        lines.append("### Notes")
        for n in summary.notes:
            lines.append(f"- {n}")
    lines.append("")
    lines.append("<!-- advisory only; never auto-apply -->")
    lines.append("")
    return "\n".join(lines)


def _open_report(path: Path) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
        print(f"[weekly_review] Opened {path}", flush=True)
    except Exception as exc:
        print(f"[weekly_review] Could not open report: {exc}", flush=True)


def notify_owner(subject: str, body: str, out_path: Path | None = None) -> bool:
    emailed = False
    telegrammed = False
    try:
        from modules.alerts import send_email

        emailed = bool(send_email(subject, body))
        print(f"[weekly_review] Email {'sent' if emailed else 'failed/not configured'}.", flush=True)
    except Exception as exc:
        print(f"[weekly_review] Email failed: {exc}", flush=True)

    try:
        from modules.alerts import send_telegram

        path_line = f"\nFile: {out_path}" if out_path else ""
        # Telegram length cap — executive section only.
        exec_end = body.find("## 1. Mandate")
        snippet = body[: exec_end if exec_end > 0 else 2800]
        if len(snippet) > 3200:
            snippet = snippet[:3000] + "\n…(truncated)"
        telegrammed = bool(send_telegram(f"{subject}{path_line}\n\n{snippet}"))
        print(
            f"[weekly_review] Telegram {'sent' if telegrammed else 'failed/not configured'}.",
            flush=True,
        )
    except Exception as exc:
        print(f"[weekly_review] Telegram failed: {exc}", flush=True)

    if emailed or telegrammed:
        return True
    pending = ROOT / "data" / "weekly_review_pending.txt"
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text(f"{subject}\n\n{body}", encoding="utf-8")
    print(f"[weekly_review] Wrote pending notice to {pending}", flush=True)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Institutional paper weekly research note")
    parser.add_argument("--open", action="store_true", help="Open markdown when finished")
    parser.add_argument(
        "--skip-backtest",
        action="store_true",
        help="Smoke mode: skip 90d A/B (forces NEEDS MORE DATA)",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run immediately any day; last 7d data; email subject [TEST] Weekly Bot Review",
    )
    args = parser.parse_args(argv)

    load_dotenv(find_dotenv())
    want_open = args.open or (
        os.getenv("WEEKLY_REVIEW_OPEN", "").strip().lower() in ("1", "true", "yes", "on")
    )

    # Normal path keeps Saturday-labeled artifact; --test runs any weekday with same 7d lookback.
    review_date = date.today() if args.test else _review_saturday()
    out_path = ROOT / "data" / f"weekly_review_{review_date.isoformat()}.md"
    latest_path = ROOT / "data" / "weekly_review_latest.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.test:
        print(
            f"[weekly_review] TEST mode: running immediately ({review_date.isoformat()}); "
            f"lookback={LOOKBACK_DAYS}d",
            flush=True,
        )
    else:
        print(f"[weekly_review] Research date (Saturday): {review_date.isoformat()}", flush=True)
    print("[weekly_review] 1/5 collect + QC", flush=True)
    summary, wisdom_scores, hb = collect_performance()
    goals = score_goals(summary)
    print(
        f"[weekly_review] Data grade {summary.quality.grade} | Mandate {goals.label}",
        flush=True,
    )

    print("[weekly_review] 2/5 single-factor hypothesis", flush=True)
    hypothesis = form_hypothesis(summary, hb)
    print(f"[weekly_review] Treatment: {hypothesis.env_line}", flush=True)

    if args.skip_backtest:
        print("[weekly_review] 3/5 backtest SKIPPED", flush=True)
        baseline = BacktestMetrics(ok=False, error="skipped")
        proposed = BacktestMetrics(ok=False, error="skipped")
        recommendation = "NEEDS MORE DATA"
        reasoning = (
            "Backtests skipped (--skip-backtest). This is a data/methodology preview only; "
            "no IC approval without a completed 90d A/B table."
        )
    else:
        print("[weekly_review] 3/5 controlled 90d A/B", flush=True)
        baseline = run_backtest("baseline")
        proposed = run_backtest(
            "treatment",
            env_overrides={hypothesis.env_key: hypothesis.proposed_value},
        )
        recommendation, reasoning = decide_recommendation(summary, baseline, proposed, goals)

    md = build_markdown(
        review_date,
        summary,
        hypothesis,
        baseline,
        proposed,
        recommendation,
        reasoning,
        wisdom_scores,
        hb,
        goals,
    )

    print(f"[weekly_review] 4/5 write {out_path.name}", flush=True)
    out_path.write_text(md, encoding="utf-8")
    try:
        shutil.copyfile(out_path, latest_path)
    except Exception as exc:
        print(f"[weekly_review] latest copy failed: {exc}", flush=True)

    print("[weekly_review] 5/5 notify", flush=True)
    subject = "[TEST] Weekly Bot Review" if args.test else "Weekly Bot Review — IC action needed"
    notify_owner(subject, md, out_path=out_path)
    if want_open:
        _open_report(out_path)

    print(f"[weekly_review] Done → {recommendation}", flush=True)
    print(md)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
