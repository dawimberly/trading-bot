"""Wait for the v1.5.4 1000d backtest to finish, then append a summary table."""
from __future__ import annotations

import datetime as dt
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "backtest_v154_thorough_1000.txt"
ERR = ROOT / "backtest_v154_thorough_1000.err.txt"
LOG = ROOT / "backtest_v154_thorough_1000.watcher.log"


def _backtest_running() -> bool:
    try:
        import psutil  # type: ignore
    except Exception:
        psutil = None
    if psutil is not None:
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmd = " ".join(proc.info.get("cmdline") or [])
            except Exception:
                continue
            if "backtester.py" in cmd and "--days" in cmd and "1000" in cmd:
                return True
        return False
    # Fallback: tasklist / WMIC via ctypes-free PowerShell is overkill; poll file mtime.
    return False


def _running_via_wmic() -> bool:
    import subprocess

    try:
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "if (Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                    "Where-Object { $_.CommandLine -match 'backtester.py --days 1000' }) "
                    "{ exit 0 } else { exit 1 }"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return r.returncode == 0
    except Exception:
        return False


def _parse_summary(text: str) -> list[str]:
    keys = [
        "Total Return:",
        "VTI Buy & Hold:",
        "Sharpe Ratio:",
        "Sortino Ratio:",
        "Calmar Ratio:",
        "Max Drawdown:",
        "Final Equity:",
        "Total orders:",
        "SPY signals:",
        "NYSE signals:",
        "Crypto signals:",
        "Profit factor:",
        "Win rate (daily):",
        "Rolling Sharpe:",
        "Simulation:",
        "Historical news simulation:",
    ]
    lines = [
        "",
        "=" * 72,
        "v1.5.4 THOROUGH 1000d SUMMARY TABLE",
        "Generated: " + dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Command: python -u backtester.py --days 1000 --paper-aggressive --no-thinking",
        (
            "Flags: GARCH ON | Dynamic VTI ON | Daily Banking ON | RHYME primary | "
            "HMM soft (retrain every 5 bars, n_states=5, train=252d) | "
            "HMM primary OFF | thinking OFF | news/Felix per profile"
        ),
        "=" * 72,
    ]
    for key in keys:
        m = re.search(rf"^{re.escape(key)}\s*(.+)$", text, re.M)
        label = key.rstrip(":")
        lines.append(f"{label:<28} {(m.group(1).strip() if m else 'n/a')}")

    for pat in (
        r"Final core allocator:[^\n]*",
        r"dynamic_vti:\s*[^\n]*",
        r"thinking_engine:\s*[^\n]*",
        r"Stat Arb[^\n]*",
        r"Protective Shorts:[^\n]*",
    ):
        m = re.search(pat, text)
        if m:
            lines.append(m.group(0).strip()[:220])

    tr = re.search(r"Total Return:\s*([-\d.]+)%", text)
    vb = re.search(r"VTI Buy & Hold:\s*([-\d.]+)%", text)
    if tr and vb:
        delta = float(tr.group(1)) - float(vb.group(1))
        lines.append(f"{'vs VTI (pp)':<28} {delta:+.2f} pp")

    # Sleeve attribution table if present
    sa = re.search(
        r"--- SLEEVE ATTRIBUTION ---.*?^(?=Rolling Sharpe:|-{10,}|\Z)",
        text,
        re.M | re.S,
    )
    if sa:
        lines.append("")
        lines.append(sa.group(0).strip()[:2500])

    lines.append("=" * 72)
    lines.append("")
    return lines


def main() -> None:
    LOG.write_text(
        f"watcher start {dt.datetime.now().isoformat()}\n", encoding="utf-8"
    )
    while True:
        running = _backtest_running() or _running_via_wmic()
        LOG.write_text(
            LOG.read_text(encoding="utf-8")
            + f"{dt.datetime.now().isoformat()} running={running} "
            f"bytes={OUT.stat().st_size if OUT.exists() else 0}\n",
            encoding="utf-8",
        )
        if not running:
            break
        time.sleep(120)

    time.sleep(3)
    text = OUT.read_text(encoding="utf-8", errors="replace") if OUT.exists() else ""
    err = ERR.read_text(encoding="utf-8", errors="replace") if ERR.exists() else ""
    if "v1.5.4 THOROUGH 1000d SUMMARY TABLE" not in text:
        if "Total Return:" in text or "FUND BACKTEST REPORT" in text:
            OUT.write_text(text + "\n".join(_parse_summary(text)), encoding="utf-8")
            LOG.write_text(
                LOG.read_text(encoding="utf-8") + "SUMMARY APPENDED\n", encoding="utf-8"
            )
        else:
            note = [
                "",
                "=" * 72,
                "v1.5.4 THOROUGH 1000d SUMMARY TABLE",
                "Generated: " + dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "STATUS: backtest process exited without a FUND BACKTEST REPORT",
                f"stdout bytes: {len(text)} | stderr bytes: {len(err)}",
                "--- stderr tail ---",
                err[-2000:],
                "=" * 72,
                "",
            ]
            OUT.write_text(text + "\n".join(note), encoding="utf-8")
            LOG.write_text(
                LOG.read_text(encoding="utf-8") + "EXIT WITHOUT REPORT\n",
                encoding="utf-8",
            )
    else:
        LOG.write_text(
            LOG.read_text(encoding="utf-8") + "SUMMARY ALREADY PRESENT\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
