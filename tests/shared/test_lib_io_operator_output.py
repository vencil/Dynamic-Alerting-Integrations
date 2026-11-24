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
from _platform_fs import symlink_or_skip  # noqa: E402

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
    symlink_or_skip(real, link)
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


def test_the_json_document_after_an_operator_output_on_stdout_is_refused(monkeypatch, capsys):
    """#3107 ④ (owner 2026-11-15): the --json document claims stdout too.
    After `-` (or a path that is stdout) took it, a second document would be
    indistinguishable from the first: nothing more is printed, and the run
    ends at the caller-error code."""
    _lib_io.write_operator_output("-", "first\n", flag="--out")
    capsys.readouterr()
    status = _lib_exitcodes.emit_result(_lib_exitcodes.Report({"a": 1}, 0))
    out, err = capsys.readouterr()
    assert status == _lib_exitcodes.EXIT_CALLER_ERROR == 2
    assert out == ""
    assert "cannot print the --json document: standard output is already used by --out" in err


def test_the_conflict_ends_at_the_tools_caller_error_code(capsys):
    """The run's caller-error code, as every other caller error — a tool
    that set it to 1 (`set_caller_error_code`) ends with 1, not with 2."""
    _lib_exitcodes.set_caller_error_code(1)
    _lib_exitcodes.claim_stdout_owner("--out")
    assert _lib_exitcodes.emit_result(_lib_exitcodes.Report({"a": 1}, 0)) == 1
    assert capsys.readouterr().out == ""


def test_a_best_effort_document_keeps_its_status_when_stdout_is_taken(capsys):
    """As when stdout fails (#3558): a best-effort document's status says
    what happened, whether or not its document could be printed."""
    _lib_exitcodes.claim_stdout_owner("--out")
    status = _lib_exitcodes.emit_result(
        _lib_exitcodes.Report({"a": 1}, 1, delivery="best_effort"))
    out, err = capsys.readouterr()
    assert status == 1 and out == ""
    assert "already used by --out" in err


def test_the_json_document_alone_is_printed(capsys):
    """CONTROL: no other owner — the document is printed and the status kept
    (the claim itself is no conflict)."""
    status = _lib_exitcodes.emit_result(_lib_exitcodes.Report({"a": 1}, 1))
    out, err = capsys.readouterr()
    assert status == 1 and out == '{\n  "a": 1\n}\n' and err == ""


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


# ── The mode of an artifact nobody named a mode for (#3107) ──────────────
# Git records one bit of a file's mode — the owner's execute bit — so that is
# the one bit a rewrite keeps; everything else is what a checkout would give.

@pytest.mark.parametrize("before,after", [
    (0o644, 0o644),
    (0o755, 0o755),
    (0o600, 0o644),   # what the old names left behind: put back
    (0o700, 0o755),   # the execute bit is kept, the rest is not copied
    (0o664, 0o644),   # nor is anything else of the old mode
    (0o654, 0o644),   # a group execute bit is not the bit git tracks
], ids=lambda m: oct(m))
def test_an_artifact_keeps_only_the_execute_bit_of_the_file_it_replaces(tmp_path, before, after):
    p = tmp_path / "a.md"
    p.write_text("old", encoding="utf-8")
    p.chmod(before)
    _lib_io.write_repo_artifact(str(p), "new", flag="--output")
    assert p.read_text(encoding="utf-8") == "new" and _mode(p) == after


def test_a_new_artifact_is_0644(tmp_path):
    p = tmp_path / "a.md"
    _lib_io.write_repo_artifact(str(p), "new", flag="--output")
    assert _mode(p) == 0o644


@pytest.mark.parametrize("existing", [None, 0o644, 0o755, 0o600], ids=str)
def test_the_umask_is_not_read(tmp_path, existing):
    """A 077 umask (a hardened shell) makes a NEW file 0600 and a script
    0700; the artifact's mode is the checkout's, whatever the shell's is."""
    p = tmp_path / "a.md"
    if existing is not None:
        p.write_text("old", encoding="utf-8")
        p.chmod(existing)
    old = os.umask(0o077)
    try:
        _lib_io.write_repo_artifact(str(p), "new", flag="--output")
    finally:
        os.umask(old)
    assert _mode(p) == (0o755 if existing == 0o755 else 0o644)


