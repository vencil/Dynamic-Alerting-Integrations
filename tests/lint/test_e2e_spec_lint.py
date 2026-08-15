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

⛔ So the rule this file enforces is not "the script is correct" but "the
script cannot be disarmed silently", plus the thing that made the old
arrangement rot: nobody was checking that the callers still call it.
``make lint-e2e`` had no caller at all for two releases and no test noticed.

⛔ AND "still calls it" has to mean CALLS, not MENTIONS. The first version of
this file asserted `path in <string>`, and a blind review walked through it
six different ways, all green: ``run: echo bash …/e2e_spec_lint.sh``, a
Makefile recipe whose only reference was a comment, ``entry: bash -c ":" …``,
a hook with ``exclude: .*``, and — worst — ``if: false`` or
``continue-on-error: true`` on the job, which turns off the single server-side
execution point #1428 exists to add. Each assertion below therefore reads the
thing that decides whether the command RUNS, not whether the path appears.

HOW THE BEHAVIOURAL CASES WORK
------------------------------
The script derives the repo root from its own location, so pointing it at a
controlled tree means copying it into a throwaway one (the pattern
``tests/lint/test_mkdocs_strict_check.py`` uses for the same reason). That also
keeps the real ``tests/e2e`` out of it: a test that depended on whether this
checkout happens to have run ``npm ci`` would pass or fail for reasons that
have nothing to do with the script.

The cases are deliberately PAIRED. Two require a non-zero exit and one requires
a ZERO exit from a correctly set-up tree — without the last one, ``exit 1`` as
the script's entire body would satisfy this file, and a broken harness would
satisfy the other two (it did: see ``_BASH`` below).
"""
from __future__ import annotations

import os
import re
import shlex
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

# The shared GitHub-glob matcher. ⛔ Imported rather than reimplemented: a
# second copy of "does this path match that `paths:` entry" is a second answer,
# and the original already refuses to grade patterns outside the alphabet it
# models instead of guessing.
sys.path.insert(0, str(_REPO_ROOT / "tests" / "ops"))
from test_ci_path_filter_coverage import _match_segments  # noqa: E402


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


def _make_commands(makefile_text: str, target: str) -> list[str]:
    """The COMMAND lines of a Makefile target — comments excluded.

    ⛔ Comments excluded because `lint-e2e` is five comment lines and one
    command, so an assertion that scans the whole recipe is satisfied by a
    comment that merely names the script. Measured: replacing the command with
    ``cd tests/e2e && npm run --silent lint`` and leaving a comment saying
    "originally scripts/tools/lint/e2e_spec_lint.sh" kept this file at 7
    passed — the precise regression its docstring claims to catch.
    """
    out: list[str] = []
    active = False
    for line in makefile_text.splitlines():
        if line.startswith(f"{target}:"):
            active = True
            continue
        if not active:
            continue
        if line.startswith("\t"):
            body = line.lstrip("\t").lstrip("@").lstrip("-").lstrip("+")
            if not body.lstrip().startswith("#"):
                out.append(body)
        elif line.strip():
            break
    return out


def _invokes_script(command: str) -> bool:
    """True when this shell command actually RUNS the script.

    ⛔ Not `_SCRIPT_REL in command`. `echo bash …/e2e_spec_lint.sh` contains
    the path and runs nothing; so does a comment. The argv is what decides.
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if not argv or _SCRIPT_REL not in argv:
        return False
    head = argv[: argv.index(_SCRIPT_REL)]
    # Either the script is the command itself, or it is the argument of a
    # shell. Anything else in front of it (echo, :, true, a `-c` payload) means
    # the path is being printed or ignored rather than executed.
    return all(token in {"bash", "sh", "/bin/bash", "/usr/bin/env"} for token in head)


def _workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _script_step() -> tuple[str, dict, dict]:
    """``(job_id, job, step)`` for the workflow step that runs the script."""
    for job_id, job in _workflow()["jobs"].items():
        for step in job.get("steps") or []:
            if _invokes_script(str(step.get("run", ""))):
                return job_id, job, step
    raise AssertionError(
        f"no step in {_WORKFLOW.name} actually runs {_SCRIPT_REL} — a step that "
        "only mentions the path (echo, a comment, a disabled shell) is not an "
        "execution point, and this job is the only one A-13 has that does not "
        "depend on a developer's work tree being installed."
    )


# --------------------------------------------------------------------------
# Negative controls for the readers above. The real files are all in the state
# these tests want, so asserting only against them cannot tell a working reader
# from a constant — a blind review replaced `_hook` and `_make_recipe` with
# hard-coded returns and this file stayed at 7 passed.
# --------------------------------------------------------------------------

_FAKE_MAKEFILE = (
    "other-target: ## something else\n"
    "\t@echo other\n"
    "\n"
    "lint-e2e: ## A-13\n"
    "\t@# 說明：本來是 scripts/tools/lint/e2e_spec_lint.sh\n"
    "\t@cd tests/e2e && npm run --silent lint\n"
    "\n"
    "next-target:\n"
    "\t@bash scripts/tools/lint/e2e_spec_lint.sh\n"
)


