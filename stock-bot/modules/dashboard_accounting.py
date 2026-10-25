"""Read-only dashboard accounting. No orders, environment changes, or journal basis.

Anchors are from the owner's broker reconciliation, 2026-10-16 15:31 ET.
Dollar investment P&L is cash-flow adjusted; it is not a time-weighted return.
Unclassified transfers or unavailable activity pages make metrics unavailable.
"""
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
AUDITED_UNTIL = datetime(2026, 10, 16, 15, 31, tzinfo=ET)
BASELINES = {
    "alpaca_live": ("2026-06-08", Decimal("99.50"), Decimal("200")),
    "alpaca_paper_v2": ("2026-09-01", Decimal("100000"), Decimal("0")),
    "alpaca_paper": ("2026-06-08", Decimal("98226.46"), Decimal("0")),
}
FLOW_TYPES = "CSD,CSW,JNLC,JNLS,ACATC,ACATS"


def _number(value):
    out = Decimal(str(value))
    if not out.is_finite():
        raise ValueError("non-finite accounting value")
    return out


def _activity_pages(client, after, until):
    params = {"activity_types": FLOW_TYPES, "after": after.isoformat(),
              "until": until.isoformat(), "direction": "asc", "page_size": 100}
    seen = set()
    for _ in range(20):
        rows = client.get("/account/activities", params)
        if not isinstance(rows, list):
            raise ValueError("activity response is not a list")
        for row in rows:
            activity_id = row.get("id")
            if not activity_id or activity_id in seen:
                raise ValueError("missing or repeated activity identity")
            seen.add(activity_id)
            yield row
        if len(rows) < 100:
            return
        params["page_token"] = rows[-1]["id"]
    raise ValueError("activity pagination limit reached")


def _cash_transfer(row, boundary):
    kind = row.get("activity_type")
    if kind not in ("CSD", "CSW"):
        raise ValueError("unclassified cash/security journal or account transfer")
    status = str(row.get("status", "")).lower()
    if status in ("canceled", "cancelled"):
        return Decimal("0"), None
    if status not in ("executed", "correct"):
        raise ValueError("unverified transfer status")
    raw = row.get("transaction_time") or row.get("date")
    if not raw:
        raise ValueError("transfer time missing")
    raw = str(raw)
    if len(raw) == 10:
        day = datetime.fromisoformat(raw).date()
        if day <= boundary.astimezone(ET).date():
            raise ValueError("date-only transfer overlaps audited boundary")
        stamp = datetime.fromisoformat(raw).replace(tzinfo=ET)
    else:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("transfer timezone missing")
        if stamp <= boundary:
            raise ValueError("activity overlaps audited boundary")
    amount = _number(row["net_amount"])
    # Alpaca net_amount is signed. Never guess the sign of a withdrawal.
    if (kind == "CSD" and amount < 0) or (kind == "CSW" and amount > 0):
        raise ValueError("unexpected transfer sign")
    return amount, stamp


def account_report(client, account, *, username, book_id, now=None):
    now = now or datetime.now(ET)
    report = {"book_id": book_id, "as_of": now.isoformat(), "daily_pnl": None,
              "daily_pct": None, "investment_pnl": None,
              "reason": "broker accounting unavailable"}
    if username != "dawimberly" or book_id not in BASELINES:
        report["reason"] = "no verified broker baseline for this profile/book"
        return report
    if now.tzinfo is None or now < AUDITED_UNTIL:
        report["reason"] = "outside the audited time range"
        return report
    try:
        equity = _number(account.equity)
        previous = _number(account.last_equity)
        if previous <= 0:
            raise ValueError("previous official equity unavailable")
        day = now.astimezone(ET).date()
        calendar = client.get("/calendar", {"start": (day-timedelta(days=14)).isoformat(),
                                             "end": day.isoformat()})
        prior = [r for r in calendar if str(r["date"]) < day.isoformat()]
        if not prior:
            raise ValueError("previous session unavailable")
        session = max(prior, key=lambda r: str(r["date"]))
        close_raw = str(session["close"])
        if "T" in close_raw:
            close = datetime.fromisoformat(close_raw.replace("Z", "+00:00"))
            if close.tzinfo is None:
                close = close.replace(tzinfo=ET)
        else:
            close = datetime.fromisoformat(str(session["date"])+"T"+close_raw).replace(tzinfo=ET)
        # The audit proves no flows after the Oct 15 official close through its cutoff.
        audited_prior = datetime(2026, 10, 15, 16, tzinfo=ET)
        if close < audited_prior:
            raise ValueError("daily flow interval precedes reconciliation")
        new_flows = Decimal("0")
        daily_flows = Decimal("0")
        for row in _activity_pages(client, AUDITED_UNTIL, now):
            amount, stamp = _cash_transfer(row, AUDITED_UNTIL)
            new_flows += amount
            if stamp is not None and stamp > close:
                daily_flows += amount
        start_date, start_equity, audited_flows = BASELINES[book_id]
        pnl = equity - previous - daily_flows
        report.update(daily_pnl=float(pnl), daily_pct=float(pnl/previous*100),
                      investment_pnl=float(equity-start_equity-audited_flows-new_flows),
                      baseline_date=start_date, baseline_equity=float(start_equity),
                      net_external_flows=float(audited_flows+new_flows),
                      previous_close=close.isoformat(), reason=None)
    except Exception:
        # Never leak broker exceptions, credentials, or unclassified activity payloads.
        report["reason"] = "cash-flow/account reconciliation unavailable; inspect read-only audit"
    return report


def daily_text(report):
    if report.get("daily_pnl") is None:
        return "Daily account P&L: unavailable"
    return (f"Daily account P&L: ${report['daily_pnl']:+,.2f} "
            f"({report['daily_pct']:+.2f}%, vs prior official close)")


def investment_text(report):
    if report.get("investment_pnl") is None:
        return "Investment P&L: unavailable (broker reconciliation required)"
    return (f"Investment P&L since {report['baseline_date']}: "
            f"${report['investment_pnl']:+,.2f} (external cash flows excluded)")
