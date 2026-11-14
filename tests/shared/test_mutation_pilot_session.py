"""The Python mutation pilot runs one pytest session per mutant; none of them may build Go.

tests/conftest.py builds da-guard, da-crdecode and da-read at the start of a
session that declares `VIBE_TEST_GO_BINARIES=build` (#2906, #3206). The
pilot (tests/shared/_mutation_pilot.py) starts a fresh pytest session per
mutant under a 120 s timeout; on the nightly runner's cold Go cache the
first one spent 338 s in those builds, the timeout was raised uncaught and
the whole run died before recording a single verdict (#3871).

So the pilot builds the binaries once, before the first mutant, and hands
each session the paths conftest's reuse branch looks for
(`VIBE_TEST_<VAR>` naming an existing file). And a session that does run
out of time is one mutant's SETUP-FAIL, not the end of the run.

The build cases use a fake `go` first on PATH that records every call and
writes the requested output file, so "was go called" is a fact, not an
inference from timing.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS = REPO_ROOT / "tests"

_BUILD_ENV = ("DA_GUARD_BINARY", "DA_CRDECODE_BINARY", "DA_READ_BINARY",
              "VIBE_TEST_DA_GUARD_BINARY", "VIBE_TEST_DA_CRDECODE_BINARY",
              "VIBE_TEST_DA_READ_BINARY", "VIBE_TEST_GO_BINARIES", "GITHUB_ACTIONS")


def _load_pilot():
    path = TESTS / "shared" / "_mutation_pilot.py"
    spec = importlib.util.spec_from_file_location("_mutation_pilot_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # @dataclass resolves its module by name
    spec.loader.exec_module(module)
    return module


pilot = _load_pilot()


def _fake_go(tmp_path: Path) -> "tuple[Path, Path]":
    """A `go` that logs its argv and creates the `-o` file, as a build would."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "go-calls.log"
    go = bindir / "go"
    go.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> {log}\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "-o" ]; then printf "#!/bin/sh\\n" > "$2"; chmod 755 "$2"; fi\n'
        "  shift\n"
        "done\n",
        encoding="utf-8")
    go.chmod(0o755)
    return bindir, log


def _calls(log: Path) -> "list[str]":
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


pytestmark = pytest.mark.skipif(os.name == "nt", reason="the fake go is a POSIX shell script")


def test_the_pilot_builds_every_binary_once_and_names_each_for_reuse(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bindir, log = _fake_go(tmp_path)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    for k in _BUILD_ENV:
        monkeypatch.delenv(k, raising=False)
    out = tmp_path / "out"
    out.mkdir()

    env = pilot.prebuild_go_binaries(out)

    from _lib_consumer_read import READER_BUILD_SOURCES
    assert len(_calls(log)) == len(READER_BUILD_SOURCES), _calls(log)
    for var, (_module, pkg, name) in READER_BUILD_SOURCES.items():
        assert any(pkg in c for c in _calls(log)), f"{pkg} not built: {_calls(log)}"
        path = env[var]
        assert os.path.isfile(path) and Path(path).name == name
        # conftest's reuse branch reads `VIBE_TEST_<VAR>`.
        assert env["VIBE_TEST_" + var] == path
        # #3178: a suite that drops to `nobody` must be able to run it.
        assert os.stat(path).st_mode & 0o777 == 0o755
    assert os.stat(out).st_mode & 0o777 == 0o755


def test_a_session_given_the_prebuilt_paths_calls_go_zero_times(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bindir, log = _fake_go(tmp_path)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    for k in _BUILD_ENV:
        monkeypatch.delenv(k, raising=False)
    out = tmp_path / "out"
    out.mkdir()
    env = pilot.prebuild_go_binaries(out)
    before = len(_calls(log))

    probe = tmp_path / "test_probe.py"
    probe.write_text("def test_probe():\n    pass\n", encoding="utf-8")
    child = {k: v for k, v in os.environ.items() if k not in _BUILD_ENV}
    child.update(env)
    child["VIBE_TEST_GO_BINARIES"] = "build"
    child["PYTHONPATH"] = str(TESTS)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "conftest", "-p", "no:cacheprovider",
         "-q", "--rootdir", str(tmp_path), str(probe)],
        cwd=tmp_path, env=child, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=300)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _calls(log)[before:] == [], f"the session rebuilt: {_calls(log)[before:]}"


