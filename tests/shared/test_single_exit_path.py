"""One exit path for da-tools CLI tools (#3124, #3558).

The contract of ``scripts/tools/_lib_exitcodes.py`` that is observable only
at the end of a REAL interpreter (atexit, excepthook, the final stdout
flush), so every behavioural case runs a subprocess:

* **stdout that cannot take the output is a failure, never success.** A
  reader that left (``tool | head -c1``, ``tool | true``), a full device, a
  closed fd 1 (``>&-``, closed mid-run): whatever the tool was about to exit
  with, the process ends non-zero — the caller-error code (2) — with one line
  ``ERROR: cannot write to stdout: <reason>``; never 0, never the
  interpreter's own 120 (#3124). Which line is printed when two things fail
  at once is not part of the contract (dev-rules §13, known limits); the
  exit code is.

* **The library writes nothing to stdout but the tool's own Report**
  (#3558). A caller error, a ``CallerError``, a hand-written ``sys.exit(2)``
  and a crash all leave stdout empty — no envelope.

* **Closed standard fds are filled at import** (#3558, ``sanitize_std_fds``):
  ``>&-`` is then an ordinary failing write (rc 2), ``2>&-`` keeps a tool's
  stderr line off stdout, and a later ``open()`` can never be handed fd 1.

* **``Report(..., delivery="best_effort")``** (patch-config, #3558 P2): the
  stdout line is printed, the tool's status is kept.
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "scripts" / "tools"
OPS_DIR = TOOLS_DIR / "ops"

# The stdout-failure line, up to its reason (EPIPE / ENOSPC / EBADF / …).
STDOUT_CLOSED_LINE = "ERROR: cannot write to stdout:"
CRASH_MARK = "unexpected internal error"


def _said_and_failed(rc: int, err: str, where: str = "", code: int = 2) -> None:
    """The new contract's floor for a delivery failure (#3558): the run
    ends non-zero (*code*), never 0 or the interpreter's 120, and stderr
    says something — the stdout line, or, where the failure surfaced as an
    exception the library cannot tie to stdout, the crash line (which line
    is a documented limit, dev-rules §13; the exit code is the contract)."""
    where = where or err
    assert rc == code, where
    assert "Exception ignored" not in err, where
    assert err.count(STDOUT_CLOSED_LINE) <= 1, where
    assert STDOUT_CLOSED_LINE in err or CRASH_MARK in err, where

# Run without PYTHONUNBUFFERED, as CI does: the 120 only appears when the
# tool's own output is still sitting in the block buffer at exit.
_ENV = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}

_PRELUDE = (
    "import io, os, sys\n"
    "sys.path.insert(0, {tools!r})\n"
    "import _lib_exitcodes\n"
)


def _script(tmp_path: Path, body: str, name: str = "tool.py") -> Path:
    script = tmp_path / name
    script.write_text(_PRELUDE.format(tools=str(TOOLS_DIR)) + body, encoding="utf-8")
    return script


# PYTHONUNBUFFERED=1: each write reaches the fd at once, so a lost stdout
# shows up mid-run (the write itself raises) instead of at the exit flush.
_UNBUFFERED_ENV = dict(_ENV, PYTHONUNBUFFERED="1")
_BUFFERING = {"buffered": _ENV, "unbuffered": _UNBUFFERED_ENV}


def _run(script: Path, stdout: int = subprocess.PIPE,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Bytes out (no text mode): stdout is compared byte for byte."""
    return subprocess.run(
        [sys.executable, str(script)], stdout=stdout, stderr=subprocess.PIPE,
        timeout=60, env=_ENV if env is None else env)


# ═══════════════════════════════════════════════════════════════════════════
# #3124 — stdout closed by the reader
# ═══════════════════════════════════════════════════════════════════════════
# The four ways a tool ends. Each is preceded by buffered output of its own.
_ENDINGS = {
    "exit0": "sys.exit(0)\n",
    "exit1": "sys.exit(1)\n",
    "die_caller_error": "_lib_exitcodes.die_caller_error('ERROR: bad input')\n",
    "crash": "raise RuntimeError('boom')\n",
}

# How a tool's entry is reached: straight at module level (excepthook +
# atexit), or through the single process entry every --json tool uses.
_ENTRIES = ("module-level", "run")


def _body(ending: str, closure: str, entry: str) -> str:
    if closure == "fd-closed":
        # fd 1 closed DURING the run (the tool's own output comes after it):
        # buffered, the exit flush fails; unbuffered, the print itself does.
        work = "os.close(1)\nprint('partial report')\n" + ending
    elif closure == "reader-gone":
        work = "print('partial report')\n" + ending
    else:  # big-output-into-head: the write itself fails, before the ending
        work = "print('x' * 1_000_000)\n" + ending
    if entry == "module-level":
        return work
    indented = "".join("    " + line + "\n" for line in work.splitlines())
    return "def main():\n" + indented + "_lib_exitcodes.run(main)\n"


def _run_closed(tmp_path: Path, body: str, closure: str,
                env: dict[str, str] | None = None) -> tuple[int, str]:
    """Run *body* with stdout closed the *closure* way; (rc, stderr)."""
    env = _ENV if env is None else env
    script = _script(tmp_path, body)
    if closure == "fd-closed":
        proc = _run(script, env=env)
        return proc.returncode, proc.stderr.decode("utf-8", "replace")
    if closure == "reader-gone":
        # A pipe whose read end is already closed: `tool | true` after `true`
        # has exited. Deterministic — no race with a reader process.
        r, w = os.pipe()
        os.close(r)
        try:
            proc = _run(script, stdout=w, env=env)
        finally:
            os.close(w)
        return proc.returncode, proc.stderr.decode("utf-8", "replace")
    head = shutil.which("head")
    if head is None:
        pytest.skip("`head` is not on PATH")
    tool = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env)
    reader = subprocess.Popen([head, "-c1"], stdin=tool.stdout,
                              stdout=subprocess.DEVNULL)
    tool.stdout.close()  # only `head` holds the read end now
    try:
        err = tool.stderr.read()
        rc = tool.wait(timeout=60)
        reader.wait(timeout=60)
    finally:
        for proc in (tool, reader):
            if proc.poll() is None:
                proc.kill()
        tool.stderr.close()
    return rc, err.decode("utf-8", "replace")


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
@pytest.mark.parametrize("entry", _ENTRIES)
@pytest.mark.parametrize("closure", ["fd-closed", "reader-gone", "big-output-into-head"])
@pytest.mark.parametrize("ending", sorted(_ENDINGS))
@pytest.mark.real_crash_exit_code  # a crash line ends at the production crash code, 2
def test_closed_stdout_ends_at_the_caller_error_code(tmp_path, ending, closure, entry,
                                                     buffering):
    """Any ending × any way of losing stdout × either buffering ⇒ rc 2,
    never 0 or 120, and a line on stderr.

    Where the reader left (``reader-gone``, ``big-output-into-head``) the
    line is the named stdout line, exactly once, and a non-crash ending
    prints no crash line. fd 1 closed mid-run with an unbuffered stdout
    raises EBADF from the write itself, which the library does not tie to
    stdout (#3558 drops the exception-chain tracing): reported as a crash,
    still rc 2 — the documented "which line" limit.
    """
    rc, err = _run_closed(tmp_path, _body(_ENDINGS[ending], closure, entry), closure,
                          _BUFFERING[buffering])
    where = f"{ending} × {closure} × {entry} × {buffering}: rc={rc}\nstderr:\n{err[-1500:]}"
    _said_and_failed(rc, err, where)
    if closure != "fd-closed":
        assert err.count(STDOUT_CLOSED_LINE) == 1, where
        if ending != "crash":
            assert CRASH_MARK not in err and "Traceback" not in err, where


def test_closed_stdout_uses_the_caller_error_code_not_the_crash_code(tmp_path):
    """The two codes are separate: a tool that moves its crash code (#3092
    will, under tests) still ends a closed-stdout run at its caller-error
    code; a tool that sets its caller-error code to 1 ends at 1."""
    body = ("_lib_exitcodes.set_crash_exit_code(70)\n"
            "print('partial report')\nos.close(1)\nraise RuntimeError('boom')\n")
    rc, err = _run_closed(tmp_path, body, "fd-closed")
    assert rc == 2, err
    body = ("_lib_exitcodes.set_caller_error_code(1)\n"
            "print('partial report')\nos.close(1)\nsys.exit(0)\n")
    rc, err = _run_closed(tmp_path, body, "fd-closed")
    assert rc == 1, err
    assert STDOUT_CLOSED_LINE in err


def test_tenant_verify_ends_a_closed_stdout_at_the_caller_error_code():
    """#3393: tenant_verify's reader-gone exit is the shared caller-error
    code 2 — nothing was verified, which the rollback runbook reads as
    "fix and re-run" — not 1, its "hash mismatch" since #3393 (it was 1
    while the tool inverted 1/2)."""
    r, w = os.pipe()
    os.close(r)
    try:
        proc = subprocess.run(
            [sys.executable, str(TOOLS_DIR / "dx" / "tenant_verify.py"), "--help"],
            stdout=w, stderr=subprocess.PIPE, timeout=60, env=_ENV)
    finally:
        os.close(w)
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 2, err
    assert STDOUT_CLOSED_LINE in err


@pytest.mark.parametrize("entry", _ENTRIES)
@pytest.mark.real_crash_exit_code
def test_a_socket_broken_pipe_is_still_a_crash(tmp_path, entry):
    """Control: a BrokenPipeError whose broken end is NOT stdout is the tool's
    own failure — traceback, crash line, crash code, and stdout delivered."""
    work = ("import socket\n"
            "print('partial report')\n"
            "a, b = socket.socketpair()\nb.close()\n"
            "a.sendall(b'x' * 1_000_000)\n")
    if entry == "run":
        work = ("def main():\n" + "".join("    " + line + "\n" for line in work.splitlines())
                + "_lib_exitcodes.run(main)\n")
    proc = _run(_script(tmp_path, work))
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 2, err
    assert f"{CRASH_MARK} (BrokenPipeError)" in err, err
    assert "Traceback" in err
    assert STDOUT_CLOSED_LINE not in err, err
    assert proc.stdout == b"partial report\n"


@pytest.mark.real_crash_exit_code
def test_an_open_stdout_keeps_the_tools_exit_code(tmp_path):
    """Anti-vacuity for the matrix: with nobody closing stdout, each ending
    keeps its own code and no named line appears."""
    for ending, want in (("exit0", 0), ("exit1", 1), ("die_caller_error", 2), ("crash", 2)):
        proc = _run(_script(tmp_path, "print('partial report')\n" + _ENDINGS[ending]))
        err = proc.stderr.decode("utf-8", "replace")
        assert proc.returncode == want, (ending, err)
        assert proc.stdout == b"partial report\n", ending
        assert STDOUT_CLOSED_LINE not in err, ending


# The conftest resets the process caller-error code after every test: a tool
# run in-process as __main__ that sets it (tenant_verify set 1 until #3393)
# must not leak it into the next test of the same worker — measured, it turned reword_chain's
# die_caller_error into rc 1. The two probes run only in the ordered,
# single-process pytest that the test below starts.
_LEAK_PROBE = "DA_CALLER_CODE_LEAK_PROBE"


