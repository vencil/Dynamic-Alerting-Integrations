"""Pin the CI Lint job lines for the hooks #2644 found with no CI entry point.

The Lint job in `.github/workflows/ci.yml` runs pre-commit hooks BY NAME; there
is no bare `pre-commit run --all-files` anywhere. #2644 inventoried every
auto-stage hook the job did not name and, for each, looked for another CI
entry point: a job running the same script, or a pytest that reds when a
violation the hook rejects is planted in the tree. The hooks below had
neither. Their own tests call the script's functions on fixtures, so they
stay green with the shipped tree in violation; the Lint line is the only CI
place the rule is applied to the repo.

⛔ The check itself is the shared `tests/_ci_lint_wiring.py` helper (YAML
parsed, never grepped — `ci.yml` comments name hook ids too). This file only
lists which hooks it applies to.

⚠️ What this does not pin: that pre-commit still selects each hook's files
(`tests/_precommit_selection.py` reports every `exclude:` as a possible
silencer, and two of these hooks carry a deliberate one), and that each
hook's `entry` still fails on a violation. Both were measured once, by hand,
when the lines were added; neither is held here.
"""
from __future__ import annotations

import pytest

from _ci_lint_wiring import assert_ci_lint_job_runs_hook

# hook id -> what it enforces (only used in the failure message).
HOOKS = {
    "file-hygiene": "NUL bytes and a missing EOF newline in md/yaml/py/jsx/json",
    "session-guard-liveness-check": "the session-guard launcher routing in .claude/settings.json (#824)",
    "env-bool-parser-guard": "hand-rolled env-to-bool parsers in Go (ADR-034)",
    "devrules-size-check": "the dev-rules.md line-count cap",
    "doc-k8s-refs": "k8s manifest paths and workload kinds named in docs",
    "commit-scope-doc-drift": "commit-convention.md scopes vs .commitlintrc.yaml",
    "aria-references-check": "dangling aria-*/htmlFor id references in portal JSX",
    "md-yaml-crd-check": "k8s objects embedded in docs vs their real CRD schema",
    "forbid-legacy-mariadb-threshold-name": "the retired bare mariadb threshold names (#731)",
    "forbid-legacy-mysql-cpu-key": "the retired mysql_cpu threshold key (#1231)",
    "forbid-legacy-mysql-cpu-selector": "the retired mysql/cpu user_threshold selector (#1231)",
    "skip-a11y-justification-check": "unjustified `skipA11y: true` in E2E specs",
}


@pytest.mark.parametrize("hook_id", sorted(HOOKS))
def test_ci_lint_job_runs_the_hook_over_the_whole_tree(hook_id: str) -> None:
    assert_ci_lint_job_runs_hook(hook_id, guards=f"{HOOKS[hook_id]} (#2644)")
