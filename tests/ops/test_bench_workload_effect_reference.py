"""bench-workload-effect.yaml: which reference R is (#2348), run as shell.

The diagnostic workflow's reference used to be hard-wired to the pinned nightly
reference (`.github/bench-reference.yaml`), which main's benchmark code had
outgrown: the overlay has not compiled on it since 2026-09-23, so W/R and M/W
were unavailable and the PR self-test could only be red. The step under test
now resolves R from three sources, and these tests run its real `run:` script
with a stub `gh` so each branch is executed, not grepped:

  * dispatch, `reference_sha` empty   → the pinned reference (kind=pinned);
  * dispatch, `reference_sha` set     → that SHA, only if 40-hex AND main
    contains it (compare API status `ahead` / `identical`), else exit 1;
  * pull_request self-test            → the first parent of the checked-out
    merge commit, i.e. main's tip under the PR (kind=self-test).

The self-test's probe of the pinned reference is a notice that never fails the
job; that is executed here too, with a stub `go`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/bench-workload-effect.yaml"
PIN = ROOT / ".github/bench-reference.yaml"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="needs bash")

_SHA_IN = "a" * 40
_SHA_MB = "b" * 40


def _step(name: str) -> dict:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    steps = [s for s in jobs["workload-effect"]["steps"] if s.get("name") == name]
    assert len(steps) == 1, f"expected one step named {name!r}, found {len(steps)}"
    return steps[0]


def _pinned_sha() -> str:
    for line in PIN.read_text(encoding="utf-8").splitlines():
        if line.startswith("sha:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError("no sha in bench-reference.yaml")


def _run(tmp_path: Path, script: str, env: dict, gh: str | None = None,
         go: str | None = None, pin: str | None = None,
         git: str | None = None) -> tuple[int, dict, str]:
    work = tmp_path / "work"
    (work / ".github").mkdir(parents=True)
    (work / ".github/bench-reference.yaml").write_text(
        pin if pin is not None else PIN.read_text(encoding="utf-8"), encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.log"
    for tool, body in (("gh", gh), ("go", go), ("git", git)):
        if body is not None:
            stub = bindir / tool
            stub.write_text("#!/usr/bin/env bash\n"
                            f"echo \"{tool} $*\" >> {calls}\n" + body,
                            encoding="utf-8", newline="\n")
            stub.chmod(0o755)
    out = tmp_path / "gh-output"
    summary = tmp_path / "summary"
    full_env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summary),
        "GITHUB_REPOSITORY": "o/r", "GITHUB_WORKSPACE": str(work),
        "SELF_TEST": "false", "INPUT_SHA": "",
        **env,
    }
    # ⛔ `-e`, as GitHub runs a `run:` without `shell:` (`bash -e {0}`). Plain
    # `bash -c` hid a probe that died on a failing command substitution
    # (#3727's first self-test, run 42846211639).
    proc = subprocess.run([BASH, "-e", "-c", script], cwd=work, env=full_env,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    outputs = {}
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            outputs[key] = value
    log = calls.read_text(encoding="utf-8") if calls.exists() else ""
    return proc.returncode, outputs, proc.stdout + proc.stderr + log


@needs_bash
def test_an_empty_dispatch_input_uses_the_pinned_reference(tmp_path) -> None:
    rc, out, log = _run(tmp_path, _step("Resolve the reference version")["run"], {},
                        gh="exit 99\n")
    assert rc == 0, log
    assert out["kind"] == "pinned" and out["sha"] == _pinned_sha(), out
    assert "gh " not in log, f"the pinned path must not consult the API:\n{log}"


@needs_bash
@pytest.mark.parametrize("status", ["ahead", "identical"])
def test_a_commit_main_contains_is_accepted(tmp_path, status) -> None:
    rc, out, log = _run(tmp_path, _step("Resolve the reference version")["run"],
                        {"INPUT_SHA": _SHA_IN}, gh=f"echo {status}\n")
    assert rc == 0, log
    assert out["kind"] == "input" and out["sha"] == _SHA_IN, out
    assert f"compare/{_SHA_IN}...main" in log, log


@needs_bash
@pytest.mark.parametrize("gh, reason", [
    ("echo diverged\n", "diverged"),
    ("echo behind\n", "behind"),
    ("exit 1\n", "unreadable"),
], ids=["diverged", "behind", "api-fails"])
def test_a_commit_main_does_not_contain_is_refused(tmp_path, gh, reason) -> None:
    """⛔ Refused BEFORE any checkout: no `sha` output means the checkout step
    gets nothing to build — a fork commit or a stray ref never runs here."""
    rc, out, log = _run(tmp_path, _step("Resolve the reference version")["run"],
                        {"INPUT_SHA": _SHA_IN}, gh=gh)
    assert rc == 1, log
    assert "sha" not in out, out
    assert reason in log, log


@needs_bash
@pytest.mark.parametrize("value", ["main", "abc123", "A" * 40, _SHA_IN + "\n"],
                         ids=["branch-name", "short", "uppercase", "newline"])
def test_a_malformed_input_is_refused_without_asking_the_api(tmp_path, value) -> None:
    rc, out, log = _run(tmp_path, _step("Resolve the reference version")["run"],
                        {"INPUT_SHA": value}, gh="echo ahead\n")
    assert rc == 1, log
    assert "sha" not in out and "gh " not in log, (out, log)


def _commit(*parents: str) -> str:
    """A `git cat-file -p HEAD` stub printing a commit with these parents."""
    lines = ["tree " + "f" * 40, *(f"parent {p}" for p in parents),
             "author a <a> 0 +0000", "committer a <a> 0 +0000", "", "msg"]
    return "printf '%s\\n' " + " ".join(f"'{l}'" for l in lines) + "\n"


@needs_bash
def test_the_self_test_uses_main_tip_under_the_pr(tmp_path) -> None:
    """R = the merge commit's FIRST parent, so R and W differ by this PR only.
    ⛔ Not the merge base: a PR branched before a coupled bench+API merge would
    overlay today's benchmarks onto an older tree and go red (blind review)."""
    rc, out, log = _run(tmp_path, _step("Resolve the reference version")["run"],
                        {"SELF_TEST": "true"}, git=_commit(_SHA_MB, "d" * 40),
                        gh="exit 99\n")
    assert rc == 0, log
    assert out["kind"] == "self-test" and out["sha"] == _SHA_MB, out
    assert out["pinned_sha"] == _pinned_sha(), out
    assert "gh " not in log, f"the self-test must not need the API:\n{log}"