@pytest.mark.skipif(not os.environ.get(_LEAK_PROBE), reason="driven by the test below")
def test_probe_sets_the_caller_error_code():
    sys.path.insert(0, str(TOOLS_DIR))
    import _lib_exitcodes as ec  # noqa: E402
    ec.set_caller_error_code(1)


@pytest.mark.skipif(not os.environ.get(_LEAK_PROBE), reason="driven by the test below")
def test_probe_sees_the_default_caller_error_code():
    sys.path.insert(0, str(TOOLS_DIR))
    import _lib_exitcodes as ec  # noqa: E402
    assert ec.get_caller_error_code() == 2


def test_the_caller_error_code_does_not_leak_into_the_next_test():
    me = Path(__file__)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
         f"{me}::test_probe_sets_the_caller_error_code",
         f"{me}::test_probe_sees_the_default_caller_error_code"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300, cwd=str(REPO_ROOT), env=dict(os.environ, **{_LEAK_PROBE: "1"}))
    assert proc.returncode == 0 and "2 passed" in proc.stdout, proc.stdout[-1500:]


def test_set_caller_error_code_rejects_unusable_codes():
    sys.path.insert(0, str(TOOLS_DIR))
    import _lib_exitcodes as ec  # noqa: E402
    for bad in (0, -1, 256, True, "2", None):
        with pytest.raises(ValueError):
            ec.set_caller_error_code(bad)
    assert ec.get_caller_error_code() == 2


# ═══════════════════════════════════════════════════════════════════════════
# #3558 — the library writes nothing to stdout but the tool's own Report
# ═══════════════════════════════════════════════════════════════════════════
def _armed(body: str) -> str:
    """*body* inside main, entered through run() — the shape of every tool
    (#3558: nothing is armed any more; the name is kept for the cases)."""
    indented = "".join("    " + line + "\n" for line in body.splitlines())
    return (f"def main():\n"
            f"{indented}"
            f"_lib_exitcodes.run(main)\n")


def _json_run(tmp_path: Path, body: str) -> tuple[int, str, str]:
    proc = _run(_script(tmp_path, body))
    return (proc.returncode, proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


@pytest.mark.parametrize("entry", ["run", "excepthook"])
def test_raised_caller_error_is_not_a_crash(tmp_path, entry):
    """`raise CallerError(message, reason)`: rc 2, ONE stderr line, no
    traceback, and nothing on stdout — whether it reaches run() or the
    module-level excepthook."""
    raise_it = "raise _lib_exitcodes.CallerError('prometheus unreachable', reason='prometheus_query_error')\n"
    body = _armed(raise_it) if entry == "run" else raise_it
    rc, out, err = _json_run(tmp_path, body)
    assert rc == 2, err
    assert err.strip().splitlines() == ["ERROR: prometheus unreachable"], err
    assert out == "", out


def test_a_caller_error_subclass_carries_its_own_reason(tmp_path):
    """The shape #3073/#3074 will use: a subclass names its reason once, on
    the class, and is never treated as a crash."""
    rc, out, err = _json_run(tmp_path, (
        "class QueryError(_lib_exitcodes.CallerError):\n"
        "    reason = 'prometheus_query_error'\n"
        + _armed("raise QueryError('ERROR: query failed: timeout')\n")))
    assert rc == 2, err
    assert CRASH_MARK not in err and "Traceback" not in err, err
    assert err.strip() == "ERROR: query failed: timeout"
    assert out == "", out


def test_caller_error_follows_the_caller_error_code(tmp_path):
    rc, out, err = _json_run(tmp_path, (
        "_lib_exitcodes.set_caller_error_code(1)\n"
        "raise _lib_exitcodes.CallerError('bad')\n"))
    assert rc == 1, err
    assert out == ""


# Every way a run ends without a Report. The library adds nothing to stdout
# on any of them (#3558: no envelope) — the tool's stderr line, its code.
_NO_REPORT_EXITS = {
    "die_caller_error": ("_lib_exitcodes.die_caller_error('ERROR: x', reason='y')\n", 2, "ERROR: x"),
    "exit_caller_error": ("print('ERROR: x', file=sys.stderr)\n"
                          "_lib_exitcodes.exit_caller_error('ERROR: x', 'y')\n", 2, "ERROR: x"),
    "CallerError": ("raise _lib_exitcodes.CallerError('x', reason='y')\n", 2, "ERROR: x"),
    "hand-written": ("print('ERROR: x', file=sys.stderr)\nsys.exit(2)\n", 2, "ERROR: x"),
    "returned-2": ("print('ERROR: x', file=sys.stderr)\nreturn 2\n", 2, "ERROR: x"),
    "crash": ("raise RuntimeError('x')\n", 2, "ERROR: unexpected internal error (RuntimeError): x"),
    "exit-0": ("sys.exit(0)\n", 0, ""),
    "exit-1": ("return 1\n", 1, ""),
    "exit-3": ("sys.exit(3)\n", 3, ""),
}


@pytest.mark.parametrize("how", sorted(_NO_REPORT_EXITS))
@pytest.mark.real_crash_exit_code
def test_no_report_means_an_empty_stdout(tmp_path, how):
    """#3558: a run that hands back no Report leaves stdout byte-for-byte
    empty — a caller error and a crash included — with the tool's line as
    the last line of stderr and its code."""
    body, code, line = _NO_REPORT_EXITS[how]
    proc = _run(_script(tmp_path, _armed(body)))
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == code, err
    assert proc.stdout == b"", proc.stdout
    assert (err.strip().splitlines() or [""])[-1] == line, err


def test_run_returns_the_mains_value_as_the_exit_status(tmp_path):
    rc, out, err = _json_run(tmp_path, "def main():\n    return 2\n_lib_exitcodes.run(main)\n")
    assert (rc, out, err) == (2, "", "")


# ═══════════════════════════════════════════════════════════════════════════
# AST gate: every armed tool enters through _lib_exitcodes.run
# ═══════════════════════════════════════════════════════════════════════════


def _is_main_guard(node: ast.stmt) -> bool:
    return (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__"
            and len(node.test.comparators) == 1
            and isinstance(node.test.comparators[0], ast.Constant)
            and node.test.comparators[0].value == "__main__")


def _entry_signature_fault(fn: ast.FunctionDef) -> str | None:
    """`run()` calls the entry with no arguments (#3579): a parameter without
    a default is a TypeError on the first run, and a ``main(argv)`` whose
    argv is required cannot be reached through run() at all."""
    a = fn.args
    positional = a.posonlyargs + a.args
    required = [p.arg for p in positional[:len(positional) - len(a.defaults)]]
    required += [k.arg for k, d in zip(a.kwonlyargs, a.kw_defaults) if d is None]
    if required:
        return (f"`{fn.name}` takes {', '.join(required)} without a default, but "
                "run() calls it with no arguments — give every parameter a default, "
                "or hand run() a no-argument wrapper")
    return None


def _guard_fault(tree: ast.Module) -> tuple[int, str] | None:
    """(line, why) when the module does not enter through
    `_lib_exitcodes.run(<entry>)`, else None."""
    guards = [n for n in tree.body if _is_main_guard(n)]
    if len(guards) != 1:
        line = guards[1].lineno if guards else 1
        return line, f"expected one `if __name__ == \"__main__\":` guard, found {len(guards)}"
    last = guards[0].body[-1]
    call = last.value if isinstance(last, ast.Expr) else None
    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and call.func.attr == "run" and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "_lib_exitcodes"
            and len(call.args) == 1 and isinstance(call.args[0], ast.Name)
            and not call.keywords):
        return (last.lineno,
                f"the guard must end with `_lib_exitcodes.run(<entry>)`, got `{ast.unparse(last)}`")
    entry = call.args[0].id
    # The definition the guard sees: the last module-level one of that name.
    defs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == entry]
    if not defs:
        return last.lineno, f"`{entry}` is not a module-level function"
    for stmt in guards[0].body[:-1]:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == entry:
                return stmt.lineno, f"`{entry}` is also called outside run(): `{ast.unparse(stmt)}`"
    if why := _entry_signature_fault(defs[-1]):
        return defs[-1].lineno, why
    return None


def _guard_problem(tree: ast.Module) -> str | None:
    fault = _guard_fault(tree)
    return None if fault is None else f"line {fault[0]}: {fault[1]}"


def _da_tools_scripts() -> dict[str, Path]:
    """Every `da-tools <cmd>` script, resolved the way the CLI resolves it:
    the population is COMMAND_MAP itself, not a list kept here."""
    import entrypoint  # noqa: E402  (path set by conftest.py)
    out = {}
    for cmd, script in sorted(entrypoint.COMMAND_MAP.items()):
        path, searched = entrypoint._resolve_script_path(script)
        assert path is not None, f"{cmd}: {script} not found in {searched}"
        out[cmd] = Path(path)
    return out


def test_every_da_tools_command_enters_through_run():
    """#3156 (owner, 2026-10-21): every CLI tool — armed or not — ends through
    `_lib_exitcodes.run`, the one exit that owns the last flush. The rounds
    #3124 / #3115 / #3156 fixed edges of `run()`; a tool outside it does not
    get those fixes. No pending list: a new command must enter through
    `run()` from its first commit."""
    scripts = _da_tools_scripts()
    # anti-vacuity: the population is the CLI, Python tools in both dirs
    assert {"guard", "tenant-verify", "alert-quality"} <= set(scripts), sorted(scripts)
    # distinct files, not commands: a resolver that sent every command to
    # one script would otherwise still count 50+
    assert len(set(scripts.values())) >= 50, sorted(scripts)
    bad = {}
    for cmd, path in scripts.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if why := _guard_problem(tree):
            bad[cmd] = f"{path.relative_to(REPO_ROOT)}: {why}"
    assert not bad, "\n".join(f"{c}: {w}" for c, w in sorted(bad.items()))


@pytest.mark.parametrize("src, ok", [
    ("if __name__ == '__main__':\n    import _lib_exitcodes\n    _lib_exitcodes.run(main)\n", True),
    ("if __name__ == '__main__':\n    main()\n", False),
    ("if __name__ == '__main__':\n    sys.exit(main())\n", False),
    ("if __name__ == '__main__':\n    main()\n    _lib_exitcodes.run(main)\n", False),
    ("if __name__ == '__main__':\n    _lib_exitcodes.run(lambda: main())\n", False),
    ("", False),
])
def test_guard_predicate_on_known_shapes(src, ok):
    """The gate's predicate, fed shapes whose answer is known."""
    tree = ast.parse("def main():\n    pass\n" + src)
    assert (_guard_problem(tree) is None) is ok, _guard_problem(tree)


@pytest.mark.parametrize("sig, ok", [
    ("def main():", True),
    ("def main(argv=None):", True),
    ("def main(argv: list[str] | None = None, *, lang='en'):", True),
    ("def main(*args, **kwargs):", True),
    ("def main(argv):", False),
    ("def main(argv, /, x=1):", False),
    ("def main(argv=None, *, lang):", False),
])
def test_entry_signature_on_known_shapes(sig, ok):
    """#3579: run() calls the entry with no arguments, so every parameter
    needs a default. The fault names the def's own line."""
    src = ("import sys\n" + sig + "\n    pass\n"
           "if __name__ == '__main__':\n    _lib_exitcodes.run(main)\n")
    fault = _guard_fault(ast.parse(src))
    assert (fault is None) is ok, fault
    if not ok:
        assert fault[0] == 2, fault


