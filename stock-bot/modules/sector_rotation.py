"""Sector rotation sleeve — paper-first, optional small live book.

Rotates into the strongest sector SPDRs (momentum + RS vs SPY) using the
existing sector screener. Rebalances monthly or on regime change. Caps any
single sector at 20–30%. Scales with Smart Dynamic VTI and conviction sizing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import config
from modules.safe_io import read_json_file, write_json_file
from modules.sector_screener import (
    SECTOR_ETF_DEFS,
    compute_sector_regime_score,
    compute_sector_strengths,
    sector_etf_symbols,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
_STATE_PATH = ROOT / "data" / "sector_rotation_state.json"
_SLEEVE = "SECTOR_ROT"
_ET = ZoneInfo("America/New_York")


def _state_path() -> Path:
    raw = getattr(config, "SECTOR_ROTATION_STATE_FILE", None)
    if raw:
        p = Path(str(raw))
        return p if p.is_absolute() else ROOT / p
    return _STATE_PATH


def _load_state() -> dict[str, Any]:
    raw = read_json_file(_state_path()) or {}
    return raw if isinstance(raw, dict) else {}


def _save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_file(path, state)


def _now_et() -> datetime:
    return datetime.now(_ET)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def ticker_sector(symbol: str) -> str:
    """Map symbol → sector label (ETF defs, then dynamic universe map)."""
    sym = config.normalize_symbol(symbol)
    if not sym:
        return "Other"
    if sym in SECTOR_ETF_DEFS:
        return str(SECTOR_ETF_DEFS[sym][0])
    try:
        from modules.dynamic_universe import sector_for_symbol

        return sector_for_symbol(sym) or "Other"
    except Exception:
        return "Other"


def sector_rotation_live_enabled() -> bool:
    return bool(
        getattr(config, "SECTOR_ROTATION_ENABLED", True)
        and getattr(config, "SECTOR_ROTATION_LIVE_SLEEVE", False)
    )


def effective_sector_rotation_enabled() -> bool:
    """Paper / research on when flagged; live only with explicit opt-in."""
    if not bool(getattr(config, "SECTOR_ROTATION_ENABLED", True)):
        return False
    # Legacy research toggle
    paper_flag = bool(getattr(config, "PAPER_SECTOR_ROTATION_ENABLED", False))
    if config.PAPER_TRADING or config.paper_aggressive_context() or config.backtest_paper_sleeves_context():
        return bool(paper_flag or getattr(config, "SECTOR_ROTATION_PAPER_DEFAULT", True))
    if config.is_realistic_research_active():
        return True
    return sector_rotation_live_enabled()


def _max_sector_pct() -> float:
    return _clamp(float(getattr(config, "SECTOR_ROTATION_MAX_SECTOR_PCT", 0.25)), 0.10, 0.35)


def _sleeve_cap_pct(*, live: bool = False) -> float:
    if live:
        return _clamp(float(getattr(config, "SECTOR_ROTATION_LIVE_CAP_PCT", 0.05)), 0.01, 0.15)
    return _clamp(float(getattr(config, "SECTOR_ROTATION_CAP_PCT", 0.20)), 0.05, 0.40)


def _top_n() -> int:
    """Hold 2–3 leading sector SPDRs (Realistic Research default: 3)."""
    return max(2, min(3, int(getattr(config, "SECTOR_ROTATION_TOP_N", 3))))


def _min_score() -> float:
    return float(getattr(config, "SECTOR_ROTATION_MIN_SCORE", 0.0))


def _rebalance_drift_pct() -> float:
    return _clamp(float(getattr(config, "SECTOR_ROTATION_DRIFT_PCT", 0.04)), 0.01, 0.20)


def _vti_scale(equity: float, executor=None, *, regime: str = "", data=None) -> float:
    """Shrink sector sleeve when Smart Dynamic VTI is elevated (defensive)."""
    try:
        vti_pct = float(
            config.vti_core_allocation_pct(
                equity=equity,
                volatility=getattr(executor, "_last_vol_label", None)
                if executor is not None
                else None,
                regime=regime or None,
                data=data,
            )
        )
    except Exception:
        vti_pct = float(getattr(config, "VTI_CORE_PCT", 0.4) or 0.4)
    # At 40% VTI → 1.0x; at 70% VTI → ~0.55x; floor 0.45x so sleeve never vanishes.
    scale = 1.0 - 0.9 * max(0.0, vti_pct - 0.40)
    return _clamp(scale, 0.45, 1.15)


def _conviction_adjust(
    etf: str, data, regime: str, need: float
) -> tuple[float, float]:
    """Return (adjusted_notional, conviction_score)."""
    conv = 0.55
    if not config.effective_conviction_sizing_enabled():
        return need, conv
    try:
        from modules.risk_management import compute_conviction_score, conviction_scale

        conv = float(compute_conviction_score(etf, data, regime, sleeve="nyse"))
        need = round(need * conviction_scale(conv, scale_band=(0.75, 1.15)), 2)
    except Exception as exc:
        logger.debug("sector rotation conviction skipped for %s: %s", etf, exc)
    return need, conv


def build_sector_targets(
    data,
    *,
    regime: str | None = None,
) -> dict[str, Any]:
    """Rank sectors and return target weights (sum ≤ 1 within sleeve)."""
    del regime
    strengths = compute_sector_strengths(data)
    if not strengths:
        return {
            "targets": {},
            "rows": [],
            "regime_score": 0.5,
            "reason": "no_sector_data",
        }

    regime_score = compute_sector_regime_score(data, strengths)
    min_score = _min_score()
    strong_min = float(getattr(config, "SECTOR_STRONG_SCORE_MIN", 0.06))
    floor = min(min_score, strong_min * 0.25)

    ranked = [r for r in strengths if float(r.get("score") or 0) >= floor]
    if not ranked:
        ranked = strengths[:1]

    top = ranked[: _top_n()]
    # Soft-qualify: prefer RS>0 and above short MA when available.
    preferred = [
        r
        for r in top
        if float(r.get("rs_vs_spy") or 0) >= float(getattr(config, "SECTOR_RS_MIN", 0.0))
        and (r.get("above_ma_short") or r.get("above_ma200"))
    ]
    if preferred:
        top = preferred[: _top_n()]

    raw_scores = [max(0.01, float(r.get("score") or 0) + 0.05) for r in top]
    total = sum(raw_scores) or 1.0
    max_pct = _max_sector_pct()

    # Cap + redistribute overflow; do not renormalize above max (cash buffer OK).
    weights = {r["etf"]: raw_scores[i] / total for i, r in enumerate(top)}
    for _ in range(6):
        overflow = 0.0
        free: list[str] = []
        for etf, w in list(weights.items()):
            if w > max_pct + 1e-12:
                overflow += w - max_pct
                weights[etf] = max_pct
            elif w < max_pct - 1e-12:
                free.append(etf)
        if overflow <= 1e-9 or not free:
            break
        free_room = sum(max_pct - weights[e] for e in free)
        if free_room <= 1e-12:
            break
        for e in free:
            room = max_pct - weights[e]
            weights[e] += overflow * (room / free_room)

    # If still over 1.0 (shouldn't), scale down; never force sum up past caps.
    s = sum(weights.values())
    if s > 1.0 + 1e-9:
        weights = {k: v / s for k, v in weights.items()}
        weights = {k: min(v, max_pct) for k, v in weights.items()}
    weights = {k: round(float(v), 4) for k, v in weights.items()}

    rows = []
    for r in strengths:
        etf = r["etf"]
        rows.append(
            {
                **r,
                "target_weight": weights.get(etf, 0.0),
                "selected": etf in weights,
            }
        )

    return {
        "targets": weights,
        "rows": rows,
        "regime_score": regime_score,
        "reason": "ok",
        "top": [r["etf"] for r in top],
    }


def _month_key(dt: datetime | None = None) -> str:
    d = dt or _now_et()
    return f"{d.year:04d}-{d.month:02d}"


def should_rebalance(
    state: dict[str, Any],
    *,
    regime: str,
    force: bool = False,
    current_weights: dict[str, float] | None = None,
    target_weights: dict[str, float] | None = None,
    as_of: datetime | None = None,
) -> tuple[bool, str]:
    if force:
        return True, "force"
    month = _month_key(as_of)
    if not state.get("last_rebalance_month"):
        return True, "initial"
    if state.get("last_rebalance_month") != month:
        return True, "monthly"
    prev_regime = str(state.get("last_regime") or "")
    if prev_regime and regime and prev_regime != regime:
        # Only major rhyme letter changes (A/B/C/D/E).
        def _letter(r: str) -> str:
            u = r.upper()
            for L in ("RHYME_A", "RHYME_B", "RHYME_C", "RHYME_D", "RHYME_E"):
                if L in u:
                    return L
            return u[:12]

        if _letter(prev_regime) != _letter(regime):
            return True, "regime_change"
    if current_weights is not None and target_weights is not None:
        drift = 0.0
        keys = set(current_weights) | set(target_weights)
        for k in keys:
            drift += abs(float(current_weights.get(k, 0)) - float(target_weights.get(k, 0)))
        if drift * 0.5 >= _rebalance_drift_pct():  # L1/2 ≈ avg abs drift
            return True, "drift"
    return False, "hold"


def _position_value(pos) -> float:
    mv = getattr(pos, "market_value", None)
    if mv is not None:
        return abs(float(mv))
    qty = abs(float(getattr(pos, "qty", 0) or 0))
    px = float(getattr(pos, "current_price", 0) or 0)
    return qty * px


def _sector_positions(executor) -> dict[str, Any]:
    held: dict[str, Any] = {}
    etfs = set(sector_etf_symbols())
    try:
        positions = executor._get_positions()
    except Exception:
        return held
    for pos in positions:
        sym = config.normalize_symbol(pos.symbol)
        if sym in etfs and float(getattr(pos, "qty", 0) or 0) > 0:
            held[sym] = pos
    return held


def _current_weights(held: dict[str, Any], sleeve_value: float) -> dict[str, float]:
    if sleeve_value <= 0:
        return {sym: 0.0 for sym in held}
    return {sym: _position_value(pos) / sleeve_value for sym, pos in held.items()}


def run_sector_rotation_cycle(
    data,
    executor,
    *,
    regime: str = "",
    market_open: bool = True,
    live: bool = False,
    journal=None,
    yield_gated: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Rebalance sector SPDR book toward strongest sectors."""
    result: dict[str, Any] = {
        "enabled": False,
        "live": live,
        "targets": {},
        "actions": [],
        "skipped": None,
        "rebalance_reason": None,
    }
    if not effective_sector_rotation_enabled():
        result["skipped"] = "disabled"
        return result
    if live and not sector_rotation_live_enabled():
        result["skipped"] = "live_opt_in_off"
        return result
    if not market_open:
        result["skipped"] = "market_closed"
        return result
    if config.effective_yield_gate(yield_gated, regime=regime):
        result["skipped"] = "yield_gate"
        return result
    if "RHYME_B" in str(regime or "").upper():
        result["skipped"] = "regime_b"
        return result

    result["enabled"] = True
    plan = build_sector_targets(data, regime=regime)
    targets = plan.get("targets") or {}
    result["targets"] = targets
    result["regime_score"] = plan.get("regime_score")
    result["rows"] = plan.get("rows") or []

    try:
        account = executor._get_account()
        equity = float(account.equity)
        cash = float(account.cash)
    except Exception as exc:
        result["skipped"] = f"account_error:{exc}"
        return result

    vti_scale = _vti_scale(equity, executor, regime=regime, data=data)
    # Sector leadership boosts sleeve when regime_score is high.
    regime_boost = 0.85 + 0.30 * float(plan.get("regime_score") or 0.5)
    cap_pct = _sleeve_cap_pct(live=live) * vti_scale * _clamp(regime_boost, 0.7, 1.2)
    cap_pct = min(cap_pct, _sleeve_cap_pct(live=live) * 1.15)
    sleeve_budget = round(equity * cap_pct, 2)
    result["cap_pct"] = round(cap_pct, 4)
    result["vti_scale"] = round(vti_scale, 3)
    result["sleeve_budget"] = sleeve_budget

    held = _sector_positions(executor)
    open_val = round(sum(_position_value(p) for p in held.values()), 2)
    cur_w = _current_weights(held, open_val if open_val > 0 else sleeve_budget)

    state = _load_state()
    book_key = "live" if live else "paper"
    books = state.get("books") if isinstance(state.get("books"), dict) else {}
    book_state = books.get(book_key) if isinstance(books.get(book_key), dict) else {}

    do_reb, reason = should_rebalance(
        book_state,
        regime=regime,
        force=force,
        current_weights=cur_w,
        target_weights=targets,
    )
    result["rebalance_reason"] = reason
    if not do_reb:
        result["skipped"] = "hold"
        result["open_value"] = open_val
        return result

    if not targets:
        # Flatten sleeve to cash.
        actions = []
        for sym, pos in held.items():
            n = round(_position_value(pos), 2)
            if n < config.effective_min_notional(equity):
                continue
            try:
                order = executor.execute_full_exit(sym, reason="sector_rot_flat", sleeve=_SLEEVE)
                ok = bool(order) and (
                    executor.order_filled(order) if hasattr(executor, "order_filled") else True
                )
            except Exception as exc:
                logger.warning("sector rotation exit %s failed: %s", sym, exc)
                ok = False
            actions.append({"action": "sell", "symbol": sym, "notional": n, "ok": ok})
        result["actions"] = actions
        book_state.update(
            {
                "last_rebalance_month": _month_key(),
                "last_regime": regime,
                "targets": {},
                "updated_at": _now_iso(),
            }
        )
        books[book_key] = book_state
        state["books"] = books
        state["last_plan"] = plan
        _save_state(state)
        return result

    # Dollar targets within sleeve budget.
    dollar_targets = {etf: round(sleeve_budget * w, 2) for etf, w in targets.items()}
    min_n = config.effective_min_notional(equity)
    actions: list[dict[str, Any]] = []

    # 1) Sell names not in target or overweight.
    for sym, pos in list(held.items()):
        current = _position_value(pos)
        target_n = float(dollar_targets.get(sym, 0.0))
        if sym not in dollar_targets:
            sell_n = current
        elif current > target_n + max(min_n, sleeve_budget * 0.02):
            sell_n = current - target_n
        else:
            continue
        sell_n = round(sell_n, 2)
        if sell_n < min_n:
            continue
        try:
            if sell_n >= current * 0.92:
                order = executor.execute_full_exit(sym, reason="sector_rot_trim", sleeve=_SLEEVE)
            else:
                order = executor.execute_reduce_notional(
                    sym, sell_n, reason="sector_rot_trim", sleeve=_SLEEVE
                )
            ok = bool(order) and (
                executor.order_filled(order) if hasattr(executor, "order_filled") else True
            )
        except Exception as exc:
            logger.warning("sector rotation trim %s failed: %s", sym, exc)
            ok = False
            order = None
        actions.append({"action": "sell", "symbol": sym, "notional": sell_n, "ok": ok})
        if ok:
            open_val = max(0.0, open_val - sell_n)
            cash += sell_n

    # Refresh held after sells.
    held = _sector_positions(executor)
    open_val = round(sum(_position_value(p) for p in held.values()), 2)

    # 2) Buy underweights with conviction scaling.
    for etf, target_n in sorted(dollar_targets.items(), key=lambda kv: -kv[1]):
        current = _position_value(held[etf]) if etf in held else 0.0
        need = round(target_n - current, 2)
        if need < min_n:
            continue
        need = min(need, cash * 0.95, max(0.0, sleeve_budget - open_val))
        if need < min_n:
            continue

        conv = 0.55
        need, conv = _conviction_adjust(etf, data, regime, need)

        need = min(need, cash * 0.95)
        if need < min_n:
            continue
        try:
            order = executor.execute_order(
                etf,
                "buy",
                notional=need,
                reason=f"sector_rot_{plan.get('reason')}",
                sleeve=_SLEEVE,
            )
            ok = bool(order) and (
                executor.order_filled(order) if hasattr(executor, "order_filled") else True
            )
        except Exception as exc:
            logger.warning("sector rotation buy %s failed: %s", etf, exc)
            ok = False
        actions.append(
            {
                "action": "buy",
                "symbol": etf,
                "notional": need,
                "ok": ok,
                "conviction": round(float(conv), 3),
                "target_pct": targets.get(etf),
            }
        )
        if ok:
            open_val += need
            cash = max(0.0, cash - need)
            if journal and hasattr(journal, "log_signal"):
                try:
                    journal.log_signal(
                        etf, "buy", regime, f"sector_rot:{etf}", float(conv), equity, need
                    )
                except Exception as exc:
                    logger.debug("sector rotation soft-fail: %s", exc)

    book_state.update(
        {
            "last_rebalance_month": _month_key(),
            "last_regime": regime,
            "targets": targets,
            "cap_pct": cap_pct,
            "updated_at": _now_iso(),
            "reason": reason,
        }
    )
    books[book_key] = book_state
    state["books"] = books
    state["last_plan"] = {
        "targets": targets,
        "regime_score": plan.get("regime_score"),
        "top": plan.get("top"),
        "as_of": _now_iso(),
    }
    _save_state(state)
    result["actions"] = actions
    result["open_value"] = open_val
    return result


