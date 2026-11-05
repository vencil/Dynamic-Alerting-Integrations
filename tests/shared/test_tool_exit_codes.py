"""Exit code tests for da-tools CLI tools: ``--help`` and an unknown flag.

Verifies that every registered tool:
  - exits 0 on --help
  - exits 2 (EXIT_CALLER_ERROR) on one unrecognised flag,
    ``--this-flag-does-not-exist-xyz`` (non-zero for the argparse-exempt
    tools in INVALID_ARG_EXIT2_EXEMPT)

⚠️ Scope, stated so nobody reads more into a green run than it earns:

  COVERED    ``--help`` → rc 0, and an unrecognised flag → rc 2 (non-zero for
             the exempt tools). That second path is the one argparse hands
             every tool for free — ``_lib_exitcodes.py`` says so itself — so a
             green run says little about the tools' own exit-code handling.
  NOT COVERED  a recognised flag with an unusable value: a path that does not
             exist, an invalid value, an unreachable environment (Prometheus /
             API), an IO failure. ``_lib_exitcodes.py`` files all of these
             under EXIT_CALLER_ERROR (2), and none of them is fed here. #1642
             found five such defects live (rc 0 or 1) while this file was green.
  WEAK       even the unknown-flag case, for two kinds of tool, because rc 2
             can come from a reason unrelated to the flag:
             - tools that declare a required argument: argparse's
               missing-required error exits 2 whether or not the unknown flag
               is rejected;
             - tools whose clean run already fails to reach their environment
               (e.g. backtest_threshold without Prometheus exits 2): a tool
               that swallowed unknown flags would still pass.
             The assertion only discriminates for tools whose bare run would
             exit 0. See #1642 for the measurement.
"""
import json
import os
import subprocess
import sys
import pytest
from pathlib import Path

TOOLS_DIR = Path(__file__).parent.parent.parent / "scripts" / "tools"
sys.path.insert(0, str(TOOLS_DIR))
from _lib_toolcount import tool_map_scope  # noqa: E402


def collect_tools():
    """Every tool `docs/internal/tool-map.md` inventories.

    ⛔ The population is `_lib_toolcount.tool_map_scope` — the repo root plus
    ops/ dx/ lint/, skipping `_`-prefixed helpers. It used to be a second
    hand-written copy of that predicate (#1541).

    ⛔ The top level is included (#1642): ``scripts/tools/validate_all.py``
    runs the required check ``Drift Detection (validate_all.py)``, and a tool
    the gate does not enumerate can drift to any exit code unnoticed.
    """
    return [path for _subdir, path in tool_map_scope(TOOLS_DIR)]


def test_population_includes_the_top_level_runner():
    """Anti-vacuity for the #1642 fix above.

    ``TOOLS_DIR`` is part of the walk; if it were dropped again the
    parametrized sweeps below would just run fewer cases and stay green,
    which is exactly how validate_all.py stayed out of the contract for the
    life of this file. A named member of the top level is asserted to be
    enumerated, so the population cannot shrink back silently.
    """
    names = {t.name for t in ALL_TOOLS}
    assert "validate_all.py" in names, (
        "scripts/tools/validate_all.py is not in the exit-code population; "
        "the top-level walk has been lost (#1642)")
    assert not any(n.startswith("_") for n in names), (
        "a _-prefixed helper was enumerated; those are libraries, not CLIs")


ALL_TOOLS = collect_tools()


@pytest.mark.parametrize("tool_path", ALL_TOOLS, ids=[t.name for t in ALL_TOOLS])
def test_help_exits_zero(tool_path):
    """Every tool should exit 0 on --help."""
    result = subprocess.run(
        [sys.executable, str(tool_path), "--help"],
        capture_output=True, timeout=10
    )
    assert result.returncode == 0, (
        f"{tool_path.name} --help failed with exit code {result.returncode}\n"
        f"stderr: {result.stderr.decode()[:300]}"
    )


