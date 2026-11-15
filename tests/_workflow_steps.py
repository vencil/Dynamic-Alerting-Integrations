"""Reading and running workflow `run:` steps the way GitHub does (#3946).

  * `run_steps` — every `run:` step of every workflow and composite action,
    with the `shell:` that is in effect for it. Files that cannot be read are
    returned, not skipped.
  * `step_shell_argv` / `run_step` — the WHOLE `run:` text, in a file, under
    the flags GitHub gives that step's shell. ⛔ Never a slice of the script
    and never `bash -c`: a step with no `shell:` runs as `bash -e {0}`, so a
    bare non-zero exit ends it on the spot, and a test that starts after that
    line (or runs without `-e`) cannot see it (#3944, #3946).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github/workflows"
ACTIONS_DIR = REPO_ROOT / ".github/actions"

# What GitHub documents for `jobs.<id>.steps[*].shell`. Only these two are
# modelled; any other value is refused by `step_shell_argv`, never guessed.
_SHELL_FLAGS = {
    None: ["-e"],
    "bash": ["--noprofile", "--norc", "-eo", "pipefail"],
}


class UnmodelledShell(Exception):
    """A `shell:` whose flags this module does not know."""


def bash_path() -> str:
    """bash, or a failure — never a skip on CI: "could not look" must not read
    as "looked and found nothing"."""
    import pytest
    bash = shutil.which("bash")
    if bash is None:
        if os.environ.get("CI"):
            pytest.fail("bash is not on PATH: no workflow step can be executed")
        pytest.skip("needs bash")
    return bash


def step_shell_argv(shell: str | None) -> list[str]:
    """The argv GitHub runs a `run:` step with, from the step's EFFECTIVE
    `shell:` (see `effective_shell`). No `shell:` → `bash -e {0}`;
    `shell: bash` → `bash --noprofile --norc -eo pipefail {0}`. Either way
    errexit is ON, and `set -uo pipefail` in the script does not turn it off."""
    if shell not in _SHELL_FLAGS:
        raise UnmodelledShell(f"unmodelled `shell: {shell}` — teach "
                              "tests/_workflow_steps.py its flags")
    return [bash_path(), *_SHELL_FLAGS[shell]]


def effective_shell(doc: dict, job: dict, step: dict) -> str | None:
    """`shell:` on the step, else the job's `defaults.run.shell`, else the
    workflow's — GitHub's precedence."""
    for holder in (step, (job.get("defaults") or {}).get("run") or {},
                   (doc.get("defaults") or {}).get("run") or {}):
        if holder.get("shell") is not None:
            return holder["shell"]
    return None


@dataclass(frozen=True)
class RunStep:
    file: str            # relative to `.github/`, e.g. `workflows/ci.yml`
    job: str             # the job id; `runs` for a composite action
    step: str            # the step's `name:`, or `#<index>` when it has none
    shell: str | None
    run: str

    @property
    def label(self) -> str:
        return f"{self.file} · {self.job} · {self.step}"


def _steps_of(file: str, job_id: str, doc: dict, job: dict) -> list[RunStep]:
    found = []
    for index, step in enumerate(job.get("steps") or []):
        # Also what a `steps:` that is a map or a string comes down to: its
        # items are keys or characters. Without this, `"run" in step` is a
        # substring test and the job reads as having no `run:` at all.
        if not isinstance(step, dict):
            raise TypeError(f"{job_id} step #{index}: not a mapping")
        if "run" not in step:
            continue
        if not isinstance(step["run"], str):
            raise TypeError(f"{job_id} step #{index}: `run:` is not a string")
        found.append(RunStep(file, job_id, str(step.get("name") or f"#{index}"),
                             effective_shell(doc, job, step), step["run"]))
    return found


def run_steps(workflow_dir: Path = WORKFLOW_DIR,
              actions_dir: Path | None = ACTIONS_DIR) -> tuple[list[RunStep], list[str]]:
    """Every `run:` step of every workflow under `workflow_dir` and of every
    composite action under `actions_dir`, and the files that could not be
    read as one — returned, not skipped: the caller must fail on them."""
    steps: list[RunStep] = []
    unread: list[str] = []
    paths = [(p, f"workflows/{p.name}") for p in
             sorted([*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")])]
    if actions_dir is not None and actions_dir.is_dir():
        paths += [(p, f"actions/{p.relative_to(actions_dir).as_posix()}") for p in
                  sorted([*actions_dir.rglob("action.yml"), *actions_dir.rglob("action.yaml")])]
    for path, file in paths:
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            if file.startswith("actions/"):
                if not isinstance(doc, dict) or not isinstance(doc.get("runs"), dict):
                    raise TypeError("no `runs:` mapping")
                # A composite step has no defaults to inherit: `shell:` is its own.
                steps += _steps_of(file, "runs", {}, doc["runs"])
                continue
            if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
                raise TypeError("no `jobs:` mapping")
            for job_id, job in doc["jobs"].items():
                steps += _steps_of(file, str(job_id), doc, job)
        except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError, AttributeError,
                TypeError) as err:
            unread.append(f"{file}: {type(err).__name__}: {(str(err).splitlines() or [''])[0]}")
    return steps, unread


def run_step(run: str, shell: str | None, tmp_path: Path, env: dict,
             stand_ins: dict[str, str] | None = None, cwd: Path | None = None):
    """Run a step's entire `run:` text under its shell's flags. `stand_ins`:
    command name → the body of a `/bin/sh` script put first on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, body in (stand_ins or {}).items():
        fake = bindir / name
        fake.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        fake.chmod(0o755)
    script = tmp_path / "step.sh"
    script.write_text(run, encoding="utf-8")
    return subprocess.run(
        [*step_shell_argv(shell), str(script)], capture_output=True, text=True,
        encoding="utf-8", timeout=30, check=False, cwd=cwd or tmp_path,
        env={"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", **env})
