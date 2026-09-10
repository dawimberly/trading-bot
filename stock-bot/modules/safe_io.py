"""Windows-safe console and file writes (avoids OSError errno 22 on broken stdout)."""

from __future__ import annotations

import json
import logging
import os
import random
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, TextIO

logger = logging.getLogger(__name__)

# Retry tuning for contested JSON side-effect files (MC + live paper bot).
_JSON_LOCK_TIMEOUT_SEC = float(os.getenv("JSON_LOCK_TIMEOUT_SEC", "45"))
_JSON_WRITE_RETRIES = max(1, int(os.getenv("JSON_WRITE_RETRIES", "12")))
_JSON_WRITE_RETRY_BASE_SEC = float(os.getenv("JSON_WRITE_RETRY_BASE_SEC", "0.05"))


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
        except UnicodeEncodeError:
            # Windows cp1252 consoles choke on ≥ → etc. in banners; never kill the bot.
            text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
            safe = text.encode(getattr(self._stream, "encoding", None) or "ascii", errors="replace").decode(
                getattr(self._stream, "encoding", None) or "ascii",
                errors="replace",
            )
            try:
                return self._stream.write(safe)
            except Exception:
                return len(data) if data else 0
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
        with _json_file_lock(str(p), shared=True):
            data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _lock_path_for(path: str | Path) -> Path:
    return Path(f"{path}.lock")


def _unique_tmp_path(path: str | Path) -> str:
    pid = os.getpid()
    tid = threading.get_ident()
    nonce = random.randint(0, 999_999)
    return f"{path}.tmp.{pid}.{tid}.{nonce}"