# Tools whose bad-flag handling legitimately does NOT go through argparse's
# exit-2 path. Documented exceptions only (with rationale), so new drift is
# caught rather than silently allowed.
INVALID_ARG_EXIT2_EXEMPT: set[str] = {
    # These two a11y dev tools take a bare positional `path` (no argparse
    # flag parsing), so an unrecognised "--flag" is treated as a path and
    # raises an uncaught FileNotFoundError. That used to be Python's default
    # exit 1; since #3090 the crash hook makes it 2 — but as a crash
    # (traceback), not a rejected flag, so they stay exempt here.
    # Minor known inconsistency in non-customer-facing dx tools; left as-is
    # to avoid widening this sweep. Their valid exit paths DO use the 0/1/2
    # constants (see the files' main()).
    "check_aria_references.py",
    "axe_lite_static.py",
}

# The argparse-exempt tools, resolved back to their Paths in ALL_TOOLS
# (sorted by name so parametrize ids are deterministic).
_ARGPARSE_EXEMPT_TOOLS = sorted(
    (t for t in ALL_TOOLS if t.name in INVALID_ARG_EXIT2_EXEMPT),
    key=lambda t: t.name,
)


def test_exempt_set_resolves_to_registered_tools():
    """Every INVALID_ARG_EXIT2_EXEMPT name must exist in ALL_TOOLS.

    Guards against a rename/move silently emptying the shrunk non-zero
    sweep below (a stale exempt name would otherwise just vanish from
    the parametrize list)."""
    assert {t.name for t in _ARGPARSE_EXEMPT_TOOLS} == INVALID_ARG_EXIT2_EXEMPT


@pytest.mark.parametrize(
    "tool_path", _ARGPARSE_EXEMPT_TOOLS,
    ids=[t.name for t in _ARGPARSE_EXEMPT_TOOLS],
)
def test_invalid_args_exits_nonzero(tool_path):
    """Argparse-exempt tools should still exit non-zero on invalid arguments.

    Deliberately runs ONLY on INVALID_ARG_EXIT2_EXEMPT: every other tool is
    already covered by test_invalid_args_exits_caller_error below, which
    asserts exit == 2 — and exit 2 implies non-zero, so a full-ALL_TOOLS
    sweep here added ~186 redundant subprocess spawns with zero extra
    coverage. This test only guards the two argparse-exempt tools that the
    exit-2 sweep skips (an uncaught FileNotFoundError: the crash exit code,
    2 since #3090)."""
    result = subprocess.run(
        [sys.executable, str(tool_path), "--this-flag-does-not-exist-xyz"],
        capture_output=True, timeout=10
    )
    assert result.returncode != 0, (
        f"{tool_path.name} accepted invalid args (exit code {result.returncode})"
    )


# ── #452 Track A: 0/1/2 exit-code contract ────────────────────────────
# The shared contract lives in scripts/tools/_lib_exitcodes.py:
#   0 = EXIT_OK, 1 = EXIT_VIOLATION (finding), 2 = EXIT_CALLER_ERROR.
# Below tightens the unknown-flag check from "non-zero" to "exactly 2"
# (caller error). That is ONE of the EXIT_CALLER_ERROR triggers the contract
# lists — the one argparse owns — not the contract's boundary: bad values for
# recognised flags, missing paths, unreachable environments and IO failures
# are not exercised (see the Scope block in the module docstring, #1642).
# Tools with a documented richer scheme (diag_pr_ci's 0/1/2/3) still exit 2
# on bad *flags* because
# argparse owns that path — so this holds for all of them.


def test_lib_exitcodes_constants_are_canonical():
    """SSOT constants must be 0/1/2 (Go da-guard/da-parser mirror)."""
    sys.path.insert(0, str(TOOLS_DIR))
    import _lib_exitcodes as ec  # noqa: E402
    assert (ec.EXIT_OK, ec.EXIT_VIOLATION, ec.EXIT_CALLER_ERROR) == (0, 1, 2)


