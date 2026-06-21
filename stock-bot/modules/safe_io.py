"""Windows-safe console and file writes (avoids OSError errno 22 on broken stdout)."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, TextIO

logger = logging.getLogger(__name__)


class _NullStream:
    """Absorb writes when there is no console (PyInstaller --windowed sets stdout=None)."""

    encoding = "utf-8"
    errors = "replace"

    def write(self, data) -> int:
        if not data:
            return 0
        if isinstance(data, bytes):
            return len(data)
        return len(str(data))

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise OSError("no fileno")


class _SafeStream:
    """Wrap a text stream so write/flush ignore Windows EINVAL (broken pipe)."""

    def __init__(self, stream: TextIO | _NullStream):
        self._stream = stream

    def write(self, data):
        if self._stream is None:
            return len(data) if data else 0
        try:
            return self._stream.write(data)
        except (OSError, AttributeError) as exc:
            if isinstance(exc, OSError) and getattr(exc, "errno", None) != 22:
                raise
            return len(data) if data else 0

    def flush(self):
        if self._stream is None:
            return None
        try:
            self._stream.flush()
        except (OSError, AttributeError) as exc:
            if isinstance(exc, OSError) and getattr(exc, "errno", None) != 22:
                raise

    def __getattr__(self, name):
        if self._stream is None:
            raise AttributeError(name)
        return getattr(self._stream, name)


def ensure_stdio_streams() -> None:
    """Replace missing stdout/stderr before logging or print (windowed EXE)."""
    if sys.stdout is None:
        sys.stdout = _NullStream()  # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = _NullStream()  # type: ignore[assignment]


def install_safe_stdout() -> None:
    """Call once at process start before the main trading loop."""
    ensure_stdio_streams()
    if not isinstance(sys.stdout, _SafeStream):
        sys.stdout = _SafeStream(sys.stdout)  # type: ignore[assignment]
    if not isinstance(sys.stderr, _SafeStream):
        sys.stderr = _SafeStream(sys.stderr)  # type: ignore[assignment]


def fatal_startup(message: str, *, exit_code: int = 1) -> None:
    """Log startup failure, show a dialog when there is no console, and exit."""
    ensure_stdio_streams()
    text = str(message).strip()
    log_dir = Path("logs")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "startup_fatal.log").write_text(text + "\n", encoding="utf-8")
    except OSError:
        pass
    try:
        logging.getLogger(__name__).critical(text)
    except Exception:
        pass
    safe_print(f"[FATAL] {text}")
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
                0,
                text[:2000],
                "Weinstein Trading Bot",
                0x00000010,
            )
        except Exception:
            pass
    raise SystemExit(exit_code)


def safe_print(*args, sep: str = " ", end: str = "\n", file=None, flush: bool = False) -> None:
    """Print without crashing when stdout is piped/closed/missing (common on Windows)."""
    target = file if file is not None else sys.stdout
    if target is None:
        target = _NullStream()
    text = sep.join(str(a) for a in args) + end
    try:
        target.write(text)
        if flush:
            target.flush()
    except UnicodeEncodeError:
        text = text.encode("ascii", errors="replace").decode("ascii")
        target.write(text)
        if flush:
            target.flush()
    except (OSError, AttributeError) as exc:
        if isinstance(exc, OSError) and getattr(exc, "errno", None) != 22:
            raise


def read_json_file(path: Path | str) -> dict:
    """Load a JSON object from disk; return {} on missing or corrupt files."""
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def write_json_file(path: Path | str, payload: dict, *, indent: int = 2) -> bool:
    """Write JSON to disk; return False on I/O failure."""
    try:
        Path(path).write_text(json.dumps(payload, indent=indent), encoding="utf-8")
        return True
    except OSError:
        logger.warning("write_json_file failed: %s", path, exc_info=True)
        return False


def write_json_atomic(path: str, payload: Any, *, indent: int = 2) -> None:
    """Atomic JSON write (temp file + replace) with non-JSON-native values stringified."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=indent, default=str)
        os.replace(tmp, path)
    except Exception:
        logger.warning("Atomic write failed for %s", path, exc_info=True)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
