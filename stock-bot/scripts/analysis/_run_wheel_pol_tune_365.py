"""365d A/B: baseline (sleeves OFF) vs tuned Wheel + Politician Copy."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ANALYSIS = Path(__file__).resolve().parent
PY = Path(sys.executable)
LEG = ANALYSIS / "_run_single_ab_leg.py"
STATUS = ANALYSIS / "backtest_v154_wheel_pol_tune_365.status.txt"
SUMMARY = ANALYSIS / "backtest_v154_wheel_pol_tune_365.summary.txt"

BASE_SETS = [
    "MARKOV_HMM_ENABLED=true",
    "MARKOV_HMM_PRIMARY_REGIME=false",
    "PAPER_WHEEL_SLEEVE_ENABLED=false",
    "PAPER_POLITICIAN_COPY_ENABLED=false",
]

TREAT_SETS = [
    "MARKOV_HMM_ENABLED=true",
    "MARKOV_HMM_PRIMARY_REGIME=false",
    "PAPER_WHEEL_SLEEVE_ENABLED=true",
    "PAPER_POLITICIAN_COPY_ENABLED=true",
    "WHEEL_OTM_PCT=0.04",
    "WHEEL_DTE_BARS=14",
    "WHEEL_MANAGE_BARS=7",
    "POLITICIAN_COPY_LOOKBACK_DAYS=60",
    "POLITICIAN_COPY_MIN_AMOUNT_USD=10000",
    "POLITICIAN_COPY_SIZE_SCALE=0.025",
    "POLITICIAN_COPY_FILL_LAG_DAYS=3",
]


def _write_status(msg: str) -> None:
    STATUS.write_text(
        f"{datetime.now(timezone.utc).isoformat()} {msg}\n", encoding="utf-8"
    )
    print(msg, flush=True)


def _run_leg(label: str, sets: list[str], out_txt: Path, out_json: Path) -> int:
    cmd = [
        str(PY),
        str(LEG),
        "--days",
        "365",
        "--label",
        label,
        "--export-json",
        str(out_json),
    ]
    for s in sets:
        cmd.extend(["--set", s])
    _write_status(f"START {label} -> {out_txt.name}")
    with out_txt.open("w", encoding="utf-8") as fh:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            stdout=fh,
            stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    _write_status(f"DONE {label} exit={proc.returncode}")
    return int(proc.returncode)


def _load_metrics(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _pick(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def main() -> int:
    os.chdir(ROOT)
    # Force weekday-aligned seed v2
    seed = ROOT / "data" / "politician_trades_seed.json"
    if seed.is_file():
        seed.unlink()
    sys.path.insert(0, str(ROOT))
    from modules.politician_copy_sleeve import ensure_seed_trades, candidates_for_bar, rank_top_traders
    from datetime import datetime, timezone, timedelta

    trades = ensure_seed_trades(force=True)
    sample_asof = datetime.now(timezone.utc) - timedelta(days=30)
    while sample_asof.weekday() >= 5:
        sample_asof -= timedelta(days=1)
    top = rank_top_traders(trades)
    cands = candidates_for_bar(trades, as_of=sample_asof, top_traders=top)
    _write_status(
        f"SEED v2 trades={len(trades)} top={top[:3]} sample_cands={len(cands)} asof={sample_asof.date()}"
    )
    if not cands:
        _write_status("WARN: no sample candidates — politician sleeve may still under-fill")

    ctrl_txt = ANALYSIS / "backtest_v154_wheel_pol_tune_ctrl_365.txt"
    ctrl_json = ANALYSIS / "backtest_v154_wheel_pol_tune_ctrl_365.json"
    treat_txt = ANALYSIS / "backtest_v154_wheel_pol_tune_treat_365.txt"
    treat_json = ANALYSIS / "backtest_v154_wheel_pol_tune_treat_365.json"

    rc1 = _run_leg("BASELINE sleeves OFF", BASE_SETS, ctrl_txt, ctrl_json)
    rc2 = _run_leg("TUNED Wheel+Politician", TREAT_SETS, treat_txt, treat_json)

    base = _load_metrics(ctrl_json)
    treat = _load_metrics(treat_json)

    def row(name: str, m: dict) -> str:
        ret = _pick(m, "total_return_pct", "total_return", "return_pct", default=None)
        sharpe = _pick(m, "sharpe", "sharpe_ratio", default=None)
        mdd = _pick(m, "max_drawdown_pct", "max_drawdown", "max_dd", default=None)
        vti = _pick(m, "benchmark_return_pct", "vti_return", "benchmark_return", default=None)
        wheel = m.get("wheel_sleeve") or {}
        pol = m.get("politician_copy") or {}
        vs_vti = None
        if ret is not None and vti is not None:
            try:
                vs_vti = round(float(ret) - float(vti), 2)
            except (TypeError, ValueError):
                vs_vti = None
        return (
            f"{name}: ret={ret}% sharpe={sharpe} mdd={mdd}% vti={vti}% vsVTI={vs_vti}pp | "
            f"wheel prem={wheel.get('total_premium')} cycles={wheel.get('manage_cycles')} | "
            f"pol copies={pol.get('total_copies')} notional={pol.get('total_notional')}"
        )

    lines = [
        "Wheel+Politician tune 365d A/B",
        f"baseline exit={rc1} treatment exit={rc2}",
        row("BASELINE", base),
        row("TUNED", treat),
        "",
        "Tunes: WHEEL_OTM=0.04 DTE=14 MANAGE=7 | POL lookback=60 min$=10k scale=0.025 lag=3d",
    ]
    SUMMARY.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _write_status("SUMMARY written")
    print("\n".join(lines), flush=True)
    return 0 if rc1 == 0 and rc2 == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
