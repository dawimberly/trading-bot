"""Localhost research UI. The backtest itself lives in lab.adapter and lab.runner."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from lab.adapter import data_health
from lab.duplicates import DuplicateSearch, find_duplicate, load_completed_folds, proposed_one_fold_spec
from lab.expansion import inventory
from lab.families import PILOT_NOTE, feasibility, specifications
from lab.jobs import enqueue, job_detail, list_jobs, set_control
from lab.reports import result_tables
from lab.specs import (
    UNTOUCHED_REASON,
    baseline_spec,
    default_research_spec,
    frozen_selector_spec,
    planned_rows,
    validate_spec,
)
from lab.store import connect

st.set_page_config(page_title="Strategy lab", layout="wide")
st.title("Strategy lab")
st.caption("Development research only. A high return here is not a best strategy and is not approved for production.")

page = st.sidebar.radio(
    "Section",
    ["Research plan", "Data health", "Experiment setup", "Jobs", "Results", "History"],
)


def _elapsed(job: dict) -> str:
    start = job.get("started_at")
    if not start:
        return "—"
    end = job.get("finished_at") or datetime.now(timezone.utc).isoformat()
    try:
        seconds = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except ValueError:
        return "—"
    return f"{max(seconds, 0.0):.0f}s"


def _remaining(job: dict) -> str:
    if job.get("status") not in ("queued", "running", "paused"):
        return "—"
    eta = job.get("eta_seconds")
    if eta is None:
        return "waiting for pilot"
    return f"{max(float(eta), 0.0):.0f}s"


def _show_duplicate(report: dict) -> None:
    fold = report["fold"]
    st.error(report["message"])
    st.write(
        f"Predefined selection for that training window: "
        f"{fold['selected']['ma_win']}/{fold['selected']['max_hold']}/{fold['selected']['atr_mult']}. "
        "Parameter order is the stored order. This is not a best-strategy label."
    )
    st.dataframe(pd.DataFrame(fold["training"]), hide_index=True)
    segment = fold["stitched_segment"]
    st.write(
        f"Fold {fold['fold']} of the continuous evaluation entered at ${segment['entering_equity']:,.2f} "
        f"and finished at ${segment['ending_equity']:,.2f} "
        f"(segment return {segment['net_return']:.4f}). "
        "That segment is not a fresh $100,000 one-fold book."
    )


if page == "Research plan":
    from lab.adapter import calendar

    st.header("Completed one-fold grid")
    st.write(
        "The proposed 27-candidate, one-fold MA grid uses the frozen triples, a 504-session training window, "
        "and one 63-session evaluation ending 2026-10-14. It is compared with the payable-date walk-forward before any queue."
    )
    report = find_duplicate(proposed_one_fold_spec(), calendar())
    if report is None:
        st.write("The proposed one-fold window does not match a stored fold. It was still not queued.")
    else:
        _show_duplicate(report)
    aggregate = load_completed_folds()["aggregate"]
    st.write(
        "The completed four-fold book, same grid and same selection rule: "
        f"selector return {aggregate['selector']['net_return']:.4f}, "
        f"fixed A {aggregate['baseline_A']['net_return']:.4f}, "
        f"SPY total return {aggregate['spy_total_return']['net_return']:.4f}."
    )

    st.header("Data expansion")
    info = inventory()
    span = info["span"]
    st.write(
        f"Frozen snapshot `{info['snapshot_id'][:12]}` covers {span['first_session']} through {span['last_session']} "
        f"({span['sessions']} sessions, {span['symbols']} symbols). It is not replaced by a new file."
    )
    st.subheader("Required for a longer validated history")
    st.dataframe(pd.DataFrame(info["requirements"]), hide_index=True)
    st.subheader("Sources")
    st.dataframe(pd.DataFrame(info["sources"]), hide_index=True)
    st.subheader("Market conditions")
    st.dataframe(pd.DataFrame(info["regimes"]), hide_index=True)
    actions = info["corporate_actions"]
    st.write(actions["para_tape"])
    st.write(actions["payable_exception"])
    st.write(f"Unapplied corporate actions stored with the snapshot: {actions['unapplied']['rows']}.")
    st.dataframe(pd.DataFrame([{"kind": key, "rows": value} for key, value in sorted(actions["unapplied"]["counts"].items())]), hide_index=True)
    if info["registered"]:
        st.write("Separately stored snapshots")
        st.dataframe(pd.DataFrame(info["registered"]), hide_index=True)
    else:
        st.write("No additional snapshot has been stored. The frozen file remains the only price snapshot.")

    st.header("Proposed families")
    st.write(PILOT_NOTE)
    st.write("Neither family is queued. A new regime is not in the snapshot, and the signals have not been scored.")
    matrix = feasibility(span["sessions"], span["first_session"], span["last_session"])
    st.dataframe(pd.DataFrame(matrix), hide_index=True)
    for spec in specifications():
        st.subheader(spec["family"])
        st.write(spec["signal"])
        st.write(spec["distinct"])
        st.write(spec["warmup_rule"])
        st.write(spec["book"]["selection"])
        st.write(f"Benchmark: {spec['book']['benchmark']}. Slippage: {spec['book']['slip_bps_per_side']} bps per side.")
        st.json({"grid": spec["grid"], "fixed_book": spec["book"], "warmup_sessions": spec["warmup_sessions"]})

elif page == "Data health":
    health = data_health()
    st.write(f"Snapshot `{health['snapshot_id'][:12]}`")
    st.write(f"Sessions {health['first_session']} through {health['last_session']} ({health['sessions']}).")
    st.write(health["coverage_note"])
    st.write(UNTOUCHED_REASON)
    st.json(health["provenance"])
    if health["payable_before_ex"]:
        st.write("Payable-date exception")
        st.dataframe(pd.DataFrame(health["payable_before_ex"]), hide_index=True)

elif page == "Experiment setup":
    kind = st.selectbox("Strategy", ["research_grid", "baseline_a", "frozen_selector"])
    base = {"research_grid": default_research_spec, "baseline_a": baseline_spec, "frozen_selector": frozen_selector_spec}[kind]()
    base["name"] = st.text_input("Name", base["name"])
    base["budget_hours"] = st.radio("Wall-clock budget", [24, 48], horizontal=True)
    base["costs"]["slip_bps_per_side"] = st.radio("Slippage, basis points per side", [5, 10], horizontal=True)
    st.write("Benchmark: SPY total return. Cash interest stays at zero.")
    base["schedule"]["anchor"] = st.text_input("Anchor session", base["schedule"]["anchor"])
    if kind == "research_grid":
        base["grid"]["ma_win"] = [int(x) for x in st.text_input("MA windows", "70").split(",") if x.strip()]
        base["grid"]["max_hold"] = [int(x) for x in st.text_input("Hold limits", "30").split(",") if x.strip()]
        base["grid"]["atr_mult"] = [float(x) for x in st.text_input("ATR multipliers", "2.0").split(",") if x.strip()]
        base["schedule"]["warmup_sessions"] = int(st.number_input("Warm-up sessions", value=85, step=1))
        base["schedule"]["train_sessions"] = int(st.number_input("Training sessions", value=40, step=1))
        base["schedule"]["eval_sessions"] = int(st.number_input("Evaluation sessions", value=15, step=1))
        base["schedule"]["folds"] = int(st.number_input("Folds", value=1, step=1))
        base["risk"]["max_drawdown_at_most"] = float(st.number_input("Max drawdown allowed in selection", value=0.25, step=0.01, format="%.2f"))
        base["risk"]["min_closed_trades"] = int(st.number_input("Minimum closed trades", value=int(base["risk"]["min_closed_trades"]), step=1))
    lock = st.checkbox("Lock a validation range out of this search")
    if lock:
        base["locked_validation"] = {
            "start": st.text_input("Lock start", "2026-01-01"),
            "end": st.text_input("Lock end", "2026-10-14"),
        }
        base["include_locked_validation"] = False
    st.write("Training rankings and walk-forward evaluation are stored separately.")
    if st.button("Queue experiment"):
        try:
            from lab.adapter import calendar, data_health as health_fn

            spec = validate_spec(base, calendar())
            job_id = enqueue(spec, calendar=calendar(), snapshot_id=health_fn()["snapshot_id"], candidates_total=planned_rows(spec))
            st.success(f"Queued {job_id}. Start the worker; this page does not run the backtest.")
        except DuplicateSearch as exc:
            _show_duplicate(exc.report)
        except Exception as exc:
            st.error(str(exc))

elif page == "Jobs":
    rows = list_jobs()
    if not rows:
        st.write("No jobs yet.")
    else:
        show = []
        for row in rows:
            show.append(
                {
                    "id": row["id"][:8],
                    "status": row["status"],
                    "done": f"{row['candidates_done']}/{row['candidates_total']}",
                    "pilot_seconds": row["pilot_seconds"],
                    "remaining": _remaining(row),
                    "elapsed": _elapsed(row),
                    "error": row["error"],
                }
            )
        st.dataframe(pd.DataFrame(show), hide_index=True)
        selected = st.selectbox("Job", [row["id"] for row in rows])
        col1, col2, col3 = st.columns(3)
        if col1.button("Pause"):
            set_control(selected, "pause")
            st.rerun()
        if col2.button("Resume"):
            set_control(selected, "resume")
            st.rerun()
        if col3.button("Cancel"):
            set_control(selected, "cancel")
            st.rerun()

elif page == "Results":
    rows = list_jobs()
    if not rows:
        st.write("No results yet.")
    else:
        selected = st.selectbox("Job", [row["id"] for row in rows], key="results-job")
        view = result_tables(selected)
        st.subheader("Walk-forward evaluation")
        st.write("Only fully finished evaluations are listed. They are not a production ranking.")
        st.dataframe(view["evaluation"], hide_index=True)
        if view["equity"] is not None:
            st.line_chart(view["equity"])
        st.subheader("Training rankings")
        st.write("Used to choose parameters. Not the walk-forward result.")
        st.dataframe(view["training"], hide_index=True)
        st.subheader("Incomplete or failed")
        st.dataframe(view["other"], hide_index=True)

elif page == "History":
    conn = connect()
    try:
        experiments = [dict(row) for row in conn.execute("SELECT id, name, created_at, spec_hash, data_role FROM experiments ORDER BY created_at DESC")]
    finally:
        conn.close()
    st.write("Every queued specification stays in the registry. A new name does not delete an older search.")
    st.dataframe(pd.DataFrame(experiments), hide_index=True)
    if experiments:
        chosen = st.selectbox("Experiment", [row["id"] for row in experiments])
        detail_jobs = [row for row in list_jobs() if row["experiment_id"] == chosen]
        if detail_jobs:
            detail = job_detail(detail_jobs[0]["id"])
            st.json(json.loads(detail["experiment"]["spec_json"]))
            st.write("Artifacts")
            st.dataframe(pd.DataFrame(detail["artifacts"]), hide_index=True)
