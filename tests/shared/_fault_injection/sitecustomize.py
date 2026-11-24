"""Test-only fault injection for #3724. Imported by Python at start-up when
this directory is on PYTHONPATH and DA_FI_INPUT names a declared input.

From the moment `_lib_input` says the input is judged (its audit event
``da.input.judged`` naming this path, in this process), every look the run
takes at the input — or at anything under it — is a counted point: a stat
(``os.stat`` / ``lstat`` / ``access`` / ``readlink``, which raise no audit
event of their own), and the audit events that take a path (``open``,
``os.listdir``, ``os.scandir``, ``os.walk``, ``glob.glob``, ``os.mkdir``, …).
So is every ``subprocess.Popen`` after it: a child that is not Python (the
Go readers) is not seen inside, so the point is the moment before it starts.

The count is shared by every Python process of the run (DA_FI_COUNT, under
``flock``). With DA_FI_K=k the k-th point is where the fault happens:
DA_FI_MODE ``input`` removes the input; ``file`` removes the file under it
that the point looks at; ``victim`` removes DA_FI_VICTIM (a path relative to
the input) — at a ``subprocess.Popen`` point, a file a Go reader would walk
(#3965). DA_FI_MODE ``judged-victim`` removes DA_FI_VICTIM at the judgement
itself (the audit event, no count): the moment between the judging walk and
anything after it. Every point is a JSON line in DA_FI_LOG with the
first frame outside the standard library (its site), so the gate can check
that the k-th point of an injected run is the k-th of the baseline.

A ``subprocess.Popen`` point also records ``blind``: True when nothing the
child is handed can name the input — no argv element (a relative one
resolved against the child's cwd), the cwd itself, and no environment value
(the hook's own ``DA_FI_*`` aside) is the input or a path under it, or
contains the input's path as text — and the child is not Python (a Python
child runs this hook and looks for itself). A blind child cannot tell the
input is gone, so removing it just before that child starts is the same
fault as removing it at the next point; the gate uses that to skip repeats
(#4224). ``blind`` is a conclusion from what the child is HANDED: a child
that learns the path some other way (stdin, a file it is pointed at) is
outside it, and the gate cross-checks the conclusion instead of trusting it.

Nothing is decided here: the gate test reads the log and judges the run.
"""
import fcntl
import json
import os
import shutil
import sys
import threading

