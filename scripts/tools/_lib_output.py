"""The output entry points: every write to an output path goes through here.

#4223. Split out of ``_lib_io`` so that a tool can reach them without
importing PyYAML or ``_lib_constants``: an isolated pre-commit hook has no
PyYAML, and a generator that rewrites ``_lib_constants.py`` must not load it
(a broken generated block would kill ``--update`` at import time, before it
could repair the block). At import it loads the standard library,
``_lib_exitcodes`` and ``_lib_label`` only; :func:`write_repo_artifact`
also loads ``_lib_atomic_write`` (stdlib + ``_lib_label`` + this module) on
first use: a layout that writes an artifact ships both (the da-tools image
does; the recipe-preview core ships this module only, and never writes one).
``tests/shared/test_read_only_modes_need_no_pyyaml.py`` calls every entry
and every failure exit with PyYAML, ``_lib_constants`` and ``_lib_io``
refused.

``_lib_io`` re-exports every name below, so ``from _lib_io import …`` keeps
working; new code imports from here. A spy or ``monkeypatch`` replaces the
name where the CALLER looks it up: the entries call each other through
THIS module's globals (patch ``_lib_output``), while a caller that looks a
name up on ``_lib_io`` — ``_lib_io.write_onboard_hints`` included — sees
``_lib_io``'s own binding, which patching ``_lib_output`` does not replace.
"""
from __future__ import annotations

import contextlib
import errno
import functools
import io
import os
import stat
import sys
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, TypeVar

import _lib_exitcodes  # noqa: E402
from _lib_exitcodes import die_caller_error
from _lib_label import safe_label

_F = TypeVar("_F", bound=Callable[..., Any])

class OutputWriteError(OSError):
    """An output entry could not write (or create the directory for) *path*.

    #1641. Raised by the two output entries (:func:`write_operator_output`,
    :func:`write_repo_artifact`) and :func:`ensure_dir` in place of the bare
    ``OSError`` family, so that the ONE class of failure — "the output path is unusable" — has one name and
    one message shape across every tool, instead of a traceback at rc=1
    (which in this repo reads as EXIT_VIOLATION, "your config has a
    violation", for what is a mistyped ``-o``).

    It subclasses :class:`OSError` on purpose: every call site that already
    guarded with ``except OSError`` keeps working unchanged, and ``errno`` /
    ``strerror`` / ``filename`` are populated from the original exception.

    Attributes:
        path:   The path that could not be written.
        flag:   The CLI flag the path came from (``"-o/--output"``), or
                ``None`` when the path is derived internally.
        cause:  The original :class:`OSError`.
        action: ``"write"`` (default) or ``"create directory"``.

    ``str(exc)`` is the operator-facing line::

        cannot write <path>: <strerror> (errno <n>) — check the value given to <flag>
        cannot write <path>: <strerror> (errno <n>) — internal output path, this is a bug or an unwritable workspace
    """

    def __init__(
        self,
        path: Any,
        cause: OSError,
        *,
        flag: Optional[str] = None,
        action: str = "write",
    ) -> None:
        self.path = str(path)
        self.flag = flag
        self.cause = cause
        self.action = action
        strerror = getattr(cause, "strerror", None) or str(cause) or type(cause).__name__
        super().__init__(getattr(cause, "errno", None), strerror, self.path)

    def __str__(self) -> str:
        detail = self.strerror or str(self.cause)
        if self.errno is not None:
            detail = f"{detail} (errno {self.errno})"
        if self.flag:
            hint = f"check the value given to {self.flag}"
        else:
            hint = "internal output path, this is a bug or an unwritable workspace"
        return f"cannot {self.action} {self.path}: {detail} — {hint}"


def _die_on_write_error(exc: OutputWriteError, exit_code: Optional[int]) -> None:
    """Print the one-line message to stderr and exit — no traceback, and
    nothing on stdout (#3558)."""
    # #1538: the path is operator argv — still escape control characters
    # before it reaches a terminal (blind review).
    die_caller_error(f"ERROR: {safe_label(str(exc))}", reason="output_unwritable",
                     code=exit_code)


