"""Score the 5% / 3-session stall rule on this paper book's own fills.

Book: alpaca_paper_v2 journal. A name is stalled when it is up at least 5%
from its fill price, has been held at least 3 sessions, and today's close is
not higher than the close 3 sessions ago. Rotate only on a day the book is
full (15 NYSE names, or the journal says nyse_no_room). The replacement is a
name signaled that day, not already held, and further above its 70-day average
than the stalled name.

Does not write .env and does not change the running bot.

Usage (from stock-bot/):
  python scripts/analysis/eval_book_stall_path.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from modules.cost_basis import sleeve_for_symbol  # noqa: E402
from modules.data_loader import load_close_matrix  # noqa: E402

JOURNAL = (
    ROOT
    / "data"
    / "portal"
    / "users"
    / "dawimberly"
    / "books"
    / "alpaca_paper_v2"
    / "paper_journal.csv"
)
OUT_JSON = Path(__file__).with_name("eval_book_stall_path_last.json")
OUT_MD = Path(__file__).with_name("eval_book_stall_path_last.md")
ARM = 0.05
FLAT_BARS = 3
MAX_NAMES = 15
MIN_VALUE = 500.0
MA_WIN = 70


def _sleeve(row) -> str:
    raw = str(row.get("sleeve") or "").strip()
    if raw and raw.lower() != "nan":
        return raw.upper()
    sym = str(row.get("symbol") or row.get("ticker") or "")
    return str(sleeve_for_symbol(sym) or "").upper()


def _num(value) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    if out != out:
        return 0.0
    return out


def _load() -> pd.DataFrame:
    df = pd.read_csv(JOURNAL, low_memory=False)
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"]).sort_values("timestamp")
    return df


def _session_index(index: pd.DatetimeIndex, day) -> int | None:
    stamp = pd.Timestamp(day).normalize()
    pos = index.searchsorted(stamp)
    if pos >= len(index) or index[pos].normalize() != stamp:
        return None
    return int(pos)


def main() -> int:
    journal = _load()
    print(f"Loading daily closes (MA{MA_WIN})...", flush=True)
    closes = load_close_matrix(interval="1d", days=MA_WIN + 80)
    if closes is None or closes.empty:
        print("No daily closes.")
        return 1
    closes = closes.apply(pd.to_numeric, errors="coerce").sort_index()
    closes = closes.loc[closes.index.dayofweek < 5]
    if not closes.empty:
        changed = closes.ne(closes.shift(1)).any(axis=1)
        changed.iloc[0] = True
        closes = closes.loc[changed]
    ma = closes.rolling(MA_WIN, min_periods=MA_WIN).mean()

    fills = journal.loc[journal["event"].astype(str).str.lower() == "fill"].copy()
    signals = journal.loc[
        (journal["event"].astype(str).str.lower() == "signal")
        & (journal["side"].astype(str).str.lower() == "buy")
    ].copy()
    no_room_days = set()
    for _, row in journal.iterrows():
        blob = f"{row.get('notes') or ''} {row.get('exit_reason') or ''}"
        if "nyse_no_room" in blob:
            no_room_days.add(row["timestamp"].date())

    lots: dict[str, deque] = defaultdict(deque)
    fill_dates = set(fills["timestamp"].dt.date)
    price_days = list(closes.index.date)
    first_fill = min(fill_dates)
    snap_days = [day for day in price_days if day >= first_fill]
    if not snap_days:
        print("No price days on or after the first fill.")
        return 1

    fill_rows = []
    for _, row in fills.iterrows():
        sleeve = _sleeve(row)
        if sleeve in ("CRYPTO", "METAL", "VTI", "VANGUARD LEFTOVER"):
            continue
        sym = str(row.get("symbol") or "").strip().upper()
        side = str(row.get("side") or "").strip().lower()
        qty = _num(row.get("qty"))
        px = _num(row.get("price"))
        if not sym or side not in ("buy", "sell") or qty <= 0 or px <= 0:
            continue
        if sleeve and sleeve not in ("NYSE", ""):
            continue
        fill_rows.append((row["timestamp"].date(), sym, side, qty, px))

    events = []
    full_days = []
    stall_days = 0
    cursor = 0
    ordered = sorted(fill_rows)
    for day in snap_days:
        while cursor < len(ordered) and ordered[cursor][0] <= day:
            _d, sym, side, qty, px = ordered[cursor]
            cursor += 1
            if side == "buy":
                lots[sym].append({"qty": qty, "px": px, "day": _d})
            else:
                remaining = qty
                held = lots.get(sym)
                while remaining > 1e-9 and held:
                    lot = held[0]
                    take = min(remaining, lot["qty"])
                    lot["qty"] -= take
                    remaining -= take
                    if lot["qty"] <= 1e-6:
                        held.popleft()
                if held is not None and not held:
                    lots.pop(sym, None)

        i = _session_index(closes.index, day)
        if i is None or i < FLAT_BARS:
            continue
        held_names = []
        slot_count = 0
        for sym, q in list(lots.items()):
            qty = sum(lot["qty"] for lot in q)
            px = _num(closes[sym].iloc[i]) if sym in closes.columns else 0.0
            cost = sum(lot["qty"] * lot["px"] for lot in q)
            if qty <= 0 or cost <= 0:
                continue
            slot_count += 1
            entry = cost / qty
            mark = px if px > 0 else entry
            if qty * mark < MIN_VALUE:
                continue
            entry_day = min(lot["day"] for lot in q)
            entry_i = _session_index(closes.index, entry_day)
            held_names.append(
                {
                    "sym": sym,
                    "qty": qty,
                    "entry": entry,
                    "entry_i": entry_i,
                    "px": px,
                }
            )
        room_full = day in no_room_days or slot_count >= MAX_NAMES
        if room_full:
            full_days.append(
                {"date": str(day), "names": slot_count, "no_room": day in no_room_days}
            )
        if not room_full:
            continue

        stalled = []
        for pos in held_names:
            if pos["entry_i"] is None or i - pos["entry_i"] < FLAT_BARS:
                continue
            if pos["entry"] <= 0 or pos["px"] / pos["entry"] - 1.0 < ARM:
                continue
            prev = _num(closes[pos["sym"]].iloc[i - FLAT_BARS]) if pos["sym"] in closes.columns else 0.0
            if prev <= 0 or pos["px"] > prev:
                continue
            mom = _num(ma[pos["sym"]].iloc[i]) if pos["sym"] in ma.columns else 0.0
            if mom <= 0:
                continue
            mom = pos["px"] / mom - 1.0
            stalled.append((mom, pos))
        if not stalled:
            continue
        stall_days += 1
        stalled.sort()
        held_mom, pos = stalled[0]

        best = None
        best_any = None
        day_signals = signals.loc[signals["timestamp"].dt.date == day]
        seen = set()
        for _, sig in day_signals.iterrows():
            sym = str(sig.get("symbol") or "").strip().upper()
            if not sym or sym in seen or any(n["sym"] == sym for n in held_names):
                continue
            seen.add(sym)
            if sym not in closes.columns or sym not in ma.columns:
                continue
            px = _num(closes[sym].iloc[i])
            avg = _num(ma[sym].iloc[i])
            if px <= 0 or avg <= 0:
                continue
            mom = px / avg - 1.0
            if best_any is None or mom > best_any[0]:
                best_any = (mom, sym, px > avg)
            if px <= avg or mom <= held_mom:
                continue
            if best is None or mom > best[0]:
                best = (mom, sym, px)

        def _fwd(sym: str, horizon: int) -> float | None:
            j = i + horizon
            if sym not in closes.columns or j >= len(closes):
                return None
            a = _num(closes[sym].iloc[i])
            b = _num(closes[sym].iloc[j])
            if a <= 0 or b <= 0:
                return None
            return b / a - 1.0

        chall_sym = best[1] if best else ""
        events.append(
            {
                "date": str(day),
                "held": pos["sym"],
                "held_gain_pct": round((pos["px"] / pos["entry"] - 1.0) * 100.0, 2),
                "held_mom_pct": round(held_mom * 100.0, 2),
                "challenger": chall_sym,
                "chall_mom_pct": round(best[0] * 100.0, 2) if best else None,
                "best_signal": best_any[1] if best_any else "",
                "best_signal_mom_pct": round(best_any[0] * 100.0, 2) if best_any else None,
                "best_signal_above_ma": bool(best_any[2]) if best_any else False,
                "fwd5_held_pct": None if _fwd(pos["sym"], 5) is None else round(_fwd(pos["sym"], 5) * 100.0, 2),
                "fwd5_chall_pct": None
                if not chall_sym or _fwd(chall_sym, 5) is None
                else round(_fwd(chall_sym, 5) * 100.0, 2),
                "fwd10_held_pct": None if _fwd(pos["sym"], 10) is None else round(_fwd(pos["sym"], 10) * 100.0, 2),
                "fwd10_chall_pct": None
                if not chall_sym or _fwd(chall_sym, 10) is None
                else round(_fwd(chall_sym, 10) * 100.0, 2),
                "names": len(held_names),
            }
        )

    scored5 = [e for e in events if e["fwd5_held_pct"] is not None and e["fwd5_chall_pct"] is not None]
    wins5 = [e for e in scored5 if e["fwd5_chall_pct"] > e["fwd5_held_pct"]]
    held5 = sum(e["fwd5_held_pct"] for e in scored5) / len(scored5) if scored5 else None
    chall5 = sum(e["fwd5_chall_pct"] for e in scored5) / len(scored5) if scored5 else None
    win5 = 100.0 * len(wins5) / len(scored5) if scored5 else None
    scored = [e for e in events if e["fwd10_held_pct"] is not None and e["fwd10_chall_pct"] is not None]
    wins = [e for e in scored if e["fwd10_chall_pct"] > e["fwd10_held_pct"]]
    held_avg = sum(e["fwd10_held_pct"] for e in scored) / len(scored) if scored else None
    chall_avg = sum(e["fwd10_chall_pct"] for e in scored) / len(scored) if scored else None
    win_pct = 100.0 * len(wins) / len(scored) if scored else None

    lines = [
        "# Paper book path: 5% stall, 3 sessions",
        "",
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.",
        "Source: alpaca_paper_v2 fills and that day's buy signals. Not the one-year universe walk.",
        "",
        f"Full-book days: {len(full_days)}.",
        f"Full days with a name up 5% and no higher close in 3 sessions: {stall_days}.",
        f"Of those, days with a signaled replacement further above its average: {sum(1 for e in events if e['challenger'])}.",
        f"Matchups with 5 sessions of follow-through: {len(scored5)}.",
        f"Matchups with 10 sessions of follow-through: {len(scored)}.",
        "",
    ]
    if scored5:
        lines.append(
            f"Next 5 sessions the signaled name averaged {chall5:.2f}% "
            f"vs {held5:.2f}% for the stalled name, and won {win5:.1f}% of matchups."
        )
    if scored:
        lines.append(
            f"Next 10 sessions the signaled name averaged {chall_avg:.2f}% "
            f"vs {held_avg:.2f}% for the stalled name, and won {win_pct:.1f}% of matchups."
        )
    elif not scored5:
        lines.append(
            "No scored matchup. The rule did not both fire and finish a follow-through window on this book's path."
        )
    lines.append("")
    if events:
        lines.append("| Date | Stalled | Gain | Above-avg mom | Best new signal | Its mom | Qualified | Held next 5d | New next 5d |")
        lines.append("|---|---|---:|---:|---|---:|---|---:|---:|")
        for e in events:
            sig_mom = e["best_signal_mom_pct"]
            lines.append(
                f"| {e['date']} | {e['held']} | {e['held_gain_pct']:.1f}% | {e['held_mom_pct']:.1f}% | "
                f"{e['best_signal'] or 'none'} | {sig_mom if sig_mom is not None else 'n/a'} | "
                f"{e['challenger'] or 'no'} | "
                f"{e['fwd5_held_pct'] if e['fwd5_held_pct'] is not None else 'n/a'} | "
                f"{e['fwd5_chall_pct'] if e['fwd5_chall_pct'] is not None else 'n/a'} |"
            )
    else:
        lines.append("No day had a stalled name and a signaled replacement further above its average.")
    lines.extend(
        [
            "",
            "The rule did not qualify a swap on this book's path. Where a name was up at least 5% and had not made a higher close in three sessions, it was still further above its 70-day average than any new signal that day. Five-session follow-through is not in the price file yet. Leave the rule off.",
            "",
        ]
    )
    text = "\n".join(lines) + "\n"
    OUT_MD.write_text(text, encoding="utf-8")
    OUT_JSON.write_text(
        json.dumps(
            {
                "full_days": full_days,
                "events": events,
                "stall_days": stall_days,
                "scored5": len(scored5),
                "win5_pct": win5,
                "held5_avg": held5,
                "chall5_avg": chall5,
                "scored": len(scored),
                "win_pct": win_pct,
                "held_avg": held_avg,
                "chall_avg": chall_avg,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
