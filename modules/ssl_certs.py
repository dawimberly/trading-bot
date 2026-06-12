"""Ensure valid TLS CA bundle paths for Alpaca/httpx (especially PyInstaller exe)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_SSL_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")


def _clear_invalid_ssl_env() -> None:
    for var in _SSL_VARS:
        val = os.environ.get(var, "").strip()
        if not val:
            continue
        if not Path(val).expanduser().is_file():
            os.environ.pop(var, None)


def _set_bundle(var: str, path: Path) -> None:
    os.environ[var] = str(path)


def configure_ssl_certificates() -> str | None:
    """
    Point SSL env vars at a real cacert.pem.
    Returns the path used, or None if unchanged.
    """
    _clear_invalid_ssl_env()

    for var in _SSL_VARS:
        val = os.environ.get(var, "").strip()
        if val and Path(val).expanduser().is_file():
            return val

    candidates: list[Path] = []

    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            base = Path(meipass)
            candidates.extend(
                [
                    base / "certifi" / "cacert.pem",
                    base / "cacert.pem",
                ]
            )
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend(
            [
                exe_dir / "_internal" / "certifi" / "cacert.pem",
                exe_dir / "certifi" / "cacert.pem",
            ]
        )

    try:
        import certifi

        candidates.insert(0, Path(certifi.where()))
    except ImportError:
        pass

    for path in candidates:
        if path.is_file():
            _set_bundle("SSL_CERT_FILE", path)
            _set_bundle("REQUESTS_CA_BUNDLE", path)
            return str(path)

    return None