# ---------------------------------------------------------------------------
# #3107 v2: the two output entry points.
#
# OPERATOR OUTPUT — a path the operator named on a flag (``--findings-json``,
# ``--jwks-out``, ``-o``…). The kernel opens it, the way ``>`` and GNU tools
# do: no userspace path walk, no temp file, no rename. ``-`` is stdout.
# REPO ARTIFACT — a committed file a regen tool rewrites (doc maps, index
# tables, state manifests). Atomic replace, the ``_atomic_write`` contract.
# ---------------------------------------------------------------------------

STDOUT_PATH = "-"

#: #3107: every function an output path is written (or its directory made)
#: through, as ``<module>.<function>``, by what it is. Names only — nothing
#: here changes what any of them does. The write gates read it
#: (``tests/shared/test_output_path_write_failure.py``): the population of
#: "tools that name an output flag at a writer", and "which entry did this
#: write come from" under the run-time write audit. A function added here or
#: in ``_lib_yaml`` / ``_lib_atomic_write`` that takes ``flag=`` and is not
#: named below turns that file's ``test_the_entry_point_list_is_complete`` red.
OUTPUT_ENTRY_POINTS: dict[str, tuple[str, ...]] = {
    # A path the operator named on a flag: opened by the kernel, in place.
    "operator": ("_lib_output.open_operator_output", "_lib_output.write_operator_output",
                 "_lib_output.write_operator_output_or_die"),
    # A committed file a tool rewrites: replaced atomically.
    "artifact": ("_lib_output.write_repo_artifact", "_lib_atomic_write.atomic_write_text"),
    # ``mkdir -p`` of an output directory, for either class.
    "directory": ("_lib_output.ensure_dir", "_lib_output.ensure_dir_or_die"),
    # Not writers: they name the flag for a write made by other code — a raw
    # sink inside ``output_write``, the artifact's pre-read check.
    "names_flag_only": ("_lib_output.output_write", "_lib_output.check_repo_artifact"),
    # Call one of the above with the caller's ``flag``.
    "wrapper": ("_lib_io.write_onboard_hints", "_lib_yaml.write_yaml_crd"),
}


def claim_stdout(owner: str) -> None:
    """Record that *owner* (a flag, or ``"report"`` for the tool's own stdout
    report) writes to stdout this run. A second claimant is a caller error:
    two documents on one stream cannot be told apart by a consumer. The
    record is the run's exit state (``_lib_exitcodes.claim_stdout_owner``:
    ``run()`` starts each run without one)."""
    other = _lib_exitcodes.claim_stdout_owner(owner)
    if other is not None:
        raise OutputWriteError(
            STDOUT_PATH, OSError(None, f"standard output is already used by {other}"), flag=owner)


