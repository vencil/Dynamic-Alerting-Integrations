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
fails here rather than being skipped. A pytest started indirectly (a script
that runs it: the mutation pilot, the federation E2E driver) is not visible
here; tests/conftest.py refuses an undeclared session when GITHUB_ACTIONS is
true, which catches those when they run.
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

# Words that may precede the command itself: wrappers, and the shell keywords
# that open a compound command.
_WRAPPERS = {"timeout", "env", "time", "nice", "exec", "xvfb-run", "sudo",
             "xargs", "then", "do", "else", "elif", "!", "{"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SHELLS = {"bash", "sh", "dash", "zsh"}


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


def _runs_pytest(words: list[str]) -> bool:
    i = 0
    while i < len(words) and (_ASSIGNMENT.match(words[i]) or words[i] in _WRAPPERS
                              or words[i].startswith("-") or words[i].isdigit()):
        i += 1
    rest = words[i:]
    if not rest:
        return False
    cmd = rest[0].rsplit("/", 1)[-1]
    if cmd in {"pytest", "py.test"}:
        return True
    if cmd == "uv" and rest[1:2] == ["run"]:
        return _runs_pytest(rest[2:])
    if cmd in _SHELLS and "-c" in rest[1:]:
        j = rest.index("-c", 1)
        return j + 1 < len(rest) and runs_pytest(rest[j + 1])
    # `python -m pytest`, with interpreter flags before it (`-X dev`, `-u`),
    # `coverage run -m pytest`, an interpreter named by an expression.
    return any(rest[k:k + 2] == ["-m", "pytest"] for k in range(1, len(rest) - 1))


def runs_pytest(script: str) -> bool:
    return any(_runs_pytest(words) for words in _commands(script))


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
    "uv run pytest -q",
    "python -X dev -m pytest",
    "python -u -m pytest tests/",
    "coverage run -m pytest",
    "bash -c 'pytest -q'",
    "sudo pytest",
    "xargs pytest",
    "py.test tests/",
    "if true; then pytest; fi",
    "for f in a b; do pytest $f; done",
    "{ pytest; }",
    "! pytest",
    "${{ matrix.py }} -m pytest",
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
    "python -m pip install pytest pytest-xdist",
    "uv pip install pytest",
    "bash -c 'echo pytest'",
])
def test_these_do_not(script: str) -> None:
    assert not runs_pytest(script)
