#!/usr/bin/env python3
"""
Standalone UFC card analyzer — Rich CLI entry for ufc-predict.exe.

Usage:
    ufc-predict "Freedom 250"
    ufc-predict --event "UFC 303" --odds --explain --profile live
    ufc-predict --list-events
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --- Path bootstrap (must run before config-dependent imports) ---

_entry = Path(__file__).resolve()
_root = _entry.parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from src.project_paths import bootstrap

_PREDICTOR_ROOT = bootstrap(entry_file=_entry)

import pandas as pd
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

import config
from main import list_events, run_cli
from src.explainability import parse_explanation_json
from src.fight_brief import build_fight_brief
from src.logging_utils import setup_logging as setup_file_logging
from src.safe_io import install_safe_stdout
from src.strategy import StrategyConfig, extract_bet_candidates, kelly_stake, strategy_from_profile
from ufc_betting_bot.modules.edge import raw_kelly_fraction

console = Console()


def _truncate(text: str, max_len: int) -> str:
    text = str(text or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _pick_edge(row: pd.Series) -> float | None:
    pick = str(row.get("predicted_winner", ""))
    f1 = str(row.get("fighter_1", ""))
    if pd.notna(row.get("best_edge")):
        return float(row["best_edge"])
    if pick == f1 and pd.notna(row.get("edge_f1")):
        return float(row["edge_f1"])
    if pick != f1 and pd.notna(row.get("edge_f2")):
        return float(row["edge_f2"])
    if pd.notna(row.get("edge_pct")):
        return float(row["edge_pct"]) / 100.0
    return None


def _kelly_pct(row: pd.Series, strategy: StrategyConfig, bankroll: float) -> str:
    edge = _pick_edge(row)
    if edge is None:
        return "-"
    cand = extract_bet_candidates(row, config=strategy)
    if cand is None:
        return "-"
    stake = kelly_stake(
        bankroll,
        prob=cand.prob,
        decimal_odds=cand.decimal_odds,
        edge=cand.edge,
        config=strategy,
    )
    if stake <= 0:
        frac = raw_kelly_fraction(cand.prob, cand.decimal_odds) * strategy.kelly_fraction
        return f"{frac * 100:.2f}%" if frac > 0 else "-"
    return f"{(stake / bankroll) * 100:.2f}%"


def _shap_top_driver(row: pd.Series) -> str:
    if pd.notna(row.get("shap_explanation")):
        exp = parse_explanation_json(row.get("shap_explanation"))
        toward = exp.get("toward_pick") or exp.get("top_features") or []
        if toward:
            return str(toward[0].get("label", toward[0].get("feature", "")))
    return "-"


def _brief_reasoning(row: pd.Series, risk_metrics: dict[str, Any] | None) -> str:
    edge = _pick_edge(row)
    edge_pct = edge * 100.0 if edge is not None else None
    return build_fight_brief(row, risk_metrics=risk_metrics, edge_pct=edge_pct, max_len=72)


def _render_banner(profile: str) -> None:
    title = Text("UFC PREDICT", style="bold white on blue")
    subtitle = Text("Weekend card analyzer", style="dim")
    console.print(Panel(f"{title}\n{subtitle}", border_style="blue", padding=(0, 2)))


def _render_preflight(result: dict[str, Any]) -> None:
    if result.get("preflight_skipped"):
        console.print("[dim]Preflight skipped (--skip-preflight)[/dim]\n")
        return
    code = result.get("preflight_code", 1)
    lines = result.get("preflight_lines") or []
    style = "green" if code == 0 else "red"
    console.print(Panel("\n".join(lines) if lines else "Preflight complete.", title="Preflight", border_style=style))


def _model_prob(row: pd.Series) -> float | None:
    pick = str(row.get("predicted_winner", ""))
    f1 = str(row.get("fighter_1", ""))
    prob = row.get("predicted_prob", row.get("prob_f1_win"))
    if pd.isna(prob) and pick == f2 and pd.notna(row.get("prob_f2_win")):
        prob = row.get("prob_f2_win")
    if pd.isna(prob):
        return None
    return float(prob)


def _render_predictions_table(
    preds: pd.DataFrame,
    *,
    event_name: str,
    risk_metrics: dict[str, Any] | None,
    strategy: StrategyConfig,
    bankroll: float,
    explain: bool = False,
    compact: bool = True,
) -> None:
    console.print(f"\n[bold cyan]{event_name}[/bold cyan]  [dim]({len(preds)} bouts)[/dim]")

    table = Table(
        box=box.MINIMAL_DOUBLE_HEAD if compact else box.ROUNDED,
        show_header=True,
        header_style="bold magenta",
        pad_edge=False,
    )
    table.add_column("Fight", max_width=28)
    table.add_column("Pick", max_width=16, style="bold green")
    table.add_column("Model", justify="right", width=6)
    table.add_column("Edge", justify="right", width=7)
    if not compact or explain:
        table.add_column("Kelly", justify="right", width=6)
    if explain:
        table.add_column("SHAP", max_width=14)

    rows = []
    for _, row in preds.iterrows():
        edge = _pick_edge(row)
        rows.append((edge if edge is not None else -1.0, row))
    rows.sort(key=lambda x: x[0], reverse=True)

    for edge, row in rows:
        f1 = str(row.get("fighter_1", ""))
        f2 = str(row.get("fighter_2", ""))
        pick = str(row.get("predicted_winner", ""))
        prob = _model_prob(row)
        prob_txt = f"{prob:.0%}" if prob is not None else "-"
        edge_txt = f"{edge * 100:+.1f}%" if edge is not None else "-"
        edge_style = "green" if edge and edge >= strategy.min_edge else "dim"
        cells = [
            _truncate(f"{f1} vs {f2}", 28),
            _truncate(pick, 16),
            prob_txt,
            Text(edge_txt, style=edge_style),
        ]
        if not compact or explain:
            cells.append(_kelly_pct(row, strategy, bankroll))
        if explain:
            cells.append(_truncate(_shap_top_driver(row), 14))
        table.add_row(*cells)

    console.print(table)
    if "odds_matched" in preds.columns and config.ODDS_API_KEY:
        matched = int(preds.get("odds_matched", pd.Series(dtype=bool)).sum())
        console.print(f"[dim]Odds matched: {matched}/{len(preds)}[/dim]")


def _render_bets(alert_data: dict[str, Any], *, safety_blocked: bool, safety_reason: str) -> None:
    singles = alert_data.get("singles") or []
    parlays = alert_data.get("parlays") or []

    if safety_blocked:
        console.print(
            Panel(
                f"[bold red]Safety gate active[/bold red]\n{safety_reason}\n"
                "Singles and parlays withheld until gate clears.",
                title="Live profile",
                border_style="red",
            )
        )
        return

    if singles:
        top3 = singles[:3]
        table = Table(title="Top 3 Single Bets", box=box.SIMPLE_HEAVY, header_style="bold yellow")
        table.add_column("#", justify="right", width=3)
        table.add_column("Fight", min_width=24)
        table.add_column("Pick", min_width=14)
        table.add_column("Edge", justify="right", width=8)
        table.add_column("Stake", justify="right", width=10)
        table.add_column("Brief", min_width=32)
        for i, s in enumerate(top3, 1):
            table.add_row(
                str(i),
                _truncate(s.get("fight", ""), 32),
                str(s.get("pick", "")),
                f"{s.get('edge_pct', 0):+.1f}%",
                f"${s.get('suggested_stake', 0):.2f}",
                _truncate(s.get("brief") or s.get("reasoning", ""), 64),
            )
        console.print(table)
    else:
        console.print("[yellow]No singles cleared minimum edge.[/yellow]")

    qualified = [p for p in parlays if p.get("expected_value", 0) > 0]
    if qualified:
        ptable = Table(title="Qualified Same-Card Parlays", box=box.SIMPLE_HEAVY, header_style="bold cyan")
        ptable.add_column("#", justify="right", width=3)
        ptable.add_column("Legs", min_width=40)
        ptable.add_column("Odds", justify="right", width=8)
        ptable.add_column("Comb. Prob", justify="right", width=10)
        ptable.add_column("EV", justify="right", width=8)
        ptable.add_column("Stake", justify="right", width=10)
        for i, p in enumerate(qualified[:5], 1):
            ptable.add_row(
                str(i),
                _truncate(p.get("picks", ""), 56),
                f"{p.get('combined_odds', 0):.2f}",
                f"{p.get('combined_prob', 0):.0%}",
                f"{p.get('expected_value', 0):.2f}",
                f"${p.get('suggested_stake', 0):.2f}",
            )
        console.print(ptable)
    elif parlays:
        console.print("[dim]Parlays found but none met EV threshold.[/dim]")
    else:
        console.print("[dim]No qualified same-card parlays.[/dim]")

    model_parlays = alert_data.get("model_parlays") or []
    if model_parlays and not config.is_live_profile():
        mtable = Table(
            title="Top Model-Only Parlays (research, no odds required)",
            box=box.MINIMAL,
            header_style="bold blue",
        )
        mtable.add_column("#", justify="right", width=3)
        mtable.add_column("Legs", max_width=52)
        mtable.add_column("Comb. Model", justify="right", width=11)
        for i, p in enumerate(model_parlays[:5], 1):
            mtable.add_row(
                str(i),
                _truncate(p.get("picks", ""), 52),
                f"{p.get('combined_prob', 0):.0%}",
            )
        console.print(mtable)
        console.print("[dim]Model parlays rank by probability only — not +EV without market lines.[/dim]")


def _total_edge(preds: pd.DataFrame) -> float:
    total = 0.0
    for _, row in preds.iterrows():
        edge = _pick_edge(row)
        if edge is not None and edge > 0:
            total += edge
    return total


def _recommended_action(
    alert_data: dict[str, Any],
    *,
    safety_blocked: bool,
    safety_reason: str,
) -> str:
    if safety_blocked:
        return f"HOLD - {safety_reason}"
    n_s = len(alert_data.get("singles") or [])
    n_p = len(alert_data.get("parlays") or [])
    if n_s == 0 and n_p == 0:
        min_e = alert_data.get("min_edge", 0.05)
        return f"PASS - no bets cleared {min_e:.0%} min edge"
    parts = []
    if n_s:
        parts.append(f"{n_s} single{'s' if n_s != 1 else ''}")
    if n_p:
        parts.append(f"{n_p} parlay{'s' if n_p != 1 else ''}")
    return f"BET - {', '.join(parts)} within profile caps"


def _render_summary(
    result: dict[str, Any],
    preds: pd.DataFrame,
    alert_data: dict[str, Any],
) -> None:
    risk = alert_data.get("risk_metrics") or {}
    risk_txt = alert_data.get("risk_summary", "MC risk: unavailable")
    total = _total_edge(preds)
    action = _recommended_action(
        alert_data,
        safety_blocked=result.get("safety_blocked", False),
        safety_reason=result.get("safety_reason", ""),
    )

    lines = [
        f"[bold]Card risk (MC):[/bold] {risk_txt}",
        f"[bold]Total positive edge:[/bold] {total * 100:.1f}% (sum of per-fight edges)",
        f"[bold]Profile:[/bold] {result.get('profile', config.UFC_PROFILE)} "
        f"(singles {alert_data.get('min_edge', 0):.0%} / parlay EV {alert_data.get('min_parlay_ev', 0):.0%})",
        f"[bold]Recommended:[/bold] {action}",
    ]
    if alert_data.get("warnings"):
        for w in alert_data["warnings"][:3]:
            lines.append(f"[yellow]WARN {w}[/yellow]")
    console.print(Panel("\n".join(lines), title="Summary", border_style="green"))


def _render_analysis(result: dict[str, Any], *, explain: bool) -> None:
    """Render one or more cards with per-event sections."""
    strategy = strategy_from_profile()
    alert_data = result.get("alerts") or {}
    bankroll = float(alert_data.get("bankroll") or config.INITIAL_BANKROLL)
    compact = not config.is_live_profile()
    cards = result.get("cards") or []

    if not cards:
        cards = [
            {
                "event_name": result.get("event_name", "Card"),
                "predictions": result.get("predictions"),
                "alerts": alert_data,
            }
        ]

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    console.print(f"[dim]Generated {now}[/dim]")
    if len(cards) > 1:
        names = ", ".join(c.get("event_name", "") for c in cards)
        console.print(f"[bold]Analyzing {len(cards)} cards:[/bold] {names}\n")

    for i, card in enumerate(cards):
        if i > 0:
            console.rule(style="dim")
        ev = card.get("event_name", "Card")
        preds = card.get("predictions")
        if preds is None or preds.empty:
            console.print(f"[yellow]{ev}: no predictions[/yellow]")
            continue
        card_alerts = card.get("alerts") or {}
        risk = card_alerts.get("risk_metrics") or alert_data.get("risk_metrics")
        _render_predictions_table(
            preds,
            event_name=ev,
            risk_metrics=risk,
            strategy=strategy,
            bankroll=bankroll,
            explain=explain,
            compact=compact,
        )
        _render_bets(
            card_alerts,
            safety_blocked=result.get("safety_blocked", False),
            safety_reason=result.get("safety_reason", ""),
        )

    if len(cards) > 1:
        console.rule("[bold]Combined summary[/bold]")
    _render_summary(result, result.get("predictions", pd.DataFrame()), alert_data)


def _build_html_report(result: dict[str, Any]) -> str:
    preds = result["predictions"]
    alert_data = result["alerts"]
    event = result.get("event_name", "Card")
    rows_html = []
    for _, row in preds.iterrows():
        f1, f2 = str(row.get("fighter_1", "")), str(row.get("fighter_2", ""))
        pick = str(row.get("predicted_winner", ""))
        edge = _pick_edge(row)
        edge_txt = f"{edge * 100:+.1f}%" if edge is not None else "—"
        prob = row.get("predicted_prob", row.get("prob_f1_win"))
        prob_txt = f"{float(prob):.0%}" if pd.notna(prob) else "—"
        rows_html.append(
            f"<tr><td>{f1} vs {f2}</td><td>{pick}</td><td>{prob_txt}</td>"
            f"<td>{edge_txt}</td><td>{_shap_top_driver(row)}</td></tr>"
        )

    singles_html = "".join(
        f"<li><b>{s['fight']}</b> — {s['pick']} ({s.get('edge_pct', 0):+.1f}%) "
        f"${s.get('suggested_stake', 0):.2f}</li>"
        for s in (alert_data.get("singles") or [])[:3]
    )
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/>
<title>UFC Predict — {event}</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #0f1419; color: #e7e9ea; }}
h1 {{ color: #1d9bf0; }}
table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
th, td {{ border: 1px solid #38444d; padding: 0.5rem 0.75rem; text-align: left; }}
th {{ background: #192734; }}
.panel {{ background: #192734; padding: 1rem; border-radius: 8px; margin: 1rem 0; }}
</style></head><body>
<h1>{event}</h1>
<p>Generated {result.get('generated_at', '')} · Profile {result.get('profile', '')}</p>
<div class="panel"><b>Summary:</b> {alert_data.get('risk_summary', '')}</div>
<h2>Predictions</h2>
<table><thead><tr><th>Fight</th><th>Pick</th><th>Prob</th><th>Edge</th><th>SHAP</th></tr></thead>
<tbody>{''.join(rows_html)}</tbody></table>
<h2>Top Singles</h2><ul>{singles_html or '<li>None</li>'}</ul>
</body></html>"""


