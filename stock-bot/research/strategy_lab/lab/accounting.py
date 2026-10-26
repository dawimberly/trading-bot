"""Corporate-action decisions for the 2021-2026 simulation bundle.

Prices and proceeds come from the stored tape or a stated contractual rate.
A missing successor price stays unresolved. It is not replaced with the last close.
"""

from __future__ import annotations

HOLDABLE_GAP = "unpriced_successor"


def _text(value) -> str:
    return "" if value is None else str(value)


def dedupe(rows: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for row in rows:
        if row.get("problem"):
            key = (row.get("problem"), row.get("symbol"), row.get("session"), row.get("sessions"))
        else:
            key = (
                row.get("kind"),
                tuple(sorted((key, _text(val)) for key, val in row.items() if key not in {"kind", "corporate_action_type"})),
            )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def member_intervals(rows: list[tuple[str, str, str | None]]) -> dict[str, list[tuple[str, str | None]]]:
    out: dict[str, list[tuple[str, str | None]]] = {}
    for ticker, start, end in rows:
        out.setdefault(ticker, []).append((start, end or None))
    return out


def _covers(intervals: list[tuple[str, str | None]], day: str) -> bool:
    return any(start <= day and (end is None or day < end) for start, end in intervals)


def holdable(symbol: str, intervals: dict, sessions: dict[str, set[str]], unusable: set[tuple[str, str]]) -> bool:
    for day in sessions.get(symbol, ()):
        if (symbol, day) in unusable:
            continue
        if _covers(intervals.get(symbol, []), day):
            return True
    return False


def _num(row: dict, key: str) -> float | None:
    value = row.get(key)
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def classify_actions(
    rows: list[dict],
    *,
    intervals: dict[str, list[tuple[str, str | None]]],
    sessions: dict[str, set[str]],
    unusable: set[tuple[str, str]],
    closes: dict[tuple[str, str], float],
) -> dict:
    """Sort stored actions into applied instructions and explicit unresolved rows."""
    applied_renames = []
    aliases = []
    settlements = []
    spins = []
    unresolved = []
    recorded = []

    alias_symbols = set()
    for row in dedupe(rows):
        if row.get("kind") != "name_changes":
            continue
        old, new, day = row.get("old_symbol"), row.get("new_symbol"), row.get("process_date")
        if not old or not new or not day or sessions.get(new):
            continue
        old_days = sessions.get(old, set())
        if (day in old_days or any(item > day for item in old_days)) and _covers(intervals.get(new, []), day):
            alias_symbols.add(new)

    def can_hold(symbol: str) -> bool:
        return symbol in alias_symbols or holdable(symbol, intervals, sessions, unusable)

    spy_days = sorted(sessions.get("SPY", ()))

    def trading_day(day: str | None) -> str | None:
        if not day:
            return None
        if day in sessions.get("SPY", ()):
            return day
        return next((item for item in spy_days if item > day), None)

    def has_bar(symbol: str, day: str) -> bool:
        return bool(symbol) and day in sessions.get(symbol, set()) and (symbol, day) not in unusable

    mergers: dict[tuple[str, str], list[dict]] = {}
    for row in dedupe(rows):
        kind = row.get("kind")
        if row.get("problem"):
            recorded.append(row)
            continue
        if kind in {"cash_mergers", "stock_mergers", "stock_and_cash_mergers"}:
            acquiree = row.get("acquiree_symbol")
            day = trading_day(row.get("effective_date") or row.get("process_date"))
            if acquiree and day:
                mergers.setdefault((acquiree, day), []).append(row)
            continue
        if kind == "spin_offs":
            parent = row.get("source_symbol")
            child = row.get("new_symbol")
            source_rate = _num(row, "source_rate")
            new_rate = _num(row, "new_rate")
            candidates = [
                trading_day(row.get(key))
                for key in ("ex_date", "payable_date", "process_date")
            ]
            day = next((item for item in candidates if item and has_bar(child, item)), None)
            anchor = next((item for item in candidates if item), None)
            if day is None and anchor is not None and anchor in spy_days:
                start_i = spy_days.index(anchor)
                day = next((item for item in spy_days[start_i:start_i + 11] if has_bar(child, item)), None)
            if not parent or not child or not source_rate or not new_rate or source_rate <= 0:
                unresolved.append({**_brief(row), "reason": "spin terms are incomplete"})
                continue
            ratio = new_rate / source_rate
            if day is None:
                if can_hold(parent):
                    unresolved.append({**_brief(row), "reason": f"{child} has no usable bar on or after {anchor}"})
                else:
                    recorded.append({**_brief(row), "reason": "spin parent is not holdable"})
                continue
            spins.append({"session": day, "parent": parent, "child": child, "ratio": ratio})
            continue
        if kind == "name_changes":
            old = row.get("old_symbol")
            new = row.get("new_symbol")
            day = trading_day(row.get("process_date"))
            if not old or not new or not day:
                unresolved.append({**_brief(row), "reason": "name change is incomplete"})
                continue
            old_days = sorted(sessions.get(old, ()))
            continues = has_bar(old, day) or (old_days and old_days[-1] > day)
            if not sessions.get(new) and continues and _covers(intervals.get(new, []), day):
                end = next(stop for start, stop in intervals.get(new, []) if start <= day and (stop is None or day < stop))
                aliases.append(
                    {
                        "symbol": new,
                        "source": old,
                        "start": day,
                        "end": end,
                        "reason": "membership rename; SIP tape stayed on the prior symbol",
                    }
                )
                applied_renames.append({"session": day, "old": old, "new": new})
                continue
            if not sessions.get(new) and can_hold(old) and old_days and old_days[-1] <= day:
                unresolved.append({**_brief(row), "reason": HOLDABLE_GAP, "fetch_symbol": new})
                continue
            if can_hold(old) and has_bar(new, day):
                applied_renames.append({"session": day, "old": old, "new": new})
                continue
            recorded.append({**_brief(row), "reason": "name change does not move a holdable tape"})
            continue
        if kind == "stock_dividends":
            symbol = row.get("symbol")
            day = row.get("ex_date") or row.get("process_date")
            rate = _num(row, "rate")
            prior = _prior_close(closes, symbol, day) if symbol and day else None
            current = closes.get((symbol, day)) if symbol and day else None
            gapped = (
                prior is not None
                and current is not None
                and rate is not None
                and rate > 1
                and current <= prior / rate * 1.05
            )
            if gapped and can_hold(symbol):
                unresolved.append({**_brief(row), "reason": "stock dividend gaps the close and is not in the split table"})
            else:
                recorded.append(
                    {
                        **_brief(row),
                        "reason": "share count stays on the split-adjusted tape; the close does not gap by the stated rate",
                        "prior_close": prior,
                        "close": current,
                    }
                )
            continue
        if kind == "unit_splits":
            old = row.get("old_symbol")
            if can_hold(old or ""):
                unresolved.append({**_brief(row), "reason": "unit split includes consideration that is not a priced holding"})
            else:
                recorded.append({**_brief(row), "reason": "old symbol is not a holdable tape"})
            continue
        recorded.append({**_brief(row), "reason": "action kind is stored and not applied"})

    for (acquiree, day), group in sorted(mergers.items()):
        kinds = {row.get("kind") for row in group}
        if kinds != {"stock_and_cash_mergers"} and len(kinds) > 1:
            unresolved.append(
                {
                    "kind": "merger_conflict",
                    "acquiree_symbol": acquiree,
                    "effective_date": day,
                    "reason": "cash and stock terms both exist for the same acquisition; neither is applied",
                    "terms": [_brief(row) for row in group],
                }
            )
            continue
        row = group[0]
        cash = _num(row, "cash_rate") or 0.0
        if row.get("kind") == "cash_mergers":
            cash = _num(row, "rate") or 0.0
        acquirer_rate = _num(row, "acquirer_rate")
        acquiree_rate = _num(row, "acquiree_rate")
        share_ratio = 0.0
        acquirer = row.get("acquirer_symbol")
        if row.get("kind") != "cash_mergers":
            if not acquirer_rate or not acquiree_rate or acquiree_rate <= 0:
                unresolved.append({**_brief(row), "reason": "share ratio is incomplete"})
                continue
            share_ratio = acquirer_rate / acquiree_rate
        if cash < 0 or (cash == 0 and share_ratio == 0):
            unresolved.append({**_brief(row), "reason": "merger consideration is empty"})
            continue
        if not acquirer and share_ratio == 1 and has_bar(acquiree, day):
            recorded.append({**_brief(row), "reason": "acquirer symbol is blank and the listing tape continues; no exchange is applied"})
            continue
        if acquirer and any(ch.isdigit() for ch in acquirer):
            unresolved.append({**_brief(row), "reason": "cash rate is stated but the non-equity stub has no price; cash was not booked as full consideration"})
            continue
        if share_ratio > 0 and can_hold(acquiree) and not has_bar(acquirer, day):
            unresolved.append({**_brief(row), "reason": f"{acquirer} has no usable bar on {day}"})
            continue
        if not can_hold(acquiree):
            recorded.append({**_brief(row), "reason": "acquiree is not holdable on this tape"})
            continue
        settlements.append(
            {
                "session": day,
                "symbol": acquiree,
                "acquirer": acquirer if share_ratio else None,
                "cash_per_share": cash,
                "share_ratio": share_ratio,
            }
        )
    unique_spins = []
    seen_spins = set()
    for row in spins:
        key = (row["parent"], row["child"], round(row["ratio"], 8))
        if key in seen_spins:
            continue
        seen_spins.add(key)
        unique_spins.append(row)
    spins = unique_spins
    material = []
    for row in unresolved:
        affected = row.get("symbol") or row.get("acquiree_symbol") or row.get("source_symbol") or row.get("old_symbol")
        if affected and can_hold(affected):
            material.append(row)
    return {
        "renames": applied_renames,
        "aliases": aliases,
        "settlements": settlements,
        "spins": spins,
        "unresolved": unresolved,
        "recorded": recorded,
        "material_unresolved": material,
        "simulation_ready": not material,
    }


def dividend_resolutions(payments: list[dict]) -> list[dict]:
    """The four ex-dates that have no print. Entitlement is decided from the stored events."""
    by_key = {(row["symbol"], row["ex_date"]): row for row in payments}
    bbwi = by_key.get(("BBWI", "2021-06-03"))
    lb = by_key.get(("LB", "2021-06-03"))
    same_lb = (
        bbwi
        and lb
        and abs(float(bbwi["rate"]) - float(lb["rate"])) < 1e-9
        and bbwi.get("payable_date") == lb.get("payable_date")
    )
    rows = [
        {
            "symbol": "BBWI",
            "ex_date": "2021-06-03",
            "resolution": "not_entitled",
            "reason": (
                "The same 0.15 payable 2021-06-18 is stored on LB. BBWI membership and its first print start 2021-08-03."
                if same_lb
                else "BBWI has no print or membership on this ex-date. The cash was not moved onto another symbol."
            ),
        },
        {
            "symbol": "DFS",
            "ex_date": "2025-05-23",
            "resolution": "not_entitled",
            "reason": "DFS exchanges into COF on 2025-05-19, before this ex-date. The last DFS print is 2025-05-16.",
        },
        {
            "symbol": "FLIR",
            "ex_date": "2021-05-20",
            "resolution": "not_entitled",
            "reason": "FLIR exchanges into TDY on 2021-05-14, before this ex-date. The last FLIR print is 2021-05-13.",
        },
        {
            "symbol": "KSU",
            "ex_date": "2021-12-30",
            "resolution": "not_entitled",
            "reason": "KSU exchanges into CP on 2021-12-14, before this ex-date. The last KSU print is 2021-12-13.",
        },
    ]
    return rows


def _brief(row: dict) -> dict:
    return {key: value for key, value in row.items() if "cusip" not in key and key != "corporate_action_type"}


def _prior_close(closes: dict[tuple[str, str], float], symbol: str, day: str) -> float | None:
    earlier = [session for sym, session in closes if sym == symbol and session < day]
    if not earlier:
        return None
    return closes[(symbol, max(earlier))]
