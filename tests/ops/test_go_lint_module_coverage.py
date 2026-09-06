"""Drift guards for what golangci-lint actually READS — three invariants.

The invariant behind all three (#1751): **every Go source file in the tree is
analysed by some linter.** It did not hold, and both gaps were silent — the
enrolled modules printed `0 issues`, which reads like whole-repo coverage:

  1. MODULE ENROLMENT — some `go.mod` trees had no lint step at all. Derived
     here from `git ls-files` on one side and from validate.yaml's `go-lint`
     steps on the other, and asserted EQUAL in both directions, so a new module
     without a step is red (and so is a step pointing at a module that no
     longer exists).
  2. GATE REACHABILITY — a lint step whose job the changed file cannot wake is
     a gate that never runs on the PR that breaks it (#1399's shape). Each
     module directory is replayed through the go-lint job's own `if:` against
     validate.yaml's real path filter.
  3. BUILD-TAG ENROLMENT — files behind a `//go:build` tag are outside the
     default build, so `golangci-lint run ./...` never loads them and still
     exits 0. `components/tenant-api/test/forgee2e/` sat there.

⛔ Invariant 3 is a PIN, not a classifier: it asserts this repo's build tags are
exactly the named set below, each with a disposition. That is the legitimate
use of enumeration — the objects are named and finite, and a new tag forces one
review instead of being absorbed. Do NOT convert it into "tags that look like
GOOS are fine": that predicate has no authority here and would silently absorb
the next `forge_e2e`.

⛔ Every assertion here except the controls is NEGATIVE ("nothing unenrolled"),
so the disarm surface is the two derivations. Both carry an anti-vacuity pin
(`_ANCHOR_MODULE`, `_ANCHOR_TAG`): an empty derivation makes every `all(...)`
below trivially true, which is the failure this module exists to prevent.

⛔ The matching and condition-evaluation helpers are IMPORTED from
`test_ci_path_filter_coverage`, never reimplemented — a second answer to "does
this path match that filter" is a second answer.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
VALIDATE = ROOT / ".github" / "workflows" / "validate.yaml"

# The job that runs golangci-lint. Named, because "the job whose steps mention
# golangci-lint" would make a renamed-away job look like zero missing steps.
LINT_JOB = "go-lint"

sys.path.insert(0, str(ROOT / "tests" / "ops"))
from test_ci_path_filter_coverage import (  # noqa: E402
    _condition_triggers,
    _load_workflow,
    _tracked_files,
    _workflow_filters,
)

# Anti-vacuity pins. Neither is a fact about linting — they exist so that a
# derivation returning the empty set fails LOUDLY instead of passing every
# assertion below. Changing either is a deliberate review.
_ANCHOR_MODULE = "components/tenant-api"
_ANCHOR_TAG = "forge_e2e"

# The only step shape modelled. Anything else that mentions golangci-lint
# raises rather than being skipped: a skipped step is an unenrolled module that
# invariant 1 would then report as correctly enrolled.
_LINT_RUN = re.compile(r"^golangci-lint\s+run\s+\./\.\.\.$")

# `//go:build <expr>` — the identifiers, whatever the operators. `!windows`
# yields `windows`, `linux && amd64` yields both: an unpinned name lands in the
# set and invariant 3 goes red, which is the loud direction.
_BUILD_LINE = re.compile(r"^//go:build\s+(.+?)\s*$", re.MULTILINE)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")

# Disposition for every build tag in the tree. "enrol" means the owning
# module's .golangci.yml must list it under `run.build-tags` (verified below,
# not taken on trust); "default" means the default linux/amd64 build already
# satisfies it, so golangci-lint loads those files with no configuration.
_TAG_DISPOSITION = {
    "forge_e2e": "enrol",
    "unix": "default",
}


def _go_modules() -> frozenset[str]:
    """Every Go module directory, from git rather than a walk.

    `_tracked_files` carries the truncation floor that makes an unsplit or
    short listing fail loudly — the reason for reusing it rather than globbing.
    """
    return frozenset(
        str(Path(p).parent).replace("\\", "/")
        for p in _tracked_files()
        if p.endswith("go.mod")
    )


def _lint_steps() -> dict[str, str]:
    """Step name -> the module directory it lints, off the real workflow."""
    job = _load_workflow(VALIDATE)["jobs"][LINT_JOB]
    steps: dict[str, str] = {}
    for step in job["steps"]:
        run = str(step.get("run", "")).strip()
        if "golangci-lint" not in run:
            continue
        if not _LINT_RUN.fullmatch(run):
            # The install step legitimately names the binary; everything else
            # is a shape this module cannot map back to a module directory.
            if "golangci-lint run" not in run:
                continue
            raise AssertionError(
                f"{VALIDATE.name}::{LINT_JOB} runs golangci-lint in a shape "
                f"this guard does not model: {run!r}. It cannot tell which "
                "module that step covers, so invariant 1 would count the "
                "module as unenrolled — or, worse, count a differently-scoped "
                "step as full coverage. Model the shape here first.")
        directory = step.get("working-directory")
        if not directory:
            raise AssertionError(
                f"{VALIDATE.name}::{LINT_JOB} step {step.get('name')!r} runs "
                "`golangci-lint run ./...` with no `working-directory:`, so it "
                "lints the repo root — where there is no go.mod and the run is "
                "a no-op that still exits 0.")
        steps[str(step.get("name"))] = str(directory).rstrip("/")
    return steps


def _build_tags() -> dict[str, list[str]]:
    """Build-tag identifier -> the tracked .go files carrying it."""
    found: dict[str, list[str]] = {}
    for rel in _tracked_files():
        if not rel.endswith(".go"):
            continue
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except OSError:
            continue
        for expression in _BUILD_LINE.findall(text):
            for ident in _IDENT.findall(expression):
                found.setdefault(ident, []).append(rel)
    return found


def _owning_module(rel: str, modules: frozenset[str]) -> str:
    """The nearest ancestor directory of `rel` that is a Go module."""
    candidates = [m for m in modules if rel.startswith(m + "/")]
    assert candidates, f"{rel} sits under no go.mod — nothing lints it at all."
    return max(candidates, key=len)


def _configured_build_tags(module: str) -> list[str]:
    config = ROOT / module / ".golangci.yml"
    assert config.is_file(), (
        f"{module} has no .golangci.yml, so `run.build-tags` cannot enrol "
        "anything and its tagged files are outside every linter.")
    parsed = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    return [str(t) for t in (parsed.get("run") or {}).get("build-tags") or []]


# ── invariant 1: module enrolment ──────────────────────────────────────────


def test_every_go_module_has_a_lint_step() -> None:
    modules = _go_modules()
    assert _ANCHOR_MODULE in modules, (
        f"the go.mod derivation lost {_ANCHOR_MODULE!r} — it returned "
        f"{sorted(modules)}. Every assertion in this module is negative, so an "
        "empty or truncated derivation passes them all. Fix the derivation; do "
        "not move the anchor to whatever the derivation happens to return.")
    linted = set(_lint_steps().values())
    assert _ANCHOR_MODULE in linted, (
        f"no lint step covers {_ANCHOR_MODULE!r}; the step derivation returned "
        f"{sorted(linted)} and cannot be trusted to say anything about the "
        "other modules either.")

    missing = sorted(modules - linted)
    assert not missing, (
        "Go module(s) with no lint step in "
        f"{VALIDATE.name}::{LINT_JOB}: {missing}.\n"
        "⛔ This is not cosmetic: the enrolled modules print `0 issues`, which "
        "reads like whole-repo coverage, so an unenrolled module is invisible "
        "rather than obviously missing — #1751 found real errcheck defects "
        "sitting in a module nobody had ever linted.\n"
        "Add a step with `working-directory: <module>` and "
        "`run: golangci-lint run ./...`, plus a `.golangci.yml` in the module.")

    stale = sorted(linted - modules)
    assert not stale, (
        f"{VALIDATE.name}::{LINT_JOB} lints director(ies) that hold no go.mod: "
        f"{stale}. golangci-lint exits 0 there, so the step looks like "
        "coverage and buys none. Point it at a module or delete it.")


# ── invariant 2: the gate can actually be woken ────────────────────────────


def test_editing_a_module_wakes_the_lint_job() -> None:
    """A lint step whose job the module cannot trigger is #1399's shape.

    Ordering matters and is the reason this test exists rather than a note:
    adding a step before the filter reaches the module installs a gate that the
    PR editing that module never runs — and it reports `0 issues`, which is
    indistinguishable from working.
    """
    filters = _workflow_filters(VALIDATE)
    condition = str(_load_workflow(VALIDATE)["jobs"][LINT_JOB]["if"])

    # Control: a path no `validate` entry covers must NOT wake the job.
    # Without it, a filter that matched everything (or an evaluator stuck on
    # True) would pass the real assertion below vacuously.
    assert not _condition_triggers(condition, "zzz-not-a-tree/probe.go",
                                   filters), (
        "the `validate` filter reports an unrelated top-level path as "
        "triggering, so this test cannot distinguish a covered module from an "
        "uncovered one. Check for a catch-all entry before reading the result "
        "below as coverage.")

    unreachable = sorted(
        directory for directory in set(_lint_steps().values())
        if not _condition_triggers(condition, f"{directory}/probe.go", filters)
    )
    assert not unreachable, (
        "these modules are linted by a step whose job their own edits cannot "
        f"trigger: {unreachable}.\n"
        f"⛔ A PR touching only such a module skips {LINT_JOB} — and a "
        "path-skipped required check reports `skipped`, which SATISFIES "
        "branch protection. The lint step is real and never runs.\n"
        f"Add a covering pattern to the `validate` filter in {VALIDATE.name} "
        "BEFORE relying on the step.")


# ── invariant 3: build tags ────────────────────────────────────────────────


def test_build_tags_are_pinned_and_enrolled() -> None:
    found = _build_tags()
    assert _ANCHOR_TAG in found, (
        f"the //go:build scan lost {_ANCHOR_TAG!r} — it found {sorted(found)}. "
        "An empty scan makes the enrolment assertion below trivially true.")

    unpinned = sorted(set(found) - set(_TAG_DISPOSITION))
    assert not unpinned, (
        f"build tag(s) with no disposition here: {unpinned} "
        f"(in {[f for t in unpinned for f in found[t]][:5]}).\n"
        "⛔ Files behind an unenrolled tag are outside the default build, so "
        "`golangci-lint run ./...` never loads them AND still exits 0 — the "
        "gap is silent by construction.\n"
        "Decide, then record it here: `enrol` (add the tag to the owning "
        "module's `run.build-tags`) or `default` (the default linux/amd64 "
        "build already satisfies it, so those files are already linted).")

    gone = sorted(set(_TAG_DISPOSITION) - set(found))
    assert not gone, (
        f"pinned build tag(s) no longer present in any .go file: {gone}. Drop "
        "the entry — a pin over an empty set asserts nothing but reads like "
        "coverage.")

    modules = _go_modules()
    for tag, disposition in _TAG_DISPOSITION.items():
        if disposition != "enrol":
            continue
        for module in sorted({_owning_module(f, modules) for f in found[tag]}):
            assert tag in _configured_build_tags(module), (
                f"{module}/.golangci.yml does not list {tag!r} under "
                f"`run.build-tags`, so its {len(found[tag])} tagged file(s) are "
                "not analysed — `golangci-lint run ./...` loads none of them "
                "and prints `0 issues`.")
