"""Every CI step that runs pytest declares whether its session builds Go binaries.

`tests/conftest.py` builds da-guard, da-crdecode and da-read at the start of
every pytest session that has `go` (minutes on a cold cache), unless
`VIBE_TEST_GO_BINARIES=skip`. Left implicit, a light job pays those minutes
without anyone deciding it should — and that is how `E2E Spec Lint (A-13)`
came to time out. So a workflow step that runs pytest must say which it is:
`VIBE_TEST_GO_BINARIES` is `build` or `skip` in the step's, the job's or the
workflow's `env`. A wrong `skip` is not silent: a test that needs a binary
fails with the tool's "binary missing" error.

"Runs pytest" is decided on the shell commands of `run:`, not by searching
for the word: `pip install pytest` installs it, `pytest …`, `python -m pytest
…` and `timeout 600 pytest …` run it. A workflow file that does not parse
fails here rather than being skipped. Not covered: pytest started indirectly
(a `make` target, a script) — no workflow does that today.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOWS = _REPO / ".github" / "workflows"
_ENV = "VIBE_TEST_GO_BINARIES"
_VALUES = {"build", "skip"}

# Words that may precede the command itself.
_WRAPPERS = {"timeout", "env", "time", "nice", "exec", "xvfb-run"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _commands(script: str) -> list[list[str]]:
    """The simple commands of a shell script, each as its words."""
    joined = re.sub(r"\\\n", " ", script)
    out: list[list[str]] = []
    for line in joined.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for part in re.split(r"&&|\|\||;|\||\$\(|`|\(|\)", line):
            try:
                words = shlex.split(part, comments=True)
            except ValueError:
                words = part.split()
            if words:
                out.append(words)
    return out


def runs_pytest(script: str) -> bool:
    for words in _commands(script):
        i = 0
        while i < len(words) and (_ASSIGNMENT.match(words[i]) or words[i] in _WRAPPERS
                                  or words[i].startswith("-") or words[i].isdigit()):
            i += 1
        rest = words[i:]
        if not rest:
            continue
        cmd = rest[0].rsplit("/", 1)[-1]
        if cmd == "pytest":
            return True
        if re.fullmatch(r"python3?(\.\d+)?", cmd) and rest[1:3] == ["-m", "pytest"]:
            return True
    return False


def _workflow_files() -> list[Path]:
    return sorted(p for p in _WORKFLOWS.iterdir() if p.suffix in {".yml", ".yaml"})


def pytest_steps_without_a_declaration(path: Path) -> list[str]:
    """`job / step` for each step of `path` that runs pytest without a valid
    `VIBE_TEST_GO_BINARIES`; raises if the file does not parse."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise AssertionError(f"{path.name}: not a workflow mapping")
    wf_env = doc.get("env") or {}
    bad: list[str] = []
    for job_id, job in (doc.get("jobs") or {}).items():
        job_env = (job or {}).get("env") or {}
        for n, step in enumerate((job or {}).get("steps") or []):
            run = step.get("run")
            if not isinstance(run, str) or not runs_pytest(run):
                continue
            env = {**wf_env, **job_env, **(step.get("env") or {})}
            value = env.get(_ENV)
            if str(value) not in _VALUES:
                bad.append(f"{job_id} / {step.get('name') or f'step {n}'}: {_ENV}={value!r}")
    return bad


def test_the_scan_sees_the_known_pytest_steps() -> None:
    names = {p.name for p in _workflow_files()}
    assert {"ci.yml", "playwright.yml"} <= names
    ci = yaml.safe_load((_WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    runs = [s.get("run", "") for s in ci["jobs"]["python-tests-run"]["steps"]]
    assert any(runs_pytest(r) for r in runs if isinstance(r, str))


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_every_pytest_step_declares_whether_it_builds_go_binaries(path: Path) -> None:
    bad = pytest_steps_without_a_declaration(path)
    assert bad == [], (
        f"set {_ENV} to 'build' or 'skip' (step, job or workflow env) on:\n  "
        + "\n  ".join(bad))


@pytest.mark.parametrize("script", [
    "pytest tests/ -q",
    "python -m pytest tests/x.py -v",
    "python3 -m pytest -q",
    "timeout 600 pytest tests/",
    "FOO=1 pytest tests/",
    "cd tools && pytest",
    "pip install x \\\n  && pytest -q",
    "set -e\nout=$(pytest -q)",
])
def test_these_run_pytest(script: str) -> None:
    assert runs_pytest(script)


@pytest.mark.parametrize("script", [
    "pip install pytest pyyaml -c requirements/ci-constraints.txt",
    "python -m pip install pytest",
    "echo 'run pytest later'",
    "# pytest tests/",
    "pre-commit run tool-map-check --all-files",
    "npx playwright test",
])
def test_these_do_not(script: str) -> None:
    assert not runs_pytest(script)