@pytest.mark.parametrize("tool_path", ALL_TOOLS, ids=[t.name for t in ALL_TOOLS])
def test_invalid_args_exits_caller_error(tool_path):
    """Unrecognised flags must exit exactly 2 (EXIT_CALLER_ERROR), not just
    any non-zero (#452 Track A). Only the unknown-flag path; for tools with
    a required argument or an unreachable environment, rc 2 may come from
    that instead (see the module docstring's Scope block, #1642)."""
    if tool_path.name in INVALID_ARG_EXIT2_EXEMPT:
        pytest.skip(f"{tool_path.name} documented exempt")
    result = subprocess.run(
        [sys.executable, str(tool_path), "--this-flag-does-not-exist-xyz"],
        capture_output=True, timeout=10
    )
    assert result.returncode == 2, (
        f"{tool_path.name} should exit 2 (EXIT_CALLER_ERROR) on a bad flag, "
        f"got {result.returncode}\nstderr: {result.stderr.decode('utf-8', 'replace')[:300]}"
    )
    # #3090: a crash is rc 2 too — the bad flag must be refused, not crash.
    assert b"ERROR: unexpected internal error" not in result.stderr, (
        f"{tool_path.name} crashed on a bad flag instead of refusing it")


# ── Uncaught exception → crash exit code (#3090) ─────────────────────
#
# A crash used to end at the interpreter's default rc 1 — the same number as
# EXIT_VIOLATION ("ran and found something"). Importing _lib_exitcodes now
# installs a sys.excepthook that prints the traceback, then one
# "ERROR: unexpected internal error (<type>): <message>" line, and exits with
# the crash code (EXIT_CALLER_ERROR unless the tool set its own).
#
# ⛔ These run in a REAL subprocess on purpose: CI executes the scripts
# directly (not through `da-tools`), and an excepthook only fires at the top
# level of an interpreter — an in-process `pytest.raises` never reaches it.

_CRASH_PRELUDE = (
    "import atexit, sys\n"
    "sys.path.insert(0, {tools!r})\n"
    "import _lib_exitcodes\n"
    "atexit.register(lambda: print('ATEXIT-RAN', file=sys.stderr, flush=True))\n"
)


def _run_crash_script(tmp_path, body, *, prelude=_CRASH_PRELUDE, env=None):
    script = tmp_path / "crash_tool.py"
    script.write_text(prelude.format(tools=str(TOOLS_DIR)) + body, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30, env=env,
    )


def _stderr_lines(proc):
    return [line for line in proc.stderr.splitlines() if line.strip()]


@pytest.mark.real_crash_exit_code
def test_direct_run_crash_exits_caller_error(tmp_path):
    """Raise at top level of a script that imported _lib_exitcodes → rc 2,
    traceback kept, then the one ERROR line; atexit still runs and stdout
    written before the crash is not lost (no os._exit)."""
    proc = _run_crash_script(
        tmp_path, "print('partial report')\nraise RuntimeError('boom')\n")
    assert proc.returncode == 2, proc.stderr
    assert "Traceback (most recent call last)" in proc.stderr
    lines = _stderr_lines(proc)
    assert "ERROR: unexpected internal error (RuntimeError): boom" in lines, proc.stderr
    # ERROR line comes after the traceback (it is the line to grep for)
    assert lines.index("ERROR: unexpected internal error (RuntimeError): boom") \
        > lines.index("RuntimeError: boom")
    assert lines[-1] == "ATEXIT-RAN", proc.stderr
    assert proc.stdout == "partial report\n"