def test_the_entry_the_guard_sees_is_the_last_definition():
    src = ("def main(argv):\n    pass\n"
           "def main():\n    pass\n"
           "if __name__ == '__main__':\n    _lib_exitcodes.run(main)\n")
    assert _guard_fault(ast.parse(src)) is None
    flipped = src.replace("def main(argv)", "def tmp(argv)").replace(
        "def main():", "def main(argv):")
    assert _guard_fault(ast.parse(flipped)) == (3, _entry_signature_fault(
        ast.parse(flipped).body[1]))


# ── #3579: every `__main__` under the tool trees, not only da-tools ─────────

#: The trees whose `if __name__ == "__main__":` modules must enter through
#: run() (#3579, owner 2026-11-05). Repo-relative POSIX prefixes.
GATED_TREES = ("scripts/tools/", "scripts/ops/", "scripts/dx/", "scripts/session-guards/")

#: ⛔ The ceiling of entries not yet through run() (#3579): remove a line when
#: its tool migrates, and lower the count in
#: test_the_pending_entries_are_a_pinned_ceiling with it. An addition here is
#: a new tool outside the one exit and needs its own ticket first — a new
#: tool enters through run() from its first commit.
PENDING: frozenset[str] = frozenset({
    "scripts/dx/generate_adr_index.py",
    "scripts/dx/generate_planning_index.py",
    "scripts/ops/add_t_parallel.py",
    "scripts/ops/audit_rules_drift.py",
    "scripts/ops/bench_synth_tenants.py",
    "scripts/ops/check_image_refs_resolve.py",
    "scripts/ops/ci_flake_retry.py",
    "scripts/ops/commit_helper.py",
    "scripts/ops/configmap_assemble.py",
    "scripts/ops/dx_run.py",
    "scripts/ops/file_race_report.py",
    "scripts/ops/fuse_plumbing_commit.py",
    "scripts/ops/go_test_reads.py",
    "scripts/ops/guard_defaults_scopes.py",
    "scripts/ops/inject_default_key.py",
    "scripts/ops/parse_go_test_json.py",
    "scripts/ops/plan_race_shards.py",
    "scripts/ops/route_metrics_through_field.py",
    "scripts/ops/use_get_metrics.py",
    "scripts/session-guards/paths_map.py",
    "scripts/session-guards/preflight_bash.py",
    "scripts/session-guards/session-init.py",
    "scripts/session-guards/skill_usage.py",
    "scripts/session-guards/stop_evidence.py",
    "scripts/session-guards/vscode_git_toggle.py",
    "scripts/tools/dx/add_frontmatter.py",
    "scripts/tools/dx/analyze_bench_history.py",
    "scripts/tools/dx/analyze_probe.py",
    "scripts/tools/dx/axe_lite_static.py",
    "scripts/tools/dx/bench_verdict.py",
    "scripts/tools/dx/bump_docs.py",
    "scripts/tools/dx/bump_playbook_versions.py",
    "scripts/tools/dx/check_aria_references.py",
    "scripts/tools/dx/compile_custom_alerts.py",
    "scripts/tools/dx/converge_status.py",
    "scripts/tools/dx/describe_tenant.py",
    "scripts/tools/dx/gen_agent_adapters.py",
    "scripts/tools/dx/gen_am_duration_json.py",
    "scripts/tools/dx/gen_recipe_contract.py",
    "scripts/tools/dx/gen_tenant_id_json.py",
    "scripts/tools/dx/generate_alert_reference.py",
    "scripts/tools/dx/generate_byo_rulepack_table.py",
    "scripts/tools/dx/generate_changelog.py",
    "scripts/tools/dx/generate_crd_schemas.py",
    "scripts/tools/dx/generate_doc_map.py",
    "scripts/tools/dx/generate_nav.py",
    "scripts/tools/dx/generate_rule_pack_readme.py",
    "scripts/tools/dx/generate_rulepack_configmaps.py",
    "scripts/tools/dx/generate_tenant_fixture.py",
    "scripts/tools/dx/generate_tool_map.py",
    "scripts/tools/dx/measure_judgement_budget.py",
    "scripts/tools/dx/migrate_conf_d.py",
    "scripts/tools/dx/migrate_ssot_language.py",
    "scripts/tools/dx/pair_bench_ratio.py",
    "scripts/tools/dx/paired_trend_watch.py",
    "scripts/tools/dx/pr_preflight.py",
    "scripts/tools/dx/render_soak_diff.py",
    "scripts/tools/dx/reword_chain.py",
    "scripts/tools/dx/run_chaos_soak.py",
    "scripts/tools/dx/scaffold_lint.py",
    "scripts/tools/dx/suggest_related.py",
    "scripts/tools/dx/sync_glossary_abbr.py",
    "scripts/tools/dx/sync_schema.py",
    "scripts/tools/dx/sync_tool_registry.py",
    "scripts/tools/lint/check_account_registry_monotonic.py",
    "scripts/tools/lint/check_ad_hoc_git_scripts.py",
    "scripts/tools/lint/check_admin_config_schema.py",
    "scripts/tools/lint/check_bat_ascii_purity.py",
    "scripts/tools/lint/check_bilingual_annotations.py",
    "scripts/tools/lint/check_changelog_no_tbd.py",
    "scripts/tools/lint/check_chart_package_contents.py",
    "scripts/tools/lint/check_chart_pull_surface.py",
    "scripts/tools/lint/check_chart_ship_surface.py",
    "scripts/tools/lint/check_codename_gate.py",
    "scripts/tools/lint/check_codename_leak.py",
    "scripts/tools/lint/check_confd_schema.py",
    "scripts/tools/lint/check_configmap_mount_completeness.py",
    "scripts/tools/lint/check_cross_ns_url_consistency.py",
    "scripts/tools/lint/check_design_token_usage.py",
    "scripts/tools/lint/check_dev_bypass_manifest.py",
    "scripts/tools/lint/check_dev_rules_enforcement.py",
    "scripts/tools/lint/check_devrules_size.py",
    "scripts/tools/lint/check_dist_source_consistency.py",
    "scripts/tools/lint/check_doc_links.py",
    "scripts/tools/lint/check_env_bool_parsers.py",
    "scripts/tools/lint/check_flaky_registry.py",
    "scripts/tools/lint/check_go_fmt.py",
    "scripts/tools/lint/check_ha_threshold_aggregation.py",
    "scripts/tools/lint/check_hardcode_tenant.py",
    "scripts/tools/lint/check_head_blob_hygiene.py",
    "scripts/tools/lint/check_helm_values_secrets.py",
    "scripts/tools/lint/check_hub_badge_drift.py",
    "scripts/tools/lint/check_iac_helm.py",
    "scripts/tools/lint/check_iac_vibe_rules.py",
    "scripts/tools/lint/check_image_pin_capability.py",
    "scripts/tools/lint/check_includes_sync.py",
    "scripts/tools/lint/check_init_chart_pin_published.py",
    "scripts/tools/lint/check_jsx_loader_compat.py",
    "scripts/tools/lint/check_k8s_manifests.py",
    "scripts/tools/lint/check_ksm_version_allowlist.py",
    "scripts/tools/lint/check_leftouterjoin_enrichment.py",
    "scripts/tools/lint/check_lint_toolchain_fit.py",
    "scripts/tools/lint/check_maintenance_symmetry.py",
    "scripts/tools/lint/check_md_yaml_drift.py",
    "scripts/tools/lint/check_metric_group_pairs.py",
    "scripts/tools/lint/check_open_encoding.py",
    "scripts/tools/lint/check_orphan_docs.py",
    "scripts/tools/lint/check_orphan_lint.py",
    "scripts/tools/lint/check_orphan_recordings.py",
    "scripts/tools/lint/check_path_metadata_consistency.py",
    "scripts/tools/lint/check_pint.py",
    "scripts/tools/lint/check_planning_status_sync.py",
    "scripts/tools/lint/check_playbook_freshness.py",
    "scripts/tools/lint/check_playwright_rtl_drift.py",
    "scripts/tools/lint/check_portal_asset_shipping.py",
    "scripts/tools/lint/check_portal_audience_enum.py",
    "scripts/tools/lint/check_pr_scope_drift.py",
    "scripts/tools/lint/check_repo_name.py",
    "scripts/tools/lint/check_routing_profiles.py",
    "scripts/tools/lint/check_scrape_reachability.py",
    "scripts/tools/lint/check_session_guard_liveness.py",
    "scripts/tools/lint/check_single_writer_invariant.py",
    "scripts/tools/lint/check_skip_a11y_justification.py",
    "scripts/tools/lint/check_structure.py",
    "scripts/tools/lint/check_subprocess_timeout.py",
    "scripts/tools/lint/check_threshold_observed_map.py",
    "scripts/tools/lint/check_threshold_reachability.py",
    "scripts/tools/lint/check_threshold_registry.py",
    "scripts/tools/lint/check_tool_registry_jsx_parity.py",
    "scripts/tools/lint/check_undefined_tokens.py",
    "scripts/tools/lint/check_unpinned_deps.py",
    "scripts/tools/lint/check_vmalert_coverage.py",
    "scripts/tools/lint/check_window_x_no_fallback.py",
    "scripts/tools/lint/check_workflow_git_push_permissions.py",
    "scripts/tools/lint/check_workload_closure_drift.py",
    "scripts/tools/lint/detect_sed_damage.py",
    "scripts/tools/lint/fix_file_hygiene.py",
    "scripts/tools/lint/lint_html_doc_links.py",
    "scripts/tools/lint/lint_jsx_babel.py",
    "scripts/tools/lint/trufflehog_to_sarif.py",
    "scripts/tools/lint/validate_mermaid.py",
    "scripts/tools/lint/validate_planning_session_row.py",
    "scripts/tools/ops/_federation_revocation_reconciler.py",
    "scripts/tools/ops/da_assembler.py",
    "scripts/tools/ops/generate_tenant_mapping_rules.py",
    "scripts/tools/ops/inject_metadata_join.py",
})


def _gated_sources() -> dict[str, str]:
    """repo-relative path -> source of every `.py` under GATED_TREES that has
    a module-level `__main__` guard. The population is the tree itself
    (`git ls-files`, tests/_tree.py), not a list kept here."""
    from _tree import REPO_ROOT as TREE_ROOT, repo_files  # noqa: E402
    out = {}
    for path in repo_files(".py"):
        rel = path.relative_to(TREE_ROOT).as_posix()
        if not rel.startswith(GATED_TREES):
            continue
        source = path.read_text(encoding="utf-8")
        if "__main__" not in source:
            continue
        tree = ast.parse(source, filename=rel)  # a file that cannot parse fails here
        if any(_is_main_guard(n) for n in tree.body):
            out[rel] = source
    return out


def _gate_faults(sources: dict[str, str], pending: frozenset[str]) -> list[str]:
    """Every fault of the gate over *sources* (path -> source) with *pending*
    as the ceiling. A pure function, so its answers can be pinned on shapes
    whose answer is known (test_gate_faults_on_known_shapes)."""
    faults = []
    for rel in sorted(pending - set(sources)):
        faults.append(f"{rel}: in PENDING but not an entry point under the gated "
                      "trees (gone, moved, or no `__main__` guard) — drop it from PENDING")
    for rel, source in sorted(sources.items()):
        fault = _guard_fault(ast.parse(source, filename=rel))
        if fault is None and rel in pending:
            faults.append(f"{rel}: enters through run() now — drop it from PENDING "
                          "and lower the pinned count")
        elif fault is not None and rel not in pending:
            faults.append(f"{rel}:{fault[0]}: {fault[1]}")
    return faults


