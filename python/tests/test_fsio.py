from __future__ import annotations

import json
import multiprocessing
import os
import stat
import time
from pathlib import Path

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
