"""Contract for ``_lib_io.output_write`` / ``exit_on_output_write_error`` (#1789).

WHAT THESE TWO HELPERS ARE FOR
------------------------------
#1641 closed the write-failure class for paths that go through the secure
writers. It could not close it for the sites that write RAW — ``open()``,
``Path.mkdir()``, ``shutil.copy2()``, a ``csv.writer`` holding the handle —
because routing those through ``write_text_secure`` would change the bytes or
the permissions of the SUCCESS path. ``output_write`` closes the same class by
WRAPPING such a site instead of converting it: on success it does nothing at
all, and on an ``OSError`` that is about the output path it re-raises the one
``OutputWriteError`` the rest of the toolchain already knows how to print.

THE ONE JUDGEMENT CALL, AND WHY IT IS TESTED FROM BOTH SIDES
------------------------------------------------------------
A ``with`` block is a lexical region, not a path filter: an input file read
inside it, or the SOURCE half of a copy, raises the same ``OSError`` family as
the output write. Attributing those to the output flag would print
``check the value given to --output`` for a broken ``--sources``. So the
helper matches ``exc.filename`` / ``exc.filename2`` against the wrapped path
and lets everything else fly. Both directions are pinned below: the
CONVERTED cases would go quiet (traceback at rc=1 again) if the match arm
were deleted, and the PASS-THROUGH cases would start lying about which flag
to check if the match arm were widened to "any OSError in the block".

Everything here runs against a real ``tmp_path``: no mocks, no monkeypatched
``open``. The exceptions are the ones the kernel actually produces, with the
``errno`` and ``filename`` values it actually sets — a hand-built
``OSError(errno.ENOTDIR, "x")`` would let a helper that only ever looks at
``strerror`` pass (rulebook D-05d: the discriminating power must come from
the code under test, not from the fixture).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import _lib_io  # noqa: E402  (sys.path via tests/conftest.py)
from _lib_io import OutputWriteError, output_write  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "scripts" / "tools"
TIMEOUT_S = 60


@pytest.fixture
def blocker(tmp_path: Path) -> Path:
    """A regular FILE standing where a directory would have to be.

    Every "cannot write there" shape below is built from this rather than
    from permission bits: CI and the dev container run as uid 0, where
    ``chmod 0o500`` is ignored and the test would pass vacuously.
    """
    p = tmp_path / "blocker"
    p.write_text("this is a file, not a directory\n", encoding="utf-8")
    return p


# ═══════════════════════════════════════════════════════════════════════════
# (a) + (d) — the CONVERT half: the failure is about the wrapped path
# ═══════════════════════════════════════════════════════════════════════════
def test_mkdir_under_a_file_is_converted_and_names_the_flag(blocker: Path):
    """Deleting ``output_write``'s ``except OSError`` arm — or the ``candidate ==
    target`` arm of ``_output_write_names_target`` — makes this red.

    ``Path.mkdir`` sets ``filename`` to the path it was asked to create, which
    IS the wrapped path: the plainest convertible shape there is.
    """
    out = blocker / "run"
    with pytest.raises(OutputWriteError) as ei:
        with output_write(out, flag="-o/--output-dir", action="create directory"):
            out.mkdir(parents=True, exist_ok=True)

    exc = ei.value
    assert isinstance(exc, OSError), "must stay catchable by a pre-existing except OSError"
    assert exc.path == str(out)
    assert exc.flag == "-o/--output-dir"
    assert exc.action == "create directory"
    assert exc.errno is not None and exc.strerror
    assert isinstance(exc.__cause__, OSError) and not isinstance(exc.__cause__, OutputWriteError)
    assert str(exc) == (f"cannot create directory {out}: {exc.strerror} "
                        f"(errno {exc.errno}) — check the value given to -o/--output-dir")


def test_open_for_write_under_a_file_is_converted(blocker: Path):
    """Narrowing the helper to ``Path.mkdir``-shaped failures only (say, by
    testing ``isinstance(exc, NotADirectoryError)`` on the mkdir call alone)
    makes this red: it is the raw ``open('w')`` site, the commonest one."""
    out = blocker / "report.txt"
    with pytest.raises(OutputWriteError) as ei:
        with output_write(out, flag="--out"):
            with open(out, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("x\n")
    assert ei.value.action == "write"
    assert "--out" in str(ei.value)


def test_makedirs_intermediate_component_is_converted_as_an_ancestor(tmp_path: Path):
    """Dropping the ANCESTOR arm (``candidate in target.parents``) makes this
    red — and that regression is invisible to every other test here.

    ``os.makedirs("a/b/c")`` blocked at ``a`` reports ``filename == "<tmp>/a/b"``:
    the component it could not create, which is never the path the caller
    named. Matching on equality alone would let this fly as a bare
    ``NotADirectoryError`` traceback at rc=1.
    """
    (tmp_path / "a").write_text("file where a directory should be\n", encoding="utf-8")
    out = tmp_path / "a" / "b" / "c"
    with pytest.raises(OutputWriteError) as ei:
        with output_write(out, flag="--output-dir", action="create directory"):
            os.makedirs(out)
    assert ei.value.path == str(out)
    # The kernel named the intermediate component, not `out`; the message
    # still names the operator's path, which is the one they can fix.
    assert str(out) in str(ei.value)


def test_relative_and_absolute_spellings_of_the_same_path_match(tmp_path: Path, monkeypatch):
    """Comparing the raw strings instead of ``Path(...).resolve(strict=False)``
    makes this red: the tool holds ``out/report.txt`` (relative, straight from
    argv) while the kernel reports the absolute path it resolved."""
    (tmp_path / "out").write_text("blocker\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rel = Path("out") / "report.txt"
    with pytest.raises(OutputWriteError) as ei:
        with output_write(rel, flag="-o/--output"):
            rel.write_text("x", encoding="utf-8")
    assert ei.value.path == str(rel)


def test_oserror_with_no_filename_is_converted(blocker: Path):
    """Requiring ``exc.filename`` to be set makes this red.

    Plenty of failures arrive bare — an ``OSError`` a helper re-raised, an
    ``errno`` ``shutil`` built itself. Inside a block that exists only to
    produce the output, bare means the output.
    """
    with pytest.raises(OutputWriteError) as ei:
        with output_write(blocker / "x", flag=None):
            raise OSError(28, "No space left on device")
    assert ei.value.flag is None
    assert "internal output path" in str(ei.value)


def test_either_filename_matching_is_enough(tmp_path: Path, blocker: Path):
    """Reading only ``exc.filename`` and ignoring ``filename2`` makes this red.

    ``os.replace`` / ``shutil.move`` set BOTH: source in ``filename``,
    destination in ``filename2``. The destination is the output.
    """
    src = tmp_path / "staged.txt"
    src.write_text("payload\n", encoding="utf-8")
    dst = blocker / "final.txt"
    with pytest.raises(OutputWriteError) as ei:
        with output_write(dst, flag="--manifest-path"):
            os.replace(src, dst)
    assert ei.value.path == str(dst)


# ═══════════════════════════════════════════════════════════════════════════
# (b) + (c) — the PASS-THROUGH half: the failure is about some other file
# ═══════════════════════════════════════════════════════════════════════════
def test_missing_input_read_inside_the_block_flies_through(tmp_path: Path):
    """Widening the helper to "any OSError inside the block" makes this red.

    A wrapped region usually reads its inputs too. Converting this would
    print ``cannot write <output> … check the value given to -o`` for a
    mistyped INPUT path, sending the operator to the one flag that is right.
    """
    out = tmp_path / "report.txt"
    missing = tmp_path / "absent-input.yaml"
    with pytest.raises(FileNotFoundError) as ei:
        with output_write(out, flag="-o/--output"):
            data = missing.read_text(encoding="utf-8")
            out.write_text(data, encoding="utf-8")
    assert not isinstance(ei.value, OutputWriteError)
    assert ei.value.filename == str(missing)
    assert not out.exists()


def test_copy2_with_a_directory_source_flies_through(tmp_path: Path):
    """Same widening, caught from the shape that motivated the rule.

    ``ops/assemble_config_dir`` copies each ``--sources`` entry into the
    ``--output`` directory with ``shutil.copy2(src, dst)``. A source tree
    holding a DIRECTORY named ``x.yaml`` (measured, not hypothetical) raises
    ``IsADirectoryError`` whose ``filename`` is the source. The destination
    directory is perfectly writable, so naming ``--output`` here would be a
    lie the operator cannot act on.
    """
    src = tmp_path / "sources" / "x.yaml"
    src.mkdir(parents=True)
    out_dir = tmp_path / "build"
    out_dir.mkdir()
    dst = out_dir / "x.yaml"
    with pytest.raises(IsADirectoryError) as ei:
        with output_write(dst, flag="--output", action="copy into"):
            shutil.copy2(src, dst)
    assert not isinstance(ei.value, OutputWriteError)
    assert ei.value.filename == str(src)


def test_a_sibling_output_is_not_this_wrapped_path(tmp_path: Path):
    """Matching on "shares a parent with" (or on the directory rather than the
    file) instead of "is the path or an ancestor of it" makes this red."""
    (tmp_path / "d").mkdir()
    other = tmp_path / "d" / "other"
    other.write_text("blocker\n", encoding="utf-8")
    mine = tmp_path / "d" / "mine.txt"
    with pytest.raises(OSError) as ei:
        with output_write(mine, flag="-o/--output"):
            (other / "nested").write_text("x", encoding="utf-8")
    assert not isinstance(ei.value, OutputWriteError)


# ═══════════════════════════════════════════════════════════════════════════
# (e) — no double wrapping, and non-OSError is none of our business
# ═══════════════════════════════════════════════════════════════════════════
def test_inner_output_write_error_on_the_same_path_is_not_wrapped_again(blocker: Path):
    """Dropping the ``except OutputWriteError: raise`` arm makes this red.

    ``OutputWriteError`` IS an ``OSError`` and carries the failing path in
    ``filename``, so on the SAME path it satisfies the match rule and would be
    wrapped a second time — the outer ``flag`` would overwrite the accurate
    inner one and the real cause would sit one ``__cause__`` deeper. This is
    the shape that actually occurs: a block wrapped for its raw ``open`` also
    calls a secure writer for a sibling artefact under the same output path.

    ⚠️ The same assertion on a DIFFERENT inner path proves nothing — that one
    survives on the filename rule alone, with or without the arm (measured).
    """
    target = blocker / "same.json"
    with pytest.raises(OutputWriteError) as ei:
        with output_write(target, flag="--outer-flag", action="create directory"):
            _lib_io.write_json_secure(str(target), {"a": 1}, flag="--inner-flag")
    exc = ei.value
    assert exc.path == str(target)
    assert exc.flag == "--inner-flag", "the outer flag must not overwrite the inner one"
    assert exc.action == "write", "the outer action must not overwrite the inner one"
    assert not isinstance(exc.__cause__, OutputWriteError), "wrapped twice"


def test_inner_output_write_error_on_another_path_is_not_wrapped_either(blocker: Path):
    """Same arm, second shape: an inner error about a path OUTSIDE the wrapped
    one must keep its own identity rather than be re-attributed."""
    with pytest.raises(OutputWriteError) as ei:
        with output_write(blocker / "outer.txt", flag="--outer-flag"):
            _lib_io.write_json_secure(str(blocker / "inner.json"), {"a": 1},
                                      flag="--inner-flag")
    assert ei.value.path == str(blocker / "inner.json")
    assert ei.value.flag == "--inner-flag"


def test_non_oserror_flies_through(tmp_path: Path):
    """Catching ``Exception`` instead of ``OSError`` makes this red: a
    ``TypeError`` from a non-serialisable payload is a programming error, and
    reporting it as "cannot write, check your -o" hides the real bug."""
    with pytest.raises(TypeError):
        with output_write(tmp_path / "out.json", flag="-o/--output"):
            raise TypeError("Object of type set is not JSON serializable")


def test_success_path_writes_the_exact_bytes_and_mode(tmp_path: Path):
    """Making ``output_write`` do anything on the success path — chmod, an
    atomic-rename dance, a newline fixup — makes this red.

    The reason raw sites are WRAPPED rather than converted to the secure
    writers is that their bytes and their permission bits must not move.
    """
    out = tmp_path / "keep.txt"
    with output_write(out, flag="-o/--output"):
        with open(out, "wb") as fh:
            fh.write(b"a\r\nb\n")
        os.chmod(out, 0o644)
    assert out.read_bytes() == b"a\r\nb\n"
    assert out.stat().st_mode & 0o777 == 0o644


# ═══════════════════════════════════════════════════════════════════════════
# (f) — the decorator: rc 2, exactly one line, no traceback
# ═══════════════════════════════════════════════════════════════════════════
_DECORATED_MAIN = """\
import sys
sys.path.insert(0, {tools!r})
from _lib_io import exit_on_output_write_error, output_write
from _lib_exitcodes import EXIT_OK


