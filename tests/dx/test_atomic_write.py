"""Tests for scripts/tools/dx/_atomic_write.py (Trap #60, #2082, #2128).

The helper used to be a fixed ``<target>.tmp`` + ``os.replace``. Measured on
main (#2128), that regressed four ways against a plain in-place write, and
each has a test here that is red under the old helper:

* ``TestSymlink`` — the link was replaced by a regular file and the file it
  pointed at kept the OLD bytes;
* ``TestHardLink`` — the other name kept the OLD bytes, nlink fell to 1;
* ``TestUsersDotTmp`` — a user's own ``<target>.tmp`` was deleted;
* ``TestUnwritableDirectory`` — rc 1 with a ``PermissionError`` naming the
  tmp, target untouched (run for real as uid 65534: root bypasses DAC).

Plus the #2082 properties that came with the move (ENOSPC keeps the previous
file, xattrs, owner/mode) and the error contract the callers rely on: every
failure is an ``OutputWriteError`` naming the TARGET with the caller's flag.
"""
from __future__ import annotations

import errno
import importlib.util
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "tools" / "dx" / "_atomic_write.py"

import _lib_io  # noqa: E402  (tests/conftest.py puts scripts/tools on sys.path)

OLD = "OLD\n"
NEW = "NEW\n"
NOBODY = 65534


