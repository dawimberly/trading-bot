"""Kill WHEEL_POL / single-ab python processes."""
from __future__ import annotations
import subprocess
import sys

out = subprocess.check_output(
    ["wmic", "process", "where", "name='python.exe'", "get", "ProcessId,CommandLine", "/FORMAT:CSV"],
    text=True,
    errors="replace",
)
keys = ("WHEEL_POL", "wheel_pol", "_run_single_ab_leg", "_run_div_ab")
for line in out.splitlines():
    if not any(k in line for k in keys):
        continue
    print(line[:220])
    pid = line.split(",")[-1].strip()
    if pid.isdigit() and "--kill" in sys.argv:
        subprocess.run(["taskkill", "/PID", pid, "/F"], check=False)