@pytest.mark.parametrize("existing", [None, 0o644, 0o755], ids=str)
@pytest.mark.parametrize("asked", [0o600, 0o640, 0o644])
def test_a_mode_the_caller_names_is_the_mode(tmp_path, existing, asked):
    """`state-reconcile` and `configmap_assemble` pass 0600 for a manifest
    that holds secrets: the file being replaced has no say in it."""
    p = tmp_path / "m.yaml"
    if existing is not None:
        p.write_text("old", encoding="utf-8")
        p.chmod(existing)
    _lib_io.write_repo_artifact(str(p), "new", flag="--output", mode=asked, atomic_only=True)
    assert _mode(p) == asked


@pytest.mark.parametrize("mode", [0o644, 0o755], ids=oct)
def test_an_artifact_reached_through_a_symlink_has_the_targets_execute_bit(tmp_path, mode):
    """The bit is read from the file, not from the link: a symlink's own mode
    is 0777, so reading the link would make every linked artifact 0755."""
    target = tmp_path / "real.sh"
    target.write_text("old", encoding="utf-8")
    target.chmod(mode)
    link = tmp_path / "link.sh"
    symlink_or_skip(target, link)
    _lib_io.write_repo_artifact(str(link), "new", flag="--output")
    assert target.read_text(encoding="utf-8") == "new" and _mode(target) == mode
    assert link.is_symlink()


def test_the_file_stdout_already_is_counts_as_dash(tmp_path, monkeypatch):
    """R3: reopening the file fd 1 writes to with O_TRUNC would wipe what the
    shell redirect already holds; it is written through stdout instead."""
    p = tmp_path / "out.txt"
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("head\n")
        fh.flush()
        monkeypatch.setattr(sys, "stdout", fh)
        assert _lib_io.targets_stdout(str(p))
        _lib_io.write_operator_output(str(p), "doc\n", flag="--out")
    assert p.read_text(encoding="utf-8") == "head\ndoc\n"


def test_a_failed_write_leaves_the_file_empty_not_a_prefix(tmp_path):
    """R4: a reader never sees half a document."""
    p = tmp_path / "f.json"
    p.write_text("old", encoding="utf-8")
    with pytest.raises(RuntimeError):
        with _lib_io.open_operator_output(str(p), flag="--out") as fh:
            fh.write("half")
            fh.flush()
            raise RuntimeError("boom")
    assert p.read_text(encoding="utf-8") == ""


def test_an_artifact_that_is_our_standard_stream_is_refused(tmp_path, monkeypatch):
    p = tmp_path / "m.yaml"
    p.write_text("old", encoding="utf-8")
    fd = os.open(p, os.O_RDONLY)
    saved = os.dup(0)
    try:
        os.dup2(fd, 0)
        with pytest.raises(_lib_io.OutputWriteError, match="standard stream"):
            _lib_io.write_repo_artifact(str(p), "new", flag="--output")
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(fd)
    assert p.read_text(encoding="utf-8") == "old"


