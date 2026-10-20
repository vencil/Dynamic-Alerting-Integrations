"""Under the test suite a crash does not end at rc 2 (#3092).

A crash ends at rc 2 (#3090), the same code as every named caller error, so a
test that only checks ``returncode == 2`` cannot tell "the tool named the
error" from "the tool crashed". tests/conftest.py therefore sets
``DA_TEST_CRASH_EXIT_CODE=70`` for the whole run, and ``_lib_exitcodes`` and
the da-tools entrypoint end every crash there; a child given its own ``env=``
gets the variable from conftest's Popen shim. Production never sets it.

What this file pins, each assertion red when its piece is gone:

* the library reads the variable (a child that crashes ends at 70, not 2);
* the shim adds it to an explicit ``env=`` (a child started with a minimal
  env still ends at 70) — also positional, bytes-keyed and ``from subprocess
  import Popen``;
* it wins over every crash code a tool chooses itself, and only 3..255
  count (the parse rule; its mirror in entrypoint.py is pinned in
  test_entrypoint.py);
* it moves the crash code only: a named caller error and a closed stdout
  keep the caller-error code;
* ``@pytest.mark.real_crash_exit_code`` takes it away for one test, children
  included.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "scripts" / "tools"
ENV = "DA_TEST_CRASH_EXIT_CODE"
CRASH_MARK = "unexpected internal error"
SESSION_CODE = 70

# What a module-level `dict(os.environ)` holds: copied at import, before any
# test's opt-out.
_ENV_AT_IMPORT = dict(os.environ)

_PRELUDE = f"import os, sys\nsys.path.insert(0, {str(TOOLS_DIR)!r})\nimport _lib_exitcodes\n"


def _conftest():
    mod = sys.modules.get("conftest")
    assert mod is not None and Path(mod.__file__).resolve() == REPO_ROOT / "tests" / "conftest.py"
    return mod


def _script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "tool.py"
    path.write_text(_PRELUDE + body, encoding="utf-8")
    return path


def _minimal_env() -> dict[str, str]:
    """A child env built from scratch, as ~77 tests build theirs: no
    DA_TEST_CRASH_EXIT_CODE unless the shim adds it."""
    return {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"}


def _run(script: Path, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60, **kwargs)


def test_the_session_moves_the_crash_code_off_2():
    assert os.environ.get(ENV) == str(SESSION_CODE)
    sys.path.insert(0, str(TOOLS_DIR))
    import _lib_exitcodes as ec  # noqa: E402
    assert ec.TEST_CRASH_EXIT_CODE_ENV == _conftest().TEST_CRASH_EXIT_CODE_ENV == ENV
    assert ec.effective_crash_exit_code() == SESSION_CODE
    assert ec.get_crash_exit_code() == ec.EXIT_CALLER_ERROR  # the tool's own choice


def test_a_crashing_child_ends_at_the_test_crash_code(tmp_path):
    """Red when the library stops reading the variable."""
    proc = _run(_script(tmp_path, "raise RuntimeError('boom')\n"))
    assert proc.returncode == SESSION_CODE, proc.stderr
    assert f"{CRASH_MARK} (RuntimeError): boom" in proc.stderr


def test_a_child_given_its_own_env_still_gets_it(tmp_path):
    """Red when the Popen shim is gone: the child's env is built from scratch."""
    proc = _run(_script(tmp_path, "raise RuntimeError('boom')\n"), env=_minimal_env())
    assert proc.returncode == SESSION_CODE, proc.stderr


