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
def test_closed_stdout_ends_at_the_caller_error_code(tmp_path, ending, closure, entry,
                                                     buffering):
    """Any ending × any way of losing stdout × either buffering ⇒ rc 2 and
    the named line, never 120; and only a real crash prints the crash line.

    Where the write fails before the ending runs — the ``big-output-into-head``
    column, and every column when unbuffered — every row is "a write to a
    lost stdout raised" (BrokenPipeError, or EBADF on a closed fd), which
    must not be reported as a crash either.
    """
    rc, err = _run_closed(tmp_path, _body(_ENDINGS[ending], closure, entry), closure,
                          _BUFFERING[buffering])
    where = f"{ending} × {closure} × {entry} × {buffering}: rc={rc}\nstderr:\n{err[-1500:]}"
    assert rc == 2, where
    assert STDOUT_CLOSED_LINE in err, where
    assert err.count(STDOUT_CLOSED_LINE) == 1, where
    assert "Exception ignored" not in err, where
    if ending != "crash" or closure == "big-output-into-head" or buffering == "unbuffered":
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


@pytest.mark.real_crash_exit_code
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
    # 39 since migration-audit was deleted (#3419). Not derived from
    # test_json_stdout_contract.JSON_TOOLS: that set is "declares a --json
    # flag", this one "arms the envelope" — equal today, not by definition.
    assert len(tools) >= 39, sorted(tools)
    bad = {path: why for path, tree in tools.items() if (why := _guard_problem(tree))}
    assert not bad, "\n".join(f"{p}: {w}" for p, w in sorted(bad.items()))


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


# ═══════════════════════════════════════════════════════════════════════════
# Review round (a19e90df): `>&-`, other write errors, a tool's own handler,
# one-line envelope errors, redirected stdout, bypassing the stdout tap
# ═══════════════════════════════════════════════════════════════════════════
def _run_with_fds_closed(script: Path, *, close: str) -> subprocess.CompletedProcess:
    """`tool >&-` / `tool 2>&-`: the shell closes the fd before Python starts,
    so `sys.stdout` (or `sys.stderr`) is None in the tool."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not on PATH")
    return subprocess.run(
        [bash, "-c", f'exec "$0" "$1" {close}', sys.executable, str(script)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, env=_ENV)


_ARMED_EXITS = {
    "die_caller_error": "_lib_exitcodes.die_caller_error('ERROR: bad input', reason='r')\n",
    "exit_caller_error": ("print('ERROR: bad input', file=sys.stderr)\n"
                          "_lib_exitcodes.exit_caller_error('ERROR: bad input', reason='r')\n"),
    "hand-written": "print('ERROR: bad input', file=sys.stderr)\nreturn 2\n",
    "crash": "raise RuntimeError('boom')\n",
}


@pytest.mark.parametrize("how", sorted(_ARMED_EXITS))
@pytest.mark.real_crash_exit_code
def test_shell_closed_stdout_is_not_a_crash(tmp_path, how):
    """`tool --json >&-`: sys.stdout is None. There is nowhere to put the
    envelope; the exit must still be the tool's own (rc 2, its stderr line),
    not a crash in the envelope printer."""
    proc = _run_with_fds_closed(_script(tmp_path, _armed(_ARMED_EXITS[how])), close=">&-")
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 2, err
    assert "crash envelope failed" not in err and "AttributeError" not in err, err
    if how == "crash":
        assert err.count("Traceback") == 1, err
    else:
        assert "Traceback" not in err and CRASH_MARK not in err, err
        assert "ERROR: bad input" in err, err
    assert STDOUT_CLOSED_LINE not in err, err  # nothing was lost: stdout never existed


@pytest.mark.parametrize("how", ["die_caller_error", "hand-written"])
def test_shell_closed_stderr_keeps_stdout_one_document(tmp_path, how):
    """`tool --json 2>&-`: sys.stderr is None. The stderr line must not land
    on stdout (print(file=None) would), so stdout stays one document."""
    proc = _run_with_fds_closed(_script(tmp_path, _armed(_ARMED_EXITS[how])), close="2>&-")
    assert proc.returncode == 2
    doc = json.loads(proc.stdout)
    assert doc["status"] == "caller_error", proc.stdout


def test_a_full_device_is_named_as_a_write_error(tmp_path):
    """`tool > /dev/full`: the exit flush fails with ENOSPC — the reader did
    not close anything, so the line names the error, not a closed stdout."""
    if not os.path.exists("/dev/full"):
        pytest.skip("/dev/full does not exist here")
    with open("/dev/full", "wb") as full:
        proc = _run(_script(tmp_path, "print('partial report')\nsys.exit(0)\n"), stdout=full.fileno())
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 2, err
    assert "ERROR: cannot write to stdout: No space left on device" in err, err
    assert STDOUT_CLOSED_LINE not in err, err


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
@pytest.mark.parametrize("exit_fn", ["die_caller_error", "exit_caller_error"])
def test_a_lost_stdout_caught_by_the_tools_own_handler(tmp_path, exit_fn, buffering):
    """A tool's own `except Exception` (config_diff, operator_generate) turns
    the BrokenPipeError of a lost stdout into its named caller error. That is
    still the reader leaving: the closed-stdout exit, not the tool's reason."""
    body = _armed(
        "try:\n"
        "    print('x' * 1_000_000)\n"
        "except Exception as exc:\n"
        f"    _lib_exitcodes.{exit_fn}(f'ERROR: report failed: {{exc}}', reason='compare_failed')\n")
    rc, err = _run_closed(tmp_path, body, "reader-gone", _BUFFERING[buffering])
    assert rc == 2, err
    assert STDOUT_CLOSED_LINE in err and "report failed" not in err, err
    assert CRASH_MARK not in err and "Traceback" not in err, err