def test_skip_builds_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bindir, log = _fake_go(tmp_path)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("VIBE_TEST_GO_BINARIES", "skip")
    assert pilot.prebuild_go_binaries(tmp_path) == {}
    assert _calls(log) == []


def test_a_failed_build_stops_the_pilot_with_go_s_message(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    go = bindir / "go"
    go.write_text('#!/bin/sh\necho "fake go: broken module" >&2\nexit 1\n', encoding="utf-8")
    go.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("VIBE_TEST_GO_BINARIES", raising=False)
    with pytest.raises(pilot.PrebuildError, match="broken module"):
        pilot.prebuild_go_binaries(tmp_path)


def test_every_session_gets_the_prebuilt_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: "list[dict[str, str] | None]" = []

    def fake_run(cmd, **kw):
        seen.append(kw.get("env"))
        return subprocess.CompletedProcess(cmd, 0, stdout="1 passed\n", stderr="")

    monkeypatch.setattr(pilot.subprocess, "run", fake_run)
    pilot.run_tests("tests/x.py", {"DA_GUARD_BINARY": "/b/da-guard"})
    assert seen and seen[0] is not None
    assert seen[0]["DA_GUARD_BINARY"] == "/b/da-guard"
    assert seen[0]["PATH"] == os.environ["PATH"], "the rest of the environment is inherited"


@pytest.mark.parametrize("output", [b"collected 3\npartial line\n", "collected 3\npartial line\n", None])
def test_a_session_that_times_out_is_one_timeout(monkeypatch: pytest.MonkeyPatch, output) -> None:
    # A real TimeoutExpired carries bytes even under text=True.
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 0), output=output)

    monkeypatch.setattr(pilot.subprocess, "run", fake_run)
    rc, tail = pilot.run_tests("tests/x.py", {})
    assert rc is None
    assert pilot.verdict(rc, tail).startswith("TIMEOUT")
    if output:
        assert "partial line" in tail


def test_a_timeout_is_reported_apart_from_catalog_rot_and_turns_the_run_red(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    m = pilot.MUTATIONS[0]
    monkeypatch.setattr(pilot, "prebuild_go_binaries", lambda dest: {})
    monkeypatch.setattr(pilot, "run_tests", lambda target, env: (None, "stalled"))
    monkeypatch.setattr(pilot.Mutation, "apply", lambda self: None)
    monkeypatch.setattr(pilot.Mutation, "revert", lambda self, original: None)
    assert pilot._run([m], "/nonexistent") == 1
    out = capsys.readouterr().out
    assert "0 setup-failures, 1 timeouts" in out
    assert "TIMEOUTS (" in out and "SETUP FAILURES" not in out


def test_a_build_past_its_timeout_is_a_prebuild_error(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("VIBE_TEST_GO_BINARIES", raising=False)
    monkeypatch.setattr(pilot.shutil, "which", lambda name: "/usr/bin/" + name)

    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 0))

    monkeypatch.setattr(pilot.subprocess, "run", fake_run)
    with pytest.raises(pilot.PrebuildError, match="ran past"):
        pilot.prebuild_go_binaries(tmp_path)


@pytest.mark.parametrize("rc,prefix", [(0, "SURVIVED"), (1, "CAUGHT"),
                                       (2, "SETUP-FAIL"), (4, "SETUP-FAIL"),
                                       (5, "SETUP-FAIL")])
def test_verdicts_by_return_code(rc: int, prefix: str) -> None:
    assert pilot.verdict(rc, "tail").startswith(prefix)
