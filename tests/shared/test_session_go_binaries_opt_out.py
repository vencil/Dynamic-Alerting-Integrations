"""A pytest session can declare it needs no Go binary (`VIBE_TEST_GO_BINARIES=skip`).

tests/conftest.py builds da-guard, da-crdecode and da-read in
``pytest_configure`` for every session that has ``go`` (#2906, #3206): the
tools under test are spread across the suite, some read conf.d at collection
time, and a test may call them under a short subprocess timeout or under
the generator's ``RLIMIT_DATA`` — so the build happens up front, never inside
a test. On a CI runner with a cold Go build cache that costs minutes, which a
job running only a handful of tests that never touch Go (Playwright's
``E2E Spec Lint`` step, ``timeout-minutes: 5``) pays in full and times out
on.

Such a job sets ``VIBE_TEST_GO_BINARIES=skip``: nothing is built and no
``*_BINARY`` variable is set, so a test that does need one fails with the
tool's "binary missing" error — loudly, never a silent skip. Any other
non-empty value is a usage error, so a typo cannot quietly mean "build".

Each case runs a real child pytest that loads tests/conftest.py, with a fake
``go`` first on PATH that records every call and fails.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS = REPO_ROOT / "tests"

# Everything conftest sets or reads for the session builds; the parent session
# has its own values, which the child must not inherit.
_BUILD_ENV = ("DA_GUARD_BINARY", "DA_CRDECODE_BINARY", "DA_READ_BINARY",
              "VIBE_TEST_DA_GUARD_BINARY", "VIBE_TEST_DA_CRDECODE_BINARY",
              "VIBE_TEST_DA_READ_BINARY", "VIBE_TEST_GO_BINARIES")

_PROBE = '''
import os
def test_probe():
    print("PROBE", *(f"{k}={os.environ.get(k, '')}" for k in
          ("DA_GUARD_BINARY", "DA_CRDECODE_BINARY", "DA_READ_BINARY")))
'''


def _run(tmp_path: Path, value: "str | None") -> "tuple[subprocess.CompletedProcess[str], list[str]]":
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "go-calls.log"
    go = bindir / "go"
    go.write_text(f'#!/bin/sh\necho "$@" >> {log}\necho "fake go: not building" >&2\nexit 1\n',
                  encoding="utf-8")
    go.chmod(0o755)
    probe = tmp_path / "test_probe.py"
    probe.write_text(_PROBE, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in _BUILD_ENV}
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONPATH"] = str(TESTS)
    if value is not None:
        env["VIBE_TEST_GO_BINARIES"] = value
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "conftest", "-p", "no:cacheprovider",
         "-q", "-s", "--rootdir", str(tmp_path), str(probe)],
        cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=300)
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc, calls


pytestmark = pytest.mark.skipif(os.name == "nt", reason="the fake go is a POSIX shell script")


def test_skip_builds_nothing_and_sets_no_binary(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, "skip")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert calls == [], f"go was called despite skip: {calls}"
    assert "PROBE DA_GUARD_BINARY= DA_CRDECODE_BINARY= DA_READ_BINARY=" in proc.stdout, proc.stdout


def test_by_default_every_binary_is_built_up_front(tmp_path: Path) -> None:
    # The fake go fails every build, so no variable gets set — what this pins
    # is that configure asked for all three before any test ran.
    proc, calls = _run(tmp_path, None)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    built = " ".join(calls)
    for pkg in ("./cmd/da-guard", "./cmd/da-crdecode"):
        assert pkg in built, f"{pkg} not built up front: {calls}"
    assert len(calls) >= 3, calls


@pytest.mark.parametrize("value", ["Skip", "no", "0", "false", "skp"])
def test_any_other_value_is_a_usage_error(tmp_path: Path, value: str) -> None:
    proc, calls = _run(tmp_path, value)
    assert proc.returncode == 4, (proc.returncode, proc.stdout + proc.stderr)  # pytest UsageError
    assert "VIBE_TEST_GO_BINARIES" in proc.stderr + proc.stdout
    assert calls == []


@pytest.mark.parametrize("value", ["", "build"])
def test_build_and_empty_mean_build(tmp_path: Path, value: str) -> None:
    # `build` is what a CI job declares (test_workflow_pytest_declares_go_binaries);
    # empty behaves like unset, for a local shell.
    proc, calls = _run(tmp_path, value)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert calls, f"{value!r} must build"
