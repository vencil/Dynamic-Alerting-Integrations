"""#3107 v2 output entry points in `_lib_io` (owner 2026-10-21 / 10-24).

Operator output: the kernel opens the path; a regular file this euid owns,
not reached through a symlink, ends 0600 — created now or already there
(V15); `-` is stdout, claimed once per run. Repo artifact: refused unless the
target is absent or a regular file, then replaced atomically.
"""
from __future__ import annotations

import io
import os
import stat
import sys

import pytest

import _lib_exitcodes  # noqa: E402  (tests/conftest.py puts scripts/tools on sys.path)
import _lib_io  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")


def _mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def test_a_new_file_is_created_0600(tmp_path):
    p = tmp_path / "f.json"
    _lib_io.write_operator_output(str(p), "x\n", flag="--out")
    assert p.read_text(encoding="utf-8") == "x\n" and _mode(p) == 0o600


def test_an_existing_own_file_is_tightened_before_the_write(tmp_path):
    """V15: an operator-made 0644 file would leave the output world-readable."""
    p = tmp_path / "f.json"
    p.write_text("old", encoding="utf-8")
    p.chmod(0o644)
    _lib_io.write_operator_output(str(p), "new", flag="--out")
    assert p.read_text(encoding="utf-8") == "new" and _mode(p) == 0o600


def test_a_symlink_target_keeps_its_mode_and_the_link_stays(tmp_path):
    real = tmp_path / "real.json"
    real.write_text("old", encoding="utf-8")
    real.chmod(0o644)
    link = tmp_path / "f.json"
    link.symlink_to(real)
    _lib_io.write_operator_output(str(link), "new", flag="--out")
    assert link.is_symlink() and real.read_text(encoding="utf-8") == "new"
    assert _mode(real) == 0o644


def test_dash_is_stdout_and_a_second_owner_is_refused(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    _lib_io.write_operator_output("-", "doc\n", flag="--findings-json")
    assert buf.getvalue() == "doc\n"
    with pytest.raises(_lib_io.OutputWriteError, match="already used by --findings-json"):
        _lib_io.write_operator_output("-", "two\n", flag="--jwks-out")


def test_each_run_starts_with_stdout_unclaimed():
    """Risk 4 of the v2 design: the claim is the run's state, not the
    process's — `run()` clears it, so a second run in one process may claim."""
    assert _lib_exitcodes.claim_stdout_owner("earlier-run") is None
    seen = []

    def entry():
        seen.append(_lib_exitcodes.claim_stdout_owner("this-run"))
        return 0

    with pytest.raises(SystemExit) as exc:
        _lib_exitcodes.run(entry)
    assert exc.value.code == 0 and seen == [None]


def test_a_fifo_without_a_reader_is_an_error_not_a_hang(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(_lib_io.OutputWriteError):
        _lib_io.write_operator_output(str(fifo), "x", flag="--out")


@pytest.mark.parametrize("make", ["dash", "fifo", "dir"])
def test_an_artifact_that_is_not_a_regular_file_is_refused(tmp_path, make):
    if make == "dash":
        path = "-"
    elif make == "fifo":
        path = str(tmp_path / "pipe")
        os.mkfifo(path)
    else:
        path = str(tmp_path)
    with pytest.raises(_lib_io.OutputWriteError):
        _lib_io.write_repo_artifact(path, "x", flag="--output")


def test_an_artifact_is_replaced_and_ends_with_the_mode_asked(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text("old", encoding="utf-8")
    p.chmod(0o644)
    ino = os.stat(p).st_ino
    _lib_io.write_repo_artifact(str(p), "new", flag="--output", mode=0o600)
    assert p.read_text(encoding="utf-8") == "new" and _mode(p) == 0o600
    assert os.stat(p).st_ino != ino  # replaced, not rewritten in place
