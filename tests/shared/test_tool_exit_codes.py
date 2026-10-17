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
# Tools with a documented richer scheme (diag_pr_ci's 0/1/2/3,
# tenant_verify's inverted contract) still exit 2 on bad *flags* because
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


def _run_crash_script(tmp_path, body, *, prelude=_CRASH_PRELUDE):
    script = tmp_path / "crash_tool.py"
    script.write_text(prelude.format(tools=str(TOOLS_DIR)) + body, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30,
    )


def _stderr_lines(proc):
    return [line for line in proc.stderr.splitlines() if line.strip()]


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


def test_importing_tenant_verify_leaves_the_crash_code_alone(tmp_path):
    """tenant_verify sets its crash code 1 only when it is the program
    (`__main__`): a module that imports it keeps 2 (#3090 review)."""
    proc = _run_crash_script(
        tmp_path,
        f"sys.path.insert(0, {str(TOOLS_DIR / 'dx')!r})\n"
        "import tenant_verify\nprint(_lib_exitcodes.get_crash_exit_code())\n")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "2"


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


def test_main_decorator_calls_on_crash_and_exits_with_crash_code(tmp_path):
    """@_lib_exitcodes.main(on_crash=...) — the hook for tools with a --json
    contract to emit their envelope before the process ends."""
    proc = _run_crash_script(
        tmp_path,
        "import json\n"
        "def envelope(exc):\n"
        "    print(json.dumps({'status': 'internal_error', 'type': type(exc).__name__}))\n"
        "@_lib_exitcodes.main(on_crash=envelope)\n"
        "def main():\n    raise OSError('disk gone')\n"
        "main()\n")
    assert proc.returncode == 2, proc.stderr
    assert proc.stdout.strip() == '{"status": "internal_error", "type": "OSError"}'
    assert "Traceback" in proc.stderr
    assert "ERROR: unexpected internal error (OSError): disk gone" in _stderr_lines(proc)
    assert "ATEXIT-RAN" in proc.stderr


def test_armed_crash_envelope_is_printed_once_on_stdout(tmp_path):
    """arm_crash_envelope (#3094): a crash in --json mode puts exactly one
    envelope on stdout — the tool's extra keys kept, status/reason/error not
    overridable — and still exits 2 with the traceback on stderr."""
    proc = _run_crash_script(
        tmp_path,
        "_lib_exitcodes.arm_crash_envelope(schema='t/v1', status='ok')\n"
        "raise OSError('disk gone')\n")
    assert proc.returncode == 2, proc.stderr
    assert json.loads(proc.stdout) == {
        "schema": "t/v1", "status": "caller_error", "reason": "internal_error",
        "error": "ERROR: unexpected internal error (OSError): disk gone"}
    assert "Traceback" in proc.stderr
    assert "ATEXIT-RAN" in proc.stderr


def test_armed_crash_envelope_builds_the_tools_zeroed_report_at_crash_time(tmp_path):
    """dev-rules §13: the envelope keeps the tool's own schema keys, zeroed;
    build() runs when the crash happens, not when the envelope is armed."""
    proc = _run_crash_script(
        tmp_path,
        "state = {'n': 0}\n"
        "_lib_exitcodes.arm_crash_envelope(lambda: {'total': state['n'], 'reason': 'x'},"
        " tool='t')\n"
        "state['n'] = 7\nraise ValueError('late')\n")
    assert proc.returncode == 2, proc.stderr
    assert json.loads(proc.stdout) == {
        "total": 7, "tool": "t", "status": "caller_error", "reason": "internal_error",
        "error": "ERROR: unexpected internal error (ValueError): late"}


def test_failing_build_still_prints_the_three_key_envelope(tmp_path):
    """A build() that raises must not leave stdout empty: rc 2 keeps its envelope."""
    proc = _run_crash_script(
        tmp_path,
        "_lib_exitcodes.arm_crash_envelope(lambda: {}['x'], tool='t')\n"
        "raise RuntimeError('boom')\n")
    assert proc.returncode == 2, proc.stderr
    assert json.loads(proc.stdout) == {
        "tool": "t", "status": "caller_error", "reason": "internal_error",
        "error": "ERROR: unexpected internal error (RuntimeError): boom"}
    assert _stderr_lines(proc)[-2] == \
        "ERROR: unexpected internal error (RuntimeError): boom", proc.stderr


def test_crash_envelope_on_a_closed_stdout_adds_no_second_traceback(tmp_path):
    """`tool --json | head -c1`: stdout is gone, so the envelope is dropped
    quietly — the crash report on stderr is the only one, rc stays 2."""
    proc = _run_crash_script(
        tmp_path,
        "import os\n_lib_exitcodes.arm_crash_envelope()\n"
        "os.close(1)\nraise RuntimeError('x')\n")
    assert proc.returncode == 2, proc.stderr
    assert "crash envelope failed" not in proc.stderr
    assert proc.stderr.count("Traceback") == 1, proc.stderr