def test_every_tool_entry_enters_through_run():
    """#3579 (owner, 2026-11-05): every `__main__` under scripts/{tools,ops,
    dx,session-guards} ends through `_lib_exitcodes.run`, with an entry run()
    can call. The ones not migrated yet are PENDING, a ceiling that only
    shrinks."""
    sources = _gated_sources()
    # anti-vacuity: each gated tree is in the population, and so is every
    # da-tools script (the narrower gate above is a subset of this one).
    for tree in GATED_TREES:
        assert any(rel.startswith(tree) for rel in sources), (tree, len(sources))
    in_cli = {p.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
              for p in _da_tools_scripts().values()}
    assert in_cli <= set(sources), sorted(in_cli - set(sources))
    assert len(sources) >= 200, len(sources)
    faults = _gate_faults(sources, PENDING)
    assert not faults, "\n".join(faults)


def test_the_pending_entries_are_a_pinned_ceiling():
    """The count is written out so that a shorter or longer PENDING is a
    visible edit of this line too (#3579): only ever lower it."""
    assert len(PENDING) == 146


_RUN_TOOL = ("import _lib_exitcodes\ndef main(argv=None):\n    return 0\n"
             "if __name__ == '__main__':\n    _lib_exitcodes.run(main)\n")
_BARE_TOOL = ("import sys\ndef main():\n    return 0\n"
              "if __name__ == '__main__':\n    sys.exit(main())\n")
_REQUIRED_ARG_TOOL = _RUN_TOOL.replace("main(argv=None)", "main(argv)")


@pytest.mark.parametrize("sources, pending, want", [
    ({"a.py": _RUN_TOOL}, frozenset(), []),
    ({"a.py": _BARE_TOOL}, frozenset({"a.py"}), []),
    ({"a.py": _BARE_TOOL}, frozenset(),
     ["a.py:5: the guard must end with `_lib_exitcodes.run(<entry>)`, got `sys.exit(main())`"]),
    ({"a.py": _RUN_TOOL}, frozenset({"a.py"}),
     ["a.py: enters through run() now — drop it from PENDING and lower the pinned count"]),
    ({}, frozenset({"gone.py"}),
     ["gone.py: in PENDING but not an entry point under the gated trees (gone, moved, "
      "or no `__main__` guard) — drop it from PENDING"]),
    ({"a.py": _REQUIRED_ARG_TOOL}, frozenset(),
     ["a.py:2: `main` takes argv without a default, but run() calls it with no "
      "arguments — give every parameter a default, or hand run() a no-argument wrapper"]),
], ids=["compliant", "pending", "new-bare", "pending-but-compliant", "pending-gone",
        "required-arg"])
def test_gate_faults_on_known_shapes(sources, pending, want):
    assert _gate_faults(sources, pending) == want


@pytest.mark.parametrize("kind", ["ast", "text", "yaml", "meta", "freshness"])
def test_a_scaffolded_lint_enters_through_run(kind):
    """#3579: `scaffold_lint.py` writes new lints into scripts/tools/lint, a
    gated tree — what it generates must pass the gate from its first commit."""
    import scaffold_lint as sl  # noqa: E402  (path set by conftest.py)
    assert kind in sl.VALID_KINDS
    source = sl.render_script(sl.derive_paths("probe", kind), "probe")
    assert _guard_fault(ast.parse(source)) is None


