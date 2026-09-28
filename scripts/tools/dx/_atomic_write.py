#!/usr/bin/env python3
"""Atomic write helper shared by the regen tools (Trap #60, #2082, #2128).

Long-running regen tools (``generate_doc_map.py``, ``generate_tool_map.py``,
…) that write their output directly on top of the target can leave a
half-written file if the write is interrupted — ENOSPC, a kill, or on FUSE
workspaces "context-compaction dropped the write cache mid-flush". A reader
must see the old bytes or the new ones, never a prefix.

:func:`atomic_write_text` is the one implementation. It used to be a fixed
``<target>.tmp`` + ``os.replace(tmp, target)``, which was measured (#2128) to
regress four ways against a plain in-place write: a symlink output was
replaced by a regular file (its target kept the OLD bytes), a hard-linked
output was detached from its other names, a user's own ``<target>.tmp`` was
deleted, and an unwritable directory holding a writable file failed with a
traceback naming the tmp. The #2082 writer in ``generate_tenant_metadata``
did not; its logic now lives here and that tool calls this.

THE CONTRACT
------------
* **A symlink is written THROUGH, not replaced.** The atomic replace happens
  on ``os.path.realpath(path)``: the link stays a link and the file it points
  at gets the new bytes (across filesystems too — the tmp is created beside
  the REAL target). ⚠️ This deliberately departs from google/renameio and
  python-atomicwrites, which rename over the path they are given and so turn
  a symlink into a regular file. For a regen output that is somebody's
  symlink into another tree, replacing the link is the surprise.
* **A hard-linked file, or a directory we cannot create a file in, falls
  back to the in-place write with a WARN on stderr** — atomicity is lost for
  that run, and said so, instead of silently detaching the other names or
  failing a write that works in place. Same for xattrs / ACLs / a security
  label a replacement would not carry (:func:`_xattr_mismatch`), an owner we
  cannot restore (someone else's writable file, non-root run), and a target
  that cannot be renamed over (a mount point).
* **The mode is best effort on a file we do not own.** After a successful
  in-place write, a ``chmod`` refused with EPERM (not the file's owner) is a
  WARN, not an error: the bytes landed, and "cannot write" would be false.
* **A read-only file is refused, not replaced.** ``os.access(W_OK)`` false
  ⇒ the in-place write, which the kernel refuses ⇒ an error — even when the
  directory would allow a replace. (The pre-#2128 helper replaced it.)
* **A dangling symlink into a missing directory is an error**: its target's
  directory cannot take a tmp and the write through the link fails. (The
  pre-#2128 helper replaced the link with a regular file.)
* **The tmp is private**: ``mkstemp`` beside the real target, so a file the
  user happens to call ``<target>.tmp`` is never touched.
* **Errors name the TARGET, never the tmp.** Every ``OSError`` leaves as
  ``_lib_io.OutputWriteError`` (or :class:`OutputNoSpaceError` for a full /
  over-quota filesystem) naming *path* — the path the caller passed, not the
  tmp and not the symlink's resolved target. That is what lets
  ``_lib_io.output_write`` / ``exit_on_output_write_error`` turn it into
  rc 2 plus one line instead of a traceback: their attribution rule accepts
  only the path itself or an ancestor of it.
* **The CLI flag in the message is the caller's.** ``flag="--output"`` makes
  the message end "check the value given to --output"; ``flag=None`` (the
  default, for a path the tool derives itself) names no flag.
* **A failed write never falls back.** ENOSPC / EIO from the tmp write or its
  fsync removes the tmp, leaves the target untouched, and raises — writing in
  place at that point would put the truncation risk straight back.
"""
from __future__ import annotations

import errno
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional, Union

# `_lib_io` (OutputWriteError, safe_label) lives one level up in
# scripts/tools/. Callers outside scripts/tools/dx/ put only this directory on
# sys.path, so the helper makes its own dependency importable.
_TOOLS_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from _lib_io import OutputWriteError, safe_label  # noqa: E402