def test_disarmed_crash_envelope_prints_nothing(tmp_path):
    """A tool whose own document is already out disarms: a later crash must
    not add a second document."""
    proc = _run_crash_script(
        tmp_path,
        "_lib_exitcodes.arm_crash_envelope()\nprint('{}')\n"
        "_lib_exitcodes.disarm_crash_envelope()\nraise RuntimeError('late')\n")
    assert proc.returncode == 2, proc.stderr
    assert proc.stdout == "{}\n"


def test_main_decorator_on_crash_wins_over_the_armed_envelope(tmp_path):
    """An explicit on_crash is the tool's own envelope: the armed default is
    not printed beside it."""
    proc = _run_crash_script(
        tmp_path,
        "_lib_exitcodes.arm_crash_envelope()\n"
        "@_lib_exitcodes.main(on_crash=lambda exc: print('OWN'))\n"
        "def main():\n    raise RuntimeError('x')\n"
        "main()\n")
    assert proc.returncode == 2, proc.stderr
    assert proc.stdout == "OWN\n"


def test_bare_main_decorator_prints_the_armed_envelope(tmp_path):
    proc = _run_crash_script(
        tmp_path,
        "_lib_exitcodes.arm_crash_envelope()\n"
        "@_lib_exitcodes.main\n"
        "def main():\n    raise RuntimeError('x')\n"
        "main()\n")
    assert proc.returncode == 2, proc.stderr
    assert json.loads(proc.stdout)["reason"] == "internal_error"


class TestMainDecoratorInProcess:
    """The decorator's own contract, without a subprocess."""

    @staticmethod
    def _ec(monkeypatch):
        sys.path.insert(0, str(TOOLS_DIR))
        import _lib_exitcodes as ec  # noqa: E402
        # idempotent reset (not save-then-restore): every test starts from 2
        monkeypatch.setattr(ec, "_crash_exit_code", ec.EXIT_CALLER_ERROR)
        monkeypatch.setattr(ec, "_crash_envelope_armed", None)
        return ec

    def test_armed_envelope_fires_at_most_once(self, monkeypatch, capsys):
        ec = self._ec(monkeypatch)
        ec.arm_crash_envelope()
        ec._print_crash_envelope(RuntimeError("a"))
        ec._print_crash_envelope(RuntimeError("b"))
        out = capsys.readouterr().out
        assert json.loads(out)["error"].endswith("(RuntimeError): a")

    def test_bare_decorator_exits_crash_code(self, monkeypatch, capsys):
        ec = self._ec(monkeypatch)

        @ec.main
        def tool():
            raise RuntimeError("boom")

        with pytest.raises(SystemExit) as exc_info:
            tool()
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "Traceback" in err
        assert err.rstrip().splitlines()[-1] == \
            "ERROR: unexpected internal error (RuntimeError): boom"

    def test_on_crash_receives_the_exception(self, monkeypatch, capsys):
        ec = self._ec(monkeypatch)
        seen = []

        @ec.main(on_crash=seen.append)
        def tool():
            raise ValueError("bad shape")

        with pytest.raises(SystemExit) as exc_info:
            tool()
        assert exc_info.value.code == 2
        assert len(seen) == 1 and isinstance(seen[0], ValueError)

    def test_failing_on_crash_still_exits_crash_code(self, monkeypatch, capsys):
        ec = self._ec(monkeypatch)

        def broken(exc):
            raise TypeError("envelope writer broke")

        @ec.main(on_crash=broken)
        def tool():
            raise ValueError("first")

        with pytest.raises(SystemExit) as exc_info:
            tool()
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "ERROR: unexpected internal error (ValueError): first" in err
        assert "envelope writer broke" in err

    def test_decorator_crash_exit_code_overrides_module_setting(self, monkeypatch):
        ec = self._ec(monkeypatch)

        @ec.main(crash_exit_code=1)
        def tool():
            raise RuntimeError("x")

        with pytest.raises(SystemExit) as exc_info:
            tool()
        assert exc_info.value.code == 1

    def test_decorator_uses_set_crash_exit_code(self, monkeypatch):
        ec = self._ec(monkeypatch)
        ec.set_crash_exit_code(1)

        @ec.main
        def tool():
            raise RuntimeError("x")

        with pytest.raises(SystemExit) as exc_info:
            tool()
        assert exc_info.value.code == 1

    @pytest.mark.parametrize("code", [0, 1, 2, None])
    def test_decorator_passes_system_exit_through(self, monkeypatch, code):
        ec = self._ec(monkeypatch)
        called = []

        @ec.main(on_crash=called.append)
        def tool():
            sys.exit(code)

        with pytest.raises(SystemExit) as exc_info:
            tool()
        assert exc_info.value.code == code
        assert called == []

    def test_decorator_passes_keyboard_interrupt_through(self, monkeypatch):
        ec = self._ec(monkeypatch)

        @ec.main
        def tool():
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            tool()

    def test_decorator_returns_the_wrapped_value(self, monkeypatch):
        ec = self._ec(monkeypatch)

        @ec.main
        def tool(a, *, b):
            return a + b

        assert tool(1, b=2) == 3
        assert tool.__name__ == "tool"

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
