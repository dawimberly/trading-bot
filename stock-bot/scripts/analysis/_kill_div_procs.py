"""Kill div A/B related python processes; list matches."""
from __future__ import annotations

import subprocess
import sys


def main() -> int:
    kill = "--kill" in sys.argv
    try:
        out = subprocess.check_output(
            [
                "wmic",
                "process",
                "where",
                "name='python.exe'",
                "get",
                "ProcessId,CommandLine",
                "/FORMAT:CSV",
            ],
            text=True,
            errors="replace",
        )
    except Exception as exc:
        print(f"wmic failed: {exc}")
        return 1
    keys = ("_run_div_ab", "_run_single_ab_leg", "backtest_v154_div", "backtester.py")
    killed = []
    for line in out.splitlines():
        if not line.strip() or line.startswith("Node,"):
            continue
        low = line.lower()
        if not any(k.lower() in low for k in keys):
            continue
        parts = line.split(",")
        pid = parts[-1].strip() if parts else ""
        print(line[:240])
        if kill and pid.isdigit():
            subprocess.run(["taskkill", "/PID", pid, "/F"], check=False)
            killed.append(pid)
    if kill:
        print(f"killed={killed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
