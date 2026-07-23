"""Ensure valid TLS CA bundle paths for Alpaca/httpx (especially PyInstaller exe)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_SSL_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
_CONFIGURED: str | None = None


def _normalize(path: Path) -> str:
    return str(path.expanduser().resolve())


def _env_bundle_path() -> Path | None:
    for var in _SSL_VARS:
        val = os.environ.get(var, "").strip()
        if not val:
            continue
        path = Path(val).expanduser()
        if path.is_file():
            return path
    return None


def _bundle_owned_by_current_process(path: Path) -> bool:
    """Reject CA paths baked for a different frozen app (e.g. monitor _internal)."""
    if not path.is_file():
        return False
    resolved = path.expanduser().resolve()
    resolved_s = _normalize(resolved).lower()
    if "_internal" in resolved_s or "_mei" in resolved_s:
        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).resolve().parent
            exe_s = _normalize(exe_dir).lower()
            meipass = getattr(sys, "_MEIPASS", None)
            allowed_roots = {exe_s}
            if meipass:
                allowed_roots.add(_normalize(Path(meipass)).lower())
            if not any(root and root in resolved_s for root in allowed_roots):
                return False
        elif "pythontradingmonitor" in resolved_s:
            return False
    return True


def _clear_stale_ssl_env() -> None:
    for var in _SSL_VARS:
        val = os.environ.get(var, "").strip()
        if not val:
            continue
        path = Path(val).expanduser()
        if not path.is_file() or not _bundle_owned_by_current_process(path):
            os.environ.pop(var, None)


def _apply_bundle(path: Path) -> str:
    bundle = _normalize(path)
    for var in _SSL_VARS:
        os.environ[var] = bundle
    return bundle


def configure_ssl_certificates(*, force: bool = False) -> str | None:
    """
    Point SSL env vars at a real cacert.pem.
    Returns the path used, or None if no bundle was found.
    """
    global _CONFIGURED

    _clear_stale_ssl_env()

    if not force and _CONFIGURED:
        current = _env_bundle_path()
        if current is not None and _bundle_owned_by_current_process(current):
            if _normalize(current) == _CONFIGURED:
                return _CONFIGURED

    existing = _env_bundle_path()
    if existing is not None and _bundle_owned_by_current_process(existing):
        _CONFIGURED = _apply_bundle(existing)
        return _CONFIGURED

    from modules.runtime_paths import resolve_ca_bundle_path

    resolved = resolve_ca_bundle_path()
    if resolved is None:
        return None

    _CONFIGURED = _apply_bundle(resolved)
    return _CONFIGURED
