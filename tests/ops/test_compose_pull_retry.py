"""The federation E2E runner pulls its images with retries before `up`.

`Federation E2E (ADR-020 IV-2j)` is a required check
(.github/required-checks.yaml). Its runner, scripts/ops/federation_e2e_run.sh,
pulls each compose stack's images through `compose_pull_with_retry`
(scripts/ops/_compose_pull_retry.sh) so a registry hiccup is retried, and, if
it persists, ends the run with exit 2 and a message that names the image pull
rather than looking like a failed scenario.

Two halves:

  * the function, EXECUTED under bash with `docker` and `sleep` stubbed on
    PATH — attempts, backoff, exit code, message, and the argv it hands docker;
  * the runner's wiring, read from the script text — every `docker compose …
    up` is directly preceded by a pull of the same stack whose exit code is
    propagated.

⚠️ Not covered: the real `docker compose pull` (no daemon in the unit-test
environment) — in particular that `--ignore-buildable` is accepted by the CI
runner's compose version, and that a pulled image makes the following `up`
pull nothing. The stub accepts any flag. The wiring half is a line-level read
of the script: a pull moved into a branch that does not run is not seen.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts/ops/_compose_pull_retry.sh"
RUNNER = ROOT / "scripts/ops/federation_e2e_run.sh"

BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="needs bash")

# Fails while the counter is below ZQ_FAIL_FIRST, then succeeds. Records argv.
_FAKE_DOCKER = """#!/usr/bin/env bash
n=$(cat "$ZQ_STATE/count" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$ZQ_STATE/count"
printf '%s\\n' "$*" >> "$ZQ_STATE/docker-argv"
if [ "$n" -le "$ZQ_FAIL_FIRST" ]; then
  echo "unauthorized: authentication required" >&2
  exit 1
fi
exit 0
"""
_FAKE_SLEEP = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$ZQ_STATE/sleeps"
"""


def _run(tmp_path: Path, fail_first: int, snippet: str,
         lib: Path = LIB) -> tuple[subprocess.CompletedProcess, list[str], list[str]]:
    bindir, state = tmp_path / "bin", tmp_path / "state"
    bindir.mkdir()
    state.mkdir()
    for name, body in (("docker", _FAKE_DOCKER), ("sleep", _FAKE_SLEEP)):
        (bindir / name).write_text(body, encoding="utf-8", newline="\n")
        (bindir / name).chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
           "ZQ_STATE": str(state), "ZQ_FAIL_FIRST": str(fail_first)}
    proc = subprocess.run(
        [BASH, "-c", f'set -euo pipefail\nsource "$1"\n{snippet}', "zq", str(lib)],
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)

    def lines(name: str) -> list[str]:
        path = state / name
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return proc, lines("docker-argv"), lines("sleeps")


# The runner's own call shape, so the exit code asserted is the one the job sees.
_CALL = 'compose_pull_with_retry zqstack -p zqproj -f zq.yml || exit "$?"\necho ZQ-CONTINUED'
_PULL_ARGV = "compose -p zqproj -f zq.yml pull --ignore-buildable"


@needs_bash
@pytest.mark.parametrize("fail_first, pulls, sleeps", [
    (0, 1, []), (1, 2, ["5"]), (2, 3, ["5", "15"])],
    ids=["first-try", "second-try", "third-try"])
def test_a_pull_that_eventually_succeeds_lets_the_runner_continue(
        tmp_path: Path, fail_first: int, pulls: int, sleeps: list[str]) -> None:
    proc, argv, slept = _run(tmp_path, fail_first, _CALL)
    assert proc.returncode == 0, proc.stderr
    assert "ZQ-CONTINUED" in proc.stdout
    assert argv == [_PULL_ARGV] * pulls, argv
    assert slept == sleeps, slept
    assert "IMAGE PULL FAILED" not in proc.stderr


@needs_bash
def test_a_pull_that_never_succeeds_ends_with_2_and_names_the_pull(
        tmp_path: Path) -> None:
    proc, argv, slept = _run(tmp_path, 99, _CALL)
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert "ZQ-CONTINUED" not in proc.stdout
    assert argv == [_PULL_ARGV] * 3, argv
    # No sleep after the last attempt: nothing follows it.
    assert slept == ["5", "15"], slept
    assert "zqstack stack: IMAGE PULL FAILED after 3 attempts" in proc.stderr
    assert "NOT a test failure" in proc.stderr


_UP = re.compile(r"^\s*docker compose\s+(?P<args>.*?)\s*\bup\b")
_PULL = re.compile(
    r'^\s*compose_pull_with_retry\s+[\w-]+\s*(?P<args>.*?)\s*\|\|\s*exit "\$\?"\s*$')


def unpulled_ups(script: str) -> list[str]:
    """`docker compose … up` lines not directly preceded (comments, blank
    lines and `echo` aside) by a propagated pull of the same compose args. PURE."""
    commands = [line for line in script.splitlines()
                if line.strip() and not line.lstrip().startswith(("#", "echo "))]
    problems = []
    for index, line in enumerate(commands):
        up = _UP.match(line)
        if not up:
            continue
        pull = _PULL.match(commands[index - 1]) if index else None
        if not pull or pull.group("args") != up.group("args"):
            problems.append(line.strip())
    return problems


def test_every_compose_up_in_the_runner_follows_a_propagated_pull() -> None:
    script = RUNNER.read_text(encoding="utf-8")
    ups = [line for line in script.splitlines() if _UP.match(line)]
    assert len(ups) >= 2, f"found {len(ups)} `docker compose … up` lines; expected both stacks"
    assert "_compose_pull_retry.sh" in script, "the runner does not source the pull helper"
    assert unpulled_ups(script) == [], (
        "these stacks come up without a retried pull first; an image-pull "
        "failure there reads as a failed required check:", unpulled_ups(script))


def test_the_wiring_finder_can_fail() -> None:
    good = ('compose_pull_with_retry zq -p zq || exit "$?"\n# note\necho up\n'
            "docker compose -p zq up -d\n")
    assert unpulled_ups(good) == []
    for bad in (
            "docker compose -p zq up -d\n",                              # no pull
            'compose_pull_with_retry zq || exit "$?"\ndocker compose -p zq up -d\n',  # other stack
            "compose_pull_with_retry zq -p zq\ndocker compose -p zq up -d\n",       # rc not propagated
            "compose_pull_with_retry zq -p zq || true\ndocker compose -p zq up -d\n",
            'compose_pull_with_retry zq -p zq || exit "$?"\nfoo\ndocker compose -p zq up -d\n'):
        assert unpulled_ups(bad), bad
