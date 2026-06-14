"""Windows-safe I/O and atomic JSON persistence (ported from trading bot)."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def safe_print(*args, sep: str = " ", end: str = "\n", file=None, flush: bool = False) -> None:
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
    if not isinstance(sys.stdout, _SafeStream):
        sys.stdout = _SafeStream(sys.stdout)
    if not isinstance(sys.stderr, _SafeStream):
        sys.stderr = _SafeStream(sys.stderr)


def read_json_file(path: Path | str) -> dict:
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def write_json_file(path: Path | str, payload: dict, *, indent: int = 2) -> bool:
    try:
        Path(path).write_text(json.dumps(payload, indent=indent, default=str), encoding="utf-8")
        return True
    except OSError:
        logger.warning("write_json_file failed: %s", path, exc_info=True)
        return False


def write_json_atomic(path: Path | str, payload: Any, *, indent: int = 2) -> bool:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=indent, default=str), encoding="utf-8")
        os.replace(tmp, p)
        return True
    except OSError:
        logger.warning("Atomic write failed for %s", path, exc_info=True)
        try:
            if tmp.is_file():
                tmp.unlink()
        except OSError:
            pass
        return False