def _load_module():
    spec = importlib.util.spec_from_file_location("_atomic_write_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


@pytest.fixture
def target(tmp_path: Path) -> Path:
    t = tmp_path / "out.md"
    t.write_text(OLD, encoding="utf-8")
    return t


def _entries(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir())


# ---------------------------------------------------------------------------
# Control + the basics that did not change
# ---------------------------------------------------------------------------
def test_must_fire_a_plain_file_is_replaced(mod, target, capsys):
    ino = os.stat(target).st_ino
    mod.atomic_write_text(target, NEW)
    assert target.read_text(encoding="utf-8") == NEW
    assert os.stat(target).st_ino != ino, "a plain file must get the atomic replace"
    assert _entries(target.parent) == ["out.md"], "no tmp may be left behind"
    assert capsys.readouterr().err == ""


def test_writes_new_file(mod, tmp_path):
    t = tmp_path / "new.md"
    mod.atomic_write_text(t, "hello\nworld\n")
    assert t.read_text(encoding="utf-8") == "hello\nworld\n"


def test_honors_explicit_lf_newline(mod, tmp_path):
    t = tmp_path / "lf.md"
    mod.atomic_write_text(t, "a\nb\nc\n", newline="\n")
    assert t.read_bytes() == b"a\nb\nc\n"


def test_honors_platform_default_newline(mod, tmp_path):
    t = tmp_path / "platform.md"
    mod.atomic_write_text(t, "a\nb\n", newline=None)
    assert t.read_bytes() == (b"a\r\nb\r\n" if os.name == "nt" else b"a\nb\n")


def test_encoding_param_is_respected(mod, tmp_path):
    t = tmp_path / "latin.md"
    mod.atomic_write_text(t, "café\n", encoding="latin-1")
    assert t.read_bytes() == b"caf\xe9\n"


def test_replace_is_used_not_copy(mod, target):
    """Behaviour lock: one ``os.replace`` from a tmp onto the (real) target."""
    with mock.patch.object(mod.os, "replace", wraps=os.replace) as spy:
        mod.atomic_write_text(target, NEW)
    assert spy.call_count == 1
    src, dst = spy.call_args.args
    assert str(src).endswith(".tmp") and Path(src).parent == Path(os.path.realpath(target)).parent
    assert Path(dst) == Path(os.path.realpath(target))


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
def test_the_mode_is_set_on_the_fd_before_the_replace(mod, target):
    """``fchmod`` on the open tmp, before ``os.replace``: the target is never
    visible with a umask-default mode, and no path-based chmod can follow a
    symlink swapped in for the tmp name."""
    order: list[str] = []
    real_fchmod, real_replace = os.fchmod, os.replace

    def fchmod(fd, m):
        order.append(f"fchmod({oct(m)})")
        return real_fchmod(fd, m)

    def replace(src, dst):
        order.append("replace")
        return real_replace(src, dst)

    with mock.patch.object(mod.os, "fchmod", fchmod), \
            mock.patch.object(mod.os, "replace", replace), \
            mock.patch.object(mod.os, "chmod", side_effect=AssertionError("chmod by name")):
        mod.atomic_write_text(target, NEW)
    assert order == ["fchmod(0o644)", "replace"], order


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
@pytest.mark.parametrize("mode", [0o644, 0o600])
def test_the_mode_is_applied(mod, tmp_path, mode):
    t = tmp_path / "m.md"
    t.write_text(OLD, encoding="utf-8")
    os.chmod(t, 0o640)
    mod.atomic_write_text(t, NEW, mode=mode)
    assert stat.S_IMODE(os.stat(t).st_mode) == mode


# ---------------------------------------------------------------------------
# The four #2128 regressions
# ---------------------------------------------------------------------------
class TestSymlink:
    def test_a_symlink_stays_a_symlink_and_its_target_gets_the_bytes(self, mod, tmp_path):
        real = tmp_path / "real" / "data.md"
        real.parent.mkdir()
        real.write_text(OLD, encoding="utf-8")
        link = tmp_path / "link.md"
        link.symlink_to(real)
        mod.atomic_write_text(link, NEW)
        assert link.is_symlink(), "the symlink was replaced by a regular file"
        assert os.readlink(link) == str(real)
        assert real.read_text(encoding="utf-8") == NEW
        # the tmp was created beside the REAL target, and is gone
        assert _entries(real.parent) == ["data.md"]
        assert _entries(tmp_path) == ["link.md", "real"]

    def test_a_dangling_symlink_creates_its_target(self, mod, tmp_path):
        real = tmp_path / "real.md"
        link = tmp_path / "link.md"
        link.symlink_to(real)
        mod.atomic_write_text(link, NEW)
        assert link.is_symlink() and real.read_text(encoding="utf-8") == NEW


class TestHardLink:
    def test_every_name_is_updated_in_place_with_a_warn(self, mod, target, capsys):
        other = target.with_name("other.md")
        os.link(target, other)
        ino = os.stat(target).st_ino
        mod.atomic_write_text(target, NEW)
        assert other.read_text(encoding="utf-8") == NEW
        assert os.stat(target).st_ino == ino and os.stat(target).st_nlink == 2
        err = capsys.readouterr().err
        assert "WARN:" in err and "2 hard links" in err and "in place" in err, err

    def test_the_in_place_fallback_keeps_the_callers_encoding(self, mod, target):
        os.link(target, target.with_name("other.md"))
        mod.atomic_write_text(target, "café\n", encoding="latin-1")
        assert target.read_bytes() == b"caf\xe9\n"


class TestUsersDotTmp:
    def test_a_users_own_dot_tmp_is_not_touched(self, mod, target):
        users = target.with_name(target.name + ".tmp")
        users.write_text("MINE\n", encoding="utf-8")
        mod.atomic_write_text(target, NEW)
        assert target.read_text(encoding="utf-8") == NEW
        assert users.read_text(encoding="utf-8") == "MINE\n"
        assert _entries(target.parent) == ["out.md", "out.md.tmp"]


def _run_as_nobody(fn) -> dict:
    """Run *fn* in a forked child with uid/gid 65534; return what it returns
    (a JSON-able dict), or ``{"raised": repr}``."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover — child
        os.close(r)
        try:
            os.setgroups([])
            os.setresgid(NOBODY, NOBODY, NOBODY)
            os.setresuid(NOBODY, NOBODY, NOBODY)
            result = fn()
        except BaseException as exc:  # noqa: BLE001
            result = {"raised": f"{type(exc).__name__}: {exc}"}
        os.write(w, json.dumps(result).encode())
        os.close(w)
        os._exit(0)
    os.close(w)
    data = b""
    while chunk := os.read(r, 65536):
        data += chunk
    os.close(r)
    os.waitpid(pid, 0)
    return json.loads(data)


class TestUnwritableDirectory:
    def test_a_tmp_that_cannot_be_created_falls_back_in_place(self, mod, target, capsys):
        def refuse(*a, **k):
            raise PermissionError(errno.EACCES, "Permission denied", str(k.get("dir")))

        with mock.patch.object(tempfile, "mkstemp", refuse):
            mod.atomic_write_text(target, NEW)
        assert target.read_text(encoding="utf-8") == NEW
        err = capsys.readouterr().err
        assert "WARN:" in err and "cannot create a temporary file beside it" in err, err

    @pytest.mark.skipif(os.name != "posix" or not hasattr(os, "setresuid"),
                        reason="needs POSIX uids")
    def test_an_unwritable_directory_for_real(self, mod, tmp_path):
        """The measured #2128 case: a writable file in a directory the
        running user cannot create files in. Root bypasses DAC, so a root run
        forks and drops to uid 65534 for the write; a non-root run uses a
        0555 directory of its own."""
        if os.geteuid() == 0:
            base = Path(tempfile.mkdtemp(prefix="aw2128-"))
            try:
                os.chmod(base, 0o755)              # root-owned: nobody cannot create here
                out = base / "out.txt"
                out.write_text(OLD, encoding="utf-8")
                os.chown(out, NOBODY, NOBODY)      # …but may write the file itself

                def child():
                    import contextlib
                    import io
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        mod.atomic_write_text(out, NEW)
                    return {"err": err.getvalue(), "uid": os.geteuid()}

                res = _run_as_nobody(child)
                assert "raised" not in res, res
                assert res["uid"] == NOBODY
                content, entries = out.read_text(encoding="utf-8"), _entries(base)
            finally:
                shutil.rmtree(base, ignore_errors=True)
        else:
            d = tmp_path / "ro"
            d.mkdir()
            out = d / "out.txt"
            out.write_text(OLD, encoding="utf-8")
            os.chmod(d, 0o555)
            try:
                import contextlib
                import io
                buf = io.StringIO()
                with contextlib.redirect_stderr(buf):
                    mod.atomic_write_text(out, NEW)
                res = {"err": buf.getvalue()}
                content, entries = out.read_text(encoding="utf-8"), _entries(d)
            finally:
                os.chmod(d, 0o755)
        assert content == NEW
        assert entries == ["out.txt"]
        assert "WARN:" in res["err"] and "writing it in place" in res["err"], res


# ---------------------------------------------------------------------------
# Failures: never a fallback, always named by the target
# ---------------------------------------------------------------------------
class _HalfThenENOSPC:
    """A handle whose ``write`` lands half the text, then fails ENOSPC."""

    fired = False

    def __init__(self, fh):
        self._fh = fh

    def write(self, s):
        self._fh.write(s[: len(s) // 2])
        self._fh.flush()
        type(self).fired = True
        raise OSError(errno.ENOSPC, "No space left on device (simulated)")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()
        return False

    def __getattr__(self, name):
        return getattr(self._fh, name)


def test_enospc_mid_write_keeps_the_previous_file(mod, target, monkeypatch):
    """The Trap #60 / #2082 fault, injected where the helper now writes: the
    tmp's ``os.fdopen`` handle (it was ``open(<target>.tmp)`` before)."""
    real_fdopen = os.fdopen
    _HalfThenENOSPC.fired = False

    def fdopen(fd, mode="r", *a, **k):
        fh = real_fdopen(fd, mode, *a, **k)
        return _HalfThenENOSPC(fh) if "w" in mode else fh

    monkeypatch.setattr(os, "fdopen", fdopen)
    with pytest.raises(mod.OutputNoSpaceError) as info:
        mod.atomic_write_text(target, NEW * 100, flag="--output")
    print(f"fired={_HalfThenENOSPC.fired}")
    assert _HalfThenENOSPC.fired, "the injected ENOSPC never ran"
    exc = info.value
    assert isinstance(exc, _lib_io.OutputWriteError)
    assert exc.path == str(target) and exc.previous_kept is True
    assert "full or over quota" in str(exc) and "previous file is unchanged" in str(exc)
    assert target.read_text(encoding="utf-8") == OLD
    assert _entries(target.parent) == ["out.md"], "the half-written tmp was left behind"


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EIO])
def test_a_failing_fsync_does_not_replace(mod, target, monkeypatch, code):
    def fsync(fd):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(_lib_io.OutputWriteError):
        mod.atomic_write_text(target, NEW)
    assert target.read_text(encoding="utf-8") == OLD
    assert _entries(target.parent) == ["out.md"]


