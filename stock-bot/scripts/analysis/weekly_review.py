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
LIVE_BOOK = PAPER_BOOK.parent / "alpaca_live"
NYSE_REVIEW_MD = ROOT / "scripts" / "analysis" / "nyse_entry_quality_review.md"

Decision = Literal["APPROVE", "REJECT", "HOLD"]


def _env_bool(key: str, default: str = "false") -> bool:
    return os.getenv(key, default).strip().lower() in ("1", "true", "yes", "on")


def _nyse_quality_section_enabled() -> bool:
    return _env_bool("WEEKLY_REVIEW_NYSE_QUALITY_SECTION", "true")

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
class NyseEntryQuality:
    fixes_enabled: bool = False
    journal_exits: int = 0
    with_entry_hour: int = 0
    open_chase_trades: int = 0
    midday_trades: int = 0
    open_chase_win_rate: float | None = None
    midday_win_rate: float | None = None
    open_chase_avg_pnl: float | None = None
    midday_avg_pnl: float | None = None
    sim_365_return_delta_pp: float | None = None
    sim_365_sharpe_delta: float | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class LivePaperDelta:
    paper_return_pct: float | None = None
    live_return_pct: float | None = None
    delta_pp: float | None = None
    paper_equity: float | None = None
    live_equity: float | None = None
    paper_sharpe: float | None = None
    live_sharpe: float | None = None
    paper_source: str = "none"
    live_source: str = "none"
    notes: list[str] = field(default_factory=list)


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


def _load_book_journal(book_dir: Path) -> tuple[pd.DataFrame, str]:
    """Load journal for a specific portal book (no root fallback — avoids live/paper collision)."""
    path = book_dir / "paper_journal.csv"
    df = _load_csv(path)
    if df.empty or "equity" not in df.columns:
        return pd.DataFrame(), "none"
    return df, path.name


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


def _period_return_from_journal(book_dir: Path, days: int) -> tuple[RiskMetrics, str]:
    journal, source = _load_book_journal(book_dir)
    if journal.empty:
        return RiskMetrics(), source
    cycles = journal
    if "event" in journal.columns:
        cyc = journal[journal["event"].astype(str).str.lower() == "cycle"]
        if not cyc.empty:
            cycles = cyc
    cutoff = datetime.now() - timedelta(days=days)
    recent = cycles[cycles["timestamp"] >= cutoff]
    if recent.empty:
        recent = cycles.tail(500)
    daily_raw = recent.groupby(recent["timestamp"].dt.date)["equity"].last().dropna()
    daily, _, _ = _clean_daily_equity(daily_raw)
    if not daily.empty:
        keep_from = daily.index[-1] - timedelta(days=days)
        try:
            daily = daily[daily.index >= keep_from]
        except Exception:
            daily = daily.tail(days)
    return _risk_from_curve(daily), source


def _collect_live_paper_delta() -> LivePaperDelta:
    delta = LivePaperDelta()
    paper_risk, paper_src = _period_return_from_journal(PAPER_BOOK, LOOKBACK_DAYS)
    live_risk, live_src = _period_return_from_journal(LIVE_BOOK, LOOKBACK_DAYS)
    delta.paper_return_pct = paper_risk.period_return_pct
    delta.live_return_pct = live_risk.period_return_pct
    delta.paper_sharpe = paper_risk.sharpe
    delta.live_sharpe = live_risk.sharpe
    delta.paper_equity = paper_risk.end_equity
    delta.live_equity = live_risk.end_equity
    delta.paper_source = paper_src
    delta.live_source = live_src
    if delta.paper_return_pct is not None and delta.live_return_pct is not None:
        delta.delta_pp = delta.paper_return_pct - delta.live_return_pct
    if live_src == "none":
        delta.notes.append("Live book journal missing — delta is paper-only.")
    elif paper_src == "none":
        delta.notes.append("Paper book journal missing.")
    else:
        delta.notes.append(
            "Read-only comparison; live Profile A is never modified by this review."
        )
    return delta