def test_interactive_session_keeps_the_default_hook(tmp_path):
    """`python -i` (and the REPL) report an error and carry on: the crash hook
    must not end the session at rc 2 (#3090 review)."""
    proc = subprocess.run(
        [sys.executable, "-i", "-q"], capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=30,
        input=(f"import sys; sys.path.insert(0, {str(TOOLS_DIR)!r})\n"
               "import _lib_exitcodes\n1/0\nprint('still alive')\n"))
    assert proc.returncode == 0, proc.stderr
    assert "still alive" in proc.stdout
    assert "unexpected internal error" not in proc.stderr


def test_run_in_an_interactive_session_leaves_the_original_exception(tmp_path):
    """`python -i tool.py` post-mortem through `run(main)`: the crash is
    re-raised for the default hook, so `sys.last_value` (what `pdb.pm()`
    debugs) is the tool's exception, not `SystemExit(2)` (#3237)."""
    script = tmp_path / "crash_tool.py"
    script.write_text(_CRASH_PRELUDE.format(tools=str(TOOLS_DIR))
                      + "def main():\n    raise RuntimeError('boom')\n"
                      + "_lib_exitcodes.run(main)\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-i", "-q", str(script)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30,
        input="print('LAST', type(sys.last_value).__name__, sys.last_value)\n")
    assert proc.returncode == 0, proc.stderr
    assert "LAST RuntimeError boom" in proc.stdout, (proc.stdout, proc.stderr)
    assert "unexpected internal error" not in proc.stderr, proc.stderr


@pytest.mark.parametrize("interactive, inspect, ps1, tty, expected", [
    (1, 1, False, False, True),   # python -i, piped stdin: a prompt follows
    (0, 1, False, True, True),    # PYTHONINSPECT with a terminal: a prompt follows
    (0, 1, False, False, False),  # PYTHONINSPECT, no terminal: CPython exits
    (0, 0, True, False, True),    # already at a prompt (REPL)
    (0, 0, False, True, False),   # a plain run from a terminal
])
def test_interactive_is_the_interpreters_own_test(monkeypatch, interactive, inspect,
                                                  ps1, tty, expected):
    """#3237: `_interactive()` answers "does a prompt follow the script" the way
    CPython decides it, PYTHONINSPECT (flags.inspect alone) included."""
    sys.path.insert(0, str(TOOLS_DIR))
    import _lib_exitcodes as ec
    real = sys.flags

    class _Flags:  # every other flag as it is: importlib reads them too
        def __getattr__(self, name):
            return getattr(real, name)
    flags = _Flags()
    flags.interactive, flags.inspect = interactive, inspect
    monkeypatch.setattr(ec.sys, "flags", flags)
    if ps1:
        monkeypatch.setattr(ec.sys, "ps1", ">>> ", raising=False)
    else:
        monkeypatch.delattr(ec.sys, "ps1", raising=False)
    monkeypatch.setattr(ec.os, "isatty", lambda fd: tty and fd == 0)
    assert ec._interactive() is expected


def test_importing_tenant_verify_leaves_both_codes_alone(tmp_path):
    """Importing tenant_verify (now at module level, #3393) leaves the
    importing process's crash and caller-error codes at 2 (#3090 review)."""
    proc = _run_crash_script(
        tmp_path,
        f"sys.path.insert(0, {str(TOOLS_DIR / 'dx')!r})\n"
        "import tenant_verify\n"
        "print(_lib_exitcodes.get_crash_exit_code(), "
        "_lib_exitcodes.get_caller_error_code())\n")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "2 2"


#: Every channel that moves the process crash / caller-error code, which no
#: production tool uses since #3393 (tenant_verify was the one tool that
#: moved its codes, to 1/1): the setters, the module globals they write, and
#: the per-call `code=` of `exit_on_crash` (the `@main(crash_exit_code=)`
#: decorator it used to sit beside was retired, #3278).
_CODE_SETTERS = ("set_crash_exit_code", "set_caller_error_code")
_CODE_GLOBALS = ("_crash_exit_code", "_caller_error_code")
_PER_CALL = ("exit_on_crash", "code")