def _failing_replace(monkeypatch):
    real = os.replace

    def replace(src, dst, *a, **k):
        if str(src).endswith(".tmp"):
            raise OSError(errno.EIO, "Input/output error (simulated)", src, dst)
        return real(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", replace)


class TestErrorNamesTheTarget:
    def test_the_error_names_the_target_not_the_tmp(self, mod, target, monkeypatch):
        """``_lib_io.output_write`` converts only an error naming the path it
        wraps (or an ancestor); a tmp name would fly through as a traceback."""
        _failing_replace(monkeypatch)
        with pytest.raises(_lib_io.OutputWriteError) as info:
            mod.atomic_write_text(target, NEW)
        exc = info.value
        assert exc.path == str(target)
        assert exc.filename == str(target)
        assert ".tmp" not in str(exc)
        assert _lib_io._output_write_names_target(exc, target)
        assert target.read_text(encoding="utf-8") == OLD
        assert _entries(target.parent) == ["out.md"]

    def test_a_symlinks_error_names_the_link_not_its_resolved_target(self, mod, tmp_path, monkeypatch):
        real = tmp_path / "real.md"
        real.write_text(OLD, encoding="utf-8")
        link = tmp_path / "link.md"
        link.symlink_to(real)
        _failing_replace(monkeypatch)
        with pytest.raises(_lib_io.OutputWriteError) as info:
            mod.atomic_write_text(link, NEW)
        assert info.value.path == str(link)

    def test_flag_none_names_no_flag(self, mod, target, monkeypatch):
        _failing_replace(monkeypatch)
        with pytest.raises(_lib_io.OutputWriteError) as info:
            mod.atomic_write_text(target, NEW)
        msg = str(info.value)
        assert info.value.flag is None
        assert "check the value given to" not in msg
        assert msg.startswith(f"cannot write {target}: ")

    def test_a_flag_is_named_in_the_hint(self, mod, target, monkeypatch):
        _failing_replace(monkeypatch)
        with pytest.raises(_lib_io.OutputWriteError) as info:
            mod.atomic_write_text(target, NEW, flag="--target")
        assert str(info.value).endswith("check the value given to --target")
        assert "--output" not in str(info.value)

    def test_output_write_turns_it_into_rc2_one_line(self, mod, target, monkeypatch, capsys):
        """End to end through the two wrappers every caller uses."""
        _failing_replace(monkeypatch)

        @_lib_io.exit_on_output_write_error
        def main():
            with _lib_io.output_write(target, flag="--out"):
                mod.atomic_write_text(target, NEW, flag="--out")

        with pytest.raises(SystemExit) as info:
            main()
        assert info.value.code == 2
        err = capsys.readouterr().err
        assert err.startswith(f"ERROR: cannot write {target}: ")
        assert err.rstrip().endswith("check the value given to --out")
        assert ".tmp" not in err and "Traceback" not in err

    def test_a_directory_as_target_is_an_error_named_by_it(self, mod, tmp_path):
        d = tmp_path / "adir"
        d.mkdir()
        with pytest.raises(_lib_io.OutputWriteError) as info:
            mod.atomic_write_text(d, NEW)
        assert info.value.path == str(d)


# ---------------------------------------------------------------------------
# Blind-review round (#2128 S1 / S2 / N2 / N3)
# ---------------------------------------------------------------------------
def _hard_link_forces_in_place(target: Path) -> None:
    os.link(target, target.with_name("other.md"))


def _chmod_refused_for(monkeypatch, target: Path, code: int) -> list:
    real = os.chmod
    fired: list = []

    def chmod(path, mode, *a, **k):
        if os.fspath(path) == str(target):
            fired.append(mode)
            raise PermissionError(code, os.strerror(code), str(target))
        return real(path, mode, *a, **k)

    monkeypatch.setattr(os, "chmod", chmod)
    return fired


class TestChmodAfterAnInPlaceWrite:
    """S1(a): someone else's writable file (root-owned 0666, a non-root run).
    The in-place write succeeds; its chmod is refused EPERM. That must not
    turn a write that happened into "cannot write" at rc 2."""

    def test_eperm_is_a_warn_and_the_content_stays(self, mod, target, monkeypatch, capsys):
        _hard_link_forces_in_place(target)
        os.chmod(target, 0o666)
        fired = _chmod_refused_for(monkeypatch, target, errno.EPERM)
        mod.atomic_write_text(target, NEW)       # must not raise
        assert fired == [0o644]
        assert target.read_text(encoding="utf-8") == NEW
        err = capsys.readouterr().err
        assert "its mode cannot be set to 0o644 (not its owner); it stays 0o666" in err, err

    def test_eperm_with_the_mode_already_right_is_silent_about_the_mode(
            self, mod, target, monkeypatch, capsys):
        _hard_link_forces_in_place(target)
        os.chmod(target, 0o644)
        _chmod_refused_for(monkeypatch, target, errno.EPERM)
        mod.atomic_write_text(target, NEW)
        assert "mode cannot be set" not in capsys.readouterr().err

    def test_another_chmod_error_is_still_an_error(self, mod, target, monkeypatch):
        _hard_link_forces_in_place(target)
        _chmod_refused_for(monkeypatch, target, errno.EROFS)
        with pytest.raises(_lib_io.OutputWriteError) as info:
            mod.atomic_write_text(target, NEW)
        assert info.value.path == str(target)


def test_an_owner_that_cannot_be_restored_goes_in_place_with_a_warn(mod, target, monkeypatch,
                                                                     capsys):
    """N2: the fchown fallback WARNs, as the module docstring says."""
    st = os.stat(target)
    monkeypatch.setattr(os, "geteuid", lambda: st.st_uid + 1)

    def fchown(fd, uid, gid):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "fchown", fchown)
    ino = st.st_ino
    mod.atomic_write_text(target, NEW)
    assert os.stat(target).st_ino == ino, "went atomic although the owner cannot be kept"
    assert target.read_text(encoding="utf-8") == NEW
    err = capsys.readouterr().err
    assert f"it belongs to uid {st.st_uid}" in err and "writing it in place" in err, err
    assert _entries(target.parent) == ["out.md"]