def _hour_from_entry_hour(raw: str) -> int | None:
    if not raw or str(raw).lower() in ("nan", "none", ""):
        return None
    s = str(raw).strip()
    if ":" in s:
        try:
            return int(s.split(":")[0])
        except ValueError:
            return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _parse_nyse_sim_highlights() -> tuple[float | None, float | None]:
    """Pull 365d intraday sim deltas from research memo if present."""
    if not NYSE_REVIEW_MD.is_file():
        return None, None
    try:
        text = NYSE_REVIEW_MD.read_text(encoding="utf-8")
    except Exception:
        return None, None
    ret_delta = sharpe_delta = None
    m = re.search(
        r"\|\s*Total return\s*\|\s*([\d.]+)\s*\|\s*\*\*([\d.]+)\*\*\s*\|\s*([+-]?[\d.]+)\s*pp",
        text,
    )
    if m:
        try:
            ret_delta = float(m.group(3))
        except ValueError:
            pass
    m = re.search(
        r"\|\s*Sharpe \(daily equity\)\s*\|\s*[\d.]+\s*\|\s*\*\*[\d.]+\*\*\s*\|\s*([+-]?[\d.]+)",
        text,
    )
    if m:
        try:
            sharpe_delta = float(m.group(1))
        except ValueError:
            pass
    return ret_delta, sharpe_delta


def _collect_nyse_entry_quality(journal: pd.DataFrame, cutoff: datetime) -> NyseEntryQuality:
    nq = NyseEntryQuality(
        fixes_enabled=_env_bool("PAPER_MOMENTUM_QUALITY_FIXES", "false"),
    )
    nq.sim_365_return_delta_pp, nq.sim_365_sharpe_delta = _parse_nyse_sim_highlights()

    if journal.empty or "event" not in journal.columns:
        nq.notes.append("No journal — NYSE hour stats unavailable; see intraday research memo.")
        return nq

    df = journal[journal["timestamp"] >= cutoff].copy()
    ev = df["event"].astype(str).str.lower()
    exits = df[ev.isin(TRADE_EVENTS)]
    if "sleeve" in exits.columns:
        nyse = exits[exits["sleeve"].astype(str).str.lower() == "nyse"]
    else:
        nyse = exits.iloc[0:0]
    nq.journal_exits = int(len(nyse))
    if nq.journal_exits == 0:
        nq.notes.append("No NYSE closed trades in 7d window.")
        nq.notes.append(
            "`entry_hour` populates on exits after quality-fixes deploy — journal may be sparse."
        )
        return nq

    open_pnls: list[float] = []
    midday_pnls: list[float] = []
    for _, row in nyse.iterrows():
        eh = row.get("entry_hour", "")
        if pd.notna(eh) and str(eh).strip():
            nq.with_entry_hour += 1
        hour = _hour_from_entry_hour(str(eh) if pd.notna(eh) else "")
        pnl = _extract_pnl(str(row.get("notes") or ""))
        if hour is not None and 9 <= hour < 10:
            nq.open_chase_trades += 1
            if pnl is not None:
                open_pnls.append(pnl)
        elif hour is not None and 12 <= hour < 14:
            nq.midday_trades += 1
            if pnl is not None:
                midday_pnls.append(pnl)

    if open_pnls:
        nq.open_chase_avg_pnl = float(np.mean(open_pnls))
        nq.open_chase_win_rate = 100.0 * sum(1 for p in open_pnls if p > 0) / len(open_pnls)
    if midday_pnls:
        nq.midday_avg_pnl = float(np.mean(midday_pnls))
        nq.midday_win_rate = 100.0 * sum(1 for p in midday_pnls if p > 0) / len(midday_pnls)
    if nq.with_entry_hour == 0:
        nq.notes.append(
            "No `entry_hour` on NYSE exits yet — hour buckets empty until post-deploy closes."
        )
    return nq


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
    hypothesis: Hypothesis,
) -> tuple[Decision, str, str]:
    """Return (decision, detail_reasoning, one_sentence_rationale)."""
    if not baseline.ok or not proposed.ok:
        detail = (
            f"Controlled experiment incomplete (baseline_ok={baseline.ok}, "
            f"proposed_ok={proposed.ok}; {baseline.error or '—'} | {proposed.error or '—'})."
        )
        return (
            "HOLD",
            detail,
            "90d A/B incomplete; no paper parameter change without a valid experiment.",
        )

    ret_delta = (proposed.return_pct or 0) - (baseline.return_pct or 0)
    sharpe_delta = (proposed.sharpe or 0) - (baseline.sharpe or 0)
    base_dd = baseline.dd_magnitude or 0.0
    prop_dd = proposed.dd_magnitude or 0.0
    dd_mag_delta = prop_dd - base_dd

    improved = sharpe_delta > 0.05 and ret_delta >= -0.5 and dd_mag_delta <= 0.5
    worsened = sharpe_delta < -0.05 or (ret_delta < -1.0 and dd_mag_delta > 0.25)

    caveats = []
    if summary.quality.grade == "C":
        caveats.append("data grade C")
    if summary.closed_trades < MIN_TRADES_GOAL:
        caveats.append("thin closed-trade sample")
    if goals.label.startswith("UNRELIABLE"):
        caveats.append("mandate unreliable")
    caveat_txt = (" Caveats: " + "; ".join(caveats) + ".") if caveats else ""

    detail_base = (
        f"ΔReturn={ret_delta:+.2f}pp, ΔSharpe={sharpe_delta:+.2f}, Δ|DD|={dd_mag_delta:+.2f}pp."
    )

    if improved:
        return (
            "APPROVE",
            f"90d A/B supports single-factor change ({detail_base}) Paper-only; owner edits `.env`."
            + caveat_txt,
            f"90d A/B supports `{hypothesis.env_line}` ({detail_base})",
        )
    if worsened:
        return (
            "REJECT",
            f"90d A/B falsifies `{hypothesis.env_key}` change ({detail_base}) Retain current paper config."
            + caveat_txt,
            f"90d A/B falsifies `{hypothesis.env_line}`; keep current paper config.",
        )
    return (
        "HOLD",
        f"Effect size mixed / within noise ({detail_base}) Gather another week of clean data."
        + caveat_txt,
        f"Mixed 90d A/B for `{hypothesis.env_key}`; no paper change this week.",
    )


