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
import hashlib
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


#: A registered entry name (#3611): its 3..7 are the tool's own codes, so a
#: toy tool that must end at a code of its own other than 0/1/2 is written
#: under this name — any other name ends such a code as a caller error.
_CODED_TOOL = "patch_config.py"


def _coded(tmp_path: Path, body: str) -> Path:
    return _script(tmp_path, body, name=_CODED_TOOL)


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
    script = _coded(tmp_path, body)
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
    proc = _run(_coded(tmp_path, _armed(body)))
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


def _is_main_test(node: ast.AST) -> bool:
    """A comparison of `__name__` with "__main__" — either order, any
    operator, anywhere in an expression (#3579 F1: the population is every
    module that asks the question, not only the ones that ask it the
    standard way)."""
    if not isinstance(node, ast.Compare):
        return False
    parts = [node.left, *node.comparators]
    return (any(isinstance(p, ast.Name) and p.id == "__name__" for p in parts)
            and any(isinstance(p, ast.Constant) and p.value == "__main__" for p in parts))


def _is_main_guard(node: ast.stmt) -> bool:
    """The one standard shape: `if __name__ == "__main__":` with no else."""
    return (isinstance(node, ast.If) and not node.orelse
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__"
            and len(node.test.ops) == 1 and isinstance(node.test.ops[0], ast.Eq)
            and len(node.test.comparators) == 1
            and isinstance(node.test.comparators[0], ast.Constant)
            and node.test.comparators[0].value == "__main__")


def _module_scope(tree: ast.Module):
    """Every node evaluated when the module runs: function and lambda bodies
    are left out (they run when called); decorators, defaults and class
    bodies are kept (they run at import)."""
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            if not isinstance(node, ast.Lambda):
                stack.extend(node.decorator_list)
            stack.extend(node.args.defaults)
            stack.extend(d for d in node.args.kw_defaults if d is not None)
        else:
            stack.extend(ast.iter_child_nodes(node))


