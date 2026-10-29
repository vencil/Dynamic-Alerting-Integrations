"""The one judgement of a path the caller gave (#3276).

Standard library only — no PyYAML, and `_lib_exitcodes` (whose import
installs the crash hook) is imported only when a refusal is raised — so
every tool can use it, including those that deliberately run without
PyYAML (pr-preflight, coverage-delta) or without the crash hook
(analyze-probe).
"""
from __future__ import annotations

import errno
import os
import stat
from typing import Any, NamedTuple, Optional

from _lib_label import safe_label


def _caller_error(message: str, reason: str) -> BaseException:
    """`_lib_exitcodes.CallerError`, imported only for a refusal."""
    from _lib_exitcodes import CallerError  # noqa: PLC0415  (see the module docstring)
    return CallerError(message, reason)


class PathProblem(NamedTuple):
    """Why a caller-given path cannot be used (`classify_path`)."""

    reason: str
    """`--json` reason: ``{dir,file,path}_not_{found,accessible}``."""
    message: str
    """The stderr line, without ``ERROR:``."""
    missing: bool
    """True only when nothing is at the path (ENOENT, ENOTDIR, ELOOP)."""


#: #3276: what a stat errno means for a caller-given path. Only "nothing is
#: there" is not-found; every other errno (EACCES under a parent the caller
#: cannot enter, EIO, …) is not-accessible — never folded into "not found".
_STAT_MISSING = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})


def classify_path(path: Any, kind: str, *, flag: Optional[str] = None) -> Optional[PathProblem]:
    """None when *path* (given by the caller, via *flag*) is usable as a
    *kind* — ``"dir"``, ``"file"`` or ``"any"`` — else the `PathProblem`.

    #3276. The one judgement of a caller-given path, replacing each tool's
    ``Path.is_dir()`` / ``exists()`` / ``is_file()`` and ``os.path.*``:
    which errnos those swallow differs across Python versions (3.13 raises
    EACCES, 3.14 answers False) and ``os.path.*`` already answers False, so
    a path under a parent the caller cannot enter was a traceback or a
    "not found". Here ``os.stat`` decides by errno.

    * ``"dir"``: a directory. Only stat: whether its content can be listed
      is the reader's verdict (for a conf.d, da-guard's ``walk_error``).
    * ``"file"``: a regular file that opens for reading — what
      ``Path.is_file()`` answered, so a reader that still asks ``is_file()``
      never sees a FIFO or a device it would take for "no file" (fail-open:
      ``--policy /dev/null`` evaluating no policy). The open is made only on
      a regular file: on a FIFO or a device it would have side effects.
    * ``"any"``: anything that stats.
    """
    # '' is SUPPLIED (an unset shell variable) and must stay visible.
    shown = safe_label(str(path)) if str(path) else "''"
    label = f"{flag} {shown}" if flag else shown
    noun = {"dir": "dir", "file": "file", "any": "path"}[kind]
    try:
        st = os.stat(path)
    except OSError as exc:
        missing = exc.errno in _STAT_MISSING
        return PathProblem(f"{noun}_not_{'found' if missing else 'accessible'}",
                           f"{label}: {exc.strerror}", missing)
    if kind == "dir" and not stat.S_ISDIR(st.st_mode):
        return PathProblem("dir_not_found", f"{label}: not a directory", False)
    if kind == "file":
        if not stat.S_ISREG(st.st_mode):
            return PathProblem("file_not_found", f"{label}: not a regular file", False)
        try:
            os.close(os.open(path, os.O_RDONLY))
        except OSError as exc:
            return PathProblem("file_not_accessible", f"{label}: {exc.strerror}", False)
    return None


def require_path(path: Any, kind: str, *, flag: Optional[str] = None) -> None:
    """`classify_path`, raising `CallerError` (rc 2 under ``run()``, the
    reason in the ``--json`` envelope) when *path* is not usable."""
    problem = classify_path(path, kind, flag=flag)
    if problem is not None:
        raise _caller_error(problem.message, problem.reason)


def require_dir(path: Any, *, flag: Optional[str] = None) -> None:
    """`require_path` for a directory."""
    require_path(path, "dir", flag=flag)


def require_file(path: Any, *, flag: Optional[str] = None) -> None:
    """`require_path` for a file."""
    require_path(path, "file", flag=flag)


def raise_path_problems(problems: "list[Optional[PathProblem]]") -> None:
    """Raise one `CallerError` naming every problem in *problems* (one
    ``ERROR:`` line each, Nones skipped) with the first one's reason; return
    when there is none. For a tool given several paths at once."""
    found = [p for p in problems if p is not None]
    if found:
        raise _caller_error("\nERROR: ".join(p.message for p in found),
                                         found[0].reason)