def _fmt_pct(val: float | None, digits: int = 2) -> str:
    if val is None:
        return "n/a"
    return f"{val:.{digits}f}%"


def _fmt_num(val: float | None, digits: int = 2) -> str:
    if val is None:
        return "n/a"
    return f"{val:.{digits}f}"


def _is_sparse_week(summary: PerfSummary) -> bool:
    return summary.quality.grade == "C" or summary.closed_trades < MIN_TRADES_GOAL


def _wisdom_tone(wisdom_scores: list[dict], regime: str) -> str | None:
    reg = (regime or "").lower()
    if "defensive" in reg or "stress" in reg or "bear" in reg:
        return "defensive"
    if not wisdom_scores:
        return None
    rets = [
        float(r["return_pct"])
        for r in wisdom_scores
        if r.get("return_pct") is not None and not math.isnan(float(r["return_pct"]))
    ]
    if len(rets) >= 3 and sum(rets) / len(rets) < 0:
        return "defensive"
    if len(rets) >= 3 and sum(rets) / len(rets) > 0.15:
        return "supportive"
    return "neutral"


def _key_observations(
    summary: PerfSummary,
    nyse_quality: NyseEntryQuality | None,
    wisdom_scores: list[dict],
    hb: dict | None,
    goals: GoalsStatus,
) -> list[str]:
    obs: list[str] = []
    if _is_sparse_week(summary):
        obs.append(
            f"Insufficient closed trades for strong mandate "
            f"({summary.closed_trades} closes, grade {summary.quality.grade})."
        )
    if summary.closed_trades == 0:
        obs.append("No closed trades in 7d — sleeve ranks use mark-to-market only.")
    if nyse_quality is not None and nyse_quality.journal_exits == 0:
        obs.append("No NYSE closed trades in 7d window.")
    elif nyse_quality is not None and nyse_quality.fixes_enabled:
        obs.append(
            f"NYSE quality fixes on; {nyse_quality.journal_exits} exit(s) logged "
            f"({nyse_quality.with_entry_hour} with entry_hour)."
        )
    for name, st in sorted(summary.sleeves.items(), key=lambda kv: abs(kv[1].contribution), reverse=True):
        if name == "unknown" or abs(st.contribution) < 1:
            continue
        if st.realized_trades == 0 and st.unrealized_pnl != 0:
            obs.append(f"{name.title()} unrealized {st.unrealized_pnl:+.0f} (MTM).")
        elif st.realized_trades > 0:
            obs.append(
                f"{name.title()} realized {st.realized_pnl:+.0f} "
                f"({st.realized_trades} closes, WR {_fmt_pct(st.win_rate, 0)})."
            )
        if len([o for o in obs if "unrealized" in o or "realized" in o]) >= 3:
            break
    tone = _wisdom_tone(wisdom_scores, summary.regime)
    if tone == "defensive":
        obs.append(f"Wisdom/regime tone defensive (`{summary.regime}`).")
    elif tone == "supportive":
        obs.append("Wisdom daily returns supportive this week.")
    if summary.risk.period_return_pct is not None:
        if summary.risk.period_return_pct < goals.target_return_pct:
            obs.append(
                f"7d return {_fmt_pct(summary.risk.period_return_pct)} below "
                f"{goals.target_return_pct}% target."
            )
    if hb and hb.get("halted"):
        obs.append("Bot halted per heartbeat — investigate before any param change.")
    if not obs:
        obs.append(f"Regime `{summary.regime}`; {summary.closed_trades} closed trades; grade {summary.quality.grade}.")
    return obs[:6]


