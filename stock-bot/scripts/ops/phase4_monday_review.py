"""Read-only Monday review of the Phase 4 Medium paper trial.

Writes a report. Does not restart bots, change configuration, place orders,
or enable or disable scheduled tasks.

  python scripts/ops/phase4_monday_review.py
  python scripts/ops/phase4_monday_review.py --date 2026-10-12
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_ET = ZoneInfo("America/New_York")
BOOK = "alpaca_paper_v2"
USERNAME = "dawimberly"
JOURNAL = (
    ROOT
    / "data"
    / "portal"
    / "users"
    / USERNAME
    / "books"
    / BOOK
    / "paper_journal.csv"
)
PAPER_TASK = "PythonTrading_Autonomous_Paper"
PHASE4_MARK = "pythontrading-phase4"
MAIN_RUN_ALL = "pythontrading\\stock-bot\\run_all.py"
REPORT_DIR = ROOT / "reports" / "phase4_trial"


def _blank(value) -> bool:
    text = str(value or "").strip()
    return text == "" or text.lower() in {"nan", "none"}


def _num(value) -> float | None:
    if _blank(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _norm_cmd(cmd: str) -> str:
    return (cmd or "").replace("/", "\\").lower()


def _is_phase4_paper(cmd: str) -> bool:
    low = _norm_cmd(cmd)
    return PHASE4_MARK in low and "run_paper_bot.py" in low


def _is_main_live(cmd: str) -> bool:
    low = _norm_cmd(cmd)
    return MAIN_RUN_ALL in low and PHASE4_MARK not in low and "run_all.py" in low


def _session_rows(rows: list[dict], day: str) -> list[dict]:
    out = []
    for row in rows:
        ts = str(row.get("timestamp") or "")
        if ts.startswith(day):
            out.append(row)
    return out


def _check(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def evaluate(snapshot: dict) -> dict:
    """Score one session. Does not read the clock, disk, or network."""
    checks: list[dict] = []
    day = str(snapshot.get("session_date") or "")
    rows = _session_rows(list(snapshot.get("journal_rows") or []), day)
    fills = [row for row in rows if str(row.get("event") or "").strip() == "fill"]
    closed = [row for row in rows if str(row.get("event") or "").strip() == "trade_closed"]
    sells = [row for row in fills if str(row.get("side") or "").strip().lower() == "sell"]
    buys = [row for row in fills if str(row.get("side") or "").strip().lower() == "buy"]
    orders = list(snapshot.get("broker_orders") or [])

    paper_cmds = [str(cmd) for cmd in snapshot.get("paper_cmds") or []]
    phase4 = [cmd for cmd in paper_cmds if _is_phase4_paper(cmd)]
    other_paper = [cmd for cmd in paper_cmds if "run_paper_bot.py" in _norm_cmd(cmd) and not _is_phase4_paper(cmd)]
    phase4_engines = [
        cmd
        for cmd in snapshot.get("run_all_cmds") or []
        if PHASE4_MARK in _norm_cmd(str(cmd)) and "run_all.py" in _norm_cmd(str(cmd))
    ]
    if phase4 and phase4_engines and not other_paper:
        checks.append(_check("paper_process", "PASS", "Phase 4 is the only Medium paper supervisor"))
    else:
        checks.append(
            _check(
                "paper_process",
                "FAIL",
                f"phase4 supervisors={len(phase4)} engines={len(phase4_engines)} other={len(other_paper)}",
            )
        )

    live_cmd = str(snapshot.get("live_cmd") or "")
    if _is_main_live(live_cmd):
        checks.append(_check("live_process", "PASS", "Live bot is still the main-tree run_all.py"))
    else:
        checks.append(_check("live_process", "FAIL", "Live bot command is not the main-tree run_all.py"))

    task = str(snapshot.get("paper_task_status") or "").strip().lower()
    if task == "disabled":
        checks.append(_check("paper_restart_task", "PASS", f"{PAPER_TASK} is Disabled"))
    else:
        checks.append(_check("paper_restart_task", "FAIL", f"{PAPER_TASK} status is {task or 'unknown'}"))

    journal = str(snapshot.get("journal_path") or "")
    if Path(journal) == JOURNAL:
        checks.append(_check("journal_path", "PASS", str(JOURNAL)))
    else:
        checks.append(_check("journal_path", "FAIL", f"journal path is {journal or 'missing'}"))

    paper_hb = snapshot.get("paper_heartbeat") or {}
    if paper_hb.get("paper") is True and not paper_hb.get("halted") and not str(paper_hb.get("error") or "").strip():
        checks.append(
            _check(
                "paper_heartbeat",
                "PASS",
                f"paper=true halted=false age_s={paper_hb.get('age_s')}",
            )
        )
    else:
        checks.append(_check("paper_heartbeat", "FAIL", "Paper heartbeat is missing, halted, or not paper"))

    live_hb = snapshot.get("live_heartbeat") or {}
    if live_hb.get("paper") is False and not live_hb.get("halted"):
        checks.append(_check("live_heartbeat", "PASS", "live paper=false halted=false"))
    else:
        checks.append(_check("live_heartbeat", "FAIL", "Live heartbeat is missing, halted, or marked paper"))

    fill_ids = [str(row.get("order_id") or "").strip() for row in fills if str(row.get("order_id") or "").strip()]
    dup_fill_ids = sorted(oid for oid, count in Counter(fill_ids).items() if count > 1)
    broker_ids = [str(order.get("id") or "").strip() for order in orders if str(order.get("id") or "").strip()]
    dup_broker = sorted(oid for oid, count in Counter(broker_ids).items() if count > 1)
    rejected = [order for order in orders if "reject" in str(order.get("status") or "").lower()]
    filled_broker = [
        order
        for order in orders
        if _num(order.get("filled_qty")) not in (None, 0.0)
    ]
    filled_ids = {str(order.get("id") or "").strip() for order in filled_broker}
    journal_ids = set(fill_ids)

    if not fills and not orders:
        checks.append(_check("broker_match", "NOT_YET_TESTED", "No session orders or fills"))
        checks.append(_check("duplicate_orders", "NOT_YET_TESTED", "No session order ids"))
        checks.append(_check("rejections", "NOT_YET_TESTED", "No session broker orders"))
    else:
        missing_journal = sorted(filled_ids - journal_ids)
        missing_broker = sorted(journal_ids - {oid for oid in broker_ids})
        if dup_fill_ids or dup_broker:
            checks.append(
                _check(
                    "duplicate_orders",
                    "FAIL",
                    f"journal dups={dup_fill_ids} broker dups={dup_broker}",
                )
            )
        else:
            checks.append(_check("duplicate_orders", "PASS", "No duplicate order ids"))
        if rejected:
            symbols = ", ".join(sorted({str(order.get("symbol") or "") for order in rejected}))
            checks.append(_check("rejections", "FAIL", f"{len(rejected)} rejected ({symbols})"))
        else:
            checks.append(_check("rejections", "PASS", "No rejected orders"))
        if missing_journal or missing_broker:
            checks.append(
                _check(
                    "broker_match",
                    "FAIL",
                    f"broker fills missing from journal={missing_journal} journal fills missing from broker={missing_broker}",
                )
            )
        else:
            checks.append(_check("broker_match", "PASS", f"{len(filled_ids)} filled order ids match"))

    fill_pnl = 0.0
    fill_pnl_n = 0
    for row in fills:
        pnl = _num(row.get("realized_pnl"))
        if pnl is not None:
            fill_pnl += pnl
            fill_pnl_n += 1
    closed_with_pnl = [row for row in closed if _num(row.get("realized_pnl")) is not None]
    if not fills and not closed:
        checks.append(_check("realized_pnl", "NOT_YET_TESTED", "No fills and no trade_closed rows"))
        checks.append(_check("trade_closed_pnl", "NOT_YET_TESTED", "No trade_closed rows"))
    elif closed_with_pnl:
        checks.append(
            _check(
                "trade_closed_pnl",
                "FAIL",
                f"{len(closed_with_pnl)} trade_closed rows carry realized_pnl",
            )
        )
        checks.append(
            _check(
                "realized_pnl",
                "FAIL",
                f"fill realized_pnl={fill_pnl:.2f} from {fill_pnl_n} fills; trade_closed also has dollars",
            )
        )
    else:
        checks.append(_check("trade_closed_pnl", "PASS", "trade_closed rows do not carry realized_pnl"))
        checks.append(
            _check(
                "realized_pnl",
                "PASS",
                f"fill realized_pnl={fill_pnl:.2f} from {fill_pnl_n} fills",
            )
        )

    lots = snapshot.get("lots") or {}
    lot_order_ids = set()
    if isinstance(lots, dict):
        for rows_for_symbol in (lots.get("lots") or lots).values():
            if not isinstance(rows_for_symbol, list):
                continue
            for lot in rows_for_symbol:
                if isinstance(lot, dict) and str(lot.get("order_id") or "").strip():
                    lot_order_ids.add(str(lot["order_id"]).strip())
    buy_ids = [str(row.get("order_id") or "").strip() for row in buys if str(row.get("order_id") or "").strip()]
    sold_symbols = {str(row.get("symbol") or "").strip().upper() for row in sells}
    missing_lots = []
    for row in buys:
        oid = str(row.get("order_id") or "").strip()
        symbol = str(row.get("symbol") or "").strip().upper()
        if not oid:
            continue
        if oid not in lot_order_ids and symbol not in sold_symbols:
            missing_lots.append(oid)
    if not buys and not sells:
        checks.append(_check("open_trade_ids", "NOT_YET_TESTED", "No session buys or sells"))
    elif missing_lots:
        checks.append(_check("open_trade_ids", "FAIL", f"buy order ids missing from lots: {missing_lots}"))
    else:
        checks.append(_check("open_trade_ids", "PASS", f"{len(buy_ids)} buy order ids accounted for"))

    meta = [row for row in snapshot.get("exit_metadata") or [] if str(row.get("timestamp") or "").startswith(day)]
    meta_ids = [str(row.get("order_id") or "").strip() for row in meta if str(row.get("order_id") or "").strip()]
    sell_ids = [str(row.get("order_id") or "").strip() for row in sells if str(row.get("order_id") or "").strip()]
    if not sells and not meta:
        checks.append(_check("exit_metadata", "NOT_YET_TESTED", "No session exits"))
    else:
        dup_meta = sorted(oid for oid, count in Counter(meta_ids).items() if count > 1)
        extra = sorted(set(meta_ids) - set(sell_ids))
        missing_meta = sorted(set(sell_ids) - set(meta_ids))
        pnl_mismatch = []
        fill_by_id = {str(row.get("order_id") or "").strip(): row for row in sells}
        for row in meta:
            oid = str(row.get("order_id") or "").strip()
            fill = fill_by_id.get(oid)
            if fill is None:
                continue
            if _num(row.get("realized_pnl")) != _num(fill.get("realized_pnl")):
                pnl_mismatch.append(oid)
            if str(row.get("pnl_role") or "") != "slice":
                pnl_mismatch.append(oid)
        if dup_meta or extra or missing_meta or pnl_mismatch:
            checks.append(
                _check(
                    "exit_metadata",
                    "FAIL",
                    f"dups={dup_meta} extra={extra} missing={missing_meta} pnl_mismatch={sorted(set(pnl_mismatch))}",
                )
            )
        else:
            checks.append(_check("exit_metadata", "PASS", f"{len(sell_ids)} exits match fill slices"))

    statuses = {item["status"] for item in checks}
    if "FAIL" in statuses:
        verdict = "FAIL"
        recommendation = "Roll the Medium paper bot back to the main tree, then re-enable the 11 PM task. Do not keep Phase 4."
    elif "NOT_YET_TESTED" in statuses:
        verdict = "NOT_YET_TESTED"
        recommendation = "Leave Phase 4 running. Accounting checks still need a session with paper fills."
    else:
        verdict = "PASS"
        recommendation = "Retain Phase 4. Move on to profitability work. Leave the 11 PM task disabled until paper is back on the main tree."
    return {
        "verdict": verdict,
        "recommendation": recommendation,
        "fill_realized_pnl": round(fill_pnl, 2),
        "fills": len(fills),
        "checks": checks,
    }


def _query_task(name: str) -> str:
    proc = subprocess.run(
        ["schtasks", "/Query", "/TN", name, "/FO", "LIST"],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in (proc.stdout or "").splitlines():
        if line.strip().lower().startswith("status:"):
            return line.split(":", 1)[1].strip()
    return "missing"


def _bot_processes() -> list[dict]:
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -and ($_.CommandLine -match 'run_paper_bot\\.py|run_all\\.py') } | "
        "ForEach-Object { '{0}|{1}|{2}' -f $_.ProcessId, $_.ParentProcessId, $_.CommandLine }"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )
    rows = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        rows.append({"pid": parts[0], "ppid": parts[1], "cmd": parts[2]})
    return rows


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _heartbeat(path: Path, now: datetime) -> dict:
    data = _read_json(path)
    if not data:
        return {}
    age = int((now - datetime.fromtimestamp(path.stat().st_mtime, tz=now.tzinfo)).total_seconds())
    return {
        "paper": data.get("paper"),
        "halted": bool(data.get("halted")),
        "error": str(data.get("last_cycle_error") or ""),
        "age_s": age,
        "timestamp": data.get("timestamp"),
    }


def _journal_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8", errors="replace") as handle:
        return list(csv.DictReader(handle))


def _exit_metadata(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _book_env(path: Path) -> dict[str, str]:
    vals: dict[str, str] = {}
    if not path.is_file():
        return vals
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        vals[key.strip()] = value.strip().strip('"').strip("'")
    return vals


def _broker_orders(day: str) -> tuple[list[dict], str]:
    env = _book_env(JOURNAL.parent / ".env")
    paper = (env.get("PAPER_TRADING") or "").lower()
    allow_live = (env.get("ALLOW_LIVE_TRADING") or "").lower()
    if paper not in {"true", "1", "yes"} or allow_live in {"yes", "true", "1"}:
        return [], "book env is not paper-only"
    key = env.get("APCA_API_KEY_ID") or ""
    secret = env.get("APCA_API_SECRET_KEY") or ""
    if not key or not secret:
        return [], "paper keys missing"
    import config
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    from modules.alpaca_client import build_trading_client

    client = build_trading_client(key, secret, paper=True)
    start = datetime.fromisoformat(day).replace(tzinfo=_ET)
    end = start.replace(hour=23, minute=59, second=59)
    req = GetOrdersRequest(status=QueryOrderStatus.ALL, after=start, until=end, limit=500, nested=True)
    try:
        found = list(client.get_orders(filter=req))
    except Exception:
        return [], "paper broker query failed"
    orders = []
    for order in found:
        orders.append(
            {
                "id": str(getattr(order, "id", "") or ""),
                "symbol": str(getattr(order, "symbol", "") or ""),
                "status": str(getattr(order, "status", "") or "").split(".")[-1],
                "filled_qty": getattr(order, "filled_qty", None),
            }
        )
    return orders, ""


def _render(result: dict, snapshot: dict) -> str:
    lines = [
        f"# Phase 4 Monday review — {snapshot.get('session_date')}",
        "",
        f"Verdict: **{result['verdict']}**",
        "",
        result["recommendation"],
        "",
        f"Fills: {result['fills']}. Fill realized P&L: {result['fill_realized_pnl']:.2f}.",
        "",
        "| Check | Status | Detail |",
        "| --- | --- | --- |",
    ]
    for item in result["checks"]:
        detail = str(item["detail"]).replace("|", "/")
        lines.append(f"| {item['name']} | {item['status']} | {detail} |")
    lines.append("")
    lines.append("This report does not restart bots or change the 11 PM task.")
    lines.append("")
    return "\n".join(lines)


def _write_report(text: str, day: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"{day}.md"
    path.write_text(text, encoding="utf-8")
    (REPORT_DIR / "latest.md").write_text(text, encoding="utf-8")
    return path


def collect(day: str, now: datetime) -> dict:
    book = JOURNAL.parent
    procs = _bot_processes()
    paper_pid = ""
    pid_path = book / "bot.pid"
    if pid_path.is_file():
        paper_pid = pid_path.read_text(encoding="utf-8").strip()
    live_pid = ""
    live_pid_path = book.parent / "alpaca_live" / "bot.pid"
    if live_pid_path.is_file():
        live_pid = live_pid_path.read_text(encoding="utf-8").strip()
    paper_cmds = [row["cmd"] for row in procs if "run_paper_bot.py" in _norm_cmd(row["cmd"])]
    if paper_pid:
        paper_cmds = [row["cmd"] for row in procs if row["pid"] == paper_pid] or paper_cmds
    live_cmd = ""
    for row in procs:
        if row["pid"] == live_pid:
            live_cmd = row["cmd"]
            break
    broker_orders, broker_error = _broker_orders(day)
    snapshot = {
        "session_date": day,
        "journal_path": str(JOURNAL) if JOURNAL.is_file() else "",
        "paper_cmds": paper_cmds,
        "run_all_cmds": [row["cmd"] for row in procs if "run_all.py" in _norm_cmd(row["cmd"])],
        "live_cmd": live_cmd,
        "paper_task_status": _query_task(PAPER_TASK),
        "journal_rows": _journal_rows(JOURNAL),
        "broker_orders": broker_orders,
        "lots": _read_json(book / "open_trade_ids.json"),
        "exit_metadata": _exit_metadata(book / "exit_metadata.jsonl"),
        "paper_heartbeat": _heartbeat(book / "bot_heartbeat.json", now),
        "live_heartbeat": _heartbeat(book.parent / "alpaca_live" / "bot_heartbeat.json", now),
    }
    if broker_error:
        snapshot["broker_error"] = broker_error
    return snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Phase 4 Medium paper review")
    parser.add_argument("--date", default="", help="Session date YYYY-MM-DD, default today ET")
    args = parser.parse_args(argv)
    now = datetime.now(_ET)
    day = args.date.strip() or now.date().isoformat()
    snapshot = collect(day, now)
    if snapshot.get("broker_error"):
        result = evaluate(snapshot)
        result["checks"].append(_check("broker_query", "FAIL", str(snapshot["broker_error"])))
        result["verdict"] = "FAIL"
        result["recommendation"] = (
            "Broker check failed. Do not roll back from this report, and do not treat the trial as passed."
        )
    else:
        result = evaluate(snapshot)
    text = _render(result, snapshot)
    path = _write_report(text, day)
    print(text)
    print(f"Report: {path}")
    return 1 if result["verdict"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