# ═══════════════════════════════════════════════════════════════════════════
# #3558 — closed standard fds are filled at import (sanitize_std_fds)
# ═══════════════════════════════════════════════════════════════════════════
def _run_with_fds_closed(script: Path, *, close: str,
                         env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """`tool >&-` / `tool 2>&-` / `tool <&-`: the shell closes the fd before
    Python starts, so the interpreter leaves `sys.stdout` (or `sys.stderr`)
    None — until `_lib_exitcodes` is imported."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not on PATH")
    return subprocess.run(
        [bash, "-c", f'exec "$0" "$1" {close}', sys.executable, str(script)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        env=_ENV if env is None else env)


# (body, its code with every fd open, whether it writes stdout)
_CLOSED_FD_ENDINGS = {
    "printed-exit0": ("print('report')\nreturn 0\n", 0, True),
    "printed-exit1": ("print('report')\nreturn 1\n", 1, True),
    "report": ("return _lib_exitcodes.Report({'n': 1})\n", 0, True),
    "report-status-1": ("return _lib_exitcodes.Report({'n': 1}, 1)\n", 1, True),
    "big-report": ("return _lib_exitcodes.Report({'x': 'y' * (io.DEFAULT_BUFFER_SIZE * 4)})\n",
                   0, True),
    "silent-exit0": ("print('note', file=sys.stderr)\nreturn 0\n", 0, False),
    "die_caller_error": ("_lib_exitcodes.die_caller_error('ERROR: bad input')\n", 2, False),
    "crash": ("raise RuntimeError('boom')\n", 2, False),
}


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
@pytest.mark.parametrize("close", [">&-", "<&- >&-", "2>&-", "<&-"])
@pytest.mark.parametrize("ending", sorted(_CLOSED_FD_ENDINGS))
@pytest.mark.real_crash_exit_code
def test_a_closed_std_fd_at_start(tmp_path, ending, close, buffering):
    """`>&-`: a tool that writes stdout ends at 2 with the named line (it was
    0 before #3558: the output was dropped); one that writes nothing keeps
    its code. `2>&-` / `<&-`: the tool's code and its stdout as with every fd
    open — no stderr line on stdout."""
    body, code, writes = _CLOSED_FD_ENDINGS[ending]
    proc = _run_with_fds_closed(_script(tmp_path, _armed(body)), close=close,
                                env=_BUFFERING[buffering])
    err = proc.stderr.decode("utf-8", "replace")
    where = f"{ending} × {close} × {buffering}: rc={proc.returncode}\n{err[-1200:]}"
    stdout_closed = ">&-" in close.split()
    assert "Exception ignored" not in err, where
    if stdout_closed and writes:
        assert proc.returncode == 2, where
        assert err.count(STDOUT_CLOSED_LINE + " Bad file descriptor") == 1, where
    else:
        assert proc.returncode == code, where
        assert STDOUT_CLOSED_LINE not in err, where
    if not stdout_closed:
        with_all = _run(_script(tmp_path, _armed(body)), env=_BUFFERING[buffering])
        assert proc.stdout == with_all.stdout, where
    if close == "2>&-":
        assert b"note" not in proc.stdout and b"ERROR" not in proc.stdout, where


_FD_PROBE = """
import fcntl, subprocess, tempfile
def mode(fd):
    acc = fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE
    return {os.O_RDONLY: 'r', os.O_WRONLY: 'w', os.O_RDWR: 'rw'}[acc]
seen = {}
for fd in (0, 1, 2):
    seen[fd] = (os.path.realpath(f'/proc/self/fd/{fd}'), mode(fd), os.get_inheritable(fd))
with tempfile.TemporaryFile() as f:
    seen['open'] = f.fileno()
child = subprocess.run([sys.executable, '-c', 'import os; os.write(1, b"x")'],
                       stderr=subprocess.DEVNULL)
seen['child'] = child.returncode
os.write(3, repr(seen).encode())
"""


@pytest.mark.parametrize("close", [">&-", "<&- >&- 2>&-"])
def test_a_closed_fd_gets_the_null_device_read_only_and_inheritable(tmp_path, close):
    """git `sanitize_stdfds` / Rust std: each closed fd 0..2 holds the null
    device, read-only (a write fails, it does not vanish), inheritable (a
    child sees the same), so a later `open()` is never handed fd 1 and a
    child writing its inherited stdout fails."""
    if not os.path.exists("/proc/self/fd"):
        pytest.skip("no /proc/self/fd here")
    probe = _script(tmp_path, _FD_PROBE)
    r, w = os.pipe()
    bash = shutil.which("bash") or pytest.skip("bash is not on PATH")
    proc = subprocess.run([bash, "-c", f'exec "$0" "$1" {close} 3>&{w}', sys.executable, str(probe)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
                          env=_ENV, pass_fds=(w,))
    os.close(w)
    with os.fdopen(r, "rb") as reader:
        seen = eval(reader.read().decode())  # noqa: S307 — our own repr
    closed = {int(c[0]) if c[0].isdigit() else (1 if c.startswith(">") else 0)
              for c in close.split()}
    for fd in closed:
        assert seen[fd] == (os.devnull, "r", True), (fd, seen, proc.stderr)
    assert seen["open"] not in (0, 1, 2), seen
    assert seen["child"] != 0, seen


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
@pytest.mark.parametrize("argv", [["--version"], ["--help"]])
def test_the_entrypoints_own_output_to_a_closed_stdout_is_rc_2(argv, buffering):
    """`da-tools --version >&-` / `--help >&-`: the dispatcher's own output
    fails like a tool's — rc 2 and the named line, never 0 or 120."""
    bash = shutil.which("bash") or pytest.skip("bash is not on PATH")
    entry = REPO_ROOT / "components" / "da-tools" / "app" / "entrypoint.py"
    proc = subprocess.run([bash, "-c", 'exec "$0" "$@" >&-', sys.executable, str(entry), *argv],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
                          env=_BUFFERING[buffering])
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 2, err
    assert err.count(STDOUT_CLOSED_LINE + " Bad file descriptor") == 1, err
    assert "Exception ignored" not in err and "Traceback" not in err, err


def test_a_full_device_is_named_as_a_write_error(tmp_path):
    """`tool > /dev/full`: the exit flush fails with ENOSPC — the reader did
    not close anything, so the line names the error, not a closed stdout."""
    if not os.path.exists("/dev/full"):
        pytest.skip("/dev/full does not exist here")
    with open("/dev/full", "wb") as full:
        proc = _run(_script(tmp_path, "print('partial report')\nsys.exit(0)\n"), stdout=full.fileno())
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 2, err
    assert err.count(STDOUT_CLOSED_LINE) == 1, err


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
@pytest.mark.parametrize("exit_fn", ["die_caller_error", "exit_caller_error"])
def test_a_lost_stdout_caught_by_the_tools_own_handler(tmp_path, exit_fn, buffering):
    """A tool's own `except Exception` (config_diff, operator_generate) turns
    the BrokenPipeError of a lost stdout into its named caller error: rc 2,
    not a crash. Which line names it is not the contract any more (#3558:
    the exception chain is not followed — dev-rules §13, known limits)."""
    body = _armed(
        "try:\n"
        "    print('x' * 1_000_000)\n"
        "except Exception as exc:\n"
        f"    _lib_exitcodes.{exit_fn}(f'ERROR: report failed: {{exc}}', reason='compare_failed')\n")
    rc, err = _run_closed(tmp_path, body, "reader-gone", _BUFFERING[buffering])
    assert rc == 2, err
    assert CRASH_MARK not in err and "Traceback" not in err, err


def test_or_die_writers_follow_the_caller_error_code(tmp_path, monkeypatch):
    """`write_*_or_die` / `ensure_dir_or_die` default to the process
    caller-error code, like every other caller-error exit."""
    sys.path.insert(0, str(TOOLS_DIR))
    import _lib_exitcodes as ec  # noqa: E402
    import _lib_io  # noqa: E402
    monkeypatch.setattr(ec, "_caller_error_code", 1)
    for call in (lambda: _lib_io.write_text_or_die(str(tmp_path / "absent" / "f"), "x"),
                 lambda: _lib_io.write_json_or_die(str(tmp_path / "absent" / "f"), {}),
                 lambda: _lib_io.ensure_dir_or_die(tmp_path / "file" / "sub")):
        (tmp_path / "file").write_text("", encoding="utf-8")
        with pytest.raises(SystemExit) as exc_info:
            call()
        assert exc_info.value.code == 1


# ── stdout writes the tap cannot see ───────────────────────────────────


def test_an_unarmed_caller_error_with_stderr_closed_keeps_stdout_empty(tmp_path):
    """Not --json, `2>&-`: die_caller_error's line must not fall back to
    stdout (print(file=None) would print it there)."""
    proc = _run_with_fds_closed(
        _script(tmp_path, "_lib_exitcodes.die_caller_error('ERROR: bad input')\n"), close="2>&-")
    assert (proc.returncode, proc.stdout) == (2, b"")


# ═══════════════════════════════════════════════════════════════════════════
# Round 3: "did stdout fail?" is observed (the stdout tap records the
# failure), never inferred from an errno or from polling another stream
# ═══════════════════════════════════════════════════════════════════════════
_SOCKET_EPIPE = ("import socket\n"
                 "a, b = socket.socketpair()\nb.close()\n"
                 "a.sendall(b'x' * 1_000_000)\n")


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
def test_a_socket_epipe_while_the_reader_is_gone_keeps_its_own_reason(tmp_path, buffering):
    """S1: stdout's reader has left AND the tool's own socket raises EPIPE,
    which its handler names. Same errno, not stdout's failure: the tool's
    reason stands. (The tool writes nothing to stdout here — a write would
    fail first, unbuffered, and that one IS stdout's.)"""
    body = _armed(
        "try:\n" + "".join("    " + ln + "\n" for ln in _SOCKET_EPIPE.splitlines()) +
        "except OSError:\n"
        "    _lib_exitcodes.die_caller_error('ERROR: api unreachable', reason='api_unreachable')\n")
    rc, err = _run_closed(tmp_path, body, "reader-gone", _BUFFERING[buffering])
    assert rc == 2, err
    assert "ERROR: api unreachable" in err, err


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
@pytest.mark.real_crash_exit_code  # a crash line ends at the production crash code, 2
def test_a_socket_epipe_crash_while_the_reader_is_gone_is_rc_2(tmp_path, buffering):
    """A socket's EPIPE while stdout's reader has also left: rc 2 either way.
    The library asks stdout (POLLERR) and finds it gone, so the line it
    prints is the stdout one — the documented "which line" limit (#3558),
    not a different exit code."""
    body = _armed(_SOCKET_EPIPE)
    rc, err = _run_closed(tmp_path, body, "reader-gone", _BUFFERING[buffering])
    _said_and_failed(rc, err)


def test_another_fd_ebadf_while_stdout_is_closed_is_a_crash(tmp_path):
    body = _armed("os.write(99, b'x')\n")
    rc, err = _run_closed(tmp_path, body, "reader-gone", _UNBUFFERED_ENV)
    assert f"{CRASH_MARK} (OSError)" in err, err


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
@pytest.mark.parametrize("how", ["uncaught", "handled"])
@pytest.mark.real_crash_exit_code  # a crash line ends at the production crash code, 2
def test_a_full_device_mid_run_is_a_write_error_not_a_crash(tmp_path, how, buffering):
    """S2: more than a buffer's worth into /dev/full fails mid-run (ENOSPC):
    the caller-error code. Uncaught, the ENOSPC raised by the write is not
    tied to stdout once the bytes are dropped (#3558: the documented "which
    line" limit) — rc 2 all the same; handled, the tool's own line."""
    if not os.path.exists("/dev/full"):
        pytest.skip("/dev/full does not exist here")
    work = "print('x' * 1_000_000)\n"
    if how == "handled":
        work = ("try:\n    print('x' * 1_000_000)\nexcept OSError as e:\n"
                "    _lib_exitcodes.die_caller_error(f'ERROR: {e}', reason='r')\n")
    with open("/dev/full", "wb") as full:
        proc = _run(_script(tmp_path, _armed(work)), stdout=full.fileno(),
                    env=_BUFFERING[buffering])
    err = proc.stderr.decode("utf-8", "replace")
    if how == "handled":
        assert proc.returncode == 2, err
        assert "ERROR: [Errno 28] No space left on device" in err, err
        assert "Traceback" not in err and CRASH_MARK not in err, err
    else:
        _said_and_failed(proc.returncode, err)


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
def test_a_broken_stderr_under_redirect_stdout_is_not_a_lost_stdout(tmp_path, buffering):
    """S3: inside `redirect_stdout(sys.stderr)` the progress lines go to
    stderr, whose reader left. stdout is healthy and the tool's caller
    error stands: rc 2, nothing on stdout (#3558: no envelope)."""
    script = _script(tmp_path, _armed(
        "import contextlib\n"
        "with contextlib.redirect_stdout(sys.stderr):\n"
        "    try:\n"
        "        for i in range(100000):\n"
        "            print('progress', i)\n"
        "        sys.stdout.flush()\n"
        "    except OSError as e:\n"
        "        _lib_exitcodes.die_caller_error(f'ERROR: progress failed: {e}', reason='progress_failed')\n"))
    r, w = os.pipe()
    os.close(r)
    try:
        proc = subprocess.run([sys.executable, str(script)], stdout=subprocess.PIPE,
                              stderr=w, timeout=60, env=_BUFFERING[buffering])
    finally:
        os.close(w)
    assert proc.returncode == 2  # not 120: a broken stderr never changes the code
    assert proc.stdout == b"", proc.stdout


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
@pytest.mark.parametrize("size", ["small", "big"])
def test_the_lost_stdout_exit_honours_an_explicit_code(tmp_path, size, buffering):
    """N1: `die_caller_error(..., code=7)` from a stdout-loss handler: never
    0. The tool caught stdout's failure itself, so what decides is the last
    flush (gnulib ``close_stdout``, #3558): bytes still buffered for the
    broken stdout fail it again — rc 2 and the stdout line; none left (the
    big write took them down with it) — the tool's 7 stands."""
    n = 100 if size == "small" else 1_000_000
    body = _armed(f"try:\n    print('x' * {n})\n    sys.stdout.flush()\nexcept OSError:\n"
                  "    _lib_exitcodes.die_caller_error('ERROR: x', reason='r', code=7)\n")
    rc, err = _run_closed(tmp_path, body, "reader-gone", _BUFFERING[buffering])
    assert "ERROR: x" in err, err
    if size == "small" and buffering == "buffered":
        assert rc == 2, err
        assert err.count(STDOUT_CLOSED_LINE) == 1, err
    else:
        assert rc == 7, err


def test_the_final_flush_is_of_the_original_stdout_not_a_rebound_one(tmp_path):
    """S3 at exit: `sys.stdout` is still bound to stderr when the process
    ends, and the ORIGINAL stdout's reader has left with output pending. The
    original stdout is the one flushed (else the interpreter's own flush
    makes it 120), and the line naming its failure goes to stderr."""
    body = "print('partial report')\nsys.stdout = sys.stderr\nsys.exit(0)\n"
    rc, err = _run_closed(tmp_path, body, "reader-gone")
    assert rc == 2, err
    assert STDOUT_CLOSED_LINE in err, err


# ═══════════════════════════════════════════════════════════════════════════
# Round 4: no stale exit state; only an EXPLICIT cause chain counts
# ═══════════════════════════════════════════════════════════════════════════
_BROKEN_STREAM = (
    "import io, errno\n"
    "class Broken(io.TextIOBase):\n"
    "    def writable(self):\n        return True\n"
    "    def write(self, s):\n        raise OSError(errno.EPIPE, 'Broken pipe')\n"
    "    def flush(self):\n        raise OSError(errno.EPIPE, 'Broken pipe')\n")


_STATE_PROBE = "DA_EXIT_STATE_PROBE"


@pytest.mark.skipif(not os.environ.get(_STATE_PROBE), reason="driven by the test below")
def test_probe_leaves_every_piece_of_exit_state_dirty(monkeypatch):
    sys.path.insert(0, str(TOOLS_DIR))
    import _lib_exitcodes as ec  # noqa: E402
    ec.set_crash_exit_code(70)
    ec.set_caller_error_code(1)
    ec.claim_stdout_owner("probe")
    ec._failure_reported = True          # a stdout failure said this run
    ec._best_effort = True               # a best-effort Report written
    ec._stdout_failure_line = lambda d: "ERROR [X]: " + d


@pytest.mark.skipif(not os.environ.get(_STATE_PROBE), reason="driven by the test below")
def test_probe_sees_clean_exit_state():
    sys.path.insert(0, str(TOOLS_DIR))
    import _lib_exitcodes as ec  # noqa: E402
    assert (ec._failure_reported, ec._best_effort, ec._stdout_failure_line,
            ec._stdout_owner, ec.get_crash_exit_code(),
            ec.get_caller_error_code()) == (False, False, None, None,
                                            ec._IMPORT_CRASH_EXIT_CODE,
                                            ec._IMPORT_CALLER_ERROR_CODE)


def test_exit_state_does_not_leak_between_tests():
    """M1/S2: no test may see another's codes, its said stdout failure (it
    would silence the next one's line), its best-effort delivery (it would
    keep a status a failed stdout must replace) or its stdout owner."""
    me = Path(__file__)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
         f"{me}::test_probe_leaves_every_piece_of_exit_state_dirty",
         f"{me}::test_probe_sees_clean_exit_state"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300, cwd=str(REPO_ROOT), env=dict(os.environ, **{_STATE_PROBE: "1"}))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0 and "2 passed" in proc.stdout, out[-2000:]


# A small write stays in the buffer after the failed flush, so the exit
# flush would fail again; a big one fails inside write() and leaves none.
_SIZES = {"small": 100, "big": 1_000_000}


@pytest.mark.parametrize("size", sorted(_SIZES))
@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
def test_a_bug_in_a_broken_pipe_handler_is_still_a_crash(tmp_path, buffering, size):
    """S1 (p1): the handler for stdout's BrokenPipeError has a bug of its
    own: a crash, reported as one. Its code is the tool's crash code — unless
    bytes are still buffered for the broken stdout (small, buffered), whose
    last flush then fails too: rc 2 and the stdout line after the crash line
    (#3558, gnulib ``close_stdout``: stdout's failure is never forgiven)."""
    body = ("_lib_exitcodes.set_crash_exit_code(70)\n"
            "def main():\n"
            f"    try:\n        print('x' * {_SIZES[size]})\n        sys.stdout.flush()\n"
            "    except BrokenPipeError:\n        cleanup = None\n        cleanup()\n"
            "    return 0\n_lib_exitcodes.run(main)\n")
    rc, err = _run_closed(tmp_path, body, "reader-gone", _BUFFERING[buffering])
    assert f"{CRASH_MARK} (TypeError)" in err and "Traceback" in err, err
    if size == "small" and buffering == "buffered":
        assert rc == 2, err
        assert err.count(STDOUT_CLOSED_LINE) == 1, err
    else:
        assert rc == 70, err
        assert STDOUT_CLOSED_LINE not in err, err


@pytest.mark.parametrize("size", sorted(_SIZES))
@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
def test_a_tools_own_error_inside_a_broken_pipe_handler_keeps_its_line_and_code(
        tmp_path, buffering, size):
    """S1 (p2): after stdout's failure the tool hits ANOTHER error and names
    it with its own code: its line stands, and its code — unless bytes are
    still buffered for the broken stdout (small, buffered): the last flush
    fails too, rc 2 (#3558, gnulib ``close_stdout``)."""
    body = ("def main():\n"
            f"    try:\n        print('x' * {_SIZES[size]})\n        sys.stdout.flush()\n"
            "    except BrokenPipeError:\n"
            "        try:\n            open('/nonexistent/d/f', 'w')\n"
            "        except OSError as e:\n"
            "            _lib_exitcodes.die_caller_error(f'ERROR: cannot write --output: {e}', "
            "reason='output_unwritable', code=5)\n"
            "    return 0\n_lib_exitcodes.run(main)\n")
    rc, err = _run_closed(tmp_path, body, "reader-gone", _BUFFERING[buffering])
    assert "ERROR: cannot write --output" in err, err
    if size == "small" and buffering == "buffered":
        assert rc == 2, err
        assert err.count(STDOUT_CLOSED_LINE) == 1, err
    else:
        assert rc == 5, err
        assert STDOUT_CLOSED_LINE not in err, err


def test_reset_restores_the_codes_this_module_was_imported_with(monkeypatch):
    """Not a hard-coded 2: a process whose import set other codes (#3092's
    crash code under tests) gets those back."""
    sys.path.insert(0, str(TOOLS_DIR))
    import _lib_exitcodes as ec  # noqa: E402
    monkeypatch.setattr(ec, "_IMPORT_CRASH_EXIT_CODE", 70)
    monkeypatch.setattr(ec, "_IMPORT_CALLER_ERROR_CODE", 3)
    ec.set_crash_exit_code(9)
    ec.set_caller_error_code(9)
    ec.reset_exit_state()
    assert (ec.get_crash_exit_code(), ec.get_caller_error_code()) == (70, 3)


_CALLER_EXITS = {
    "die_caller_error": "_lib_exitcodes.die_caller_error('ERROR: bad input', reason='r')\n",
    "hand-written": "print('ERROR: bad input', file=sys.stderr)\nreturn 2\n",
}


@pytest.mark.parametrize("how", sorted(_CALLER_EXITS))
def test_an_fd_less_stdout_is_not_a_stdout_failure(how):
    """A stdout with no fd (a host swapped in StringIO): `fileno()` raises
    io.UnsupportedOperation, an OSError — that is not stdout failing: only
    the tool's own line, no named stdout line, the tool's code."""
    body = ("import io\nsys.stdout = io.StringIO()\n"
            + _armed(_CALLER_EXITS[how]))
    code = f"import sys\nsys.path.insert(0, {str(TOOLS_DIR)!r})\nimport _lib_exitcodes\n" + body
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=60, env={**_ENV, "PYTHONIOENCODING": "utf-8"})
    assert proc.returncode == 2, proc.stderr
    assert "ERROR: bad input" in proc.stderr, proc.stderr
    assert "cannot write to stdout" not in proc.stderr, proc.stderr
    assert STDOUT_CLOSED_LINE not in proc.stderr, proc.stderr


