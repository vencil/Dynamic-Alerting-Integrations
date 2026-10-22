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


# ── running a tool where a chmod really denies (#3206) ──────────────────

import contextlib  # noqa: E402
import sys  # noqa: E402


@contextlib.contextmanager
def unprivileged_sandbox(repo):
    """Yield ``(box, run)`` for a test that needs a mode bit to really deny.

    Under uid 0 the mode bits do not stop a stat or a read, so ``run(argv)``
    executes *argv* as ``nobody`` (``setpriv --reuid=65534``); as any other
    uid it runs *argv* as is. *box* is a fresh directory under ``/var/tmp``
    (mode 0755 — ``tmp_path`` and this checkout may sit in a 0700 directory
    ``nobody`` cannot traverse) holding a copy of ``scripts/``,
    ``docs/schemas/`` and the session's ``$DA_GUARD_BINARY`` (as
    ``box/da-guard``, which ``run`` exports); the test builds its conf.d in
    *box* too. Skips when it cannot — not POSIX, no da-guard, root without
    ``setpriv``, an unprivileged uid that cannot import the tools'
    dependencies — and FAILS instead when ``VIBE_REQUIRE_UNPRIVILEGED_FS`` is
    set (CI), as the helm-gated tests do with ``VIBE_REQUIRE_HELM``: there a
    skip would leave the permission cases unmeasured behind a green run.
    Everything the test made unreadable is made readable again before the
    box is removed."""
    def _skip_or_fail(why):
        if os.environ.get("VIBE_REQUIRE_UNPRIVILEGED_FS", ""):
            pytest.fail(f"VIBE_REQUIRE_UNPRIVILEGED_FS is set, but {why}: the "
                        "permission cases would go unmeasured")
        pytest.skip(why)

    if os.name == "nt" or not has_posix_modes():
        _skip_or_fail("POSIX mode bits do not apply here")
    da_guard = os.environ.get("DA_GUARD_BINARY", "")
    if not da_guard or not os.path.isfile(da_guard):
        _skip_or_fail("no session da-guard to run the tool with")
    prefix = []
    if os.geteuid() == 0:
        setpriv = shutil.which("setpriv")
        if setpriv is None:
            _skip_or_fail("running as root and `setpriv` is not installed")
        prefix = [setpriv, "--reuid=65534", "--regid=65534", "--clear-groups"]
    base = "/var/tmp" if os.path.isdir("/var/tmp") else None
    box = tempfile.mkdtemp(prefix="vibe-unpriv-", dir=base)
    try:
        os.chmod(box, 0o755)
        shutil.copytree(os.path.join(repo, "scripts"), os.path.join(box, "scripts"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(os.path.join(repo, "docs", "schemas"),
                        os.path.join(box, "docs", "schemas"))
        shutil.copy2(da_guard, os.path.join(box, "da-guard"))
        for dirpath, dirnames, filenames in os.walk(box):
            for n in dirnames:
                os.chmod(os.path.join(dirpath, n), 0o755)
            for n in filenames:
                p = os.path.join(dirpath, n)
                os.chmod(p, 0o755 if n == "da-guard" else 0o644)
        # No PYTHONPATH, and no coverage start hook: the unprivileged uid
        # cannot write the session's coverage data, and coverage would print
        # its own traceback into the output the tests read.
        env = {k: v for k, v in os.environ.items()
               if k != "PYTHONPATH" and not k.startswith("COVERAGE_")}
        env.update(DA_GUARD_BINARY=os.path.join(box, "da-guard"), HOME=box,
                   PYTHONDONTWRITEBYTECODE="1")

        def run(argv, **kw):
            return subprocess.run([*prefix, *argv], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", env=env,
                                  cwd=box, timeout=300, check=False, **kw)

        probe = run([sys.executable, "-c", "import yaml"])
        if probe.returncode != 0:
            _skip_or_fail("the unprivileged uid cannot import the tools' dependencies: "
                          + (probe.stderr.strip().splitlines() or ["?"])[-1])
        yield box, run
    finally:
        for dirpath, dirnames, _f in os.walk(box):
            for n in dirnames:
                with contextlib.suppress(OSError):
                    os.chmod(os.path.join(dirpath, n), 0o755)
        shutil.rmtree(box, ignore_errors=True)