def _is_our_stdout(pre: Optional[os.stat_result]) -> bool:
    """Is *pre* (the kernel's stat of the target) the file, pipe or socket
    our fd 1 already writes to?  Reopening a regular file with O_TRUNC wipes
    what the shell redirect already holds; reopening a pipe interleaves two
    documents. Either way it is treated as ``-``. A character device (tty,
    /dev/null) is not: reopening one is harmless."""
    if pre is None or not (stat.S_ISREG(pre.st_mode) or stat.S_ISFIFO(pre.st_mode)
                           or stat.S_ISSOCK(pre.st_mode)):
        return False
    try:
        out = os.fstat(sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        return False
    return (out.st_dev, out.st_ino) == (pre.st_dev, pre.st_ino)


def targets_stdout(path: str) -> bool:
    """Will an operator output at *path* be written through stdout (``-``,
    or the file / pipe / socket fd 1 already is)?  A tool that also prints a
    receipt to stdout moves it to stderr when this is true."""
    if path == STDOUT_PATH:
        return True
    try:
        return _is_our_stdout(os.stat(path))
    except OSError:
        return False


class _StdoutSink(io.TextIOBase):
    """Text sink over ``sys.stdout`` that never closes it."""

    def writable(self) -> bool:
        return True

    #: #4055: whether a write or flush through this sink failed — the one
    #: case in which what stdout still buffers is dropped.
    failed = False
    _closing = False

    def write(self, s: str) -> int:  # type: ignore[override]
        try:
            return sys.stdout.write(s)
        except OSError:
            self.failed = True
            raise

    def flush(self) -> None:
        if self._closing:
            return
        try:
            sys.stdout.flush()
        except OSError:
            self.failed = True
            raise

    def close(self) -> None:
        """Close the sink, never ``sys.stdout``, and without flushing it
        (#4055): the owner flushed it already, and a finalizer flushing a
        stdout that failed would print an "Exception ignored" traceback."""
        self._closing = True
        super().close()


class _OperatorFile(io.TextIOWrapper):
    """The text handle over an operator output file that remembers whether a
    write or flush through it failed (#4230) — the same bookkeeping as
    :attr:`_StdoutSink.failed`, so that only the handle's own failure is
    reported as the output path's."""

    failed = False

    def write(self, s: str) -> int:  # type: ignore[override]
        try:
            return super().write(s)
        except OSError:
            self.failed = True
            raise

    def flush(self) -> None:
        try:
            super().flush()
        except OSError:
            self.failed = True
            raise


_O_OPERATOR = (os.O_WRONLY | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
               | getattr(os, "O_NOCTTY", 0) | getattr(os, "O_BINARY", 0))


def _kernel_open_operator_output(path: str, mode: int, append: bool,
                                  nofollow: bool = False) -> tuple[int, bool]:
    """``open(2)`` *path* for writing; return ``(fd, created)``.

    O_NONBLOCK only for the open itself: a FIFO with no reader fails with
    ENXIO instead of blocking forever; it is cleared before any write. A
    socket fails the open (ENXIO) on its own. *nofollow*: O_NOFOLLOW — a
    symlink at *path* fails the open (ELOOP) instead of being written through.
    """
    nonblock = getattr(os, "O_NONBLOCK", 0)
    flags = _O_OPERATOR | (os.O_APPEND if append else os.O_TRUNC) | nonblock
    if nofollow:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, mode)
    try:
        # Only where O_NONBLOCK was set: on Windows (3.12+) set_blocking
        # works on pipes only and fails on a regular file's fd.
        if nonblock:
            os.set_blocking(fd, True)
        st = os.fstat(fd)
        try:
            # The name as it is NOW (after the open): a symlink swapped in
            # before the open, or after, is not this euid's file to chmod.
            now = os.lstat(path)
            via_link = (not stat.S_ISREG(now.st_mode)
                        or (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino))
        except OSError:
            via_link = True
        # R2 + V15 (#3107 risk 2, owner 2026-10-24): a regular file this euid
        # owns gets *mode* before any byte is written — created by this call
        # or already there (an operator-made 0644 file would leave a findings
        # file world-readable). Not someone else's file (V2: no chmod, the
        # write succeeds), not a device, and not a file reached through a
        # symlink (its target is the link owner's business). A failed fchmod
        # is the write's failure (rc 2).
        own = (stat.S_ISREG(st.st_mode) and not via_link
               and st.st_uid == getattr(os, "geteuid", lambda: st.st_uid)())
        if own and stat.S_IMODE(st.st_mode) != mode:
            os.fchmod(fd, mode) if hasattr(os, "fchmod") else os.chmod(path, mode)
        return fd, stat.S_ISREG(st.st_mode)
    except BaseException:
        os.close(fd)
        raise


