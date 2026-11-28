"""#3981: the runtime entries of `_lib_output` — a private directory a
service makes for its own running, and the files in it.

Directory (`make_runtime_dir`): never a symlink; one already there must be
this euid's with ``mode & 0o077 == 0``; a new one is exactly 0700 whatever
the umask. File (`open_runtime_file` / `write_runtime_file`): what is at the
name is unlinked (never followed), a directory there is refused, and the
file is made ``O_EXCL|O_NOFOLLOW`` 0600. Every refusal is `OutputWriteError`
with the reason as its ``cause``.
"""
from __future__ import annotations

import errno
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import _lib_output  # noqa: E402  (tests/conftest.py puts scripts/tools on sys.path)
from _lib_output import (  # noqa: E402
    OutputWriteError, make_runtime_dir, open_runtime_file, write_runtime_file,
)
from _platform_fs import symlink_or_skip  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes and owners")

TOOLS = Path(__file__).resolve().parents[2] / "scripts" / "tools"


def _mode(p) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


def _refusal(call) -> OutputWriteError:
    with pytest.raises(OutputWriteError) as info:
        call()
    return info.value


# ── the directory ──────────────────────────────────────────────────────────

def test_a_new_directory_is_0700_whatever_the_umask(tmp_path):
    """0277 takes the owner's write bit from what `mkdir` makes: the entry
    sets it back through the directory's own descriptor."""
    old = os.umask(0o277)
    try:
        make_runtime_dir(tmp_path / "d")
    finally:
        os.umask(old)
    assert _mode(tmp_path / "d") == 0o700


def test_parents_are_made_with_the_default_mode_and_only_on_request(tmp_path):
    leaf = tmp_path / "a" / "b" / "d"
    err = _refusal(lambda: make_runtime_dir(leaf))
    assert err.errno == errno.ENOENT and err.action == "create directory"
    old = os.umask(0o022)
    try:
        make_runtime_dir(leaf, parents=True)
    finally:
        os.umask(old)
    assert _mode(leaf) == 0o700
    assert _mode(tmp_path / "a") == 0o755 and _mode(tmp_path / "a" / "b") == 0o755