def test_make_commands_ignores_comments_and_stops_at_the_next_target() -> None:
    """⛔ Both halves were measured to matter.

    Keeping comment lines let a comment naming the script satisfy the caller
    assertion. Not stopping at the next target let ANY target in the Makefile
    satisfy it — including, in the fixture below, one that really does invoke
    the script while `lint-e2e` itself no longer does.
    """
    commands = _make_commands(_FAKE_MAKEFILE, "lint-e2e")
    assert commands == ["cd tests/e2e && npm run --silent lint"], commands
    assert not any(_invokes_script(c) for c in commands), (
        "a comment mentioning the script was read as a command"
    )
    assert _make_commands(_FAKE_MAKEFILE, "next-target") == [
        "bash scripts/tools/lint/e2e_spec_lint.sh"
    ]


def test_invokes_script_separates_running_from_mentioning() -> None:
    """The six disarms a blind review walked through, as a fixture."""
    assert _invokes_script(f"bash {_SCRIPT_REL}")
    assert _invokes_script(f"sh {_SCRIPT_REL}")
    assert _invokes_script(_SCRIPT_REL)

    for disarmed in (
        f"echo bash {_SCRIPT_REL}",
        f"echo {_SCRIPT_REL}",
        f'bash -c ":" {_SCRIPT_REL}',
        f"true {_SCRIPT_REL}",
        f"# {_SCRIPT_REL}",
        "cd tests/e2e && npm run --silent lint",
    ):
        assert not _invokes_script(disarmed), (
            f"{disarmed!r} was read as running the script. Every one of these "
            "keeps the path visible while executing nothing, which is exactly "
            "how a caller assertion based on substring containment was walked "
            "through six ways."
        )


# --------------------------------------------------------------------------
# The contract.
# --------------------------------------------------------------------------


def test_all_three_entry_points_run_the_one_script() -> None:
    """⛔ The "single source of truth" claim must be checkable, not prose.

    The script's header names three entry points. Before #1428 the same claim
    was made in `frontend-quality-backlog.md` about a Makefile target that
    nothing invoked — wrong, and it stayed wrong because no test read it.
    """
    assert _SCRIPT.is_file(), f"{_SCRIPT_REL} is missing"

    hook = _hook()
    entry = str(hook.get("entry", ""))
    assert _invokes_script(entry), (
        f"the `playwright-lint` hook's entry is {entry!r}, which does not run "
        f"{_SCRIPT_REL}. ⛔ Inlining the command here is what the script exists "
        "to stop: the hook, `make lint-e2e` and the CI job then drift "
        "independently, which is how the CI leg came to be missing."
    )
    assert "stages" not in hook, (
        "the hook grew a `stages:` key. Any value but the inherited "
        "`pre-commit` takes it off the commit path — `stages: [manual]` means "
        "it runs only when somebody remembers to ask for it."
    )
    assert "exclude" not in hook, (
        "the hook grew an `exclude:` pattern, which can empty its file set "
        "without touching `files:`. If some path genuinely must be exempt, say "
        "so in `files:` where the whole selection is readable at once."
    )

    commands = _make_commands(_MAKEFILE.read_text(encoding="utf-8"), "lint-e2e")
    assert any(_invokes_script(c) for c in commands), (
        f"`make lint-e2e` runs {commands!r}, none of which invokes "
        f"{_SCRIPT_REL}."
    )

    _script_step()  # raises with its own message when no step runs the script


def test_the_ci_leg_cannot_be_switched_off_without_this_test_noticing() -> None:
    """⛔ #1428's whole subject is a gate that exists and does not execute.

    Measured on this very workflow: `if: false` on the job and
    `continue-on-error: true` on the step each left 47 tests passing, with the
    only server-side execution point A-13 has turned off and nothing in the
    repo red. `tests/ops/test_ci_path_filter_coverage.py` does not cover this
    workflow — its scan is scoped to the ones using `dorny/paths-filter`.
    """
    job_id, job, step = _script_step()

    assert "if" not in job, (
        f"job {job_id!r} grew an `if:` condition ({job.get('if')!r}). This job "
        "is the only place A-13 runs that does not depend on a developer's "
        "work tree; a condition on it is indistinguishable from deleting it, "
        "and the check reports nothing rather than reporting red."
    )
    assert "if" not in step, (
        f"the step running {_SCRIPT_REL} grew an `if:` ({step.get('if')!r}). "
        "A skipped step is a green job."
    )
    for where, node in (("job", job), ("step", step)):
        assert node.get("continue-on-error") in (None, False), (
            f"{where} sets `continue-on-error: {node.get('continue-on-error')!r}`. "
            "⛔ This is the cheapest way to make a red A-13 go away, and the "
            "workflow's own comments describe the leg as advisory — which is "
            "about branch protection, not about swallowing the result. An "
            "advisory check that cannot report red is decoration."
        )
    assert not job.get("needs"), (
        f"job {job_id!r} grew `needs: {job.get('needs')!r}`. GitHub skips a job "
        "whose dependency was skipped or failed, so a `needs:` here hands the "
        "off-switch to another job."
    )