@contextlib.contextmanager
def open_operator_output(path: str, *, flag: Optional[str], mode: int = 0o600,
                         append: bool = False,
                         nofollow: bool = False) -> Iterator[io.TextIOBase]:
    """Open an operator-named output path for UTF-8/LF text writing.

    * ``-`` ⇒ stdout (claimed for *flag*; never closed).
    * the regular file stdout already writes to ⇒ same as ``-``.
    * anything else ⇒ ``open(2)`` by the kernel (``O_CREAT|O_TRUNC``, or
      ``O_APPEND``); *mode* applies to a regular file this euid owns and did
      not reach through a symlink — created now or already there (V15).
      *nofollow*: a symlink at *path* is refused (O_NOFOLLOW), never written
      through (#1560: a file that holds credential values).
    * an ``OSError`` of opening *path*, or of a write or flush through the
      yielded handle ⇒ :class:`OutputWriteError` naming *path* and *flag*.
      Any other ``OSError`` the ``with`` body raises (reading another file,
      say) propagates as it is: it is not the output path's fault (#4230).
      A write into a regular file that raises leaves it empty, not a prefix
      (a process killed mid-write can still leave one).
    """
    # #4230: None until the handle is open — an OSError before that is the
    # open's. After it, only the handle's own failure is the output's.
    handle_failed: Optional[bool] = None
    try:
        try:
            pre = os.stat(path) if path != STDOUT_PATH else None
        except OSError:
            pre = None
        if path == STDOUT_PATH or _is_our_stdout(pre):
            claim_stdout(flag or path)
            sink = _StdoutSink()
            handle_failed = False
            try:
                yield sink
                sink.flush()
            except OSError:
                handle_failed = sink.failed
                # #4055: stdout itself failed and the caller reports it in its
                # own line; drop what stdout still buffers, so the last flush
                # at exit does not fail again and print a second one. Another
                # OSError of the body leaves a working stdout alone.
                if sink.failed:
                    _lib_exitcodes.discard_failed_stdout()
                raise
            finally:
                sink.close()
            return
        fd, regular = _kernel_open_operator_output(path, mode, append, nofollow)
        fh = _OperatorFile(io.BufferedWriter(io.FileIO(fd, "w", closefd=True)),
                           encoding="utf-8", newline="\n")
        handle_failed = False
        try:
            yield fh
            fh.flush()
        except BaseException:
            # Read before the cleanup below: its own flush failing must not
            # turn the body's error into the output's.
            handle_failed = fh.failed
            if regular and not append:
                with contextlib.suppress(OSError, ValueError):
                    fh.flush()
                with contextlib.suppress(OSError, ValueError):
                    os.ftruncate(fh.fileno(), 0)
            raise
        finally:
            with contextlib.suppress(OSError, ValueError):
                fh.close()
    except OutputWriteError:
        raise
    except OSError as exc:
        if handle_failed is False:
            raise  # the body's own OSError, not the output path's (#4230)
        raise OutputWriteError(path, exc, flag=flag) from exc


def write_operator_output(path: str, content: str, *, flag: Optional[str],
                          mode: int = 0o600, append: bool = False,
                          nofollow: bool = False) -> None:
    """Write *content* to an operator-named path; see :func:`open_operator_output`."""
    with open_operator_output(path, flag=flag, mode=mode, append=append,
                              nofollow=nofollow) as fh:
        fh.write(content)


def write_operator_output_or_die(path: str, content: str, *, flag: Optional[str],
                                 mode: int = 0o600, append: bool = False,
                                 exit_code: Optional[int] = None,
                                 nofollow: bool = False) -> None:
    """:func:`write_operator_output`; an unusable path exits (rc 2, one line)."""
    try:
        write_operator_output(path, content, flag=flag, mode=mode, append=append,
                              nofollow=nofollow)
    except OutputWriteError as exc:
        _die_on_write_error(exc, exit_code)


def is_special_file(path: str) -> bool:
    """Whether *path* is there and is not a regular file (``/dev/null``, a
    tty, a FIFO): writing it replaces nothing, so it collides with nothing.
    Whether it is fd 1 is the stdout owners' question, not this one."""
    try:
        return not stat.S_ISREG(os.stat(path).st_mode)
    except OSError:
        return False


def same_file(a: Optional[str], b: Optional[str]) -> bool:
    """Whether paths *a* and *b* name one file, so that writing one replaces
    the other (#3615): the same path once symlinks are resolved
    (``os.path.realpath`` — also for a file not created yet), or the same
    inode (``os.path.samefile`` — a hard link). A path that is not a regular
    file (``is_special_file``) is never the same file. generate-routes'
    output clashes and migrate-credentials' outputs both judge by it."""
    if not a or not b or is_special_file(a) or is_special_file(b):
        return False
    if os.path.realpath(a) == os.path.realpath(b):
        return True
    try:
        return os.path.exists(a) and os.path.exists(b) and os.path.samefile(a, b)
    except OSError:
        return False