def _setter_calls(tree):
    """(line, name) of every reach for a code channel in *tree*: a setter or
    code global named in any way — called, imported (under any alias),
    assigned, or as a `getattr` string — or `exit_on_crash(..., code=)`
    under any name it was imported as. Not seen for the latter: a `getattr`
    string, `functools.partial`, `**{...}`, a local alias; a same-named
    function of another module is a false hit (none in scripts/tools)."""
    import ast
    names = set(_CODE_SETTERS) | set(_CODE_GLOBALS)
    # `exit_on_crash` under any name this tree imported it as
    per_call = {_PER_CALL[0]} | {
        a.asname for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        for a in node.names if a.name == _PER_CALL[0] and a.asname}
    hits = []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Attribute) and node.attr in names:
            name = node.attr
        elif isinstance(node, ast.Name) and node.id in names:
            name = node.id
        elif isinstance(node, ast.ImportFrom):
            name = next((a.name for a in node.names if a.name in names), None)
        elif isinstance(node, ast.Constant) and node.value in names:
            name = node.value
        elif (isinstance(node, ast.Call)
              and per_call & {getattr(node.func, "attr", None), getattr(node.func, "id", None)}
              and any(k.arg == _PER_CALL[1] for k in node.keywords)):
            name = "exit_on_crash(code=)"
        if name is not None:
            hits.append((getattr(node, "lineno", 0), name))
    return sorted(set(hits))


def test_no_tool_moves_the_crash_or_caller_error_code():
    """#3393: every tool exits at the shared crash and caller-error code 2.
    A tool that calls a setter would bring back a second meaning of 1 or 2
    — the inversion #3393 removed. Read as the AST's calls, not as text:
    the setters' names also appear in comments and in the lints' save /
    restore (which call them with the saved value, and are allowed)."""
    import ast
    allowed = {TOOLS_DIR / "_lib_exitcodes.py",
               # save / restore around an in-process tool run
               TOOLS_DIR / "lint" / "check_cli_contract.py",
               TOOLS_DIR / "lint" / "check_cli_default_drift.py"}
    # #3579: every tree whose entries exit through run(), not scripts/tools
    # alone — a session-guard declares its codes with use_hook_protocol()
    # only, and must not move them again after it.
    scripts = TOOLS_DIR.parent
    paths = sorted(p for tree in ("tools", "ops", "dx", "session-guards")
                   for p in (scripts / tree).rglob("*.py"))
    scanned, offenders = 0, []
    for path in paths:
        if path in allowed:
            continue
        scanned += 1
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders += [f"{path.relative_to(scripts)}:{line} {name}"
                      for line, name in _setter_calls(tree)]
    assert scanned > 100, scanned  # the walk reached the tools
    assert offenders == [], offenders


def test_the_setter_scan_sees_a_call():
    """Must-ring control for the scan above: the same predicate flags the
    call shape tenant_verify used before #3393."""
    import ast
    for src, want in [
        ("import _lib_exitcodes\n_lib_exitcodes.set_crash_exit_code(1)\n", "set_crash_exit_code"),
        ("from _lib_exitcodes import set_caller_error_code as _s\n_s(1)\n",
         "set_caller_error_code"),
        ("import _lib_exitcodes\ngetattr(_lib_exitcodes, 'set_crash_exit_code')(1)\n",
         "set_crash_exit_code"),
        ("import _lib_exitcodes\n_lib_exitcodes._caller_error_code = 1\n", "_caller_error_code"),
        ("import _lib_exitcodes as _ec\n_ec.exit_on_crash(RuntimeError(), code=1)\n",
         "exit_on_crash(code=)"),
        ("from _lib_exitcodes import exit_on_crash\nexit_on_crash(RuntimeError(), code=1)\n",
         "exit_on_crash(code=)"),
        ("from _lib_exitcodes import exit_on_crash as eoc\neoc(RuntimeError(), code=1)\n",
         "exit_on_crash(code=)"),
    ]:
        names = {name for _line, name in _setter_calls(ast.parse(src))}
        assert want in names, (src, names)