@pytest.mark.parametrize("where", ["dev-full", "reader-gone"])
def test_a_real_tools_caller_error_writes_nothing_to_a_failing_stdout(tmp_path, where):
    """`onboard_platform --json` with no inputs: a caller error, so nothing
    is written to stdout (#3558: there used to be an envelope) — and a
    stdout that cannot take anything is then never asked: its own line,
    rc 2, no stdout line."""
    if where == "dev-full" and not os.path.exists("/dev/full"):
        pytest.skip("/dev/full does not exist here")
    argv = [sys.executable, str(OPS_DIR / "onboard_platform.py"), "-o", str(tmp_path / "o"), "--json"]
    if where == "dev-full":
        with open("/dev/full", "wb") as full:
            proc = subprocess.run(argv, stdout=full.fileno(), stderr=subprocess.PIPE,
                                  timeout=60, env=_ENV)
    else:
        r, w = os.pipe()
        os.close(r)
        try:
            proc = subprocess.run(argv, stdout=w, stderr=subprocess.PIPE, timeout=60, env=_ENV)
        finally:
            os.close(w)
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 2, err
    assert "ERROR: At least one phase input is required." in err, err
    assert STDOUT_CLOSED_LINE not in err, err


# ═══════════════════════════════════════════════════════════════════════════
# #3156 — three edges of the stdout-failure handling
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
def test_a_still_closed_fd_1_keeps_the_tools_own_code(tmp_path, buffering):
    """#3156 (3), the other side: fd 1 was closed (by the tool, mid-run) when
    stdout failed and is still closed when the tool leaves with its own code
    (5). Buffered, the bytes still owed to stdout fail the last flush again:
    rc 2 and the stdout line (#3558, gnulib ``close_stdout``); unbuffered
    nothing is owed: the tool's 5."""
    body = ("os.close(1)\n"
            "def main():\n"
            "    try:\n        print('x' * 100)\n        sys.stdout.flush()\n"
            "    except OSError:\n"
            "        try:\n            open('/nonexistent/d/f', 'w')\n"
            "        except OSError as e:\n"
            "            _lib_exitcodes.die_caller_error(f'ERROR: cannot write --output: {e}', "
            "reason='output_unwritable', code=5)\n"
            "    return 0\n_lib_exitcodes.run(main)\n")
    proc = _run(_script(tmp_path, body), env=_BUFFERING[buffering])
    err = proc.stderr.decode("utf-8", "replace")
    assert "ERROR: cannot write --output" in err, err
    if buffering == "buffered":
        assert proc.returncode == 2, err
        assert err.count(STDOUT_CLOSED_LINE + " Bad file descriptor") == 1, err
    else:
        assert proc.returncode == 5, err
        assert STDOUT_CLOSED_LINE not in err, err


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
def test_a_caller_error_reaching_the_excepthook_after_a_broken_pipe_prints_one_line(
        tmp_path, buffering):
    """#3156 (2): a tool without ``run()`` raises ``CallerError`` while
    handling stdout's failure; it reaches the module excepthook, where
    ``sys.exc_info()`` is empty. The exception must be handed on explicitly,
    so it is not a crash: its own line, its caller-error code (3 here) — the
    stdout line may follow it (the last flush, #3558)."""
    body = ("_lib_exitcodes.set_caller_error_code(3)\n"
            "try:\n    print('x' * 100)\n    sys.stdout.flush()\n"
            "except BrokenPipeError:\n"
            "    raise _lib_exitcodes.CallerError('my own reason')\n")
    rc, err = _run_closed(tmp_path, body, "reader-gone", _BUFFERING[buffering])
    assert "ERROR: my own reason" in err, err
    assert rc == 3, err
    assert err.count(STDOUT_CLOSED_LINE) <= 1, err
    assert CRASH_MARK not in err, err


# #3558 drops the seal: a tool that closes or replaces fd 1 ITSELF after
# stdout failed, with bytes still buffered for the old stdout (buffered
# mode), gets them flushed into its own file at exit. No tool in the repo
# closes or dup2s fd 1; the case is kept as a strict xfail so a change in
# either direction is seen. Recorded on #3558.
_NO_SEAL = pytest.mark.xfail(strict=True, reason="#3558: no seal of a failed stdout; "
                                                 "a tool's own reuse of fd 1 gets stale bytes")


def _xfail_buffered(buffering: str):
    return [_NO_SEAL] if buffering == "buffered" else []


@pytest.mark.parametrize("buffering", [pytest.param(b, marks=_xfail_buffered(b))
                                       for b in sorted(_BUFFERING)])
def test_a_reused_fd_1_gets_neither_the_null_device_nor_stale_bytes(tmp_path, buffering):
    """#3156 (3): stdout failed while fd 1 was closed; the tool then opened a
    file, which got fd 1. Pointing "stdout's fd" at the null device now
    would discard that file's output, so no fd is touched (#3156 v2: the
    library writes to no fd 0/1/2 on exit). And the bytes still buffered for the failed
    stdout must not land in the tool's file either: it holds its own line
    only."""
    out = tmp_path / "out.txt"
    body = ("os.close(1)\n"
            "try:\n    print('old')\n    sys.stdout.flush()\n"
            "except OSError:\n"
            f"    f = open({str(out)!r}, 'w')\n"
            "    assert f.fileno() == 1, f.fileno()\n"
            "    f.write('mine\\n')\n"
            "    _lib_exitcodes.die_caller_error('ERROR: x')\n")
    proc = _run(_script(tmp_path, body), env=_BUFFERING[buffering])
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 2, err
    content = out.read_text(encoding="utf-8")
    assert "old" not in content, content
    assert content == "mine\n", err


@pytest.mark.parametrize("buffering", [pytest.param(b, marks=_xfail_buffered(b))
                                       for b in sorted(_BUFFERING)])
def test_a_file_dup2ed_onto_fd_1_after_a_broken_pipe_gets_no_stale_bytes(
        tmp_path, buffering):
    """#3156 (3), review F1: after stdout's EPIPE the tool ``dup2``-s a file of
    its own onto fd 1 and writes to it. No fd is redirected, and the bytes still buffered for the broken
    stdout are discarded, not flushed into that file at exit."""
    out = tmp_path / "out.txt"
    body = ("def main():\n"
            "    try:\n        print('old' * 40)\n        sys.stdout.flush()\n"
            "    except BrokenPipeError:\n"
            f"        fd = os.open({str(out)!r}, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)\n"
            "        os.dup2(fd, 1)\n        os.close(fd)\n"
            "        os.write(1, b'mine\\n')\n"
            "        _lib_exitcodes.die_caller_error('ERROR: mine', code=5)\n"
            "    return 0\n_lib_exitcodes.run(main)\n")
    rc, err = _run_closed(tmp_path, body, "reader-gone", _BUFFERING[buffering])
    assert rc == 5, err
    content = out.read_text(encoding="utf-8")
    assert "old" not in content, content
    assert content == "mine\n", err


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
def test_a_closed_stderr_and_a_broken_pipe_end_at_the_caller_error_code(tmp_path, buffering):
    """#3156, review S1: fd 2 closed before the run, then stdout's reader
    leaves. Before #3156 the null device opened for the redirect got fd 2
    (the lowest free one) and closing it after the dup2 closed fd 2 again,
    so the final flush failed and the process ended at the interpreter's
    120."""
    body = ("os.close(2)\n"
            "def main():\n    print('x' * 100)\n    sys.stdout.flush()\n    return 0\n"
            "_lib_exitcodes.run(main)\n")
    rc, _err = _run_closed(tmp_path, body, "reader-gone", _BUFFERING[buffering])
    assert rc == 2


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
def test_the_excepthook_runs_every_atexit_handler_before_leaving_a_failed_stdout(
        tmp_path, buffering):
    """#3156 v2, M5: a tool without ``run()`` crashes with output still owed to
    a stdout whose reader left. The excepthook settles stdout itself
    (``_finalize_and_exit``: names the failure, seals the stream) and hands
    the exit back, so ALL atexit handlers run — also one registered before
    this module was imported (coverage's own is one). Left to the
    import-time fallback, the failure would only be met at exit, where the
    fallback has to ``os._exit`` before that handler runs."""
    mark = tmp_path / "mark.txt"
    script = tmp_path / "tool.py"
    script.write_text(
        "import atexit, os, sys\n"
        f"atexit.register(lambda: open({str(mark)!r}, 'w', encoding='utf-8').write('ran'))\n"
        f"sys.path.insert(0, {str(TOOLS_DIR)!r})\n"
        "import _lib_exitcodes\n"
        "print('x' * 100)\n"
        "raise RuntimeError('boom')\n", encoding="utf-8")
    r, w = os.pipe()
    os.close(r)
    try:
        proc = _run(script, stdout=w, env=_BUFFERING[buffering])
    finally:
        os.close(w)
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 2, err
    assert err.count(STDOUT_CLOSED_LINE) == 1, err
    assert mark.read_text(encoding="utf-8") == "ran", err