def check_repo_artifact(path: Any, *, flag: Optional[str] = None) -> str:
    """The preconditions of :func:`write_repo_artifact`, callable before a
    tool READS the artifact it will rewrite (a FIFO or ``/dev/stdout`` would
    otherwise block the read). Returns ``os.fspath(path)``."""
    p = os.fspath(path)
    if p == STDOUT_PATH:
        raise OutputWriteError(p, OSError(None, "an artifact cannot be standard output"), flag=flag)
    try:
        st = os.stat(p)
    except FileNotFoundError:
        # Absent — or a dangling link the kernel let us follow (it refuses
        # the sticky-directory case with EACCES above): the _atomic_write
        # contract decides, as before.
        st = None
    except OSError as exc:
        raise OutputWriteError(p, exc, flag=flag) from exc
    if st is not None and not stat.S_ISREG(st.st_mode):
        raise OutputWriteError(p, OSError(None, "not a regular file"), flag=flag)
    if st is not None:
        # /dev/stdout, /proc/self/fd/N … resolve (in userspace) to the file
        # the shell redirected; replacing it would discard that stream.
        for fd in (0, 1, 2):
            try:
                o = os.fstat(fd)
            except OSError:
                continue
            if (o.st_dev, o.st_ino) == (st.st_dev, st.st_ino):
                raise OutputWriteError(p, OSError(None, "is this process's standard stream"), flag=flag)
    return p


def _repo_artifact_mode(path: str) -> int:
    """The mode a rewritten artifact gets when the caller names none: what
    git would check it out with. Git tracks one bit of a file's mode, the
    owner's execute bit, so that bit is kept and nothing else is read — not
    the other bits of the file being replaced, not the umask: ``0o755`` for
    an existing file that has ``S_IXUSR``, ``0o644`` for any other and for a
    new one."""
    try:
        executable = bool(os.stat(path).st_mode & stat.S_IXUSR)
    except OSError:
        executable = False
    return 0o755 if executable else 0o644


def write_repo_artifact(path: Any, content: str, *, flag: Optional[str] = None,
                        mode: Optional[int] = None, atomic_only: bool = False) -> None:
    """Atomically rewrite a committed artifact a regen tool owns.

    The ``_atomic_write`` contract, behind two kernel-side preconditions:
    the target (or, when absent, its directory) must ``stat`` through the
    kernel — so ``fs.protected_symlinks`` and directory permissions answer
    before any userspace ``realpath`` runs — and an existing target must be
    a regular file. ``-``, devices, FIFOs and sockets are
    refused with :class:`OutputWriteError`.

    *mode* (#3107): the permission bits the file ends up with. Left out, it
    is :func:`_repo_artifact_mode` — a regen leaves a tracked file the way a
    checkout made it (``0o644``, or ``0o755`` for an executable one), where
    the old names made it ``0o600`` behind ``git status``'s back. A caller
    that passes one (a manifest holding secrets: ``0o600``) gets exactly it.
    """
    p = check_repo_artifact(path, flag=flag)
    if mode is None:
        mode = _repo_artifact_mode(p)
    # Imported here: _lib_atomic_write imports this module (OutputWriteError).
    from _lib_atomic_write import atomic_write_text  # noqa: E402  (implementation)
    atomic_write_text(p, content, mode=mode, flag=flag, atomic_only=atomic_only)


