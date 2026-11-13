"""sitecustomize.py — record what a Python process loads and reads (#3756).

Not imported by name. A workflow job puts THIS DIRECTORY on `PYTHONPATH` and
sets `DA_PATH_TRACE_DIR` (`scripts/ops/workflow_path_filter.py env` prints both
lines), and from then on every Python interpreter the job starts — a step's own
`python3 x.py`, one started from a shell script or a Makefile target — imports
this module at startup, because that is what the interpreter does with a module
named `sitecustomize`. With `DA_PATH_TRACE_DIR` unset it does nothing.

What it records, one `kind<TAB>absolute path` line per distinct pair, appended
to a log in that directory:

  * `exec`  — the file of every code object the interpreter executes as a
              module (the `exec` audit event). This is the event that sees an
              import: a cached import opens the `.pyc`, not the source, and a
              tool that rolls `sys.modules` back after loading something
              (check_image_refs_resolve.py does) leaves nothing to dump.
  * `open`  — every file opened for reading through Python (the `open` audit
              event, `os.open` included): data files, and the source of a
              module with no cache.
  * `mod`   — `__file__` of every module still in `sys.modules` at exit.
  * `path`  — the `sys.path` entries seen. They are where a literal `import x`
              in a listed file is looked up (see the consumer).
  * `error` — an event this module could not record (not a path: the event and
              the exception). The consumer refuses a trace that holds one: an
              event that was dropped is a load nobody measured.

A relative path is resolved against the working directory AT THE EVENT, and
the resolved path is what is de-duplicated: `open("README.md")` before and
after an `os.chdir` are two files.

Which of those are first-party, and whether the workflow's path filter covers
them, is decided by `scripts/ops/workflow_path_filter.py`, never here: this file
runs inside every interpreter of the job, so it stays small. Nothing raised in
the hook reaches the traced program — it becomes an `error` line.

NOT seen, by construction (the consumer's docstring lists them as boundaries):
a file read by a non-Python process (`cat`, `go build`, node), an interpreter
started with `-I` / `-E` / `-S` or with an `env=` that drops the two variables
(those ignore `PYTHONPATH`, `site`, or never get it), and a directory listing
(a file ADDED to a scanned directory).

Lines are written with one `os.write` each to an `O_APPEND` descriptor opened
before the hook is installed, so a forked child shares the log safely and a
process that ends through `os._exit` or a signal has already recorded
everything it loaded.
"""
import os
import sys


def _install():
    trace_dir = os.environ.get("DA_PATH_TRACE_DIR")
    if not trace_dir:
        return
    os.makedirs(trace_dir, exist_ok=True)
    fd = os.open(
        os.path.join(trace_dir, "py-%d-%s.log" % (os.getpid(), os.urandom(4).hex())),
        os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    seen = set()
    abspath, fsdecode, write = os.path.abspath, os.fsdecode, os.write
    write_only = os.O_WRONLY  # an `open` with this access mode cannot read

    def emit(kind, text):
        key = (kind, text)
        if key not in seen:
            seen.add(key)
            write(fd, ("%s\t%s\n" % (kind, text)).encode("utf-8", "surrogateescape"))

    def record(kind, path):
        if path is None or isinstance(path, int):
            return  # a file descriptor: whatever opened it was recorded then
        path = fsdecode(path)
        if not path or path.startswith("<") or "\n" in path or "\t" in path:
            return
        emit(kind, abspath(path))

    def failed(event, exc):
        try:
            emit("error", "%s event: %s: %s" % (
                event, type(exc).__name__, str(exc).replace("\n", " ")[:200]))
        except Exception:  # noqa: BLE001 — nothing left to tell it to
            pass

    def hook(event, args):
        if event != "exec" and event != "open" and event != "import":
            return
        try:
            if event == "exec":
                record("exec", getattr(args[0], "co_filename", None))
            elif event == "open":
                mode = args[1] if len(args) > 1 else None
                flags = args[2] if len(args) > 2 else None
                if isinstance(mode, str):
                    if "r" not in mode and "+" not in mode:
                        return
                elif isinstance(flags, int) and flags & os.O_ACCMODE == write_only:
                    return
                record("open", args[0])
            else:
                for entry in args[2] or ():
                    record("path", entry or os.curdir)
        except Exception as exc:  # noqa: BLE001 — never into the traced program
            failed(event, exc)

    def at_exit():
        try:
            for module in list(sys.modules.values()):
                record("mod", getattr(module, "__file__", None))
            for entry in list(sys.path):
                record("path", entry or os.curdir)
        except Exception as exc:  # noqa: BLE001
            failed("exit", exc)

    import atexit
    atexit.register(at_exit)
    sys.addaudithook(hook)


def _chain():
    """Import the `sitecustomize` this one shadows, if there is one."""
    here = os.path.dirname(os.path.abspath(__file__))
    rest = [p for p in sys.path if os.path.abspath(p or os.curdir) != here]
    if len(rest) == len(sys.path):
        return
    import importlib.machinery
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", rest)
    if spec is None or spec.loader is None:
        return
    import importlib.util
    spec.loader.exec_module(importlib.util.module_from_spec(spec))


try:
    _install()
except Exception:  # noqa: BLE001 — a tracing failure must not fail the job step
    pass
_chain()
