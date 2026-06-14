"""Windows-safe console and file writes (avoids OSError errno 22 on broken stdout)."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def safe_print(*args, sep: str = " ", end: str = "\n", file=None, flush: bool = False) -> None:
    """Print without crashing when stdout is piped/closed (common on Windows)."""
    target = file if file is not None else sys.stdout
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
    except OSError as exc:
        if getattr(exc, "errno", None) != 22:
            raise


class _SafeStream:
    """Wrap a text stream so write/flush ignore Windows EINVAL (broken pipe)."""

    def __init__(self, stream):
        self._stream = stream

    def write(self, data):
        try:
            return self._stream.write(data)
        except OSError as exc:
            if getattr(exc, "errno", None) == 22:
                return len(data) if data else 0
            raise

    def flush(self):
        try:
            self._stream.flush()
        except OSError as exc:
            if getattr(exc, "errno", None) != 22:
                raise

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install_safe_stdout() -> None:
    """Call once at process start before the main trading loop."""
    if not isinstance(sys.stdout, _SafeStream):
        sys.stdout = _SafeStream(sys.stdout)
    if not isinstance(sys.stderr, _SafeStream):
        sys.stderr = _SafeStream(sys.stderr)


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
