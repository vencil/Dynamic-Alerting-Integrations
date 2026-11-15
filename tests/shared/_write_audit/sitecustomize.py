"""Test-only write audit (#3107). Imported by Python at start-up when this
directory is on PYTHONPATH and DA_WRITE_AUDIT_LOG names a log file. A child
Python process inherits both, so it is audited into the same log.

One JSON line per event, in the order raised:

* ``loaded`` — once per process, at start-up (``argv`` = ``sys.orig_argv``).
  The gate requires it: an absent line is "the hook did not load", which is
  a failure of the run and never "nothing was written".
* every filesystem change raised as an audit event: a write-mode ``open`` /
  ``os.open``, ``os.rename`` (``os.replace`` too), ``os.chmod``, ``os.chown``,
  ``os.mkdir``, ``os.link``, ``os.symlink``, ``os.truncate``, ``os.remove``,
  ``os.rmdir`` — with ``path`` (the destination, for the two-path events),
  ``pid``, and ``stack``: the repo's ``scripts/`` frames that raised it,
  innermost first, as ``<file relative to the repo, symlinks and ``..``
  resolved>:<function>``.
* ``spawn`` — ``subprocess.Popen`` / ``os.posix_spawn`` / ``os.exec`` /
  ``os.system``, with the executable. A process that is not Python writes
  where this hook cannot see; the spawn is the only trace of it.

What a reader of the log has to know about ``path``:

* It is what the caller passed. A RELATIVE one carries ``cwd`` (the working
  directory when the event was raised) so it can be placed.
* A path relative to a ``dir_fd`` (``shutil.rmtree`` under
  ``TemporaryDirectory`` cleanup) is resolved through ``/proc/self/fd`` where
  that exists; where it cannot be, the line carries ``dir_fd`` and NO ``cwd``
  — it must not be read against the working directory.
* An integer ``path`` is a file descriptor (``open(fd, "w")``,
  ``os.truncate(fd, …)``): the file it names was opened by an earlier event.

The log is raw on purpose: what counts as noise is the gate's rule
(``test_output_path_write_failure.py``), stated and tested there.
"""
import json
import os
import sys

_LOG = os.environ.get("DA_WRITE_AUDIT_LOG")
_WRITE_BITS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
# event -> (index of the path it changes, index of its dir_fd or None)
_PATH_EVENTS = {
    "os.rename": (1, 3),
    "os.chmod": (0, 2),
    "os.chown": (0, 3),
    "os.mkdir": (0, 2),
    "os.link": (1, 3),
    "os.symlink": (1, 2),
    "os.truncate": (0, None),
    "os.remove": (0, 1),
    "os.rmdir": (0, 1),
}
_SPAWN_EVENTS = {"subprocess.Popen", "os.posix_spawn", "os.exec", "os.system"}
if _LOG:
    _fd = os.open(_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    _busy = False
    _REPO = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.realpath(__file__)))))
    _SCRIPTS = os.path.join(_REPO, "scripts") + os.sep
    _real: dict = {}

    def _repo_file(filename):
        """*filename* relative to the repo if it is under scripts/, else None.
        Resolved first: a tool run as ``ops/x.py`` imports ``ops/../_lib_io.py``."""
        if filename not in _real:
            real = os.path.realpath(filename)
            _real[filename] = (real[len(_REPO) + 1:].replace(os.sep, "/")
                               if real.startswith(_SCRIPTS) else None)
        return _real[filename]

    def _frames():
        f = sys._getframe(2)
        out = []
        while f is not None:
            rel = _repo_file(f.f_code.co_filename)
            if rel is not None:
                out.append(f"{rel}:{f.f_code.co_name}")
            f = f.f_back
        return out

    def _text(value):
        if isinstance(value, int) or value is None:
            return value
        try:
            return os.fsdecode(value)
        except TypeError:
            return repr(value)

    def _place(rec, dir_fd):
        """Say what a relative ``rec["path"]`` is relative to."""
        path = rec["path"]
        if not isinstance(path, str) or os.path.isabs(path):
            return
        if dir_fd is None or dir_fd == -1:  # -1: how the event spells "no dir_fd"
            rec["cwd"] = os.getcwd()
            return
        try:
            rec["path"] = os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), path)
        except OSError:
            rec["dir_fd"] = dir_fd

    def _emit(rec):
        rec["pid"] = os.getpid()
        os.write(_fd, (json.dumps(rec) + "\n").encode())

    def _hook(event, args):
        global _busy
        if _busy:
            return
        rec = None
        dir_fd = None
        if event == "open":
            path, mode, flags = args
            if path is None:
                return
            if (mode and any(c in mode for c in "wax+")) or (mode is None and flags & _WRITE_BITS):
                rec = {"ev": "open", "path": _text(path), "flags": flags}
        elif event in _PATH_EVENTS:
            at, fd_at = _PATH_EVENTS[event]
            rec = {"ev": event, "path": _text(args[at])}
            if fd_at is not None and len(args) > fd_at:
                dir_fd = args[fd_at]
            if event == "os.rename":
                rec["src"] = _text(args[0])
        elif event in _SPAWN_EVENTS:
            exe = args[0]
            if event == "subprocess.Popen" and len(args) > 1 and args[1]:
                argv = args[1] if isinstance(args[1], (list, tuple)) else [args[1]]
                rec = {"ev": "spawn", "exe": _text(exe) or _text(argv[0]),
                       "argv": [_text(a) for a in argv[:4]]}
            else:
                rec = {"ev": "spawn", "exe": _text(exe), "argv": []}
        if rec is None:
            return
        _busy = True
        try:
            if rec["ev"] != "spawn":
                _place(rec, dir_fd)
                rec["stack"] = _frames()
            _emit(rec)
        finally:
            _busy = False

    _emit({"ev": "loaded", "argv": list(getattr(sys, "orig_argv", sys.argv))})
    sys.addaudithook(_hook)
