"""The write-failure class contract of the output entries (#1641, #3107).

A mistyped output path must end in ``ERROR: cannot write <path>: … — check
the value given to <flag>`` at rc=2, never a traceback at rc=1 — rc=1 in this
repo is ``EXIT_VIOLATION`` ("your config has a finding"). The entries in
``_lib_io`` raise :class:`OutputWriteError` (an ``OSError`` subclass, so an
``except OSError`` still catches it); their ``_or_die`` forms turn it into
that one line at the caller-error code.

This file pins the helper contract (message shape, errno, ``from`` chaining,
rc, no traceback, the flag forwarded through library wrappers). Whether each
TOOL's output flag reaches that line is behavioural, per flag, with a control
run: ``tests/shared/test_output_path_write_failure.py``.

⚠️ #3107 ④ retired the static half that lived here: an AST tripwire that
every call of the old in-place writers (``write_text_secure`` and friends) sat
inside a catching ``try``, and its library allowlist. Those names are gone,
so its population was empty and it could not fail. It is not retargeted at
the new entries: a lexical scan does not follow a catch in ``main()`` or a
decorator, and reads 40 of their 48 tool call sites as "unguarded"; the
behavioural rows measure the real rc and stderr instead.
"""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

import _lib_io  # noqa: E402  (sys.path via tests/conftest.py)
from _platform_fs import symlink_or_skip  # noqa: E402
import _lib_python  # noqa: E402
import _lib_yaml  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "scripts" / "tools"

# ═══════════════════════════════════════════════════════════════════════════
# Helper contract — OutputWriteError and the _or_die sisters
# ═══════════════════════════════════════════════════════════════════════════
@pytest.fixture
def missing_parent(tmp_path: Path) -> Path:
    """A path whose parent directory does not exist (ENOENT on open)."""
    return tmp_path / "absent" / "out.txt"