def test_a_failed_stdout_still_waits_for_non_daemon_threads(tmp_path):
    """#3156 v2: on the failure path too, the exit goes back to the
    interpreter's shutdown, which waits for non-daemon threads — an
    ``os._exit`` would kill one mid-work."""
    mark = tmp_path / "mark.txt"
    body = ("import threading, time\n"
            "def late():\n    time.sleep(0.3)\n"
            f"    open({str(mark)!r}, 'w', encoding='utf-8').write('done')\n"
            "def main():\n    threading.Thread(target=late).start()\n"
            "    print('x' * 100)\n    return 0\n"
            "_lib_exitcodes.run(main)\n")
    rc, err = _run_closed(tmp_path, body, "reader-gone")
    assert rc == 2, err
    assert err.count(STDOUT_CLOSED_LINE) == 1, err
    assert mark.read_text(encoding="utf-8") == "done", err


def test_a_clean_exit_goes_back_to_the_interpreter(tmp_path):
    """#3156 v2 (variant B): when stdout and stderr flush cleanly, ``run()``
    hands the exit back as ``SystemExit`` instead of ``os._exit``. An
    embedding caller (``runpy`` + ``except SystemExit``) carries on, and a
    ``NamedTemporaryFile`` the tool still holds is deleted at shutdown."""
    tool = _script(tmp_path, (
        "import tempfile\n"
        "HELD = tempfile.NamedTemporaryFile(prefix='held-', dir={d!r}, delete=True)\n"
        "def main():\n    print('report')\n    return 3\n"
        "_lib_exitcodes.run(main)\n").format(d=str(tmp_path)))
    host = tmp_path / "host.py"
    host.write_text(
        "import runpy, sys\n"
        "try:\n"
        f"    runpy.run_path({str(tool)!r}, run_name='__main__')\n"
        "except SystemExit as done:\n"
        "    print('after', done.code)\n", encoding="utf-8")
    proc = _run(host)
    out = proc.stdout.decode("utf-8", "replace")
    assert proc.returncode == 0, proc.stderr
    assert out == "report\nafter 3\n", out
    assert not list(tmp_path.glob("held-*")), list(tmp_path.glob("held-*"))


# ── #3156 v2 review round: seal, never redirect ─────────────────────────
_LATE = "import time\ntime.sleep(0.3)\n"  # let `| true` leave first
# A write that cannot fit in the CHILD's stdout buffer, so a buffered stdout
# whose reader left fails at the write itself, not at the exit flush. Derived
# from the child's own io.DEFAULT_BUFFER_SIZE, never a literal: Python 3.14
# raised it from 8 KiB to 128 KiB, and the 100000-char literal this replaced
# then sat in the buffer — the two "caught exit" cases silently tested the
# exit-flush path instead and read rc 2.
_OVERFLOW = "'x' * (io.DEFAULT_BUFFER_SIZE * 4)"


def _run_reader_gone(tmp_path: Path, body: str, buffering: str = "buffered") -> tuple[int, str]:
    """*body* after the prelude, with a stdout whose reader has left;
    (rc, stderr)."""
    script = _script(tmp_path, body)
    r, w = os.pipe()
    os.close(r)
    try:
        proc = subprocess.run([sys.executable, str(script)], stdout=w,
                              stderr=subprocess.PIPE, timeout=60, env=_BUFFERING[buffering])
    finally:
        os.close(w)
    return proc.returncode, proc.stderr.decode("utf-8", "replace")


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
def test_a_caught_lib_exit_does_not_decide_the_final_code(tmp_path, buffering):
    """Review F1: a tool without ``run()`` catches the lib's exit (``except
    SystemExit``), carries on, and ends with its own verdict (1). The bytes
    went down with the failed big write, so the last flush has nothing to
    fail on: that verdict is the exit code — the caught exit decided
    nothing (never 0)."""
    body = (_LATE + "try:\n    try:\n        print(" + _OVERFLOW + ")\n"
            "    except OSError:\n        _lib_exitcodes.die_caller_error('ERROR: report failed', code=5)\n"
            "except SystemExit:\n    pass\nsys.exit(1)\n")
    rc, err = _run_reader_gone(tmp_path, body, buffering)
    assert rc == 1, err
    assert "ERROR: report failed" in err, err


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
@pytest.mark.parametrize("where", ["after-caught-exit", "atexit"])
def test_stdout_still_takes_writes_after_its_failure_was_handled(tmp_path, buffering, where):
    """Review F2: after stdout failed, ``sys.stdout`` still takes writes — a
    ``json.dump`` after a caught exit, or an atexit handler, is not an
    AttributeError crash. Whatever the tool then exits with, stdout did fail
    and nothing hides it: rc 2 (#3558: the failure is no longer "answered
    for" by the caught exit — that read as 0)."""
    if where == "atexit":
        body = ("import atexit\natexit.register(lambda: sys.stdout.write('summary\\n'))\n"
                + _LATE + "print(" + _OVERFLOW + ")\n")
    else:
        body = (_LATE + "import json\ntry:\n    try:\n        print(" + _OVERFLOW + ")\n"
                "    except OSError:\n        _lib_exitcodes.die_caller_error('ERROR: report failed')\n"
                "except SystemExit:\n    pass\njson.dump({'v': 1}, sys.stdout)\nsys.exit(0)\n")
    rc, err = _run_reader_gone(tmp_path, body, buffering)
    assert rc == 2, err
    assert "Exception ignored" not in err and "AttributeError" not in err, err


@pytest.mark.parametrize("how", ["swallowed", "atexit-after-run"])
def test_a_stderr_whose_reader_left_never_makes_it_120(tmp_path, how):
    """Review F3: stderr's reader left with a line still buffered. Without
    ``run()`` the tool swallowed the write error; with it, an atexit handler
    wrote after the clean exit was handed back. Either way stderr loses its
    lines and nothing else: the tool's own 0, not the interpreter's 120."""
    if how == "swallowed":
        body = (_LATE + "try:\n    sys.stderr.write('partial')\n    sys.stderr.flush()\n"
                "except BrokenPipeError:\n    pass\n")
    else:
        body = ("import atexit\ndef main():\n    atexit.register(lambda: sys.stderr.write('late'))\n"
                + "".join("    " + ln + "\n" for ln in _LATE.splitlines())
                + "    return 0\n_lib_exitcodes.run(main)\n")
    script = _script(tmp_path, body)
    r, w = os.pipe()
    os.close(r)
    try:
        proc = subprocess.run([sys.executable, str(script)], stdout=subprocess.DEVNULL,
                              stderr=w, timeout=60, env=_ENV)
    finally:
        os.close(w)
    assert proc.returncode == 0


@pytest.mark.parametrize("entry", ["run", "excepthook"])
def test_a_crash_with_stderr_gone_still_ends_at_the_crash_code(tmp_path, entry):
    """Review F3 (c): the crash report cannot be written (stderr's reader
    left; stdout is fine). The crash code (70) still says what happened —
    not the interpreter's 1 for an error in the report, nor 120."""
    crash = "raise ValueError('boom ' * 2000)"
    if entry == "run":
        body = ("_lib_exitcodes.set_crash_exit_code(70)\ndef main():\n"
                + "".join("    " + ln + "\n" for ln in _LATE.splitlines())
                + f"    print('out')\n    {crash}\n_lib_exitcodes.run(main)\n")
    else:
        body = "_lib_exitcodes.set_crash_exit_code(70)\n" + _LATE + f"print('out')\n{crash}\n"
    script = _script(tmp_path, body)
    r, w = os.pipe()
    os.close(r)
    try:
        proc = subprocess.run([sys.executable, str(script)], stdout=subprocess.DEVNULL,
                              stderr=w, timeout=60, env=_ENV)
    finally:
        os.close(w)
    assert proc.returncode == 70


@_NO_SEAL
def test_a_second_wrapper_on_a_failed_stdout_leaks_nothing_into_a_reused_fd(tmp_path):
    """Review F4: a second ``TextIOWrapper`` shares the failed stdout's
    buffer; fd 1 is then reused by the tool's log file. Sealing drops
    writes at every layer (not the fd), so neither wrapper flushes stale
    bytes into the log at exit."""
    log = tmp_path / "log.txt"
    body = ("import io\nkeep = []\ndef main():\n"
            "    w = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')\n    keep.append(w)\n"
            "    w.write('REPORT-VIA-W\\n')\n    print('stale stdout bytes')\n    os.close(1)\n"
            "    try:\n        sys.stdout.flush()\n    except OSError:\n"
            f"        f = open({str(log)!r}, 'w')\n        keep.append(f)\n"
            "        f.write('tool log line\\n')\n"
            "        raise _lib_exitcodes.CallerError('cannot write report')\n"
            "    return 0\n_lib_exitcodes.run(main)\n")
    proc = _run(_script(tmp_path, body))
    assert proc.returncode == 2, proc.stderr
    assert log.read_text(encoding="utf-8") == "tool log line\n"


def test_an_exit_status_beyond_a_c_long_is_handed_back_unmasked(tmp_path):
    """Review F5: ``run()`` hands the tool's status back as given — 2**70 ends
    the way the interpreter ends it (255), never masked to 0 (success)."""
    proc = _run(_script(tmp_path, "_lib_exitcodes.run(lambda: 2 ** 70)\n"))
    assert proc.returncode == 255, proc.stderr


_HELD = {
    "saved-reference": "print('late', file=SAVED)\n",
    "__stdout__": "print('late', file=sys.__stdout__)\nsys.__stdout__.flush()\n",
    "buffer": "sys.stdout.buffer.write(b'late')\n",
    "logging": "logging.getLogger().warning('late')\n",
    "redirect-restored": None,  # built below
}