def test_nlink_zero_takes_the_atomic_path(mod, target, monkeypatch):
    """S2: st_nlink == 0 (the file was unlinked by a concurrent replace after
    our stat) has no other names to keep — it must not fall into the
    in-place truncate."""
    real_stat = os.stat
    seen: list = []

    def fake_stat(path, *a, **k):
        st = real_stat(path, *a, **k)
        if os.fspath(path) == str(target) and not a and not k:
            seen.append(True)
            fields = list(st[:10])
            fields[stat.ST_NLINK] = 0
            return os.stat_result(fields)
        return st

    real_write_text = Path.write_text

    def write_text(self, *a, **k):
        if str(self) == str(target):
            raise AssertionError("in-place write for st_nlink == 0")
        return real_write_text(self, *a, **k)

    monkeypatch.setattr(os, "stat", fake_stat)
    monkeypatch.setattr(Path, "write_text", write_text)
    ino = real_stat(target).st_ino
    mod.atomic_write_text(target, NEW)
    monkeypatch.undo()
    assert seen, "the nlink=0 stat was never served"
    assert os.stat(target).st_ino != ino
    assert target.read_text(encoding="utf-8") == NEW


def test_a_dangling_symlink_into_a_missing_directory_is_an_error(mod, tmp_path):
    """N3: the documented behaviour change — the pre-#2128 helper replaced
    such a link with a regular file; now the link stays and it is an error
    named by the link."""
    link = tmp_path / "link.md"
    link.symlink_to(tmp_path / "no-such-dir" / "data.md")
    with pytest.raises(_lib_io.OutputWriteError) as info:
        mod.atomic_write_text(link, NEW)
    assert info.value.path == str(link)
    assert link.is_symlink()
    assert _entries(tmp_path) == ["link.md"]


