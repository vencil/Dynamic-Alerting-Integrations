"""#4015: ``_lib_consumer_read._unopenable`` judges and reads one fd.

The read happens before the Go program runs, so no subprocess timeout
covers it: a path that is a regular file when judged and a FIFO when opened
would block an ordinary open until a writer comes. ``_unopenable`` opens
non-blocking and judges the fd it opened (``fstat``), so the file judged is
the file read.

Every call here runs under a deadline of its own (``_within``): a reading
that blocks is a failure of the test, not a hung suite.
"""
from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest

import _lib_consumer_read as cr
from _platform_fs import require_os_attrs, symlink_or_skip  # noqa: E402

_DEADLINE = 10.0


def _within(path: Path, call=None) -> "str | None":
    """``_unopenable(path)`` (or *call*) in a thread joined with a deadline.
    A call still blocked at the deadline fails the test; a FIFO it waits on
    is then opened for writing once, so the thread can end."""
    out: dict = {}

    def run() -> None:
        try:
            out["got"] = (call or cr._unopenable)(str(path))
        except BaseException as e:  # noqa: BLE001 — re-raised in the test thread
            out["err"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(_DEADLINE)
    if t.is_alive():
        try:
            if stat.S_ISFIFO(os.lstat(path).st_mode):
                os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass
        t.join(_DEADLINE)
        pytest.fail(f"_unopenable({path}) blocked for {_DEADLINE}s")
    if "err" in out:
        raise out["err"]
    return out["got"]


def test_a_path_swapped_to_a_fifo_after_it_was_judged_does_not_block(tmp_path, monkeypatch):
    """Fault injection: the path is a regular file at the first look (a
    ``stat``, or the open itself) and a FIFO by the time it is opened. The
    judgement must be of what was opened: ``NOT_REGULAR``, at once."""
    require_os_attrs("mkfifo")
    path = tmp_path / "rules.yml"
    path.write_text("groups: []\n", encoding="utf-8")
    swapped = []

    def swap() -> None:
        if not swapped:
            swapped.append(True)
            os.unlink(path)
            os.mkfifo(path)

    real_stat, real_open = os.stat, os.open

    def stat_then_swap(p, *a, **kw):
        st = real_stat(p, *a, **kw)
        if os.fspath(p) == str(path):
            swap()
        return st

    def swap_then_open(p, *a, **kw):
        if os.fspath(p) == str(path):
            swap()
        return real_open(p, *a, **kw)

    monkeypatch.setattr(os, "stat", stat_then_swap)
    monkeypatch.setattr(os, "open", swap_then_open)
    assert _within(path) == cr.NOT_REGULAR
    assert swapped, "the path was never looked at"


def _regular(tmp_path: Path) -> Path:
    p = tmp_path / "real.yml"
    p.write_text("groups: []\n" * 100_000, encoding="utf-8")  # > one 1 MiB read
    return p


def _symlink(tmp_path: Path) -> Path:
    link = tmp_path / "link.yml"
    symlink_or_skip(_regular(tmp_path), link)
    return link


def _directory(tmp_path: Path) -> Path:
    return tmp_path


def _fifo(tmp_path: Path) -> Path:
    require_os_attrs("mkfifo")
    p = tmp_path / "fifo.yml"
    os.mkfifo(p)
    return p


def _dev_null(_tmp_path: Path) -> Path:
    if not Path("/dev/null").exists():
        pytest.skip("needs /dev/null")
    return Path("/dev/null")


def _dev_zero(_tmp_path: Path) -> Path:
    if not Path("/dev/zero").exists():
        pytest.skip("needs /dev/zero")
    return Path("/dev/zero")


def _missing(tmp_path: Path) -> Path:
    return tmp_path / "absent.yml"


def _dangling(tmp_path: Path) -> Path:
    link = tmp_path / "dangling.yml"
    symlink_or_skip(tmp_path / "absent.yml", link)
    return link


def _no_read_permission(tmp_path: Path) -> Path:
    require_os_attrs("geteuid")
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file: the permission refusal cannot be made here")
    p = tmp_path / "secret.yml"
    p.write_text("groups: []\n", encoding="utf-8")
    p.chmod(0)
    return p


@pytest.mark.parametrize("make,want", [
    (_regular, None),
    (_symlink, None),
    (_directory, cr.NOT_REGULAR),
    (_fifo, cr.NOT_REGULAR),
    (_dev_null, cr.NOT_REGULAR),
    (_dev_zero, cr.NOT_REGULAR),
    (_missing, f"{cr.CANNOT_OPEN}: No such file or directory"),
    (_dangling, f"{cr.CANNOT_OPEN}: No such file or directory"),
    (_no_read_permission, f"{cr.CANNOT_OPEN}: Permission denied"),
], ids=lambda v: getattr(v, "__name__", "").lstrip("_") or None)
def test_each_kind_of_path(tmp_path, make, want) -> None:
    assert _within(make(tmp_path)) == want


def test_an_open_the_system_refuses_is_named_by_its_reason(tmp_path, monkeypatch) -> None:
    """The reason shape when the open fails (EACCES), also where the suite
    runs as root and a mode-000 file still opens (``_no_read_permission``
    skips there): the failure is injected at the open."""
    import errno

    def refuse(p, *a, **kw):
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), os.fspath(p))

    path = _regular(tmp_path)
    monkeypatch.setattr(os, "open", refuse)
    monkeypatch.setattr("builtins.open", refuse)
    assert _within(path) == f"{cr.CANNOT_OPEN}: Permission denied"


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc/self/fd")
def test_the_fd_is_closed_whatever_the_answer(tmp_path) -> None:
    paths = [_regular(tmp_path), tmp_path, _missing(tmp_path)]
    if hasattr(os, "mkfifo"):
        paths.append(_fifo(tmp_path))
    before = len(os.listdir("/proc/self/fd"))
    for p in paths * 20:
        _within(p)
    assert len(os.listdir("/proc/self/fd")) == before