def run_sector_rotation_backtest_day(
    window,
    executor,
    regime: str,
    i: int,
    pair_cooldown: dict | None = None,
    *,
    cooldown_bars: int = 3,
    volatility=None,
) -> int:
    """One backtest bar: monthly / regime-change rebalance of top sector SPDRs."""
    del pair_cooldown, cooldown_bars, volatility
    if not effective_sector_rotation_enabled():
        return 0
    if not bool(getattr(config, "SECTOR_ROTATION_BACKTEST_ENABLED", True)):
        return 0
    if "RHYME_B" in str(regime or "").upper():
        return 0
    if window is None or getattr(window, "empty", True) or i < 20:
        return 0

    try:
        as_of = window.index[i].to_pydatetime()
        if getattr(as_of, "tzinfo", None) is None:
            as_of = as_of.replace(tzinfo=_ET)
    except Exception:
        as_of = _now_et()

    data = window.iloc[: i + 1]
    plan = build_sector_targets(data, regime=regime)
    targets = plan.get("targets") or {}

    state = getattr(executor, "_sector_rot_bt_state", None)
    if not isinstance(state, dict):
        state = {}
        executor._sector_rot_bt_state = state

    held = _sector_positions(executor)
    try:
        account = executor._get_account()
        equity = float(account.equity)
        cash = float(account.cash)
    except Exception:
        return 0

    open_val = round(sum(_position_value(p) for p in held.values()), 2)
    cur_w = _current_weights(held, open_val if open_val > 0 else 1.0)
    do_reb, reason = should_rebalance(
        state,
        regime=regime,
        current_weights=cur_w,
        target_weights=targets,
        as_of=as_of,
    )
    if not do_reb:
        return 0

    trades = 0
    vti_scale = _vti_scale(equity, executor, regime=regime, data=data)
    regime_boost = 0.85 + 0.30 * float(plan.get("regime_score") or 0.5)
    cap_pct = _sleeve_cap_pct(live=False) * vti_scale * _clamp(regime_boost, 0.7, 1.2)
    cap_pct = min(cap_pct, _sleeve_cap_pct(live=False) * 1.15)
    sleeve_budget = round(equity * cap_pct, 2)
    min_n = config.effective_min_notional(equity)

    # Flatten / trim names not in target set.
    for sym, pos in list(held.items()):
        current = _position_value(pos)
        target_n = float((targets or {}).get(sym, 0.0)) * sleeve_budget
        if sym not in targets:
            sell_n = current
        elif current > target_n + max(min_n, sleeve_budget * 0.02):
            sell_n = current - target_n
        else:
            continue
        sell_n = round(sell_n, 2)
        if sell_n < min_n:
            continue
        try:
            if sell_n >= current * 0.92:
                order = executor.execute_full_exit(sym, reason=f"sector_rot_{reason}", sleeve=_SLEEVE)
            else:
                order = executor.execute_reduce_notional(
                    sym, sell_n, reason=f"sector_rot_{reason}", sleeve=_SLEEVE
                )
            ok = bool(order) and (
                executor.order_filled(order) if hasattr(executor, "order_filled") else True
            )
        except Exception:
            ok = False
        if ok:
            trades += 1
            cash += sell_n
            open_val = max(0.0, open_val - sell_n)

    held = _sector_positions(executor)
    open_val = round(sum(_position_value(p) for p in held.values()), 2)
    dollar_targets = {etf: round(sleeve_budget * w, 2) for etf, w in targets.items()}

    for etf, target_n in sorted(dollar_targets.items(), key=lambda kv: -kv[1]):
        current = _position_value(held[etf]) if etf in held else 0.0
        need = round(target_n - current, 2)
        if need < min_n:
            continue
        need = min(need, cash * 0.95, max(0.0, sleeve_budget - open_val))
        need, _conv = _conviction_adjust(etf, data, regime, need)
        need = min(need, cash * 0.95)
        if need < min_n:
            continue
        if etf not in data.columns:
            continue
        try:
            order = executor.execute_order(
                etf,
                "buy",
                notional=need,
                reason=f"sector_rot_{reason}",
                sleeve=_SLEEVE,
            )
            ok = bool(order) and (
                executor.order_filled(order) if hasattr(executor, "order_filled") else True
            )
        except Exception:
            ok = False
        if ok:
            trades += 1
            open_val += need
            cash = max(0.0, cash - need)

    state.update(
        {
            "last_rebalance_month": _month_key(as_of),
            "last_regime": regime,
            "targets": targets,
            "reason": reason,
            "bar": i,
        }
    )
    executor._sector_rot_bt_state = state
    return trades