_DEFAULT_MODE = 0o644
_NO_SPACE_ERRNOS = frozenset(
    e for e in (errno.ENOSPC, getattr(errno, "EDQUOT", None)) if e is not None
)
# fsync errnos that mean "this filesystem does not do fsync", not "the bytes
# did not land". ENOSPC / EIO from fsync are NOT here: with delayed
# allocation that is where a full disk shows up, and replacing the good file
# with that tmp would be the exact damage this helper exists to prevent.
_FSYNC_UNSUPPORTED_ERRNOS = frozenset(
    e for e in (errno.EINVAL, getattr(errno, "ENOTSUP", None),
                getattr(errno, "EOPNOTSUPP", None)) if e is not None
)
# mkstemp's prefix embeds the target name; cap it (in UTF-8 BYTES) so a
# target name near NAME_MAX cannot turn a write that works in place into
# ENAMETOOLONG. (If it still happens, the mkstemp fallback catches it.)
_TMP_PREFIX_NAME_CAP = 64
# os.replace errnos meaning "this name cannot be renamed over", where writing
# through it may still work — so the answer is the in-place write, not rc 2:
# EBUSY — the target is a mount point (docker `-v file:file`, k8s subPath);
# EXDEV — the same, seen through some bind setups; EPERM / EACCES — rename
# refused by policy rather than by file mode (an LSM such as SELinux /
# AppArmor, an append-only or immutable attribute). ⚠️ Not the sticky-/tmp
# case: measured, that never reaches the rename (a non-root run falls back
# at the fchown first, root has CAP_FOWNER).
_REPLACE_REFUSED_ERRNOS = frozenset({errno.EBUSY, errno.EXDEV, errno.EPERM, errno.EACCES})
# listxattr / getxattr errnos that mean "this filesystem has no xattrs": that
# side reads as {}. Any OTHER failure is "cannot compare" ⇒ in place.
_XATTR_UNSUPPORTED_ERRNOS = frozenset(
    e for e in (getattr(errno, "ENOTSUP", None), getattr(errno, "EOPNOTSUPP", None))
    if e is not None
)
# Kernel-maintained xattrs left out of the old-vs-new comparison. IMA's
# ``security.ima`` is a hash of the CONTENT and EVM's ``security.evm`` an HMAC
# over inode / uid / gid / mode: they differ on ANY new file, so comparing them
# would send every write in place on an IMA/EVM host and cost the atomic
# replace its protection — yet they are not "lost": the kernel computes them
# afresh for the replacement. Exactly these two; every other security.* is
# compared.
_KERNEL_MAINTAINED_XATTRS = frozenset({"security.ima", "security.evm"})


class OutputNoSpaceError(OutputWriteError):
    """``OutputWriteError`` for a full / over-quota filesystem (#2082).

    The shared message ends in "check the value given to <flag>", which for
    ENOSPC sends the operator to the one thing that is right. Same class
    family (so ``exit_on_output_write_error`` still turns it into rc 2 with no
    traceback), different hint — and it says whether the previous file
    survived, because on the atomic path it did and on the in-place
    fallbacks it may not have.
    """

    def __init__(self, path: Any, cause: OSError, *, flag: Optional[str],
                 previous_kept: bool) -> None:
        super().__init__(path, cause, flag=flag)
        self.previous_kept = previous_kept

    def __str__(self) -> str:
        detail = self.strerror or str(self.cause)
        if self.errno is not None:
            detail = f"{detail} (errno {self.errno})"
        state = ("the previous file is unchanged" if self.previous_kept
                 else "the file may now be incomplete")
        return (f"cannot {self.action} {self.path}: {detail} — the filesystem "
                f"holding it is full or over quota; free space and re-run "
                f"({state})")


class _Opts:
    """What one call asked for, handed to the internals as one value."""

    __slots__ = ("encoding", "newline", "mode", "flag")

    def __init__(self, encoding: str, newline: Optional[str], mode: int,
                 flag: Optional[str]) -> None:
        self.encoding = encoding
        self.newline = newline
        self.mode = mode
        self.flag = flag


def _output_error(out: Path, exc: OSError, opts: _Opts, *,
                  previous_kept: bool) -> OutputWriteError:
    """The #1789 exception for a failure while producing *out*, named by *out*.

    Named by the path the caller passed, never by the private tmp file or
    the symlink's resolved target: those are paths the operator did not type,
    and ``_lib_io.output_write`` would not attribute them to *out*.
    """
    if getattr(exc, "errno", None) in _NO_SPACE_ERRNOS:
        return OutputNoSpaceError(out, exc, flag=opts.flag, previous_kept=previous_kept)
    return OutputWriteError(out, exc, flag=opts.flag)