def test_a_handler_for_another_os_error_still_names_its_reason(tmp_path):
    """Control: an OSError that is not stdout's (a missing file) keeps the
    tool's own message and envelope."""
    rc, out, err = _json_run(tmp_path, _armed(
        "try:\n"
        "    open('/nonexistent/x/y')\n"
        "except Exception as exc:\n"
        "    _lib_exitcodes.die_caller_error(f'ERROR: cannot read: {exc}', reason='compare_failed')\n"))
    assert rc == 2, err
    assert json.loads(out)["reason"] == "compare_failed"
    assert STDOUT_CLOSED_LINE not in err


def test_the_generic_envelope_names_the_error_line_not_the_last_line(tmp_path):
    """A hint printed after the ERROR line is not the error; control
    characters (an ANSI escape from a file name) do not reach the JSON."""
    rc, out, err = _json_run(tmp_path, _armed(
        "print('ERROR: cannot read \\x1b[31mevil\\x1b[0m.yaml', file=sys.stderr)\n"
        "print('  hint: fix the file and re-run', file=sys.stderr)\nreturn 2\n"))
    assert rc == 2, err
    error = json.loads(out)["error"]
    assert error.startswith("ERROR: cannot read ") and "evil" in error, error
    assert "\x1b" not in error and "hint" not in error, error


def test_a_multi_line_message_is_one_line_in_the_envelope(tmp_path):
    rc, out, err = _json_run(tmp_path, _armed(
        "_lib_exitcodes.die_caller_error('ERROR: two problems:\\n  a\\n  b', reason='r')\n"))
    assert rc == 2, err
    assert json.loads(out)["error"] == "ERROR: two problems: a b"
    assert "  a\n  b" in err  # stderr keeps the tool's own layout


def test_the_envelope_goes_to_the_armed_stdout_under_redirect_stdout(tmp_path):
    """patch_config / policy_engine run parts of main under
    `redirect_stdout(sys.stderr)`; an exit from inside must still put the
    envelope on the real stdout, not on stderr."""
    rc, out, err = _json_run(tmp_path, _armed(
        "import contextlib\n"
        "with contextlib.redirect_stdout(sys.stderr):\n"
        "    _lib_exitcodes.die_caller_error('ERROR: boom', reason='x')\n"))
    assert rc == 2, err
    assert json.loads(out)["reason"] == "x"
    assert '"status"' not in err, err


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
def _stdout_bypasses(tree: ast.AST) -> list[str]:
    """Writes an armed tool could make to stdout without `sys.stdout.write`:
    `sys.stdout.buffer`, `os.write` to fd 1 / stdout's fileno, rebinding
    `sys.stdout` (also via `setattr`), and the aliases that would hide those
    from this walk (`from sys import stdout`, `import sys as x`). Binding
    the stdout OBJECT to another name (`out = sys.stdout`) is not seen — a
    documented limit; writes through it still go through the tap.
    (`contextlib.redirect_stdout` is fine: the envelope goes to the stdout
    object that was armed.)"""
    def is_sys_stdout(n: ast.AST) -> bool:
        return (isinstance(n, ast.Attribute) and n.attr == "stdout"
                and isinstance(n.value, ast.Name) and n.value.id == "sys")
    found = []
    for n in ast.walk(tree):
        # aliases that would hide the shapes below from this walk
        if isinstance(n, ast.ImportFrom) and n.module == "sys" and any(
                a.name in ("stdout", "__stdout__") for a in n.names):
            found.append(f"{n.lineno}: from sys import stdout")
        elif isinstance(n, ast.Import) and any(
                a.name == "sys" and a.asname not in (None, "sys") for a in n.names):
            found.append(f"{n.lineno}: import sys as <alias>")
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id == "setattr" and len(n.args) >= 2
              and isinstance(n.args[0], ast.Name) and n.args[0].id == "sys"
              and isinstance(n.args[1], ast.Constant) and n.args[1].value == "stdout"):
            found.append(f"{n.lineno}: setattr(sys, 'stdout', ...)")
        elif isinstance(n, ast.Attribute) and n.attr == "buffer" and is_sys_stdout(n.value):
            found.append(f"{n.lineno}: sys.stdout.buffer")
        elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            if any(is_sys_stdout(t) for t in targets):
                found.append(f"{n.lineno}: sys.stdout rebound")
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "write" and isinstance(n.func.value, ast.Name)
              and n.func.value.id == "os" and n.args):
            fd = n.args[0]
            if (isinstance(fd, ast.Constant) and fd.value == 1) or "stdout" in ast.unparse(fd):
                found.append(f"{n.lineno}: {ast.unparse(n)[:60]}")
    return found


