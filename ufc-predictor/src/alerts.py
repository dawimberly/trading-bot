"""Live alerting: high-edge singles, parlays, SHAP + MC risk summaries."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import requests

import config
from src.explainability import parse_explanation_json
from src.fight_brief import build_card_brief, build_fight_brief
from src.safe_io import read_json_file, write_json_atomic
from src.parlay_builder import build_parlays_for_card, leg_pick_label, threshold_context_for_alerts
from src.strategy import (
    StrategyConfig,
    build_model_only_parlay_candidates,
    extract_bet_candidates,
    kelly_stake,
    strategy_from_profile,
)

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _fighter_names(row: pd.Series) -> tuple[str, str]:
    f1 = str(row.get("fighter_1", row.get("fighter1", "Fighter 1")))
    f2 = str(row.get("fighter_2", row.get("fighter2", "Fighter 2")))
    return f1, f2


def _pick_edge(row: pd.Series) -> tuple[float | None, str | None]:
    pick = str(row.get("predicted_winner", ""))
    f1, f2 = _fighter_names(row)
    if pd.notna(row.get("best_edge")):
        return float(row["best_edge"]), pick
    if pick == f1 and pd.notna(row.get("edge_f1")):
        return float(row["edge_f1"]), pick
    if pick == f2 and pd.notna(row.get("edge_f2")):
        return float(row["edge_f2"]), pick
    if pd.notna(row.get("edge_pct")):
        return float(row["edge_pct"]) / 100.0, pick
    return None, pick


def _short_reasoning(row: pd.Series, *, max_len: int = 140) -> str:
    text = ""
    if pd.notna(row.get("reasoning")):
        text = str(row["reasoning"])
    elif pd.notna(row.get("shap_explanation")):
        exp = parse_explanation_json(row.get("shap_explanation"))
        text = exp.get("reasoning", "")
        if not text and exp.get("available"):
            toward = exp.get("toward_pick") or exp.get("top_features") or []
            if toward:
                labels = [t.get("label", "") for t in toward[:2]]
                text = f"Driven by {', '.join(labels)}."
    if not text:
        conf = row.get("confidence_label", "")
        text = f"Model pick with {conf} confidence." if conf else "Model edge signal."
    if len(text) > max_len:
        return text[: max_len - 3].rstrip() + "..."
    return text


def _risk_blurb(risk_metrics: dict[str, Any] | None) -> str:
    if not risk_metrics or not risk_metrics.get("available"):
        return "MC risk: unavailable (run with odds)."
    cr = risk_metrics.get("card_pnl", {})
    cap = risk_metrics.get("suggested_max_risk_pct", risk_metrics.get("base_max_risk_pct", 8))
    return (
        f"MC card: mean PnL ${cr.get('mean_pnl', 0):+,.0f}, "
        f"P(loss) {cr.get('prob_loss', 0):.0%}, "
        f"max risk {cap:.1f}%"
    )


def _suggest_stake(
    row: pd.Series,
    *,
    bankroll: float,
    strategy: StrategyConfig,
    card_risk: dict[str, Any] | None,
) -> float:
    cand = extract_bet_candidates(row, config=strategy)
    if cand is None:
        return 0.0
    cap_frac = strategy.max_card_risk_fraction
    if card_risk and card_risk.get("available"):
        cap_frac = float(card_risk.get("suggested_max_risk_fraction", cap_frac))
    stake = kelly_stake(
        bankroll,
        prob=cand.prob,
        decimal_odds=cand.decimal_odds,
        edge=cand.edge,
        config=strategy,
    )
    return float(min(stake, bankroll * cap_frac))


def generate_alerts(
    predictions_df: pd.DataFrame,
    risk_metrics: dict[str, Any] | None = None,
    *,
    min_edge: float | None = None,
    min_parlay_ev: float | None = None,
    bankroll: float | None = None,
    strategy: StrategyConfig | None = None,
    event_name: str | None = None,
    use_dynamic_thresholds: bool | None = None,
    recent_wins: list[bool] | None = None,
) -> dict[str, Any]:
    """
    Build actionable alert payload: high-edge singles, qualified parlays, risk + reasoning.
    """
    bankroll = bankroll or config.INITIAL_BANKROLL
    ctx = threshold_context_for_alerts(
        predictions_df,
        bankroll=bankroll,
        recent_wins=recent_wins,
        use_dynamic=use_dynamic_thresholds,
    )
    min_edge = ctx["min_edge"] if min_edge is None else min_edge
    min_parlay_ev = ctx["min_parlay_ev"] if min_parlay_ev is None else min_parlay_ev
    strategy = strategy or ctx["strategy"]
    strategy.min_edge = min_edge

    if predictions_df.empty:
        return {
            "available": False,
            "reason": "No predictions to alert on.",
            "generated_at": _utc_now(),
            "singles": [],
            "parlays": [],
        }

    if risk_metrics is None:
        try:
            from src.risk_manager import assess_upcoming_card_risk

            risk_metrics = assess_upcoming_card_risk(
                predictions_df,
                bankroll=bankroll,
                simulations=min(config.MC_CARD_SIMULATIONS, 2000),
            )
        except Exception as exc:
            logger.debug("Card risk for alerts skipped: %s", exc)
            risk_metrics = {"available": False}

    ev_name = event_name
    if not ev_name:
        if "event_name" in predictions_df.columns and predictions_df["event_name"].notna().any():
            ev_name = str(predictions_df["event_name"].dropna().iloc[0])
        elif "event" in predictions_df.columns and predictions_df["event"].notna().any():
            ev_name = str(predictions_df["event"].dropna().iloc[0])
        else:
            ev_name = "Upcoming card"

    singles: list[dict[str, Any]] = []
    for _, row in predictions_df.iterrows():
        edge, pick = _pick_edge(row)
        if edge is None or edge < min_edge:
            continue
        f1, f2 = _fighter_names(row)
        prob = row.get("predicted_prob")
        if pd.isna(prob):
            prob = row.get("prob_f1_win") if pick == f1 else row.get("prob_f2_win")
        stake = _suggest_stake(row, bankroll=bankroll, strategy=strategy, card_risk=risk_metrics)
        edge_pct = edge * 100.0
        singles.append(
            {
                "fight_id": str(row.get(config.FIGHT_ID_COLUMN, f"{f1}|{f2}")),
                "fight": f"{f1} vs {f2}",
                "pick": pick,
                "prob": float(prob) if pd.notna(prob) else None,
                "edge": edge,
                "edge_pct": edge_pct,
                "reasoning": _short_reasoning(row),
                "brief": build_fight_brief(row, risk_metrics=risk_metrics, edge_pct=edge_pct),
                "suggested_stake": stake,
                "confidence": str(row.get("confidence_label", "")),
            }
        )

    singles.sort(key=lambda x: x["edge"], reverse=True)

    parlays: list[dict[str, Any]] = []
    groups = (
        predictions_df.groupby("event_name")
        if "event_name" in predictions_df.columns
        else [("card", predictions_df)]
    )
    card_cap = bankroll * float(
        risk_metrics.get("suggested_max_risk_fraction", strategy.max_card_risk_fraction)
        if risk_metrics and risk_metrics.get("available")
        else strategy.max_card_risk_fraction
    )
    parlay_stake_pool = card_cap * 0.25

    for _ev, grp in groups:
        parlay_candidates, _, _, grp_min_ev = build_parlays_for_card(
            grp,
            bankroll=bankroll,
            recent_wins=recent_wins,
            use_dynamic=use_dynamic_thresholds,
        )
        for p in parlay_candidates:
            if p.expected_value < (min_parlay_ev if min_parlay_ev is not None else grp_min_ev):
                continue
            legs_txt = " + ".join(leg_pick_label(c) for c in p.legs)
            parlays.append(
                {
                    "legs": [
                        {
                            "fight_id": c.fight_id,
                            "side": c.bet_side,
                            "edge": c.edge,
                            "prob": c.prob,
                            "odds": c.decimal_odds,
                            "fighter1_name": c.fighter1_name,
                            "fighter2_name": c.fighter2_name,
                            "pick_name": c.pick_name,
                            "winner_name": c.winner_name or c.pick_name,
                        }
                        for c in p.legs
                    ],
                    "n_legs": len(p.legs),
                    "combined_prob": p.combined_prob,
                    "combined_odds": p.combined_odds,
                    "expected_value": p.expected_value,
                    "picks": legs_txt,
                    "suggested_stake": round(parlay_stake_pool / max(len(parlay_candidates), 1), 2),
                }
            )
    parlays.sort(key=lambda x: x["expected_value"], reverse=True)
    parlays = parlays[: config.ALERT_MAX_PARLAYS]

    model_parlays: list[dict[str, Any]] = []
    if not config.is_live_profile():
        for ev_key, grp in groups:
            model_parlays.extend(
                build_model_only_parlay_candidates(
                    grp,
                    min_pick_prob=0.52,
                    parlay_max_legs=strategy.parlay_max_legs,
                    parlay_min_combined_prob=strategy.parlay_min_combined_prob,
                )
            )
        model_parlays.sort(key=lambda x: x["combined_prob"], reverse=True)
        model_parlays = model_parlays[: config.ALERT_MAX_PARLAYS]

    warnings: list[str] = []
    if risk_metrics and risk_metrics.get("warnings"):
        warnings.extend(risk_metrics["warnings"][:3])
    if not singles and not parlays:
        warnings.append(f"No bets cleared min edge {min_edge:.0%} or parlay EV {min_parlay_ev:.0%}.")

    payload = {
        "available": bool(singles or parlays),
        "event_name": ev_name,
        "generated_at": _utc_now(),
        "profile": config.UFC_PROFILE,
        "min_edge": min_edge,
        "min_parlay_ev": min_parlay_ev,
        "bankroll": bankroll,
        "dynamic_thresholds": ctx.get("use_dynamic", False),
        "threshold_detail": ctx.get("thresholds"),
        "risk_summary": _risk_blurb(risk_metrics),
        "risk_metrics": risk_metrics,
        "singles": singles,
        "parlays": parlays,
        "model_parlays": model_parlays,
        "warnings": warnings,
        "singles_count": len(singles),
        "parlays_count": len(parlays),
        "model_parlays_count": len(model_parlays),
    }
    payload["card_brief"] = build_card_brief(payload)
    return payload


def format_alert_text(alert_data: dict[str, Any]) -> str:
    """Rich plain-text alert for console / logs."""
    lines = [
        "",
        "=" * 60,
        f"UFC VALUE ALERT — {alert_data.get('event_name', 'Card')}",
        f"Generated {alert_data.get('generated_at', '')}",
        "=" * 60,
        alert_data.get("risk_summary", ""),
        "",
    ]
    if alert_data.get("singles"):
        lines.append(f"SINGLES ({len(alert_data['singles'])})")
        lines.append("-" * 40)
        for s in alert_data["singles"]:
            prob = f"{s['prob']:.0%}" if s.get("prob") is not None else "—"
            brief = s.get("brief") or s.get("reasoning", "")
            lines.append(
                f"• {s['fight']}\n"
                f"  Pick: {s['pick']} | Model {prob} | Edge {s['edge_pct']:+.1f}% | "
                f"Stake ${s['suggested_stake']:.2f}\n"
                f"  {brief}"
            )
        lines.append("")

    if alert_data.get("parlays"):
        lines.append(f"PARLAYS ({len(alert_data['parlays'])})")
        lines.append("-" * 40)
        for p in alert_data["parlays"]:
            lines.append(
                f"• {p['n_legs']}-leg @ {p['combined_odds']:.2f} | "
                f"Prob {p['combined_prob']:.0%} | EV {p['expected_value']:+.2f} | "
                f"Stake ${p['suggested_stake']:.2f}\n"
                f"  {p['picks']}"
            )
        lines.append("")

    for w in alert_data.get("warnings", []):
        lines.append(f"⚠ {w}")
    lines.append("")
    return "\n".join(lines)


def alert_fingerprint(alert_data: dict[str, Any]) -> str:
    """Stable hash for dedup / watch-mode new-alert detection."""
    payload = {
        "event": alert_data.get("event_name"),
        "singles": [
            (s["fight_id"], round(s["edge"], 4), s["pick"]) for s in alert_data.get("singles", [])
        ],
        "parlays": [
            (p["n_legs"], round(p["expected_value"], 4), p["picks"])
            for p in alert_data.get("parlays", [])
        ],
    }
    raw = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _load_alert_state() -> dict[str, Any]:
    state = read_json_file(config.ALERT_STATE_PATH)
    return state if state else {"events": {}}


def _save_alert_state(state: dict[str, Any]) -> None:
    write_json_atomic(config.ALERT_STATE_PATH, state)


def should_send_alert(
    alert_data: dict[str, Any],
    *,
    cooldown_minutes: int | None = None,
) -> tuple[bool, str]:
    """
    Enforce per-event cooldown and fingerprint dedup.

    Returns (ok_to_send, reason_if_blocked).
    """
    if not alert_data.get("available"):
        return False, "no actionable bets"

    bankroll = alert_data.get("bankroll")
    if bankroll is not None:
        try:
            from src.risk_manager import check_bankroll_safety

            allowed, block_reason = check_bankroll_safety(float(bankroll))
            if not allowed:
                return False, block_reason
        except Exception as exc:
            logger.debug("Safety check skipped: %s", exc)

    cooldown = cooldown_minutes or config.ALERT_COOLDOWN_MINUTES
    event = str(alert_data.get("event_name", "default"))
    fp = alert_fingerprint(alert_data)
    state = _load_alert_state()
    events = state.setdefault("events", {})
    rec = events.get(event, {})
    last_fp = rec.get("fingerprint")
    last_at = rec.get("sent_at")

    if last_fp == fp:
        return False, "duplicate fingerprint (same bets)"

    if last_at and cooldown > 0:
        try:
            last_dt = datetime.fromisoformat(str(last_at).replace("Z", "+00:00"))
            elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60.0
            if elapsed < cooldown:
                return False, f"cooldown ({cooldown - elapsed:.0f}m remaining)"
        except (ValueError, TypeError):
            pass

    return True, ""


def record_alert_sent(alert_data: dict[str, Any]) -> None:
    state = _load_alert_state()
    event = str(alert_data.get("event_name", "default"))
    state.setdefault("events", {})[event] = {
        "fingerprint": alert_fingerprint(alert_data),
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "singles_count": alert_data.get("singles_count", 0),
        "parlays_count": alert_data.get("parlays_count", 0),
    }
    _save_alert_state(state)


def send_discord_alert(
    alert_data: dict[str, Any],
    webhook_url: str,
    *,
    dry_run: bool = False,
) -> bool:
    """Post Discord webhook embed(s). Returns True on success."""
    if not webhook_url:
        logger.warning("Discord webhook URL not set.")
        return False
    if dry_run:
        logger.info("[DRY-RUN] Discord alert:\n%s", format_alert_text(alert_data))
        return True

    color = 0x3DDC84 if alert_data.get("available") else 0xF87171
    embeds: list[dict[str, Any]] = [
        {
            "title": f"🥊 {alert_data.get('event_name', 'UFC Card')}",
            "description": alert_data.get("card_brief") or alert_data.get("risk_summary", ""),
            "color": color,
            "fields": [
                {
                    "name": "Singles",
                    "value": str(alert_data.get("singles_count", 0)),
                    "inline": True,
                },
                {
                    "name": "Parlays",
                    "value": str(alert_data.get("parlays_count", 0)),
                    "inline": True,
                },
                {
                    "name": "Bankroll",
                    "value": f"${alert_data.get('bankroll', 0):,.0f}",
                    "inline": True,
                },
            ],
            "footer": {"text": alert_data.get("generated_at", "")},
        }
    ]

    for s in alert_data.get("singles", [])[:6]:
        prob = f"{s['prob']:.0%}" if s.get("prob") is not None else "—"
        embeds.append(
            {
                "title": f"✅ {s['pick']}",
                "description": s["fight"],
                "color": 0x60A5FA,
                "fields": [
                    {"name": "Edge", "value": f"{s['edge_pct']:+.1f}%", "inline": True},
                    {"name": "Model", "value": prob, "inline": True},
                    {"name": "Stake", "value": f"${s['suggested_stake']:.2f}", "inline": True},
                    {"name": "Why", "value": (s.get("brief") or s.get("reasoning", "—"))[:256]},
                ],
            }
        )

    for p in alert_data.get("parlays", [])[:3]:
        embeds.append(
            {
                "title": f"🎲 {p['n_legs']}-leg parlay",
                "description": p["picks"][:256],
                "color": 0xFBBF24,
                "fields": [
                    {"name": "Odds", "value": f"{p['combined_odds']:.2f}", "inline": True},
                    {"name": "Prob", "value": f"{p['combined_prob']:.0%}", "inline": True},
                    {"name": "EV", "value": f"{p['expected_value']:+.2f}", "inline": True},
                    {"name": "Stake", "value": f"${p['suggested_stake']:.2f}", "inline": True},
                ],
            }
        )

    if alert_data.get("warnings"):
        embeds.append(
            {
                "title": "⚠ Risk notes",
                "description": "\n".join(alert_data["warnings"])[:1024],
                "color": 0xF87171,
            }
        )

    try:
        resp = requests.post(
            webhook_url,
            json={"embeds": embeds[:10], "username": config.ALERT_BOT_NAME},
            timeout=config.ALERT_REQUEST_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.error("Discord alert failed: %s", exc)
        return False


def send_telegram_alert(
    alert_data: dict[str, Any],
    bot_token: str,
    chat_id: str,
    *,
    dry_run: bool = False,
) -> bool:
    """Send Telegram HTML message. Returns True on success."""
    if not bot_token or not chat_id:
        logger.warning("Telegram bot token or chat id not set.")
        return False

    text = _telegram_html(alert_data)
    if dry_run:
        logger.info("[DRY-RUN] Telegram alert:\n%s", text)
        return True

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=config.ALERT_REQUEST_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.error("Telegram alert failed: %s", exc)
        return False


def _telegram_html(alert_data: dict[str, Any]) -> str:
    lines = [
        f"<b>🥊 UFC VALUE ALERT</b>",
        f"<b>{_escape_html(alert_data.get('event_name', 'Card'))}</b>",
        f"<i>{alert_data.get('generated_at', '')}</i>",
        "",
        _escape_html(alert_data.get("risk_summary", "")),
        "",
    ]
    for s in alert_data.get("singles", [])[:8]:
        prob = f"{s['prob']:.0%}" if s.get("prob") is not None else "—"
        lines.append(
            f"<b>{_escape_html(s['pick'])}</b> — {_escape_html(s['fight'])}\n"
            f"Edge <b>{s['edge_pct']:+.1f}%</b> | Model {prob} | "
            f"Stake <b>${s['suggested_stake']:.2f}</b>\n"
            f"<i>{_escape_html(s.get('brief') or s.get('reasoning', ''))}</i>\n"
        )
    for p in alert_data.get("parlays", [])[:4]:
        lines.append(
            f"<b>Parlay ({p['n_legs']} legs)</b> @ {p['combined_odds']:.2f}\n"
            f"EV {p['expected_value']:+.2f} | Stake ${p['suggested_stake']:.2f}\n"
            f"{_escape_html(p['picks'])}\n"
        )
    for w in alert_data.get("warnings", [])[:3]:
        lines.append(f"⚠ {_escape_html(w)}")
    body = "\n".join(lines)
    if len(body) > 4000:
        body = body[:3990] + "\n…"
    return body


def _escape_html(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def dispatch_alerts(
    alert_data: dict[str, Any],
    *,
    discord: bool = False,
    telegram: bool = False,
    dry_run: bool = False,
    respect_cooldown: bool = True,
    discord_webhook: str | None = None,
    telegram_token: str | None = None,
    telegram_chat_id: str | None = None,
) -> dict[str, Any]:
    """
    Send alerts to configured channels with cooldown / dedup safety.

    Returns status dict with channels sent and skip reason.
    """
    result: dict[str, Any] = {
        "sent": False,
        "discord": False,
        "telegram": False,
        "dry_run": dry_run,
        "skipped": False,
        "skip_reason": "",
    }

    if respect_cooldown and not dry_run:
        ok, reason = should_send_alert(alert_data)
        if not ok:
            result["skipped"] = True
            result["skip_reason"] = reason
            logger.info("Alert skipped: %s", reason)
            return result

    if discord:
        result["discord"] = send_discord_alert(
            alert_data,
            discord_webhook or config.DISCORD_WEBHOOK,
            dry_run=dry_run,
        )
    if telegram:
        result["telegram"] = send_telegram_alert(
            alert_data,
            telegram_token or config.TELEGRAM_BOT_TOKEN,
            telegram_chat_id or config.TELEGRAM_CHAT_ID,
            dry_run=dry_run,
        )

    result["sent"] = result["discord"] or result["telegram"]
    if result["sent"] and not dry_run:
        record_alert_sent(alert_data)
        try:
            from src.bet_journal import log_alert_dispatch

            log_alert_dispatch(alert_data, status=result)
        except Exception as exc:
            logger.debug("Journal log skipped: %s", exc)
    return result