def test_atomic_only_never_falls_back_to_an_in_place_write(tmp_path, monkeypatch):
    """Review of #3107 (M1): a directory that cannot take the tmp used to send
    the shared writer to an in-place write with a WARN (rc 0) — for
    configmap-assemble, the half manifest risk-1 B exists to prevent. With
    atomic_only it is an error and the old file is kept."""
    import _lib_atomic_write
    p = tmp_path / "m.yaml"
    p.write_text("old", encoding="utf-8")

    def no_tmp(*a, **kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(_lib_atomic_write.tempfile, "mkstemp", no_tmp)
    with pytest.raises(_lib_io.OutputWriteError, match="atomically"):
        _lib_io.write_repo_artifact(str(p), "new", flag="--output", atomic_only=True)
    assert p.read_text(encoding="utf-8") == "old"
    # Control: without atomic_only the shared contract's fallback stands.
    _lib_io.write_repo_artifact(str(p), "new", flag="--output")
    assert p.read_text(encoding="utf-8") == "new"


def test_atomic_only_replaces_a_hard_linked_file(tmp_path):
    """As the tools' own tmp+replace did: the name gets a new inode, the other
    link keeps the old content — never an in-place rewrite."""
    p = tmp_path / "m.yaml"
    p.write_text("old", encoding="utf-8")
    other = tmp_path / "other"
    os.link(p, other)
    _lib_io.write_repo_artifact(str(p), "new", flag="--output", atomic_only=True)
    assert p.read_text(encoding="utf-8") == "new" and other.read_text(encoding="utf-8") == "old"


# ── #4055: a `-` output whose stdout fails ends with one line ─────────
#
# Only an output smaller than stdout's buffer showed it: the write is
# buffered, the flush fails, and what stays buffered failed again twice —
# in the sink's finalizer ("Exception ignored" + a traceback) and in the
# last flush at exit (a second ERROR line). A larger output fails in the
# write itself, so a test on a real tool's large output passes either way.

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir,
                          "scripts", "tools")
_ENTRY = """\
import sys
sys.path.insert(0, sys.argv[1])
import _lib_exitcodes, _lib_io
def main():
    _lib_io.write_operator_output_or_die("-", "x" * int(sys.argv[2]), flag="-o/--output")
    return 0
if sys.argv[3] == "run":
    _lib_exitcodes.run(main)
main()
"""


@pytest.mark.parametrize("redirect", [">/dev/full", ">&-"], ids=["ENOSPC", "closed"])
@pytest.mark.parametrize("size", [10, 100_000], ids=["buffered", "unbuffered"])
@pytest.mark.parametrize("way_out", ["run", "atexit"])
def test_a_failed_stdout_output_says_one_line_and_no_traceback(tmp_path, redirect, size, way_out):
    import shlex
    import subprocess
    if redirect == ">/dev/full" and not os.path.exists("/dev/full"):
        pytest.skip("no /dev/full")
    script = tmp_path / "entry.py"
    script.write_text(_ENTRY, encoding="utf-8")
    cmd = " ".join(shlex.quote(a) for a in (
        sys.executable, "-I", str(script), os.path.abspath(_TOOLS_DIR), str(size), way_out))
    done = subprocess.run(["sh", "-c", f"exec {cmd} {redirect}"], stdin=subprocess.DEVNULL,
                          stderr=subprocess.PIPE, text=True, encoding="utf-8",
                          timeout=60)
    lines = done.stderr.splitlines()
    assert done.returncode == 2, done.stderr
    assert len(lines) == 1 and lines[0].startswith("ERROR: cannot write -:"), done.stderr


def test_closing_the_stdout_sink_never_flushes_stdout(monkeypatch):
    """#4055: the sink's finalizer closes it; a flush there of a stdout that
    already failed prints an "Exception ignored" traceback."""
    flushes = []

    class _Broken(io.StringIO):
        def flush(self):
            flushes.append(1)
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(sys, "stdout", _Broken())
    sink = _lib_io._StdoutSink()
    sink.close()
    assert flushes == [] and sink.closed


_OTHER_OSERROR = """\
import sys
sys.path.insert(0, sys.argv[1])
import _lib_exitcodes, _lib_io
def main():
    try:
        with _lib_io.open_operator_output("-", flag="-o") as fh:
            fh.write("partial-doc\\n")
            open(sys.argv[2], encoding="utf-8")  # not stdout's failure
    except FileNotFoundError:
        pass
    print("after")
    return 0
_lib_exitcodes.run(main)
"""

_REBOUND = """\
import sys
sys.path.insert(0, sys.argv[1])
import _lib_exitcodes, _lib_io
real = sys.stdout
sys.stdout = open("/dev/full", "w", encoding="utf-8")
try:
    _lib_io.write_operator_output("-", "x", flag="-o")
except _lib_io.OutputWriteError:
    pass
sys.stdout = real
print("driver output on the real fd 1")
"""


