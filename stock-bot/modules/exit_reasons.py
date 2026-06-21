"""Normalize, log, and summarize sell exit reasons across backtest and live."""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

logger = logging.getLogger(__name__)

# Standard exit-reason codes (stable for logs, journal, HTML).
THINKING_TILT_REBALANCE = "thinking_tilt_rebalance"
YIELD_GATE = "yield_gate"
DRAWDOWN_HALT = "drawdown_halt"
DAILY_LOSS_LIMIT = "daily_loss_limit"
DYNAMIC_RISK = "dynamic_risk_reduction"
EOD_REBALANCE = "eod_rebalance"
LOSS_CUTTING = "loss_cutting"
LOSS_CUTTING_HARD = "loss_cutting_hard_stop"
LOSS_CUTTING_TRAIL = "loss_cutting_trail"
PROFIT_TARGET = "profit_target"
PROFIT_PROTECT = "profit_protect"
STAT_ARB_EXIT = "stat_arb_exit"
IPO_TRIM = "ipo_trim"
SLEEVE_LIQUIDATE = "sleeve_liquidate"
SLEEVE_TRIM = "sleeve_trim"
BOND_RISK_ON = "bond_risk_on_exit"
SCALING_TAKE = "scaling_take"
VTI_CORE_REBALANCE = "vti_core_rebalance"
HALT_LIQUIDATION = "halt_liquidation"
REDUCE = "reduce"
EXIT = "exit"
UNKNOWN = "unknown"

_LABELS: dict[str, str] = {
    THINKING_TILT_REBALANCE: "Thinking Engine tilt / sleeve rebalance",
    YIELD_GATE: "Yield gate (macro)",
    DRAWDOWN_HALT: "Drawdown halt (halt_resume_dd)",
    DAILY_LOSS_LIMIT: "Daily loss limit",
    DYNAMIC_RISK: "Dynamic risk reduction",
    EOD_REBALANCE: "End-of-day rebalance",
    LOSS_CUTTING: "Loss cutting",
    LOSS_CUTTING_HARD: "Loss cutting — hard stop",
    LOSS_CUTTING_TRAIL: "Loss cutting — trailing stop",
    PROFIT_TARGET: "Profit target / trailing stop",
    PROFIT_PROTECT: "Profit protect / winners trailing",
    STAT_ARB_EXIT: "Stat-arb / pair exit",
    IPO_TRIM: "IPO sleeve trim",
    SLEEVE_LIQUIDATE: "Sleeve liquidate (target zero)",
    SLEEVE_TRIM: "Sleeve trim (above target)",
    BOND_RISK_ON: "Bond sleeve risk-on exit",
    SCALING_TAKE: "Scaling strategy take-profit",
    VTI_CORE_REBALANCE: "VTI core rebalance",
    HALT_LIQUIDATION: "Halt breach liquidation",
    REDUCE: "Partial reduce",
    EXIT: "Full exit (unspecified)",
    UNKNOWN: "Unclassified sell",
}


def label_for(code: str) -> str:
    return _LABELS.get(code, _LABELS[UNKNOWN])