def _sparse_monitor_rationale(hypothesis: Hypothesis, summary: PerfSummary) -> str:
    return (
        f"Monitor only — data too sparse (grade {summary.quality.grade}, "
        f"{summary.closed_trades} closes); track `{hypothesis.env_line}` until grade B+ and 90d A/B."
    )


def _grade_criteria_lines() -> list[str]:
    return [
        "| Grade | Criteria | Mandate usable? |",
        "|-------|----------|-----------------|",
        (
            f"| **A** | ≥{MIN_DAILY_OBS} clean days, 0 equity jumps, "
            f"≥{MIN_TRADES_GOAL} closed trades | Yes |"
        ),
        (
            f"| **B** | ≥{MIN_DAILY_OBS} days, ≤2 jumps after filter, "
            f"trades may be sparse | Yes (cautious) |"
        ),
        "| **C** | Missing equity, <2 days, or noisy/unfiltered | **No** |",
    ]
    return [
        "| Grade | Criteria | Mandate usable? |",
        "|-------|----------|-----------------|",
        (
            f"| **A** | ≥{MIN_DAILY_OBS} clean days, 0 equity jumps, "
            f"≥{MIN_TRADES_GOAL} closed trades | Yes |"
        ),
        (
            f"| **B** | ≥{MIN_DAILY_OBS} days, ≤2 jumps after filter, "
            f"trades may be sparse | Yes (cautious) |"
        ),
        "| **C** | Missing equity, <2 days, or noisy/unfiltered | **No** |",
    ]


