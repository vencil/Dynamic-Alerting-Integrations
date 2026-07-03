#!/usr/bin/env python3
"""check_workflow_git_push_permissions.py — guard against a silently no-op `git push`.

Root cause chronicle
---------------------
`docs-ci.yaml`'s `check-coverage` job ran `git commit` + `git push || true` on
every push to `main` to auto-commit `docs/assets/doc-coverage-badge.json`.
The repo's "Workflow permissions" org/repo setting defaults `GITHUB_TOKEN` to
**read-only**, and the workflow declared no `contents: write` permission
anywhere — so every single push since the file was first committed
(v2.0.0-preview) failed with a permission error, and `|| true` swallowed it.
Nobody noticed because the badge itself was never consumed by anything
(README's coverage badge is a static hardcoded shields.io badge, unrelated).
Found during PR #983 review (2026-07-03); the dead steps were removed rather
than fixed forward since the feature had no consumer.

This hook makes that failure mode loud instead of silent: any GitHub Actions
step whose `run:` block invokes `git push` must resolve to an explicit
`contents: write` grant — job-level `permissions:` overrides workflow-level.
If NEITHER declares `contents: write` (including the common case of no
`permissions:` block at all), this fails. Relying on the implicit repo
default is exactly the mistake that caused the original bug — the YAML gives
no visibility into that setting, and it has already been `read` once.

What this does NOT check
-------------------------
Pushes performed by a dedicated action (e.g. `stefanzweifel/git-auto-commit
-action`, `peter-evans/create-pull-request`) rather than a literal `git push`
in a `run:` block — those take their own `token:`/`github-token:` input and
are a different risk shape, out of scope here.

Usage
-----
  python3 scripts/tools/lint/check_workflow_git_push_permissions.py [--ci]

Exit codes (per scripts/tools/_lib_exitcodes.py):
  0  every `git push` step resolves to an explicit contents: write grant
     (or no workflow performs a raw `git push` at all)
  1  at least one `git push` step lacks one (--ci)
  2  caller error (workflows dir missing, YAML parse failure)
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import List, Optional

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover - compat shim optional
    def try_utf8_stdout() -> None:  # type: ignore
        pass
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

GIT_PUSH_RE = re.compile(r"\bgit\s+push\b")


def _strip_shell_comment_lines(run: str) -> str:
    """Drop whole-line shell comments from a `run:` block before scanning.

    A `run: |` block scalar is one literal string to the YAML parser — a `#`
    explanatory comment inside it (like this hook's own pre-commit-config.yaml
    entry) is indistinguishable from code without this. Only handles
    whole-line comments (stripped line starts with `#`); a trailing `# ...`
    after real code on the same line is out of scope for this heuristic.
    """
    return "\n".join(
        line for line in run.splitlines() if not line.strip().startswith("#")
    )


def _repo_root() -> Path:
    p = Path(_THIS_DIR).resolve()
    for parent in [p, *p.parents]:
        if (parent / ".git").exists():
            return parent
    return p.parents[2]


def _contents_permission(perm) -> Optional[str]:
    """Extract the effective `contents` grant from a single `permissions:` value.

    Handles the mapping form (`{contents: write, ...}`) and the two string
    shorthands (`read-all` / `write-all`). A dict is authoritative for every
    scope: GitHub Actions does NOT merge it with any other `permissions:`
    block, so a scope the dict doesn't list is a definite `none` — NOT an
    unknown to fall back on. Only a totally absent `permissions:` key (perm
    is None) is genuinely unknown/implicit; callers must check for that
    case themselves rather than relying on this returning None for it.
    """
    if perm is None:
        return None
    if isinstance(perm, str):
        if perm == "write-all":
            return "write"
        if perm == "read-all":
            return "read"
        return "none"  # unrecognized shorthand — treat conservatively
    if isinstance(perm, dict):
        contents = perm.get("contents")
        return contents if isinstance(contents, str) else "none"
    return "none"


def scan_workflow(path: Path) -> List[str]:
    """Return violation strings for every ungranted `git push` step in *path*."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [f"{path.name}: YAML parse failure: {exc}"]
    if not isinstance(doc, dict):
        return []

    top_permissions_raw = doc.get("permissions")
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return []

    violations: List[str] = []
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        job_permissions_raw = job.get("permissions")
        # A job-level `permissions:` key, if present AT ALL, fully replaces
        # the workflow-level block (GH Actions does not merge per-scope) —
        # only fall back to top-level when the job declares no block at all.
        if job_permissions_raw is not None:
            effective = _contents_permission(job_permissions_raw)
        else:
            effective = _contents_permission(top_permissions_raw)

        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for idx, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                continue
            run = step.get("run")
            if not isinstance(run, str):
                continue
            if not GIT_PUSH_RE.search(_strip_shell_comment_lines(run)):
                continue
            if effective == "write":
                continue
            step_label = step.get("name", f"step #{idx}")
            granted = effective or "implicit (repo default — NOT guaranteed write)"
            violations.append(
                f"{path.name}: job '{job_name}' → '{step_label}' runs `git push` "
                f"but the effective `contents` permission is '{granted}'. Add "
                f"`permissions: {{contents: write}}` to the job (or workflow) — "
                f"do not rely on the repo's default Workflow permissions setting."
            )
    return violations


def scan_repo(workflows_dir: Path) -> List[str]:
    violations: List[str] = []
    for path in sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml")):
        violations.extend(scan_workflow(path))
    return violations


def main() -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Forbid a `git push` step in .github/workflows/ that isn't "
        "backed by an explicit contents: write permission grant."
    )
    parser.add_argument(
        "--workflows-dir",
        default=str(_repo_root() / ".github" / "workflows"),
        help="Directory of GitHub Actions workflow files to scan.",
    )
    parser.add_argument("--ci", action="store_true", help="Exit 1 on any violation.")
    args = parser.parse_args()

    workflows_dir = Path(args.workflows_dir)
    if not workflows_dir.is_dir():
        print(f"ERROR: workflows dir not found: {workflows_dir}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    violations = scan_repo(workflows_dir)

    if violations:
        print("❌ git push permission guard: found ungranted `git push` step(s):")
        for v in violations:
            print(f"  - {v}")
    else:
        print("✅ git push permission guard: no ungranted `git push` steps found.")

    if violations and args.ci:
        return EXIT_VIOLATION
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
