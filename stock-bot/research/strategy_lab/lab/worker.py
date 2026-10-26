"""One local worker. It does not stop unrelated processes or send orders."""

from __future__ import annotations

import os
import sys
import time

from lab import DEPLOYMENT_ENABLED, ORDERS_ENABLED
from lab.jobs import claim, owner_id
from lab.runner import execute


_JOB_HANDLE = None


def apply_limits() -> str:
    """One process, below-normal priority, and a configurable memory ceiling.

    STRATEGY_LAB_PRIORITY is idle, below_normal, or normal.
    STRATEGY_LAB_MEMORY_MB defaults to 4096. Zero disables the ceiling.
    """
    if os.name != "nt":
        return "limits skipped: not Windows"
    import ctypes
    from ctypes import wintypes

    global _JOB_HANDLE
    kernel = ctypes.windll.kernel32
    priorities = {"idle": 0x00000040, "below_normal": 0x00004000, "normal": 0x00000020}
    name = os.environ.get("STRATEGY_LAB_PRIORITY", "below_normal")
    kernel.SetPriorityClass(kernel.GetCurrentProcess(), priorities.get(name, priorities["below_normal"]))
    try:
        limit_mb = int(os.environ.get("STRATEGY_LAB_MEMORY_MB", "4096"))
    except ValueError:
        limit_mb = 4096
    if limit_mb <= 0:
        return f"priority={name} memory ceiling off"

    class BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("Basic", BasicLimit),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel.CreateJobObjectW.restype = ctypes.c_void_p
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    job = kernel.CreateJobObjectW(None, None)
    info = ExtendedLimit()
    info.Basic.LimitFlags = 0x00000100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
    info.ProcessMemoryLimit = limit_mb * 1024 * 1024
    job_object_extended = 9
    kernel.SetLastError(0)
    configured = kernel.SetInformationJobObject(job, job_object_extended, ctypes.byref(info), ctypes.sizeof(info))
    set_error = 0 if configured else kernel.GetLastError()
    kernel.SetLastError(0)
    assigned = bool(configured) and bool(kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()))
    assign_error = 0 if assigned else kernel.GetLastError()
    if assigned:
        _JOB_HANDLE = job
        return f"priority={name} memory_mb={limit_mb} applied=yes"
    if job:
        kernel.CloseHandle(job)
    return f"priority={name} memory_mb={limit_mb} applied=no set_error={set_error} assign_error={assign_error}"


def serve_once() -> str | None:
    if ORDERS_ENABLED or DEPLOYMENT_ENABLED:
        raise RuntimeError("orders and deployment are disabled")
    job = claim(owner_id())
    if job is None:
        return None
    return execute(job, job["owner"])


def main() -> int:
    print(apply_limits())
    once = "--once" in sys.argv
    while True:
        outcome = serve_once()
        if once:
            print("worker idle" if outcome is None else outcome)
            return 0
        if outcome is None:
            time.sleep(2.0)


if __name__ == "__main__":
    raise SystemExit(main())
