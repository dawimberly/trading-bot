from datetime import date, datetime
from zoneinfo import ZoneInfo

from modules import alerts, h1d_telegram_summary as s, periodic_summary as ps
from modules.h1d_trend_mix import STRATEGY_SYMBOLS, target_weights

ET = ZoneInfo("America/New_York")


def _data():
    return {
        "as_of": datetime(2026, 10, 26, 16, 1, tzinfo=ET), "book_label": "Live",
        "equity": 301.88, "day_pnl": 0.66, "day_pct": 0.22, "cash_pct": 0.1, "invested_pct": 99.9,
        "in_mix_pct": 98.7,
        "signal": {"risk_on": True, "close": 760.0, "sma": 724.0, "gap_pct": 4.97},
        "next_rebalance": date(2026, 11, 2),
        "positions": [{"symbol": "SSO", "weight_pct": 50.1, "today_pct": 0.7},
                      {"symbol": "XLK", "weight_pct": 4.2, "today_pct": 1.1},
                      {"symbol": "UNH", "weight_pct": 0.001, "today_pct": -1.0, "in_target": False}],
        "orders": s.classify_orders([
            {"symbol": "SSO", "side": "buy", "status": "filled", "filled_value": 150.0},
            {"symbol": "CRM", "side": "sell", "status": "filled", "filled_value": 20.0},
            {"symbol": "XLY", "side": "buy", "status": "rejected", "notional": 12.5},
        ], set(STRATEGY_SYMBOLS)),
        "health": {"errors_24h": 0, "halted": False, "hb_age_min": 2, "hb_time": "03:00 PM"},
    }


def test_format_clean():
    msg = s.format_h1d_summary(_data())
    for want in ("Live - H1D 50/50 mix", "$301.88", "+0.22%", "In mix:    98.7%", "RISK-ON (SSO)",
                 "+4.97%", "Mon Nov 02", "SSO", "H1D trades (1)", "Switch sells of old names: 1 fills, $20.00 across 1 names",
                 "Rejected/canceled (1)", "1 leftover dust (<0.05%): UNH", "Errors 24h: 0", "Heartbeat: 03:00 PM CT"):
        assert want in msg, want
    for banned in ("Regime", "RHYME", "NYSE", "Sharpe", "Health", "skip", "Logs:", ".csv", ".json"):
        assert banned not in msg, banned


def test_in_mix_and_rebalance():
    t = target_weights(True)
    assert s.in_mix_pct(dict(t), t) == 100.0
    assert abs(s.in_mix_pct({}, t)) < 1e-9
    assert s.in_mix_pct({"SSO": 0.5}, {}) is None
    assert s.next_rebalance_date(date(2026, 10, 26)) == date(2026, 11, 2)
    assert s.next_rebalance_date(date(2026, 12, 15)) == date(2027, 1, 4)


def test_heartbeat_and_errors(tmp_path):
    hb = tmp_path / "bot_heartbeat.json"
    hb.write_text('{"timestamp": "2026-10-26T14:50:00", "halted": false}', encoding="utf-8")
    (tmp_path / "bot.log").write_text(
        "2026-10-26 10:00:00 ERROR boom key=SECRET123\n2026-10-20 10:00:00 ERROR old\n", encoding="utf-8")
    h = s.local_health(hb, now=datetime(2026, 10, 26, 15, 0, tzinfo=ZoneInfo("America/Chicago")))
    assert h["errors_24h"] == 1 and round(h["hb_age_min"]) == 10
    assert "SECRET123" not in h["recent_errors"][0]


def test_live_hook_uses_h1d(monkeypatch):
    monkeypatch.setattr(ps.config, "h1d_trend_mix_exclusive", lambda: True, raising=False)
    monkeypatch.setattr(ps.config, "PAPER_TRADING", False, raising=False)
    monkeypatch.setattr(s, "build_h1d_summary", lambda broker=None: "CLEAN BODY")
    sent = {}
    monkeypatch.setattr(ps.alerts, "alerts_configured", lambda: True)
    monkeypatch.setattr(ps.alerts, "broadcast", lambda subj, body, **k: sent.update(subj=subj, body=body) or True)
    monkeypatch.setattr(ps, "_save_live_daily_ts", lambda d: None)
    monkeypatch.setattr(ps, "gather_live_daily_summary", lambda **k: (_ for _ in ()).throw(AssertionError("legacy")))
    assert ps.send_daily_live_summary(force=True)
    assert sent["body"] == "CLEAN BODY" and "Daily close summary" in sent["subj"]


def test_paper_hook_h1d_and_legacy(monkeypatch):
    sent = {}
    monkeypatch.setattr(alerts, "_daily_summary_due", lambda: True)
    monkeypatch.setattr(alerts, "broadcast", lambda subj, body, **k: sent.update(subj=subj, body=body) or True)
    monkeypatch.setattr(alerts, "_load_state", lambda: {})
    monkeypatch.setattr(alerts, "_save_state", lambda st: None)
    monkeypatch.setattr(s, "build_h1d_summary", lambda broker=None: "CLEAN BODY")
    monkeypatch.setattr(alerts.config, "h1d_trend_mix_exclusive", lambda: True, raising=False)
    alerts.maybe_daily_summary(1000.0, 10.0, "RHYME_D", False)
    assert sent["body"] == "CLEAN BODY"
    monkeypatch.setattr(alerts.config, "h1d_trend_mix_exclusive", lambda: False, raising=False)
    alerts.maybe_daily_summary(1000.0, 10.0, "RHYME_D", False)
    assert "Regime:" in sent["body"] and "Logs:" in sent["body"]


def test_fallback_when_h1d_unavailable(monkeypatch):
    sent = {}
    monkeypatch.setattr(alerts, "_daily_summary_due", lambda: True)
    monkeypatch.setattr(alerts, "broadcast", lambda subj, body, **k: sent.update(body=body) or True)
    monkeypatch.setattr(alerts, "_load_state", lambda: {})
    monkeypatch.setattr(alerts, "_save_state", lambda st: None)
    monkeypatch.setattr(s, "build_h1d_summary", lambda broker=None: None)
    monkeypatch.setattr(alerts.config, "h1d_trend_mix_exclusive", lambda: True, raising=False)
    alerts.maybe_daily_summary(1000.0, 10.0, "RHYME_D", False)
    assert "Regime:" in sent["body"]