def test_armed_tools_write_stdout_only_through_sys_stdout():
    """The tap that keeps a second document off stdout sees `sys.stdout.write`
    (print, json.dump) only. A tool that writes around it would get the
    library's envelope after its own document."""
    tools = _armed_tools()
    assert len(tools) >= 39  # 39 since migration-audit was deleted (#3419); see above
    bad = {p: b for p, tree in tools.items() if (b := _stdout_bypasses(tree))}
    assert not bad, bad


@pytest.mark.parametrize("src, n", [
    ("sys.stdout.buffer.write(b'x')\n", 1),
    ("os.write(1, b'x')\n", 1),
    ("os.write(sys.stdout.fileno(), b'x')\n", 1),
    ("sys.stdout = open('f', 'w')\n", 1),
    ("os.write(fd, b'x')\nsys.stdout.write('x')\nprint('x')\n", 0),
    ("with contextlib.redirect_stdout(sys.stderr):\n    pass\n", 0),
])
def test_stdout_bypass_predicate_on_known_shapes(src, n):
    assert len(_stdout_bypasses(ast.parse(src))) == n


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
@pytest.mark.real_crash_exit_code
def test_a_socket_epipe_crash_while_the_reader_is_gone_is_a_crash(tmp_path, buffering):
    body = _armed(_SOCKET_EPIPE)
    rc, err = _run_closed(tmp_path, body, "reader-gone", _BUFFERING[buffering])
    assert rc == 2, err
    assert f"{CRASH_MARK} (BrokenPipeError)" in err and "Traceback" in err, err


def test_another_fd_ebadf_while_stdout_is_closed_is_a_crash(tmp_path):
    body = _armed("os.write(99, b'x')\n")
    rc, err = _run_closed(tmp_path, body, "reader-gone", _UNBUFFERED_ENV)
    assert f"{CRASH_MARK} (OSError)" in err, err


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
def test_a_wrapped_stdout_failure_is_still_the_reader_leaving(tmp_path, buffering):
    """N2: the handler sees `RuntimeError(...) from <BrokenPipeError>`; the
    chain leads back to stdout's own failure."""
    body = _armed(
        "try:\n"
        "    try:\n"
        "        print('x' * 1_000_000)\n"
        "        sys.stdout.flush()\n"
        "    except OSError as e:\n"
        "        raise RuntimeError('render failed') from e\n"
        "except RuntimeError as e:\n"
        "    _lib_exitcodes.die_caller_error(f'ERROR: {e}', reason='render_failed')\n")
    rc, err = _run_closed(tmp_path, body, "reader-gone", _BUFFERING[buffering])
    assert rc == 2, err
    assert STDOUT_CLOSED_LINE in err and "render failed" not in err, err


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
@pytest.mark.parametrize("how", ["uncaught", "handled"])
def test_a_full_device_mid_run_is_a_write_error_not_a_crash(tmp_path, how, buffering):
    """S2: more than a buffer's worth into /dev/full fails mid-run (ENOSPC):
    named, caller-error code, no traceback."""
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
    assert proc.returncode == 2, err
    assert "ERROR: cannot write to stdout: No space left on device" in err, err
    assert "Traceback" not in err and CRASH_MARK not in err, err


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
def test_a_broken_stderr_under_redirect_stdout_is_not_a_lost_stdout(tmp_path, buffering):
    """S3: inside `redirect_stdout(sys.stderr)` the progress lines go to
    stderr, whose reader left. stdout is healthy: the tool's own reason and
    its envelope, on stdout."""
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
    assert json.loads(proc.stdout)["reason"] == "progress_failed", proc.stdout


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
@pytest.mark.parametrize("size", ["small", "big"])
def test_the_lost_stdout_exit_honours_an_explicit_code(tmp_path, size, buffering):
    """N1: `die_caller_error(..., code=7)` from a stdout-loss handler keeps 7
    — also with bytes still buffered for the broken stdout (small output,
    buffered), which the exit must drop or the final flush fails again and
    replaces 7 with the caller-error code."""
    n = 100 if size == "small" else 1_000_000
    body = _armed(f"try:\n    print('x' * {n})\n    sys.stdout.flush()\nexcept OSError:\n"
                  "    _lib_exitcodes.die_caller_error('ERROR: x', reason='r', code=7)\n")
    rc, err = _run_closed(tmp_path, body, "reader-gone", _BUFFERING[buffering])
    assert rc == 7, err
    assert err.count(STDOUT_CLOSED_LINE) == 1, err