@exit_on_output_write_error
def main() -> int:
    with output_write({out!r}, flag="-o/--output"):
        with open({out!r}, "w", encoding="utf-8") as fh:
            fh.write("never reached\\n")
    return EXIT_OK


sys.exit(main())
"""

_DECORATED_MAIN_OTHER = """\
import sys
sys.path.insert(0, {tools!r})
from _lib_io import exit_on_output_write_error


@exit_on_output_write_error
def main() -> int:
    raise ValueError("some other bug")


sys.exit(main())
"""

_DECORATED_MAIN_INPUT_OSERROR = """\
import sys
sys.path.insert(0, {tools!r})
from _lib_io import exit_on_output_write_error


@exit_on_output_write_error
def main() -> int:
    # An INPUT that is not there: a real OSError, but not an OutputWriteError.
    with open({missing!r}, encoding="utf-8") as fh:
        return len(fh.read())


sys.exit(main())
"""


def _run_script(tmp_path: Path, source: str) -> subprocess.CompletedProcess:
    script = tmp_path / "decorated_tool.py"
    script.write_text(textwrap.dedent(source), encoding="utf-8")
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=TIMEOUT_S, env=env)


def test_decorator_exits_2_with_one_line_and_no_traceback(tmp_path: Path, blocker: Path):
    """Removing ``@exit_on_output_write_error`` from a tool's ``main`` — or
    letting it exit 1 — makes this red.

    Run in a REAL subprocess, not with ``pytest.raises(SystemExit)``: whether
    a traceback reaches stderr is decided by the interpreter after ``main``
    returns, and an in-process assertion cannot see it at all.
    """
    out = blocker / "report.txt"
    proc = _run_script(tmp_path, _DECORATED_MAIN.format(tools=str(TOOLS_DIR), out=str(out)))

    assert proc.returncode == 2, f"rc={proc.returncode}\n{proc.stderr}"
    assert "Traceback" not in proc.stderr, proc.stderr
    lines = [line for line in proc.stderr.splitlines() if line.strip()]
    assert len(lines) == 1, proc.stderr
    assert lines[0].startswith("ERROR: cannot write ")
    assert str(out) in lines[0]
    assert "check the value given to -o/--output" in lines[0]
    assert proc.stdout == ""


def test_decorator_leaves_other_exceptions_alone(tmp_path: Path):
    """Broadening the decorator to ``except OSError`` (or ``Exception``) makes
    this red: an unrelated failure must keep its traceback and its rc=1, not
    be relabelled as an output-path problem with a flag to check."""
    proc = _run_script(tmp_path, _DECORATED_MAIN_OTHER.format(tools=str(TOOLS_DIR)))

    assert proc.returncode == 1, proc.stderr
    assert "Traceback" in proc.stderr
    assert "ValueError" in proc.stderr
    assert "ERROR: cannot " not in proc.stderr


def test_decorator_does_not_claim_a_plain_oserror(tmp_path: Path):
    """Broadening the decorator from ``OutputWriteError`` to ``OSError`` makes
    this red — and the ``ValueError`` case above does NOT catch that widening
    (measured: it stays green under it).

    An unreadable INPUT reaching ``main`` is an ``OSError`` with no output path
    and no flag. ``_die_on_write_error`` would still print a line and exit 2
    for it, i.e. answer "check the value given to …" with nothing to check.
    """
    missing = tmp_path / "absent-input.yaml"
    proc = _run_script(tmp_path, _DECORATED_MAIN_INPUT_OSERROR.format(
        tools=str(TOOLS_DIR), missing=str(missing)))

    assert proc.returncode == 1, proc.stderr
    assert "Traceback" in proc.stderr
    assert "FileNotFoundError" in proc.stderr
    assert "ERROR: cannot " not in proc.stderr