def _run_script(tmp_path, source, *args):
    import subprocess
    script = tmp_path / "entry.py"
    script.write_text(source, encoding="utf-8")
    return subprocess.run([sys.executable, "-I", str(script), os.path.abspath(_TOOLS_DIR), *args],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          encoding="utf-8", timeout=60)


def test_another_oserror_in_the_body_leaves_a_working_stdout_alone(tmp_path):
    """#4055 review: only stdout's own failure drops what it buffers."""
    done = _run_script(tmp_path, _OTHER_OSERROR, str(tmp_path / "missing" / "f"))
    assert done.returncode == 0, done.stderr
    assert done.stdout == "partial-doc\nafter\n"


@pytest.mark.skipif(not os.path.exists("/dev/full"), reason="no /dev/full")
def test_a_rebound_stdout_that_fails_leaves_the_real_fd_1_working(tmp_path):
    done = _run_script(tmp_path, _REBOUND)
    assert done.stdout == "driver output on the real fd 1\n", done.stderr


def test_a_body_error_is_not_replaced_by_a_failed_stdout(monkeypatch):
    """The ``finally`` that closes the sink must not flush a broken stdout
    and raise its OSError over what the body raised."""
    class _Broken(io.StringIO):
        def flush(self):
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(sys, "stdout", _Broken())
    monkeypatch.setattr(_lib_exitcodes, "_stdout_owner", None, raising=False)
    with pytest.raises(ValueError):
        with _lib_io.open_operator_output("-", flag="-o") as fh:
            fh.write("x")
            raise ValueError("the body's own error")


# #4230: only the output handle's own failure is the output path's. Any other
# OSError of the ``with`` body propagates as it is — not an OutputWriteError
# that tells the operator to check the value given to the flag.

_BODY_OSERROR = """\
import sys
sys.path.insert(0, sys.argv[1])
import _lib_exitcodes, _lib_io
def main():
    with _lib_io.open_operator_output(sys.argv[2], flag="-o") as fh:
        fh.write("partial-doc\\n")
        open(sys.argv[3], encoding="utf-8")  # another file: not the output's failure
    return 0
_lib_exitcodes.run(main)
"""


def _body_target(tmp_path, where):
    return "-" if where == "stdout" else str(tmp_path / "out.json")


@pytest.mark.parametrize("where", ["stdout", "file"])
def test_another_oserror_of_the_body_is_not_the_outputs(tmp_path, where, monkeypatch):
    monkeypatch.setattr(_lib_exitcodes, "_stdout_owner", None, raising=False)
    missing = tmp_path / "missing" / "input.yaml"
    with pytest.raises(FileNotFoundError) as caught:
        with _lib_io.open_operator_output(_body_target(tmp_path, where), flag="-o") as fh:
            fh.write("x")
            open(missing, encoding="utf-8")
    assert not isinstance(caught.value, _lib_io.OutputWriteError)
    assert caught.value.filename == str(missing)


@pytest.mark.parametrize("where", ["stdout", "file"])
def test_the_operator_is_not_told_to_check_the_flag_for_another_files_error(tmp_path, where):
    """The acceptance line of #4230, end to end through ``run``."""
    missing = tmp_path / "missing" / "input.yaml"
    done = _run_script(tmp_path, _BODY_OSERROR, _body_target(tmp_path, where), str(missing))
    assert "check the value given to -o" not in done.stderr, done.stderr
    assert "cannot write" not in done.stderr, done.stderr
    assert "OutputWriteError" not in done.stderr, done.stderr
    assert str(missing) in done.stderr, done.stderr


@pytest.mark.skipif(not os.path.exists("/dev/full"), reason="no /dev/full")
def test_a_body_error_with_unflushed_output_stays_the_bodys(tmp_path):
    """The cleanup's own flush fails on /dev/full; it must not turn the
    body's error into the output's (the attribution is read before it)."""
    missing = tmp_path / "missing" / "input.yaml"
    with pytest.raises(FileNotFoundError) as caught:
        with _lib_io.open_operator_output("/dev/full", flag="-o") as fh:
            fh.write("x")
            open(missing, encoding="utf-8")
    assert not isinstance(caught.value, _lib_io.OutputWriteError)


