"""Sequential v1.5.4 tune A/B (Stat Arb quality → ARIMA → VTI floor → SPY-like).

Env:
  V154_TUNE_STAGE=stat_arb|arima|vti_floor|spy_like|all
  V154_TUNE_DAYS=365
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
PY = Path(r"c:\Users\Owner\PythonTrading\.venv\Scripts\python.exe")
DAYS = os.environ.get("V154_TUNE_DAYS", "365")
STAGE = os.environ.get("V154_TUNE_STAGE", "all").strip().lower()

METRIC_RE = {
    "return": re.compile(r"Total Return:\s+([+-]?\d+(?:\.\d+)?)%", re.I),
    "sharpe": re.compile(r"Sharpe Ratio:\s+([+-]?\d+(?:\.\d+)?)", re.I),
    "maxdd": re.compile(r"Max Drawdown:\s+([+-]?\d+(?:\.\d+)?)%", re.I),
    "vti_bh": re.compile(r"VTI Buy & Hold:\s+([+-]?\d+(?:\.\d+)?)%", re.I),
    "avg_vti": re.compile(r"VTI core \(avg\):\s+([+-]?\d+(?:\.\d+)?)%", re.I),
}
QUALITY_ROW_RE = re.compile(
    r"^(fill-rate baseline|v1\.5\.4 quality).*?"
    r"([+-]?\d+\.\d+)%\s+([+-]?\d+\.\d+)\s+([+-]?\d+\.\d+)%",
    re.I | re.M,
)
QUALITY_DELTA_RE = re.compile(r"Delta \(after - before\):\s*(.+)", re.I)


def _banner(msg: str) -> None:
    print("\n" + "=" * 72, flush=True)
    print(msg, flush=True)
    print("=" * 72 + "\n", flush=True)


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PAPER_DEPLOY_DEBUG"] = "false"
    env["MARKOV_HMM_ENABLED"] = env.get("MARKOV_HMM_ENABLED", "false")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _run_logged(cmd: list[str], log_path: Path, env: dict[str, str] | None = None) -> str:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"CMD: {' '.join(cmd)}", flush=True)
    print(f"LOG: {log_path}", flush=True)
    t0 = time.time()
    with log_path.open("w", encoding="utf-8", errors="replace") as fh:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=env or _base_env(),
            stdout=fh,
            stderr=subprocess.STDOUT,
            text=True,
        )
        pid_path = log_path.with_suffix(".pid")
        pid_path.write_text(str(proc.pid), encoding="utf-8")
        rc = proc.wait()
    elapsed = time.time() - t0
    text = log_path.read_text(encoding="utf-8", errors="replace")
    print(f"exit={rc} elapsed={elapsed / 60:.1f}m", flush=True)
    if rc != 0:
        print(f"WARNING: non-zero exit {rc} for {log_path.name}", flush=True)
    return text


def _parse_metrics(text: str) -> dict:
    # Prefer the last FUND BACKTEST REPORT block
    chunks = text.split("--- FUND BACKTEST REPORT")
    block = chunks[-1] if len(chunks) > 1 else text
    out: dict[str, float] = {}
    for key, rx in METRIC_RE.items():
        matches = list(rx.finditer(block))
        if matches:
            out[key] = float(matches[-1].group(1))
    if "return" in out and "vti_bh" in out:
        out["vs_vti"] = out["return"] - out["vti_bh"]
    return out


def _print_row(label: str, m: dict, notes: str = "") -> str:
    vs = m.get("vs_vti")
    vs_s = f"{vs:+.2f}pp" if vs is not None else "n/a"
    avg = m.get("avg_vti")
    avg_s = f"{avg:.1f}%" if avg is not None else "n/a"
    line = (
        f"| {label} | {m.get('return', float('nan')):+.2f}% | "
        f"{m.get('sharpe', float('nan')):.2f} | "
        f"{m.get('maxdd', float('nan')):.2f}% | {vs_s} | {notes or avg_s} |"
    )
    print(line, flush=True)
    return line


def run_stat_arb() -> list[str]:
    _banner(f"STAGE 1: Stat Arb quality compare ({DAYS}d) — no default knob changes")
    log = OUT / f"_v154_stat_arb_quality_{DAYS}.log"
    # Quiet runner wrapper
    env = _base_env()
    text = _run_logged(
        [
            str(PY),
            "-u",
            str(OUT / "_run_stat_arb_quality_quiet.py"),
            DAYS,
        ],
        log,
        env=env,
    )
    lines = [
        "### Stat Arb quality (fill-rate baseline vs v1.5.4)",
        "Knobs unchanged (Z 2.1-2.7, RR 1.7, trail 45/30, partial@1.2, pairs 8-12).",
    ]
    for m in QUALITY_ROW_RE.finditer(text):
        label = m.group(1)
        ret, sharpe, maxdd = float(m.group(2)), float(m.group(3)), float(m.group(4))
        lines.append(
            _print_row(
                f"SA {label}",
                {"return": ret, "sharpe": sharpe, "maxdd": maxdd},
                notes="compare-stat-arb-quality",
            )
        )
    dm = QUALITY_DELTA_RE.search(text)
    if dm:
        lines.append(f"Delta: {dm.group(1).strip()}")
        print(f"Delta: {dm.group(1).strip()}", flush=True)
    # Also scrape last report metrics if present after quality legs
    return lines


def run_single_ab(
    *,
    stage_name: str,
    legs: list[tuple[str, dict[str, str], str]],
) -> list[str]:
    _banner(f"{stage_name} ({DAYS}d, MARKOV_HMM_ENABLED=false)")
    lines = [f"### {stage_name}"]
    leg_script = OUT / "_run_single_ab_leg.py"
    for label, overrides, log_name in legs:
        env = _base_env()
        # Also put in env for _env_explicit() during profile enforce.
        env.update(overrides)
        log = OUT / log_name
        cmd = [
            str(PY),
            "-u",
            str(leg_script),
            "--days",
            DAYS,
            "--label",
            label,
        ]
        for k, v in overrides.items():
            cmd.extend(["--set", f"{k}={v}"])
        text = _run_logged(cmd, log, env=env)
        m = _parse_metrics(text)
        note = f"avgVTI={m.get('avg_vti', float('nan')):.1f}%" if "avg_vti" in m else ""
        lines.append(_print_row(label, m, notes=note))
        side = log.with_name(log.stem + "_metrics.txt")
        side.write_text(
            "\n".join(f"{k}={v}" for k, v in sorted(m.items())) + "\n",
            encoding="utf-8",
        )
    return lines


def main() -> None:
    stages = ["stat_arb", "arima", "vti_floor", "spy_like"]
    if STAGE == "all":
        todo = stages
    elif STAGE in stages:
        todo = stages[stages.index(STAGE) :]
    else:
        raise SystemExit(f"Unknown V154_TUNE_STAGE={STAGE!r}")

    print(
        f"v1.5.4 tune A/B | days={DAYS} | stages={todo} | "
        f"HMM={os.environ.get('MARKOV_HMM_ENABLED', 'false')}",
        flush=True,
    )
    all_lines: list[str] = [
        f"v1.5.4 tune A/B days={DAYS}",
        f"started={time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"HMM={os.environ.get('MARKOV_HMM_ENABLED', 'false')}",
        "",
        "| Test | Return | Sharpe | MaxDD | vs VTI | notes |",
        "|---|---:|---:|---:|---:|---|",
    ]

    if "stat_arb" in todo:
        all_lines.extend(run_stat_arb())

    if "arima" in todo:
        all_lines.extend(
            run_single_ab(
                stage_name="STAGE 2: ARIMA OFF vs ON",
                legs=[
                    (
                        "ARIMA OFF",
                        {"ARIMA_ENABLED": "false"},
                        f"_v154_arima_off_{DAYS}.log",
                    ),
                    (
                        "ARIMA ON",
                        {"ARIMA_ENABLED": "true"},
                        f"_v154_arima_on_{DAYS}.log",
                    ),
                ],
            )
        )

    if "vti_floor" in todo:
        all_lines.extend(
            run_single_ab(
                stage_name="STAGE 3: optional VTI floor OFF vs ON",
                legs=[
                    (
                        "VTI floor fixed~35%",
                        {
                            "DYNAMIC_VTI_OPTIONAL_ENABLED": "false",
                            "DYNAMIC_VTI_ALLOW_ZERO": "false",
                        },
                        f"_v154_vti_floor_fixed_{DAYS}.log",
                    ),
                    (
                        "VTI optional 20%/0%",
                        {
                            "DYNAMIC_VTI_OPTIONAL_ENABLED": "true",
                            "DYNAMIC_VTI_ALLOW_ZERO": "true",
                            "DYNAMIC_VTI_FLOOR_MIN": "0.20",
                        },
                        f"_v154_vti_optional_{DAYS}.log",
                    ),
                ],
            )
        )

    if "spy_like" in todo:
        all_lines.extend(
            run_single_ab(
                stage_name="STAGE 4: SPY-like boost OFF vs ON (optional VTI ON)",
                legs=[
                    (
                        "SPY-like boost OFF",
                        {
                            "DYNAMIC_VTI_OPTIONAL_ENABLED": "true",
                            "DYNAMIC_VTI_ALLOW_ZERO": "true",
                            "DYNAMIC_VTI_FLOOR_MIN": "0.20",
                            "SPY_LIKE_BOOST_ENABLED": "false",
                        },
                        f"_v154_spy_like_off_{DAYS}.log",
                    ),
                    (
                        "SPY-like boost ON",
                        {
                            "DYNAMIC_VTI_OPTIONAL_ENABLED": "true",
                            "DYNAMIC_VTI_ALLOW_ZERO": "true",
                            "DYNAMIC_VTI_FLOOR_MIN": "0.20",
                            "SPY_LIKE_BOOST_ENABLED": "true",
                        },
                        f"_v154_spy_like_on_{DAYS}.log",
                    ),
                ],
            )
        )

    all_lines.append(f"finished={time.strftime('%Y-%m-%d %H:%M:%S')}")
    summary = OUT / f"_v154_tune_ab_{DAYS}_summary.txt"
    summary.write_text("\n".join(all_lines) + "\n", encoding="utf-8")
    print(f"\nWrote {summary}", flush=True)
    print("\n".join(all_lines), flush=True)


if __name__ == "__main__":
    main()
