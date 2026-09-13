#!/usr/bin/env python3
"""Daily Gate A/B logger — NYSE vs VTI open % (ex-SPCX).

Appends one row per calendar day to data/gate_ab_log.csv.
Freeze-safe: does not trim, rebalance, or change trading .env knobs.

Usage (from stock-bot/):
  python scripts/ops/log_gate_ab.py
  python scripts/ops/log_gate_ab.py --force
  python scripts/ops/log_gate_ab.py --backfill-packs --since 2026-07-30

Schedule (recommended): scripts/ops/install_gate_ab_task.ps1
Do NOT wire into run_paper_bot.py while freeze is on.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LOG_PATH = ROOT / "data" / "gate_ab_log.csv"
ET = ZoneInfo("America/New_York")
CORE = {"VTI", "VOO", "ITOT"}
SPY = {"SPY"}
METAL = {"GLD", "SLV", "CPER", "IAU", "GDX"}
SPCX = "SPCX"
HEADER = [
    "date",
    "vti_open_pct",
    "nyse_open_pct",
    "nyse_ex_spcx_pct",
    "nyse_beats_vti",
    "nyse_ex_spcx_beats_vti",
    "source",
]


def _resolve_paper_env_file() -> Path | None:
    try:
        from modules.portal_paths import (
            bind_project_root,
            book_env_path,
            get_last_username,
            has_alpaca_config,
        )

        bind_project_root(ROOT)
        username = (get_last_username() or "").strip().lower()
        portal = ROOT / "data" / "portal" / "users"
        candidates: list[Path] = []
        if username and has_alpaca_config(username, "alpaca_paper"):
            candidates.append(book_env_path(username, "alpaca_paper"))
        if portal.is_dir():
            candidates.extend(portal.glob("*/books/alpaca_paper/.env"))
        existing = [p for p in candidates if p.is_file()]
        if existing:
            return max(existing, key=lambda p: p.stat().st_mtime)
    except Exception:
        pass
    stock = ROOT / ".env"
    return stock if stock.is_file() else None


def _load_paper_credentials() -> tuple[str, str]:
    from dotenv import dotenv_values

    env_path = _resolve_paper_env_file()
    vals: dict[str, str | None] = {}
    if env_path is not None:
        vals.update(dotenv_values(env_path))
    for k, v in list(vals.items()):
        if v is not None and k not in os.environ:
            os.environ[k] = v

    key = (
        os.getenv("PAPER_APCA_API_KEY_ID")
        or os.getenv("PAPER_API_KEY")
        or os.getenv("APCA_API_KEY_ID")
        or os.getenv("ALPACA_API_KEY")
        or os.getenv("APCA_API_KEY")
    )
    secret = (
        os.getenv("PAPER_APCA_API_SECRET_KEY")
        or os.getenv("PAPER_SECRET_KEY")
        or os.getenv("APCA_API_SECRET_KEY")
        or os.getenv("ALPACA_SECRET_KEY")
        or os.getenv("APCA_API_SECRET")
    )
    if not key or not secret:
        raise RuntimeError(
            "Missing paper Alpaca credentials "
            "(PAPER_APCA_* / APCA_API_KEY_ID / ALPACA_API_KEY)"
        )
    return key, secret


def _sleeve(sym: str) -> str:
    s = sym.upper()
    if s in CORE:
        return "core"
    if s in SPY:
        return "spy"
    if s in METAL:
        return "metal"
    return "nyse"


def _fetch_snapshot() -> dict[str, Any]:
    from alpaca.trading.client import TradingClient

    key, secret = _load_paper_credentials()
    client = TradingClient(key, secret, paper=True)

    vti_cost = vti_mv = 0.0
    nyse_cost = nyse_mv = 0.0
    spcx_cost = spcx_mv = 0.0

    for p in client.get_all_positions():
        sym = str(p.symbol).upper()
        cost = float(p.avg_entry_price) * abs(float(p.qty))
        mv = float(p.market_value)
        sleeve = _sleeve(sym)
        if sleeve == "core":
            vti_cost += cost
            vti_mv += mv
        elif sleeve == "nyse":
            nyse_cost += cost
            nyse_mv += mv
            if sym == SPCX:
                spcx_cost += cost
                spcx_mv += mv

    vti_open_pct = ((vti_mv / vti_cost) - 1.0) * 100.0 if vti_cost else 0.0
    nyse_open_pct = ((nyse_mv / nyse_cost) - 1.0) * 100.0 if nyse_cost else 0.0
    ex_cost = nyse_cost - spcx_cost
    ex_mv = nyse_mv - spcx_mv
    nyse_ex_spcx_pct = ((ex_mv / ex_cost) - 1.0) * 100.0 if ex_cost else 0.0

    return {
        "vti_open_pct": vti_open_pct,
        "nyse_open_pct": nyse_open_pct,
        "nyse_ex_spcx_pct": nyse_ex_spcx_pct,
        "spcx_mv": spcx_mv,
        "spcx_cost": spcx_cost,
        "nyse_beats_vti": nyse_open_pct > vti_open_pct,
        "nyse_ex_spcx_beats_vti": nyse_ex_spcx_pct > vti_open_pct,
        "source": "alpaca",
    }


def _pack_rows(since: str) -> list[dict[str, Any]]:
    """Approx open % from overnight packs (UPnL / implied sleeve MV).

    Gate B (ex-SPCX) cannot be computed from packs — left blank.
    """
    out: list[dict[str, Any]] = []
    for p in sorted((ROOT / "data").glob("overnight_pack_*.txt")):
        day = p.stem.replace("overnight_pack_", "")
        if day < since:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        m_eq = re.search(r"Paper:\s*\$([0-9,]+\.\d+)", text)
        if not m_eq:
            continue
        eq = float(m_eq.group(1).replace(",", ""))
        vals: dict[str, float] = {}
        for m in re.finditer(
            r"(core|nyse|spy|metal)\s+(\d+)\s+([+-]?[0-9,]+\.\d+)\s+([0-9.]+)%",
            text,
        ):
            sleeve = m.group(1)
            upnl = float(m.group(3).replace(",", ""))
            pct_book = float(m.group(4))
            mv = eq * pct_book / 100.0
            if mv:
                vals[sleeve] = (upnl / mv) * 100.0
        if "core" not in vals or "nyse" not in vals:
            continue
        vti = vals["core"]
        nyse = vals["nyse"]
        out.append(
            {
                "date": day,
                "vti_open_pct": f"{vti:.4f}",
                "nyse_open_pct": f"{nyse:.4f}",
                "nyse_ex_spcx_pct": "",  # unknown from packs
                "nyse_beats_vti": str(nyse > vti),
                "nyse_ex_spcx_beats_vti": "",  # unknown
                "source": "pack_approx",
            }
        )
    return out


def _read_rows() -> list[dict[str, str]]:
    if not LOG_PATH.is_file():
        return []
    with LOG_PATH.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    # Normalize missing source
    for r in rows:
        r.setdefault("source", "alpaca")
    return rows


def _write_rows(rows: list[dict[str, Any]]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    rows.sort(key=lambda r: r.get("date", ""))
    with LOG_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEADER)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in HEADER})


def _as_bool(v: Any) -> bool | None:
    if v is None or str(v).strip() == "":
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "t"):
        return True
    if s in ("0", "false", "no", "f"):
        return False
    return None


def _summarize(rows: list[dict[str, str]], window: int = 15) -> None:
    rows = sorted(rows, key=lambda r: r.get("date", ""))
    last = rows[-window:] if rows else []
    n = len(last)
    a_vals = [_as_bool(r.get("nyse_beats_vti")) for r in last]
    b_vals = [_as_bool(r.get("nyse_ex_spcx_beats_vti")) for r in last]
    a_known = [v for v in a_vals if v is not None]
    b_known = [v for v in b_vals if v is not None]
    a_true = sum(1 for v in a_known if v)
    b_true = sum(1 for v in b_known if v)

    # Gate B streak: skip unknown (pack) rows; stop on False
    streak = 0
    for v in reversed(b_vals):
        if v is None:
            continue
        if v is True:
            streak += 1
        else:
            break

    streak_a = 0
    for v in reversed(a_vals):
        if v is True:
            streak_a += 1
        elif v is False:
            break
        # None shouldn't happen for A

    if len(b_known) < 10:
        # Prefer A if B history thin (pack backfill)
        if n < window:
            verdict = "PENDING"
        elif streak_a >= 10 and a_true >= 10:
            verdict = "PASS_A_ONLY"
        else:
            verdict = "FAIL"
    elif n < window:
        verdict = "PENDING"
    elif streak >= 10 and b_true >= 10:
        verdict = "PASS"
    else:
        verdict = "FAIL"

    print(f"Last {n} session(s) in log (window={window}):")
    print(f"  Gate A known True: {a_true}/{len(a_known)} (streak={streak_a})")
    print(f"  Gate B known True: {b_true}/{len(b_known)} (streak={streak}; blank=pack)")
    print(
        f"Gate A: {a_true}/{window} | Gate B (ex-SPCX): {b_true}/{window} | "
        f"streak_B: {streak} | streak_A: {streak_a} | -> {verdict}"
    )
    if verdict == "PASS_A_ONLY":
        print("Note: Gate B needs alpaca rows (ex-SPCX); pack backfill is Gate A only.")
    if n < window:
        print(f"(PENDING until {window} logged sessions; have {n})")


def _merge_row(rows: list[dict[str, str]], row: dict[str, Any], force: bool) -> str:
    idx = next((i for i, r in enumerate(rows) if r.get("date") == row["date"]), None)
    if idx is None:
        rows.append(row)
        return "appended"
    existing = rows[idx]
    # Never overwrite alpaca with pack_approx unless --force and source matches intent
    if existing.get("source") == "alpaca" and row.get("source") == "pack_approx":
        return "kept_alpaca"
    if not force and existing.get("source") == row.get("source"):
        return "skipped"
    rows[idx] = row
    return "replaced"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="Replace today's/matching rows")
    ap.add_argument("--date", type=str, default="", help="Override log date YYYY-MM-DD")
    ap.add_argument(
        "--backfill-packs",
        action="store_true",
        help="Seed history from overnight_pack_*.txt (Gate A only; no ex-SPCX)",
    )
    ap.add_argument(
        "--since",
        type=str,
        default="2026-07-30",
        help="Backfill start date (default Jul-30 trough)",
    )
    ap.add_argument("--no-snapshot", action="store_true", help="Skip live Alpaca append")
    args = ap.parse_args()

    rows = _read_rows()

    if args.backfill_packs:
        n_new = 0
        for prow in _pack_rows(args.since):
            action = _merge_row(rows, prow, force=False)
            if action == "appended":
                n_new += 1
        print(f"Pack backfill since {args.since}: +{n_new} new rows (alpaca rows kept)")

    if not args.no_snapshot:
        today = args.date.strip() or datetime.now(ET).date().isoformat()
        date.fromisoformat(today)
        snap = _fetch_snapshot()
        row = {
            "date": today,
            "vti_open_pct": f"{snap['vti_open_pct']:.4f}",
            "nyse_open_pct": f"{snap['nyse_open_pct']:.4f}",
            "nyse_ex_spcx_pct": f"{snap['nyse_ex_spcx_pct']:.4f}",
            "nyse_beats_vti": str(bool(snap["nyse_beats_vti"])),
            "nyse_ex_spcx_beats_vti": str(bool(snap["nyse_ex_spcx_beats_vti"])),
            "source": "alpaca",
        }
        action = _merge_row(rows, row, force=args.force)
        if action == "skipped":
            print(f"Already logged alpaca row for {today} — skip (use --force)")
        else:
            print(f"{action} alpaca row for {today}")
            print(
                f"{today}, {row['vti_open_pct']}, {row['nyse_open_pct']}, "
                f"{row['nyse_ex_spcx_pct']}, {row['nyse_beats_vti']}, "
                f"{row['nyse_ex_spcx_beats_vti']}"
            )
            print(
                f"(SPCX cost=${snap['spcx_cost']:.2f} mv=${snap['spcx_mv']:.2f} — "
                "stripped for Gate B)"
            )

    _write_rows(rows)
    _summarize(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
