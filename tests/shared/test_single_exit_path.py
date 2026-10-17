"""One exit path for da-tools CLI tools (#3124, #3115).

Two contracts of ``scripts/tools/_lib_exitcodes.py``, both observable only at
the end of a REAL interpreter (atexit, excepthook, the final stdout flush), so
every behavioural case runs a subprocess:

* **A reader that closed stdout early is not a crash (#3124).** ``tool |
  head -c1``, ``tool | true``, a closed fd 1: whatever the tool was about to
  exit with — 0, 1, a named caller error, a crash — the process used to end
  at rc **120** (the interpreter's own "could not flush stdout" status, outside
  the 0/1/2 contract), and a large write into a closed pipe was reported as
  ``ERROR: unexpected internal error (BrokenPipeError)``. Now the process
  ends at the CALLER-ERROR code (2; tenant_verify's 1) with one line,
  ``ERROR: stdout was closed before all output was written``. A
  ``BrokenPipeError`` that did NOT come from stdout (a socket) is still a
  crash — the control case below.

* **Every rc-2 exit of a ``--json`` tool prints one envelope (#3115).** A tool
  that armed its envelope (``arm_crash_envelope``) and then leaves through a
  caller-error exit — ``die_caller_error``, a shared ``exit_on_*`` decorator,
  a raised ``CallerError``, or a hand-written ``sys.exit(2)`` reaching
  ``_lib_exitcodes.run`` — gets exactly one ``{"status": "caller_error",
  "reason": <named>, "error": <line>}`` document on stdout, unless it already
  wrote its own. The per-tool recipes live in test_json_stdout_contract.py;
  this file pins the mechanism and the AST gate that every armed tool enters
  through ``_lib_exitcodes.run``.
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

STDOUT_CLOSED_LINE = "ERROR: stdout was closed before all output was written"
CRASH_MARK = "unexpected internal error"

# Run without PYTHONUNBUFFERED, as CI does: the 120 only appears when the
# tool's own output is still sitting in the block buffer at exit.
_ENV = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}

_PRELUDE = (
    "import os, sys\n"
    "sys.path.insert(0, {tools!r})\n"
    "import _lib_exitcodes\n"
)


def _script(tmp_path: Path, body: str, name: str = "tool.py") -> Path:
    script = tmp_path / name
    script.write_text(_PRELUDE.format(tools=str(TOOLS_DIR)) + body, encoding="utf-8")
    return script


def _run(script: Path, stdout: int = subprocess.PIPE) -> subprocess.CompletedProcess:
    """Bytes out (no text mode): stdout is compared byte for byte."""
    return subprocess.run(
        [sys.executable, str(script)], stdout=stdout, stderr=subprocess.PIPE,
        timeout=60, env=_ENV)


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
        work = "print('partial report')\nos.close(1)\n" + ending
    elif closure == "reader-gone":
        work = "print('partial report')\n" + ending
    else:  # big-output-into-head: the write itself fails, before the ending
        work = "print('x' * 1_000_000)\n" + ending
    if entry == "module-level":
        return work
    indented = "".join("    " + line + "\n" for line in work.splitlines())
    return "def main():\n" + indented + "_lib_exitcodes.run(main)\n"


def _run_closed(tmp_path: Path, body: str, closure: str) -> tuple[int, str]:
    """Run *body* with stdout closed the *closure* way; (rc, stderr)."""
    script = _script(tmp_path, body)
    if closure == "fd-closed":
        proc = _run(script)
        return proc.returncode, proc.stderr.decode("utf-8", "replace")
    if closure == "reader-gone":
        # A pipe whose read end is already closed: `tool | true` after `true`
        # has exited. Deterministic — no race with a reader process.
        r, w = os.pipe()
        os.close(r)
        try:
            proc = _run(script, stdout=w)
        finally:
            os.close(w)
        return proc.returncode, proc.stderr.decode("utf-8", "replace")
    head = shutil.which("head")
    if head is None:
        pytest.skip("`head` is not on PATH")
    tool = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=_ENV)
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


@pytest.mark.parametrize("entry", _ENTRIES)
@pytest.mark.parametrize("closure", ["fd-closed", "reader-gone", "big-output-into-head"])
@pytest.mark.parametrize("ending", sorted(_ENDINGS))
def test_closed_stdout_ends_at_the_caller_error_code(tmp_path, ending, closure, entry):
    """Any ending × any way of losing stdout ⇒ rc 2 and the named line, never
    120; and only a real crash prints the crash line.

    In the ``big-output-into-head`` column the write fails before the ending
    runs, so every row there is "a BrokenPipeError from stdout" — which must
    not be reported as a crash either.
    """
    rc, err = _run_closed(tmp_path, _body(_ENDINGS[ending], closure, entry), closure)
    where = f"{ending} × {closure} × {entry}: rc={rc}\nstderr:\n{err[-1500:]}"
    assert rc == 2, where
    assert STDOUT_CLOSED_LINE in err, where
    assert err.count(STDOUT_CLOSED_LINE) == 1, where
    assert "Exception ignored" not in err, where
    if ending != "crash" or closure == "big-output-into-head":
        assert CRASH_MARK not in err, where
        assert "Traceback" not in err, where


def test_closed_stdout_uses_the_caller_error_code_not_the_crash_code(tmp_path):
    """The two codes are separate: a tool that moves its crash code (#3092
    will, under tests) still ends a closed-stdout run at its caller-error
    code; a tool whose caller-error code is 1 (tenant_verify) ends at 1."""
    body = ("_lib_exitcodes.set_crash_exit_code(70)\n"
            "print('partial report')\nos.close(1)\nraise RuntimeError('boom')\n")
    rc, err = _run_closed(tmp_path, body, "fd-closed")
    assert rc == 2, err
    body = ("_lib_exitcodes.set_caller_error_code(1)\n"
            "print('partial report')\nos.close(1)\nsys.exit(0)\n")
    rc, err = _run_closed(tmp_path, body, "fd-closed")
    assert rc == 1, err
    assert STDOUT_CLOSED_LINE in err


def test_tenant_verify_ends_a_closed_stdout_at_its_own_caller_error_code():
    """tenant_verify's 2 means "hash mismatch" (a shipped rollback runbook
    keys on it), so its reader-gone exit is its caller-error code, 1."""
    r, w = os.pipe()
    os.close(r)
    try:
        proc = subprocess.run(
            [sys.executable, str(TOOLS_DIR / "dx" / "tenant_verify.py"), "--help"],
            stdout=w, stderr=subprocess.PIPE, timeout=60, env=_ENV)
    finally:
        os.close(w)
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 1, err
    assert STDOUT_CLOSED_LINE in err


@pytest.mark.parametrize("entry", _ENTRIES)
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
# run in-process as __main__ (tenant_verify sets 1) must not leak it into the
# next test of the same worker — measured, it turned reword_chain's
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
# #3115 — every caller-error exit of an armed tool prints one envelope
# ═══════════════════════════════════════════════════════════════════════════
def _armed(body: str, build: str = "None", fields: str = "") -> str:
    """A `--json` tool: arm, then *body* inside main, entered through run()."""
    indented = "".join("    " + line + "\n" for line in body.splitlines())
    return (f"def main():\n"
            f"    _lib_exitcodes.arm_crash_envelope({build}{', ' if fields else ''}{fields})\n"
            f"{indented}"
            f"_lib_exitcodes.run(main)\n")


def _json_run(tmp_path: Path, body: str) -> tuple[int, str, str]:
    proc = _run(_script(tmp_path, body))
    return (proc.returncode, proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


def test_hand_written_exit_2_gets_the_generic_envelope(tmp_path):
    """`print(...stderr); sys.exit(2)` — no named reason, but the envelope
    carries the line the tool printed (alert_quality --period bogus)."""
    rc, out, err = _json_run(tmp_path, _armed(
        "print(\"Error: invalid period 'bogus'\", file=sys.stderr)\nsys.exit(2)\n",
        fields="tool='t'"))
    assert rc == 2, err
    assert json.loads(out) == {"tool": "t", "status": "caller_error",
                               "reason": "caller_error",
                               "error": "Error: invalid period 'bogus'"}


def test_die_caller_error_names_its_reason(tmp_path):
    rc, out, err = _json_run(tmp_path, _armed(
        "_lib_exitcodes.die_caller_error('ERROR: cannot read x.yaml: bad', "
        "reason='yaml_file_unreadable')\n",
        build="lambda: {'total': 0, 'reason': 'zeroed'}"))
    assert rc == 2, err
    assert json.loads(out) == {"total": 0, "status": "caller_error",
                               "reason": "yaml_file_unreadable",
                               "error": "ERROR: cannot read x.yaml: bad"}
    assert err.strip() == "ERROR: cannot read x.yaml: bad"


@pytest.mark.parametrize("entry", ["run", "excepthook"])
def test_raised_caller_error_is_not_a_crash(tmp_path, entry):
    """`raise CallerError(message, reason)`: rc 2, ONE stderr line, no
    traceback, and the envelope names the reason — whether it reaches
    run() or the module-level excepthook."""
    raise_it = "raise _lib_exitcodes.CallerError('prometheus unreachable', reason='prometheus_query_error')\n"
    body = (_armed(raise_it) if entry == "run" else
            "_lib_exitcodes.arm_crash_envelope()\n" + raise_it)
    rc, out, err = _json_run(tmp_path, body)
    assert rc == 2, err
    assert err.strip().splitlines() == ["ERROR: prometheus unreachable"], err
    assert json.loads(out) == {"status": "caller_error", "reason": "prometheus_query_error",
                               "error": "ERROR: prometheus unreachable"}


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
    assert json.loads(out)["reason"] == "prometheus_query_error"


def test_caller_error_follows_the_caller_error_code(tmp_path):
    rc, out, err = _json_run(tmp_path, (
        "_lib_exitcodes.set_caller_error_code(1)\n"
        "raise _lib_exitcodes.CallerError('bad')\n"))
    assert rc == 1, err
    assert out == ""


def test_a_tools_own_document_is_not_doubled(tmp_path):
    """The tool printed its own caller_error document and exits 2 without
    disarming: nothing is added (patch_config's early exits)."""
    rc, out, err = _json_run(tmp_path, _armed(
        "print(json.dumps({'status': 'caller_error', 'reason': 'own'}))\nsys.exit(2)\n"
        .replace("json.dumps", "__import__('json').dumps")))
    assert rc == 2, err
    assert json.loads(out) == {"status": "caller_error", "reason": "own"}


def test_a_crash_after_the_tools_own_document_adds_none(tmp_path):
    rc, out, err = _json_run(tmp_path, _armed("print('{}')\nraise RuntimeError('late')\n"))
    assert rc == 2, err
    assert out == "{}\n"
    assert CRASH_MARK in err


def test_unarmed_exit_2_prints_nothing(tmp_path):
    """Not --json: the stdout of a caller error stays empty."""
    rc, out, err = _json_run(tmp_path, (
        "def main():\n    _lib_exitcodes.die_caller_error('ERROR: x', reason='y')\n"
        "_lib_exitcodes.run(main)\n"))
    assert (rc, out, err.strip()) == (2, "", "ERROR: x")


@pytest.mark.parametrize("code", ["0", "1", "3", "None"])
def test_other_exit_codes_pass_through_without_an_envelope(tmp_path, code):
    for how in (f"sys.exit({code})\n", f"return {code}\n"):
        rc, out, err = _json_run(tmp_path, _armed(how))
        assert rc == (0 if code == "None" else int(code)), (how, err)
        assert out == "", how


def test_run_returns_the_mains_value_as_the_exit_status(tmp_path):
    rc, out, err = _json_run(tmp_path, "def main():\n    return 2\n_lib_exitcodes.run(main)\n")
    assert (rc, out, err) == (2, "", "")


def test_the_stdout_tap_changes_nothing_a_tool_can_see(tmp_path):
    """Arming wraps stdout's write to notice the tool's own document; the
    stream is the same object, its buffer, fileno and encoding unchanged,
    and write still returns the count."""
    rc, out, err = _json_run(tmp_path, (
        "before = (sys.stdout, sys.stdout.buffer, sys.stdout.fileno(), sys.stdout.encoding)\n"
        "_lib_exitcodes.arm_crash_envelope()\n"
        "after = (sys.stdout, sys.stdout.buffer, sys.stdout.fileno(), sys.stdout.encoding)\n"
        "assert before == after, (before, after)\n"
        "n = sys.stdout.write('abc')\n"
        "print(n, file=sys.stderr)\n"
        "_lib_exitcodes.arm_crash_envelope()\n"   # arming twice taps once
        "sys.stdout.write('d\\n')\n"))
    assert (rc, out, err.strip()) == (0, "abcd\n", "3"), err


# ═══════════════════════════════════════════════════════════════════════════
# AST gate: every armed tool enters through _lib_exitcodes.run
# ═══════════════════════════════════════════════════════════════════════════
def _calls_arm(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name == "arm_crash_envelope":
                return True
    return False


def _is_main_guard(node: ast.stmt) -> bool:
    return (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__"
            and len(node.test.comparators) == 1
            and isinstance(node.test.comparators[0], ast.Constant)
            and node.test.comparators[0].value == "__main__")


def _armed_tools() -> dict[str, ast.Module]:
    out = {}
    for d in (TOOLS_DIR, OPS_DIR):
        for f in sorted(d.glob("*.py")):
            if f.name == "_lib_exitcodes.py":
                continue
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
            if _calls_arm(tree):
                out[str(f.relative_to(REPO_ROOT))] = tree
    return out


def _guard_problem(tree: ast.Module) -> str | None:
    guards = [n for n in tree.body if _is_main_guard(n)]
    if len(guards) != 1:
        return f"expected one `if __name__ == \"__main__\":` guard, found {len(guards)}"
    last = guards[0].body[-1]
    call = last.value if isinstance(last, ast.Expr) else None
    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and call.func.attr == "run" and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "_lib_exitcodes"
            and len(call.args) == 1 and isinstance(call.args[0], ast.Name)
            and not call.keywords):
        return f"the guard must end with `_lib_exitcodes.run(<entry>)`, got `{ast.unparse(last)}`"
    entry = call.args[0].id
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    if entry not in defined:
        return f"`{entry}` is not a module-level function"
    for stmt in guards[0].body[:-1]:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == entry:
                return f"`{entry}` is also called outside run(): `{ast.unparse(stmt)}`"
    return None


def test_every_armed_tool_enters_through_run():
    """A `--json` tool that arms its envelope must reach `_lib_exitcodes.run`:
    that is where a hand-written `sys.exit(2)` gets its envelope and where a
    `CallerError` stops being a crash. Without it the tool would print
    nothing on those paths and only a recipe for that exact path would see."""
    tools = _armed_tools()
    # anti-vacuity: the population is the --json tools, both directories
    assert "scripts/tools/validate_all.py" in tools
    assert "scripts/tools/ops/alert_quality.py" in tools
    assert len(tools) >= 40, sorted(tools)
    bad = {path: why for path, tree in tools.items() if (why := _guard_problem(tree))}
    assert not bad, "\n".join(f"{p}: {w}" for p, w in sorted(bad.items()))


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