@pytest.mark.real_crash_exit_code
def test_crash_inside_main_function_exits_caller_error(tmp_path):
    """The usual shape: `if __name__ == "__main__": main()` raising deep inside."""
    proc = _run_crash_script(
        tmp_path,
        "def _inner():\n    {}['missing']\n"
        "def main():\n    _inner()\n"
        "if __name__ == '__main__':\n    sys.exit(main())\n")
    assert proc.returncode == 2, proc.stderr
    assert "Traceback" in proc.stderr
    assert "ERROR: unexpected internal error (KeyError): 'missing'" in _stderr_lines(proc)


@pytest.mark.parametrize("code", [0, 1, 2])
def test_system_exit_passes_through_unchanged(tmp_path, code):
    """sys.exit(N) is never relabelled as a crash."""
    proc = _run_crash_script(tmp_path, f"sys.exit({code})\n")
    assert proc.returncode == code, proc.stderr
    assert "ERROR: unexpected internal error" not in proc.stderr
    assert "ATEXIT-RAN" in proc.stderr


def test_keyboard_interrupt_is_not_turned_into_caller_error(tmp_path):
    """Ctrl-C is not an internal error: the interpreter's own handling stays
    (POSIX: the process re-kills itself with SIGINT, rc -2 / 130)."""
    proc = _run_crash_script(tmp_path, "raise KeyboardInterrupt\n")
    assert proc.returncode not in (0, 1, 2), (proc.returncode, proc.stderr)
    assert "ERROR: unexpected internal error" not in proc.stderr
    assert "KeyboardInterrupt" in proc.stderr


@pytest.mark.real_crash_exit_code
def test_tool_can_set_its_own_crash_exit_code(tmp_path):
    """set_crash_exit_code(N) is what the hook exits with."""
    proc = _run_crash_script(
        tmp_path, "_lib_exitcodes.set_crash_exit_code(1)\nraise ValueError('x')\n")
    assert proc.returncode == 1, proc.stderr
    assert "ERROR: unexpected internal error (ValueError): x" in _stderr_lines(proc)


def test_existing_custom_excepthook_is_not_clobbered(tmp_path):
    """A process that already installed its own excepthook keeps it."""
    prelude = (
        "import os, sys\n"
        "sys.excepthook = lambda *a: (print('CUSTOM-HOOK', file=sys.stderr),"
        " sys.stderr.flush(), os._exit(7))\n"
        "sys.path.insert(0, {tools!r})\n"
        "import _lib_exitcodes\n"
    )
    proc = _run_crash_script(tmp_path, "raise RuntimeError('boom')\n", prelude=prelude)
    assert proc.returncode == 7, proc.stderr
    assert "CUSTOM-HOOK" in proc.stderr
    assert "ERROR: unexpected internal error" not in proc.stderr


@pytest.mark.real_crash_exit_code
def test_a_crash_writes_nothing_to_stdout(tmp_path):
    """#3558 (was the #3094 crash envelope): a crash puts nothing on stdout
    — no envelope — and still exits 2 with the traceback and the one
    `ERROR:` line on stderr, atexit handlers run."""
    proc = _run_crash_script(tmp_path, "raise OSError('disk gone')\n")
    assert proc.returncode == 2, proc.stderr
    assert proc.stdout == ""
    assert "Traceback" in proc.stderr
    assert "ERROR: unexpected internal error (OSError): disk gone" in _stderr_lines(proc)
    assert "ATEXIT-RAN" in proc.stderr


@pytest.mark.real_crash_exit_code
def test_run_writes_nothing_to_stdout_on_a_crash(tmp_path):
    proc = _run_crash_script(
        tmp_path,
        "def main():\n"
        "    raise RuntimeError('x')\n"
        "_lib_exitcodes.run(main)\n")
    assert proc.returncode == 2, proc.stderr
    assert proc.stdout == ""


