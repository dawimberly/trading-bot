"""365d diversification A/B: control vs pick-rotation + sector RS 0.02."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ANALYSIS = Path(__file__).resolve().parent
PY = Path(os.environ.get("V154_PYTHON", r"c:\Users\Owner\PythonTrading\.venv\Scripts\python.exe"))
DAYS = os.environ.get("V154_DIV_DAYS", "365")
RUNNER = ROOT / "scripts" / "analysis" / "_run_single_ab_leg.py"

COMMON_CLEAR = [
    "BASE_UNIVERSE_SIZE",
    "SECTOR_EXPANSION_SIZE",
    "SECTOR_MAX_TOTAL_TICKERS",
    "SECTOR_FALLBACK_MOMENTUM_COUNT",
    "NYSE_PICK_ROTATION_ENABLED",
    "NYSE_STRICT_INTERSECT_CANDIDATES",
    "PAPER_DYNAMIC_UNIVERSE_STRICT",
    "PAPER_MOMENTUM_QUALITY_FIXES",
    "SECTOR_RS_MIN",
]

CONTROL_SET = [
    "MARKOV_HMM_PRIMARY_REGIME=false",
    "MARKOV_HMM_ENABLED=true",
]

# Treatment: pick rotation (5/20) + sector screener activation (RS min 0.02).
# Keep BASE125/MAX200 locked (cleared above). No strict-intersect / quality-fixes.
TREATMENT_SET = [
    "MARKOV_HMM_PRIMARY_REGIME=false",
    "MARKOV_HMM_ENABLED=true",
    "NYSE_PICK_ROTATION_ENABLED=true",
    "NYSE_MAX_PICKS_PER_SYMBOL_WINDOW=5",
    "NYSE_PICK_WINDOW_BARS=20",
    "SECTOR_RS_MIN=0.02",
]


def _leg_env(extra: list[str]) -> dict[str, str]:
    env = os.environ.copy()
    for key in COMMON_CLEAR:
        env.pop(key, None)
    env["PAPER_DEPLOY_DEBUG"] = "false"
    env["PYTHONUNBUFFERED"] = "1"
    env["MARKOV_HMM_PRIMARY_REGIME"] = "false"
    for item in extra:
        key, val = item.split("=", 1)
        env[key.strip()] = val.strip()
    return env


def _run_leg(label: str, out_stem: str, extra: list[str]) -> int:
    out_txt = ANALYSIS / f"{out_stem}.txt"
    out_err = ANALYSIS / f"{out_stem}.err.txt"
    out_json = ANALYSIS / f"{out_stem}.json"
    cmd = [
        str(PY),
        str(RUNNER),
        "--days",
        str(DAYS),
        "--label",
        label,
        "--export-json",
        str(out_json),
        *[f"--set={s}" for s in extra],
    ]
    env = _leg_env(extra)
    t0 = time.time()
    print(f"{datetime.now().isoformat()} {label} starting -> {out_txt}", flush=True)
    with open(out_txt, "w", encoding="utf-8") as out_f, open(
        out_err, "w", encoding="utf-8"
    ) as err_f:
        proc = subprocess.run(cmd, cwd=str(ROOT), env=env, stdout=out_f, stderr=err_f)
    elapsed = (time.time() - t0) / 60.0
    print(
        f"{datetime.now().isoformat()} {label} exit={proc.returncode} "
        f"elapsed_min={elapsed:.1f}",
        flush=True,
    )
    return proc.returncode


def _metrics_from_txt(txt_path: Path) -> dict:
    """Fallback metrics when JSON export is missing."""
    if not txt_path.is_file():
        return {}
    text = txt_path.read_text(encoding="utf-8", errors="replace")
    out: dict = {}
    import re

    def _f(pat: str):
        m = re.search(pat, text)
        return float(m.group(1)) if m else None

    out["return_pct"] = _f(r"Total Return:\s+([-\d.]+)%")
    out["sharpe"] = _f(r"Sharpe Ratio:\s+([-\d.]+)")
    out["max_dd_pct"] = _f(r"Max Drawdown:\s+([-\d.]+)%")
    vti = _f(r"VTI Buy & Hold:\s+([-\d.]+)%")
    if out["return_pct"] is not None and vti is not None:
        out["vs_vti_pp"] = round(out["return_pct"] - vti, 2)
    m = re.search(r"Active sectors:\s+([^\n|]+)", text)
    if m:
        out["active_sectors_note"] = m.group(1).strip()
    return out


def _metrics(json_path: Path, txt_path: Path | None = None) -> dict:
    if not json_path.is_file():
        base = _metrics_from_txt(txt_path) if txt_path else {}
        return base
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _metrics_from_txt(txt_path) if txt_path else {}
    picks = data.get("nyse_pick_counts") or {}
    total_picks = sum(int(v) for v in picks.values())
    top2 = sum(sorted((int(v) for v in picks.values()), reverse=True)[:2])
    top2_share = (top2 / total_picks * 100.0) if total_picks else 0.0
    ret = data.get("total_return_pct")
    vti = data.get("benchmark_return_pct")
    vs_vti = None
    if ret is not None and vti is not None:
        vs_vti = round(float(ret) - float(vti), 2)
    out = {
        "return_pct": ret,
        "sharpe": data.get("sharpe"),
        "max_dd_pct": data.get("max_drawdown_pct"),
        "vs_vti_pp": vs_vti,
        "nyse_pick_counts": picks,
        "unique_symbols": len(picks),
        "top2_share_pct": round(top2_share, 1),
    }
    if txt_path:
        txt_m = _metrics_from_txt(txt_path)
        if txt_m.get("active_sectors_note"):
            out["active_sectors_note"] = txt_m["active_sectors_note"]
    return out


def main() -> int:
    if not PY.is_file():
        print(f"Python not found: {PY}", file=sys.stderr)
        return 2
    status_path = ANALYSIS / "backtest_v154_div_365.status.txt"
    ctrl_stem = "backtest_v154_div_ctrl_365"
    treat_stem = "backtest_v154_div_treat_365"
    skip_control = os.environ.get("V154_DIV_SKIP_CONTROL", "").lower() in (
        "1",
        "true",
        "yes",
    )

    with open(status_path, "w", encoding="utf-8") as st:
        st.write(f"{datetime.now().isoformat()} wrapper start skip_control={skip_control}\n")

    rc_ctrl = 0
    if skip_control:
        print("Skipping control (reuse existing log)", flush=True)
        with open(status_path, "a", encoding="utf-8") as st:
            st.write(f"{datetime.now().isoformat()} CONTROL skipped (reuse)\n")
    else:
        rc_ctrl = _run_leg("CONTROL", ctrl_stem, CONTROL_SET)
        with open(status_path, "a", encoding="utf-8") as st:
            st.write(
                f"{datetime.now().isoformat()} CONTROL exit={rc_ctrl} "
                f"out={ANALYSIS / (ctrl_stem + '.txt')}\n"
            )

    rc_treat = _run_leg("TREATMENT", treat_stem, TREATMENT_SET)
    with open(status_path, "a", encoding="utf-8") as st:
        st.write(
            f"{datetime.now().isoformat()} TREATMENT exit={rc_treat} "
            f"out={ANALYSIS / (treat_stem + '.txt')}\n"
        )
        st.write(
            f"{datetime.now().isoformat()} DONE ctrl_exit={rc_ctrl} treat_exit={rc_treat}\n"
        )

    summary_path = ANALYSIS / "backtest_v154_div_365_summary.txt"
    ctrl_m = _metrics(
        ANALYSIS / f"{ctrl_stem}.json", ANALYSIS / f"{ctrl_stem}.txt"
    )
    treat_m = _metrics(
        ANALYSIS / f"{treat_stem}.json", ANALYSIS / f"{treat_stem}.txt"
    )
    lines = [
        "365d Diversification A/B summary",
        "treatment = pick rotation 5/20 + SECTOR_RS_MIN=0.02",
        f"control: {ctrl_m}",
        f"treatment: {treat_m}",
    ]
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)
    return 0 if rc_ctrl == 0 and rc_treat == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