def test_the_envelope_error_keeps_wide_spaces_and_drops_format_characters(tmp_path):
    """N3: only line breaks / tabs fold; U+3000 and NBSP stay, so the
    envelope matches the stderr line; Cf characters (U+202E, U+200B) go."""
    rc, out, err = _json_run(tmp_path, _armed(
        "_lib_exitcodes.die_caller_error('ERROR: 設定檔　壞了\\u00a0x \\u202eevil\\u200b', reason='r')\n"))
    assert rc == 2, err
    assert json.loads(out)["error"] == "ERROR: 設定檔　壞了\u00a0x ?evil?"


def test_the_generic_error_line_must_start_with_error_colon(tmp_path):
    """N4: `ERRORS: 3` is a count, not the error line."""
    rc, out, err = _json_run(tmp_path, _armed(
        "print('ERROR: the real one', file=sys.stderr)\n"
        "print('ERRORS: 3 found', file=sys.stderr)\nreturn 2\n"))
    assert json.loads(out)["error"] == "ERROR: the real one"


@pytest.mark.parametrize("src, n", [
    ("from sys import stdout\n", 1),
    ("import sys as system\n", 1),
    ("setattr(sys, 'stdout', x)\n", 1),
    ("from sys import argv, path\nimport sys\n", 0),
])
def test_stdout_bypass_predicate_sees_aliases(src, n):
    """N5 (aliasing the stdout OBJECT itself stays a documented limit)."""
    assert len(_stdout_bypasses(ast.parse(src))) == n


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


@pytest.mark.parametrize("unarm", ["disarm", "envelope-consumed"])
def test_a_stdout_armed_earlier_is_not_flushed_once_unarmed(tmp_path, unarm):
    """M1: an in-process test arms with a stream that fails (a fake, a
    capture object) and puts the real stdout back. Once the arm is gone,
    that stream is nobody's stdout: the process must not end at rc 2 over it."""
    how = ("_lib_exitcodes.disarm_crash_envelope()\n" if unarm == "disarm"
           else "_lib_exitcodes._crash_envelope_armed = None\n")
    proc = _run(_script(tmp_path, _BROKEN_STREAM +
                        "sys.stdout = Broken()\n_lib_exitcodes.arm_crash_envelope()\n"
                        "sys.stdout = sys.__stdout__\n" + how + "print('fine')\n"))
    err = proc.stderr.decode("utf-8", "replace")
    assert (proc.returncode, proc.stdout) == (0, b"fine\n"), err
    assert "ERROR" not in err, err


_STATE_PROBE = "DA_EXIT_STATE_PROBE"


@pytest.mark.skipif(not os.environ.get(_STATE_PROBE), reason="driven by the test below")
def test_probe_leaves_every_piece_of_exit_state_dirty(monkeypatch):
    sys.path.insert(0, str(TOOLS_DIR))
    import _lib_exitcodes as ec  # noqa: E402
    ns: dict = {}
    exec(_BROKEN_STREAM, ns)
    monkeypatch.setattr(sys, "stdout", ns["Broken"]())
    ec.arm_crash_envelope()          # never disarmed: the conftest must
    ec.set_crash_exit_code(70)
    ec.set_caller_error_code(1)
    ec._record_stdout_failure(OSError(32, "Broken pipe"), sys.stdout)
    ec._stdout_closed_reported = True
    ec._lost_envelope = (OSError(32, "Broken pipe"), sys.stdout)