def test_the_shim_covers_positional_bytes_and_imported_popen(tmp_path):
    from subprocess import Popen  # the name a module may hold on to
    script = str(_script(tmp_path, "raise RuntimeError('boom')\n"))
    env = _minimal_env()
    bytes_env = {k.encode(): v.encode() for k, v in env.items()}
    argv = [sys.executable, script]
    procs = {
        "imported Popen": Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=env),
        "bytes keys": Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=bytes_env),
        # Popen(args, bufsize, executable, stdin, stdout, stderr, preexec_fn,
        # close_fds, shell, cwd, env)
        # (bytes mode: nothing is read back; positional on purpose)
        "positional env": Popen(argv, -1, None, None, subprocess.DEVNULL,  # open-encoding: ignore
                                subprocess.DEVNULL, None, True, False, None, env),
    }
    for how, proc in procs.items():
        try:
            proc.wait(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
        assert proc.returncode == SESSION_CODE, how


def test_an_env_that_names_the_variable_keeps_its_own_value(tmp_path):
    env = dict(_minimal_env(), **{ENV: "71"})
    proc = _run(_script(tmp_path, "raise RuntimeError('boom')\n"), env=env)
    assert proc.returncode == 71, proc.stderr
    assert env[ENV] == "71"


def test_the_shim_never_changes_the_callers_mapping(monkeypatch):
    env = _minimal_env()
    out = _conftest()._with_test_crash_exit_code(env)
    assert ENV not in env
    assert out[ENV] == str(SESSION_CODE) and out is not env
    monkeypatch.delenv(ENV)  # as under the opt-out
    stripped = _conftest()._with_test_crash_exit_code(out)
    assert ENV not in stripped and out[ENV] == str(SESSION_CODE)


_OWN_CRASH_CODES = {
    "set_crash_exit_code": "_lib_exitcodes.set_crash_exit_code(1)\nraise RuntimeError('x')\n",
    "@main(crash_exit_code=)": ("@_lib_exitcodes.main(crash_exit_code=5)\n"
                                "def main():\n    raise RuntimeError('x')\nmain()\n"),
    "exit_on_crash(code=)": ("try:\n    raise RuntimeError('x')\n"
                             "except RuntimeError as e:\n    _lib_exitcodes.exit_on_crash(e, code=9)\n"),
    "run()": "def main():\n    raise RuntimeError('x')\n_lib_exitcodes.run(main)\n",
}


@pytest.mark.parametrize("how", sorted(_OWN_CRASH_CODES))
def test_it_wins_over_the_tools_own_crash_code(tmp_path, how):
    proc = _run(_script(tmp_path, _OWN_CRASH_CODES[how]))
    assert proc.returncode == SESSION_CODE, proc.stderr
    assert CRASH_MARK in proc.stderr


@pytest.mark.parametrize("raw, expected", [
    ("70", 70), ("3", 3), ("255", 255), ("070", 70),
    ("2", None), ("1", None), ("0", None), ("256", None), ("-5", None),
    ("+70", None), (" 70", None), ("70 ", None), ("", None), ("abc", None),
    ("７０", None),  # full-width digits: isdigit() is True, isascii() is not
])
def test_only_3_to_255_count(monkeypatch, raw, expected):
    sys.path.insert(0, str(TOOLS_DIR))
    import _lib_exitcodes as ec  # noqa: E402
    monkeypatch.setenv(ENV, raw)
    assert ec.crash_exit_code_from_env() == expected
    ec.set_crash_exit_code(1)
    assert ec.effective_crash_exit_code() == (1 if expected is None else expected)
    assert ec.get_crash_exit_code() == 1


def test_an_ignored_value_leaves_the_real_crash_code(tmp_path):
    proc = _run(_script(tmp_path, "raise RuntimeError('boom')\n"),
                env=dict(_minimal_env(), **{ENV: "2"}))
    assert proc.returncode == 2, proc.stderr


_NOT_CRASHES = {
    "die_caller_error": "_lib_exitcodes.die_caller_error('ERROR: bad input')\n",
    "CallerError": "raise _lib_exitcodes.CallerError('bad input')\n",
    "closed stdout": "print('partial')\nos.close(1)\nraise RuntimeError('boom')\n",
}


@pytest.mark.parametrize("how", sorted(_NOT_CRASHES))
def test_the_caller_error_code_does_not_move(tmp_path, how):
    """A named caller error, and a crash whose stdout was closed (that exit is
    the caller-error code, #3124), keep 2 under the variable."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    proc = _run(_script(tmp_path, _NOT_CRASHES[how]), env=env)
    assert proc.returncode == 2, proc.stderr


@pytest.mark.real_crash_exit_code
def test_the_opt_out_marker_restores_the_real_crash_code(tmp_path):
    """Red when the marker stops removing the variable — for this process,
    an inherited env and an explicit one alike."""
    assert ENV not in os.environ
    script = _script(tmp_path, "raise RuntimeError('boom')\n")
    assert _run(script).returncode == 2
    assert _run(script, env=_minimal_env()).returncode == 2
    # an env copied from os.environ before the test began still carries it
    assert _run(script, env=_ENV_AT_IMPORT).returncode == 2


def test_the_opt_out_does_not_outlive_its_test():
    """Runs after the marked test in file order (and on its own under -n)."""
    assert os.environ.get(ENV) == str(SESSION_CODE)