def _warn_not_atomic(out: Path, why: str) -> None:
    print(
        f"WARN: {safe_label(str(out))}: {why}; writing it in place instead "
        f"of atomically — an interrupted run can leave it truncated",
        file=sys.stderr,
    )


def _write_in_place(out: Path, content: str, opts: _Opts, *, set_mode: bool) -> None:
    """The pre-#2082 writer: truncate, write, chmod. Only for the fallbacks.

    #2128 S1: a ``chmod`` refused with EPERM AFTER the write succeeded — the
    file belongs to someone else (root-owned 0666, a non-root run) — is a
    WARN, not an error. The content is on disk; reporting "cannot write" at
    rc 2 would be false. Silent when the mode is already the one asked for.
    Any other chmod failure still raises.
    """
    try:
        # `newline` is atomic_write_text's own parameter, default "\n" —
        # pinned by test_line_ending_policy::test_atomic_write_text_defaults_to_lf.
        out.write_text(content, encoding=opts.encoding, newline=opts.newline)  # line-ending: ignore
    except OSError as exc:
        raise _output_error(out, exc, opts, previous_kept=False) from exc
    if not set_mode:
        return
    try:
        os.chmod(out, opts.mode)
    except OSError as exc:
        if exc.errno != errno.EPERM:
            raise _output_error(out, exc, opts, previous_kept=False) from exc
        try:
            current = stat.S_IMODE(os.stat(out).st_mode)
        except OSError:
            current = None
        if current != opts.mode:
            shown = "unknown" if current is None else oct(current)
            print(
                f"WARN: {safe_label(str(out))}: written, but its mode cannot be "
                f"set to {oct(opts.mode)} (not its owner); it stays {shown}",
                file=sys.stderr,
            )


def _xattrs(ref: Union[str, int]) -> dict:
    """Every extended attribute of *ref* (a path or an fd), as ``{name: value}``.

    A filesystem without xattr support reads as ``{}``; a name that vanished
    between list and get (ENODATA) is skipped, and so is
    ``_KERNEL_MAINTAINED_XATTRS``. Any other ``OSError`` is raised.
    """
    try:
        names = os.listxattr(ref)
    except OSError as exc:
        if exc.errno in _XATTR_UNSUPPORTED_ERRNOS:
            return {}
        raise
    found: dict = {}
    for name in names:
        if name in _KERNEL_MAINTAINED_XATTRS:
            continue
        try:
            found[name] = os.getxattr(ref, name)
        except OSError as exc:
            if exc.errno in _XATTR_UNSUPPORTED_ERRNOS:
                return {}
            if exc.errno == getattr(errno, "ENODATA", None):
                continue
            raise
    return found


def _xattr_mismatch(target: Path, fd: int) -> Optional[str]:
    """The WARN text when the tmp *fd* would not reproduce *target*'s xattrs.

    A replace makes a NEW inode, and POSIX ACLs (``system.posix_acl_*``),
    the SELinux label (``security.selinux``) and ``user.*`` are all xattrs:
    measured, a ``user.*`` xattr is gone after a replace and kept by the
    in-place write. So the tmp's xattrs — whatever the directory gave it
    (default ACL, SELinux default context) — are compared with the target's,
    and only a difference sends the write in place. On an SELinux host both
    usually get the same ``security.selinux`` ⇒ atomic stays. IMA / EVM
    values are not compared (``_KERNEL_MAINTAINED_XATTRS``).
    Not copied: setting ``security.*`` / ``trusted.*`` needs privileges.

    ⚠️ Only what the RUNNING user can list is compared. Without
    CAP_SYS_ADMIN, ``listxattr`` omits ``trusted.*`` on BOTH sides, so they
    compare equal and a replace silently drops a root-set ``trusted.*``
    (measured as uid 65534).

    ⚠️ Linux only: without ``os.listxattr`` (macOS, the BSDs, Windows)
    nothing is compared and a replace does not keep xattrs (``com.apple.*``
    and the like) — the pre-comparison behaviour of #2082.
    """
    if not hasattr(os, "listxattr"):
        return None
    try:
        old, new = _xattrs(str(target)), _xattrs(fd)
    except OSError as exc:
        return (f"its extended attributes cannot be compared (errno {exc.errno}) "
                f"and a replacement might not keep them")
    if old == new:
        return None
    differ = sorted(n for n in old.keys() | new.keys() if old.get(n) != new.get(n))
    return ("it carries extended attributes (ACLs / security labels) that a "
            f"replacement would not keep: {', '.join(differ)}")