def test_a_private_directory_already_there_is_accepted_and_left_alone(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    d.chmod(0o500)  # private, if not writable: its mode is not the entry's to change
    try:
        make_runtime_dir(d)
        assert _mode(d) == 0o500
    finally:
        d.chmod(0o700)


def test_exist_ok_false_refuses_one_already_there(tmp_path):
    d = tmp_path / "d"
    d.mkdir(mode=0o700)
    err = _refusal(lambda: make_runtime_dir(d, exist_ok=False))
    assert isinstance(err.cause, FileExistsError) and err.path == str(d)


@pytest.mark.parametrize("mode", [0o701, 0o710, 0o750, 0o755, 0o777, 0o1777])
def test_a_directory_open_to_group_or_others_is_refused(tmp_path, mode):
    d = tmp_path / "d"
    d.mkdir()
    d.chmod(mode)
    err = _refusal(lambda: make_runtime_dir(d))
    assert isinstance(err.cause, PermissionError), err
    assert f"mode {stat.S_IMODE(mode):04o} gives group or others access" in str(err)
    assert _mode(d) == stat.S_IMODE(mode), "a refused directory's mode is not changed"


def test_a_directory_of_another_uid_is_refused(tmp_path):
    """Root makes one with chown; a non-root run of the same refusal is
    `test_the_owner_rule_holds_for_a_user_that_is_not_root`."""
    if os.geteuid() != 0:
        pytest.skip("chown to another uid needs root (the non-root run is below)")
    d = tmp_path / "d"
    d.mkdir(mode=0o700)
    os.chown(d, 65534, 65534)
    err = _refusal(lambda: make_runtime_dir(d))
    assert isinstance(err.cause, PermissionError)
    assert "owned by uid 65534, not by this process (euid 0)" in str(err)


_AS_USER = r"""
import os, sys
sys.path.insert(0, sys.argv[1])
from _lib_output import OutputWriteError, make_runtime_dir
for path in sys.argv[2:]:
    try:
        make_runtime_dir(path)
        print("ok", path)
    except OutputWriteError as exc:
        print("refused", path, exc.cause)
"""


def test_the_owner_rule_holds_for_a_user_that_is_not_root():
    """Measured as uid 65534 (setpriv): a 0700 directory root owns is
    refused naming both uids; one 65534 owns, and a new one it makes, are
    accepted. Root's own run cannot stand for this: root passes every
    access check, and the owner check is the only thing left to refuse."""
    setpriv = shutil.which("setpriv")
    if os.geteuid() != 0 or setpriv is None:
        pytest.skip("needs root and setpriv to run as uid 65534")
    base = Path(tempfile.mkdtemp(prefix="rtdir", dir="/var/tmp"))
    try:
        base.chmod(0o755)
        roots = base / "roots"
        roots.mkdir()
        roots.chmod(0o700)
        mine = base / "mine"
        mine.mkdir()
        mine.chmod(0o700)
        os.chown(mine, 65534, 65534)
        base_w = base / "w"
        base_w.mkdir()
        os.chown(base_w, 65534, 65534)
        made = base_w / "made"
        out = subprocess.run(
            [setpriv, "--reuid=65534", "--regid=65534", "--clear-groups",
             sys.executable, "-I", "-c", _AS_USER, str(TOOLS), str(roots), str(mine), str(made)],
            capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert out.returncode == 0, out.stderr
        lines = out.stdout.splitlines()
        assert lines[0] == (f"refused {roots} [Errno 1] owned by uid 0, not by this process "
                            f"(euid 65534): '{roots}'"), lines
        assert lines[1:] == [f"ok {mine}", f"ok {made}"], lines
        assert _mode(made) == 0o700 and os.lstat(made).st_uid == 65534
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_a_symlink_is_refused_whatever_it_points_at(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    for name, target in (("to_dir", private), ("dangling", tmp_path / "nowhere")):
        link = tmp_path / name
        symlink_or_skip(target, link)
        err = _refusal(lambda: make_runtime_dir(link))
        assert isinstance(err.cause, NotADirectoryError), (name, err)
        assert "a symbolic link, not a directory" in str(err)
        assert link.is_symlink()


def test_a_file_in_its_place_is_refused(tmp_path):
    f = tmp_path / "f"
    f.write_text("keep", encoding="utf-8")
    err = _refusal(lambda: make_runtime_dir(f))
    assert isinstance(err.cause, FileExistsError) and f.read_text(encoding="utf-8") == "keep"


# ── the file ───────────────────────────────────────────────────────────────

def test_a_new_file_is_0600_with_the_bytes_as_given(tmp_path):
    p = tmp_path / "f"
    old = os.umask(0)
    try:
        write_runtime_file(p, b"a\r\nb\0")
    finally:
        os.umask(old)
    assert p.read_bytes() == b"a\r\nb\0" and _mode(p) == 0o600


def test_a_file_already_there_is_replaced_by_a_new_one(tmp_path):
    """Unlinked and made anew: a hard link to the old one keeps its bytes,
    and the new file has the entry's mode, not the old one's."""
    p = tmp_path / "f"
    p.write_bytes(b"old")
    p.chmod(0o644)
    os.link(p, tmp_path / "other-name")
    write_runtime_file(p, b"new")
    assert p.read_bytes() == b"new" and _mode(p) == 0o600
    assert (tmp_path / "other-name").read_bytes() == b"old"


def test_a_symlink_at_the_name_is_removed_never_followed(tmp_path):
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    link = tmp_path / "f"
    symlink_or_skip(victim, link)
    write_runtime_file(link, b"new")
    assert not link.is_symlink() and link.read_bytes() == b"new"
    assert victim.read_bytes() == b"keep"


def test_a_directory_at_the_name_is_refused_and_kept(tmp_path):
    d = tmp_path / "f"
    (d / "inside").mkdir(parents=True)
    err = _refusal(lambda: write_runtime_file(d, b"x"))
    assert err.errno == errno.EISDIR and isinstance(err.cause, IsADirectoryError)
    assert (d / "inside").is_dir()


def test_the_descriptor_is_the_callers_and_not_inherited(tmp_path):
    fd = open_runtime_file(tmp_path / "f")
    try:
        assert not os.get_inheritable(fd)
        assert os.fstat(fd).st_mode & 0o777 == 0o600
        os.write(fd, b"x")
    finally:
        os.close(fd)
    assert (tmp_path / "f").read_bytes() == b"x"


def test_a_write_that_fails_names_the_file(tmp_path, monkeypatch):
    """The write's own OSError (a full disk) is the file's: `OutputWriteError`
    naming it, with its errno; the descriptor is closed by the handle."""
    closed = []

    class _Full:
        def __init__(self, fd):
            self.fd = fd

        def write(self, _b):
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            os.close(self.fd)
            closed.append(self.fd)

    monkeypatch.setattr(_lib_output.os, "fdopen", lambda fd, mode: _Full(fd))
    p = tmp_path / "f"
    err = _refusal(lambda: write_runtime_file(p, b"x"))
    assert err.errno == errno.ENOSPC and err.path == str(p) and closed


def test_a_handle_that_cannot_be_made_closes_the_descriptor(tmp_path, monkeypatch):
    seen = []

    def _no_handle(fd, mode):
        seen.append(fd)
        raise OSError(errno.ENOMEM, os.strerror(errno.ENOMEM))

    monkeypatch.setattr(_lib_output.os, "fdopen", _no_handle)
    err = _refusal(lambda: write_runtime_file(tmp_path / "f", b"x"))
    assert err.errno == errno.ENOMEM
    with pytest.raises(OSError) as closed:
        os.fstat(seen[0])
    assert closed.value.errno == errno.EBADF
