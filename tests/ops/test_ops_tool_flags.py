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
import shutil
import subprocess
import sys
import tempfile
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


# ── the rewrite goes through the artifact entry (#3981) ──────────────────

#: tool → (the positional's metavar, a Go file the tool changes).
_CHANGES = {
    "add_t_parallel.py": ("FILE_OR_DIR",
                          "package p\n\nimport \"testing\"\n\nfunc TestA(t *testing.T) {\n\t_ = 1\n}\n"),
    "route_metrics_through_field.py": ("FILE", "package p\n\nfunc f() {\n\tIncParseFailure()\n}\n"),
    "use_get_metrics.py": ("FILE", "package p\n\nfunc f() {\n\tm.metrics.Inc()\n}\n"),
}
#: A file none of the three has anything to change in, with CRLF line ends.
_NOTHING_TO_DO = b"package p\r\n\r\nfunc g() {}\r\n"


def _run_with_stdin(tool: str, stdin_path: Path, *argv: str) -> subprocess.CompletedProcess:
    with open(stdin_path, "rb") as fh:
        return subprocess.run([sys.executable, str(OPS / tool), *argv], stdin=fh,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120, env=_ENV)


@pytest.mark.parametrize("tool", CODEMODS)
def test_a_target_that_is_stdin_is_refused_and_the_others_are_left_alone(tool, tmp_path):
    """Both files need the rewrite; the second is this process's stdin. Every
    target is judged before the first write: rc 2, one line naming the file
    and the positional, and the first file keeps its bytes."""
    metavar, before = _CHANGES[tool]
    first, second = tmp_path / "a_test.go", tmp_path / "b_test.go"
    first.write_text(before, encoding="utf-8")
    second.write_text(before, encoding="utf-8")
    proc = _run_with_stdin(tool, second, str(first), str(second))
    assert proc.returncode == 2, proc.stderr
    assert CRASH not in proc.stderr
    assert proc.stderr.strip().splitlines() == [
        f"ERROR: cannot write {second}: is this process's standard stream"
        f" — check the value given to {metavar}"], proc.stderr
    assert first.read_text(encoding="utf-8") == before
    assert proc.stdout == ""


@pytest.mark.parametrize("tool", CODEMODS)
def test_a_file_with_nothing_to_change_is_left_alone_even_as_stdin(tool, tmp_path):
    """No rewrite, so no judgement of the target either: rc 0 with the file
    as stdin, and its bytes — CRLF included — kept. route_metrics_through_field
    used to rewrite every file it was given, turning CRLF into LF."""
    target = tmp_path / "a_test.go"
    target.write_bytes(_NOTHING_TO_DO)
    proc = _run_with_stdin(tool, target, str(target))
    assert proc.returncode == 0, proc.stderr
    assert target.read_bytes() == _NOTHING_TO_DO


@pytest.mark.parametrize("before,after", [(0o640, 0o644), (0o750, 0o755)])
@pytest.mark.parametrize("tool", CODEMODS)
def test_a_rewritten_file_gets_the_mode_a_checkout_would_give(tool, before, after, tmp_path):
    """The artifact entry: the owner-execute bit is kept, nothing else of
    the old mode (a bare write kept 0640 as it was)."""
    target = tmp_path / "a_test.go"
    target.write_text(_CHANGES[tool][1], encoding="utf-8")
    target.chmod(before)
    proc = _run(tool, str(target))
    assert proc.returncode == 0, proc.stderr
    assert target.read_text(encoding="utf-8") != _CHANGES[tool][1]
    assert target.stat().st_mode & 0o777 == after


@pytest.mark.parametrize("tool", CODEMODS)
def test_a_write_that_fails_is_rc2_one_line_naming_the_positional(tool):
    """The target can be read but neither replaced nor written in place:
    rc 2, one line naming the file and the positional, where a bare write
    was `unexpected internal error (PermissionError)`. Root can write
    anything, so under root the tool runs as uid 65534 on a root-owned file
    in a root-owned directory; otherwise the file and directory are read-only."""
    metavar, before = _CHANGES[tool]
    prefix: list[str] = []
    if os.geteuid() == 0:
        if shutil.which("setpriv") is None:
            pytest.skip("root, and no setpriv to drop to another uid")
        prefix = ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups"]
    d = Path(tempfile.mkdtemp(dir="/var/tmp"))  # tmp_path is 0700: 65534 could not reach it
    try:
        target = d / "a_test.go"
        target.write_text(before, encoding="utf-8")
        if prefix:
            d.chmod(0o755)
        else:
            target.chmod(0o444)
            d.chmod(0o555)
        proc = subprocess.run([*prefix, sys.executable, str(OPS / tool), str(target)],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120, env=_ENV)
        assert proc.returncode == 2, proc.stderr
        assert CRASH not in proc.stderr
        assert proc.stderr.strip().splitlines()[-1] == (
            f"ERROR: cannot write {target}: Permission denied (errno 13)"
            f" — check the value given to {metavar}"), proc.stderr
        assert target.read_text(encoding="utf-8") == before
    finally:
        d.chmod(0o755)
        shutil.rmtree(d)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
@pytest.mark.parametrize("where", ["argv", "dir"])
def test_add_t_parallel_refuses_a_fifo_before_any_write(where, tmp_path):
    """A FIFO named `*_test.go` (given, or met in a directory walk) blocked the
    read forever, after the files before it had been rewritten. Now it is
    rc 2, one line naming it, and nothing is written."""
    _metavar, before = _CHANGES["add_t_parallel.py"]
    good = tmp_path / "a_test.go"
    good.write_text(before, encoding="utf-8")
    fifo = tmp_path / "p_test.go"
    os.mkfifo(fifo)
    argv = [str(tmp_path)] if where == "dir" else [str(good), str(fifo)]
    proc = _run("add_t_parallel.py", *argv)
    assert proc.returncode == 2, proc.stderr
    assert proc.stderr.strip().splitlines() == [
        f"ERROR: cannot read {fifo}: not a regular file"
        " — check the value given to FILE_OR_DIR"], proc.stderr
    assert good.read_text(encoding="utf-8") == before


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