def ensure_dir(path: Any, *, flag: Optional[str] = None, mode: int = 0o777) -> None:
    """``mkdir -p`` *path*, raising :class:`OutputWriteError` on failure.

    *mode* (#3982): the leaf directory's permission bits before the umask,
    as ``os.makedirs(..., mode=)`` — a directory that will hold secrets
    passes ``0o700``. Parents it creates get the default, and an existing
    directory is left as it is.

    #1641. Output-directory tools create the directory themselves before the
    first secure write, and ``os.makedirs(..., exist_ok=True)`` raises the
    same ``OSError`` family (``NotADirectoryError`` when a path component is
    a file, ``PermissionError``, …) OUTSIDE the writer. Routing the mkdir
    through here keeps the failure in the one class, with the same message
    shape (``cannot create directory <path>: …``).
    """
    # #3982 (independent review): `Path("")` is `.`, which always exists —
    # an empty value (`-o "$UNSET"`) would "succeed" and the tool would then
    # write into the working directory. `os.makedirs("")` refuses it
    # (ENOENT), as the tools did before they came through here.
    if os.fspath(path) == "":
        exc = FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), "")
        raise OutputWriteError(path, exc, flag=flag, action="create directory") from exc
    try:
        Path(path).mkdir(mode=mode, parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputWriteError(path, exc, flag=flag, action="create directory") from exc


def ensure_dir_or_die(
    path: Any,
    *,
    flag: Optional[str] = None,
    exit_code: Optional[int] = None,
    mode: int = 0o777,
) -> None:
    """:func:`ensure_dir`, but an unusable path ends the process (rc=2)."""
    try:
        ensure_dir(path, flag=flag, mode=mode)
    except OutputWriteError as exc:
        _die_on_write_error(exc, exit_code)


def _output_write_names_target(exc: OSError, path: Any) -> bool:
    """Does *exc* name *path* (or a parent of it) as the thing that failed?

    The decision procedure behind :func:`output_write`'s "convert or let it
    fly" split, kept as a named function so the tests exercise the SAME code
    the context manager runs (rulebook D-05d).

    * Neither ``filename`` nor ``filename2`` is set ⇒ TRUE. ``Path.mkdir``,
      ``Path.chmod`` and ``os.replace`` populate them, but plenty of failures
      arrive bare (an ``OSError`` re-raised by a helper, ``shutil`` wrapping
      an ``errno`` it built itself), and inside a block that exists only to
      produce the output file, bare is the output.
    * Either one resolves to *path* itself, or to an ANCESTOR of it ⇒ TRUE.
      The ancestor arm is what makes ``os.makedirs("a/b/c")`` convertible:
      the component that could not be created is ``a/b``, never the path the
      caller named.
    * Anything else ⇒ FALSE: the failure is about some OTHER file, and
      calling it "cannot write <output> — check the value given to -o" would
      be a lie that sends the operator to the wrong flag.

    Comparison goes through ``os.fspath`` + ``Path(...).resolve(strict=False)``
    so ``out`` and ``/abs/cwd/out`` are the same path; ``strict=False``
    because the whole point is that the path does not exist yet. A filename
    that is not path-like (a raw file descriptor ``int``, ``bytes``) cannot be
    compared and counts as "names something else" — fail towards letting the
    original exception through with its own type and message intact.

    An EMPTY filename is treated as absent rather than as ``""`` (which would
    resolve to the current directory and match anything under it).

    ⛔ ``resolve`` can raise ``OSError`` too, not only ``TypeError`` /
    ``ValueError``: a RELATIVE path needs the cwd, and a cwd that was deleted
    out from under the process makes ``os.getcwd()`` raise
    ``FileNotFoundError``. Letting that escape would replace the write error
    the operator caused with a chained traceback at rc=1 from the predicate
    that was supposed to classify it — the exact failure this whole ticket is
    about, produced by its own machinery. Both arms therefore catch it and
    fall back to "cannot compare", which lets the ORIGINAL exception through
    untouched.
    """
    named = [n for n in (getattr(exc, "filename", None), getattr(exc, "filename2", None))
             if n is not None and n != ""]
    if not named:
        return True
    try:
        target = Path(os.fspath(path)).resolve(strict=False)
    except (TypeError, ValueError, OSError):
        return False
    for name in named:
        try:
            candidate = Path(os.fspath(name)).resolve(strict=False)
        except (TypeError, ValueError, OSError):
            continue  # fd number / bytes / no cwd: not comparable
        if candidate == target or candidate in target.parents:
            return True
    return False


@contextlib.contextmanager
def output_write(
    path: Any,
    *,
    flag: Optional[str],
    action: str = "write",
) -> Iterator[None]:
    """Make a RAW write to *path* fail the way the output entries do (#1789).

    The output entries already turn "the output path is
    unusable" into one :class:`OutputWriteError`, but a tool that writes with
    ``open()``, ``Path.mkdir()``, ``shutil.copy2()`` or ``csv.writer`` cannot
    use them without changing the bytes it emits. This wraps such a site
    instead of converting it::

        with output_write(out_dir, flag="-o/--output-dir", action="create directory"):
            out_dir.mkdir(parents=True, exist_ok=True)

    so that a mistyped ``-o`` ends in the same one-line ``ERROR: cannot …``
    at rc=2 as every ``_or_die`` call site, instead of a traceback at rc=1
    (which a CI consumer reads as ``EXIT_VIOLATION``, "your config has a
    finding"). The bytes and the permissions of the SUCCESS path are
    untouched: on success this context manager does nothing at all.

    Args:
        path: The output path the block is trying to produce.
        flag: The CLI flag *path* came from (``"-o/--output"``), named in the
              message. Keyword-only and REQUIRED — pass ``None`` explicitly
              for an internally derived path, so that every wrapped site has
              answered the question rather than inherited a default.
        action: The verb in the message: ``"write"`` (default),
                ``"create directory"``, ``"copy into"``, …

    ⚠️ **Pass the path the block acts ON, not the path the tool is ultimately
    producing.** For a ``mkdir`` in front of a file write that means the
    PARENT: wrapping ``out.parent.mkdir(...)`` with ``path=out`` prints
    "cannot create directory /srv/reports/health.json" — a sentence about a
    path nobody was creating, pointing at a file that is not the problem.
    Measured on six sites in this batch before they were split (#1789); the
    behavioural gate now parses the verb out of the line and checks the path
    against it, so the two cannot drift apart again.

    Raises:
        OutputWriteError: for an ``OSError`` raised inside the block that
            :func:`_output_write_names_target` attributes to *path*.
        Anything else: unchanged, including an ``OSError`` about a DIFFERENT
            file and an :class:`OutputWriteError` that a nested secure writer
            already raised (never double-wrapped — the inner one already
            carries the right path, flag and action).

    ⛔ **Why "an OSError about a different file" flies through, and why that
    is not over-caution.** ``ops/assemble_config_dir`` copies each source
    file into the output directory with ``shutil.copy2(src, dst)``; ``src``
    comes from ``--sources``. A source ENTRY that is itself a directory (a
    ``--sources`` tree holding a directory named ``x.yaml`` — measured, not
    hypothetical) raises ``IsADirectoryError`` with ``filename`` pointing at
    the SOURCE. Converting that would print "cannot copy into
    <output>/x.yaml … check the value given to --output" and send the
    operator to edit the one flag that is correct. Same for any input file
    read inside the wrapped block.

    ⚠️ **The design rule for where to put the ``with``.** This converts an
    exception on its way OUT of the block, so a NARROW handler between the
    raise and the ``with`` swallows it first and this never sees it — an
    ``except FileNotFoundError`` inside the block, or a ``try/except OSError:
    continue`` loop around the write, keeps its old behaviour and no rc
    changes. That is a silent miss, not a loud one: wrap the site, then read
    outwards for handlers that already intercept the write. Measured over the
    sites this ticket wraps: the only handlers INSIDE a wrapped block are the
    three in ``ops/state_reconcile.write_json`` — ``mkstemp``'s converts to
    ``OutputWriteError`` by hand, the cleanup ``except BaseException``
    re-raises, and the ``except OSError: pass`` swallows the temp-file
    ``unlink`` only, never the write. And reading out to the enclosing ``def``
    is NOT far enough: ``ops/da_assembler``'s swallowing ``except Exception``
    sits two levels out from the ``with`` — past ``write_rendered``, in its
    caller ``reconcile_one`` — and only an explicit re-raise added there lets
    the conversion reach the decorator.
    """
    try:
        yield
    except OutputWriteError:
        raise
    except OSError as exc:
        if not _output_write_names_target(exc, path):
            raise
        raise OutputWriteError(path, exc, flag=flag, action=action) from exc


def exit_on_output_write_error(fn: _F) -> _F:
    """Decorate a CLI ``main`` so an unusable OUTPUT path exits 2, named.

    The sister of :func:`exit_on_yaml_file_error` for the write direction
    (#1789): :class:`OutputWriteError` — raised by the secure writers, by
    :func:`ensure_dir`, or by a raw site wrapped in :func:`output_write` —
    becomes ``ERROR: cannot <action> <path>: … — check the value given to
    <flag>`` on stderr plus ``sys.exit(EXIT_CALLER_ERROR)``, with no
    traceback.

    Use it on tools whose write sites are spread across several helpers, so
    the class is closed once at the entry point instead of at each site.

    ⚠️ Only :class:`OutputWriteError`, never a bare ``OSError``: an
    ``OSError`` that reached ``main`` from somewhere else (an unreadable
    INPUT, a socket) has no output path to name and no flag to point at, and
    turning it into this message would misattribute it.
    """
    @functools.wraps(fn)
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except OutputWriteError as exc:
            _die_on_write_error(exc, None)
    return _wrapped  # type: ignore[return-value]