def sector_rotation_dashboard_rows(data=None, *, limit: int = 12) -> list[dict[str, str]]:
    """Dashboard rows: sector strength + target allocation."""
    if data is None:
        try:
            from modules.pipeline_strategies import load_pipeline_data

            data = load_pipeline_data()
        except Exception:
            data = None
    if data is None:
        return []

    plan = build_sector_targets(data)
    state = _load_state()
    books = state.get("books") if isinstance(state.get("books"), dict) else {}
    paper = (books.get("paper") or {}).get("targets") or {}
    live = (books.get("live") or {}).get("targets") or {}

    rows: list[dict[str, str]] = []
    for r in (plan.get("rows") or [])[:limit]:
        etf = str(r.get("etf") or "")
        tw = float(r.get("target_weight") or 0)
        active = paper.get(etf) or live.get(etf)
        status = "TARGET" if tw > 0 else ("HELD" if active else "—")
        if tw > 0:
            status = "ROTATE IN"
        rs = float(r.get("rs_vs_spy") or 0)
        rows.append(
            {
                "Sector": str(r.get("label") or etf),
                "ETF": etf,
                "Score": f"{float(r.get('score') or 0):+.2%}",
                "RS vs SPY": f"{rs:+.2%}",
                "Target": f"{tw:.0%}" if tw > 0 else "—",
                "Status": status,
                "_tag": "orb_up" if tw > 0 else "",
            }
        )
    return rows