@pytest.fixture
def under_a_file(tmp_path: Path) -> Path:
    """A path whose parent is a regular FILE (ENOTDIR on open / mkdir).

    Root-safe: permission bits are ignored by uid 0, but a file in the way
    of a directory component fails for everyone.
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file\n", encoding="utf-8")
    return blocker / "out.txt"


class TestOutputWriteError:
    def test_is_an_oserror_and_keeps_errno_strerror_filename(self, missing_parent):
        with pytest.raises(_lib_io.OutputWriteError) as ei:
            _lib_io.write_operator_output(str(missing_parent), "x", flag="-o/--output")
        exc = ei.value
        assert isinstance(exc, OSError)
        assert exc.errno == 2
        assert exc.strerror
        assert exc.filename == str(missing_parent)
        assert exc.path == str(missing_parent)
        assert exc.flag == "-o/--output"
        assert isinstance(exc.cause, FileNotFoundError)
        assert exc.__cause__ is exc.cause  # raised `from exc`

    def test_message_names_path_errno_and_flag(self, missing_parent):
        with pytest.raises(_lib_io.OutputWriteError) as ei:
            _lib_io.write_operator_output(str(missing_parent), "x", flag="-o/--output")
        msg = str(ei.value)
        assert msg.startswith(f"cannot write {missing_parent}: ")
        assert "(errno 2)" in msg
        assert msg.endswith("— check the value given to -o/--output")
        assert "internal output path" not in msg

    def test_message_without_flag_says_internal_path(self, missing_parent):
        with pytest.raises(_lib_io.OutputWriteError) as ei:
            _lib_io.write_operator_output(str(missing_parent), "x", flag=None)
        msg = str(ei.value)
        assert msg.startswith(f"cannot write {missing_parent}: ")
        assert msg.endswith(
            "— internal output path, this is a bug or an unwritable workspace")
        assert "check the value given to" not in msg

    def test_existing_except_oserror_guards_still_catch_it(self, missing_parent):
        """An ``except OSError`` guard still catches it."""
        caught = None
        try:
            _lib_io.write_operator_output(str(missing_parent), "x", flag=None)
        except OSError as exc:
            caught = exc
        assert isinstance(caught, _lib_io.OutputWriteError)

    def test_parent_is_a_file_is_the_same_class(self, under_a_file):
        with pytest.raises(_lib_io.OutputWriteError) as ei:
            _lib_io.write_operator_output(str(under_a_file), "x", flag="--out")
        assert ei.value.errno is not None
        assert "check the value given to --out" in str(ei.value)

    def test_success_path_unchanged_lf_and_0o600(self, tmp_path):
        target = tmp_path / "ok.txt"
        _lib_io.write_operator_output(str(target), "a\nb\n", flag="-o")
        assert target.read_bytes() == b"a\nb\n"
        if os.name != "nt":
            assert target.stat().st_mode & 0o777 == 0o600


class TestOrDieSisters:
    def test_write_operator_output_or_die_exits_2_with_one_line(self, missing_parent, capsys):
        with pytest.raises(SystemExit) as ei:
            _lib_io.write_operator_output_or_die(str(missing_parent), "x", flag="-o/--output")
        assert ei.value.code == EXIT_CALLER_ERROR == 2
        out, err = capsys.readouterr()
        assert out == ""
        lines = err.strip().splitlines()
        assert len(lines) == 1, err
        assert lines[0].startswith("ERROR: cannot write ")
        assert str(missing_parent) in lines[0]
        assert "-o/--output" in lines[0]
        assert "Traceback" not in err

    def test_a_file_in_the_way_exits_2_too(self, under_a_file, capsys):
        with pytest.raises(SystemExit) as ei:
            _lib_io.write_operator_output_or_die(str(under_a_file), "x", flag="--output")
        assert ei.value.code == 2
        _, err = capsys.readouterr()
        assert err.startswith("ERROR: cannot write ")
        assert "check the value given to --output" in err

    def test_exit_code_is_overridable(self, missing_parent):
        with pytest.raises(SystemExit) as ei:
            _lib_io.write_operator_output_or_die(str(missing_parent), "x", flag=None, exit_code=7)
        assert ei.value.code == 7

    def test_or_die_writes_normally_on_a_good_path(self, tmp_path):
        target = tmp_path / "good.txt"
        _lib_io.write_operator_output_or_die(str(target), "hello", flag="-o")
        assert target.read_text(encoding="utf-8") == "hello"

    def test_ensure_dir_raises_the_same_class_on_a_file_component(self, under_a_file):
        with pytest.raises(_lib_io.OutputWriteError) as ei:
            _lib_io.ensure_dir(under_a_file / "deeper", flag="-o/--output-dir")
        msg = str(ei.value)
        assert msg.startswith(f"cannot create directory {under_a_file / 'deeper'}: ")
        assert msg.endswith("— check the value given to -o/--output-dir")
        assert ei.value.action == "create directory"

    def test_ensure_dir_or_die_exits_2(self, under_a_file, capsys):
        with pytest.raises(SystemExit) as ei:
            _lib_io.ensure_dir_or_die(under_a_file, flag="--out")
        assert ei.value.code == 2
        _, err = capsys.readouterr()
        assert err.startswith("ERROR: cannot create directory ")
        assert "Traceback" not in err

    def test_ensure_dir_refuses_an_empty_path(self, tmp_path, monkeypatch):
        """#3982: `Path("")` is `.` — an empty value (an unset variable) must
        not "create" the working directory and let the tool write there.
        The same refusal `os.makedirs("")` gave: ENOENT, named by the flag."""
        monkeypatch.chdir(tmp_path)
        with pytest.raises(_lib_io.OutputWriteError) as ei:
            _lib_io.ensure_dir("", flag="-o/--output-dir")
        assert ei.value.errno == errno.ENOENT
        assert "cannot create directory : No such file or directory" in str(ei.value)
        assert "check the value given to -o/--output-dir" in str(ei.value)
        assert list(tmp_path.iterdir()) == []

    def test_ensure_dir_creates_parents_and_is_idempotent(self, tmp_path):
        target = tmp_path / "a" / "b" / "c"
        _lib_io.ensure_dir(target)
        _lib_io.ensure_dir_or_die(target)
        assert target.is_dir()

    @pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
    def test_ensure_dir_mode_is_the_leafs_and_leaves_an_existing_one(self, tmp_path):
        """#3982: `mode=` as `os.makedirs` takes it — the leaf only, under
        the umask; a directory already there keeps its own mode."""
        import stat
        leaf = tmp_path / "p" / "secrets"
        old = os.umask(0)
        try:
            _lib_io.ensure_dir(leaf, mode=0o700)
            _lib_io.ensure_dir_or_die(tmp_path / "q", mode=0o700)
        finally:
            os.umask(old)
        assert stat.S_IMODE(leaf.stat().st_mode) == 0o700
        assert stat.S_IMODE((tmp_path / "q").stat().st_mode) == 0o700
        assert stat.S_IMODE(leaf.parent.stat().st_mode) == 0o777
        leaf.chmod(0o750)
        _lib_io.ensure_dir(leaf, mode=0o700)
        assert stat.S_IMODE(leaf.stat().st_mode) == 0o750


class TestLibraryPassthrough:
    """Library wrappers forward ``flag`` so the tool-level handler's message
    still names the operator's flag."""

    def test_write_yaml_crd_writes_through_a_symlink_and_replaces_the_content(self, tmp_path):
        """#3107 ④ (an `nofollow=True` survived the ② mutations): the
        operator entry follows a symlink to its target, and an existing
        file is rewritten whole, not appended to."""
        target = tmp_path / "real.yaml"
        target.write_text("old: content that is longer than the new one\n", encoding="utf-8")
        link = tmp_path / "crd.yaml"
        symlink_or_skip(target, link)
        _lib_yaml.write_yaml_crd(link, {"kind": "X"}, flag="--output-dir")
        assert link.is_symlink()
        assert target.read_text(encoding="utf-8").strip() == "kind: X"

    def test_write_yaml_crd_forwards_flag(self, missing_parent):
        with pytest.raises(_lib_io.OutputWriteError) as ei:
            _lib_yaml.write_yaml_crd(missing_parent, {"a": 1}, flag="--output-dir")
        assert ei.value.flag == "--output-dir"
        assert "check the value given to --output-dir" in str(ei.value)

    def test_write_onboard_hints_forwards_flag(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("file\n", encoding="utf-8")
        with pytest.raises(_lib_io.OutputWriteError) as ei:
            _lib_io.write_onboard_hints(str(blocker), {"tenants": ["alpha"]},
                                        flag="-o/--output-dir")
        assert ei.value.flag == "-o/--output-dir"

    def test_write_onboard_hints_writes_through_the_operator_entry(self, tmp_path, monkeypatch):
        """#3107: the hints file is opened by the operator entry's kernel
        open (not by an old name), holds `json.dump(indent=2,
        ensure_ascii=False)` with no trailing newline, and is 0600."""
        opened = []
        real = _lib_io._kernel_open_operator_output

        def spy(path, *args, **kwargs):
            opened.append(path)
            return real(path, *args, **kwargs)
        monkeypatch.setattr(_lib_io, "_kernel_open_operator_output", spy)
        hints = {"tenants": ["alpha"], "db_types": {"alpha": ["mariadb"]}, "note": "閾值"}
        path = _lib_io.write_onboard_hints(str(tmp_path), hints, flag="-o/--output-dir")
        assert opened == [path]
        assert Path(path).read_bytes() == json.dumps(
            hints, indent=2, ensure_ascii=False).encode("utf-8")
        if os.name == "posix":
            assert os.stat(path).st_mode & 0o777 == 0o600

    def test_write_yaml_crd_writes_through_the_operator_entry(self, tmp_path, monkeypatch):
        opened = []
        real = _lib_io._kernel_open_operator_output

        def spy(path, *args, **kwargs):
            opened.append(path)
            return real(path, *args, **kwargs)
        monkeypatch.setattr(_lib_io, "_kernel_open_operator_output", spy)
        out = tmp_path / "crd.yaml"
        _lib_yaml.write_yaml_crd(out, {"kind": "PrometheusRule", "b": 1, "a": "閾值"},
                                 flag="--output-dir")
        assert opened == [str(out)]
        assert out.read_bytes() == "kind: PrometheusRule\nb: 1\na: 閾值\n".encode("utf-8")
        if os.name == "posix":
            assert os.stat(out).st_mode & 0o777 == 0o600

    def test_facade_re_exports_the_new_names(self):
        for name in ("OutputWriteError", "ensure_dir", "ensure_dir_or_die", "write_operator_output",
                     "write_operator_output_or_die", "open_operator_output"):
            assert getattr(_lib_python, name) is getattr(_lib_io, name), name


def test_helper_module_has_no_import_cycle_with_exitcodes():
    """``_lib_io`` imports ``EXIT_CALLER_ERROR`` from the SSOT directly; this
    only works while ``_lib_exitcodes`` does not import ``_lib_io`` back."""
    src = (TOOLS_DIR / "_lib_exitcodes.py").read_text(encoding="utf-8")
    assert "_lib_io" not in src
    assert _lib_io.EXIT_CALLER_ERROR == 2
