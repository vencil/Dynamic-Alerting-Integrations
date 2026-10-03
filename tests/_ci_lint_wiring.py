"""Pin "the CI Lint job runs this pre-commit hook by name, and a failure fails it".

The Lint job in `.github/workflows/ci.yml` runs hooks BY NAME
(`pre-commit run <id> --all-files`, one line each). A hook that is not on that
list has no CI entry point at all: it runs only where a contributor's local
hook happens to. Its own unit tests stay green either way, because they call
the script's functions and never read the workflow.

Extracted from `tests/dx/test_line_ending_policy.py` (#2539, the
`open-encoding-audit` line) so `subprocess-timeout-audit` (#2643) is pinned by
the same reading instead of a second copy.

⛔ Parses the YAML, never greps: `ci.yml` comments already name hook ids. The
`run:` script is compared per logical line (`_logical_shell_lines` drops
comment lines and joins continuations), and the line must be EXACTLY the
command — so `|| true`, `; true` and similar rc-swallowing tails do not match.
Neither the step nor the job may carry `if:` / `continue-on-error`, the step
must not override `shell:`, no `defaults.run.shell` may replace GitHub's
`bash -e`, and the script must not `set +e`.

⚠️ Shapes this does not measure: calling the line from a shell function or an
`if` block, a `trap`, or an earlier step rewriting `pre-commit` itself. None of
them is an idiomatic way to swallow an rc; they are not modelled.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
LINT_JOB = "lint"

# The shared workflow loader and `run:` line splitter. ⛔ Imported, not
# reimplemented: the splitter already knows that a `#` comment is not continued
# by a trailing backslash, which a second copy would have to relearn.
sys.path.insert(0, str(REPO_ROOT / "tests" / "ops"))
from test_ci_path_filter_coverage import (  # noqa: E402
    _load_workflow,
    _logical_shell_lines,
)


def assert_ci_lint_job_runs_hook(hook_id: str, guards: str) -> None:
    """Fail unless the Lint job runs `pre-commit run <hook_id> --all-files` as a gate.

    `guards` names what the hook enforces; it only goes into the failure
    message ("without it <guards> only run where a local hook happens to").
    """
    workflow = _load_workflow(CI_WORKFLOW)
    assert LINT_JOB in workflow.get("jobs", {}), (
        f"job {LINT_JOB!r} is gone from {CI_WORKFLOW.name}; if it was renamed, "
        f"update this pin — do not drop it.")
    job = workflow["jobs"][LINT_JOB]
    for key in ("if", "continue-on-error"):
        assert key not in job, (
            f"{CI_WORKFLOW.name}::{LINT_JOB} has job-level `{key}:` — the "
            f"{hook_id} line would no longer be an unconditional gate.")
    for scope in (workflow, job):
        shell = ((scope.get("defaults") or {}).get("run") or {}).get("shell")
        assert shell is None, (
            f"`defaults.run.shell: {shell}` overrides GitHub's `bash -e`; "
            f"a failing {hook_id} line might no longer fail its step.")

    wanted = ["pre-commit", "run", hook_id, "--all-files"]
    hits = []
    for step in job.get("steps") or []:
        lines = _logical_shell_lines(str(step.get("run") or ""))
        if any(line.split() == wanted for line in lines):
            hits.append((step, lines))
    assert hits, (
        f"{CI_WORKFLOW.name}::{LINT_JOB} no longer runs `{' '.join(wanted)}` as "
        f"a bare, uncommented line. That line is the ONLY CI entry point "
        f"for {guards}; without it they only run where a contributor's local "
        f"hook happens to.")
    for step, lines in hits:
        label = step.get("name") or "<unnamed step>"
        for key in ("if", "continue-on-error", "shell"):
            assert key not in step, (
                f"step {label!r} running {hook_id} sets `{key}:` — it can "
                f"be skipped or its failure swallowed.")
        disarmed = [ln for ln in lines
                    if ln.split()[:1] == ["set"]
                    and any(t.startswith("+") and "e" in t
                            for t in ln.split()[1:])]
        assert not disarmed, (
            f"step {label!r} turns off errexit ({disarmed}); a failing "
            f"{hook_id} line would no longer fail the step.")