def format_sector_rotation_banner() -> str | None:
    if not effective_sector_rotation_enabled():
        return ">>> Sector Rotation: OFF"
    live = "LIVE opt-in ON" if sector_rotation_live_enabled() else "paper-only"
    return (
        f">>> Sector Rotation: ON (top {_top_n()}, max/sector {_max_sector_pct():.0%}, "
        f"cap {_sleeve_cap_pct():.0%}, monthly/regime rebalance, {live}) <<<"
    )


def format_weekly_sector_rotation_note(data=None) -> str:
    if not effective_sector_rotation_enabled():
        return ""
    try:
        if data is None:
            from modules.pipeline_strategies import load_pipeline_data

            data = load_pipeline_data()
        plan = build_sector_targets(data)
        tops = plan.get("top") or []
        if not tops:
            return "Sector rotation: ON (no leaders)"
        bits = []
        targets = plan.get("targets") or {}
        labels = {e: SECTOR_ETF_DEFS.get(e, (e,))[0] for e in tops}
        for etf in tops[:3]:
            bits.append(f"{labels.get(etf, etf)} {targets.get(etf, 0):.0%}")
        return (
            f"Sector rotation: ON | top {_top_n()} leaders {', '.join(bits)} | "
            f"max/sector {_max_sector_pct():.0%} | "
            f"regime {float(plan.get('regime_score') or 0):.2f}"
        )
    except Exception as exc:
        logger.debug("sector rotation weekly note failed: %s", exc)
        return "Sector rotation: ON"


def format_telegram_weekly_sector_rotation_block(data=None) -> str:
    note = format_weekly_sector_rotation_note(data)
    if not note:
        return ""
    return f"\n\n{note}"