def test_editing_a_spec_or_the_runner_triggers_the_workflow() -> None:
    """⛔ A path-filtered workflow does not run for files outside its filter.

    Two measured holes, opposite ends of the same rule: dropping
    `tests/e2e/**` from `paths:` (the trigger for the change class A-13 exists
    to catch) left this file at 7 passed, and before #1428 the runner itself
    was outside the filter, so a pull request that only edited it never started
    the one CI leg that executes it.

    Evaluated, not string-matched: `tests/e2e/**` could legitimately be spelled
    another way, and an assertion on the literal would then demand a rewrite
    for a change that broke nothing.
    """
    specs = sorted((_REPO_ROOT / "tests" / "e2e").glob("*.spec.ts"))
    assert specs, "no E2E specs found — the sample below would prove nothing"
    sample = specs[0].relative_to(_REPO_ROOT).as_posix()

    triggers = _workflow().get("on") or _workflow().get(True)
    for event in ("push", "pull_request"):
        patterns = (triggers.get(event) or {}).get("paths") or []
        assert patterns, f"`on.{event}` has no `paths:` at all"
        for target, why in (
            (sample, "editing an E2E spec is the change class A-13 guards"),
            (_SCRIPT_REL, "editing the runner is the change most likely to break it"),
        ):
            assert any(
                _match_segments(pattern.split("/"), target.split("/"))
                for pattern in patterns
            ), (
                f"`on.{event}.paths` does not match {target!r} — {why}, and a "
                "pull request doing exactly that would not start this workflow."
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


def test_the_script_probes_for_presence_not_for_an_executable_bit() -> None:
    """⛔ A static pin, because this one cannot be discriminated behaviourally.

    `-e` and `-x` are not distinguishable on the platform this repo is
    developed on: measured, `node_modules/.bin/eslint` reports as executable
    under Git Bash, so swapping the probe to `-x` leaves every behavioural case
    green. The distinction still matters — the executable bit carries no
    reliable meaning across the filesystems this runs on, and a work tree that
    is in fact ready must not be reported as missing — so it is pinned by
    reading the script instead. Same shape as the repo's other cross-platform
    rules: where behaviour cannot separate the two, pin the source.
    """
    text = _SCRIPT.read_text(encoding="utf-8")
    probes = re.findall(r"\[\s*!\s*(-[a-z])\s+node_modules/\.bin/eslint\s*\]", text)
    assert probes == ["-e"], (
        f"the eslint probe uses {probes!r}. It must be `-e`: `-x` asks about a "
        "permission bit that Windows filesystems do not carry meaningfully, "
        "and answering 'missing' for a tree that is installed sends the reader "
        "to run `npm ci` again, which will not help."
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
    return subprocess.run(
        [_BASH, str(root / "scripts" / "tools" / "lint" / _SCRIPT.name)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )


def test_bash_is_available_wherever_this_suite_is_meant_to_run() -> None:
    """⛔ Without this, `_BASH = None` turns four cases into a silent skip.

    Measured: setting it to None left 3 passed / 4 skipped and rc=0 — the
    skip-as-green shape this repo has a documented history with. The skips are
    legitimate on a developer box without bash; they are not legitimate on the
    runners, and every job in this repo's workflows is `ubuntu-latest`.
    """
    if not os.environ.get("CI"):
        pytest.skip("only meaningful where the suite is required to be complete")
    assert _BASH is not None, (
        "bash is not on PATH in CI, so the behavioural half of this file "
        "skipped rather than ran. A skipped guard reports the same green as a "
        "passing one."
    )


@pytest.mark.skipif(_BASH is None, reason="bash is required to run the script")
def test_missing_e2e_directory_is_an_error_not_a_pass(tmp_path: Path) -> None:
    """A checkout with no tests/e2e must fail loudly rather than report success.

    ⛔ The assertion names the script's own prefix, not the string "tests/e2e".
    Measured: deleting the guard entirely still satisfied a `"tests/e2e" in
    stderr` check, because `set -e` then makes bash print
    `cd: …/repo/tests/e2e: No such file or directory` — the path contains the
    needle. The message being the script's own is the whole point of it.
    """
    result = _run(_throwaway_tree(tmp_path))
    assert result.returncode != 0, (
        "the script exited 0 with no tests/e2e to lint — a green tick that "
        f"checked nothing.\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "[A-13]" in result.stderr and "refusing" in result.stderr, (
        "the failure came from somewhere other than the script's own guard; a "
        "reader gets a bare `cd:` error and no idea what was supposed to run."
        f"\nstderr={result.stderr!r}"
    )


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
    assert "[A-13]" in result.stderr and "npm ci" in result.stderr, (
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


def _install_stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(body, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


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
    _install_stub(bin_dir, "npm", '#!/usr/bin/env bash\necho "stub npm $*"\nexit 0\n')

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
    _install_stub(
        bin_dir, "npm", '#!/usr/bin/env bash\necho "stub npm rejects" >&2\nexit 1\n'
    )

    result = _run(root, extra_path=bin_dir)
    assert result.returncode != 0, (
        "the linter rejected the specs and the script still exited 0 — the "
        "one failure mode that turns this whole arrangement into decoration."
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
