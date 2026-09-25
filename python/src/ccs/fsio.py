"""Safe file IO: atomic JSON writes, tolerant reads, flock locks, JSONL appends (ADR-0013)."""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any

log = logging.getLogger(__name__)

_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def dumps_json(obj: Any) -> str:
    """Canonical JSON text shared with the Swift writer: sorted keys, indent 2, UTF-8, newline."""
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def scrub_surrogates(obj: Any) -> Any:
    """A copy of JSON-like `obj` with lone surrogates replaced by U+FFFD, so it encodes as UTF-8.

    JSON parsing keeps `\\udcff` escapes as lone surrogates, which break every later UTF-8 write.
    """
    if isinstance(obj, str):
        return _SURROGATE_RE.sub("\ufffd", obj)
    if isinstance(obj, dict):
        return {scrub_surrogates(k): scrub_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(scrub_surrogates(v) for v in obj)
    return obj


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Write `text` to `path` atomically: temp file in the same dir, fsync, `os.replace`.

    A symlinked `path` stays a symlink: the link is resolved first and its target is replaced
    (dotfile managers link `settings.json` / `config.json`).
    """
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    _fsync_dir(path.parent)


def atomic_write_json(path: Path, obj: Any, *, mode: int = 0o600) -> None:
    """Atomically write `obj` as canonical JSON (see `dumps_json`)."""
    atomic_write_text(path, dumps_json(obj), mode)


def read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON object; `None` if missing, partial, invalid or not an object. Never raises."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        log.debug("read_json(%s): %s", path, exc)
        return None
    try:
        data = json.loads(text)
    except ValueError as exc:
        log.debug("read_json(%s): invalid JSON: %s", path, exc)
        return None
    if not isinstance(data, dict):
        log.debug("read_json(%s): not a JSON object", path)
        return None
    return data


def append_jsonl(path: Path, obj: Any) -> None:
    """Append one JSON line with a single `O_APPEND` write."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


class FileLock:
    """An exclusive `fcntl.flock` lock on a lock file. Usable as a context manager."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        """Whether this object currently holds the lock."""
        return self._fd is not None

    def acquire(self, blocking: bool = True) -> bool:
        """Take the lock. Non-blocking mode returns False when another holder has it."""
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError:
            os.close(fd)
            return False
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return True

    def release(self) -> None:
        """Release the lock if held."""
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[FileLock]:
    """Blocking exclusive lock for the duration of the `with` block."""
    lock = FileLock(path)
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()


def try_lock(path: Path) -> FileLock | None:
    """Non-blocking lock: a held `FileLock` (caller releases it), or `None` if already locked."""
    lock = FileLock(path)
    return lock if lock.acquire(blocking=False) else None
