"""PyInstaller runtime hook — project root + TLS CA bundle before Alpaca/httpx."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _find_project_root() -> Path:
    exe_dir = Path(sys.executable).resolve().parent
    candidate = exe_dir
    for _ in range(8):
        if (candidate / "run_all.py").is_file():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return exe_dir


if getattr(sys, "frozen", False):
    os.environ.setdefault("PYTHONTRADING_ROOT", str(_find_project_root()))
    root = Path(os.environ["PYTHONTRADING_ROOT"])
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    try:
        from modules.ssl_certs import configure_ssl_certificates

        configure_ssl_certificates(force=True)
    except Exception:
        for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
            val = os.environ.get(var, "").strip()
            if val and not Path(val).expanduser().is_file():
                os.environ.pop(var, None)
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            bundled = Path(meipass) / "certifi" / "cacert.pem"
            if bundled.is_file():
                bundle = str(bundled)
                os.environ["SSL_CERT_FILE"] = bundle
                os.environ["REQUESTS_CA_BUNDLE"] = bundle
                os.environ["CURL_CA_BUNDLE"] = bundle

    try:
        os.chdir(str(root))
    except OSError:
        pass

    data_root = root / "dist" if (root / "dist" / "Weinstein-Trading-Bot.exe").is_file() else root
    if getattr(sys, "frozen", False) and (data_root / "Weinstein-Trading-Bot.exe").is_file():
        try:
            os.chdir(str(data_root))
        except OSError:
            pass

    try:
        from modules.safe_io import ensure_stdio_streams

        ensure_stdio_streams()
    except Exception:
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