def normalize_exit_reason(raw: str, *, sleeve: str = "") -> tuple[str, str]:
    """Map free-text reason + sleeve to (code, human label)."""
    text = (raw or "").strip().lower()
    sleeve_l = (sleeve or "").strip().lower()

    if not text and not sleeve_l:
        return UNKNOWN, label_for(UNKNOWN)

    if "hard_stop" in text or text.startswith("hard_stop"):
        return LOSS_CUTTING_HARD, label_for(LOSS_CUTTING_HARD)
    if "conservative_trail" in text or "conviction_trail" in text:
        return LOSS_CUTTING_TRAIL, label_for(LOSS_CUTTING_TRAIL)
    if text.startswith("take_") or "scaling_take" in text:
        return SCALING_TAKE, label_for(SCALING_TAKE)
    if "profit_trailing" in text or "profit_target" in text:
        return PROFIT_TARGET, label_for(PROFIT_TARGET)
    if "profit_protect" in text or sleeve_l == "profit_protect":
        return PROFIT_PROTECT, label_for(PROFIT_PROTECT)
    if "loss_cut" in text or sleeve_l == "loss_cutting":
        return LOSS_CUTTING, label_for(LOSS_CUTTING)
    if "ipo_trim" in text:
        return IPO_TRIM, label_for(IPO_TRIM)
    if "bond_risk_on" in text:
        return BOND_RISK_ON, label_for(BOND_RISK_ON)
    if "liquidate sleeve" in text or text == "sleeve_liquidate":
        return SLEEVE_LIQUIDATE, label_for(SLEEVE_LIQUIDATE)
    if "above target" in text or text == "sleeve_trim_above_target":
        return SLEEVE_TRIM, label_for(SLEEVE_TRIM)
    if "vti_core" in text or "vti core" in text:
        return VTI_CORE_REBALANCE, label_for(VTI_CORE_REBALANCE)
    if "halt" in text or "drawdown" in text or "breach" in text:
        return DRAWDOWN_HALT, label_for(DRAWDOWN_HALT)
    if "yield" in text or "yield_gate" in text:
        return YIELD_GATE, label_for(YIELD_GATE)
    if "daily_loss" in text or "daily loss" in text:
        return DAILY_LOSS_LIMIT, label_for(DAILY_LOSS_LIMIT)
    if "dynamic_risk" in text or "pod_risk" in text or "risk_parity" in text:
        return DYNAMIC_RISK, label_for(DYNAMIC_RISK)
    if "eod" in text or "end-of-day" in text or "session closed" in text:
        return EOD_REBALANCE, label_for(EOD_REBALANCE)
    if "tilt" in text or "thinking" in text:
        return THINKING_TILT_REBALANCE, label_for(THINKING_TILT_REBALANCE)
    if text in ("exit", "reduce"):
        code = EXIT if text == "exit" else REDUCE
        return code, label_for(code)
    if text and len(text) <= 32 and "/" not in text and " " not in text:
        # Short pair keys from stat-arb often look like BTC/ETH.
        if "-" in text and any(c.isalpha() for c in text):
            return STAT_ARB_EXIT, label_for(STAT_ARB_EXIT)

    if text:
        return UNKNOWN, f"{label_for(UNKNOWN)}: {raw}"
    return UNKNOWN, label_for(UNKNOWN)


def enrich_trade_record(
    order: dict,
    *,
    symbol: str,
    side: str,
    exit_reason: str = "",
    sleeve: str = "",
    bar_date: str = "",
) -> dict:
    """Attach normalized exit metadata to a fill record."""
    sym = order.get("symbol") or symbol
    s = (side or order.get("side") or "").lower()
    raw_reason = exit_reason or order.get("reason") or order.get("exit_reason") or ""
    slv = sleeve or order.get("sleeve") or ""
    code, label = (
        normalize_exit_reason(raw_reason, sleeve=slv)
        if s == "sell"
        else ("", "")
    )
    record = {
        **order,
        "symbol": sym,
        "side": s,
        "sleeve": slv,
        "bar_date": bar_date or order.get("bar_date") or "",
        "reason_raw": raw_reason,
        "exit_reason": code,
        "exit_reason_label": label,
    }
    if s == "sell" and code:
        logger.info(
            "exit_trade symbol=%s notional=%s exit_reason=%s detail=%r sleeve=%s date=%s",
            sym,
            record.get("notional"),
            code,
            raw_reason or label,
            slv,
            record.get("bar_date"),
        )
    return record