def _bindings(tree: ast.Module, name: str) -> list[ast.AST]:
    """Every statement or name that binds *name* in the module's namespace,
    in source order. `import *` and `global <name>` count: either can put
    something else under the name."""
    out: list[ast.AST] = []
    for node in _module_scope(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                out.append(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if any(a.name == "*" or (a.asname or a.name.split(".")[0]) == name
                   for a in node.names):
                out.append(node)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            if node.id == name:
                out.append(node)
    out += [n for n in ast.walk(tree)
            if isinstance(n, (ast.Global, ast.Nonlocal)) and name in n.names]
    return sorted(out, key=lambda n: (n.lineno, n.col_offset))


#: The decorators the run() tools put on their entry (26 of 97 measured on
#: 2026-11-05): each turns one library error into rc 2 and forwards
#: `*args, **kwargs` to the def, so run()'s no-argument call reaches it
#: unchanged — test_the_allowed_entry_decorators_forward_a_bare_call runs
#: each. Matched by the last name of a dotted reference; `session()` is
#: `_lib_parse_cache.session()`, a context manager used as a decorator.
_ENTRY_DECORATORS = frozenset({
    "exit_on_yaml_file_error", "exit_on_output_write_error", "exit_on_served_values_error",
    "exit_on_parse_tree_error", "exit_on_observed_map_error",
})


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (head := _dotted(node.value)):
        return f"{head}.{node.attr}"
    return None


def _is_allowed_entry_decorator(deco: ast.expr) -> bool:
    if isinstance(deco, ast.Call):
        return (not deco.args and not deco.keywords
                and _dotted(deco.func) == "_lib_parse_cache.session")
    name = _dotted(deco)
    return name is not None and name.rsplit(".", 1)[-1] in _ENTRY_DECORATORS


def _is_plain_lib_import(node: ast.AST) -> bool:
    return (isinstance(node, ast.Import) and len(node.names) == 1
            and node.names[0].name == "_lib_exitcodes" and node.names[0].asname is None)


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
    `_lib_exitcodes.run(<entry>)` and only through it, else None (#3579).

    The shape that passes, and nothing looser:

    * exactly one module-level `if __name__ == "__main__":` (no else), and no
      other test of `__name__` against "__main__" anywhere in the module;
    * its body is `_lib_exitcodes.run(<entry>)`, optionally preceded by
      `import _lib_exitcodes` — the two shapes the run() tools use (94 / 3
      of 97 measured on 2026-11-05);
    * `_lib_exitcodes` is bound only by plain `import _lib_exitcodes`;
    * `<entry>` is bound once, by a module-level `def` decorated only with
      _ENTRY_DECORATORS, whose every parameter has a default, and is not
      called at module scope.

    ⚠️ Not seen: rebinding through `globals()` / `setattr` on the module, a
    `sys.modules` entry planted before the import, or a module that defines
    its own function under an _ENTRY_DECORATORS name (one does, as a no-op
    fallback: generate_rule_pack_split.py)."""
    guards = [n for n in tree.body if _is_main_guard(n)]
    tests = [n for n in ast.walk(tree) if _is_main_test(n)]
    if len(guards) != 1 or len(tests) != 1:
        stray = [t for t in tests if not guards or t is not guards[0].test]
        line = stray[0].lineno if stray else 1
        return line, ("expected exactly one module-level `if __name__ == \"__main__\":` "
                      "and no other test of `__name__` against \"__main__\"; found "
                      f"{len(guards)} guard(s) and {len(tests)} test(s)")
    *before, last = guards[0].body
    if before and not (len(before) == 1 and _is_plain_lib_import(before[0])):
        stmt = before[-1] if len(before) == 1 else before[0]
        return stmt.lineno, ("the guard holds only `_lib_exitcodes.run(<entry>)`, optionally "
                             f"after `import _lib_exitcodes`; got `{ast.unparse(stmt)}`")
    call = last.value if isinstance(last, ast.Expr) else None
    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and call.func.attr == "run" and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "_lib_exitcodes"
            and len(call.args) == 1 and isinstance(call.args[0], ast.Name)
            and not call.keywords):
        return (last.lineno,
                f"the guard must end with `_lib_exitcodes.run(<entry>)`, got `{ast.unparse(last)}`")
    lib = _bindings(tree, "_lib_exitcodes")
    if not lib:
        return last.lineno, "`_lib_exitcodes` is never imported"
    for node in lib:
        if not _is_plain_lib_import(node):
            return node.lineno, ("`_lib_exitcodes` must be bound only by "
                                 f"`import _lib_exitcodes`, got `{ast.unparse(node).splitlines()[0]}`")
    entry = call.args[0].id
    binds = _bindings(tree, entry)
    if len(binds) != 1 or not isinstance(binds[0], ast.FunctionDef) or binds[0] not in tree.body:
        odd = next((b for b in binds if not (isinstance(b, ast.FunctionDef) and b in tree.body)),
                   binds[1] if len(binds) > 1 else None)
        line = odd.lineno if odd is not None else last.lineno
        got = ast.unparse(odd).splitlines()[0] if odd is not None else "no definition"
        return line, (f"`{entry}` must be bound once, by one module-level `def`; "
                      f"found {len(binds)} binding(s), e.g. `{got}`")
    fn = binds[0]
    for deco in fn.decorator_list:
        if not _is_allowed_entry_decorator(deco):
            return deco.lineno, (f"`{entry}` is decorated with `{ast.unparse(deco)}`, which is "
                                 "not one of the decorators known to forward run()'s no-argument "
                                 "call (_ENTRY_DECORATORS)")
    for node in _module_scope(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == entry:
            return node.lineno, f"`{entry}` is also called outside run(): `{ast.unparse(node)}`"
    if why := _entry_signature_fault(fn):
        return fn.lineno, why
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
    src = ("import _lib_exitcodes\n" + sig + "\n    pass\n"
           "if __name__ == '__main__':\n    _lib_exitcodes.run(main)\n")
    fault = _guard_fault(ast.parse(src))
    assert (fault is None) is ok, fault
    if not ok:
        assert fault[0] == 2, fault


_OK_TOOL = ("import sys\nimport _lib_exitcodes\n"
            "def main(argv=None):\n    return 0\n"
            "if __name__ == '__main__':\n    _lib_exitcodes.run(main)\n")
_GUARD = "if __name__ == '__main__':\n    _lib_exitcodes.run(main)\n"


def _shape(old: str, new: str) -> str:
    assert _OK_TOOL.count(old) == 1, old
    return _OK_TOOL.replace(old, new)


@pytest.mark.parametrize("src", [
    _OK_TOOL,
    _shape("import _lib_exitcodes\n", "").replace(
        "':\n", "':\n    import _lib_exitcodes\n"),
    _OK_TOOL.replace("':\n", "':\n    import _lib_exitcodes\n"),
    _shape("def main(argv=None):",
           "@exit_on_yaml_file_error\n@_lib_parse_cache.session()\n"
           "@recommend.observed_map_lib.exit_on_observed_map_error\ndef main(argv=None):"),
], ids=["module-import", "guard-import", "both-imports", "allowed-decorators"])
def test_the_shapes_the_run_tools_use_pass(src):
    assert _guard_fault(ast.parse(src)) is None


def test_the_allowed_entry_decorators_forward_a_bare_call():
    """Each name in _ENTRY_DECORATORS, taken from the library the tools take
    it from, leaves a no-argument call reaching the def: what run() does."""
    import _lib_io
    import _lib_parse_cache
    import _lib_tenant_values
    import _observed_map_lib  # noqa: E402  (scripts/tools/ops, path set by conftest.py)
    found = {
        "exit_on_yaml_file_error": _lib_io.exit_on_yaml_file_error,
        "exit_on_output_write_error": _lib_io.exit_on_output_write_error,
        "exit_on_served_values_error": _lib_tenant_values.exit_on_served_values_error,
        "exit_on_parse_tree_error": _lib_parse_cache.exit_on_parse_tree_error,
        "exit_on_observed_map_error": _observed_map_lib.exit_on_observed_map_error,
    }
    assert set(found) == _ENTRY_DECORATORS

    def entry(argv=None, *, lang="en"):
        return (argv, lang)

    for name, deco in found.items():
        assert deco(entry)() == (None, "en"), name
    assert _lib_parse_cache.session()(entry)() == (None, "en")


@pytest.mark.parametrize("src, line", [
    (_shape(_GUARD, "if '__main__' == __name__:\n    _lib_exitcodes.run(main)\n"), 5),
    (_shape("== '__main__':", "== '__main__' and sys.argv:"), 5),
    (_shape(_GUARD, "try:\n    " + _GUARD.replace("\n    ", "\n        ") + "except Exception:\n"
                    "    pass\n"), 6),
    (_shape(_GUARD, _GUARD + "else:\n    pass\n"), 5),
    (_shape("== '__main__'", "!= '__main__'"), 5),
    (_shape("import sys\n", "import sys\n_X = __name__ == '__main__'\n"), 2),
    (_shape("':\n", "':\n    sys.exit(other())\n"), 6),
    (_shape(_GUARD, "main()\n" + _GUARD), 5),
    (_shape("def main(argv=None):", "def helper(x=main()):\n    pass\ndef main(argv=None):"), 3),
    (_shape("import _lib_exitcodes\n",
            "class _lib_exitcodes:\n    run = staticmethod(lambda f: f())\n"), 2),
    (_shape("import _lib_exitcodes\n", "import _lib_exitcodes\n_lib_exitcodes = object()\n"),
     3),
    (_shape("import _lib_exitcodes\n", "import os as _lib_exitcodes\n"), 2),
    (_shape("import _lib_exitcodes\n", "from os import *\nimport _lib_exitcodes\n"), 2),
    (_shape("import _lib_exitcodes\n", ""), 5),
    (_shape(_GUARD, "main = lambda: 1\n" + _GUARD), 5),
    (_shape(_GUARD, "def main():\n    return 1\n" + _GUARD), 5),
    (_shape("def main(argv=None):", "@wrap\ndef main(argv=None):"), 3),
    (_shape("def main(argv=None):", "@exit_on_yaml_file_error\n@wrap(1)\ndef main(argv=None):"),
     4),
    (_shape("def main(argv=None):", "async def main(argv=None):"), 3),
    (_shape(_GUARD, "def f():\n    global main\n    main = 1\n" + _GUARD), 6),
    (_shape("    _lib_exitcodes.run(main)\n",
            "    from _lib_exitcodes import run as r\n    r(main)\n"), 6),
    (_shape("    _lib_exitcodes.run(main)\n", "    _lib_exitcodes.run(lambda: main())\n"), 6),
], ids=["reversed-guard", "and-guard", "guard-in-try", "guard-with-else", "ne-guard",
        "stray-test", "call-before-run", "module-level-call", "call-in-default",
        "class-named-lib", "lib-reassigned", "lib-alias", "star-import", "lib-not-imported",
        "entry-rebound", "entry-redefined", "entry-decorated", "entry-decorated-twice",
        "entry-async",
        "entry-global-rebind", "run-alias", "run-lambda"])
def test_ways_around_run_are_faults(src, line):
    """#3579 F1/F2: each shape reaches the entry some other way than through
    `_lib_exitcodes.run`, or hands run() something other than the `def`
    the source shows. Each is a fault, on the line that carries it."""
    fault = _guard_fault(ast.parse(src))
    assert fault is not None and fault[0] == line, (fault, src)


# ── #3579: every `__main__` under the tool trees, not only da-tools ─────────

#: The trees whose modules must enter through run() (#3579, owner
#: 2026-11-05). Repo-relative POSIX prefixes.
GATED_TREES = ("scripts/tools/", "scripts/ops/", "scripts/dx/", "scripts/session-guards/")

#: ⛔ Frozen: the entries that did not go through run() when the gate widened
#: (#3579, 2026-11-05). Never edited — its digest is pinned in
#: test_the_baseline_is_frozen, so an entry can neither be added nor swapped
#: for another. A tool that migrates joins MIGRATED instead.
BASELINE: frozenset[str] = frozenset({
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

#: BASELINE entries that enter through run() now (or are gone). Add a line
#: when a tool migrates, and lower the count in
#: test_the_pending_entries_are_a_pinned_ceiling with it.
MIGRATED: frozenset[str] = frozenset({
    # #3579 PR2: scripts/tools/lint/, already importing _lib_exitcodes (exit codes unchanged)
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
    "scripts/tools/lint/check_subprocess_timeout.py",
    "scripts/tools/lint/check_threshold_observed_map.py",
    "scripts/tools/lint/check_threshold_reachability.py",
    "scripts/tools/lint/check_threshold_registry.py",
    "scripts/tools/lint/check_tool_registry_jsx_parity.py",
    "scripts/tools/lint/check_undefined_tokens.py",
    "scripts/tools/lint/check_unpinned_deps.py",
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
    # #3579 PR3: dx / ops / scripts-ops / scripts-dx, already importing _lib_exitcodes
    # (directly, or through a _lib_* it imports) — exit codes unchanged
    "scripts/dx/generate_adr_index.py",
    "scripts/dx/generate_planning_index.py",
    "scripts/ops/audit_rules_drift.py",
    "scripts/ops/bench_synth_tenants.py",
    "scripts/ops/configmap_assemble.py",
    "scripts/tools/dx/add_frontmatter.py",
    "scripts/tools/dx/analyze_bench_history.py",
    "scripts/tools/dx/axe_lite_static.py",
    "scripts/tools/dx/bump_docs.py",
    "scripts/tools/dx/bump_playbook_versions.py",
    "scripts/tools/dx/check_aria_references.py",
    "scripts/tools/dx/compile_custom_alerts.py",
    "scripts/tools/dx/converge_status.py",
    "scripts/tools/dx/describe_tenant.py",
    "scripts/tools/dx/gen_agent_adapters.py",
    "scripts/tools/dx/gen_recipe_contract.py",
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
    "scripts/tools/ops/da_assembler.py",
    "scripts/tools/ops/generate_tenant_mapping_rules.py",
    "scripts/tools/ops/inject_metadata_join.py",
    # #3579 PR4: the rest of scripts/tools/ (no _lib_exitcodes before) — a crash 1→2,
    # a failed stdout write 120→2, `>&-` 0→2 (owner-approved rule, #3579)
    "scripts/tools/dx/analyze_probe.py",
    "scripts/tools/dx/bench_verdict.py",
    "scripts/tools/dx/gen_am_duration_json.py",
    "scripts/tools/dx/gen_tenant_id_json.py",
    "scripts/tools/lint/check_maintenance_symmetry.py",
    "scripts/tools/lint/check_structure.py",
    "scripts/tools/lint/check_vmalert_coverage.py",
    "scripts/tools/ops/_federation_revocation_reconciler.py",
    # #3579 PR5: scripts/ops/ (scripts/tools/ appended to sys.path) — a crash 1→2,
    # a failed stdout write 120→2, `>&-` 0→2 (owner-approved rule, #3579)
    "scripts/ops/add_t_parallel.py",
    "scripts/ops/check_image_refs_resolve.py",
    "scripts/ops/ci_flake_retry.py",
    "scripts/ops/commit_helper.py",
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
    # #3579: scripts/session-guards/ — Claude Code hooks, under use_hook_protocol()
    # (owner A′, 2026-11-05): a crash or a failed write ends at 1, never the blocking 2
    "scripts/session-guards/paths_map.py",
    "scripts/session-guards/preflight_bash.py",
    "scripts/session-guards/session-init.py",
    "scripts/session-guards/skill_usage.py",
    "scripts/session-guards/stop_evidence.py",
    "scripts/session-guards/vscode_git_toggle.py",
})

#: The ceiling of entries not yet through run(): it only shrinks. A new tool
#: enters through run() from its first commit — there is no way to add one.
PENDING: frozenset[str] = BASELINE - MIGRATED

_BASELINE_SHA256 = "f6d1e806ad139f85efdf4e8ac4937fa4f28ded1a9b24e4fa7963d74c5f78cbfc"


def _gated_sources() -> dict[str, str]:
    """repo-relative path -> source of every `.py` under GATED_TREES that
    tests `__name__` against "__main__" anywhere, in any shape (#3579 F1: a
    module asking the question the wrong way is in the population and is a
    fault, not left out). The population is the tree itself (`git ls-files`,
    tests/_tree.py), not a list kept here."""
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
        if any(_is_main_test(n) for n in ast.walk(tree)):
            out[rel] = source
    return out


def _gate_faults(sources: dict[str, str], pending: frozenset[str]) -> list[str]:
    """Every fault of the gate over *sources* (path -> source) with *pending*
    as the ceiling. A pure function, so its answers can be pinned on shapes
    whose answer is known (test_gate_faults_on_known_shapes)."""
    faults = []
    for rel in sorted(pending - set(sources)):
        faults.append(f"{rel}: pending but not an entry point under the gated trees "
                      "(gone, or moved) — add it to MIGRATED")
    for rel, source in sorted(sources.items()):
        fault = _guard_fault(ast.parse(source, filename=rel))
        if fault is None and rel in pending:
            faults.append(f"{rel}: enters through run() now — add it to MIGRATED "
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


def test_the_baseline_is_frozen():
    """BASELINE is the list as it stood when the gate widened; it is never
    edited (#3579 F3). A changed digest means an entry was added or swapped —
    put it back, and migrate the tool instead."""
    digest = hashlib.sha256("\n".join(sorted(BASELINE)).encode()).hexdigest()
    assert digest == _BASELINE_SHA256
    assert len(BASELINE) == 146


def test_migrated_names_baseline_entries():
    """A MIGRATED line outside BASELINE subtracts nothing — a typo there would
    leave its tool pending without anyone seeing it."""
    assert MIGRATED <= BASELINE, sorted(MIGRATED - BASELINE)


def test_the_pending_entries_are_a_pinned_ceiling():
    """The count is written out so that a shorter PENDING is a visible edit of
    this line too (#3579): only ever lower it."""
    assert len(PENDING) == 0


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
     ["a.py: enters through run() now — add it to MIGRATED and lower the pinned count"]),
    ({}, frozenset({"gone.py"}),
     ["gone.py: pending but not an entry point under the gated trees (gone, or moved) "
      "— add it to MIGRATED"]),
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


# #3602: `<&-` is empty input, as `</dev/null` is — the reads a tool makes.
_STDIN_READS = {
    "read": "sys.stdin.read()",
    "buffer": "sys.stdin.buffer.read()",
    "lines": "list(sys.stdin)",
    "input": "input()",
}


@pytest.mark.parametrize("read", sorted(_STDIN_READS))
@pytest.mark.parametrize("stdin", ["<&-", "</dev/null"])
def test_a_closed_stdin_reads_as_empty_input(tmp_path, read, stdin):
    """`tool <&-`: the interpreter left `sys.stdin` None, which crashed every
    tool that reads it (AttributeError / TypeError / `input()`'s
    RuntimeError "lost sys.stdin", rc 2). It is bound to the null device
    sanitize gave fd 0 — the same read as `</dev/null`: empty, or EOFError."""
    body = _armed(
        "assert sys.stdin is sys.__stdin__ and sys.stdin.fileno() == 0\n"
        "try:\n"
        f"    got = {_STDIN_READS[read]}\n"
        "except EOFError:\n"
        "    got = 'EOF'\n"
        "print(repr(got))\n")
    proc = _run_with_fds_closed(_script(tmp_path, body), close=stdin)
    assert proc.returncode == 0, proc.stderr
    want = {"read": "''", "buffer": "b''", "lines": "[]", "input": "'EOF'"}[read]
    assert proc.stdout.decode().strip() == want, proc.stderr


def test_the_entrypoint_binds_a_closed_stdin_too(tmp_path):
    """`da-tools <cmd> <&-`: the dispatcher's copy of sanitize binds it as
    well, before the tool (which may not import _lib_exitcodes) runs."""
    bash = shutil.which("bash") or pytest.skip("bash is not on PATH")
    sys.path.insert(0, str(REPO_ROOT / "components" / "da-tools" / "app"))
    try:
        import entrypoint  # noqa: E402
    finally:
        sys.path.pop(0)
    opt = tmp_path / "opt"
    opt.mkdir()
    shutil.copy(REPO_ROOT / "components" / "da-tools" / "app" / "entrypoint.py", opt / "entrypoint.py")
    (opt / entrypoint.COMMAND_MAP["validate-config"]).write_text(
        "import sys\nprint(repr(sys.stdin.read()))\n", encoding="utf-8")
    proc = subprocess.run([bash, "-c", 'exec "$0" "$1" validate-config <&-', sys.executable,
                           str(opt / "entrypoint.py")],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, env=_ENV)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.decode().strip() == "''"


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
    ec._stderr_lost = True               # a stderr write lost its text (#3599)
    ec._stdout_failure_line = lambda d: "ERROR [X]: " + d


@pytest.mark.skipif(not os.environ.get(_STATE_PROBE), reason="driven by the test below")
def test_probe_sees_clean_exit_state():
    sys.path.insert(0, str(TOOLS_DIR))
    import _lib_exitcodes as ec  # noqa: E402
    assert (ec._failure_reported, ec._best_effort, ec._stderr_lost, ec._stdout_failure_line,
            ec._stdout_owner, ec.get_crash_exit_code(),
            ec.get_caller_error_code()) == (False, False, False, None, None,
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
    proc = _run(_coded(tmp_path, body), env=_BUFFERING[buffering])
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
    tool = _coded(tmp_path, (
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


_NOT_IN_CONTRACT = b"exit-code contract (scripts/tools/_lib_exitcodes.py EXTENDED_EXIT_CODES"


@pytest.mark.real_crash_exit_code
@pytest.mark.parametrize("status", [2 ** 70, 256, 512, -256, 3, -1])
def test_a_status_outside_the_contract_is_a_caller_error(tmp_path, status):
    """#3611: ``run()`` ends a status its entry is not registered for at the
    caller-error code, with one line naming it — never as given. Review F5
    (2**70) and the multiples of 256 the OS would truncate to 0 (a false
    success) are the reason; an unregistered 3 is the same refusal."""
    proc = _run(_script(tmp_path, f"_lib_exitcodes.run(lambda: {status})\n"))
    assert proc.returncode == 2, proc.stderr
    assert proc.stderr.count(_NOT_IN_CONTRACT) == 1, proc.stderr


def test_under_tests_a_status_outside_the_contract_is_the_test_crash_code(tmp_path):
    """Under the suite (DA_TEST_CRASH_EXIT_CODE), the refusal ends at the test
    crash code, so a test pinning an unregistered code goes red (#3611)."""
    proc = _run(_script(tmp_path, "_lib_exitcodes.run(lambda: 3)\n"))
    assert proc.returncode == int(os.environ["DA_TEST_CRASH_EXIT_CODE"]), proc.stderr


def test_a_registered_entry_keeps_its_own_code(tmp_path):
    """The registry's row for the entry's name lets its own codes through."""
    proc = _run(_coded(tmp_path, "_lib_exitcodes.run(lambda: 5)\n"))
    assert proc.returncode == 5, proc.stderr


def test_a_registered_entry_keeps_its_code_when_exec_ed_under_another_main(tmp_path):
    """The da-tools image runs a tool by exec-ing it as ``__main__`` from
    entrypoint.py, without putting it in sys.modules: ``__main__`` there is
    still the entrypoint. The row is the entry's own file's (#3611 review)."""
    tool = _coded(tmp_path, "def main():\n    return 5\n_lib_exitcodes.run(main)\n")
    launcher = tmp_path / "entrypoint.py"
    launcher.write_text(
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('__main__', {str(tool)!r})\n"
        "spec.loader.exec_module(importlib.util.module_from_spec(spec))\n",
        encoding="utf-8")
    proc = _run(launcher)
    assert proc.returncode == 5, proc.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="a /bin/sh stand-in for da-guard")
@pytest.mark.parametrize("code", [3, 4])
def test_da_tools_guard_keeps_da_guards_codes_through_the_entrypoint(tmp_path, code):
    """The customer path: `da-tools guard` through entrypoint.py ends at
    da-guard's own 3 / 4, as dev-rules §13 lists (#3611 review)."""
    fake = tmp_path / "fake-guard"
    fake.write_text(f"#!/bin/sh\nexit {code}\n", encoding="utf-8")
    fake.chmod(0o755)
    (tmp_path / "cfg").mkdir()
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "components/da-tools/app/entrypoint.py"),
         "guard", "defaults-impact", "--config-dir", str(tmp_path / "cfg")],
        capture_output=True, timeout=60, env=dict(_ENV, DA_GUARD_BINARY=str(fake)))
    assert proc.returncode == code, proc.stderr


@pytest.mark.real_crash_exit_code
@pytest.mark.parametrize("status, rc", [(9, 9), (255, 255), (256, 2), (-1, 2)])
def test_a_passthrough_entry_forwards_a_status_the_os_can_carry(tmp_path, status, rc):
    """PASSTHROUGH forwards whatever the wrapped command ended with, as long as
    an exit status can carry it: 256 would read as 0, a false success (#3611)."""
    proc = _run(_script(tmp_path, f"_lib_exitcodes.run(lambda: {status})\n",
                        name="dx_run.py"))
    assert proc.returncode == rc, proc.stderr


def test_a_decorated_entry_is_named_by_its_own_file(tmp_path):
    """A functools.wraps decorator defined in a library must not move the
    entry's name to the library's file (the allowed entry decorators are)."""
    (tmp_path / "_deco_lib.py").write_text(
        "import functools\n"
        "def deco(fn):\n"
        "    @functools.wraps(fn)\n"
        "    def w(*a, **k):\n"
        "        return fn(*a, **k)\n"
        "    return w\n", encoding="utf-8")
    proc = _run(_coded(tmp_path,
                       "from _deco_lib import deco\n"
                       "@deco\n"
                       "def main():\n"
                       "    return 6\n"
                       "_lib_exitcodes.run(main)\n"))
    assert proc.returncode == 6, proc.stderr


def test_the_registry_cannot_be_changed_at_runtime():
    """A tool cannot register its own code on the way out: the one list is
    the module's (#3611)."""
    import _lib_exitcodes as ec
    with pytest.raises(TypeError):
        ec.EXTENDED_EXIT_CODES["tool"] = frozenset({3})


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


@pytest.mark.real_crash_exit_code
def test_a_string_exit_status_is_printed_and_ends_as_a_caller_error(tmp_path):
    """Review 2, mutation C, and #3611: ``sys.exit("msg")`` prints the
    message, as the interpreter would, and ends at the caller-error code —
    not the interpreter's 1, which reads as a finding."""
    proc = _run(_script(tmp_path, "def main():\n    sys.exit('bad thing')\n_lib_exitcodes.run(main)\n"))
    assert proc.returncode == 2
    assert proc.stderr.count(b"bad thing") == 1, proc.stderr


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
    script = _coded(tmp_path, body)
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
    proc = _run(_coded(tmp_path, "_lib_exitcodes.run(lambda: _lib_exitcodes.Report("
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


# ── #3579 PR5: scripts/ops/ reaches the run() exit on its own ───────────────

def _ops_entries() -> list[str]:
    return sorted(rel for rel in _gated_sources() if rel.startswith("scripts/ops/"))


@pytest.mark.parametrize("via", ["path", "symlink"])
def test_every_ops_entry_finds_its_exit_from_anywhere(via, tmp_path):
    """scripts/ops/ tools append scripts/tools/ to sys.path themselves (no
    _lib_* of theirs did before #3579): run from another directory, or
    through a symlink to the file, the `import _lib_exitcodes` of the guard
    must still resolve — whatever the tool then does with `--help`."""
    import subprocess
    from _platform_fs import symlink_or_skip  # noqa: E402
    entries = _ops_entries()
    assert len(entries) >= 14, entries
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONUNBUFFERED")}
    missing = []
    for rel in entries:
        target = REPO_ROOT / rel
        if via == "symlink":
            link = tmp_path / Path(rel).name
            symlink_or_skip(target, link)
            target = link
        proc = subprocess.run([sys.executable, str(target), "--help"], cwd=tmp_path,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=60, stdin=subprocess.DEVNULL)
        if "No module named '_lib_" in proc.stderr:
            missing.append(f"{rel}: {proc.stderr.strip().splitlines()[-1]}")
    assert not missing, "\n".join(missing)


# ── #3579 (owner A′, 2026-11-05): session-guards speak the hook protocol ──

#: The tree whose entries are Claude Code hooks: exit 2 BLOCKS the tool call
#: or the stop, so a crash or a failed write must end at 1 there.
HOOK_TREE = "scripts/session-guards/"


def _hook_declaration_fault(rel: str, source: str) -> str | None:
    """None when *source* imports `_lib_exitcodes` at module level (plain
    `import _lib_exitcodes`, no alias) and the very next statement is
    `_lib_exitcodes.use_hook_protocol()` — the import installs the crash
    hook with the blocking 2, so nothing may run between the two — and
    nothing at module level rebinds `_lib_exitcodes` or sets an attribute
    on it. What runs before the import ends at Python's own 1 (no hook
    yet). Otherwise the reason."""
    tree = ast.parse(source, filename=rel)
    body = tree.body
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for t in targets:
            base = t
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name) and base.id == "_lib_exitcodes":
                return f"{rel}:{node.lineno}: `{ast.unparse(node)}` rebinds `_lib_exitcodes`"
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                if (a.asname == "_lib_exitcodes"
                        or (a.name == "_lib_exitcodes" and a.asname not in (None, "_lib_exitcodes"))):
                    return f"{rel}:{node.lineno}: `{ast.unparse(node)}` aliases `_lib_exitcodes`"
    for k, node in enumerate(body):
        if (isinstance(node, ast.Import) and len(node.names) == 1
                and node.names[0].name == "_lib_exitcodes" and node.names[0].asname is None):
            nxt = body[k + 1] if k + 1 < len(body) else None
            if (isinstance(nxt, ast.Expr) and isinstance(nxt.value, ast.Call)
                    and ast.unparse(nxt.value.func) == "_lib_exitcodes.use_hook_protocol"
                    and not nxt.value.args and not nxt.value.keywords):
                return None
            where = f"{rel}:{nxt.lineno}" if nxt is not None else rel
            return (f"{where}: the statement after `import _lib_exitcodes` must be "
                    "`_lib_exitcodes.use_hook_protocol()`")
        if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                a.name == "_lib_exitcodes" for a in node.names):
            return f"{rel}:{node.lineno}: import `_lib_exitcodes` on a line of its own"
    return f"{rel}: no module-level `import _lib_exitcodes` + `use_hook_protocol()`"


def test_every_hook_declares_the_hook_protocol_first():
    """A guard whose crash ended at run()'s default 2 would block every tool
    call it matches (measured: a cp950 host and a CJK command, #3598)."""
    hooks = {rel: src for rel, src in _gated_sources().items() if rel.startswith(HOOK_TREE)}
    assert len(hooks) >= 6, sorted(hooks)
    faults = [f for rel, src in sorted(hooks.items())
              if (f := _hook_declaration_fault(rel, src))]
    assert not faults, "\n".join(faults)


def test_only_hooks_declare_the_hook_protocol():
    """1 for a crash is the hook protocol's word, not a CLI tool's: anywhere
    else it reads as a finding (dev-rules §13)."""
    from _tree import REPO_ROOT as TREE_ROOT, repo_files  # noqa: E402
    callers = set()
    for path in repo_files(".py"):
        rel = path.relative_to(TREE_ROOT).as_posix()
        if rel.startswith("tests/") or rel == "scripts/tools/_lib_exitcodes.py":
            continue
        if "use_hook_protocol" in path.read_text(encoding="utf-8"):
            callers.add(rel)
    assert callers and all(rel.startswith(HOOK_TREE) for rel in callers), sorted(callers)


_DECL = "import _lib_exitcodes\n_lib_exitcodes.use_hook_protocol()\n"


@pytest.mark.parametrize("src,fault", [
    ("import sys\n" + _DECL, False),
    ('"""doc"""\nimport sys\nX = 1\nsys.path.insert(0, "x")\n' + _DECL + "Y = int('2')\n", False),
    ("import _lib_exitcodes\nX = int('x')\n_lib_exitcodes.use_hook_protocol()\n", True),
    ("import _lib_exitcodes\nimport no_such_mod\n_lib_exitcodes.use_hook_protocol()\n", True),
    ("import _lib_exitcodes\nprint('x')\n_lib_exitcodes.use_hook_protocol()\n", True),
    ("import _lib_exitcodes\ntry:\n    _lib_exitcodes.use_hook_protocol()\nexcept Exception:\n"
     "    pass\n", True),
    ("import _lib_exitcodes\ndef main():\n    _lib_exitcodes.use_hook_protocol()\n", True),
    ("import _lib_exitcodes\n_lib_exitcodes.use_hook_protocol(1)\n", True),
    ("import os as _lib_exitcodes\n_lib_exitcodes.use_hook_protocol()\n", True),
    ("import _lib_exitcodes as ec\nec.use_hook_protocol()\n", True),
    ("import os, _lib_exitcodes\n_lib_exitcodes.use_hook_protocol()\n", True),
    (_DECL + "_lib_exitcodes.use_hook_protocol = lambda: None\n", True),
    ("if False:\n    import _lib_exitcodes\n    _lib_exitcodes.use_hook_protocol()\n", True),
], ids=["plain", "after-path-setup", "assign-between", "import-between", "call-between",
        "in-try", "only-inside-main", "with-args", "lib-is-another-module", "lib-aliased",
        "lib-on-a-shared-line", "lib-attr-rebound", "under-if"])
def test_the_hook_declaration_rule(src, fault):
    assert (_hook_declaration_fault("x.py", src) is not None) is fault


_CRASH_AT_ENTRY = (
    "import sys\n"
    "def _prof(frame, event, arg):\n"
    "    if (event == 'call' and frame.f_code.co_name == 'main'\n"
    "            and frame.f_globals.get('__name__') == '__main__'):\n"
    "        sys.setprofile(None)\n"
    "        raise RuntimeError('injected crash')\n"
    "sys.setprofile(_prof)\n")


@pytest.mark.real_crash_exit_code
@pytest.mark.parametrize("guard", sorted(
    rel for rel in BASELINE if rel.startswith(HOOK_TREE)))
def test_a_hook_that_crashes_does_not_block(guard, tmp_path):
    """The declaration, run: a guard whose `main` crashes ends at 1 (a
    non-blocking hook error), with the crash line — not run()'s default 2,
    which would block the tool call or the stop."""
    import subprocess
    (tmp_path / "sitecustomize.py").write_text(_CRASH_AT_ENTRY, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    env["PYTHONPATH"] = str(tmp_path)
    proc = subprocess.run([sys.executable, str(REPO_ROOT / guard)], input="{}",
                          capture_output=True, text=True, encoding="utf-8",
                          env=env, timeout=60)
    assert proc.returncode == 1, (proc.returncode, proc.stderr[-600:])
    assert "unexpected internal error (RuntimeError): injected crash" in proc.stderr


@pytest.mark.skipif(not os.path.exists("/dev/full"), reason="needs /dev/full")
def test_a_hook_whose_stdout_fails_does_not_block():
    """stop_evidence --message-stdin prints its verdict: a failed stdout
    write ends at 1 with the named line, not the blocking 2."""
    import subprocess
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    with open("/dev/full", "w", encoding="utf-8") as full:
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / HOOK_TREE / "stop_evidence.py"), "--message-stdin"],
            input="nothing claimed here", stdout=full, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", env=env, timeout=60)
    assert proc.returncode == 1, (proc.returncode, proc.stderr)
    assert "ERROR: cannot write to stdout:" in proc.stderr


# ── #3611: an exit status outside 0/1/2 is registered, or it is a fault ─────
#
# run() refuses an unregistered status at runtime (`_contract_status`), but a
# test that calls `main()` in-process never reaches run(): it can pin
# `main() == 3` and stay green while the real process ends at 2. So the
# literals are also read here, statically, from every entry under the gated
# trees — any function's `return N`, `sys.exit(N)` / `SystemExit(N)` /
# `os._exit(N)`, and a module-level `EXIT_* = N`, for an int N outside 0..2 —
# and each must be in the entry's EXTENDED_EXIT_CODES row. A PASSTHROUGH row
# excuses only what it forwards, never a literal it chose itself. Measured on main before #3611: the ten unregistered 3 / 4 /
# 127 the runtime check then turned into 2 are all found by this read.

#: Functions under the gated trees whose int return is a value, not an exit
#: status. Each entry must still match something (stale entries fail), so
#: the list cannot outlive what it excuses.
NOT_EXIT_STATUS: frozenset[tuple[str, str]] = frozenset({
    ("scripts/tools/dx/inject_waveform.py", "_parse_group_interval"),
    ("scripts/tools/dx/scan_component_health.py", "_loc_score"),
    ("scripts/tools/dx/scan_component_health.py", "_recency_score"),
    ("scripts/tools/lint/check_account_registry_monotonic.py", "parse_next_account_id"),
    ("scripts/tools/ops/diagnose.py", "kubectl_budget"),
})


def _int_literal(node: ast.AST | None) -> int | None:
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    if (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant) and type(node.operand.value) is int):
        return -node.operand.value
    return None


def _extended_literals(tree: ast.Module) -> list[tuple[int, str, int]]:
    """(line, where, value) for every exit-status-shaped int outside 0..2 in
    *tree*. Read through the shapes the tools write: a literal, either arm of
    `a if c else b`, and a name bound at module level to an int, annotated or
    not (`return RC_X`); in `return` (of the function it is in, not of a
    nested def), a lambda's body, `sys.exit(…)` / `SystemExit(…)` /
    `os._exit(…)` (positional or `code=`), `Report(doc, status)`,
    `die_caller_error(…, code=)`, and any `EXIT_*` / `_EXIT_*` binding,
    module-level or local. *where* is the enclosing function for a
    return. Not read (the runtime check in run() still refuses them): a value
    reached through a local variable, a class or Enum attribute, or another
    module. A name a function rebinds locally is still read as the module's
    int: that false alarm fails loudly, and renaming the local clears it."""
    module_ints: dict[str, int] = {}
    for node in tree.body:
        if (isinstance(node, (ast.Assign, ast.AnnAssign))
                and (v := _int_literal(node.value)) is not None):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    module_ints[target.id] = v

    def values(expr: ast.AST | None) -> list[int]:
        if isinstance(expr, ast.IfExp):
            return values(expr.body) + values(expr.orelse)
        if isinstance(expr, ast.Name) and expr.id in module_ints:
            return [module_ints[expr.id]]
        v = _int_literal(expr)
        return [] if v is None else [v]

    out = []

    def add(line: int, where: str, expr: ast.AST | None) -> None:
        out.extend((line, where, v) for v in values(expr) if not 0 <= v <= 2)

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (isinstance(target, ast.Name)
                        and target.id.lstrip("_").startswith("EXIT_")):
                    add(node.lineno, target.id, node.value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # A return belongs to the function it is in: a nested def is
            # walked on its own (as ast.walk reaches it), never as its parent.
            stack = list(ast.iter_child_nodes(node))
            while stack:
                sub = stack.pop()
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    continue
                if isinstance(sub, ast.Return):
                    add(sub.lineno, node.name, sub.value)
                stack.extend(ast.iter_child_nodes(sub))
        elif isinstance(node, ast.Lambda):
            add(node.lineno, "lambda", node.body)
        elif isinstance(node, ast.Call):
            name = _dotted(node.func) or ""
            last = name.rsplit(".", 1)[-1]
            if last in ("exit", "SystemExit", "_exit"):
                arg = node.args[0] if node.args else next(
                    (k.value for k in node.keywords if k.arg in ("code", "status")), None)
                add(node.lineno, name, arg)
            elif last == "Report":  # Report(doc, status)
                arg = node.args[1] if len(node.args) > 1 else next(
                    (k.value for k in node.keywords if k.arg == "status"), None)
                add(node.lineno, name, arg)
            elif last in ("die_caller_error", "exit_caller_error"):
                add(node.lineno, name, next(
                    (k.value for k in node.keywords if k.arg == "code"), None))
    # a nested function's return is also walked from its parent: keep one
    return sorted(set(out))


def _extended_code_faults(sources: dict[str, str], registry, passthrough,
                          not_exit: frozenset[tuple[str, str]]) -> list[str]:
    """Every fault of the registry gate. Pure, so it is pinned on known shapes."""
    faults = []
    stems: dict[str, list[str]] = {}
    for rel in sources:
        stems.setdefault(Path(rel).stem, []).append(rel)
    for stem in sorted(registry):
        if stem not in stems:
            faults.append(f"EXTENDED_EXIT_CODES[{stem!r}]: no entry under the gated "
                          "trees has that name — remove the row")
        elif len(stems[stem]) > 1:
            faults.append(f"EXTENDED_EXIT_CODES[{stem!r}]: names {sorted(stems[stem])} "
                          "— the registry is keyed by file name, so it must be unique")
    excused = set()
    for rel, source in sorted(sources.items()):
        row = registry.get(Path(rel).stem, frozenset())
        for line, where, value in _extended_literals(ast.parse(source, filename=rel)):
            if (rel, where) in not_exit:
                excused.add((rel, where))
                continue
            # A PASSTHROUGH row forwards a status it did not choose; a literal
            # is one the wrapper chose itself, so it is held to 0/1/2 too.
            if row is not passthrough and value in row:
                continue
            faults.append(f"{rel}:{line}: {where} uses exit status {value}, which is not "
                          f"in its EXTENDED_EXIT_CODES row (scripts/tools/_lib_exitcodes.py)"
                          " — use 0/1/2, or register it (an owner decision, #3611)")
    for rel, where in sorted(not_exit - excused):
        faults.append(f"NOT_EXIT_STATUS {(rel, where)}: excuses nothing any more — remove it")
    return faults


def test_every_exit_status_outside_0_1_2_is_registered():
    import _lib_exitcodes as lib
    sources = _gated_sources()
    assert len(sources) >= 200, len(sources)
    faults = _extended_code_faults(sources, lib.EXTENDED_EXIT_CODES, lib.PASSTHROUGH,
                                   NOT_EXIT_STATUS)
    assert not faults, "\n".join(faults)


_REG = {"t": frozenset({3}), "p": "passthrough"}
_X = frozenset({("a/x.py", "v")})


@pytest.mark.parametrize("files, not_exit, want", [
    ({"a/t.py": "def main():\n    return 3\n", "a/p.py": ""}, frozenset(), []),
    ({"a/t.py": "def main():\n    return 4\n", "a/p.py": ""}, frozenset(),
     ["a/t.py:2: main uses exit status 4"]),
    ({"a/u.py": "def main():\n    return 3\n", "a/t.py": "", "a/p.py": ""}, frozenset(),
     ["a/u.py:2: main uses"]),
    ({"a/u.py": "def h():\n    return 127\n", "a/t.py": "", "a/p.py": ""}, frozenset(),
     ["a/u.py:2: h uses"]),
    ({"a/u.py": "import sys\nsys.exit(64)\n", "a/t.py": "", "a/p.py": ""}, frozenset(),
     ["a/u.py:2: sys.exit uses"]),
    ({"a/u.py": "raise SystemExit(-1)\n", "a/t.py": "", "a/p.py": ""}, frozenset(),
     ["a/u.py:1: SystemExit uses"]),
    ({"a/u.py": "EXIT_X = 3\n", "a/t.py": "", "a/p.py": ""}, frozenset(),
     ["a/u.py:1: EXIT_X uses"]),
    ({"a/u.py": "COUNT = 3\ndef f():\n    return 2\n", "a/t.py": "", "a/p.py": ""},
     frozenset(), []),
    ({"a/u.py": "RC_T = 3\ndef main():\n    return RC_T\n", "a/t.py": "", "a/p.py": ""},
     frozenset(), ["a/u.py:3: main uses exit status 3"]),
    ({"a/u.py": "def main(x):\n    return 4 if x else 0\n", "a/t.py": "", "a/p.py": ""},
     frozenset(), ["a/u.py:2: main uses exit status 4"]),
    ({"a/u.py": "import sys\nsys.exit(code=3)\n", "a/t.py": "", "a/p.py": ""},
     frozenset(), ["a/u.py:2: sys.exit uses exit status 3"]),
    ({"a/u.py": "def f():\n    _EXIT_T = 4\n", "a/t.py": "", "a/p.py": ""},
     frozenset(), ["a/u.py:2: _EXIT_T uses exit status 4"]),
    ({"a/u.py": "run(lambda: 3)\n", "a/t.py": "", "a/p.py": ""},
     frozenset(), ["a/u.py:1: lambda uses exit status 3"]),
    ({"a/u.py": "RC_T: int = 4\ndef main():\n    return RC_T\n", "a/t.py": "", "a/p.py": ""},
     frozenset(), ["a/u.py:3: main uses exit status 4"]),
    ({"a/u.py": "def main():\n    return Report({}, 3)\n", "a/t.py": "", "a/p.py": ""},
     frozenset(), ["a/u.py:2: Report uses exit status 3"]),
    ({"a/u.py": "def main():\n    die_caller_error('x', code=5)\n", "a/t.py": "", "a/p.py": ""},
     frozenset(), ["a/u.py:2: die_caller_error uses exit status 5"]),
    ({"a/t.py": "", "a/p.py": "",
      "a/x.py": "def main():\n    def v():\n        return 60\n    return 0\n"},
     frozenset({("a/x.py", "v")}), []),
    ({"a/p.py": "def main(rc):\n    return rc\n", "a/t.py": ""}, frozenset(), []),
    ({"a/p.py": "def main():\n    return 9\n", "a/t.py": ""}, frozenset(),
     ["a/p.py:2: main uses exit status 9"]),
    ({"a/t.py": "", "b/t.py": "", "a/p.py": ""}, frozenset(),
     ["EXTENDED_EXIT_CODES['t']: names"]),
    ({"a/p.py": ""}, frozenset(), ["EXTENDED_EXIT_CODES['t']: no entry"]),
    ({"a/t.py": "", "a/p.py": "", "a/x.py": "def v():\n    return 60\n"}, _X, []),
    ({"a/t.py": "", "a/p.py": ""}, _X, ["NOT_EXIT_STATUS ('a/x.py', 'v'): excuses nothing"]),
], ids=["registered", "unregistered", "other-file", "helper-return", "sys-exit",
        "negative", "exit-constant", "not-exit-shaped", "module-int-name", "ternary",
        "exit-keyword", "local-private-exit-constant", "lambda", "annotated-module-int",
        "report-status", "die-caller-error-code", "nested-def-is-its-own", "passthrough",
        "passthrough-own-literal", "stem-collision",
        "row-without-file", "excused", "stale-excuse"])
def test_the_registry_gate_on_known_shapes(files, not_exit, want):
    faults = _extended_code_faults(files, _REG, "passthrough", not_exit)
    assert len(faults) == len(want), faults
    for fault, prefix in zip(faults, want):
        assert fault.startswith(prefix), (fault, prefix)


# ═══════════════════════════════════════════════════════════════════════════
# #3599 — a stderr write that fails loses its text, not the run (owner ruling W)
# ═══════════════════════════════════════════════════════════════════════════
# The tool says something on stderr, then delivers its stdout and ends with
# its own code. stderr cannot take it: its reader left, or it is /dev/full.
_STDERR_FAILS = ("reader-gone", "dev-full")


def _with_broken_stderr(tmp_path: Path, body: str, how: str, buffering: str,
                        name: str = "tool.py",
                        env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    script = _script(tmp_path, body, name=name)
    env = dict(_BUFFERING[buffering] if env is None else env)
    env.pop("DA_TEST_CRASH_EXIT_CODE", None)
    if how == "dev-full":
        if not os.path.exists("/dev/full"):
            pytest.skip("/dev/full does not exist here")
        with open("/dev/full", "wb") as full:
            return subprocess.run([sys.executable, str(script)], stdout=subprocess.PIPE,
                                  stderr=full.fileno(), timeout=60, env=env)
    r, w = os.pipe()
    os.close(r)
    try:
        return subprocess.run([sys.executable, str(script)], stdout=subprocess.PIPE,
                              stderr=w, timeout=60, env=env)
    finally:
        os.close(w)


def _says_then_ends(code: str, size: str = "10") -> str:
    return _armed("print('progress ' * " + size + ", file=sys.stderr)\n"
                  "print('the answer')\n"
                  f"return {code}\n")


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
@pytest.mark.parametrize("how", _STDERR_FAILS)
@pytest.mark.parametrize("size", ["10", "io.DEFAULT_BUFFER_SIZE"])
def test_a_lost_stderr_turns_a_clean_0_into_2_and_the_run_goes_on(tmp_path, how, buffering, size):
    """W: the write does not end the tool — its stdout still arrives — and
    the 0 it ends with is not the whole answer: 2 (gnulib close_stdout's
    "never 0"). Before #3599 the first stderr write crashed the run: rc 2
    from the crash path, and no stdout."""
    proc = _with_broken_stderr(tmp_path, _says_then_ends("0", size), how, buffering)
    assert proc.returncode == 2
    assert proc.stdout == b"the answer\n"


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
@pytest.mark.parametrize("how", _STDERR_FAILS)
@pytest.mark.parametrize("code", [1, 2, 3])
def test_a_lost_stderr_keeps_a_non_zero_code(tmp_path, how, buffering, code):
    """W: 1 (a finding), 2 and a registered 3 keep their meaning — under
    the crash path they all became the crash code."""
    proc = _with_broken_stderr(tmp_path, _says_then_ends(str(code)), how, buffering,
                               name=_CODED_TOOL)
    assert proc.returncode == code
    assert proc.stdout == b"the answer\n"


@pytest.mark.parametrize("how", _STDERR_FAILS)
def test_a_lost_stderr_keeps_a_best_effort_0(tmp_path, how):
    """W: a Report delivered best effort (patch-config) keeps even its 0."""
    body = _armed("print('progress', file=sys.stderr)\n"
                  "return _lib_exitcodes.Report({'n': 1}, 0, delivery='best_effort')\n")
    proc = _with_broken_stderr(tmp_path, body, how, "buffered", name=_CODED_TOOL)
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"n": 1}


@pytest.mark.parametrize("how", _STDERR_FAILS)
def test_a_lost_stderr_under_the_hook_protocol_does_not_block(tmp_path, how):
    """A session guard's 0 becomes the hook protocol's caller-error code,
    1 ("non-blocking error") — 2 would block the tool call."""
    body = "_lib_exitcodes.use_hook_protocol()\n" + _says_then_ends("0")
    proc = _with_broken_stderr(tmp_path, body, how, "buffered")
    assert proc.returncode == 1
    assert proc.stdout == b"the answer\n"


@pytest.mark.parametrize("how", _STDERR_FAILS)
def test_a_crash_with_a_lost_stderr_keeps_the_crash_code(tmp_path, how):
    """A crash is still a crash: its code, with or without the test override."""
    body = _armed("print('progress', file=sys.stderr)\nraise RuntimeError('boom')\n")
    env = dict(_ENV, DA_TEST_CRASH_EXIT_CODE="77")
    script = _script(tmp_path, body)
    if how == "dev-full":
        if not os.path.exists("/dev/full"):
            pytest.skip("/dev/full does not exist here")
        with open("/dev/full", "wb") as full:
            proc = subprocess.run([sys.executable, str(script)], stdout=subprocess.PIPE,
                                  stderr=full.fileno(), timeout=60, env=env)
    else:
        r, w = os.pipe()
        os.close(r)
        try:
            proc = subprocess.run([sys.executable, str(script)], stdout=subprocess.PIPE,
                                  stderr=w, timeout=60, env=env)
        finally:
            os.close(w)
    assert proc.returncode == 77


def test_a_lost_stderr_at_import_time_is_counted(tmp_path):
    """A module-level stderr line (before main) is the run's text too."""
    body = ("print('module-level note', file=sys.stderr)\n"
            "_lib_exitcodes.run(lambda: print('the answer'))\n")
    proc = _with_broken_stderr(tmp_path, body, "reader-gone", "buffered")
    assert proc.returncode == 2
    assert proc.stdout == b"the answer\n"


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
def test_a_healthy_stderr_is_unchanged(tmp_path, buffering):
    """Control: stderr takes the text — bytes as written, 0 stays 0, and
    sys.stderr is still sys.__stderr__ (redirect_stdout(sys.stderr) and
    identity checks see one object) and still names fd 2."""
    body = _armed("print('progress \u2713 \u4e2d', file=sys.stderr)\n"
                  "assert sys.stderr is sys.__stderr__\n"
                  "assert sys.stderr.fileno() == 2\n"
                  "print('the answer')\n"
                  "return 0\n")
    proc = _run(_script(tmp_path, body), env=dict(_BUFFERING[buffering], PYTHONIOENCODING="utf-8"))
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == "progress \u2713 \u4e2d\n".encode("utf-8")
    assert proc.stdout == b"the answer\n"


def test_the_replaced_stderr_keeps_the_interpreters_attributes(tmp_path):
    """`name`, `mode`, and `tell()` on a file: as the stream it replaced."""
    script = tmp_path / "tool.py"
    script.write_text(
        "import sys\n"
        "before = (sys.stderr.name, sys.stderr.mode, sys.stderr.isatty())\n"
        "sys.stderr.write('x'); sys.stderr.flush()\n"
        f"sys.path.insert(0, {str(TOOLS_DIR)!r})\n"
        "import _lib_exitcodes\n"
        "assert _lib_exitcodes._ORIGINAL_STDERR is not None\n"
        "after = (sys.stderr.name, sys.stderr.mode, sys.stderr.isatty())\n"
        "assert before == after, (before, after)\n"
        "sys.stderr.write('y'); sys.stderr.flush()\n"
        "assert sys.stderr.tell() == 2\n", encoding="utf-8")
    with open(tmp_path / "err", "wb") as err:
        proc = subprocess.run([sys.executable, str(script)], stderr=err, timeout=60, env=_ENV)
    assert proc.returncode == 0, (tmp_path / "err").read_text(encoding="utf-8")


def test_a_second_copy_of_the_module_still_sees_the_loss(tmp_path):
    """A copy loaded by path finds the first copy's wrapper (no double wrap)
    and its run() reads the loss that wrapper recorded."""
    body = (f"import importlib.util\n"
            f"spec = importlib.util.spec_from_file_location('lib2', {str(TOOLS_DIR / '_lib_exitcodes.py')!r})\n"
            "lib2 = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(lib2)\n"
            "assert lib2._ORIGINAL_STDERR is None\n"
            "def main():\n"
            "    print('progress', file=sys.stderr)\n"
            "    return 0\n"
            "lib2.run(main)\n")
    proc = _with_broken_stderr(tmp_path, body, "dev-full", "buffered")
    assert proc.returncode == 2


def test_a_rebound_stderr_is_left_alone(tmp_path):
    """Only the interpreter's own stderr is replaced: a stderr the caller
    rebound before the import (a test harness's capture) is not touched."""
    script = tmp_path / "tool.py"
    script.write_text(
        "import io, sys\n"
        "mine = io.StringIO()\n"
        "sys.stderr = mine\n"
        f"sys.path.insert(0, {str(TOOLS_DIR)!r})\n"
        "import _lib_exitcodes\n"
        "assert sys.stderr is mine\n"
        "assert _lib_exitcodes._ORIGINAL_STDERR is None\n", encoding="utf-8")
    proc = _run(script)
    assert proc.returncode == 0, proc.stderr
