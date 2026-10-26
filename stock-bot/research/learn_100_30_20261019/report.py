"""Morning report from a finished or stopped payload. Does not resimulate."""

from __future__ import annotations


def write_report(payload: dict) -> str:
    status = payload["status"]
    verdict = payload.get("verdict")
    learn = payload.get("learner")
    base = payload.get("baseline")
    spy = payload.get("spy")
    lines = [
        "# Walk-forward learning study — morning report",
        "",
        f"1. **{status}**.",
        f"2. {payload['answer_learning']}",
        f"3. {payload['answer_market']}",
        f"4. {payload['answer_unknown']}",
        f"5. {payload['answer_next']}",
        "",
        f"Research verdict: **{verdict}**. This is not permission to deploy.",
        "",
        payload["scope"],
        "",
        "## What was tested",
        "",
        payload["design"],
        "",
    ]
    if learn and base:
        lines.extend(
            [
                "## Stitched evaluation",
                "",
                _table(
                    ["Book", "Net return", "Net profit", "Max DD", "Recovery", "Sharpe", "Costs", "Turnover", "Exposure"],
                    [
                        _book_line("Learned selector", learn),
                        _book_line("Fixed baseline", base),
                        _book_line("First-window fixed", payload["first_fixed"]),
                        _book_line("SPY total return", spy) if spy else ["SPY total return", "unavailable", "", "", "", "", "", "", ""],
                        ["VTI total return", "unavailable", "", "", "", "", "", "", ""],
                    ],
                ),
                "",
                f"Learner minus baseline: {payload['learner_minus_baseline_pp']:.2f} percentage points.",
                f"10 bps per side, no reselection: learner {pct(payload['stress']['learner']['net_return'])}, baseline {pct(payload['stress']['baseline']['net_return'])}, SPY {pct(payload['stress']['spy']['net_return'])}.",
                "",
                f"Chronological halves ({payload['half_fold_counts'][0]} then {payload['half_fold_counts'][1]} folds): "
                f"learner {pct(payload['halves']['learner_first'])} / {pct(payload['halves']['learner_second'])}; "
                f"baseline {pct(payload['halves']['baseline_first'])} / {pct(payload['halves']['baseline_second'])}.",
                "",
                f"Worst learner fold: {payload['worst_fold']}.",
                "",
                payload["concentration"],
                "",
                f"Selection dates: {payload['decision_dates']}. Eligible-candidate counts are in the fold table. "
                "Adjacent 100-session training windows overlap by 70 sessions, so these dates are not independent market samples.",
                "",
                "## Folds",
                "",
                _fold_table(payload["folds"]),
                "",
                "## Calendar-year slices of the continuous book",
                "",
                _year_table(payload["years"]),
                "",
            ]
        )
    if payload.get("hindsight"):
        lines.extend(["## Unattainable diagnostic", "", payload["hindsight"], ""])
    lines.extend(
        [
            "## Accounting and defects",
            "",
            payload.get("defects_text", "No evaluation book was scored."),
            "",
            "## Previously exposed history",
            "",
            payload["exposure_text"],
            "",
            "## Reproduction",
            "",
            "From `C:\\Users\\Owner\\PythonTrading\\stock-bot`:",
            "",
            "```",
            "py -3 research\\learn_100_30_20261019\\test_learning.py",
            "py -3 research\\learn_100_30_20261019\\runner.py",
            "```",
            "",
            "The runner resumes a partial run only when the specification and data hashes still match. It refuses a mismatch. It does not create a scheduled task.",
            "",
            "## Production state",
            "",
            "No production file, book setting, process, order, scheduled task, commit, or push was changed by this study.",
            "",
        ]
    )
    return "\n".join(lines)


def _book_line(name: str, row: dict) -> list[str]:
    recovery = "unrecovered" if not row.get("recovered") else f"{row.get('recovery_sessions_from_trough')} sessions"
    sharpe = "n/a" if row.get("sharpe_sqrt252") is None else f"{row['sharpe_sqrt252']:.2f}"
    turnover = "" if row.get("turnover") is None else f"{row['turnover']:.2f}x"
    exposure = "" if row.get("avg_exposure") is None else f"{100 * row['avg_exposure']:.1f}%"
    costs = "" if row.get("cost_dollars") is None else f"${row['cost_dollars']:,.0f}"
    profit = "" if row.get("net_profit") is None else f"${row['net_profit']:,.0f}"
    return [name, pct(row.get("net_return")), profit, pct(row.get("max_drawdown")), recovery, sharpe, costs, turnover, exposure]


def _fold_table(rows: list[dict]) -> str:
    head = ["Fold", "Eval", "Choice", "Train trades", "Fallback", "Return", "Max DD", "Costs"]
    body = []
    for row in rows:
        body.append(
            [
                str(row["fold"]),
                f"{row['start']} → {row['end']}",
                str(row.get("candidate_id")),
                str(row.get("train_completed")),
                str(row.get("fallback")),
                pct(row.get("net_return")),
                pct(row.get("max_drawdown")),
                f"${row.get('cost_dollars', 0):,.0f}",
            ]
        )
    return _table(head, body)


def _year_table(rows: list[dict]) -> str:
    body = []
    for row in rows:
        body.append(
            [
                row["year"],
                str(row["learner_sessions"]),
                pct(row["learner_return"]),
                pct(row["baseline_return"]),
                pct(row["spy_return"]) if row.get("spy_return") is not None else "n/a",
            ]
        )
    return _table(["Year", "Sessions", "Learner", "Baseline", "SPY"], body)


def _table(head: list[str], rows: list[list[str]]) -> str:
    line = "| " + " | ".join(head) + " |"
    rule = "| " + " | ".join("---" for _ in head) + " |"
    body = ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join([line, rule, *body])


def pct(value) -> str:
    if value is None:
        return "n/a"
    return f"{100.0 * float(value):.2f}%"