def summarize_exit_trades(trades: list[dict]) -> dict[str, Any]:
    """Count sells by exit_reason code."""
    sells = [t for t in trades if (t.get("side") or "").lower() == "sell"]
    by_code: Counter[str] = Counter()
    by_symbol: Counter[str] = Counter()
    notional_by_code: Counter[str] = Counter()
    for t in sells:
        code = t.get("exit_reason") or UNKNOWN
        by_code[code] += 1
        sym = str(t.get("symbol") or "")
        by_symbol[sym] += 1
        try:
            notional_by_code[code] += float(t.get("notional") or 0)
        except (TypeError, ValueError):
            pass
    return {
        "sell_count": len(sells),
        "by_reason": dict(by_code),
        "by_symbol": dict(by_symbol),
        "notional_by_reason": {k: round(v, 2) for k, v in notional_by_code.items()},
    }


def filter_sells_on_date(trades: list[dict], date_str: str) -> list[dict]:
    """Return sell trades whose bar_date starts with YYYY-MM-DD."""
    prefix = date_str[:10]
    out = []
    for t in trades:
        if (t.get("side") or "").lower() != "sell":
            continue
        bd = str(t.get("bar_date") or "")
        if bd.startswith(prefix):
            out.append(t)
    return out


def format_exit_trades_text(trades: list[dict], *, title: str = "Exit trades") -> str:
    sells = [t for t in trades if (t.get("side") or "").lower() == "sell"]
    if not sells:
        return f"{title}: (none)"
    lines = [title + ":"]
    for t in sorted(sells, key=lambda x: (x.get("bar_date", ""), x.get("symbol", ""))):
        lines.append(
            f"  {t.get('bar_date','?'):10} {str(t.get('symbol','')):12} "
            f"${float(t.get('notional') or 0):>10,.2f}  "
            f"{t.get('exit_reason','?'):28}  {t.get('reason_raw') or t.get('exit_reason_label','')}"
        )
    summary = summarize_exit_trades(sells)
    lines.append("")
    lines.append("Summary by reason:")
    for code, n in sorted(summary["by_reason"].items(), key=lambda x: -x[1]):
        amt = summary["notional_by_reason"].get(code, 0)
        lines.append(f"  {code:28} {n:4} sells  ${amt:,.2f}")
    return "\n".join(lines)


def build_exit_trades_html(trades: list[dict], *, highlight_date: str | None = None) -> str:
    sells = [t for t in trades if (t.get("side") or "").lower() == "sell"]
    if not sells:
        return ""
    summary = summarize_exit_trades(sells)
    rows = []
    for t in sorted(sells, key=lambda x: (x.get("bar_date", ""), x.get("symbol", ""))):
        bd = str(t.get("bar_date") or "")
        hl = highlight_date and bd.startswith(highlight_date[:10])
        style = ' style="background:#422006"' if hl else ""
        rows.append(
            f"<tr{style}><td>{bd}</td><td>{t.get('symbol','')}</td>"
            f"<td>${float(t.get('notional') or 0):,.2f}</td>"
            f"<td>{t.get('exit_reason','')}</td>"
            f"<td>{t.get('exit_reason_label','')}</td>"
            f"<td>{t.get('reason_raw','')}</td><td>{t.get('sleeve','')}</td></tr>"
        )
    sum_rows = "".join(
        f"<tr><td colspan='2'>{code}</td><td>${summary['notional_by_reason'].get(code,0):,.2f}</td>"
        f"<td colspan='4'>{n} sells — {label_for(code)}</td></tr>"
        for code, n in sorted(summary["by_reason"].items(), key=lambda x: -x[1])
    )
    note = ""
    if highlight_date:
        day_sells = filter_sells_on_date(sells, highlight_date)
        note = (
            f"<p><strong>{highlight_date}</strong>: {len(day_sells)} sell(s) in backtest window.</p>"
        )
    return f"""
<h2>Exit trades (sells)</h2>
{note}
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%;font-size:13px">
<thead><tr style="background:#1e293b">
<th>Date</th><th>Symbol</th><th>Notional</th><th>Code</th><th>Label</th><th>Raw reason</th><th>Sleeve</th>
</tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
<h3>By exit reason</h3>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:60%;font-size:13px">
<thead><tr style="background:#1e293b"><th>Reason</th><th>Notional</th><th>Detail</th></tr></thead>
<tbody>{sum_rows}</tbody>
</table>
"""