@pytest.mark.skipif(not os.environ.get(_STATE_PROBE), reason="driven by the test below")
def test_probe_leaves_a_failing_stdout_armed(monkeypatch):
    """Armed with a stream whose flush fails, no failure recorded: the reset
    takes no collection for it, so nothing but undoing the tap keeps the
    stream's last flush from recording in the next test."""
    sys.path.insert(0, str(TOOLS_DIR))
    import _lib_exitcodes as ec  # noqa: E402
    ns: dict = {}
    exec(_BROKEN_STREAM, ns)
    monkeypatch.setattr(sys, "stdout", ns["Broken"]())
    ec.arm_crash_envelope()


@pytest.mark.skipif(not os.environ.get(_STATE_PROBE), reason="driven by the test below")
def test_probe_sees_clean_exit_state():
    sys.path.insert(0, str(TOOLS_DIR))
    import _lib_exitcodes as ec  # noqa: E402
    # The previous test's fake stdout dies in a reference cycle (its tapped
    # flush refers to it): collect it here, not at whatever later moment the
    # collector happens to run, so a stale record cannot hide behind timing.
    import gc
    gc.collect()
    assert ec._lost_envelope is None
    assert (ec._crash_envelope_armed, ec._armed_stdout, ec._stdout_failures,
            ec._stdout_closed_reported, ec.get_crash_exit_code(),
            ec.get_caller_error_code()) == (None, None, [], False,
                                            ec._IMPORT_CRASH_EXIT_CODE,
                                            ec._IMPORT_CALLER_ERROR_CODE)


@pytest.mark.parametrize("dirty", ["test_probe_leaves_every_piece_of_exit_state_dirty",
                                   "test_probe_leaves_a_failing_stdout_armed"])
def test_exit_state_does_not_leak_between_tests(dirty):
    """M1/S2: a green pytest run must not end at rc 2 because one test left
    a failing stream armed (order-dependent: measured on
    test_patch_config_verify -k "test_w1 and EPIPE and json"), and no test
    may see another's codes, recorded failures or flags."""
    me = Path(__file__)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
         f"{me}::{dirty}",
         f"{me}::test_probe_sees_clean_exit_state"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300, cwd=str(REPO_ROOT), env=dict(os.environ, **{_STATE_PROBE: "1"}))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0 and "2 passed" in proc.stdout, out[-2000:]
    assert STDOUT_CLOSED_LINE not in out, out[-2000:]


# A small write stays in the buffer after the failed flush, so the exit
# flush would fail again; a big one fails inside write() and leaves none.
_SIZES = {"small": 100, "big": 1_000_000}


@pytest.mark.parametrize("size", sorted(_SIZES))
@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
def test_a_bug_in_a_broken_pipe_handler_is_still_a_crash(tmp_path, buffering, size):
    """S1 (p1): the handler for stdout's BrokenPipeError has a bug of its
    own. Its TypeError only has the BrokenPipeError as implicit context —
    it is a crash, reported as one, at the tool's crash code: the bytes
    still buffered for the broken stdout must not turn it into the
    stdout-closed exit at interpreter exit."""
    body = ("_lib_exitcodes.set_crash_exit_code(70)\n"
            "def main():\n"
            f"    try:\n        print('x' * {_SIZES[size]})\n        sys.stdout.flush()\n"
            "    except BrokenPipeError:\n        cleanup = None\n        cleanup()\n"
            "    return 0\n_lib_exitcodes.run(main)\n")
    rc, err = _run_closed(tmp_path, body, "reader-gone", _BUFFERING[buffering])
    assert f"{CRASH_MARK} (TypeError)" in err and "Traceback" in err, err
    assert rc == 70, err
    assert STDOUT_CLOSED_LINE not in err, err


@pytest.mark.parametrize("size", sorted(_SIZES))
@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
def test_a_tools_own_error_inside_a_broken_pipe_handler_keeps_its_line_and_code(
        tmp_path, buffering, size):
    """S1 (p2): after stdout's failure the tool hits ANOTHER error and names
    it with its own code. Implicit context is not a cause: its line and code
    stand — also when bytes are still buffered for the broken stdout."""
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
    assert rc == 5, err
    assert STDOUT_CLOSED_LINE not in err, err


