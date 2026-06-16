"""PyInstaller runtime hook — project root + TLS CA bundle before Alpaca/httpx."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _find_project_root() -> Path:
    candidate = Path(sys.executable).resolve().parent
    for _ in range(8):
        nested = candidate / "stock-bot"
        if (nested / "run_all.py").is_file():
            return nested
        if (candidate / "run_all.py").is_file():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return Path(sys.executable).resolve().parent


if getattr(sys, "frozen", False):
    os.environ.setdefault("PYTHONTRADING_ROOT", str(_find_project_root()))
    root = Path(os.environ["PYTHONTRADING_ROOT"])
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        val = os.environ.get(var, "").strip()
        if val and not Path(val).expanduser().is_file():
            os.environ.pop(var, None)

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / "certifi" / "cacert.pem"
        if bundled.is_file():
            os.environ["SSL_CERT_FILE"] = str(bundled)
            os.environ["REQUESTS_CA_BUNDLE"] = str(bundled)
        else:
            try:
                import certifi

                ca = Path(certifi.where())
                if ca.is_file():
                    os.environ["SSL_CERT_FILE"] = str(ca)
                    os.environ["REQUESTS_CA_BUNDLE"] = str(ca)
            except ImportError:
                pass