# ---------------------------------------------------------------------------
# Extended attributes (#2082, carried over)
# ---------------------------------------------------------------------------
def _user_xattrs_supported(p: Path) -> bool:
    if not hasattr(os, "setxattr"):
        return False
    try:
        os.setxattr(p, "user.probe", b"1")
        os.removexattr(p, "user.probe")
        return True
    except OSError:
        return False


def test_an_xattr_the_replacement_would_drop_is_kept_in_place(mod, target, capsys):
    if not _user_xattrs_supported(target):
        pytest.skip("user.* xattrs not supported on this filesystem")
    os.setxattr(target, "user.owner-note", b"keep me")
    ino = os.stat(target).st_ino
    mod.atomic_write_text(target, NEW)
    assert target.read_text(encoding="utf-8") == NEW
    assert os.getxattr(target, "user.owner-note") == b"keep me"
    assert os.stat(target).st_ino == ino
    err = capsys.readouterr().err
    assert "WARN:" in err and "user.owner-note" in err, err


def test_ima_and_evm_are_not_compared(mod, target, monkeypatch):
    """Kernel-maintained values differ on every new inode; comparing them
    would send every write in place on an IMA/EVM host."""
    real_list, real_get = os.listxattr, os.getxattr

    def listxattr(ref, *a, **k):
        return ["security.ima", "security.evm", *real_list(ref, *a, **k)]

    def getxattr(ref, name, *a, **k):
        if name in ("security.ima", "security.evm"):
            return os.urandom(8)
        return real_get(ref, name, *a, **k)

    monkeypatch.setattr(os, "listxattr", listxattr, raising=False)
    monkeypatch.setattr(os, "getxattr", getxattr, raising=False)
    ino = os.stat(target).st_ino
    mod.atomic_write_text(target, NEW)
    assert os.stat(target).st_ino != ino, "went in place over IMA/EVM values"