def test_raise_from_none_cuts_the_chain(tmp_path):
    """`raise ... from None` says "not caused by that": the tool's reason stands."""
    body = _armed(
        "try:\n"
        "    try:\n        print('x' * 1_000_000)\n"
        "    except OSError:\n        raise RuntimeError('render failed') from None\n"
        "except RuntimeError as e:\n"
        "    _lib_exitcodes.die_caller_error(f'ERROR: {e}', reason='render_failed')\n")
    rc, err = _run_closed(tmp_path, body, "reader-gone")
    assert "ERROR: render failed" in err, err


def test_a_reset_after_an_arm_alone_takes_no_collection(monkeypatch):
    """#3156: an arm with no failure recorded is undone without a full
    collection (one per such test cost ~0.4 s, the bulk of the suite's time),
    and the stream gets its own write / flush back."""
    sys.path.insert(0, str(TOOLS_DIR))
    import _lib_exitcodes as ec  # noqa: E402
    import gc
    ns: dict = {}
    exec(_BROKEN_STREAM, ns)
    fake = ns["Broken"]()
    monkeypatch.setattr(sys, "stdout", fake)
    ec.arm_crash_envelope()
    assert "flush" in vars(fake) and fake._da_stdout_tap
    calls = []
    monkeypatch.setattr(gc, "collect", lambda *a: calls.append(a) or 0)
    ec.reset_exit_state()
    assert calls == []
    assert "write" not in vars(fake) and "flush" not in vars(fake)
    assert not getattr(fake, "_da_stdout_tap", False)


def test_a_reset_keeps_the_import_time_tap_of_the_original_stdout():
    """Unarmed, a failure of the original stdout is recorded (the import-time
    tap); a reset that undoes the taps must leave that one as import made it."""
    sys.path.insert(0, str(TOOLS_DIR))
    import _lib_exitcodes as ec  # noqa: E402
    ec.reset_exit_state()
    assert getattr(sys.__stdout__, "_da_stdout_tap", False)
    assert "flush" in vars(sys.__stdout__)


def test_a_reset_gives_back_a_streams_own_instance_flush(monkeypatch):
    """A stream whose flush was already an instance attribute gets that one
    back, not the class's."""
    sys.path.insert(0, str(TOOLS_DIR))
    import _lib_exitcodes as ec  # noqa: E402
    import io
    fake = io.StringIO()
    own = fake.flush
    fake.flush = own
    monkeypatch.setattr(sys, "stdout", fake)
    ec.arm_crash_envelope()
    assert vars(fake)["flush"] is not own
    ec.reset_exit_state()
    assert vars(fake)["flush"] is own
    assert "write" not in vars(fake)


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



# ═══════════════════════════════════════════════════════════════════════════
# The library's own envelope is stdout output too (final review, finding 1)
# ═══════════════════════════════════════════════════════════════════════════
_ENVELOPE_ONLY_EXITS = {
    "die_caller_error": "_lib_exitcodes.die_caller_error('ERROR: bad input', reason='r')\n",
    "hand-written": "print('ERROR: bad input', file=sys.stderr)\nreturn 2\n",
    "crash": "raise RuntimeError('bad input')\n",
}


def _run_envelope_only(tmp_path: Path, body: str, where: str, env: dict[str, str]):
    script = _script(tmp_path, body)
    if where == "dev-full":
        if not os.path.exists("/dev/full"):
            pytest.skip("/dev/full does not exist here")
        with open("/dev/full", "wb") as full:
            proc = _run(script, stdout=full.fileno(), env=env)
        return proc.returncode, proc.stderr.decode("utf-8", "replace")
    return _run_closed(tmp_path, body, "reader-gone", env)


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
@pytest.mark.parametrize("where", ["dev-full", "reader-gone"])
@pytest.mark.parametrize("how", sorted(_ENVELOPE_ONLY_EXITS))
def test_an_envelope_that_cannot_be_written_is_named(tmp_path, how, where, buffering):
    """The envelope is the only stdout output, and stdout fails while it is
    written: the tool's own line stays, the named stdout line follows it
    exactly once, and the exit code is the one the tool was leaving with
    (here 2; a crash at the crash code)."""
    body = ("_lib_exitcodes.set_crash_exit_code(70)\n"
            + _armed(_ENVELOPE_ONLY_EXITS[how]))
    rc, err = _run_envelope_only(tmp_path, body, where, _BUFFERING[buffering])
    named = (STDOUT_CLOSED_LINE if where == "reader-gone"
             else "ERROR: cannot write to stdout: No space left on device")
    assert rc == (70 if how == "crash" else 2), err
    assert err.count(named) == 1, err
    own = err.index(f"{CRASH_MARK} (RuntimeError)" if how == "crash" else "ERROR: bad input")
    assert own < err.index(named), err


