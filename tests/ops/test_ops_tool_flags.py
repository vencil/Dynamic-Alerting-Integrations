"""Four scripts/ops tools parse their flags (#3605).

`add_t_parallel`, `route_metrics_through_field` and `use_get_metrics` took
every argument as a path, and `ci_flake_retry` took the first token it did not
know as the command to run: `--help` (or any `--xxx`) crashed with
FileNotFoundError (rc 2 by the crash rule, no usage). Now `--help` is rc 0
with the usage, an unknown flag is argparse's named refusal (rc 2), and a path
or command that is not there is a named rc 2 — for the codemods, judged
before the first file is rewritten.

ci_flake_retry keeps `--` optional (owner, option A): options end at the first
operand, as GNU `timeout` / `env` do.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS = REPO_ROOT / "scripts" / "ops"
CODEMODS = ("add_t_parallel.py", "route_metrics_through_field.py", "use_get_metrics.py")
_ENV = {k: v for k, v in os.environ.items()
        if k not in ("PYTHONUNBUFFERED", "DA_TEST_CRASH_EXIT_CODE")}
CRASH = "unexpected internal error"


def _run(tool: str, *argv: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(OPS / tool), *argv], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120,
                          env=_ENV, cwd=cwd)


@pytest.mark.parametrize("tool", CODEMODS + ("ci_flake_retry.py",))
@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_prints_the_usage_and_is_rc_0(tool, flag):
    proc = _run(tool, flag)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("usage:"), proc.stdout
    assert CRASH not in proc.stderr


@pytest.mark.parametrize("tool", CODEMODS)
def test_a_codemod_refuses_an_unknown_flag_by_name(tool, tmp_path):
    proc = _run(tool, "--bogus", str(tmp_path))
    assert proc.returncode == 2, proc.stderr
    assert "unrecognized arguments: --bogus" in proc.stderr
    assert CRASH not in proc.stderr


@pytest.mark.parametrize("tool", CODEMODS)
def test_a_codemod_with_no_path_is_a_usage_error(tool):
    proc = _run(tool)
    assert proc.returncode == 2, proc.stderr
    assert "the following arguments are required" in proc.stderr


_GO = "package p\n\nimport \"testing\"\n\nfunc TestA(t *testing.T) {\n\tm.metrics.Inc()\n}\n"


@pytest.mark.parametrize("tool", CODEMODS)
def test_a_missing_path_is_named_and_nothing_is_written(tool, tmp_path):
    """The good file comes first: it was rewritten before the crash on the
    missing one. Now every path is judged before any write."""
    good = tmp_path / "a_test.go"
    good.write_text(_GO, encoding="utf-8")
    proc = _run(tool, str(good), str(tmp_path / "missing_test.go"))
    assert proc.returncode == 2, proc.stderr
    assert "missing_test.go: No such file or directory" in proc.stderr
    assert CRASH not in proc.stderr
    assert good.read_text(encoding="utf-8") == _GO
    assert proc.stdout == ""


def test_add_t_parallel_still_rewrites_a_directory(tmp_path):
    (tmp_path / "a_test.go").write_text(_GO.replace("\tm.metrics.Inc()\n", "\t_ = 1\n"),
                                        encoding="utf-8")
    proc = _run("add_t_parallel.py", str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert "t.Parallel()" in (tmp_path / "a_test.go").read_text(encoding="utf-8")


# ── ci_flake_retry ───────────────────────────────────────────────────────

@pytest.mark.parametrize("argv", [["--bogus", "--", sys.executable, "-c", "print('RAN')"],
                                  ["--bogus", sys.executable, "-c", "print('RAN')"]])
def test_ci_flake_retry_refuses_an_unknown_option_and_runs_nothing(argv):
    """Without `--` the unknown option used to be run as the command."""
    proc = _run("ci_flake_retry.py", *argv)
    assert proc.returncode == 2, proc.stderr
    assert "unrecognized arguments: --bogus" in proc.stderr
    assert "RAN" not in proc.stdout


@pytest.mark.parametrize("argv", [["no-such-command-3605", "x"], ["--", "no-such-command-3605"]])
def test_ci_flake_retry_names_a_command_it_cannot_run(argv):
    proc = _run("ci_flake_retry.py", *argv)
    assert proc.returncode == 2, proc.stderr
    assert "cannot run 'no-such-command-3605'" in proc.stderr
    assert CRASH not in proc.stderr


@pytest.mark.parametrize("sep", [[], ["--"]])
def test_ci_flake_retry_still_runs_a_command_with_or_without_the_separator(sep):
    proc = _run("ci_flake_retry.py", *sep, sys.executable, "-c", "print('RAN')")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "RAN\n"