def atomic_write_text(
    path: Union[str, "os.PathLike[str]"],
    content: str,
    *,
    encoding: str = "utf-8",
    newline: Optional[str] = "\n",
    mode: int = _DEFAULT_MODE,
    flag: Optional[str] = None,
) -> None:
    """Write *content* to *path* so an interrupted run keeps the old file.

    See the module docstring for the contract (symlinks written through,
    hard links / unwritable directory → in place + WARN, private tmp, errors
    named by *path*).

    Args:
        path: the output file. Its parent directory must already exist.
        content: text to write.
        encoding: file encoding (default utf-8).
        newline: line-ending policy; ``"\\n"`` (default) forces LF so a
            Windows regen emits the same bytes as CI. ``None`` opts back into
            Python's platform default.
        mode: permission bits the file ends up with — set with ``fchmod`` on
            the tmp before any byte is written (a path-based chmod could
            follow a symlink swapped in for the tmp name), so the replaced
            target is never visible with a umask-default mode. Default 0644.
        flag: the CLI flag *path* came from (``"--output"``), named in the
            error message; ``None`` for a path the tool derives itself.

    **Eligible for atomic** = no file yet, or a regular file with ONE link
    that we may write (``os.access(path, W_OK)``). Everything else goes in
    place, as a plain write would:

    * a non-regular file (``/proc/self/fd/1`` → pipe / tty, a FIFO, a
      directory) — nothing to replace; no chmod on what we did not create;
      a directory still ends in an error;
    * a hard-linked file — a replace would detach this name from the other
      links without a word; WARN and write in place so every name updates;
    * a read-only file — it is refused with EACCES as before, instead of
      being replaced through a writable directory.

    Raises:
        OutputWriteError: any ``OSError`` while producing *path*, named by
            *path* — :class:`OutputNoSpaceError` for ENOSPC / EDQUOT. Wrap
            the call in ``_lib_io.output_write(path, flag=...)`` and decorate
            ``main`` with ``exit_on_output_write_error`` to end at rc 2.
    """
    out = Path(path)
    opts = _Opts(encoding, newline, mode, flag)
    try:
        st = os.stat(out)  # follows symlinks, /proc magic links included
    except FileNotFoundError:
        st = None
    except OSError as exc:
        raise _output_error(out, exc, opts, previous_kept=True) from exc

    regular = st is None or stat.S_ISREG(st.st_mode)
    eligible = st is None or (
        # `<= 1`, not `== 1`: st_nlink is 0 when the file was unlinked (by a
        # concurrent writer's replace) between our stat and now — there are
        # no other names to keep, and an in-place truncate is the one thing
        # to avoid (#2128 S2).
        regular and st.st_nlink <= 1 and os.access(out, os.W_OK)
    )
    if eligible:
        done, why = _try_atomic(out, content, st, opts)
        if done:
            return
        # No file yet ⇒ nothing to truncate, and the in-place write reports
        # the real failure itself: a WARN would only be noise ahead of it.
        if why is not None and st is not None:
            _warn_not_atomic(out, why)
    elif regular and st.st_nlink > 1:
        _warn_not_atomic(
            out,
            f"it has {st.st_nlink} hard links and replacing it would detach "
            f"this name from the others",
        )
    _write_in_place(out, content, opts, set_mode=regular)