@pytest.mark.parametrize("how", ["die_caller_error", "hand-written"])
def test_an_fd_less_stdout_is_not_a_stdout_failure(how):
    """A stdout with no fd (a host swapped in StringIO) has nowhere to put the
    envelope; that is not stdout failing. `fileno()` raises
    io.UnsupportedOperation, an OSError: only the tool's own line, no named
    stdout line, the tool's code."""
    body = ("import io\nsys.stdout = io.StringIO()\n"
            + _armed(_ENVELOPE_ONLY_EXITS[how]))
    code = f"import sys\nsys.path.insert(0, {str(TOOLS_DIR)!r})\nimport _lib_exitcodes\n" + body
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=60, env={**_ENV, "PYTHONIOENCODING": "utf-8"})
    assert proc.returncode == 2, proc.stderr
    assert "ERROR: bad input" in proc.stderr, proc.stderr
    assert "cannot write to stdout" not in proc.stderr, proc.stderr
    assert STDOUT_CLOSED_LINE not in proc.stderr, proc.stderr


@pytest.mark.parametrize("where", ["dev-full", "reader-gone"])
def test_a_real_tool_names_a_lost_envelope(tmp_path, where):
    """The reported repro: `onboard_platform --json` with no inputs prints
    only its envelope on stdout."""
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
    assert (err.count(STDOUT_CLOSED_LINE)
            + err.count("ERROR: cannot write to stdout:")) == 1, err


# ═══════════════════════════════════════════════════════════════════════════
# #3156 — three edges of the stdout-failure handling
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("size", sorted(_SIZES))
@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
def test_a_crash_raised_from_another_error_in_a_broken_pipe_handler_keeps_its_code(
        tmp_path, buffering, size):
    """#3156 (1): the handler's crash is ``raise ... from <unrelated>``. Its
    ``__cause__`` is the unrelated error, so stdout's failure sits only on
    the ``__context__`` chain; both chains must be walked so the failed
    stream counts as answered for, or the exit flush replaces the crash code (70) and adds
    the stdout-closed line."""
    body = ("_lib_exitcodes.set_crash_exit_code(70)\n"
            "def main():\n"
            f"    try:\n        print('x' * {_SIZES[size]})\n        sys.stdout.flush()\n"
            "    except BrokenPipeError:\n"
            "        raise RuntimeError('render failed') from ValueError('unrelated')\n"
            "    return 0\n_lib_exitcodes.run(main)\n")
    rc, err = _run_closed(tmp_path, body, "reader-gone", _BUFFERING[buffering])
    assert f"{CRASH_MARK} (RuntimeError)" in err, err
    assert rc == 70, err
    assert STDOUT_CLOSED_LINE not in err, err