_FILE_WRITE_FAILS = """\
import sys
sys.path.insert(0, sys.argv[1])
import _lib_exitcodes, _lib_io
def main():
    _lib_io.write_operator_output_or_die(sys.argv[2], "x" * int(sys.argv[3]), flag="-o")
    return 0
_lib_exitcodes.run(main)
"""


@pytest.mark.skipif(not os.path.exists("/dev/full"), reason="no /dev/full")
@pytest.mark.parametrize("size", [10, 100_000], ids=["flush", "write"])
def test_a_failed_write_to_a_named_path_is_still_one_line_rc_2(tmp_path, size):
    done = _run_script(tmp_path, _FILE_WRITE_FAILS, "/dev/full", str(size))
    lines = done.stderr.splitlines()
    assert done.returncode == 2, done.stderr
    assert len(lines) == 1, done.stderr
    assert lines[0].startswith("ERROR: cannot write /dev/full:"), done.stderr
    assert "check the value given to -o" in lines[0], done.stderr


@pytest.mark.skipif(not os.path.exists("/dev/full"), reason="no /dev/full")
def test_a_write_the_body_makes_through_the_handle_is_the_outputs():
    """A write that fails inside the body (not the final flush) is still the
    output path's failure."""
    with pytest.raises(_lib_io.OutputWriteError) as caught:
        with _lib_io.open_operator_output("/dev/full", flag="-o") as fh:
            fh.write("x" * 100_000)
            raise AssertionError("not reached: the write above fails")
    assert caught.value.flag == "-o"


@pytest.mark.skipif(not os.path.exists("/dev/full"), reason="no /dev/full")
def test_a_write_through_the_handles_buffer_is_the_outputs():
    """#4230 review: the failure is kept at the buffer, so a caller writing
    bytes through ``fh.buffer`` is covered as the text writes are."""
    with pytest.raises(_lib_io.OutputWriteError):
        with _lib_io.open_operator_output("/dev/full", flag="-o") as fh:
            fh.buffer.write(b"x" * 100_000)
            raise AssertionError("not reached: the write above fails")


@pytest.mark.skipif(not os.path.exists("/dev/full"), reason="no /dev/full")
@pytest.mark.parametrize("where", ["stdout", "file"])
def test_a_swallowed_write_failure_does_not_claim_a_later_error(tmp_path, where, monkeypatch):
    """#4230 review: only the OSError the handle raised is the output's — not
    any later one, after the body caught that one."""
    missing = tmp_path / "missing" / "input.yaml"
    if where == "stdout":
        monkeypatch.setattr(sys, "stdout", open("/dev/full", "w", encoding="utf-8"))
        monkeypatch.setattr(_lib_exitcodes, "_stdout_owner", None, raising=False)
        monkeypatch.setattr(_lib_exitcodes, "discard_failed_stdout", lambda: None)
    target = "-" if where == "stdout" else "/dev/full"
    with pytest.raises(FileNotFoundError) as caught:
        with _lib_io.open_operator_output(target, flag="-o") as fh:
            try:
                fh.write("x" * 100_000)
                fh.flush()
            except OSError:
                pass
            open(missing, encoding="utf-8")
    assert not isinstance(caught.value, _lib_io.OutputWriteError)


def test_the_file_handle_is_what_io_open_would_give(tmp_path):
    p = tmp_path / "f.txt"
    with _lib_io.open_operator_output(str(p), flag="-o") as fh:
        assert fh.mode == "w" and fh.encoding == "utf-8"
        assert fh.line_buffering is False
        assert fh.buffer.raw.name == fh.fileno()


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="no pty")
def test_a_terminal_output_is_line_buffered():
    """As io.open: a line written to a terminal shows when it ends."""
    master, slave = os.openpty()
    try:
        with _lib_io.open_operator_output(os.ttyname(slave), flag="-o") as fh:
            assert fh.line_buffering is True
            fh.write("line\n")
            assert os.read(master, 100).replace(b"\r", b"") == b"line\n"
    finally:
        os.close(master)
        os.close(slave)
