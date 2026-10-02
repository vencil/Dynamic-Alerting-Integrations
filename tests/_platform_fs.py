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
import shutil
import subprocess
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


def symlink_or_else(src, dst, fallback) -> None:
    """``os.symlink(src, dst)`` where this host can make symlinks; call
    ``fallback()`` where it cannot.

    For a fixture that is NOT allowed to skip and has a faithful stand-in for
    the link (the regular file git itself writes under ``core.symlinks=false``,
    say). The choice is the capability probe, not ``os.name``: on POSIX the
    probe is pinned True, so a symlink refused there still raises."""
    if can_symlink():
        os.symlink(src, dst)
    else:
        fallback()


@functools.cache
def can_name_file(name) -> bool:
    """Whether this filesystem stores a file under exactly *name* (str or
    bytes; a single path component).

    Windows refuses control characters, ``"`` and most ``:`` in a name, and
    cannot spell a non-UTF-8 byte name at all. The listing is read back
    because a refusal is not the only failure: NTFS takes ``a:b`` as stream
    ``b`` of file ``a`` and reports success."""
    with tempfile.TemporaryDirectory() as d:
        base = os.fsencode(d) if isinstance(name, bytes) else d
        try:
            with open(os.path.join(base, name), "wb"):
                pass
            return name in os.listdir(base)
        except (OSError, ValueError):   # UnicodeDecodeError is a ValueError
            return False


def require_file_name(name) -> None:
    """Skip the calling test when this filesystem cannot hold *name*."""
    if not can_name_file(name):
        pytest.skip(f"this filesystem cannot hold a file named {name!r}")


@functools.cache
def has_posix_modes() -> bool:
    """Whether ``chmod`` permission bits are stored and enforced as POSIX
    modes. On Windows only the read-only bit exists: ``chmod(0o600)`` reads
    back as ``0o666``."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "probe")
        with open(path, "wb"):
            pass
        os.chmod(path, 0o600)
        return (os.stat(path).st_mode & 0o777) == 0o600


def require_posix_modes() -> None:
    """Skip the calling test when permission bits are not POSIX modes here."""
    if not has_posix_modes():
        pytest.skip("this filesystem does not keep POSIX mode bits "
                    "(chmod 0o600 reads back differently)")


@functools.cache
def has_case_sensitive_names() -> bool:
    """Whether ``a.yaml`` and ``A.YAML`` are two files here (NTFS and the
    default macOS volume say no: the second write lands in the first)."""
    with tempfile.TemporaryDirectory() as d:
        for name in ("probe.yaml", "PROBE.YAML"):
            with open(os.path.join(d, name), "wb"):
                pass
        return len(os.listdir(d)) == 2


def require_case_sensitive_names() -> None:
    """Skip the calling test when names differing only in case collide here."""
    if not has_case_sensitive_names():
        pytest.skip("this filesystem is case-insensitive: two names differing "
                    "only in case are one file")


def require_os_attrs(*names: str) -> None:
    """Skip the calling test when ``os`` lacks any of *names* on this
    platform (``geteuid``, ``listxattr``, ...)."""
    missing = [n for n in names if not hasattr(os, n)]
    if missing:
        pytest.skip("os." + ", os.".join(missing) + " does not exist on this platform")


@functools.cache
def can_exec_shebang_scripts() -> bool:
    """Whether a file starting ``#!`` can be run as a command here. Windows'
    CreateProcess does not read the shebang, so a script standing in for a
    binary on PATH (a fake ``kubectl``) cannot be started."""
    with tempfile.TemporaryDirectory() as d:
        script = os.path.join(d, "probe")
        with open(script, "wb") as fh:
            fh.write(b"#!/bin/sh\nexit 0\n")
        os.chmod(script, 0o755)
        try:
            return subprocess.run([script], timeout=30).returncode == 0
        except OSError:
            return False


def require_shebang_scripts() -> None:
    """Skip the calling test when a ``#!`` script cannot be run as a command."""
    if not can_exec_shebang_scripts():
        pytest.skip("this host cannot run a `#!` script as a command "
                    "(a POSIX shebang stand-in for a binary on PATH)")


def require_tool(name: str) -> None:
    """Skip the calling test when executable *name* is not on PATH."""
    if shutil.which(name) is None:
        pytest.skip(f"`{name}` is not on PATH")


#: The exception type opening a directory for writing raises on this OS.
DIR_WRITE_ERROR = PermissionError if os.name == "nt" else IsADirectoryError

#: ``strerror`` text a tool echoes for that failure on this OS.
DIR_WRITE_STRERROR = os.strerror(errno.EACCES if os.name == "nt" else errno.EISDIR)