def build_markdown(
    review_date: date,
    summary: PerfSummary,
    hypothesis: Hypothesis,
    baseline: BacktestMetrics,
    proposed: BacktestMetrics,
    decision: Decision,
    rationale: str,
    detail: str,
    wisdom_scores: list[dict],
    hb: dict | None,
    goals: GoalsStatus,
    nyse_quality: NyseEntryQuality | None,
    live_paper: LivePaperDelta,
    *,
    monitor_only: bool = False,
) -> str:
    sparse = _is_sparse_week(summary)
    observations = _key_observations(summary, nyse_quality, wisdom_scores, hb, goals)
    r = summary.risk
    q = summary.quality
    lines: list[str] = [
        f"# Paper Weekly Research — {review_date.isoformat()}",
        "",
        "_Paper book only. Never auto-applies `.env`. Live Profile A is read-only here._",
        "",
        "## Executive Decision",
        f"**{decision}** — {rationale}",
        "",
        f"Treatment under review: `{hypothesis.env_line}`"
        + (" · _monitor only_" if monitor_only else ""),
        "",
        "## Mandate",
        f"**Score: {goals.label}** · lookback {LOOKBACK_DAYS}d · regime `{summary.regime}`",
    ]
    if sparse:
        lines.append(
            f"_Insufficient closed trades for strong mandate "
            f"({summary.closed_trades}/{MIN_TRADES_GOAL} closes, grade {q.grade}) — "
            f"use mark-to-market attribution below._"
        )
    lines.append("")
    lines.append(
        (
            f"| Target | Threshold | Actual |\n"
            f"|--------|-----------|--------|\n"
            f"| Ann. Sharpe | ≥ {goals.target_sharpe} | {_fmt_num(r.sharpe)} |\n"
            f"| Max DD | ≤ {goals.max_dd_pct}% | {_fmt_pct(r.max_dd_pct)} |\n"
            f"| 7d return | ≥ {goals.target_return_pct}% | {_fmt_pct(r.period_return_pct)} |\n"
            f"| Closed trades | ≥ {goals.min_trades} | {summary.closed_trades} |"
        )
    )
    hits = goals.toward_success[:3]
    misses = goals.toward_failure[:3]
    if hits:
        lines.append("")
        lines.append("Pass: " + "; ".join(hits))
    if misses:
        lines.append("Miss: " + "; ".join(misses))

    lines.extend(["", "## Data Quality", f"**Grade {q.grade}** · source `{q.equity_source}`"])
    lines.extend(_grade_criteria_lines())
    lines.extend(
        [
            "",
            (
                f"Observations: {q.rows_clean} clean / {q.rows_raw} raw · "
                f"jumps removed: {q.jumps_removed} · "
                f"method: EOD equity, √252 Sharpe, single-factor rule"
            ),
        ]
    )
    for n in q.notes[:3]:
        lines.append(f"- {n}")

    lines.extend(
        [
            "",
            "## Performance (7d, cleaned)",
            (
                f"| | |\n|--|--|\n"
                f"| Equity | {_fmt_num(r.start_equity, 0)} → {_fmt_num(r.end_equity, 0)} |\n"
                f"| Return | {_fmt_pct(r.period_return_pct)} |\n"
                f"| Sharpe / Sortino | {_fmt_num(r.sharpe)} / {_fmt_num(r.sortino)} |\n"
                f"| Max DD | {_fmt_pct(r.max_dd_pct)} |\n"
                f"| Closed trades | {summary.closed_trades} "
                f"(WR {_fmt_pct(summary.closed_win_rate, 0)}, exp {_fmt_num(summary.expectancy)}) |\n"
                f"| Daily hit rate | {_fmt_pct(r.daily_win_rate_pct, 0)} |"
            ),
        ]
    )
    lines.append("")
    lines.append("**Key Observations**")
    for ob in observations:
        lines.append(f"- {ob}")

    try:
        from modules.markov_regime import format_weekly_hmm_section

        lines.append("")
        lines.extend(format_weekly_hmm_section())
    except Exception:
        pass

    if sparse:
        lines.extend(
            [
                "",
                "## Sleeve Attribution (mark-to-market)",
                "_Realized closes sparse — contrib = realized + unrealized from heartbeat._",
                f"Best **{summary.best_sleeve}** ({summary.best_sleeve_pnl:+.0f}) · "
                f"Worst **{summary.worst_sleeve}** ({summary.worst_sleeve_pnl:+.0f})",
                "",
                "| Sleeve | Realized | Unrealized | Contrib | Cls | Src |",
                "|--------|----------|------------|---------|-----|-----|",
            ]
        )
        for name, st in sorted(summary.sleeves.items(), key=lambda kv: kv[1].contribution, reverse=True):
            if name == "unknown" and st.contribution == 0:
                continue
            lines.append(
                f"| {name} | {st.realized_pnl:+.0f} | {st.unrealized_pnl:+.0f} | "
                f"{st.contribution:+.0f} | {st.realized_trades} | {st.source} |"
            )
    else:
        lines.extend(
            [
                "",
                "## Sleeve Attribution",
                f"Best **{summary.best_sleeve}** ({summary.best_sleeve_pnl:+.0f}) · "
                f"Worst **{summary.worst_sleeve}** ({summary.worst_sleeve_pnl:+.0f})",
                "",
                "| Sleeve | Cls | Win% | Contrib | Src |",
                "|--------|-----|------|---------|-----|",
            ]
        )
        for name, st in sorted(summary.sleeves.items(), key=lambda kv: kv[1].contribution, reverse=True):
            if name == "unknown" and st.contribution == 0:
                continue
            lines.append(
                f"| {name} | {st.realized_trades} | {_fmt_pct(st.win_rate, 0)} | "
                f"{st.contribution:+.0f} | {st.source} |"
            )

    if _nyse_quality_section_enabled() and nyse_quality is not None:
        nq = nyse_quality
        lines.extend(
            [
                "",
                "## NYSE Entry Quality",
                (
                    f"`PAPER_MOMENTUM_QUALITY_FIXES`="
                    f"{'**on**' if nq.fixes_enabled else 'off'} · "
                    f"7d NYSE exits: {nq.journal_exits} "
                    f"({nq.with_entry_hour} with `entry_hour`)"
                ),
                "",
                "| Window (ET) | Trades | Win% | Avg PnL |",
                "|-------------|--------|------|---------|",
                (
                    f"| 9:30–10:00 open-chase | {nq.open_chase_trades} | "
                    f"{_fmt_pct(nq.open_chase_win_rate, 0)} | {_fmt_num(nq.open_chase_avg_pnl)} |"
                ),
                (
                    f"| 12:00–14:00 midday | {nq.midday_trades} | "
                    f"{_fmt_pct(nq.midday_win_rate, 0)} | {_fmt_num(nq.midday_avg_pnl)} |"
                ),
            ]
        )
        if nq.sim_365_return_delta_pp is not None or nq.sim_365_sharpe_delta is not None:
            lines.append(
                f"365d intraday sim (memo): Δreturn {_fmt_num(nq.sim_365_return_delta_pp)} pp · "
                f"ΔSharpe {_fmt_num(nq.sim_365_sharpe_delta)}"
            )
        for n in nq.notes[:2]:
            lines.append(f"- {n}")

    lines.extend(
        [
            "",
            "## Live vs Paper Delta",
            "_Read-only. Does not modify live._",
            "",
            (
                f"| Book | 7d return | Sharpe | Equity | Source |\n"
                f"|------|-----------|--------|--------|--------|\n"
                f"| Paper | {_fmt_pct(live_paper.paper_return_pct)} | "
                f"{_fmt_num(live_paper.paper_sharpe)} | "
                f"{_fmt_num(live_paper.paper_equity, 0)} | {live_paper.paper_source} |\n"
                f"| Live | {_fmt_pct(live_paper.live_return_pct)} | "
                f"{_fmt_num(live_paper.live_sharpe)} | "
                f"{_fmt_num(live_paper.live_equity, 0)} | {live_paper.live_source} |"
            ),
        ]
    )
    if live_paper.delta_pp is not None:
        lines.append(f"**Paper − Live return:** {live_paper.delta_pp:+.2f} pp")
    for n in live_paper.notes[:2]:
        lines.append(f"- {n}")

    lines.extend(
        [
            "",
            "## Hypothesis (single factor)",
            f"**Proposed change:** `{hypothesis.env_key}={hypothesis.proposed_value}` "
            f"(current `{hypothesis.current_value}`)",
            f"**Rationale:** {hypothesis.what}",
            f"**Mechanism:** {hypothesis.mechanism}",
        ]
    )
    if monitor_only:
        lines.append(
            "**Status: Data too sparse — monitor only.** "
            "Do not apply without grade B+ data and a passing 90d A/B."
        )
    else:
        lines.append(f"**Pass (90d A/B):** {hypothesis.expected_outcome}")
        lines.append(f"**Fail:** {hypothesis.falsification}")

    lines.extend(
        [
            "",
            f"## Controlled Experiment ({BACKTEST_DAYS}d paper-aggressive A/B)",
            "| Metric | Baseline | Treatment | Δ |",
            "|--------|----------|-----------|---|",
            (
                f"| Return | {_fmt_pct(baseline.return_pct)} | {_fmt_pct(proposed.return_pct)} | "
                f"{_fmt_num((proposed.return_pct or 0) - (baseline.return_pct or 0) if baseline.ok and proposed.ok else None)} pp |"
            ),
            (
                f"| Sharpe | {_fmt_num(baseline.sharpe)} | {_fmt_num(proposed.sharpe)} | "
                f"{_fmt_num((proposed.sharpe or 0) - (baseline.sharpe or 0) if baseline.ok and proposed.ok else None)} |"
            ),
            (
                f"| Max DD | {_fmt_pct(baseline.max_dd_pct)} | {_fmt_pct(proposed.max_dd_pct)} | "
                f"{_fmt_num((proposed.dd_magnitude or 0) - (baseline.dd_magnitude or 0) if baseline.ok and proposed.ok else None)} pp |"
            ),
        ]
    )
    if not baseline.ok:
        lines.append(f"_Baseline: {baseline.error}_")
    if not proposed.ok:
        lines.append(f"_Treatment: {proposed.error}_")

    lines.extend(
        [
            "",
            "## Recommendation",
            f"**{decision}** — {detail}",
            "",
            "## Implementation (paper only)",
        ]
    )
    if monitor_only:
        lines.extend(
            [
                "**Monitor only** — no `.env` change this week.",
                f"- Watch: `{hypothesis.env_line}`",
                "- Promote only after grade B+, ≥5 closed trades, and 90d A/B APPROVE.",
                "- Do **not** copy to live.",
            ]
        )
    else:
        lines.extend(
            [
                "Apply **only** to the paper book `.env` if APPROVE:",
                "```",
                hypothesis.env_line,
                "```",
                "Do **not** copy to live. Restart paper bot. Re-check next Saturday.",
            ]
        )

    if wisdom_scores or summary.notes:
        lines.extend(["", "## Appendix"])
        if wisdom_scores:
            lines.append("**Wisdom (last 3):**")
            for row in wisdom_scores[-3:]:
                lines.append(
                    f"- {row.get('date')}: ret={row.get('return_pct')} "
                    f"sharpe={row.get('sharpe')}"
                )
        if summary.notes:
            lines.append("**Notes:**")
            for n in summary.notes[:4]:
                lines.append(f"- {n}")

    lines.extend(["", "<!-- advisory only; never auto-apply -->", ""])
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
        exec_end = body.find("## Mandate")
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
    cutoff = datetime.now() - timedelta(days=LOOKBACK_DAYS)
    journal, _ = _load_best_paper_journal()
    nyse_quality = _collect_nyse_entry_quality(journal, cutoff) if _nyse_quality_section_enabled() else None
    live_paper = _collect_live_paper_delta()
    live_paper.paper_return_pct = summary.risk.period_return_pct
    live_paper.paper_sharpe = summary.risk.sharpe
    live_paper.paper_equity = summary.risk.end_equity
    if summary.quality.equity_source != "none":
        live_paper.paper_source = summary.quality.equity_source
    print(
        f"[weekly_review] Data grade {summary.quality.grade} | Mandate {goals.label}",
        flush=True,
    )

    print("[weekly_review] 2/5 single-factor hypothesis", flush=True)
    hypothesis = form_hypothesis(summary, hb)
    print(f"[weekly_review] Treatment: {hypothesis.env_line}", flush=True)

    monitor_only = _is_sparse_week(summary)

    if args.skip_backtest:
        print("[weekly_review] 3/5 backtest SKIPPED", flush=True)
        baseline = BacktestMetrics(ok=False, error="skipped")
        proposed = BacktestMetrics(ok=False, error="skipped")
        decision = "HOLD"
        if monitor_only:
            rationale = _sparse_monitor_rationale(hypothesis, summary)
            detail = "Backtest skipped; sparse week — monitor proposed lever, no promotion."
        else:
            detail = "Backtests skipped (--skip-backtest); format preview only."
            rationale = "No 90d A/B run; wait for full Saturday pipeline before any paper change."
    else:
        print("[weekly_review] 3/5 controlled 90d A/B", flush=True)
        baseline = run_backtest("baseline")
        proposed = run_backtest(
            "treatment",
            env_overrides={hypothesis.env_key: hypothesis.proposed_value},
        )
        decision, detail, rationale = decide_recommendation(
            summary, baseline, proposed, goals, hypothesis
        )
        if monitor_only:
            if decision == "APPROVE":
                decision = "HOLD"
                detail = (
                    f"90d A/B positive but week sparse (grade {summary.quality.grade}, "
                    f"{summary.closed_trades} closes) — monitor only."
                )
            rationale = _sparse_monitor_rationale(hypothesis, summary)

    md = build_markdown(
        review_date,
        summary,
        hypothesis,
        baseline,
        proposed,
        decision,
        rationale,
        detail,
        wisdom_scores,
        hb,
        goals,
        nyse_quality,
        live_paper,
        monitor_only=monitor_only,
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

    print(f"[weekly_review] Done -> {decision}", flush=True)
    try:
        print(md)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((md + "\n").encode("utf-8", errors="replace"))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