_INPUT = os.environ.get("DA_FI_INPUT")
_LOG = os.environ.get("DA_FI_LOG")
_COUNT = os.environ.get("DA_FI_COUNT")
if _INPUT and _LOG and _COUNT:
    _EVENT = "da.input.judged"
    _K = int(os.environ.get("DA_FI_K", "0"))
    _MODE = os.environ.get("DA_FI_MODE", "input")
    _VICTIM = os.environ.get("DA_FI_VICTIM", "")
    _NAMES = {os.path.normpath(os.path.abspath(_INPUT)), os.path.realpath(_INPUT)}
    _local = threading.local()
    _judged = [False]
    _real = {name: getattr(os, name) for name in ("stat", "lstat", "access", "readlink")}
    _STDLIB = (os.path.dirname(os.__file__), "<frozen")

    def _where(path):
        """'self' / 'child' for the input / a path under it, else None."""
        if path is None or isinstance(path, int):
            return None
        try:
            p = os.fsdecode(path)
        except (TypeError, ValueError):
            return None
        if not os.path.isabs(p):
            p = os.path.join(os.getcwd(), p)
        p = os.path.normpath(p)
        for name in _NAMES:
            if p == name:
                return "self"
            if p.startswith(name + os.sep):
                return "child"
        return None

    def _site(frame):
        """The first frame outside the standard library, and the stack."""
        frames = []
        while frame is not None:
            frames.append((frame.f_code.co_filename,
                           f"{os.path.basename(frame.f_code.co_filename)}:{frame.f_lineno}:"
                           f"{frame.f_code.co_name}"))
            frame = frame.f_back
        stack = [entry for _f, entry in frames[:10]]
        for filename, entry in frames:
            if not filename.startswith(_STDLIB):
                return entry, stack
        return (frames[0][1] if frames else "?"), stack

    def _write(rec):
        fd = os.open(_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(rec) + "\n").encode())
        finally:
            os.close(fd)

    def _next():
        fd = os.open(_COUNT, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            k = int(os.read(fd, 32).decode() or "0") + 1
            os.lseek(fd, 0, 0)
            os.ftruncate(fd, 0)
            os.write(fd, str(k).encode())
            return k
        finally:
            os.close(fd)

    def _remove_victim():
        victim = os.path.join(_INPUT, _VICTIM)
        if not _VICTIM or not os.path.isfile(victim) or os.path.islink(victim):
            return False
        os.remove(victim)
        return True

    def _fault(path, where):
        """Remove what DA_FI_MODE says; True when something was removed."""
        if _MODE == "victim":
            return _remove_victim()
        if _MODE == "file":
            if where != "child" or not os.path.isfile(path) or os.path.islink(path):
                return False
            os.remove(path)
            return True
        if not os.path.lexists(_INPUT):
            return False
        if os.path.isdir(_INPUT) and not os.path.islink(_INPUT):
            shutil.rmtree(_INPUT)
        else:
            os.remove(_INPUT)
        return True

    def _names_input(value, cwd):
        """True when *value* is, or textually contains, the input's path."""
        try:
            v = os.fsdecode(value)
        except (TypeError, ValueError):
            v = str(value)
        if any(name in v for name in _NAMES):
            return True
        return _where(v if os.path.isabs(v) else os.path.join(cwd, v)) is not None

    def _blind(popen_args):
        """The child cannot name the input (see the module docstring)."""
        if not popen_args or len(popen_args) < 4:
            return False
        executable, argv, cwd, env = popen_args[:4]
        argv = [executable] if argv is None else (
            [argv] if isinstance(argv, (str, bytes, os.PathLike)) else list(argv))
        cwd = os.fsdecode(cwd) if cwd is not None else os.getcwd()
        if _where(cwd) is not None:
            return False
        program = os.path.basename(os.fsdecode(executable if executable is not None
                                                else argv[0])).lower()
        if program.startswith("python"):
            return False
        environ = os.environ if env is None else env
        values = [v for key, v in environ.items()
                  if not os.fsdecode(key).startswith("DA_FI_")]
        return not any(_names_input(item, cwd) for item in [*argv, *values])

    def _point(event, path):
        if getattr(_local, "busy", False):
            return
        _local.busy = True
        try:
            if event == "subprocess.Popen":
                if not _judged[0]:
                    return
                where, shown = "popen", os.fsdecode(path) if path is not None else ""
            else:
                where = _where(path)
                if where is None:
                    return
                shown = os.fsdecode(path)
                if not _judged[0]:
                    return
            blind =_blind(getattr(_local, "popen", None)) if event == "subprocess.Popen" else None
            k = _next()
            fired = bool(_K) and k == _K and _fault(shown, where)
            site, stack = _site(sys._getframe(2))
            rec = {"ev": event, "where": where, "path": shown, "k": k, "fired": fired,
                   "pid": os.getpid(), "site": site, "stack": stack}
            if blind is not None:
                rec["blind"] = blind
            _write(rec)
        finally:
            _local.busy = False

    def _audit(event, args):
        if event == _EVENT:
            if _where(args[1]) == "self":
                first = not _judged[0]
                _judged[0] = True
                _write({"ev": "judged", "flag": args[0], "path": args[1], "pid": os.getpid(),
                        "ppid": os.getppid(),
                        "fired": _MODE == "judged-victim" and first and _remove_victim()})
        elif event == "subprocess.Popen":
            _local.popen = args
            _point(event, args[0])
        elif event in ("os.listdir", "os.scandir"):
            _point(event, args[0] if args and args[0] is not None else ".")
        elif event in ("open", "os.walk", "glob.glob", "os.remove", "os.rename", "os.mkdir",
                       "os.rmdir", "os.chmod", "os.utime", "os.symlink", "os.truncate",
                       "shutil.copytree", "shutil.rmtree", "shutil.copyfile", "shutil.move"):
            if args:
                _point(event, args[0])

    def _patched(name):
        real = _real[name]

        def look(path, *args, **kwargs):
            _point(f"os.{name}", path)
            return real(path, *args, **kwargs)
        look.__name__ = name
        return look

    for _name in _real:
        setattr(os, _name, _patched(_name))
    sys.addaudithook(_audit)
