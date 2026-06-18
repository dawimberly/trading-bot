"""At-a-glance bot status: live + paper equity, regime, safety, thinking.

Run:
  python status.py
  python status.py --positions
  python status.py --positions --ticker AAPL
  python scripts/cleanup_stale_positions.py --help
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

import config
from modules.logging_utils import setup_project_logging

ROOT = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)
LIVE_HEARTBEAT = Path(os.getenv("HEARTBEAT_FILE", config.HEARTBEAT_FILE))
PAPER_HEARTBEAT = Path(os.getenv("PAPER_CHASE_HEARTBEAT", "paper_chase_heartbeat.json"))


def _load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _fmt_equity(val: float | None) -> str:
    if val is None:
        return "n/a"
    return f"${val:,.2f}"


def _heartbeat_equity(hb: dict | None) -> float | None:
    if not hb:
        return None
    try:
        return float(hb.get("equity"))
    except (TypeError, ValueError):
        return None


def _heartbeat_regime(hb: dict | None) -> str | None:
    if not hb:
        return None
    regime = hb.get("regime")
    return str(regime) if regime else None


def _heartbeat_ts(hb: dict | None) -> str:
    if not hb or not hb.get("timestamp"):
        return "n/a"
    return str(hb["timestamp"])[:19]


def _heartbeat_age_minutes(hb: dict | None) -> float | None:
    if not hb or not hb.get("timestamp"):
        return None
    try:
        ts = datetime.fromisoformat(str(hb["timestamp"]).replace("Z", "+00:00"))
        age = (datetime.now(ts.tzinfo) - ts).total_seconds() / 60.0
        return max(0.0, age)
    except (TypeError, ValueError):
        return None


def _heartbeat_scan_phase(hb: dict | None) -> str:
    scan = (hb or {}).get("scan_schedule") or {}
    return str(scan.get("phase") or scan.get("label") or "").strip()


def _alpaca_equity(*, paper: bool, credentials_fn=None) -> tuple[float | None, str | None]:
    from modules.alpaca_diagnostics import fetch_alpaca_equity

    return fetch_alpaca_equity(paper=paper, credentials_fn=credentials_fn)


def _paper_research_equity() -> tuple[float | None, str | None]:
    try:
        from modules.social_sleeve import get_social_alpaca_credentials

        creds = get_social_alpaca_credentials()
        if not creds:
            return None, None
        return _alpaca_equity(paper=True, credentials_fn=lambda: creds)
    except Exception as exc:
        return None, str(exc)


def _has_alpaca_keys() -> bool:
    try:
        config.get_alpaca_credentials()
        return True
    except ValueError:
        return False


def _resolve_live_equity(hb: dict | None) -> tuple[float | None, str]:
    """Prefer Alpaca live equity; ignore stale heartbeat when keys are configured."""
    acct_eq, acct_err = _alpaca_equity(paper=False)
    if acct_eq is not None and acct_eq > 0:
        hb_eq = _heartbeat_equity(hb)
        if hb_eq is not None and abs(hb_eq - acct_eq) > 1.0:
            return acct_eq, (
                f"Alpaca live (bot heartbeat {_fmt_equity(hb_eq)} @ {_heartbeat_ts(hb)} is stale - restart bot to refresh)"
            )
        return acct_eq, "Alpaca live"

    if acct_err:
        return None, acct_err

    if _has_alpaca_keys():
        return None, "Alpaca live (fetch failed — check API keys / network)"

    hb_eq = _heartbeat_equity(hb)
    if hb_eq is not None:
        return hb_eq, f"bot heartbeat @ {_heartbeat_ts(hb)} (no Alpaca keys in .env)"
    return None, "n/a (no Alpaca keys; start bot or add APCA_* to .env)"


def _resolve_equity(
    hb: dict | None, *, paper: bool, research: bool = False
) -> tuple[float | None, str | None]:
    if not paper and not research:
        eq, _src = _resolve_live_equity(hb)
        return eq, None
    eq = _heartbeat_equity(hb)
    if eq is not None:
        return eq, None
    if research:
        return _paper_research_equity()
    live_paper = paper or config.PAPER_TRADING
    return _alpaca_equity(paper=live_paper)


def _flag(name: str, val: bool) -> str:
    return f"{name}={'on' if val else 'off'}"


def _is_live_book_active() -> bool:
    return not config.PAPER_TRADING and config.ALLOW_LIVE_TRADING


def _live_profile_line() -> str:
    vti = config.SMALL_ACCOUNT_VTI_CORE_PCT
    return (
        f"VTI ~{vti:.0%} | risk {config.SMALL_ACCOUNT_RISK_PER_TRADE:.0%} | "
        f"max ${config.SMALL_ACCOUNT_MAX_NOTIONAL:.0f}/order"
    )


def _crypto_sleeve_status(*, paper: bool = False) -> str:
    if paper:
        was = config.paper_aggressive_context()
        config.set_paper_aggressive_context(True)
        try:
            enabled = config.effective_crypto_enabled()
        finally:
            config.set_paper_aggressive_context(was)
    else:
        enabled = config.effective_crypto_enabled()
    return "ON" if enabled else "OFF (disabled)"


def _sync_paper_stack_for_status() -> None:
    """Match status display to Best Paper v2.2 + .env opt-in sleeves."""
    config.refresh_paper_new_markets_flags_from_env()
    try:
        config._load_best_paper_config().apply_best_paper_config()
    except ImportError:
        config.enforce_best_paper_stack()


def _paper_validated_defaults_line() -> str:
    """Validated Best Paper v2.2 policy line."""
    return config.get_best_paper_validated_defaults_line()


def _paper_env_override_note() -> str | None:
    """Call out .env overrides that differ from validated v2.2 defaults."""
    notes: list[str] = []
    if config.PAPER_INTERNATIONAL_SLEEVE_ENABLED:
        notes.append("ADR env override (research)")
    if config.PAPER_BOND_SLEEVE_ENABLED:
        notes.append("bond env override")
    if config.PAPER_SECTOR_ROTATION_ENABLED:
        notes.append("sector rotation env override (research)")
    if not config.PAPER_TECH_GUARD_ENABLED:
        notes.append("tech guard env override (off)")
    if not config.PAPER_THINKING_ENGINE_ENABLED:
        notes.append("thinking env override (off)")
    if not config.PAPER_DYNAMIC_UNIVERSE_ENABLED:
        notes.append("dynamic universe env override (off)")
    if config.PAPER_SCALING_STRATEGY_ENABLED:
        notes.append("scaling strategy env override")
    if config.PAPER_PATTERN_AWARENESS_ENABLED:
        notes.append("pattern awareness env override")
    return f"Overrides: {', '.join(notes)}" if notes else None


def _paper_new_markets_sleeve_line() -> str:
    """International ADR + bond sleeves (paper-only; never live Profile A)."""
    was_ctx = config.paper_aggressive_context()
    config.set_paper_aggressive_context(True)
    try:
        intl_on = config.effective_international_sleeve_enabled()
        bond_on = config.effective_bond_sleeve_enabled()
    finally:
        config.set_paper_aggressive_context(was_ctx)
    intl_cap = config.INTERNATIONAL_SLEEVE_CAP_PCT
    bond_cap = config.BOND_SLEEVE_CAP_PCT
    sym = config.BOND_SLEEVE_SYMBOL
    intl_env = config.PAPER_INTERNATIONAL_SLEEVE_ENABLED
    if intl_on:
        intl_s = f"ADR ON (research opt-in, cap {intl_cap:.0%}, USD ADRs)"
    elif intl_env:
        intl_s = "ADR env=true but inactive (needs paper chase / Profile B context)"
    else:
        intl_s = (
            "ADR off (365d: no Sharpe edge vs baseline — opt-in research only)"
        )
    if bond_on:
        bond_s = f"Bond ON ({sym} cap {bond_cap:.0%}, risk-off hedge)"
    else:
        bond_s = "Bond off (365d: flat return, modest DD help when on)"
    return f"{intl_s} | {bond_s}"


def _live_flags() -> str:
    parts = [
        _flag("dyn_vti", False),
        _flag("overlap", config.NYSE_OVERLAP_FILTER_ENABLED),
        _flag("chunk", config.ADAPTIVE_CHUNK_ENABLED),
        _flag("cofire", config.COFIRE_BUDGET_ENABLED),
        _flag("macro", config.MACRO_REGIME_ADAPTOR_ENABLED),
        _flag("social", config.SOCIAL_SLEEVE_ENABLED),
        _flag("spy_exit", config.SPY_EXIT_ON_MA_BREAK),
        "thinking=off (live locked)",
    ]
    return " | ".join(parts)


def _universe_line() -> str:
    try:
        from modules.dynamic_universe import screener_universe_meta

        meta = screener_universe_meta()
        if not meta.get("exists"):
            return "static (no screener file)"
        age = meta.get("age_days")
        age_s = f"{age:.1f}d old" if age is not None else "unknown age"
        return f"{meta.get('count', 0)} tickers | screener {age_s}"
    except Exception:
        return "n/a"


def _daily_loss_status(*, paper: bool) -> dict:
    try:
        from modules.trading_safety import get_daily_loss_status

        return get_daily_loss_status(paper=paper)
    except Exception:
        return {"tripped": False, "limit_pct": 0.0, "loss_pct": None}


def _safety_status_banner(*, live: bool) -> str:
    """One-line safety banner for live or paper book."""
    s = config.get_production_safety_summary()
    dl = _daily_loss_status(paper=not live)
    book = "LIVE" if live else "PAPER"
    limit = float(dl.get("limit_pct") or 0.0)

    if dl.get("tripped"):
        loss = dl.get("loss_pct")
        loss_s = f"{loss:.2f}%" if loss is not None else "?"
        state = f"CIRCUIT TRIPPED - loss {loss_s} >= {limit:.0f}% limit - entries blocked"
    else:
        loss = dl.get("loss_pct")
        if loss is not None:
            state = f"OK - daily breaker {limit:.0f}% (today {loss:+.2f}%, not tripped)"
        else:
            state = f"OK - daily breaker {limit:.0f}% (no session anchor yet)"

    thinking = config.get_thinking_safety_summary()
    if live:
        thinking_bit = (
            f"thinking off | tilt +/-{s['live_tilt_cap_pp']:.0f}% | "
            f"approval {'required' if thinking['manual_approval_live'] else 'off'}"
        )
    else:
        eng = "on" if thinking["paper_thinking_enabled"] else "off (opt-in)"
        thinking_bit = f"thinking {eng} | tilt +/-{s['max_sleeve_delta_pp']:.0f}%"

    breaker = "breaker on" if s["daily_loss_breaker_enabled"] else "breaker off"
    return f"SAFETY STATUS ({book}): {state} | {thinking_bit} | {breaker}"


def _thinking_effective_label() -> str:
    if not config.PAPER_THINKING_ENGINE_ENABLED:
        return "OFF (v2.2 default is ON — set PAPER_THINKING_ENGINE_ENABLED=true)"
    was_ctx = config.paper_aggressive_context()
    was_bt = config.backtest_paper_sleeves_context()
    config.set_paper_aggressive_context(True)
    config.set_backtest_paper_sleeves_context(True)
    try:
        if config.effective_thinking_engine_enabled():
            return "ON"
    finally:
        config.set_paper_aggressive_context(was_ctx)
        config.set_backtest_paper_sleeves_context(was_bt)
    if config.paper_chase_mode_enabled() or os.getenv("PAPER_CHASE_MODE", "").lower() in (
        "1",
        "true",
        "yes",
    ):
        return "ON when paper bot runs (restart run_paper_bot.py)"
    return "OFF (start paper bot with PAPER_CHASE_MODE)"


def _thinking_engine_monitor_lines(live_equity: float | None = None) -> list[str]:
    """Latest Thinking Engine decision + live apply preview."""
    try:
        from modules.thinking_engine import evaluate_live_apply_status

        mon = evaluate_live_apply_status(equity=live_equity)
    except Exception as exc:
        return [f"Thinking Engine Monitor: unavailable ({exc})"]

    lines = ["=== Thinking Engine Monitor ==="]
    ts = mon.get("timestamp")
    regime = mon.get("regime") or "n/a"
    if ts:
        lines.append(f"Last decision: {str(ts)[:19]} | regime {regime}")
    else:
        lines.append("Last decision: none (run paper bot or scripts/test_thinking_engine.py)")
        lines.append("Would apply on live: No — no decision on file")
        return lines

    narrative = str(mon.get("narrative") or "n/a")
    asymmetry = str(mon.get("asymmetry") or "n/a")
    lines.append(f"Narrative:          {narrative[:140]}")
    lines.append(f"Asymmetry:          {asymmetry[:140]}")
    lines.append(f"Recommended tilt:   {mon.get('recommended_tilt', 'n/a')}")
    lines.append(
        f"Confidence:         {mon.get('confidence_pct', 'n/a')} | "
        f"Validation: {mon.get('validation_label', 'n/a')}"
    )
    lines.append(f"Approval status:    {mon.get('approval_status', 'n/a')}")
    news_slot = mon.get("news_slot")
    if news_slot:
        lines.append(f"News slot:          {news_slot}")
        impact = mon.get("news_impact_score")
        if impact is not None:
            lines.append(f"News impact:        {float(impact):.2f}")
        ns = str(mon.get("news_summary") or "")[:140]
        if ns:
            lines.append(f"News digest:        {ns}")
    apply_label = mon.get("would_apply_label", "No")
    reason = str(mon.get("block_reason") or "").strip()
    if apply_label == "Yes":
        lines.append(f"Would apply on live: Yes — {reason}")
    else:
        lines.append(f"Would apply on live: No — {reason}")
    lines.append("Audit: thinking_engine_last.json | logs/thinking_engine.log")
    return lines


def _thinking_status_lines() -> list[str]:
    try:
        from modules.thinking_engine import get_thinking_status_snapshot

        snap = get_thinking_status_snapshot()
    except Exception:
        return ["Thinking: unavailable (import error)"]

    env = "ON" if snap["env_enabled"] else "OFF (set PAPER_THINKING_ENGINE_ENABLED=true)"
    eff = _thinking_effective_label()
    lines = [f"Engine: env {env} | effective {eff}"]

    ts = snap.get("last_timestamp")
    if ts:
        conf = snap.get("last_confidence")
        conf_s = f"{float(conf):.0%}" if conf is not None else "n/a"
        val = snap.get("validation_score")
        val_s = str(val) if val is not None else "n/a"
        regime = snap.get("last_regime") or "n/a"
        lines.append(
            f"Last run: {str(ts)[:19]} | regime {regime} | conf {conf_s} | validation {val_s}"
        )
        snip = snap.get("narrative_snip")
        if snip:
            lines.append(f"  {snip}")
        if snap.get("sector_view_snip"):
            lines.append(f"  sector: {snap['sector_view_snip']}")
        lines.append("  audit: logs/thinking_engine.log")
        if snap.get("manual_review_required") and _is_live_book_active():
            if snap.get("approved"):
                lines.append("  Live tilt: APPROVED (approval file matches last decision)")
            else:
                did = snap.get("pending_decision_id") or "?"
                lines.append(
                    f"  Live tilt: PENDING approval (decision {did}) - "
                    "run scripts/approve_thinking_tilt.py --show"
                )
        cached = _load_json(ROOT / config.THINKING_ENGINE_OUTPUT_FILE)
        if cached and cached.get("news_slot"):
            lines.append(f"  Last news slot: {cached.get('news_slot')}")
            ns = str(cached.get("news_summary") or "")[:120]
            if ns:
                lines.append(f"  News digest: {ns}")
    else:
        lines.append("Last run: none (thinking_engine_last.json not found)")

    return lines


def _profile_table_lines() -> list[str]:
    paper_label = config.get_best_paper_display_name()
    return [
        "=== Profiles ===",
        f"| | Live Profile A (~$300) | Paper {paper_label} |",
        "|--|------------------------|---------------------------|",
        f"| VTI core | {config.SMALL_ACCOUNT_VTI_CORE_PCT:.0%} (<$500) / 80% | dynamic 40-75% |",
        f"| Risk / order | {config.SMALL_ACCOUNT_RISK_PER_TRADE:.0%} / ${config.SMALL_ACCOUNT_MAX_NOTIONAL:.0f} max | dynamic 1-3% |",
        "| Crypto sleeve | OFF (disabled) | OFF (locked) |",
        "| Stat arb / vol / options | off | on (locked stack) |",
        "| Dynamic universe | off (static) | on (weekly screener, sticky) |",
        "| Tech guard | off (live) | on by default |",
        "| Sector rotation | off | off by default (opt-in) |",
        "| Thinking engine | off (live locked) | on (quality tilts) |",
        "| Conservative Top1 | off | on (spec 0.5% + -4% stop) |",
        "| Strict PIT (research) | n/a | on by default |",
        "| International ADR | off (live locked) | off by default (365d: no edge) |",
        "| Bond sleeve | off (live locked) | opt-in hedge (365d: flat return) |",
        "| Macro / social / risk parity | off | locked off |",
        f"| Stack version | Profile A | v2.2 locked |",
    ]


def _safety_table_lines() -> list[str]:
    s = config.get_production_safety_summary()
    live_dl = _daily_loss_status(paper=False)
    paper_dl = _daily_loss_status(paper=True)
    lines = [
        "=== Production safety (always on) ===",
        "| Guard | Live ($300) | Paper |",
        "|-------|-------------|-------|",
        f"| Daily loss circuit breaker | {s['daily_loss_limit_live_pct']:.0f}% -> pause entries + tilts | {s['daily_loss_limit_paper_pct']:.0f}% |",
        f"| Thinking max tilt / sleeve | +/-{s['live_tilt_cap_pp']:.0f}% | +/-{s['max_sleeve_delta_pp']:.0f}% |",
        f"| Thinking manual approval | {'required' if s['manual_approval_live'] else 'off'} | auto when engine on |",
        f"| Thinking engine default | off | {'on (v2.2)' if s['paper_thinking_enabled'] else 'off (override)'} |",
        f"| Daily loss breaker enabled | {'yes' if s['daily_loss_breaker_enabled'] else 'no'} | same |",
        f"| Today circuit (live) | "
        f"{'TRIPPED' if live_dl.get('tripped') else 'ok'} "
        f"(limit {live_dl.get('limit_pct', 0):.0f}%) | "
        f"{'TRIPPED' if paper_dl.get('tripped') else 'ok'} "
        f"(limit {paper_dl.get('limit_pct', 0):.0f}%) |",
    ]
    return lines


from modules.console_output import safe_print as _emit
from modules import status_metrics as sm
from modules.paper_journal import build_position_summary, format_positions_table


def _print_positions(*, paper: bool = True, ticker: str | None = None, compact: bool = False) -> None:
    rows, err = build_position_summary(paper=paper, ticker=ticker)
    title = "Paper open positions" if paper else "Live open positions"
    if compact and rows:
        _emit(f"=== {title} ({len(rows)}) ===")
        for row in rows[:8]:
            from modules.paper_journal import format_position_row

            _emit(f"  {format_position_row(row)}")
        if len(rows) > 8:
            _emit(f"  ... +{len(rows) - 8} more (python status.py --positions)")
        return
    if compact and err and not paper:
        _emit(f"=== {title} ===")
        _emit(f"  (unavailable: {err})")
        return
    for line in format_positions_table(rows, title=title, err=err):
        _emit(line)


def _live_positions_available() -> bool:
    """True when live Alpaca credentials can be queried."""
    if _is_live_book_active():
        return True
    rows, err = build_position_summary(paper=False)
    return err is None or bool(rows)


def _print_live_positions_summary(*, compact: bool = True) -> None:
    if not _live_positions_available():
        return
    _print_positions(paper=False, compact=compact)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Bot status: equity, regime, safety, positions")
    parser.add_argument(
        "--positions",
        action="store_true",
        help="Show open holdings (entry, unrealized P/L, days held) and exit",
    )
    parser.add_argument("--ticker", "-t", help="With --positions, filter to one ticker")
    args = parser.parse_args()

    setup_project_logging()
    if args.positions:
        _print_positions(paper=True, ticker=args.ticker, compact=False)
        if _is_live_book_active():
            _emit()
            _print_positions(paper=False, ticker=args.ticker, compact=False)
        return

    _sync_paper_stack_for_status()
    live_hb = _load_json(LIVE_HEARTBEAT if LIVE_HEARTBEAT.is_absolute() else ROOT / LIVE_HEARTBEAT)
    paper_hb = _load_json(ROOT / PAPER_HEARTBEAT)

    live_eq, live_eq_src = _resolve_live_equity(live_hb)
    paper_eq, paper_eq_err = _resolve_equity(paper_hb, paper=True)
    if paper_eq is None:
        paper_eq, paper_eq_err = _paper_research_equity()

    live_regime = _heartbeat_regime(live_hb) or "n/a"
    paper_regime = _heartbeat_regime(paper_hb) or "n/a"
    regime = live_regime if live_regime != "n/a" else paper_regime
    live_hb_age = _heartbeat_age_minutes(live_hb)
    paper_hb_age = _heartbeat_age_minutes(paper_hb)
    live_phase = _heartbeat_scan_phase(live_hb)
    paper_phase = _heartbeat_scan_phase(paper_hb)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode = "LIVE + PAPER" if _is_live_book_active() else "PAPER ONLY (.env)"
    inception = sm.status_inception_date()
    live_start, live_start_date, _ = sm.resolve_start_equity(
        paper_chase=False,
        live_only=True,
        inception=inception,
        env_override_key="STATUS_LIVE_START_EQUITY",
    )
    paper_start, paper_start_date, _ = sm.resolve_start_equity(
        paper_chase=True,
        live_only=False,
        inception=inception,
        env_override_key="STATUS_PAPER_START_EQUITY",
    )
    live_dl = _daily_loss_status(paper=False)
    paper_dl = _daily_loss_status(paper=True)

    _emit(f"PythonTrading status - {now} ({mode})")
    _emit("=" * 72)
    for line in sm.top_banner_lines(
        live_eq=live_eq,
        paper_eq=paper_eq,
        live_start=live_start,
        paper_start=paper_start,
        inception=inception,
        regime=regime,
        live_active=_is_live_book_active(),
        live_dl=live_dl,
        paper_dl=paper_dl,
        paper_hb=paper_hb,
        live_hb=live_hb,
    ):
        _emit(line)
    _emit(config.get_best_paper_final_lock_banner())
    for line in config.get_best_paper_v22_summary_lines():
        _emit(line)
    _emit("=" * 72)

    if _is_live_book_active():
        _emit(_safety_status_banner(live=True))
    else:
        _emit(_safety_status_banner(live=False))
    _emit("-" * 72)

    _emit(f"LIVE  Profile A (~$300)   equity {_fmt_equity(live_eq)}   regime {live_regime}")
    _emit(
        sm.fmt_since_start_line(
            current=live_eq,
            start=live_start,
            start_date=live_start_date,
            inception=inception,
        )
    )
    _emit(f"      {config.get_live_profile_defaults_line()}")
    _emit(f"      source: {live_eq_src}")
    _emit(f"      {_live_profile_line()}")
    _emit(f"      Crypto: {_crypto_sleeve_status()}")
    _emit(f"      flags: {_live_flags()}")
    _emit(f"      heartbeat: {_heartbeat_ts(live_hb)}")
    if live_hb_age is not None:
        stale = " STALE" if live_hb_age > 90 else ""
        _emit(f"      heartbeat age: {live_hb_age:.0f} min{stale}")
    if live_phase:
        _emit(f"      scan phase: {live_phase}")
    if live_hb and live_hb.get("last_cycle_error"):
        _emit(f"      last cycle error: {str(live_hb['last_cycle_error'])[:120]}")
    _emit()

    paper_on, paper_off = config.format_best_paper_status_lines()
    paper_label = config.get_best_paper_display_name()
    _emit(f"PAPER {paper_label}   equity {_fmt_equity(paper_eq)}   regime {paper_regime}")
    _emit(
        sm.fmt_since_start_line(
            current=paper_eq,
            start=paper_start,
            start_date=paper_start_date,
            inception=inception,
        )
    )
    _emit(f"      {config.get_best_paper_locked_header()}")
    if paper_eq_err:
        _emit(f"      source: {paper_eq_err}")
    _emit(f"      {_paper_validated_defaults_line()}")
    _emit(f"      {config.get_top1_conservative_blend_line()}")
    pit_on = config.STRICT_PIT_BACKTEST or config.backtest_strict_pit_context()
    _emit(f"      Strict PIT backtest: {'ON' if pit_on else 'off (set STRICT_PIT_BACKTEST=true)'}")
    override = _paper_env_override_note()
    if override:
        _emit(f"      {override}")
    _emit(f"      Crypto: {_crypto_sleeve_status(paper=True)}")
    _emit(f"      New markets: {_paper_new_markets_sleeve_line()}")
    _emit(f"      ON:  {paper_on}")
    _emit(f"      OFF (locked): {paper_off}")
    _emit(f"      universe: {_universe_line()}")
    _emit(f"      heartbeat: {_heartbeat_ts(paper_hb)}")
    if paper_hb_age is not None:
        stale = " STALE" if paper_hb_age > 90 else ""
        _emit(f"      heartbeat age: {paper_hb_age:.0f} min{stale}")
    if paper_phase:
        _emit(f"      scan phase: {paper_phase}")
    if paper_hb and paper_hb.get("last_cycle_error"):
        _emit(f"      last cycle error: {str(paper_hb['last_cycle_error'])[:120]}")
    _emit()
    _print_positions(paper=True, compact=True)
    if _live_positions_available():
        _emit()
        _print_live_positions_summary(compact=True)
    _emit()

    for line in _thinking_engine_monitor_lines(live_eq):
        _emit(line)
    _emit()

    _emit("THINKING (engine env)")
    for line in _thinking_status_lines():
        _emit(f"  {line}")
    _emit()

    for line in _profile_table_lines():
        _emit(line)
    _emit()
    for line in _safety_table_lines():
        _emit(line)
    _emit()
    _emit(
        "Monitor: bot_heartbeat.json | paper_chase_heartbeat.json | "
        "thinking_engine_last.json | trading_safety_state.json | logs/thinking_engine.log"
    )
    _emit()
    _emit("=== Tomorrow checklist ===")
    _emit("1. python status.py - banner (total, since start, regime, rotating insight) + ON/OFF stack")
    _emit("2. python status.py --positions  |  python journal.py --ticker AAPL")
    _emit("3. paper_chase_heartbeat.json timestamp fresh (<30 min if bot running)")
    _emit("4. thinking_engine_last.json - validation score, narrative, suggested_tilt")
    _emit("5. logs/thinking_engine.log - background refresh + tilt apply audit")
    _emit("6. trading_safety_state.json - daily loss breaker not tripped")
    _emit("7. Dust/stale paper positions: python scripts/cleanup_stale_positions.py --help")
    if _live_positions_available():
        _emit(
            "   Live dust/stale: python scripts/cleanup_live_stale_positions.py --dry-run"
        )
    _emit("8. Restart paper bot after config changes: python run_paper_bot.py")
    _emit()
    for line in config.get_best_paper_restart_lines():
        _emit(line)


if __name__ == "__main__":
    main()