@contextmanager
def _json_file_lock(path: str | Path, *, shared: bool = False) -> Iterator[None]:
    """Cross-process lock for JSON read/write (paper bot + Monte Carlo)."""
    lock_path = _lock_path_for(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _JSON_LOCK_TIMEOUT_SEC
    fd: int | None = None
    while time.monotonic() < deadline:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
            os.lseek(fd, 0, os.SEEK_SET)
            if sys.platform == "win32":
                import msvcrt

                # Non-blocking so the outer deadline is honored (LK_LOCK blocks ~10s).
                mode = msvcrt.LK_NBLCK if not shared else msvcrt.LK_NBRLCK
                msvcrt.locking(fd, mode, 1)
            else:
                import fcntl

                flag = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
                fcntl.flock(fd, flag | fcntl.LOCK_NB)
            break
        except OSError:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                fd = None
            time.sleep(_JSON_WRITE_RETRY_BASE_SEC + random.random() * _JSON_WRITE_RETRY_BASE_SEC)
    if fd is None:
        raise TimeoutError(f"timed out acquiring lock for {path}")
    try:
        yield
    finally:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def _replace_with_retry(src: str, dst: str) -> None:
    last_exc: Exception | None = None
    for attempt in range(_JSON_WRITE_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            last_exc = exc
            delay = _JSON_WRITE_RETRY_BASE_SEC * (2**attempt) + random.random() * 0.02
            time.sleep(min(delay, 2.0))
        except OSError as exc:
            if getattr(exc, "winerror", None) == 32 or getattr(exc, "errno", None) in (13, 16):
                last_exc = exc
                delay = _JSON_WRITE_RETRY_BASE_SEC * (2**attempt) + random.random() * 0.02
                time.sleep(min(delay, 2.0))
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise OSError(f"replace failed: {src} -> {dst}")


def write_json_file(path: Path | str, payload: dict, *, indent: int = 2) -> bool:
    """Write JSON to disk; ensure parent directory exists and return False on I/O failure."""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with _json_file_lock(str(p)):
            tmp = _unique_tmp_path(p)
            try:
                Path(tmp).write_text(json.dumps(payload, indent=indent), encoding="utf-8")
                _replace_with_retry(tmp, str(p))
            finally:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
        return True
    except OSError:
        logger.warning("write_json_file failed: %s", path, exc_info=True)
        return False


DEFAULT_APPEND_MAX_BYTES = 50 * 1024 * 1024  # 50 MB
DEFAULT_APPEND_BACKUPS = 3


def rotate_oversized_file(
    path: Path | str,
    *,
    max_bytes: int = DEFAULT_APPEND_MAX_BYTES,
    backup_count: int = DEFAULT_APPEND_BACKUPS,
) -> None:
    """Rename path -> path.1 .. path.N when over max_bytes (best-effort)."""
    p = Path(path)
    try:
        if not p.is_file() or p.stat().st_size < max(1024 * 1024, int(max_bytes)):
            return
    except OSError:
        return
    n = max(1, int(backup_count))
    try:
        last = Path(f"{p}.{n}")
        if last.exists():
            last.unlink()
        for i in range(n - 1, 0, -1):
            src = Path(f"{p}.{i}")
            if src.exists():
                src.replace(Path(f"{p}.{i + 1}"))
        p.replace(Path(f"{p}.1"))
    except OSError:
        # Last resort: truncate so disk cannot fill forever under locks.
        try:
            size = p.stat().st_size if p.is_file() else 0
            if size >= max(int(max_bytes) * 2, int(max_bytes) + 1):
                p.write_text("", encoding="utf-8")
        except OSError:
            logger.debug("rotate_oversized_file failed for %s", p, exc_info=True)


def append_text_rotating(
    path: Path | str,
    line: str,
    *,
    max_bytes: int = DEFAULT_APPEND_MAX_BYTES,
    backup_count: int = DEFAULT_APPEND_BACKUPS,
) -> bool:
    """Append a line with size-based rotation (for audit JSONL / plain logs)."""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        rotate_oversized_file(p, max_bytes=max_bytes, backup_count=backup_count)
        text = line if line.endswith("\n") else line + "\n"
        with p.open("a", encoding="utf-8") as handle:
            handle.write(text)
        return True
    except OSError:
        logger.warning("append_text_rotating failed: %s", p, exc_info=True)
        return False


def append_jsonl_line(path: Path | str, payload: dict[str, Any]) -> bool:
    """Append one JSON object as a line; return False on I/O failure.

    Rotates the file at ~50 MB (3 backups) so audit JSONL cannot fill the disk.
    """
    try:
        return append_text_rotating(path, json.dumps(payload, default=str))
    except (TypeError, ValueError):
        logger.warning("append_jsonl_line encode failed: %s", path, exc_info=True)
        return False


def write_json_atomic(path: str | Path, payload: Any, *, indent: int = 2) -> None:
    """Atomic JSON write (temp file + replace) with lock and retry."""
    path = str(path)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = _unique_tmp_path(path)
    try:
        with _json_file_lock(path):
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=indent, default=str)
            _replace_with_retry(tmp, path)
    except Exception:
        logger.warning("Atomic write failed for %s", path, exc_info=True)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def update_json_atomic(
    path: str | Path,
    mutator,
    *,
    default: dict | None = None,
    indent: int = 2,
) -> bool:
    """Read-modify-write JSON under one exclusive lock (retry on replace contention).

    ``mutator(payload)`` may mutate and/or return the new dict. Returns False on soft failure.
    """
    path = str(path)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    base = dict(default) if default is not None else {}
    tmp = _unique_tmp_path(path)
    try:
        with _json_file_lock(path):
            payload: dict = dict(base)
            if os.path.isfile(path):
                try:
                    with open(path, encoding="utf-8") as handle:
                        loaded = json.load(handle)
                    if isinstance(loaded, dict):
                        payload = loaded
                except (OSError, json.JSONDecodeError, TypeError):
                    payload = dict(base)
            updated = mutator(payload)
            if isinstance(updated, dict):
                payload = updated
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=indent, default=str)
            _replace_with_retry(tmp, path)
        return True
    except Exception:
        logger.warning("Atomic update failed for %s", path, exc_info=True)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False