@needs_bash
def test_an_unusable_pin_is_still_fatal(tmp_path) -> None:
    rc, out, log = _run(tmp_path, _step("Resolve the reference version")["run"], {},
                        pin="tag: nope\nsha: 123\n")
    assert rc == 1 and "sha" not in out, (out, log)


@needs_bash
@pytest.mark.parametrize("go, word", [
    ("echo './config_bench_test.go:325:16: undefined: scanDirTree'; exit 1\n", "does NOT build"),
    ("exit 0\n", "also builds"),
    # blind review: a go failure naming no file used to read as "builds"
    ("echo 'dial tcp: i/o timeout'; exit 1\n", "UNKNOWN"),
    ("exit 1\n", "UNKNOWN"),
], ids=["broken", "builds", "network", "silent"])
def test_the_pinned_probe_is_a_notice_never_a_failure(tmp_path, go, word) -> None:
    step = _step("Probe the pinned nightly reference (self-test, notice only)")
    assert step.get("if") == "env.SELF_TEST == 'true'", step.get("if")
    work = tmp_path / "work"
    app = "components/threshold-exporter/app"
    (work / app).mkdir(parents=True)
    (work / ".pinref" / app).mkdir(parents=True)
    (work / app / "x_bench_test.go").write_text("package x\n", encoding="utf-8")
    (work / ".build").mkdir()
    (work / ".build/overlay-files.txt").write_text("x_bench_test.go\n", encoding="utf-8")
    stale = work / ".pinref" / app / "gone_bench_test.go"
    stale.write_text("package x\n", encoding="utf-8")
    rc, _out, log = _run(tmp_path, step["run"], {"PINNED_SHA": "e" * 40}, go=go)
    assert rc == 0, log
    assert "::notice::" in log and word in log, log
    if word == "UNKNOWN":
        assert "also builds" not in log, f"a probe that never compiled read as 'builds':\n{log}"
    assert not stale.exists(), "the probe tree kept a bench file main does not have"


@needs_bash
@pytest.mark.parametrize("parents", [(), (_SHA_MB,), (_SHA_MB, "d" * 40, "e" * 40), ("nothex",  "d" * 40)],
                         ids=["none", "one", "three", "not-a-sha"])
def test_a_head_that_is_not_the_pr_merge_commit_fails_the_self_test(tmp_path, parents) -> None:
    rc, out, log = _run(tmp_path, _step("Resolve the reference version")["run"],
                        {"SELF_TEST": "true"}, git=_commit(*parents))
    assert rc != 0 and "sha" not in out, (rc, out, log)


@needs_bash
def test_typing_the_pinned_sha_is_the_pinned_reference(tmp_path) -> None:
    """Blind review: the pinned SHA entered by hand used to get kind=input and
    the 'not the nightly reference' banner, though it is exactly that."""
    rc, out, log = _run(tmp_path, _step("Resolve the reference version")["run"],
                        {"INPUT_SHA": _pinned_sha()}, gh="exit 99\n")
    assert rc == 0 and out["kind"] == "pinned", (out, log)
    assert "gh " not in log, log


@needs_bash
@pytest.mark.parametrize("kind, present, absent", [
    ("pinned", "拿去對當夜 bench-paired.json", "本次沒有這道對照"),
    ("input", "本次沒有這道對照", "拿去對當夜 bench-paired.json"),
    ("self-test", "本次沒有這道對照", "拿去對當夜 bench-paired.json"),
], ids=["pinned", "input", "self-test"])
def test_the_summary_drops_the_nightly_cross_check_off_the_pin(tmp_path, kind, present, absent) -> None:
    """Blind review: off the pinned reference the summary still told the reader
    to reconcile with the nightly M/R, which this run cannot be compared to."""
    step = _step("Render step summary")
    rc, _out, log = _run(tmp_path, step["run"], {
        "REF_KIND": kind, "REF_TAG": "t", "REF_SHA": "a" * 40, "BENCH_RE": "x",
        "OVERLAY_HELPERS": "", "SELF_TEST": "false"})
    summary = (tmp_path / "summary").read_text(encoding="utf-8")
    assert present in summary and absent not in summary, summary