# Documented limit (dev-rules §13, #3558): a write error something else
# swallows — here logging's own handleError — with nothing left in stdout's
# buffer for the last flush to fail on, after the tool caught the library's
# exit itself. Strict, so a change either way is seen.
_SWALLOWED = pytest.mark.xfail(strict=True, reason="#3558 known limit: a write error "
                                                   "swallowed with nothing left to flush")


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
@pytest.mark.parametrize("held", sorted(_HELD))
def test_code_still_holding_a_sealed_stdout_writes_to_nowhere(tmp_path, buffering, held, request):
    """Review 2, F1: after stdout's failure (the lib's exit caught), code
    still holding the ORIGINAL stream — a saved reference, ``sys.__stdout__``,
    its buffer, a logging handler, ``redirect_stdout`` restoring it — writes
    to it: no ValueError crash, no logging error. The exit is never 0: what
    stdout still owes fails the last flush (#3558 — the seal that made this
    the tool's own 0 is gone; a lost stdout no longer reads as success)."""
    prelude = ("import logging\nSAVED = sys.stdout\n"
               "logging.basicConfig(stream=sys.stdout)\n" + _LATE)
    handle = ("    try:\n        print(" + _OVERFLOW + ")\n        sys.stdout.flush()\n"
              "    except OSError:\n        _lib_exitcodes.die_caller_error('ERROR: report failed')\n")
    if held == "redirect-restored":
        body = (prelude + "import contextlib, io\ntry:\n"
                "    with contextlib.redirect_stdout(io.StringIO()):\n"
                "        try:\n            SAVED.write(" + _OVERFLOW + ")\n            SAVED.flush()\n"
                "        except OSError:\n"
                "            _lib_exitcodes.die_caller_error('ERROR: report failed')\n"
                "except SystemExit:\n    pass\nprint('summary')\nsys.exit(0)\n")
    else:
        body = (prelude + "try:\n" + handle + "except SystemExit:\n    pass\n"
                + _HELD[held] + "sys.exit(0)\n")
    if (held, buffering) == ("logging", "unbuffered"):
        request.node.add_marker(_SWALLOWED)
    rc, err = _run_reader_gone(tmp_path, body, buffering)
    assert rc != 0, err
    assert CRASH_MARK not in err and "Exception ignored" not in err, err
    if held != "logging":  # logging reports a failed emit itself ("--- Logging error ---")
        assert "Traceback" not in err, err


def test_a_string_exit_status_is_still_printed_when_stdout_is_lost(tmp_path):
    """Review 2, F2: ``run()``'s tool ends with ``sys.exit("bad thing")`` and
    the last flush of stdout fails: the exit is the stdout-lost one (rc 2),
    and the tool's own message is still printed, as the interpreter would."""
    body = "def main():\n    print('x' * 100)\n    sys.exit('bad thing')\n_lib_exitcodes.run(main)\n"
    rc, err = _run_reader_gone(tmp_path, body)
    assert rc == 2, err
    assert "bad thing" in err and STDOUT_CLOSED_LINE in err, err


def test_a_string_exit_status_is_printed_on_a_clean_exit(tmp_path):
    """Review 2, mutation C: the clean path hands ``sys.exit("msg")`` back
    as given — the interpreter prints it and ends at 1."""
    proc = _run(_script(tmp_path, "def main():\n    sys.exit('bad thing')\n_lib_exitcodes.run(main)\n"))
    assert proc.returncode == 1
    assert b"bad thing" in proc.stderr


def test_a_healthy_stderr_that_cannot_encode_is_not_sealed(tmp_path):
    """Review 2, F3: a strict-ASCII stderr raises UnicodeEncodeError (a
    ValueError) on the crash report. That stderr has not failed — it must
    not be sealed: a later line on it still appears."""
    body = ("import atexit\natexit.register(lambda: print('atexit note', file=sys.stderr))\n"
            "sys.stderr.reconfigure(encoding='ascii', errors='strict')\n"
            "def main():\n    raise ValueError('caf\\u00e9 failed')\n_lib_exitcodes.run(main)\n")
    proc = _run(_script(tmp_path, body))
    assert b"atexit note" in proc.stderr, proc.stderr


# ── #3450: a Report names its own stdout-failure line ────────────────────

_OWN_LINE = ("def own(detail):\n"
             "    return 'ERROR [ERR_OUTPUT]: ' + detail\n")


def _full_run(tmp_path: Path, report_kwargs: str) -> tuple[int, str]:
    if not os.path.exists("/dev/full"):
        pytest.skip("/dev/full does not exist here")
    body = (_OWN_LINE + "def bad(detail):\n    raise RuntimeError('x')\n"
            f"_lib_exitcodes.run(lambda: _lib_exitcodes.Report({{'n': 1}}, 1{report_kwargs}))\n")
    with open("/dev/full", "wb") as full:
        proc = _run(_script(tmp_path, body), stdout=full.fileno())
    return proc.returncode, proc.stderr.decode("utf-8", "replace")


def test_a_reports_own_line_replaces_the_shared_one(tmp_path):
    """The tool's line, built from the shared detail; the exit code is the
    lost-stdout exit's all the same."""
    rc, err = _full_run(tmp_path, ", on_stdout_failure=own")
    assert rc == 2, err
    assert "ERROR [ERR_OUTPUT]: cannot write to stdout: No space left on device" in err, err
    assert "ERROR: cannot write to stdout" not in err, err


def test_a_reader_that_left_gets_the_reports_own_line_too(tmp_path):
    body = (_OWN_LINE + "_lib_exitcodes.run(lambda: _lib_exitcodes.Report("
            "{'x': 'y' * 1_000_000}, on_stdout_failure=own))\n")
    rc, err = _run_closed(tmp_path, body, "reader-gone")
    assert rc == 2, err
    assert "ERROR [ERR_OUTPUT]: cannot write to stdout: Broken pipe" in err, err


@pytest.mark.parametrize("builder", ["bad", "lambda d: ''", "lambda d: 3"])
def test_a_builder_that_fails_leaves_the_shared_line(tmp_path, builder):
    """An exit path: a builder that raises, or answers with no line, must
    not cost the run its one stderr line."""
    rc, err = _full_run(tmp_path, f", on_stdout_failure={builder}")
    assert rc == 2, err
    assert "ERROR: cannot write to stdout: No space left on device" in err, err
    assert "Traceback" not in err, err


def test_on_stdout_failure_must_be_callable():
    sys.path.insert(0, str(TOOLS_DIR))
    import _lib_exitcodes
    with pytest.raises(TypeError):
        _lib_exitcodes.Report({}, on_stdout_failure="ERROR [X]")


def test_a_reports_stdout_failure_line_does_not_outlive_its_run(capsys):
    """#3450: the builder a Report set is cleared by the reset (tests that run
    tools in-process) and at the start of every run(), so a later run's
    stdout failure is not worded by an earlier run's tool. The same for a
    best-effort delivery (#3558)."""
    sys.path.insert(0, str(TOOLS_DIR))
    import _lib_exitcodes as ec  # noqa: E402

    def own(detail):
        return "ERROR [X]: " + detail

    ec.emit_result(ec.Report({}, on_stdout_failure=own, delivery="best_effort"))
    assert ec._stdout_failure_line is own and ec._best_effort
    ec.reset_exit_state()
    assert ec._stdout_failure_line is None and not ec._best_effort

    ec.emit_result(ec.Report({}, on_stdout_failure=own, delivery="best_effort"))
    seen = []
    with pytest.raises(SystemExit):
        ec.run(lambda: seen.append((ec._stdout_failure_line, ec._best_effort)) or 0)
    assert seen == [(None, False)]
    capsys.readouterr()


# ── #3558 P2: Report(..., delivery="best_effort") ────────────────────────

_DELIVERY_WAYS = {
    # (how stdout fails, size of the document)
    "dev-full-small": ("dev-full", 10),
    "dev-full-big": ("dev-full", "io.DEFAULT_BUFFER_SIZE * 4"),
    "reader-gone-small": ("reader-gone", 10),
    "reader-gone-big": ("reader-gone", "io.DEFAULT_BUFFER_SIZE * 4"),
    "closed-at-start": (">&-", 10),
}


def _deliver(tmp_path: Path, kwargs: str, way: str, buffering: str) -> tuple[int, str]:
    how, size = _DELIVERY_WAYS[way]
    body = (f"_lib_exitcodes.run(lambda: _lib_exitcodes.Report("
            f"{{'x': 'y' * ({size})}}, 5{kwargs}))\n")
    script = _script(tmp_path, body)
    env = _BUFFERING[buffering]
    if how == "dev-full":
        if not os.path.exists("/dev/full"):
            pytest.skip("/dev/full does not exist here")
        with open("/dev/full", "wb") as full:
            proc = _run(script, stdout=full.fileno(), env=env)
    elif how == "reader-gone":
        r, w = os.pipe()
        os.close(r)
        try:
            proc = _run(script, stdout=w, env=env)
        finally:
            os.close(w)
    else:
        proc = _run_with_fds_closed(script, close=">&-", env=env)
    return proc.returncode, proc.stderr.decode("utf-8", "replace")


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
@pytest.mark.parametrize("way", sorted(_DELIVERY_WAYS))
def test_best_effort_keeps_the_status_and_says_the_failure(tmp_path, way, buffering):
    """P2: the document cannot be delivered — the line is printed once, and
    the run keeps the Report's status (5 here: patch-config's codes say what
    the ConfigMap holds)."""
    rc, err = _deliver(tmp_path, ", delivery='best_effort'", way, buffering)
    assert rc == 5, err
    assert err.count(STDOUT_CLOSED_LINE) == 1, err
    assert "Exception ignored" not in err and "Traceback" not in err, err


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
@pytest.mark.parametrize("way", sorted(_DELIVERY_WAYS))
def test_the_default_delivery_replaces_the_status(tmp_path, way, buffering):
    """Control for the test above: the same failure without best effort is
    the run's failure — rc 2, whatever the Report's status was."""
    rc, err = _deliver(tmp_path, "", way, buffering)
    assert rc == 2, err
    assert err.count(STDOUT_CLOSED_LINE) == 1, err


def test_best_effort_with_a_healthy_stdout_is_the_plain_report(tmp_path):
    proc = _run(_script(tmp_path, "_lib_exitcodes.run(lambda: _lib_exitcodes.Report("
                                  "{'n': 1}, 3, delivery='best_effort'))\n"))
    assert proc.returncode == 3, proc.stderr
    assert json.loads(proc.stdout) == {"n": 1}
    assert proc.stderr == b""


@pytest.mark.parametrize("bad", ["best-effort", "", None, "ALL"])
def test_delivery_must_be_one_of_the_two(bad):
    sys.path.insert(0, str(TOOLS_DIR))
    import _lib_exitcodes as ec  # noqa: E402
    with pytest.raises(ValueError):
        ec.Report({}, delivery=bad)
    assert ec.DELIVERIES == ("all", "best_effort")


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
@pytest.mark.parametrize("then", ["return-int", "best-effort-report"])
def test_a_noted_stdout_failure_keeps_the_status(tmp_path, then, buffering):
    """`note_stdout_failure` (patch-config's `_say`): the tool caught its own
    stdout write's error and handed it over — one line, and the run keeps
    the code it ends with, even with bytes still owed to stdout."""
    ending = ("return 6" if then == "return-int"
              else "return _lib_exitcodes.Report({'n': 1}, 6, delivery='best_effort')")
    body = ("def main():\n"
            "    try:\n        print('owed')\n        print('x' * (io.DEFAULT_BUFFER_SIZE * 4))\n"
            "        sys.stdout.flush()\n"
            "    except OSError as exc:\n        _lib_exitcodes.note_stdout_failure(exc)\n"
            f"    {ending}\n"
            "_lib_exitcodes.run(main)\n")
    rc, err = _run_closed(tmp_path, body, "reader-gone", _BUFFERING[buffering])
    assert rc == 6, err
    assert err.count(STDOUT_CLOSED_LINE + " Broken pipe") == 1, err
    assert "Traceback" not in err and "Exception ignored" not in err, err
