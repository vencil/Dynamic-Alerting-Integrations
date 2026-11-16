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
    """True only when nothing is at the path (ENOENT, ENOTDIR)."""


#: #3276: what a stat errno means for a caller-given path. Only "nothing is
#: there" is not-found; every other errno (EACCES under a parent the caller
#: cannot enter, EIO, …) is not-accessible — never folded into "not found".
#: #3392: a symlink loop (ELOOP) is not "nothing there" either — ``ls`` lists
#: the link — so it is not-accessible: a tool that reads a missing input as
#: empty (config-diff's first import) refuses a loop instead of reading it so.
_STAT_MISSING = frozenset({errno.ENOENT, errno.ENOTDIR})

#: The wording after ``<flag> <path>:``, per language — the one place it is
#: written. ``en`` keeps the OS's strerror; ``zh`` says the same in Chinese
#: and keeps the errno's name (``EACCES``, ``ELOOP``) so the cause stays
#: searchable — for "nothing there" too, unless it is the plain ENOENT: a
#: file used as a directory (ENOTDIR) is listed by ``ls``.
_DETAIL = {
    "en": {"missing": lambda e: e.strerror, "inaccessible": lambda e: e.strerror,
           "not_dir": "not a directory", "not_regular": "not a regular file"},
    "zh": {"missing": lambda e: ("不存在" if e.errno == errno.ENOENT
                                 else f"不存在（{errno.errorcode.get(e.errno, e.errno)}）"),
           "inaccessible": lambda e: f"無法存取（{errno.errorcode.get(e.errno, e.errno)}）",
           "not_dir": "不是目錄", "not_regular": "不是一般檔案"},
}


class NotRegularFile(Exception):
    """`open_regular`: the path opened, and what it names is not a regular
    file (a FIFO, a device, a directory). Deliberately no ``OSError``: a
    caller's ``except OSError`` must not read it as a system error."""


#: Non-blocking where the platform has it: opening a FIFO then returns at once
#: instead of waiting for a writer. Close-on-exec: the fd is never inherited.
_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)


def open_regular(path: Any) -> int:
    """A blocking read-only fd of *path* when it is a regular file (a
    symlink to one included); the caller closes it.

    ⛔ The one place that says what a regular file is and how one is opened
    (#4015). The fd is judged (``fstat``), not the path: a path that is a
    regular file when stat'ed and a FIFO when opened is judged as the FIFO,
    and the open does not block on it, since it is non-blocking until the
    fd is known to be a regular file.

    Raises `NotRegularFile` (the fd is closed) or the open's ``OSError``."""
    fd = os.open(path, _OPEN_FLAGS)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotRegularFile(os.fspath(path))
        if _OPEN_FLAGS & getattr(os, "O_NONBLOCK", 0):
            os.set_blocking(fd, True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def classify_path(path: Any, kind: str, *, lang: str,
                  flag: Optional[str] = None) -> Optional[PathProblem]:
    """None when *path* (given by the caller, via *flag*) is usable as a
    *kind* — ``"dir"``, ``"file"`` or ``"any"`` — else the `PathProblem`.

    *lang* (``"en"`` / ``"zh"``) is the language of the line the message
    lands in — the tool's own (a bilingual tool passes its
    ``detect_cli_lang()``). It has no default: a caller that forgot it
    would put English inside a Chinese line without anyone noticing.

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
    detail = _DETAIL[lang]
    try:
        st = os.stat(path)
    except OSError as exc:
        missing = exc.errno in _STAT_MISSING
        said = detail["missing" if missing else "inaccessible"](exc)
        return PathProblem(f"{noun}_not_{'found' if missing else 'accessible'}",
                           f"{label}: {said}", missing)
    if kind == "dir" and not stat.S_ISDIR(st.st_mode):
        return PathProblem("dir_not_found", f"{label}: {detail['not_dir']}", False)
    if kind == "file":
        if not stat.S_ISREG(st.st_mode):
            return PathProblem("file_not_found", f"{label}: {detail['not_regular']}", False)
        try:
            os.close(open_regular(path))
        except NotRegularFile:  # replaced since the stat (#4015)
            return PathProblem("file_not_found", f"{label}: {detail['not_regular']}", False)
        except OSError as exc:
            return PathProblem("file_not_accessible",
                               f"{label}: {detail['inaccessible'](exc)}", False)
    return None


def supplied(args: Any, parser: Any, dest: str) -> bool:
    """True when the caller gave *dest* on the command line, False when it
    holds *parser*'s default (#3392: a path the caller gave is judged; a
    default nobody asked for keeps the tool's own reading of it).

    By identity: argparse puts its default object itself in the namespace
    when the flag is absent, and a fresh value when it is given. That holds
    for a non-str default (``Path("conf.d")``) and for a str default with no
    ``type=``; a str default WITH a ``type=`` is converted anew on each
    parse, so such a flag must not ask this.
    """
    return getattr(args, dest) is not parser.get_default(dest)


def require_path(path: Any, kind: str, *, lang: str, flag: Optional[str] = None) -> None:
    """`classify_path`, raising `CallerError` (rc 2 under ``run()``, one
    stderr line, nothing on stdout) when *path* is not usable."""
    problem = classify_path(path, kind, lang=lang, flag=flag)
    if problem is not None:
        raise _caller_error(problem.message, problem.reason)


def require_dir(path: Any, *, lang: str, flag: Optional[str] = None) -> None:
    """`require_path` for a directory."""
    require_path(path, "dir", lang=lang, flag=flag)


def require_file(path: Any, *, lang: str, flag: Optional[str] = None) -> None:
    """`require_path` for a file."""
    require_path(path, "file", lang=lang, flag=flag)


def raise_path_problems(problems: "list[Optional[PathProblem]]") -> None:
    """Raise one `CallerError` naming every problem in *problems* (one
    ``ERROR:`` line each, Nones skipped) with the first one's reason; return
    when there is none. For a tool given several paths at once."""
    found = [p for p in problems if p is not None]
    if found:
        raise _caller_error("\nERROR: ".join(p.message for p in found),
                                         found[0].reason)