def _try_atomic(out: Path, content: str, st: Optional[os.stat_result],
                opts: _Opts) -> "tuple[bool, Optional[str]]":
    """Replace *out* atomically: ``(True, None)``, or ``(False, why)`` to fall back.

    ``why`` is the WARN text, or ``None`` when no WARN is due. On a
    ``False`` return nothing has changed on disk — the tmp is gone. It gives
    up (``False``) exactly where the in-place write would still have
    produced the old result:

    * ``mkstemp`` fails for ANY reason (EACCES, EROFS, ENOSPC for inodes,
      ENAMETOOLONG, …): the directory cannot take a new file but the
      existing one may still be writable — the in-place write either
      succeeds as it always did or reports the real error;
    * the tmp cannot be handed back to the file's OWNER (``fchown``, any
      ``OSError`` — EPERM for a non-root run, EINVAL for an unmapped id in a
      rootless / userns container). WARN: since #2128 the in-place write
      then succeeds (its chmod EPERM is only a WARN, see
      :func:`_write_in_place`), so atomicity is really lost and is said so.
      Only the GROUP not being settable while the owner matches is not a
      reason: the owner can still rewrite the file, so the replace goes
      ahead (the group becomes ours);
    * the tmp's extended attributes differ from the target's
      (:func:`_xattr_mismatch`) or cannot be compared. Compared AFTER
      ``fchown`` / ``fchmod``: a ``chmod`` of a file whose access ACL was
      inherited from a default ACL rewrites the mask entry inside
      ``system.posix_acl_access``, and the in-place writer chmods too, so
      this is the state a replace would really leave;
    * ``os.replace`` refused with one of ``_REPLACE_REFUSED_ERRNOS``.

    A real write failure (write / fsync: ENOSPC, EIO, …) is NOT a fallback:
    the tmp is removed, the target is untouched, and it is raised as
    ``OutputWriteError`` naming *out*.

    ⚠️ **The one deliberate exception to "same result as in place"**: with
    the data blocks full, the in-place write could truncate the old file and
    reuse its blocks, and succeed; here the tmp write fails with ENOSPC and
    the run ends in an error with the old file intact. That truncation is
    exactly what #2082 exists to prevent, so it is kept (owner ruling on
    #2082).
    """
    target = Path(os.path.realpath(out))
    # Capped in BYTES, not characters: 64 four-byte characters would push
    # the tmp name past NAME_MAX (255 bytes) for a target name that fits.
    name_head = os.fsencode(target.name)[:_TMP_PREFIX_NAME_CAP].decode(
        "utf-8", "ignore")
    fd, tmp_name, replaced = -1, None, False
    try:
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=target.parent, prefix=f".{name_head}.", suffix=".tmp",
            )
        except OSError as exc:
            # ANY failure to create the tmp: the in-place write may still
            # work (an unwritable or read-only directory holding a writable
            # file — readOnlyRootFilesystem + a subPath / `-v file:file`
            # mount), and where it cannot, it reports the real error itself.
            return False, f"cannot create a temporary file beside it ({exc.strerror})"
        if st is not None and hasattr(os, "fchown") and (
                st.st_uid != os.geteuid() or st.st_gid != os.getegid()):
            try:
                os.fchown(fd, st.st_uid, st.st_gid)
            except OSError as exc:
                if st.st_uid != os.geteuid():
                    return False, (f"it belongs to uid {st.st_uid} and a replacement "
                                   f"could not keep that owner ({exc.strerror})")
        if hasattr(os, "fchmod"):
            try:
                os.fchmod(fd, opts.mode)
            except OSError as exc:
                # EPERM on our own tmp (an NFS root_squash export: the tmp is
                # nobody's, root may not chmod it) is the case the in-place
                # path already handles as a WARN; anything else stays an error.
                if exc.errno != errno.EPERM:
                    raise
                return False, f"cannot set the temporary file's mode ({exc.strerror})"
        else:  # Windows before 3.13: only the read-only bit exists anyway
            os.chmod(tmp_name, opts.mode)
        if st is not None:
            why = _xattr_mismatch(target, fd)
            if why is not None:
                return False, why
        # `newline` is atomic_write_text's own parameter (default "\n").
        fh = os.fdopen(fd, "w", encoding=opts.encoding, newline=opts.newline)  # line-ending: ignore
        fd = -1  # the file object owns it now
        with fh:
            fh.write(content)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError as exc:
                if exc.errno not in _FSYNC_UNSUPPORTED_ERRNOS:
                    raise
        try:
            os.replace(tmp_name, target)
        except OSError as exc:
            if exc.errno not in _REPLACE_REFUSED_ERRNOS:
                raise
            return False, f"it cannot be replaced ({exc.strerror})"
        replaced = True
        return True, None
    except OSError as exc:
        if isinstance(exc, OutputWriteError):
            raise
        raise _output_error(out, exc, opts, previous_kept=True) from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp_name is not None and not replaced:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
