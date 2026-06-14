"""Scan project tree for .pyc, .log, and other debris files.

Run: python scripts/maintenance/directory_cleanup.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def check_and_clean(root_dir):
    useless_extensions = {".pyc", ".tmp", ".log", ".bak"}
    print(f"--- Scanning Directory: {root_dir} ---")
    for dirpath, _dirnames, filenames in os.walk(root_dir):
        if ".venv" in dirpath or "venv" in dirpath:
            continue
        for f in filenames:
            ext = os.path.splitext(f)[1]
            if ext in useless_extensions:
                print(f"[DELETING] Useless file found: {f}")
            else:
                print(f"[FILE] {f} (Location: {dirpath})")
    print("\n--- Scan Complete ---")


if __name__ == "__main__":
    check_and_clean(str(Path(__file__).resolve().parents[2]))