def test_failures_in_chain_walks_both_chains_and_stops_on_a_cycle(monkeypatch):
    """#3156 (1), in-process: the failure is reached through ``__context__``
    of a ``__cause__``; a cycle in the chain ends the walk."""
    sys.path.insert(0, str(TOOLS_DIR))
    import _lib_exitcodes as ec  # noqa: E402
    failure = BrokenPipeError(32, "Broken pipe")
    stream = object()
    monkeypatch.setattr(ec, "_stdout_failures", [(failure, stream)])
    top = RuntimeError("top")
    cause = ValueError("unrelated")
    cause.__context__ = top  # a cycle back to the top
    top.__cause__ = cause
    top.__context__ = failure  # only on the context link of the top
    assert ec._failures_in_chain(top) == [stream]


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
def test_a_still_closed_fd_1_keeps_the_tools_own_code(tmp_path, buffering):
    """#3156 (3), the other side: fd 1 was closed when stdout failed and is
    still closed when the tool leaves with its own code (5). The final flush
    of the failed stream must not run again, or it fails and replaces that
    code. (Red before #3156: with fd 1 closed, the null device opened for
    the old redirect GOT fd 1, and closing it afterwards closed fd 1
    again.)"""
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
    assert proc.returncode == 5, err
    assert STDOUT_CLOSED_LINE not in err, err


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
def test_a_caller_error_reaching_the_excepthook_after_a_broken_pipe_prints_one_line(
        tmp_path, buffering):
    """#3156 (2): a tool without ``run()`` raises ``CallerError`` while
    handling stdout's failure; it reaches the module excepthook, where
    ``sys.exc_info()`` is empty. The exception must be handed on explicitly,
    so the failed stream counts as answered for: its own line only, its caller-error
    code (3 here)."""
    body = ("_lib_exitcodes.set_caller_error_code(3)\n"
            "try:\n    print('x' * 100)\n    sys.stdout.flush()\n"
            "except BrokenPipeError:\n"
            "    raise _lib_exitcodes.CallerError('my own reason')\n")
    rc, err = _run_closed(tmp_path, body, "reader-gone", _BUFFERING[buffering])
    assert "ERROR: my own reason" in err, err
    assert rc == 3, err
    assert STDOUT_CLOSED_LINE not in err, err
    assert CRASH_MARK not in err, err


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
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
    assert err.count(STDOUT_CLOSED_LINE) == 1, err
    content = out.read_text(encoding="utf-8")
    assert "old" not in content, content
    assert content == "mine\n", err


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
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
    assert err.count(STDOUT_CLOSED_LINE) == 1, err
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
    """Review F1: a tool without ``run()`` catches the lib's stdout-lost exit
    (``except SystemExit``), carries on, and ends with its own verdict (1).
    That verdict is the exit code — the caught exit decided nothing."""
    body = (_LATE + "try:\n    try:\n        print('x' * 100000)\n"
            "    except OSError:\n        _lib_exitcodes.die_caller_error('ERROR: report failed', code=5)\n"
            "except SystemExit:\n    pass\nsys.exit(1)\n")
    rc, err = _run_reader_gone(tmp_path, body, buffering)
    assert rc == 1, err
    assert err.count(STDOUT_CLOSED_LINE) == 1, err


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
@pytest.mark.parametrize("where", ["after-caught-exit", "atexit"])
def test_stdout_still_takes_writes_after_its_failure_was_handled(tmp_path, buffering, where):
    """Review F2: once stdout's failure is handled, ``sys.stdout`` still takes
    writes (dropped, as the null device would) — a ``json.dump`` after a
    caught exit, or an atexit handler, is not an AttributeError crash."""
    if where == "atexit":
        body = ("import atexit\natexit.register(lambda: sys.stdout.write('summary\\n'))\n"
                + _LATE + "print('x' * 100000)\n")
        want = 2
    else:
        body = (_LATE + "import json\ntry:\n    try:\n        print('x' * 100000)\n"
                "    except OSError:\n        _lib_exitcodes.die_caller_error('ERROR: report failed')\n"
                "except SystemExit:\n    pass\njson.dump({'v': 1}, sys.stdout)\nsys.exit(0)\n")
        want = 0
    rc, err = _run_reader_gone(tmp_path, body, buffering)
    assert rc == want, err
    assert "Traceback" not in err and "Exception ignored" not in err, err


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


@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
@pytest.mark.parametrize("held", sorted(_HELD))
def test_code_still_holding_a_sealed_stdout_writes_to_nowhere(tmp_path, buffering, held):
    """Review 2, F1: after stdout's failure is handled (the lib's exit
    caught), code still holding the ORIGINAL stream — a saved reference,
    ``sys.__stdout__``, its buffer, a logging handler, ``redirect_stdout``
    restoring it — writes as onto the null device: no ValueError crash, the
    tool's own 0."""
    prelude = ("import logging\nSAVED = sys.stdout\n"
               "logging.basicConfig(stream=sys.stdout)\n" + _LATE)
    handle = ("    try:\n        print('x' * 100000)\n        sys.stdout.flush()\n"
              "    except OSError:\n        _lib_exitcodes.die_caller_error('ERROR: report failed')\n")
    if held == "redirect-restored":
        body = (prelude + "import contextlib, io\ntry:\n"
                "    with contextlib.redirect_stdout(io.StringIO()):\n"
                "        try:\n            SAVED.write('x' * 100000)\n            SAVED.flush()\n"
                "        except OSError:\n"
                "            _lib_exitcodes.die_caller_error('ERROR: report failed')\n"
                "except SystemExit:\n    pass\nprint('summary')\nsys.exit(0)\n")
    else:
        body = (prelude + "try:\n" + handle + "except SystemExit:\n    pass\n"
                + _HELD[held] + "sys.exit(0)\n")
    rc, err = _run_reader_gone(tmp_path, body, buffering)
    if buffering == "unbuffered" and held == "redirect-restored":
        pass  # the write fails before the handler: a separate path
    else:
        assert rc == 0, err
    assert "Traceback" not in err and "Logging error" not in err, err
    assert CRASH_MARK not in err, err


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
