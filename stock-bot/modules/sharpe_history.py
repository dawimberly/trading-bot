"""Permanent Sharpe history tracking (daily windows + version markers).

Append-only log: ``data/sharpe_history.log`` (human line + JSON payload).
State: ``data/sharpe_history_state.json`` (last EOD date, version markers).
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_ROOT = Path(__file__).resolve().parents[1]
_LOG_PATH = _ROOT / "data" / "sharpe_history.log"
_STATE_PATH = _ROOT / "data" / "sharpe_history_state.json"


def _now_et() -> datetime:
    return datetime.now(_ET)


def _today_et() -> date:
    return _now_et().date()


def _ensure_data_dir() -> None:
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def _load_state() -> dict[str, Any]:
    if not _STATE_PATH.is_file():
        return {}
    try:
        raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    _ensure_data_dir()
    try:
        _STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("sharpe_history state write failed: %s", exc)


def _current_version() -> str:
    return str(getattr(config, "REALISTIC_RESEARCH_VERSION", "unknown") or "unknown")


def _parse_semver(ver: str) -> tuple[int, int, int]:
    parts = str(ver or "").strip().lstrip("vV").split(".")
    nums: list[int] = []
    for p in parts[:3]:
        try:
            nums.append(int("".join(ch for ch in p if ch.isdigit()) or "0"))
        except ValueError:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]


def _is_major_version_change(prev: str | None, cur: str) -> bool:
    """True when major.minor changes (1.5.3 -> 1.5.4 is patch-only)."""
    if not prev:
        return True
    a = _parse_semver(prev)
    b = _parse_semver(cur)
    return (a[0], a[1]) != (b[0], b[1])


def compute_sharpe(
    equities: list[float],
    *,
    periods_per_year: float = 252.0,
    min_rets: int = 4,
) -> float | None:
    """Annualized Sharpe from an equity curve (simple daily returns)."""
    if len(equities) < max(5, min_rets + 1):
        return None
    rets: list[float] = []
    for i in range(1, len(equities)):
        prev = equities[i - 1]
        if prev > 0:
            rets.append(equities[i] / prev - 1.0)
    if len(rets) < min_rets:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    if std < 1e-12:
        return 0.0
    return round((mean / std) * math.sqrt(periods_per_year), 3)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _heartbeat_regime() -> str:
    try:
        from modules.weekly_summary import _load_heartbeat

        hb = _load_heartbeat() or {}
        return str(hb.get("regime") or "")
    except Exception:
        return ""


def _conviction_avg() -> float | None:
    try:
        from modules.risk_management import get_average_conviction

        avg = get_average_conviction(days=7)
        return float(avg) if avg is not None else None
    except Exception:
        return None


def _win_rate_pct(days: int = 30) -> float | None:
    try:
        from modules.strategy_performance import get_strategy_ratings

        ratings = get_strategy_ratings(days=days)
        ranked = ratings.get("ranked") or []
        trades = 0
        wins_w = 0.0
        for row in ranked:
            n = int(row.get("trade_count") or 0)
            if n <= 0:
                continue
            wr = float(row.get("win_rate_pct") or 0.0)
            trades += n
            wins_w += (wr / 100.0) * n
        if trades < 3:
            return None
        return round(100.0 * wins_w / trades, 1)
    except Exception:
        return None


def _risk_control_adj() -> tuple[float, list[str]]:
    """Small conservative adjustments from active risk controls."""
    adj = 0.0
    notes: list[str] = []
    try:
        if config.effective_tail_risk_controls():
            adj += 0.04
            notes.append("tail_guards")
    except Exception as exc:
        logger.debug("sharpe history soft-fail: %s", exc)
    try:
        if config.effective_atr_sizing_enabled():
            adj += 0.03
            notes.append("atr_sizing")
    except Exception as exc:
        logger.debug("sharpe history soft-fail: %s", exc)
    try:
        if config.effective_correlation_guard_enabled():
            adj += 0.02
            notes.append("corr_guard")
    except Exception as exc:
        logger.debug("sharpe history soft-fail: %s", exc)
    try:
        # Yield gate reduces deployment in stress — mild expected-return drag.
        if bool(getattr(config, "YIELD_GATE_ENABLED", True)):
            adj -= 0.03
            notes.append("yield_gate")
    except Exception as exc:
        logger.debug("sharpe history soft-fail: %s", exc)
    try:
        from modules.weekly_summary import _load_heartbeat

        hb = _load_heartbeat() or {}
        if hb.get("halted"):
            adj -= 0.35
            notes.append("halted")
        wisdom = hb.get("wisdom") or {}
        if isinstance(wisdom, dict) and wisdom.get("paused"):
            adj -= 0.12
            notes.append("wisdom_paused")
        sm = wisdom.get("sizing_multiplier") if isinstance(wisdom, dict) else None
        if sm is not None and float(sm) < 0.75:
            adj -= 0.08
            notes.append("sizing_stress")
    except Exception as exc:
        logger.debug("sharpe history soft-fail: %s", exc)
    return _clamp(adj, -0.45, 0.12), notes


def _regime_adj(regime: str) -> tuple[float, str]:
    reg = (regime or "").upper()
    if "RHYME_B" in reg:
        return -0.22, "RHYME_B"
    if "RHYME_E" in reg:
        return -0.12, "RHYME_E"
    if "RHYME_C" in reg or "RHYME_D" in reg:
        return 0.06, "RHYME_C/D"
    if "RHYME_A" in reg:
        return 0.04, "RHYME_A"
    return 0.0, (regime.split(":")[-1].strip() if regime else "n/a")


def calculate_projected_sharpe(
    equity: float | None = None,
    days: int = 30,
) -> dict[str, Any]:
    """Conservative next-N-day Sharpe projection (default 30d).

    Uses clean post-major-update equity when available, then blends small
    regime / conviction / win-rate / risk-control adjustments. Caps optimism
    so projections stay realistic vs recent realized Sharpe.
    """
    days = max(5, int(days or 30))
    series = _equity_series()
    if equity is not None and float(equity) > 0:
        series = list(series) + [(_now_et(), float(equity))]

    state = _load_state()
    since_raw = state.get("last_major_update_date") or state.get("last_version_change_date")
    since_dt: date | None = None
    if since_raw:
        try:
            since_dt = date.fromisoformat(str(since_raw)[:10])
        except ValueError:
            since_dt = None

    clean = _slice_since(series, since_dt) if since_dt else list(series)
    # Prefer post-update window; if too thin, fall back to last ``days``.
    base_series = clean if len(clean) >= 6 else _slice_last_days(series, days)
    if len(base_series) < 6:
        base_series = list(series)
    base_eqs = [eq for _, eq in base_series]
    base = compute_sharpe(base_eqs)
    realized_30 = compute_sharpe([eq for _, eq in _slice_last_days(series, days)])
    realized_all = compute_sharpe([eq for _, eq in series])

    if base is None:
        base = realized_30 if realized_30 is not None else realized_all
    if base is None:
        return {
            "projected_sharpe": None,
            "horizon_days": days,
            "base_sharpe": None,
            "confidence": "low",
            "note": "Insufficient equity history for projection",
            "components": {},
        }

    # Mean-revert ~20% toward 0 (uncertainty haircut).
    shrunk = float(base) * 0.80

    regime = _heartbeat_regime()
    reg_adj, reg_label = _regime_adj(regime)

    conv = _conviction_avg()
    if conv is None:
        conv_adj = 0.0
    else:
        # High conviction helps a little; weak conviction is a mild drag.
        conv_adj = _clamp((float(conv) - 0.50) * 0.20, -0.10, 0.08)

    wr = _win_rate_pct(days=max(days, 30))
    if wr is None:
        wr_adj = 0.0
    else:
        wr_adj = _clamp((float(wr) - 50.0) / 100.0 * 0.30, -0.12, 0.10)

    risk_adj, risk_notes = _risk_control_adj()

    raw = shrunk + reg_adj + conv_adj + wr_adj + risk_adj

    # Optimism caps: don't invent a strong Sharpe from a weak base.
    if float(base) < 0:
        raw = min(raw, float(base) + 0.35, 0.35)
    else:
        raw = min(raw, float(base) + 0.25)

    projected = round(_clamp(raw, -1.50, 1.25), 3)

    # Confidence from sample size + agreement with 30d.
    n_pts = len(base_eqs)
    if n_pts >= 40 and realized_30 is not None and abs(float(realized_30) - float(base)) < 0.4:
        confidence = "moderate"
    elif n_pts >= 15:
        confidence = "low-moderate"
    else:
        confidence = "low"

    note_bits = [
        f"base={float(base):.2f}",
        f"shrink={shrunk:.2f}",
        f"regime={reg_label}({reg_adj:+.2f})",
    ]
    if conv is not None:
        note_bits.append(f"conv={float(conv):.2f}({conv_adj:+.2f})")
    if wr is not None:
        note_bits.append(f"win={float(wr):.0f}%({wr_adj:+.2f})")
    if risk_notes:
        note_bits.append(f"risk={'+'.join(risk_notes)}({risk_adj:+.2f})")

    return {
        "projected_sharpe": projected,
        "horizon_days": days,
        "base_sharpe": round(float(base), 3),
        "realized_30d": realized_30,
        "confidence": confidence,
        "note": "; ".join(note_bits),
        "components": {
            "shrunk_base": round(shrunk, 3),
            "regime": reg_label,
            "regime_adj": round(reg_adj, 3),
            "conviction_avg": conv,
            "conviction_adj": round(conv_adj, 3),
            "win_rate_pct": wr,
            "win_rate_adj": round(wr_adj, 3),
            "risk_adj": round(risk_adj, 3),
            "risk_notes": risk_notes,
            "points": n_pts,
            "since_major": since_dt.isoformat() if since_dt else None,
        },
    }


def _equity_series() -> list[tuple[datetime, float]]:
    paper = bool(config.PAPER_TRADING or config.paper_chase_mode_enabled())
    try:
        from modules.status_metrics import _merge_journal_series

        return _merge_journal_series(paper_chase=paper, live_only=not paper)
    except Exception as exc:
        logger.debug("sharpe_history equity series unavailable: %s", exc)
        return []


def _slice_since(
    series: list[tuple[datetime, float]], since: date | datetime | None
) -> list[tuple[datetime, float]]:
    if not series or since is None:
        return list(series)
    if isinstance(since, datetime):
        cutoff = since
    else:
        cutoff = datetime.combine(since, datetime.min.time(), tzinfo=_ET)
    out: list[tuple[datetime, float]] = []
    for ts, eq in series:
        t = ts
        if t.tzinfo is None:
            t = t.replace(tzinfo=_ET)
        else:
            t = t.astimezone(_ET)
        if t.date() >= cutoff.date():
            out.append((ts, eq))
    return out


def _slice_last_days(
    series: list[tuple[datetime, float]], days: int
) -> list[tuple[datetime, float]]:
    if not series:
        return []
    cutoff = _now_et() - timedelta(days=days)
    out: list[tuple[datetime, float]] = []
    for ts, eq in series:
        t = ts if ts.tzinfo else ts.replace(tzinfo=_ET)
        if t.tzinfo:
            t = t.astimezone(_ET)
        if t >= cutoff:
            out.append((ts, eq))
    return out or series[-min(len(series), max(10, days)) :]


def _append_log(event: str, payload: dict[str, Any]) -> None:
    _ensure_data_dir()
    now = _now_et()
    record = {
        "ts": now.isoformat(),
        "event": event,
        **payload,
    }
    human = (
        f"{now:%Y-%m-%d %H:%M:%S} ET | {event} | "
        f"ver={payload.get('version')} | "
        f"30d={payload.get('sharpe_30d')} 90d={payload.get('sharpe_90d')} "
        f"all={payload.get('sharpe_all')} since_update={payload.get('sharpe_since_update')} "
        f"proj30d={payload.get('projected_sharpe')}"
    )
    try:
        with _LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(human + "\n")
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError as exc:
        logger.warning("sharpe_history log append failed: %s", exc)


def _read_json_lines(limit: int | None = None) -> list[dict[str, Any]]:
    if not _LOG_PATH.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with _LOG_PATH.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError:
        return []
    if limit is not None and limit > 0:
        return rows[-limit:]
    return rows


def build_sharpe_snapshot(
    *,
    equity: float | None = None,
) -> dict[str, Any]:
    """Compute current Sharpe windows from the journal equity curve."""
    series = _equity_series()
    if equity is not None and equity > 0:
        # Anchor today's point so EOD uses live account equity.
        series = list(series) + [(_now_et(), float(equity))]

    eqs_all = [eq for _, eq in series]
    eqs_30 = [eq for _, eq in _slice_last_days(series, 30)]
    eqs_90 = [eq for _, eq in _slice_last_days(series, 90)]

    state = _load_state()
    since_raw = state.get("last_major_update_date") or state.get("last_version_change_date")
    since_dt: date | None = None
    if since_raw:
        try:
            since_dt = date.fromisoformat(str(since_raw)[:10])
        except ValueError:
            since_dt = None
    eqs_since = [eq for _, eq in _slice_since(series, since_dt)] if since_dt else eqs_all

    deploy_raw = state.get("deployment_date")
    if not deploy_raw and series:
        try:
            t0 = series[0][0]
            if t0.tzinfo is None:
                t0 = t0.replace(tzinfo=_ET)
            deploy_raw = t0.astimezone(_ET).date().isoformat()
        except Exception:
            deploy_raw = _today_et().isoformat()

    version = _current_version()
    proj = calculate_projected_sharpe(equity=equity, days=30)
    return {
        "as_of": _now_et().isoformat(),
        "date": _today_et().isoformat(),
        "version": version,
        "equity": float(equity) if equity is not None else (eqs_all[-1] if eqs_all else None),
        "sharpe_30d": compute_sharpe(eqs_30),
        "sharpe_90d": compute_sharpe(eqs_90),
        "sharpe_all": compute_sharpe(eqs_all),
        "sharpe_since_update": compute_sharpe(eqs_since) if since_dt else compute_sharpe(eqs_all),
        "projected_sharpe": proj.get("projected_sharpe"),
        "projected_horizon_days": proj.get("horizon_days"),
        "projected_confidence": proj.get("confidence"),
        "projected_note": proj.get("note"),
        "projected_components": proj.get("components") or {},
        "deployment_date": deploy_raw,
        "last_version": state.get("last_version"),
        "last_major_update_date": state.get("last_major_update_date"),
        "last_version_change_date": state.get("last_version_change_date"),
        "points_all": len(eqs_all),
        "points_30d": len(eqs_30),
        "points_90d": len(eqs_90),
        "paper": bool(config.PAPER_TRADING or config.paper_chase_mode_enabled()),
    }


def update_sharpe_history(
    equity: float | None = None,
    *,
    force: bool = False,
    market_open: bool | None = None,
) -> dict[str, Any] | None:
    """Append a daily Sharpe snapshot (idempotent once per ET day).

    Call at EOD from ``run_all`` / weekly report. When ``market_open`` is True,
    skips unless ``force`` (keeps updates post-close).
    """
    if market_open is True and not force:
        return None

    today = _today_et().isoformat()
    state = _load_state()
    if not force and state.get("last_eod_date") == today:
        return get_sharpe_snapshot()

    version = _current_version()
    prev_version = state.get("last_version")
    snap = build_sharpe_snapshot(equity=equity)

    if not state.get("deployment_date"):
        state["deployment_date"] = snap.get("deployment_date") or today

    version_changed = prev_version is not None and str(prev_version) != version
    if prev_version is None:
        state["last_version"] = version
        state["last_version_change_date"] = today
        state["last_major_update_date"] = today
        markers = list(state.get("version_markers") or [])
        markers.append(
            {
                "date": today,
                "from": None,
                "to": version,
                "major": True,
                "sharpe_30d": snap.get("sharpe_30d"),
                "sharpe_all": snap.get("sharpe_all"),
            }
        )
        state["version_markers"] = markers[-50:]
        _save_state(state)
        snap = build_sharpe_snapshot(equity=equity)
        _append_log(
            "VERSION_INIT",
            {
                **snap,
                "prev_version": None,
                "major": True,
            },
        )
    elif version_changed:
        major = _is_major_version_change(str(prev_version), version)
        state["last_version"] = version
        state["last_version_change_date"] = today
        if major:
            state["last_major_update_date"] = today
        markers = list(state.get("version_markers") or [])
        markers.append(
            {
                "date": today,
                "from": prev_version,
                "to": version,
                "major": major,
                "sharpe_30d": snap.get("sharpe_30d"),
                "sharpe_all": snap.get("sharpe_all"),
            }
        )
        state["version_markers"] = markers[-50:]
        _save_state(state)
        # Recompute since-update after marker write.
        snap = build_sharpe_snapshot(equity=equity)
        _append_log(
            "VERSION_CHANGE",
            {
                **snap,
                "prev_version": prev_version,
                "major": major,
            },
        )

    # Ensure snapshot reflects latest version marker dates.
    snap["last_version"] = state.get("last_version") or version
    snap["last_major_update_date"] = state.get("last_major_update_date")
    snap["last_version_change_date"] = state.get("last_version_change_date")
    snap["deployment_date"] = state.get("deployment_date") or snap.get("deployment_date")

    # Refresh projection after version markers settle.
    proj = calculate_projected_sharpe(equity=equity, days=30)
    snap["projected_sharpe"] = proj.get("projected_sharpe")
    snap["projected_horizon_days"] = proj.get("horizon_days")
    snap["projected_confidence"] = proj.get("confidence")
    snap["projected_note"] = proj.get("note")
    snap["projected_components"] = proj.get("components") or {}

    _append_log("DAILY", snap)
    state["last_eod_date"] = today
    state["last_version"] = version
    state["last_snapshot"] = snap
    _save_state(state)
    logger.info(
        "sharpe_history updated 30d=%s 90d=%s all=%s since_update=%s proj=%s ver=%s",
        snap.get("sharpe_30d"),
        snap.get("sharpe_90d"),
        snap.get("sharpe_all"),
        snap.get("sharpe_since_update"),
        snap.get("projected_sharpe"),
        version,
    )
    return snap


def get_sharpe_snapshot() -> dict[str, Any]:
    """Return last stored snapshot, refreshed lightly from state + live compute."""
    state = _load_state()
    cached = state.get("last_snapshot")
    if isinstance(cached, dict) and cached.get("date") == _today_et().isoformat():
        out = dict(cached)
        if out.get("projected_sharpe") is None and "projected_note" not in out:
            proj = calculate_projected_sharpe(equity=out.get("equity"), days=30)
            out["projected_sharpe"] = proj.get("projected_sharpe")
            out["projected_horizon_days"] = proj.get("horizon_days")
            out["projected_confidence"] = proj.get("confidence")
            out["projected_note"] = proj.get("note")
            out["projected_components"] = proj.get("components") or {}
        return out
    return build_sharpe_snapshot()


def load_sharpe_history(*, limit: int | None = 60) -> list[dict[str, Any]]:
    return _read_json_lines(limit=limit)


def version_history_rows(*, limit: int = 12) -> list[dict[str, Any]]:
    """Rows for dashboard / verify: version change markers newest-first."""
    state = _load_state()
    markers = list(state.get("version_markers") or [])
    rows = list(reversed(markers[-limit:]))
    # Also include init from log if no markers yet.
    if not rows:
        for rec in reversed(load_sharpe_history(limit=200)):
            if rec.get("event") in ("VERSION_INIT", "VERSION_CHANGE"):
                rows.append(
                    {
                        "date": str(rec.get("date") or "")[:10],
                        "from": rec.get("prev_version"),
                        "to": rec.get("version"),
                        "major": bool(rec.get("major")),
                        "sharpe_30d": rec.get("sharpe_30d"),
                        "sharpe_all": rec.get("sharpe_all"),
                    }
                )
                if len(rows) >= limit:
                    break
    return rows


def sharpe_trend_for_health() -> dict[str, Any]:
    """Compact trend signal for Bot Health Score."""
    hist = [r for r in load_sharpe_history(limit=40) if r.get("event") == "DAILY"]
    snap = get_sharpe_snapshot()
    cur_30 = snap.get("sharpe_30d")
    if cur_30 is None:
        return {
            "sharpe_30d": None,
            "sharpe_all": snap.get("sharpe_all"),
            "trend_delta": None,
            "trend": "unknown",
            "note": None,
        }
    prior = None
    for rec in reversed(hist[:-1]):
        v = rec.get("sharpe_30d")
        if v is not None:
            prior = float(v)
            break
    delta = None if prior is None else round(float(cur_30) - prior, 3)
    if delta is None:
        trend = "flat"
    elif delta >= 0.15:
        trend = "improving"
    elif delta <= -0.15:
        trend = "deteriorating"
    else:
        trend = "flat"
    note = None
    if trend == "improving":
        note = f"Sharpe 30d improving ({prior:.2f}→{float(cur_30):.2f})"
    elif trend == "deteriorating":
        note = f"Sharpe 30d deteriorating ({prior:.2f}→{float(cur_30):.2f})"
    elif snap.get("sharpe_all") is not None and float(snap["sharpe_all"]) >= 1.0:
        note = f"All-time Sharpe solid ({float(snap['sharpe_all']):.2f})"
    return {
        "sharpe_30d": float(cur_30),
        "sharpe_all": snap.get("sharpe_all"),
        "sharpe_since_update": snap.get("sharpe_since_update"),
        "trend_delta": delta,
        "trend": trend,
        "note": note,
        "version": snap.get("version"),
    }


def format_sharpe_history_summary() -> str:
    snap = get_sharpe_snapshot()
    def _fmt(v: Any) -> str:
        return f"{float(v):.2f}" if v is not None else "n/a"

    lines = [
        f"Sharpe history — RR v{snap.get('version')}",
        f"  All-time:     {_fmt(snap.get('sharpe_all'))} (since {snap.get('deployment_date') or 'n/a'})",
        f"  Projected:    {_fmt(snap.get('projected_sharpe'))} "
        f"(next {snap.get('projected_horizon_days') or 30}d, "
        f"{snap.get('projected_confidence') or 'n/a'})",
        f"  Since update: {_fmt(snap.get('sharpe_since_update'))} "
        f"(major {snap.get('last_major_update_date') or 'n/a'})",
        f"  30d / 90d:    {_fmt(snap.get('sharpe_30d'))} / {_fmt(snap.get('sharpe_90d'))}",
    ]
    markers = version_history_rows(limit=5)
    if markers:
        lines.append("  Versions:")
        for m in markers[:5]:
            tag = "major" if m.get("major") else "patch"
            lines.append(
                f"    {m.get('date')}: {m.get('from') or '-'} -> {m.get('to')} ({tag}) "
                f"all={_fmt(m.get('sharpe_all'))}"
            )
    return "\n".join(lines)


def dashboard_sharpe_payload() -> dict[str, Any]:
    """Snapshot + version table for the desktop dashboard."""
    snap = get_sharpe_snapshot()
    return {
        "snapshot": snap,
        "versions": version_history_rows(limit=10),
        "summary": format_sharpe_history_summary(),
        "log_path": str(_LOG_PATH),
    }