def _save_reports(result: dict[str, Any], base_path: Path | None) -> tuple[Path, Path]:
    reports_dir = config.DATA_DIR / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    slug = "".join(c if c.isalnum() else "_" for c in result.get("event_name", "card"))[:40]
    if base_path:
        json_path = base_path.with_suffix(".json") if base_path.suffix != ".json" else base_path
        html_path = json_path.with_suffix(".html")
    else:
        json_path = reports_dir / f"{slug}_{stamp}.json"
        html_path = reports_dir / f"{slug}_{stamp}.html"

    payload = {
        "event_name": result.get("event_name"),
        "generated_at": result.get("generated_at"),
        "profile": result.get("profile"),
        "preflight_code": result.get("preflight_code"),
        "safety_blocked": result.get("safety_blocked"),
        "safety_reason": result.get("safety_reason"),
        "alerts": result.get("alerts"),
        "risk_metrics": result.get("alerts", {}).get("risk_metrics"),
        "predictions": json.loads(
            result["predictions"].to_json(orient="records", date_format="iso")
        ),
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    html_path.write_text(_build_html_report(result), encoding="utf-8")
    return json_path, html_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ufc-predict",
        description="UFC card analyzer — predictions, odds edge, SHAP, MC risk.",
    )
    parser.add_argument(
        "event",
        nargs="*",
        default=None,
        help='Event name(s) (e.g. "Freedom 250"). Omit for next card(s).',
    )
    parser.add_argument("--event", dest="event_flag", action="append", metavar="NAME", help="Event name (repeatable)")
    parser.add_argument(
        "--next-two",
        action="store_true",
        help="Analyze the two soonest upcoming cards (closest event first)",
    )
    parser.add_argument(
        "--last-two",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--odds", action="store_true", help="Attach live odds and compute edge")
    parser.add_argument("--explain", action="store_true", help="SHAP explanations (slower)")
    parser.add_argument(
        "--profile",
        choices=["live", "research"],
        default=None,
        help="Risk profile (default: UFC_PROFILE env or research)",
    )
    parser.add_argument("--skip-preflight", action="store_true", help="Skip go-live checklist")
    parser.add_argument("--refresh-data", action="store_true", help="Refresh fight card cache")
    parser.add_argument("--list-events", action="store_true", help="List upcoming UFC events")
    parser.add_argument("-i", "--interactive", action="store_true", help="Prompt for event name")
    parser.add_argument("--json", metavar="PATH", help="Save JSON report to PATH")
    parser.add_argument(
        "--save-report",
        nargs="?",
        const="",
        metavar="PATH",
        help="Save JSON + HTML report (optional base path)",
    )
    parser.add_argument(
        "--backtest-2025",
        action="store_true",
        help="Run 2025 walk-forward backtest with static vs dynamic threshold comparison",
    )
    parser.add_argument(
        "--dynamic-thresholds",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Adjust min edge / parlay thresholds dynamically (default: on)",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Launch customtkinter desktop dashboard (GUI)",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Watch mode: poll for card/odds changes and alert on new value bets",
    )
    parser.add_argument(
        "--auto-odds",
        action="store_true",
        help="With --watch: quick BetNow + DraftKings odds refresh on a short interval",
    )
    parser.add_argument(
        "--poll-minutes",
        type=int,
        default=None,
        metavar="N",
        help="Watch poll interval in minutes (legacy; auto-odds uses card/odds timers)",
    )
    parser.add_argument("--discord", action="store_true", help="Send alerts to Discord webhook")
    parser.add_argument("--telegram", action="store_true", help="Send alerts to Telegram bot")
    parser.add_argument("--dry-run", action="store_true", help="Format alerts without sending")
    parser.add_argument("--alerts", action="store_true", help="Enable value-bet alert generation in watch mode")
    parser.add_argument("-q", "--quiet", action="store_true", help="Less console output")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    return parser


def main(argv: list[str] | None = None) -> int:
    install_safe_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)

    setup_file_logging(verbose=args.verbose, log_dir=config.LOG_DIR)

    profile = args.profile or os.getenv("UFC_PROFILE", "research")
    config.UFC_PROFILE = profile.strip().lower()
    config.apply_profile_overrides()
    config.DYNAMIC_THRESHOLDS_ENABLED = args.dynamic_thresholds

    if args.dashboard:
        from src.ufc_dashboard import main as dashboard_main
        return dashboard_main()

    if args.watch:
        from main import _model_exists
        from src.preflight import run_preflight
        from src.scheduler import watch_loop

        if not args.skip_preflight:
            pf = run_preflight(profile=profile)
            if pf != 0:
                return pf
        if not _model_exists():
            console.print("[bold red]No trained model. Run main.py --train first.[/bold red]")
            return 1
        if not args.quiet:
            _render_banner(profile)
        use_odds = args.odds or bool(config.ODDS_API_KEY)
        if args.auto_odds:
            console.print(
                f"[cyan]Auto odds enabled[/cyan] — card check every "
                f"{config.WATCH_CARD_CHECK_MINUTES}m, odds every {config.WATCH_AUTO_ODDS_MINUTES}m"
            )
        elif not use_odds:
            console.print("[yellow]No odds API key — enable --auto-odds for BetNow/DraftKings scrapers.[/yellow]")
        watch_loop(
            poll_minutes=args.poll_minutes,
            refresh_data=args.refresh_data,
            use_odds=use_odds and not args.auto_odds,
            explain=args.explain,
            discord=args.discord,
            telegram=args.telegram,
            dry_run=args.dry_run or config.ALERT_DRY_RUN,
            min_edge=config.profile_value("alert_min_edge"),
            auto_odds=args.auto_odds,
        )
        return 0

    if args.backtest_2025:
        from main import (
            _features_exist,
            _model_exists,
            build_or_load_features,
            load_or_refresh_data,
            load_or_train_model,
        )
        from src.backtester import backtest_2025, print_backtest_2025_summary
        from src.data_loader import load_processed_features
        from src.model_freshness import model_needs_retrain, stale_model_warning

        if not args.quiet:
            _render_banner(profile)
        if args.refresh_data or not _features_exist():
            fights = load_or_refresh_data(refresh=args.refresh_data)
            features = build_or_load_features(fights, refresh=True)
        else:
            features = load_processed_features()
        needs_train, train_reason = model_needs_retrain(force=False)
        if needs_train:
            console.print(f"[yellow]Auto-retrain: {train_reason}[/yellow]")
            load_or_train_model(features, train=True, tune="none")
        elif not _model_exists():
            console.print("[bold red]No trained model. Run main.py --train first.[/bold red]")
            return 1
        else:
            warn = stale_model_warning()
            if warn:
                console.print(f"[yellow]{warn}[/yellow]")
        try:
            result = backtest_2025(
                features,
                use_dynamic_thresholds=args.dynamic_thresholds,
                compare_threshold_modes=True,
                profile=config.UFC_PROFILE,
            )
            print_backtest_2025_summary(result)
        except (ValueError, FileNotFoundError) as exc:
            console.print(f"[bold red]{exc}[/bold red]")
            return 1
        return 0

    if args.list_events:
        _render_banner(profile)
        list_events()
        return 0

    event_query = args.event_flag or args.event
    if isinstance(event_query, list):
        event_query = [e for e in event_query if e] or None
    next_two = args.next_two or args.last_two
    if args.interactive or (not event_query and not next_two):
        _render_banner(profile)
        prompted = Prompt.ask(
            "Event name (blank = upcoming)",
            default="",
            show_default=False,
        ).strip()
        event_query = prompted or None

    use_odds = args.odds or bool(config.ODDS_API_KEY)
    explain = args.explain

    if not args.quiet:
        _render_banner(profile)

    run_event: str | list[str] | None
    if next_two:
        run_event = None
    elif isinstance(event_query, list) and len(event_query) == 1:
        run_event = event_query[0]
    elif isinstance(event_query, list) and len(event_query) > 1:
        run_event = event_query
    else:
        run_event = event_query

    try:
        result = run_cli(
            run_event,
            profile=profile,
            use_odds=use_odds,
            explain=explain,
            skip_preflight=args.skip_preflight,
            refresh_data=args.refresh_data,
            apply_safety_gates=config.is_live_profile(),
            next_two=next_two,
            include_adjacent_week=not next_two and not (
                isinstance(run_event, list) and len(run_event) > 1
            ),
            use_dynamic_thresholds=args.dynamic_thresholds,
        )
    except SystemExit as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        return int(exc.code) if exc.code else 1
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        return 130

    if result.get("error"):
        console.print(f"[bold red]{result['error']}[/bold red]")
        return 1

    if not args.quiet:
        _render_preflight(result)
        _render_analysis(result, explain=explain)

    save_base: Path | None = None
    if args.json:
        save_base = Path(args.json)
    elif args.save_report is not None:
        save_base = Path(args.save_report) if args.save_report else None

    if save_base is not None or args.save_report is not None:
        json_path, html_path = _save_reports(result, save_base)
        console.print(f"\n[green]Saved[/green] JSON → {json_path}")
        console.print(f"[green]Saved[/green] HTML → {html_path}")

    return 0 if result.get("preflight_code", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
