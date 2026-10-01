"""What the host's filesystem can do, and what it raises — per OS, by probing.

CAPABILITY PROBES (#2559)
-------------------------
A Windows host without Developer Mode cannot create symlinks
(``WinError 1314``). A test that needs one is not measuring anything there,
so it SKIPS with the reason — it must not go red, and it must not be decided
by ``os.name``: the probe tries the operation once, so the same tests start
running the day the host gains the capability. Same shape as CPython's
``os_helper.can_symlink``, pytest's ``symlink_or_skip``, Go's
``testenv.MustHaveSymlink`` and Git's ``SYMLINKS`` lazy prereq.

⛔ A probe that answers False on Linux CI would turn every test using it into
a silent skip under a green run. ``tests/shared/test_platform_fs.py`` pins
the probes to True on POSIX for that reason.

EXCEPTION TYPES (#2557)
-----------------------
POSIX reports writing to (or ``shutil.copy2`` from) a directory as EISDIR,
i.e. ``IsADirectoryError``. Windows reports the same call as EACCES, i.e.
``PermissionError``. A test that pins the POSIX type goes red on a Windows
host although the tool behaved correctly (rc 2, one line naming the path).

Each constant is ONE value per platform, not a union: widening the Linux
side to ``(IsADirectoryError, PermissionError)`` would let a real permission
failure pass as the directory case there, and Linux CI is where these tests
carry weight.
"""
import errno
import functools
import os
import tempfile

import pytest

SYMLINK_SKIP_REASON = (
    "this host cannot create symlinks (on Windows: needs Developer Mode or "
    "SeCreateSymbolicLinkPrivilege)")


@functools.cache
def can_symlink() -> bool:
    """Whether this host can create a symlink — tried once, then cached."""
    with tempfile.TemporaryDirectory() as d:
        try:
            os.symlink(os.path.join(d, "target"), os.path.join(d, "link"))
        except (OSError, NotImplementedError, AttributeError):
            return False
    return True


def require_symlinks() -> None:
    """Skip the calling test when this host cannot create symlinks.

    For a test whose symlink is created somewhere a skip cannot be raised
    from (inside a patched callee the tool invokes, say)."""
    if not can_symlink():
        pytest.skip(SYMLINK_SKIP_REASON)


def symlink_or_skip(src, dst, **kwargs) -> None:
    """``os.symlink(src, dst)``, or skip when this host cannot make symlinks.

    Argument order is ``os.symlink``'s: ``link.symlink_to(target)`` becomes
    ``symlink_or_skip(target, link)``. Only the missing CAPABILITY skips; any
    other failure of the call itself (a name that already exists, a missing
    parent) still raises, on every host."""
    require_symlinks()
    os.symlink(src, dst, **kwargs)


#: The exception type opening a directory for writing raises on this OS.
DIR_WRITE_ERROR = PermissionError if os.name == "nt" else IsADirectoryError

#: ``strerror`` text a tool echoes for that failure on this OS.
DIR_WRITE_STRERROR = os.strerror(errno.EACCES if os.name == "nt" else errno.EISDIR)
