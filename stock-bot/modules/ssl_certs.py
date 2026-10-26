"""Ensure valid TLS CA bundle paths for Alpaca/httpx (especially PyInstaller exe)."""

from __future__ import annotations

import logging
import os
import socket
import ssl
import sys
from pathlib import Path

_SSL_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
_PROBE_HOST = "api.alpaca.markets"
_CONFIGURED: str | None = None
logger = logging.getLogger(__name__)


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


def _is_verify_failure(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        "certificate_verify_failed" in text
        or "self-signed certificate" in text
        or "unable to get local issuer certificate" in text
    )


def _handshake(bundle: Path | None, *, timeout: float = 5.0) -> Exception | None:
    """TLS handshake to Alpaca. None means OK; exception is the failure."""
    kwargs: dict[str, str] = {}
    if bundle is not None:
        kwargs["cafile"] = str(bundle)
    ctx = ssl.create_default_context(**kwargs)
    try:
        with socket.create_connection((_PROBE_HOST, 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=_PROBE_HOST):
                return None
    except Exception as exc:
        return exc


def _windows_root_pems() -> list[str]:
    if sys.platform != "win32":
        return []
    try:
        entries = ssl.enum_certificates("ROOT")
    except Exception:
        return []
    pems: list[str] = []
    for der, encoding, _trust in entries:
        if encoding != "x509_asn":
            continue
        try:
            pems.append(ssl.DER_cert_to_PEM_cert(der))
        except Exception:
            continue
    return pems


def _combined_windows_bundle(base: Path) -> Path | None:
    pems = _windows_root_pems()
    if not pems:
        return None
    try:
        from modules.runtime_paths import resolve_data_root, resolve_runtime_root

        out_dir = resolve_data_root(resolve_runtime_root()) / "certifi"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / "combined_cacert.pem"
        parts = [base.read_text(encoding="utf-8", errors="replace"), "\n"]
        parts.extend(pems)
        out.write_text("".join(parts), encoding="utf-8")
        return out
    except OSError as exc:
        logger.warning("Could not write Windows+certifi CA bundle: %s", exc)
        return None


def _usable_bundle(path: Path) -> bool:
    if not path.is_file() or not _bundle_owned_by_current_process(path):
        return False
    err = _handshake(path)
    if err is None:
        return True
    if _is_verify_failure(err):
        logger.warning(
            "CA bundle failed Alpaca TLS (%s): %s",
            path,
            err,
        )
        return False
    # Network blip — keep the file; do not treat as a bad bundle.
    logger.debug("Alpaca TLS probe inconclusive for %s: %s", path, err)
    return True


def configure_ssl_certificates(*, force: bool = False) -> str | None:
    """
    Point SSL env vars at a CA bundle that can verify api.alpaca.markets.

    Rejects inherited/self-signed interceptor files. On Windows, if Mozilla
    certifi cannot verify (antivirus MITM), merge the OS ROOT store.
    """
    global _CONFIGURED

    _clear_stale_ssl_env()

    if not force and _CONFIGURED:
        current = _env_bundle_path()
        if current is not None and _usable_bundle(current):
            if _normalize(current) == _CONFIGURED:
                return _CONFIGURED

    tried: list[Path] = []
    existing = _env_bundle_path()
    if existing is not None:
        tried.append(existing)
        if _usable_bundle(existing):
            _CONFIGURED = _apply_bundle(existing)
            return _CONFIGURED
        for var in _SSL_VARS:
            os.environ.pop(var, None)

    from modules.runtime_paths import resolve_ca_bundle_path

    resolved = resolve_ca_bundle_path()
    if resolved is not None:
        tried.append(resolved)
        if _usable_bundle(resolved):
            _CONFIGURED = _apply_bundle(resolved)
            return _CONFIGURED
        combined = _combined_windows_bundle(resolved)
        if combined is not None:
            tried.append(combined)
            if _usable_bundle(combined):
                logger.warning(
                    "Using Windows ROOT + certifi CA bundle after Alpaca TLS "
                    "verify failed on %s",
                    resolved,
                )
                _CONFIGURED = _apply_bundle(combined)
                return _CONFIGURED

    logger.warning(
        "No CA bundle verified api.alpaca.markets (tried %s)",
        [str(p) for p in tried],
    )
    return None