@pytest.mark.parametrize("unbuffered", ["1", None])
@pytest.mark.real_crash_exit_code
def test_a_crash_with_stdout_closed_mid_run_has_one_traceback(tmp_path, unbuffered):
    """A crash after fd 1 was closed: the crash report on stderr is the only
    one, rc stays 2 (not 120). Run with and without PYTHONUNBUFFERED: a
    write left in a block-buffered stdout fails again at interpreter exit."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    if unbuffered:
        env["PYTHONUNBUFFERED"] = unbuffered
    proc = _run_crash_script(
        tmp_path, "import os\nos.close(1)\nraise RuntimeError('x')\n", env=env)
    assert proc.returncode == 2, proc.stderr
    assert proc.stderr.count("Traceback") == 1, proc.stderr


class TestCrashExitInProcess:
    """`exit_on_crash` and the crash code, without a subprocess."""

    @staticmethod
    def _ec(monkeypatch):
        sys.path.insert(0, str(TOOLS_DIR))
        import _lib_exitcodes as ec  # noqa: E402
        # idempotent reset (not save-then-restore): every test starts from 2
        monkeypatch.setattr(ec, "_crash_exit_code", ec.EXIT_CALLER_ERROR)
        return ec

    @pytest.mark.real_crash_exit_code
    def test_exit_on_crash_exits_crash_code(self, monkeypatch, capsys):
        ec = self._ec(monkeypatch)
        try:
            raise RuntimeError("boom")
        except RuntimeError as exc:
            with pytest.raises(SystemExit) as exc_info:
                ec.exit_on_crash(exc)
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "Traceback" in err
        assert err.rstrip().splitlines()[-1] == \
            "ERROR: unexpected internal error (RuntimeError): boom"

    @pytest.mark.real_crash_exit_code
    def test_per_call_code_overrides_module_setting(self, monkeypatch):
        ec = self._ec(monkeypatch)
        with pytest.raises(SystemExit) as exc_info:
            ec.exit_on_crash(RuntimeError("x"), code=1)
        assert exc_info.value.code == 1

    @pytest.mark.real_crash_exit_code
    def test_exit_on_crash_uses_set_crash_exit_code(self, monkeypatch):
        ec = self._ec(monkeypatch)
        ec.set_crash_exit_code(1)
        with pytest.raises(SystemExit) as exc_info:
            ec.exit_on_crash(RuntimeError("x"))
        assert exc_info.value.code == 1

    @pytest.mark.parametrize("bad", [0, -1, 256, True, "1", None])
    def test_set_crash_exit_code_rejects_unusable_codes(self, monkeypatch, bad):
        """0 would make a crash read as success; non-int / out-of-range codes
        are not exit statuses."""
        ec = self._ec(monkeypatch)
        with pytest.raises(ValueError):
            ec.set_crash_exit_code(bad)
        assert ec.get_crash_exit_code() == 2

    def test_crash_message_is_one_bounded_line(self, monkeypatch):
        ec = self._ec(monkeypatch)
        msg = ec.crash_message(RuntimeError("line one\nline two\r\n" + "x" * 2000))
        assert "\n" not in msg and "\r" not in msg
        assert msg.startswith("ERROR: unexpected internal error (RuntimeError): line one line two")
        assert len(msg) < 600

    def test_crash_message_without_text_or_with_broken_str(self, monkeypatch):
        ec = self._ec(monkeypatch)

        class Unprintable(Exception):
            def __str__(self):
                raise RuntimeError("no")

        assert ec.crash_message(RuntimeError()) == \
            "ERROR: unexpected internal error (RuntimeError): (no message)"
        assert ec.crash_message(Unprintable()) == \
            "ERROR: unexpected internal error (Unprintable): (unprintable message)"