# ---------------------------------------------------------------------------
# Integration: regen tools opt-in behavior
# ---------------------------------------------------------------------------
def test_regen_tools_expose_safe_flag():
    """Regression guard: --safe flag is wired into generate_doc_map.py / generate_tool_map.py."""
    for name in ("generate_doc_map.py", "generate_tool_map.py"):
        src = (_REPO_ROOT / "scripts" / "tools" / "dx" / name).read_text(encoding="utf-8")
        assert "--safe" in src
        assert "atomic_write_text" in src


def test_generate_tenant_metadata_uses_the_shared_helper():
    """#2128: one implementation. The #2082 writer that lived in
    generate_tenant_metadata must not come back as a second copy."""
    src = (_REPO_ROOT / "scripts" / "tools" / "dx" / "generate_tenant_metadata.py").read_text(
        encoding="utf-8")
    assert "from _atomic_write import atomic_write_text" in src
    for gone in ("def atomic_replace_output", "def _try_atomic", "def _xattr_mismatch",
                 "tempfile.mkstemp"):
        assert gone not in src, gone


def test_the_module_is_importable_from_outside_scripts_tools():
    """``scripts/dx`` / ``scripts/ops`` callers put only scripts/tools/dx on
    sys.path; the helper must make its own ``_lib_io`` import work."""
    import subprocess
    code = (
        "import sys; sys.path[:] = [p for p in sys.path if 'scripts' not in p]; "
        f"sys.path.insert(0, {str(_SCRIPT.parent)!r}); "
        "import _atomic_write, _lib_io; "
        "assert issubclass(_atomic_write.OutputNoSpaceError, _lib_io.OutputWriteError); "
        "print('ok')"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=str(_REPO_ROOT / "docs"), timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr
