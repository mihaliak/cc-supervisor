from __future__ import annotations

import errno
import fcntl
import json
import multiprocessing
import os
import stat
import time
from pathlib import Path

import pytest

from ccs import fsio


def test_atomic_write_json_format_and_mode(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "x.json"
    fsio.atomic_write_json(target, {"b": 1, "a": "💼"})
    text = target.read_text(encoding="utf-8")
    assert text == '{\n  "a": "💼",\n  "b": 1\n}\n'
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert [p.name for p in target.parent.iterdir()] == ["x.json"]  # no temp leftovers


def test_atomic_write_replaces(tmp_path: Path) -> None:
    target = tmp_path / "x.json"
    fsio.atomic_write_json(target, {"v": 1})
    fsio.atomic_write_json(target, {"v": 2})
    assert json.loads(target.read_text())["v"] == 2
    assert len(list(tmp_path.iterdir())) == 1


def test_read_json_tolerant(tmp_path: Path) -> None:
    assert fsio.read_json(tmp_path / "missing.json") is None
    truncated = tmp_path / "t.json"
    truncated.write_text('{"a": 1', encoding="utf-8")
    assert fsio.read_json(truncated) is None
    arr = tmp_path / "a.json"
    arr.write_text("[1, 2]", encoding="utf-8")
    assert fsio.read_json(arr) is None
    binary = tmp_path / "b.json"
    binary.write_bytes(b"\xff\xfe\x00")
    assert fsio.read_json(binary) is None
    ok = tmp_path / "ok.json"
    ok.write_text('{"a": 1}', encoding="utf-8")
    assert fsio.read_json(ok) == {"a": 1}


def test_append_jsonl(tmp_path: Path) -> None:
    target = tmp_path / "e.jsonl"
    fsio.append_jsonl(target, {"n": 1})
    fsio.append_jsonl(target, {"n": 2, "s": "é"})
    lines = target.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["n"] for line in lines] == [1, 2]


def test_try_lock_exclusive(tmp_path: Path) -> None:
    lock_path = tmp_path / "x.lock"
    first = fsio.try_lock(lock_path)
    assert first is not None and first.held
    assert fsio.try_lock(lock_path) is None
    first.release()
    second = fsio.try_lock(lock_path)
    assert second is not None
    second.release()


def _hold_lock(lock_path: str, ready: str, hold_seconds: float) -> None:
    with fsio.file_lock(Path(lock_path)):
        Path(ready).write_text("1")
        time.sleep(hold_seconds)


def test_lock_exclusive_across_processes(tmp_path: Path) -> None:
    lock_path = tmp_path / "x.lock"
    ready = tmp_path / "ready"
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_hold_lock, args=(str(lock_path), str(ready), 1.5))
    proc.start()
    try:
        deadline = time.monotonic() + 20
        while not ready.exists():
            assert time.monotonic() < deadline, "child never took the lock"
            time.sleep(0.05)
        assert fsio.try_lock(lock_path) is None
        started = time.monotonic()
        with fsio.file_lock(lock_path):
            waited = time.monotonic() - started
        assert waited > 0.2
    finally:
        proc.join(20)
    assert proc.exitcode == 0


def test_append_jsonl_completes_short_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`os.write` may write less than asked: the rest is written, not dropped."""
    target = tmp_path / "e.jsonl"
    real_write = os.write

    def short(fd: int, data: bytes) -> int:
        return real_write(fd, bytes(data[:7]))

    monkeypatch.setattr(fsio.os, "write", short)
    fsio.append_jsonl(target, {"n": 1, "pad": "x" * 40})
    monkeypatch.setattr(fsio.os, "write", real_write)
    fsio.append_jsonl(target, {"n": 2})
    lines = target.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["n"] for line in lines] == [1, 2]


def test_failed_append_does_not_glue_onto_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that fails midway (ENOSPC) raises and leaves no fragment behind."""
    target = tmp_path / "e.jsonl"
    fsio.append_jsonl(target, {"n": 0})
    real_write = os.write
    state = {"calls": 0}

    def full_disk(fd: int, data: bytes) -> int:
        state["calls"] += 1
        if state["calls"] == 1:
            return real_write(fd, bytes(data[:5]))
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(fsio.os, "write", full_disk)
    with pytest.raises(OSError):
        fsio.append_jsonl(target, {"n": 1, "pad": "x" * 40})
    monkeypatch.setattr(fsio.os, "write", real_write)
    fsio.append_jsonl(target, {"n": 2})
    lines = target.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["n"] for line in lines] == [0, 2]


def test_append_jsonl_starts_a_new_line_after_a_torn_tail(tmp_path: Path) -> None:
    """A fragment left by a crash mid-write never swallows the next record."""
    target = tmp_path / "e.jsonl"
    target.write_bytes(b'{"n": 0}\n{"n": 1, "tor')
    fsio.append_jsonl(target, {"n": 2})
    lines = target.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1]) == {"n": 2}


def test_lock_retries_when_the_file_is_replaced_before_flock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Standard post-acquire check: the held lock is on the file the path names now."""
    lock_path = tmp_path / "x.lock"
    real_flock = fcntl.flock
    state = {"replaced": False}

    def racing_flock(fd: int, op: int) -> None:
        if not state["replaced"] and op & fcntl.LOCK_EX:
            state["replaced"] = True
            lock_path.unlink()  # another process removed and recreated it meanwhile
            lock_path.write_text("")
        real_flock(fd, op)

    monkeypatch.setattr(fsio.fcntl, "flock", racing_flock)
    lock = fsio.try_lock(lock_path)
    monkeypatch.setattr(fsio.fcntl, "flock", real_flock)
    assert lock is not None and lock._fd is not None
    assert os.fstat(lock._fd).st_ino == os.stat(lock_path).st_ino
    assert lock.is_current()
    assert fsio.try_lock(lock_path) is None  # the file at the path is really locked
    lock.release()


def test_lock_is_current_detects_removal_and_replacement(tmp_path: Path) -> None:
    lock_path = tmp_path / "x.lock"
    lock = fsio.try_lock(lock_path)
    assert lock is not None and lock.is_current()
    lock_path.unlink()
    assert not lock.is_current()
    lock_path.write_text("")
    assert not lock.is_current()
    lock.release()
    assert not lock.is_current()


def test_atomic_write_bytes_fsyncs_file_and_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[int] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(fsio.os, "fsync", spy)
    target = tmp_path / "sub" / "x.plist"
    fsio.atomic_write_bytes(target, b"\x00data", mode=0o644)
    assert target.read_bytes() == b"\x00data"
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o644
    assert len(synced) >= 2  # the file and its directory
    assert [p.name for p in target.parent.iterdir()] == ["x.plist"]
