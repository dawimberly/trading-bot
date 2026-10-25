"""Read a prepared trade-review report. No broker, settings, or order calls."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

MAX_REPORT_BYTES = 2_000_000


def report_file(root: Path, book: str) -> Path:
    return Path(root) / "reports" / "trade_review" / f"{book}.json"


def load_prepared_report(root: Path, book: str) -> tuple[dict | None, dict]:
    path = report_file(root, book)
    meta = {
        "book": book,
        "path": str(path),
        "loaded_at": datetime.now(timezone.utc).isoformat(),
        "banner": f"Book {book} — prepared file only. This view does not contact the broker.",
    }
    if not path.is_file():
        meta["banner"] = (
            f"Book {book} — no prepared report at {path.name}. "
            "Run the collector. This view does not contact the broker."
        )
        return None, meta
    size = path.stat().st_size
    meta["bytes"] = size
    meta["file_mtime"] = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    if size > MAX_REPORT_BYTES:
        meta["banner"] = f"Book {book} — prepared report is too large to show ({size} bytes)."
        return {"book": book, "error": "report_too_large", "purchases": [], "sales": []}, meta
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        meta["banner"] = f"Book {book} — prepared report could not be read."
        return {"book": book, "error": "report_unreadable", "purchases": [], "sales": []}, meta
    if not isinstance(payload, dict) or payload.get("book") != book:
        meta["banner"] = (
            f"Book {book} — file is for {payload.get('book') if isinstance(payload, dict) else 'unknown'}. "
            "Not shown."
        )
        return None, meta
    return payload, meta


def _holding_text(rules: dict | None) -> str:
    if not rules:
        return "original plan unavailable"
    if rules.get("holding_period"):
        return str(rules["holding_period"])
    if rules.get("max_hold") is None:
        return "no time exit"
    units = rules.get("hold_units") or "implementation units"
    return f"{rules.get('max_hold')} {units}"


def _stop_text(plan: dict | None, filled: dict | None) -> str:
    rules = (plan or {}).get("exit_rules") or {}
    kind = rules.get("kind")
    if kind in {"dynamic_smart_atr", "exit_optimization"}:
        return "dynamic/unknown — not a stored stop price"
    reference = (filled or {}).get("rule_implied_stop_after_fill")
    if reference is None:
        return "fixed-rule stop reference unavailable"
    return f"{reference:.4f} fixed-rule reference from the post-fill average, not a resting broker stop"


def _pct(value) -> str:
    if value is None:
        return "unavailable"
    try:
        return f"{float(value) * 100:+.2f}%"
    except (TypeError, ValueError):
        return "unavailable"


def _num(value) -> str:
    if value is None:
        return "unavailable"
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return "unavailable"


def render_dashboard(report: dict | None, *, selected_book: str, meta: dict | None = None) -> str:
    """Text for the review tab. A later price rise is not a verdict."""
    meta = meta or {}
    lines = [
        f"Book: {selected_book}",
        meta.get("banner") or "Prepared report. No broker call from this view.",
        f"Loaded at: {meta.get('loaded_at', 'unknown')}",
        f"File written: {meta.get('file_mtime', 'unknown')}",
        "Plan adherence and post-sale price movement are separate. "
        "A later rise is not a verdict on the sale.",
        "",
    ]
    if report is None:
        lines.append("original plan unavailable — no prepared rows for this book.")
        return "\n".join(lines) + "\n"
    if report.get("book") != selected_book:
        lines.append("Stale report ignored because it belongs to a different book.")
        return "\n".join(lines) + "\n"
    coverage = report.get("coverage") or {}
    lines.append(
        "Coverage: "
        f"account …{coverage.get('account_last4') or 'unknown'} | "
        f"history {'complete' if coverage.get('complete_from_start') else 'not complete'} | "
        f"truncated={coverage.get('truncated')} | "
        f"inventory {coverage.get('initial_inventory', 'unknown')}"
    )
    if report.get("error"):
        lines.append(f"Report gap: {report['error']}. Missing figures stay unavailable.")
    if report.get("as_of"):
        lines.append(f"Report as of: {report['as_of']}")
    lines.append(f"Expected-drawdown rule: {coverage.get('expected_drawdown_rule', 'not_activated')}")
    lines += ["", "Purchases", ""]
    purchases = report.get("purchases") or []
    if not purchases:
        lines.append("No equity purchases in the prepared report.")
    for row in purchases:
        plan = row.get("original_plan") or {}
        filled = row.get("filled_plan") or {}
        selection = plan.get("selection") or {}
        journal = (report.get("journal_attribution") or {}).get(row.get("order_id"), {})
        notes = (report.get("execution_notes") or {}).get(row.get("order_id"), {})
        reason = plan.get("reason") or "original plan unavailable"
        if journal.get("pair_key"):
            reason = f"{reason}; journal attribution {journal.get('pair_key')} (not a fill price)"
        rank = selection.get("final_score")
        if rank is None:
            rank = selection.get("base_score")
        rank_text = "ranking evidence unavailable" if rank is None else f"ranking score {rank}"
        role = row.get("position_role") or "unknown"
        lines.append(
            f"{row.get('symbol')}  {role}  qty {_num(row.get('quantity'))}  "
            f"fill {_num(row.get('fill_price'))}  executions {row.get('execution_count')}"
        )
        lines.append(f"  Reason: {reason}")
        lines.append(f"  {rank_text}; plan status {row.get('plan_status')}")
        lines.append(f"  Exit rules: {((plan.get('exit_rules') or {}).get('kind')) or 'original plan unavailable'}")
        lines.append(f"  Holding limit: {_holding_text(plan.get('exit_rules'))}")
        lines.append(
            "  Post-fill average: "
            + (
                _num(filled.get("broker_average_entry_after"))
                if filled.get("broker_average_entry_after") is not None
                else "unavailable"
            )
            + f" ({filled.get('snapshot_status') or 'no snapshot'})"
        )
        lines.append(f"  Stop: {_stop_text(plan, filled)}")
        status = notes.get("broker_order_status_last")
        lines.append(
            "  Broker order status: "
            + (str(status) if status else "not on the fill activity")
            + ". An order return is not a confirmed fill. "
            "Partial executions are counted above and are not the position add/new status."
        )
        lines.append(f"  Plan adherence: {row.get('plan_adherence')}")
        lines.append("")
    lines += ["Open positions — not sales", ""]
    opens = report.get("open_positions") or []
    if not opens:
        lines.append(report.get("open_position_note") or "No current broker position in this prepared report.")
    for row in opens:
        lines.append(
            f"{row.get('symbol')}  qty {_num(row.get('quantity'))}  "
            f"sale follow-through: {row.get('sale_follow_through')}"
        )
        lines.append("  Original plan: unavailable.")
    lines.append("")
    lines += ["Sales — price follow-through only", ""]
    sales = report.get("sales") or []
    if not sales:
        lines.append("No equity sales in the prepared report.")
    for row in sales:
        lines.append(
            f"{row.get('symbol')}  {row.get('position_effect')}  "
            f"sale {_num(row.get('fill_price'))}  qty {_num(row.get('quantity'))}  "
            f"sale date {row.get('sale_date_et')}"
        )
        parts = []
        gaps = []
        for horizon in ("1", "5", "10", "20", "30"):
            milestone = (row.get("milestones") or {}).get(horizon) or {}
            parts.append(f"+{horizon} {_pct(milestone.get('price_return'))}")
            gaps.extend(milestone.get("unavailable_reasons") or [])
        lines.append("  " + " | ".join(parts))
        lines.append(
            "  Observed move: "
            f"low {_pct(row.get('lowest_move'))} high {_pct(row.get('highest_move'))} "
            f"({row.get('extrema_basis')}); "
            f"sessions observed {row.get('observed_sessions')} of {row.get('elapsed_sessions')} elapsed; "
            f"30-session path {'mature' if row.get('complete_30_sessions') else 'not mature'}"
        )
        if gaps:
            lines.append("  Data gaps: " + ", ".join(sorted(set(gaps))))
        lines.append(
            "  Price movement: hypothetical continued hold of the sold shares. "
            "Dividends, fees, and redeployment are not in this number. "
            + str(row.get("replacement_status"))
        )
        lines.append("  Original plan: original plan unavailable")
        lines.append(f"  Plan adherence: {row.get('adherence')}")
        lines.append("  Timing verdict: not assigned. A later rise is not a verdict on the sale.")
        lines.append("")
    return "\n".join(lines) + "\n"


class ReviewBoard:
    """Book-switch guard. A result for a previous book is not applied."""

    def __init__(self):
        self.book = None
        self.seq = 0
        self.text = ""

    def begin_switch(self, book: str) -> int:
        self.seq += 1
        self.book = book
        self.text = (
            f"Book: {book}\n"
            "Loading the prepared report for this book. The previous book's rows are not shown.\n"
        )
        return self.seq

    def apply(self, seq: int, book: str, report: dict | None, meta: dict | None = None) -> bool:
        if seq != self.seq or book != self.book:
            return False
        self.text = render_dashboard(report, selected_book=book, meta=meta)
        return True
