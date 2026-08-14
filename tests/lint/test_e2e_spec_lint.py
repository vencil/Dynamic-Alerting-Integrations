#!/usr/bin/env python3
"""Behavioural twin for ``scripts/tools/lint/e2e_spec_lint.sh`` (A-13, #1428).

WHY THIS FILE EXISTS
--------------------
#1428 collapsed three copies of the A-13 lint command into one script. That
removes the drift, and replaces it with a single point of failure — which was
measured, not theorised: a blind review replaced the script's final
``exec npm run --silent lint`` with ``exit 0`` and everything stayed green.
The hook passed, the whole test suite passed, and an injected ``test.fixme()``
sailed through. Server side, the only thing looking at the file was
``shellcheck``, which grades syntax and had no opinion.

⛔ So the rule this file exists to enforce is not "the script is correct" but
"the script cannot be disarmed silently", plus the thing that made the old
arrangement rot in the first place: nobody was checking that the callers still
call it. `make lint-e2e` had no caller at all for two releases and no test
noticed, because there was no test.

HOW THE BEHAVIOURAL CASES WORK
------------------------------
The script derives the repo root from its own location, so pointing it at a
controlled tree means copying it into a throwaway one (the pattern
``tests/lint/test_mkdocs_strict_check.py`` uses for the same reason). That also
keeps the real ``tests/e2e`` out of it: a test that depended on whether this
checkout happens to have run ``npm ci`` would pass or fail for reasons that have
nothing to do with the script.

The cases are deliberately PAIRED. Two of them require a non-zero exit, and the
third requires a ZERO exit from a tree that is set up correctly — without that
third one, ``exit 1`` as the script's entire body would satisfy this file.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Literal paths, not `/`-joined components: verify_diff.py builds its
# source→test map by scanning test files for literal path strings, so a split
# form would register this twin against nothing and editing the script would
# not select it.
_SCRIPT_REL = "scripts/tools/lint/e2e_spec_lint.sh"
_SCRIPT = _REPO_ROOT / "scripts/tools/lint/e2e_spec_lint.sh"
_PRECOMMIT_CONFIG = _REPO_ROOT / ".pre-commit-config.yaml"
_MAKEFILE = _REPO_ROOT / "Makefile"
_WORKFLOW = _REPO_ROOT / ".github/workflows/playwright.yml"

# ⛔ Resolve bash ONCE, to an absolute path, exactly as
# tests/lint/test_mkdocs_strict_check.py does. Passing the bare name "bash" to
# subprocess lets Windows resolve it through its own PATH, which on this host
# finds the WSL launcher at C:\Windows\System32\bash.exe: it identifies itself
# as /bin/bash, cannot see a `C:/…` path, and answers every invocation with
# exit 127 "No such file or directory". Measured while writing this file — and
# the reason the zero-exit case below is load-bearing: 127 silently satisfies
# every "must fail" assertion here, so both non-zero cases were green against a
# script that had never run.
_BASH = shutil.which("bash")


def _hook() -> dict:
    """The `playwright-lint` hook, read off the real config."""
    cfg = yaml.safe_load(_PRECOMMIT_CONFIG.read_text(encoding="utf-8"))
    hooks = [
        hook
        for repo in cfg.get("repos") or []
        for hook in repo.get("hooks") or []
        if hook.get("id") == "playwright-lint"
    ]
    assert len(hooks) == 1, (
        f"expected exactly one `playwright-lint` hook, found {len(hooks)}. "
        "This twin pins that hook's contract; with zero or two of them it is "
        "pinning nothing."
    )
    return hooks[0]


def _make_recipe(target: str) -> list[str]:
    """The recipe lines of a Makefile target, `@` and tabs stripped."""
    out: list[str] = []
    active = False
    for line in _MAKEFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{target}:"):
            active = True
            continue
        if not active:
            continue
        if line.startswith("\t"):
            out.append(line.lstrip("\t").lstrip("@"))
        elif line.strip():
            break
    return out


def test_all_three_callers_run_the_one_script() -> None:
    """⛔ The "single source of truth" claim must be checkable, not asserted in prose.

    The script's own header names three callers. Before #1428 the same claim
    was made in `frontend-quality-backlog.md` about a Makefile target that
    nothing invoked — the claim was simply wrong, and stayed wrong because no
    test read it. Each caller is read from its own file here, so a caller that
    quietly goes back to spelling the command out itself is a red, not a
    discovery two releases later.
    """
    assert _SCRIPT.is_file(), f"{_SCRIPT_REL} is missing"

    entry = str(_hook().get("entry", ""))
    assert _SCRIPT_REL in entry, (
        f"the `playwright-lint` hook runs {entry!r}, which is not "
        f"{_SCRIPT_REL}. ⛔ Inlining the command here is what this script "
        "exists to stop: the hook, `make lint-e2e` and the CI job then drift "
        "independently, which is exactly how the CI leg came to be missing."
    )

    recipe = _make_recipe("lint-e2e")
    assert any(_SCRIPT_REL in line for line in recipe), (
        f"`make lint-e2e` runs {recipe!r}, which does not invoke "
        f"{_SCRIPT_REL}."
    )

    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    runs = [
        str(step.get("run", ""))
        for job in workflow["jobs"].values()
        for step in job.get("steps") or []
    ]
    assert any(_SCRIPT_REL in run for run in runs), (
        f"no step in {_WORKFLOW.name} runs {_SCRIPT_REL}. That job is the only "
        "execution point A-13 has that does not depend on a developer's work "
        "tree being installed."
    )


def test_the_workflow_can_be_triggered_by_editing_the_script() -> None:
    """⛔ A path-filtered workflow does not run for files outside its filter.

    Measured: a pull request that only edits the runner did not start this
    workflow at all, so the single CI leg that executes the script skipped the
    one change most likely to break it — while the local hook happily ran the
    edited copy against itself.
    """
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    # `on` is the YAML 1.1 boolean `True` once safe_load is done with it.
    triggers = workflow.get("on") or workflow.get(True)
    for event in ("push", "pull_request"):
        paths = (triggers.get(event) or {}).get("paths") or []
        assert _SCRIPT_REL in paths, (
            f"`on.{event}.paths` does not list {_SCRIPT_REL}, so editing the "
            "runner does not run it."
        )


def test_hook_files_pattern_selects_specs_and_its_own_inputs() -> None:
    """Paired: the things that must be selected, and the things that must not.

    Only pinning the first half lets the pattern drift towards matching
    everything, which would make every commit in a fresh work tree demand an
    `npm ci`; only pinning the second half lets it drift to matching nothing.
    """
    pattern = re.compile(str(_hook()["files"]))

    for path in (
        "tests/e2e/portal-home.spec.ts",
        "tests/e2e/nested/thing.spec.js",
        "tests/e2e/eslint.config.mjs",
        "tests/e2e/package.json",
        "tests/e2e/package-lock.json",
        _SCRIPT_REL,
    ):
        assert pattern.search(path), f"{path!r} should be selected by the hook"

    for path in (
        ".pre-commit-config.yaml",
        "Makefile",
        "tests/e2e/playwright.config.ts",
        "tests/e2e/fixtures/axe-helper.ts",
        "docs/scenarios/gitops-ci-integration.md",
    ):
        assert not pattern.search(path), (
            f"{path!r} should NOT be selected — every extra match is a work "
            "tree that must run `npm ci` before it can commit an unrelated "
            "change."
        )


def _throwaway_tree(tmp_path: Path) -> Path:
    """A repo-shaped tree holding a copy of the script, and nothing else."""
    root = tmp_path / "repo"
    (root / "scripts" / "tools" / "lint").mkdir(parents=True)
    shutil.copy2(_SCRIPT, root / "scripts" / "tools" / "lint" / _SCRIPT.name)
    return root


def _run(root: Path, extra_path: Path | None = None):
    env = dict(os.environ)
    if extra_path is not None:
        env["PATH"] = f"{extra_path}{os.pathsep}{env['PATH']}"
    # ⛔ `as_posix()`, not `str()`. Git Bash on a Windows host reads a
    # backslashed argument as an escape sequence and silently hands bash
    # `C:UsersvencsAppData…`, which then exits 127 with "No such file or
    # directory". Measured while writing this file, and the reason the paired
    # zero-exit case below is not optional: BOTH non-zero cases went green on
    # that 127 — a broken harness satisfies every "must fail" assertion there
    # is, and only a "must succeed" case can tell you the script never ran.
    return subprocess.run(
        [_BASH, str(root / "scripts" / "tools" / "lint" / _SCRIPT.name)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )


@pytest.mark.skipif(_BASH is None, reason="bash is required to run the script")
def test_missing_e2e_directory_is_an_error_not_a_pass(tmp_path: Path) -> None:
    """A checkout with no tests/e2e must fail loudly rather than report success."""
    result = _run(_throwaway_tree(tmp_path))
    assert result.returncode != 0, (
        "the script exited 0 with no tests/e2e to lint — a green tick that "
        f"checked nothing.\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "tests/e2e" in result.stderr


@pytest.mark.skipif(_BASH is None, reason="bash is required to run the script")
def test_missing_eslint_fails_and_names_the_remedy(tmp_path: Path) -> None:
    """⛔ Fail-closed, and say what to run.

    Both halves matter. The exit code is the guard; the message is why the
    guard survives contact with a developer. What every fresh `git worktree
    add` produced before #1428 was a bare `eslint: not found`, which names no
    remedy and reads as a broken checkout — and a check that looks broken gets
    switched off rather than fixed.
    """
    root = _throwaway_tree(tmp_path)
    (root / "tests" / "e2e").mkdir(parents=True)

    result = _run(root)
    assert result.returncode != 0, (
        "the script exited 0 with no eslint installed — it reported success "
        "for specs nothing had looked at."
    )
    assert "npm ci" in result.stderr, (
        "the failure does not tell the reader what to run; that is the whole "
        f"difference #1428 made.\nstderr={result.stderr!r}"
    )
    # The message must not offer skipping as an option: an error that names its
    # own bypass teaches the bypass.
    assert not re.search(r"--no-verify|SKIP=", result.stderr), (
        "the failure message names a way to skip the check. Do not put the "
        "bypass in the error text; a reader who wants it will find it, and a "
        "reader who does not should never see it suggested."
    )


@pytest.mark.skipif(_BASH is None, reason="bash is required to run the script")
def test_a_correctly_installed_tree_exits_zero(tmp_path: Path) -> None:
    """⛔ The negative control. Without it, `exit 1` would satisfy this file.

    Stubs stand in for eslint and npm so the case measures the SCRIPT's
    decision — whether it reaches the lint and honours its exit code — rather
    than eslint's opinion of some fixture, which is covered where it belongs
    (`tests/e2e/eslint.config.mjs` plus the CI job that runs the real thing).
    """
    root = _throwaway_tree(tmp_path)
    e2e = root / "tests" / "e2e"
    (e2e / "node_modules" / ".bin").mkdir(parents=True)
    (e2e / "node_modules" / ".bin" / "eslint").write_text(
        "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8", newline="\n"
    )

    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    npm = bin_dir / "npm"
    npm.write_text(
        '#!/usr/bin/env bash\necho "stub npm $*"\nexit 0\n',
        encoding="utf-8",
        newline="\n",
    )
    npm.chmod(npm.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    result = _run(root, extra_path=bin_dir)
    assert result.returncode == 0, (
        "a correctly installed tree was rejected. This case exists so that a "
        "script which simply always fails cannot pass this file.\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


@pytest.mark.skipif(_BASH is None, reason="bash is required to run the script")
def test_a_failing_lint_is_propagated(tmp_path: Path) -> None:
    """The script must not swallow the linter's verdict.

    ``exec`` makes this true by construction today, which is precisely why it
    needs pinning: replacing that line with anything that returns its own
    status is a one-token edit, and a blind review has already shown that the
    rest of the suite does not notice.
    """
    root = _throwaway_tree(tmp_path)
    e2e = root / "tests" / "e2e"
    (e2e / "node_modules" / ".bin").mkdir(parents=True)
    (e2e / "node_modules" / ".bin" / "eslint").write_text(
        "#!/usr/bin/env bash\nexit 1\n", encoding="utf-8", newline="\n"
    )

    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    npm = bin_dir / "npm"
    npm.write_text(
        '#!/usr/bin/env bash\necho "stub npm rejects" >&2\nexit 1\n',
        encoding="utf-8",
        newline="\n",
    )
    npm.chmod(npm.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    result = _run(root, extra_path=bin_dir)
    assert result.returncode != 0, (
        "the linter rejected the specs and the script still exited 0 — the "
        "one failure mode that turns this whole arrangement into decoration."
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
