"""Hypothetical shadow books. This module never submits a brokerage order.

Activation, from this directory, when a person chooses to start it:

    python shadow_runner.py --activate

Running the file without ``--activate`` prints that command and exits.
``--activate`` records the activation clock. The first decision is the first
regular close strictly after that clock, and the first fill is the following
session's open. Sessions that already closed are not backfilled. The command
is not a scheduler and it cannot submit an order.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ABC = HERE.parent / "nyse_cash_abc"
sys.path.insert(0, str(ABC))

from engine import INITIAL_CASH, SLIP, Bar, usable  # noqa: E402

from metrics import spy_total_return_paid  # noqa: E402
from stitch import simulate_continuous  # noqa: E402

ORDERS_ENABLED = False
HORIZON_SESSIONS = 252
FOLD_SESSIONS = 63
CUTOFF = "2026-10-14"
BOOKS = ("selector", "baseline_A", "spy")
ACTIVATE_COMMAND = "python shadow_runner.py --activate"
STATE_DIR = HERE / "shadow_state"
NY = ZoneInfo("America/New_York")
PROTECTED_NAMES = {"market_data.db", "bars.sqlite", ".env"}


class ShadowError(RuntimeError):
    pass


def market_close(session: str) -> datetime:
    """Regular-session close, 16:00 America/New_York."""
    year, month, day = (int(part) for part in session.split("-"))
    return datetime(year, month, day, 16, 0, tzinfo=NY)


def first_decision_session(activated_at: datetime, sessions: list[str]) -> str | None:
    """First regular session whose close is strictly after activation.

    Sessions that already closed are not returned. They are not prospective decisions.
    """
    if activated_at.tzinfo is None:
        raise ShadowError("activation time must include a timezone")
    stamp = activated_at.astimezone(NY)
    for day in sessions:
        if market_close(day) > stamp:
            return day
    return None


def execution_session(decision_session: str, sessions: list[str]) -> str | None:
    """The following regular session, whose open is the first fill."""
    later = [day for day in sessions if day > decision_session]
    return later[0] if later else None


def sessions_ready(activated_at: datetime, sessions: list[str], asof: datetime) -> list[str]:
    """Closed sessions from the first post-activation decision through ``asof``.

    Nothing before that first close is included.
    """
    first = first_decision_session(activated_at, sessions)
    if first is None:
        return []
    asof_ny = asof.astimezone(NY)
    return [day for day in sessions if day >= first and market_close(day) <= asof_ny]


def training_window(calendar: list[str], cutoff: str, warmup: int, train: int) -> list[str]:
    """Sessions used to score one cutoff. The last session is the cutoff."""
    if cutoff not in calendar:
        raise ShadowError(f"{cutoff} is not a completed session")
    end = calendar.index(cutoff)
    start = end - (warmup + train) + 1
    if start < 0:
        raise ShadowError("not enough sessions before the cutoff")
    dates = calendar[start : end + 1]
    if dates[-1] != cutoff or len(dates) != warmup + train or any(day > cutoff for day in dates):
        raise ShadowError("training window includes a later session")
    return dates


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _digest(payload: dict) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def normalize_dividend(event: dict) -> dict | None:
    """Return a lot, or None when the payable date is missing.

    A source payable date before the ex-date is credited on the ex-date.
    """
    pay = event.get("pay_session")
    if not pay:
        return None
    ex = str(event["ex_session"])
    pay = str(pay)
    flag = None
    if pay < ex:
        pay = ex
        flag = "payable_before_ex"
    return {
        "symbol": event["symbol"],
        "ex_session": ex,
        "pay_session": pay,
        "rate": float(event["rate"]),
        "flag": flag,
    }


def _bar(raw: dict | None) -> Bar | None:
    if not raw:
        return None
    bar = Bar(float(raw["open"]), float(raw["high"]), float(raw["low"]), float(raw["close"]))
    return bar if usable(bar) else None


def _panel(bundles: list[dict]) -> dict:
    dates = [row["session"] for row in bundles]
    symbols = sorted({sym for row in bundles for sym in row["bars"] if sym != "SPY"})
    bars = {sym: [] for sym in symbols}
    member = {sym: [] for sym in symbols}
    spy_bars: dict[str, Bar] = {}
    lots = []
    spy_lots = []
    missing_payable = []
    for row in bundles:
        for sym in symbols:
            bars[sym].append(_bar(row["bars"].get(sym)))
            member[sym].append(bool(row.get("member", {}).get(sym, False)))
        spy = _bar(row["bars"].get("SPY"))
        if spy is None:
            raise ShadowError(f"SPY bar missing on {row['session']}")
        spy_bars[row["session"]] = spy
        for event in row.get("dividends") or []:
            lot = normalize_dividend(event)
            if lot is None:
                missing_payable.append({"session": row["session"], "symbol": event.get("symbol")})
                continue
            if lot["symbol"] == "SPY":
                spy_lots.append(lot)
            else:
                lots.append(lot)
    return {
        "dates": dates,
        "bars": bars,
        "member": member,
        "spy_bars": spy_bars,
        "lots": lots,
        "spy_lots": spy_lots,
        "missing_payable": missing_payable,
    }


def _endpoint_value(result: dict) -> dict:
    """Value after the horizon close. Positions are sold. Unpaid dividends stay in equity."""
    receivables = result.get("receivables") or []
    unpaid = float(sum(row["amount"] for row in receivables if not row.get("paid")))
    cash = float(result["cash"])
    equity = float(result["ending_equity"])
    return {
        "position_liquidation": "final session close",
        "spendable_cash": cash,
        "unpaid_receivables": unpaid,
        "equity": equity,
        "equity_identity": "spendable cash plus unpaid receivables after positions are sold",
    }


def _spec_or_none(choice: dict) -> dict | None:
    if choice.get("choice") != "parameters" or not choice.get("selected"):
        return None
    selected = choice["selected"]
    return {
        "ma_win": int(selected["ma_win"]),
        "max_hold": int(selected["max_hold"]),
        "atr_mult": float(selected["atr_mult"]),
    }


def _schedules(warmup: int, n_shadow: int, selections: dict[int, dict], fold_sessions: int, baseline: dict) -> tuple[list, list]:
    selector: list = [None] * warmup
    fixed: list = [None] * warmup
    for index in range(n_shadow):
        choice = selections.get(index // fold_sessions)
        if choice is None:
            raise ShadowError("selection missing for this fold")
        selector.append(_spec_or_none(choice))
        fixed.append(dict(baseline))
    return selector, fixed


def _simulate(panel: dict, shadow_dates: list[str], schedules: tuple[list, list], *, decision_start: int, liquidate: bool, slip: float, initial_cash: float, atr_period: int, max_names: int, sleeve: float) -> dict:
    selector_schedule, baseline_schedule = schedules
    common = {
        "decision_start": decision_start,
        "slip": slip,
        "initial_cash": initial_cash,
        "atr_period": atr_period,
        "max_names": max_names,
        "sleeve": sleeve,
        "dividend_lots": panel["lots"],
        "liquidate_terminal": liquidate,
        "open_book": True,
    }
    spy_bars = {day: panel["spy_bars"][day] for day in shadow_dates}
    return {
        "selector": simulate_continuous(panel["bars"], panel["member"], panel["dates"], [panel["spy_bars"][day].close for day in panel["dates"]], selector_schedule, **common),
        "baseline_A": simulate_continuous(panel["bars"], panel["member"], panel["dates"], [panel["spy_bars"][day].close for day in panel["dates"]], baseline_schedule, **common),
        "spy": spy_total_return_paid(
            shadow_dates,
            spy_bars,
            panel["spy_lots"],
            slip,
            initial_cash,
            liquidate_terminal=liquidate,
            require_lots=False,
            open_book=True,
        ),
    }


def _line_for(book: str, session: str, index: int, status: str, digest: str, result: dict | None, prior: dict | None, spec: dict | None, reason: str | None) -> dict:
    if status == "blocked":
        base = prior or {}
        return {
            "book": book,
            "session": session,
            "session_index": index,
            "status": status,
            "reason": reason,
            "inputs_hash": digest,
            "decision": None,
            "executions": [],
            "exits": [],
            "dividends_booked": [],
            "dividends_paid": [],
            "cash": base.get("cash", None),
            "receivables": base.get("receivables", []),
            "equity": base.get("equity", None),
            "positions": base.get("positions", []),
            "pending": base.get("pending", []),
            "spec": spec,
        }
    assert result is not None
    if book == "spy":
        positions = []
        if result.get("shares"):
            positions = [{"symbol": "SPY", "shares": result["shares"]}]
        receivables = result.get("receivables", [])
        booked = [row for row in receivables if row.get("ex_session") == session]
        paid = [row for row in result.get("dividends", []) if row["session"] == session]
        return {
            "book": book,
            "session": session,
            "session_index": index,
            "status": status,
            "reason": None,
            "inputs_hash": digest,
            "decision": {"buy_open": index == 2},
            "executions": [row for row in result["executions"] if row["session"] == session],
            "exits": [],
            "dividends_booked": booked,
            "dividends_paid": paid,
            "cash": result["cash"],
            "receivables": receivables,
            "equity": result["ending_equity"],
            "positions": positions,
            "pending": [],
            "spec": None,
        }
    booked = [row for row in result["receivables"] if row["ex_session"] == session]
    paid = [row for row in result["receivables"] if row["paid"] and row["pay_session"] == session]
    return {
        "book": book,
        "session": session,
        "session_index": index,
        "status": status,
        "reason": None,
        "inputs_hash": digest,
        "decision": {"spec": spec, "pending": result["pending"]},
        "executions": [row for row in result["executions"] if row["session"] == session],
        "exits": [row for row in result["trade_log"] if row["exit_date"] == session],
        "dividends_booked": booked,
        "dividends_paid": paid,
        "cash": result["cash"],
        "receivables": result["receivables"],
        "equity": result["ending_equity"],
        "positions": result["positions"],
        "pending": result["pending"],
        "spec": spec,
    }


class ShadowRunner:
    """Append-only hypothetical selector, fixed A, and SPY books."""

    def __init__(
        self,
        directory: Path,
        *,
        warmup: list[dict],
        selector,
        baseline: dict,
        horizon: int = HORIZON_SESSIONS,
        fold_sessions: int = FOLD_SESSIONS,
        slip: float = SLIP,
        initial_cash: float = INITIAL_CASH,
        atr_period: int = 14,
        max_names: int = 15,
        sleeve: float = 0.95,
        embed_inputs: bool = True,
        loader=None,
        first_decision_session: str | None = None,
    ) -> None:
        if ORDERS_ENABLED:
            raise ShadowError("brokerage orders are disabled")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.warmup = list(warmup)
        self.selector = selector
        self.baseline = dict(baseline)
        self.horizon = int(horizon)
        self.fold_sessions = int(fold_sessions)
        self.slip = float(slip)
        self.initial_cash = float(initial_cash)
        self.atr_period = int(atr_period)
        self.max_names = int(max_names)
        self.sleeve = float(sleeve)
        self.embed_inputs = embed_inputs
        self.loader = loader
        self.first_decision_session = first_decision_session
        self.decisions = self.directory / "decisions.jsonl"
        self.selections = self.directory / "selections.jsonl"
        self.rejections = self.directory / "rejections.jsonl"
        self._write_once(
            self.directory / "runner.json",
            {
                "orders_enabled": False,
                "horizon_sessions": self.horizon,
                "fold_sessions": self.fold_sessions,
                "slip_bps_per_side": self.slip * 10_000.0,
                "initial_cash": self.initial_cash,
                "baseline_A": self.baseline,
                "atr_period": self.atr_period,
                "max_names": self.max_names,
                "sleeve": self.sleeve,
                "embed_inputs": embed_inputs,
                "activation_cutoff": CUTOFF,
                "backfill_prospective": False,
            },
        )
        stored_warmup = [{"session": row["session"], "inputs_hash": _digest(row)} for row in warmup]
        self._write_once(self.directory / "warmup.json", warmup if embed_inputs else stored_warmup)

    def _write_once(self, path: Path, payload) -> None:
        text = json.dumps(payload, sort_keys=True)
        if path.exists():
            if path.read_text(encoding="utf-8") != text:
                raise ShadowError(f"{path.name} already exists and does not match")
            return
        path.write_text(text, encoding="utf-8")

    def _read_jsonl(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line:
                rows.append(json.loads(line))
        return rows

    def _append(self, path: Path, rows: list[dict]) -> None:
        text = "".join(_canonical(row) + "\n" for row in rows)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()

    def selection_map(self) -> dict[int, dict]:
        found: dict[int, dict] = {}
        for row in self._read_jsonl(self.selections):
            found[int(row["fold"])] = row
        return found

    def _sessions(self) -> list[dict]:
        """Last line per book, grouped in log order. A blocked session ends the book."""
        grouped: dict[str, dict[str, dict]] = {}
        order: list[str] = []
        for row in self._read_jsonl(self.decisions):
            if row["session"] not in grouped:
                grouped[row["session"]] = {}
                order.append(row["session"])
            grouped[row["session"]][row["book"]] = row
        ready = []
        for session in order:
            books = grouped[session]
            if set(books) != set(BOOKS):
                raise ShadowError(f"{session} does not have all three books")
            if any(books[name]["status"] == "blocked" for name in BOOKS):
                ready.append({"session": session, "books": books, "blocked": True})
                break
            ready.append({"session": session, "books": books, "blocked": False})
        return ready

    def _bundle_for_line(self, line: dict) -> dict:
        if "inputs" in line:
            return line["inputs"]
        if self.loader is None:
            raise ShadowError("recovery needs the research snapshot loader")
        bundle = self.loader(line["session"])
        if _digest(bundle) != line["inputs_hash"]:
            raise ShadowError(f"snapshot for {line['session']} no longer matches the log")
        return bundle

    def _accepted_bundles(self) -> list[dict]:
        bundles = []
        for row in self._sessions():
            if row["blocked"]:
                break
            bundles.append(self._bundle_for_line(row["books"]["selector"]))
        return bundles

    def recover(self) -> dict:
        """Replay the log. Mismatch stops the runner and does not rewrite it."""
        if ORDERS_ENABLED:
            raise ShadowError("brokerage orders are disabled")
        accepted = self._accepted_bundles()
        sessions = self._sessions()
        if not accepted:
            return {"sessions": 0, "blocked": bool(sessions and sessions[-1]["blocked"]), "books": {}}
        checked = self._run(accepted, liquidate=len(accepted) == self.horizon)
        last = sessions[len(accepted) - 1]["books"]
        for name in BOOKS:
            logged = last[name]
            got = checked[name]
            equity = got["ending_equity"]
            cash = got["cash"] if name == "spy" else got["cash"]
            if abs(float(logged["equity"]) - float(equity)) > 1e-6 or abs(float(logged["cash"]) - float(cash)) > 1e-6:
                raise ShadowError(f"recovery mismatch on {name}")
        return {"sessions": len(accepted), "blocked": bool(sessions and sessions[-1]["blocked"]), "books": last}

    def _ensure_selection(self, fold: int, cutoff: str) -> dict:
        found = self.selection_map()
        if fold in found:
            return found[fold]
        choice = self.selector(fold, cutoff)
        if choice.get("choice") not in {"parameters", "cash"}:
            raise ShadowError("selection must be parameters or cash")
        row = {"fold": fold, "cutoff": cutoff, "choice": choice["choice"], "selected": choice.get("selected")}
        self._append(self.selections, [row])
        return row

    def _run(self, shadow: list[dict], *, liquidate: bool) -> dict:
        panel = _panel(self.warmup + shadow)
        if panel["missing_payable"]:
            raise ShadowError("missing payable date")
        schedules = _schedules(len(self.warmup), len(shadow), self.selection_map(), self.fold_sessions, self.baseline)
        return _simulate(
            panel,
            [row["session"] for row in shadow],
            schedules,
            decision_start=len(self.warmup),
            liquidate=liquidate,
            slip=self.slip,
            initial_cash=self.initial_cash,
            atr_period=self.atr_period,
            max_names=self.max_names,
            sleeve=self.sleeve,
        )

    def advance(self, bundle: dict) -> list[dict]:
        """Append one session for all three books, or refuse a conflicting line."""
        if ORDERS_ENABLED:
            raise ShadowError("brokerage orders are disabled")
        session = bundle["session"]
        if self.first_decision_session and session < self.first_decision_session:
            self._append(
                self.rejections,
                [{
                    "session": session,
                    "status": "rejected",
                    "reason": "backfill",
                    "detail": "close is not strictly after activation; not a prospective decision",
                }],
            )
            raise ShadowError("refusing to backfill a decision")
        self.recover()
        if self.warmup and session <= self.warmup[-1]["session"]:
            raise ShadowError("shadow session is not after warm-up")
        sessions = self._sessions()
        if sessions and sessions[-1]["blocked"] and sessions[-1]["session"] != session:
            raise ShadowError("a blocked session must be corrected before the next session")
        if len([row for row in sessions if not row["blocked"]]) >= self.horizon and not (sessions and sessions[-1]["blocked"] and sessions[-1]["session"] == session):
            raise ShadowError("evaluation horizon is complete")
        existing = self._read_jsonl(self.decisions)
        same = [row for row in existing if row["session"] == session and row["book"] == "selector"]
        digest = _digest(bundle)
        if same and same[-1]["status"] != "blocked" and same[-1]["inputs_hash"] == digest:
            latest = {}
            for row in existing:
                if row["session"] == session:
                    latest[row["book"]] = row
            return [latest[name] for name in BOOKS]
        if same and same[-1]["status"] != "blocked" and same[-1]["inputs_hash"] != digest:
            raise ShadowError("session already logged with different inputs")
        accepted = self._accepted_bundles()
        index = len(accepted) + 1
        fold = (index - 1) // self.fold_sessions
        if fold == 0 and self.warmup:
            cutoff = self.warmup[-1]["session"]
        elif fold > 0:
            cutoff = accepted[(fold * self.fold_sessions) - 1]["session"]
        else:
            cutoff = bundle["session"]
        self._ensure_selection(fold, cutoff)
        complete = [row for row in sessions if not row["blocked"]]
        prior = complete[-1]["books"] if complete else {
            name: {"cash": self.initial_cash, "equity": self.initial_cash, "positions": [], "receivables": [], "pending": []}
            for name in BOOKS
        }
        problem = self._block_reason(bundle, prior)
        spec = _spec_or_none(self.selection_map()[fold])
        status = "corrected" if same else "complete"
        if problem:
            lines = [_line_for(name, session, index, "blocked", digest, None, prior.get(name), spec if name == "selector" else self.baseline if name == "baseline_A" else None, problem) for name in BOOKS]
            stored = self._attach_inputs(lines, bundle)
            self._append(self.decisions, stored)
            return stored
        results = self._run(accepted + [bundle], liquidate=index == self.horizon)
        lines = []
        for name in BOOKS:
            book_spec = spec if name == "selector" else self.baseline if name == "baseline_A" else None
            lines.append(_line_for(name, session, index, status, digest, results[name], None, book_spec, None))
        stored = self._attach_inputs(lines, bundle)
        if index == self.horizon:
            for name, line in zip(BOOKS, stored):
                line["endpoint"] = _endpoint_value(results[name])
        self._append(self.decisions, stored)
        if index % self.fold_sessions == 0 and index < self.horizon:
            self._ensure_selection(fold + 1, session)
        return stored

    def _attach_inputs(self, lines: list[dict], bundle: dict) -> list[dict]:
        if not self.embed_inputs:
            return lines
        lines[0] = dict(lines[0])
        lines[0]["inputs"] = bundle
        return lines

    def _block_reason(self, bundle: dict, prior: dict) -> str | None:
        if bundle.get("membership_status") == "missing":
            return "missing_membership"
        if bundle.get("bars_status") == "missing":
            return "missing_bar"
        for event in bundle.get("dividends") or []:
            if normalize_dividend(event) is None:
                return "missing_payable"
        held = []
        selector = prior.get("selector") or {}
        for pos in selector.get("positions") or []:
            held.append(pos["symbol"])
        baseline = prior.get("baseline_A") or {}
        for pos in baseline.get("positions") or []:
            if pos["symbol"] not in held:
                held.append(pos["symbol"])
        spy = prior.get("spy") or {}
        if any(pos.get("symbol") == "SPY" and pos.get("shares") for pos in spy.get("positions") or []):
            if _bar(bundle["bars"].get("SPY")) is None:
                return "missing_bar"
        for sym in held:
            if _bar(bundle["bars"].get(sym)) is None:
                return "missing_bar"
        return None

    def replay_slip(self, slip: float) -> dict:
        """Mark the stored decisions at another slippage. Does not append or reselect."""
        before = self.decisions.read_bytes() if self.decisions.exists() else b""
        accepted = self._accepted_bundles()
        if not accepted:
            return {}
        panel = _panel(self.warmup + accepted)
        schedules = _schedules(len(self.warmup), len(accepted), self.selection_map(), self.fold_sessions, self.baseline)
        marked = _simulate(
            panel,
            [row["session"] for row in accepted],
            schedules,
            decision_start=len(self.warmup),
            liquidate=len(accepted) == self.horizon,
            slip=float(slip),
            initial_cash=self.initial_cash,
            atr_period=self.atr_period,
            max_names=self.max_names,
            sleeve=self.sleeve,
        )
        after = self.decisions.read_bytes() if self.decisions.exists() else b""
        if after != before:
            raise ShadowError("slippage replay wrote the decision log")
        return {name: marked[name]["ending_equity"] for name in BOOKS}


def score_cutoff(cutoff: str) -> dict:
    """Score the frozen 27-candidate rule through ``cutoff``. Read-only. Not used by the tests."""
    if ORDERS_ENABLED:
        raise ShadowError("brokerage orders are disabled")
    from cash_lots import dividend_lots, lots_on
    from data_access import actions_for, load_market, panel_for
    from select import candidates, select_winner

    market = load_market()
    cfg = market["config"]
    dates = training_window(market["calendar"], cutoff, int(cfg["warmup_sessions"]), int(cfg["train_sessions"]))
    lots, problems = dividend_lots(market["divs"], market["splits"], market["calendar"])
    if any(row["problem"] == "missing_payable" for row in problems):
        raise ShadowError("missing payable date; selection blocked")
    usable_lots = lots_on(dates, lots)
    bars, member, spy = panel_for(dates, market["lookup"], market["membership"])
    actions = actions_for(dates, market["divs"], market["splits"])
    warmup = int(cfg["warmup_sessions"])
    rows = []
    for spec in candidates(cfg):
        schedule = [dict(spec) if i >= warmup else None for i in range(len(dates))]
        out = simulate_continuous(
            bars,
            member,
            dates,
            spy,
            schedule,
            decision_start=warmup,
            distributions=actions["distributions"],
            renames=actions["renames"],
            dividend_lots=usable_lots,
            atr_period=14,
            max_names=15,
            sleeve=0.95,
            slip=SLIP,
            initial_cash=INITIAL_CASH,
        )
        if out["end"] != cutoff or out["sessions"] != int(cfg["train_sessions"]):
            raise ShadowError("training score includes warm-up or a later session")
        if not out["reconciled"] or out["distribution_unpriced"]:
            raise ShadowError("training ledger did not reconcile")
        rows.append(
            {
                "order": spec["order"],
                "ma_win": spec["ma_win"],
                "max_hold": spec["max_hold"],
                "atr_mult": spec["atr_mult"],
                "net_return": out["net_return"],
                "max_drawdown": out["max_drawdown"],
                "turnover": out["turnover"],
                "trades": out["trades"],
                "reconciled": out["reconciled"],
                "distribution_unpriced": out["distribution_unpriced"],
            }
        )
    choice = select_winner(rows, cfg)
    choice["cutoff"] = cutoff
    return choice


def activate(now: datetime | None = None) -> int:
    """One manual pass. Does not schedule a process and does not send an order.

    The first decision is the first regular close strictly after this call.
    Sessions that already closed are not written as prospective decisions.
    """
    if ORDERS_ENABLED:
        raise ShadowError("brokerage orders are disabled")
    from data_access import load_market

    asof = (now or datetime.now(NY)).astimezone(NY)
    activation_path = STATE_DIR / "activation.json"
    if activation_path.exists():
        activated_at = datetime.fromisoformat(json.loads(activation_path.read_text(encoding="utf-8"))["activated_at"])
    else:
        activated_at = asof
    market = load_market()
    calendar = list(market["calendar"])
    if CUTOFF not in calendar:
        raise ShadowError("snapshot does not contain the research cutoff")
    inbox_sessions = sorted(_inbox_sessions(STATE_DIR / "inbox"))
    known = list(dict.fromkeys(calendar + inbox_sessions))
    ready = sessions_ready(activated_at, known, asof)
    first = first_decision_session(activated_at, known)
    history = [day for day in calendar if day <= CUTOFF]
    warmup_dates = history[-int(market["config"]["warmup_sessions"]) :]

    def load_bundle(session: str) -> dict:
        inbox = _read_inbox(STATE_DIR / "inbox", session)
        if inbox is not None:
            return inbox
        if session <= CUTOFF:
            return _bundle_from_market(market, session)
        raise ShadowError(f"no ingested observation for {session}")

    runner = ShadowRunner(
        STATE_DIR,
        warmup=[load_bundle(day) for day in warmup_dates],
        selector=lambda fold, cutoff: score_cutoff(cutoff),
        baseline=market["config"]["baseline_A"],
        embed_inputs=False,
        loader=load_bundle,
        first_decision_session=first,
    )
    _write_activation(STATE_DIR / "activation.json", activated_at, first)
    if not ready:
        print("no market close strictly after activation")
        return 0
    logged = {row["session"] for row in runner._read_jsonl(runner.decisions)}
    for session in ready:
        if session in logged or session < (first or session):
            continue
        runner.advance(load_bundle(session))
        print("appended", session)
    return 0


def _write_activation(path: Path, activated_at: datetime, first: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "activated_at": activated_at.isoformat(),
        "first_decision_session": first,
        "backfill_prospective": False,
        "orders_enabled": False,
    }
    text = json.dumps(payload, sort_keys=True)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("activated_at") != payload["activated_at"]:
            raise ShadowError("activation time is already recorded")
        existing["first_decision_session"] = first
        existing["backfill_prospective"] = False
        path.write_text(json.dumps(existing, sort_keys=True), encoding="utf-8")
        return
    path.write_text(text, encoding="utf-8")


def ingest_observation(store: Path, observation: dict) -> dict:
    """Append one future observation under ``store``. Does not touch protected databases.

    A missing bar, point-in-time membership update, or payable date is logged
    and marked blocked. The frozen strategy files are not an input.
    """
    if ORDERS_ENABLED:
        raise ShadowError("brokerage orders are disabled")
    resolved = store.resolve()
    if resolved.name in PROTECTED_NAMES or any(part.name in PROTECTED_NAMES for part in resolved.parents):
        raise ShadowError("refusing to write production or snapshot data")
    session = str(observation["session"])
    missing = []
    if not observation.get("bars"):
        missing.append({"field": "bars", "reason": "missing_bar"})
    if "membership" not in observation or observation.get("membership") is None:
        missing.append({"field": "membership", "reason": "missing_membership"})
    for event in observation.get("dividends") or []:
        if not event.get("pay_session"):
            missing.append({"field": "payable", "symbol": event.get("symbol"), "reason": "missing_payable"})
    inbox = resolved / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / f"{session}.json"
    if path.exists():
        raise ShadowError("observation already ingested; not rewritten")
    record = {"session": session, "blocked": bool(missing), "missing": missing, "observation": observation}
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    if missing:
        with (inbox / "blocks.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(_canonical({"session": session, "missing": missing}) + "\n")
    return record


def _inbox_sessions(inbox: Path) -> list[str]:
    if not inbox.is_dir():
        return []
    return [path.stem for path in sorted(inbox.glob("*.json"))]


def _read_inbox(inbox: Path, session: str) -> dict | None:
    path = inbox / f"{session}.json"
    if not path.exists():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("blocked"):
        observation = dict(record["observation"])
        reasons = {row["reason"] for row in record.get("missing") or []}
        if "missing_membership" in reasons:
            observation["membership_status"] = "missing"
        if "missing_bar" in reasons:
            observation["bars_status"] = "missing"
            observation["bars"] = observation.get("bars") or {"SPY": None}
        if isinstance(observation.get("membership"), dict) and "member" not in observation:
            observation["member"] = observation["membership"]
        return observation
    observation = dict(record["observation"])
    if isinstance(observation.get("membership"), dict) and "member" not in observation:
        observation["member"] = observation["membership"]
    return observation


def _bundle_from_market(market: dict, session: str) -> dict:
    from cash_lots import dividend_lots
    from data_access import member_on

    lots, _problems = dividend_lots(market["divs"], market["splits"], market["calendar"])
    bars = {}
    member = {}
    for symbol, series in market["lookup"].items():
        raw = series.get(session)
        if raw is None or not usable(raw):
            continue
        bars[symbol] = {"open": raw.open, "high": raw.high, "low": raw.low, "close": raw.close}
        if symbol != "SPY":
            member[symbol] = member_on(market["membership"], symbol, session)
    dividends = []
    for lot in lots:
        if lot["ex_session"] == session:
            dividends.append({"symbol": lot["symbol"], "ex_session": lot["ex_session"], "pay_session": lot["pay_session"], "rate": lot["rate"]})
    from cash_lots import spy_lots

    for lot in spy_lots(market["calendar"])[0]:
        if lot["ex_session"] == session:
            dividends.append({"symbol": "SPY", "ex_session": lot["ex_session"], "pay_session": lot["pay_session"], "rate": lot["rate"]})
    return {"session": session, "bars": bars, "member": member, "dividends": dividends}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--activate" not in args:
        print(ACTIVATE_COMMAND)
        print("shadow runner is not started")
        return 0
    return activate()


if __name__ == "__main__":
    raise SystemExit(main())
