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
  ``os.rmdir`` — with ``path`` (the destination, for the two-path events;
  a rename is also logged as ``os.rename-from`` with the name it took away),
  ``pid``, and ``stack``: the repo's ``scripts/`` frames that raised it,
  innermost first, as ``<file relative to the repo, symlinks and ``..``
  resolved>:<function>``; ``lines`` holds each frame's line number.
* ``spawn`` — ``subprocess.Popen`` / ``os.posix_spawn`` / ``os.exec`` /
  ``os.system``, with the executable, the whole ``argv`` (an ``os.system``
  command is logged as ``sh -c <command>``) and the directory it starts in. A process that is not Python writes
  where this hook cannot see; the spawn is the only trace of it.
* ``hook-error`` — the hook itself raised while handling an event. The
  exception never reaches the audited program; this line is what is left of
  the event, and the gate treats it as a failure.

Where an event landed (``path``), and when the hook cannot say:

* An absolute path is itself. A RELATIVE one carries ``cwd`` (the working
  directory when the event was raised).
* A path relative to a ``dir_fd`` (``shutil.rmtree`` under
  ``TemporaryDirectory`` cleanup) and a FILE DESCRIPTOR (``open(fd, "w")``,
  ``fchmod``, ``ftruncate``) are resolved through ``/proc/self/fd``; such a
  line carries ``fd`` (the descriptor) or ``dir_fd``. A descriptor that is
  no file — a pipe, a socket — resolves to a name that is not absolute
  (``pipe:[…]``) and is logged as that.
* ``unplaced`` (true) marks an event whose target the hook could not
  determine, and which must therefore not be read against anything:
  - a descriptor or ``dir_fd`` it could not resolve (no ``/proc``);
  - a relative path whose working directory is gone;
  - a relative path given to ``os.open``: the audit event does not carry
    ``dir_fd``, so "relative to the working directory" and "relative to some
    directory descriptor" cannot be told apart. (The builtin ``open`` has no
    ``dir_fd``; its relative path is relative to ``cwd``.)

The hook does not replace the environment's own ``sitecustomize``: after
installing itself it runs the next one on ``sys.path``, as Python would have
without this directory in front.

The log is raw on purpose: what counts as noise is the gate's rule
(``test_output_path_write_failure.py``), stated and tested there.
"""
import json
import os
import sys
import threading

_LOG = os.environ.get("DA_WRITE_AUDIT_LOG")
_HERE = os.path.dirname(os.path.realpath(__file__))
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
    def _open_log():
        """The log, on a descriptor out of a program's way: one a tool closes
        in a sweep (`os.closerange(3, 64)`) would lose every later event, and
        its number, handed out again, would be the tool's own file."""
        fd = os.open(_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            import fcntl
            high = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 700)
        except (ImportError, OSError):  # no fcntl here, or a descriptor limit below 700
            return fd
        os.close(fd)
        return high

    def _is_the_log(fd):
        try:
            st = os.fstat(fd)
        except OSError:
            return False
        return (st.st_dev, st.st_ino) == _log_id

    _fd = _open_log()
    _log_id = (os.fstat(_fd).st_dev, os.fstat(_fd).st_ino)
    # Per THREAD: the guard is against this hook's own calls re-entering it.
    # One flag for the whole process dropped every event another thread
    # raised while one was being written.
    _state = threading.local()
    _REPO = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
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

    def _frames(rec, depth=2):
        f = sys._getframe(depth)
        stack, lines = [], []
        while f is not None:
            rel = _repo_file(f.f_code.co_filename)
            if rel is not None:
                stack.append(f"{rel}:{f.f_code.co_name}")
                lines.append(f.f_lineno)
            f = f.f_back
        rec["stack"], rec["lines"] = stack, lines

    def _text(value):
        if isinstance(value, int) or value is None:
            return value
        try:
            return os.fsdecode(value)
        except TypeError:
            return repr(value)

    def _place(rec, dir_fd, os_open):
        """Say where ``rec["path"]`` is, or mark the event ``unplaced``."""
        path = rec["path"]
        if isinstance(path, int):
            rec["fd"] = path
            try:
                rec["path"] = os.readlink(f"/proc/self/fd/{path}")
            except OSError:
                rec["unplaced"] = True
            return
        if not isinstance(path, str) or os.path.isabs(path):
            return
        if dir_fd is not None and dir_fd != -1:  # -1: how the event spells "no dir_fd"
            rec["dir_fd"] = dir_fd
            try:
                rec["path"] = os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), path)
            except OSError:
                rec["unplaced"] = True
            return
        if os_open:
            rec["unplaced"] = True
            return
        try:
            rec["cwd"] = os.getcwd()
        except OSError:
            rec["unplaced"] = True

    def _emit(rec):
        """Append one line. The descriptor is checked first — closed, or
        closed and reused for another file, it is opened again by path — so
        an event is neither dropped nor written into a file of the program."""
        global _fd
        rec["pid"] = os.getpid()
        if not _is_the_log(_fd):
            _fd = _open_log()
        os.write(_fd, (json.dumps(rec) + "\n").encode())

    def _record(event, args):
        rec = None
        dir_fd = None
        os_open = False
        if event == "open":
            path, mode, flags = args
            if path is None:
                return
            if (mode and any(c in mode for c in "wax+")) or (mode is None and flags & _WRITE_BITS):
                rec = {"ev": "open", "path": _text(path), "flags": flags}
                os_open = mode is None
        elif event in _PATH_EVENTS:
            at, fd_at = _PATH_EVENTS[event]
            rec = {"ev": event, "path": _text(args[at])}
            if fd_at is not None and len(args) > fd_at:
                dir_fd = args[fd_at]
            if event == "os.rename":
                # The name the rename takes away is a change of its own: a
                # second line, placed by the same rules as any path.
                gone = {"ev": "os.rename-from", "path": _text(args[0])}
                _place(gone, args[2] if len(args) > 2 else None, False)
                _frames(gone, 3)
                _emit(gone)
        elif event in _SPAWN_EVENTS:
            exe = args[0]
            if event == "os.system":
                # One string for a shell: logged whole, as the only argument.
                rec = {"ev": "spawn", "exe": "sh", "argv": ["sh", "-c", _text(exe)]}
            elif len(args) > 1 and args[1]:
                argv = args[1] if isinstance(args[1], (list, tuple)) else [args[1]]
                rec = {"ev": "spawn", "exe": _text(exe) or _text(argv[0]),
                       "argv": [_text(a) for a in argv]}
                if event == "subprocess.Popen" and len(args) > 2 and args[2] is not None:
                    rec["cwd"] = _text(args[2])
            else:
                rec = {"ev": "spawn", "exe": _text(exe), "argv": []}
            if "cwd" not in rec:
                try:
                    rec["cwd"] = os.getcwd()
                except OSError:
                    pass
        if rec is None:
            return
        if rec["ev"] != "spawn":
            _place(rec, dir_fd, os_open)
            _frames(rec, 3)
        _emit(rec)

    def _hook(event, args):
        if getattr(_state, "busy", False):
            return
        _state.busy = True
        try:
            _record(event, args)
        except Exception as exc:  # noqa: BLE001 — nothing may reach the audited program
            try:
                _emit({"ev": "hook-error", "event": event, "error": repr(exc)})
            except Exception:  # noqa: BLE001
                pass
        finally:
            _state.busy = False

    _emit({"ev": "loaded", "argv": list(getattr(sys, "orig_argv", sys.argv))})
    sys.addaudithook(_hook)


def _run_the_next_sitecustomize():
    """Python imports ONE ``sitecustomize``: with this directory first on
    ``sys.path``, the environment's own would never run. Find it the way the
    import would have without this directory, and run it."""
    from importlib.machinery import PathFinder
    from importlib.util import module_from_spec
    rest = [p for p in sys.path if os.path.realpath(p or os.curdir) != _HERE]
    spec = PathFinder.find_spec("sitecustomize", rest)
    if spec is None or spec.loader is None:
        return
    spec.loader.exec_module(module_from_spec(spec))


_run_the_next_sitecustomize()
