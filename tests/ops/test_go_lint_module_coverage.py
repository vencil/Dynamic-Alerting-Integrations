"""Drift guards for what golangci-lint actually READS.

The goal (#1751): **every tracked Go source file is analysed by some linter.**
It did not hold, and every gap was silent — the enrolled modules printed
`0 issues`, which reads like whole-repo coverage.

⛔ That goal is NOT fully asserted here, and the difference is the point of
this paragraph. `_EXEMPT_GO_FILES` names the files still outside, each with a
reason. Read it as the list of holes, not as a formality.

  1a. MODULE ENROLMENT — a `go.mod` tree with no lint step. Derived from
      `git ls-files` on one side and validate.yaml's `go-lint` steps on the
      other, asserted EQUAL both ways: a new module without a step is red, and
      so is a step pointing at no module.
  1b. FILE COVERAGE — the same question with the quantifier that matches the
      goal. 1a is vacuous for a `.go` written BESIDE a module rather than
      inside it: there is no module to be missing. That shape is why
      `scripts/tools/ops/bench_filter.go` went years unlinted while an earlier
      revision of this docstring asserted the opposite.
  2.  GATE REACHABILITY — a lint step whose job the changed file cannot wake is
      a gate that never runs on the PR that breaks it (#1399's shape). Each
      module directory is replayed through the go-lint job's own `if:` against
      validate.yaml's real path filter.
  3.  BUILD-TAG ENROLMENT — files behind a `//go:build` tag are outside the
      default build, so `golangci-lint run ./...` never loads them and still
      exits 0. `components/tenant-api/test/forgee2e/` sat there.
  4.  TOOLCHAIN PARITY — the configs tell contributors a local run equals the
      gate; that holds only while the gate's pinned linter and the dev
      container's are the same version.

⛔ Invariant 3 is a PIN, not a classifier — it asserts this repo's build tags
are exactly the named set below, each with a hand-written disposition. Only the
`enrol` disposition is machine-verified; `default-build` and `unlinted` are
claims a human made. That is the legitimate use of enumeration (named, finite
objects; a new tag forces one review), but do NOT convert it into "tags that
look like GOOS are fine": that predicate has no authority here and would
silently absorb the next `forge_e2e`.

⛔ Every assertion here except the controls is NEGATIVE ("nothing unenrolled"),
so the disarm surface is the derivations plus what they do not read. Each
carries an anti-vacuity pin (`_ANCHOR_MODULE`, `_ANCHOR_TAG`,
`_ANCHOR_GO_FILE`): an empty derivation makes every `all(...)` below trivially
true, which is the failure this module exists to prevent.

⚠️ Known blind spots, so they are not mistaken for coverage: a `.golangci.yml`
whose CONTENT disables everything (`default: none`, a catch-all exclusion) is
enrolment as far as 1a is concerned; a module with a step but no config at all
lints with upstream defaults; and neither `strategy.matrix` nor a
`${{ }}`-valued `working-directory` is modelled — 1a reports those as a
missing/stale pair rather than naming them.

⛔ The matching and condition-evaluation helpers are IMPORTED from
`test_ci_path_filter_coverage`, never reimplemented — a second answer to "does
this path match that filter" is a second answer. Step-level `if:` and
`continue-on-error:` are deliberately NOT re-checked here: that module already
raises on both for this exact job (`GATED_LEGS` contains
`("validate.yaml", "go-lint")`).
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

# Anti-vacuity pins. None is a fact about linting — they exist so that a
# derivation returning the empty set fails LOUDLY instead of passing every
# assertion below. Changing any of them is a deliberate review.
_ANCHOR_MODULE = "components/tenant-api"
_ANCHOR_TAG = "forge_e2e"
_ANCHOR_GO_FILE = "components/tenant-api/cmd/server/main.go"

# The only step shape modelled. Anything else that mentions golangci-lint
# raises rather than being skipped: a skipped step is an unenrolled module that
# invariant 1 would then report as correctly enrolled.
_LINT_RUN = re.compile(r"^golangci-lint\s+run\s+\./\.\.\.$")

# `//go:build <expr>` — the identifiers, whatever the operators. `!windows`
# yields `windows`, `linux && amd64` yields both: an unpinned name lands in the
# set and invariant 3 goes red, which is the loud direction.
_BUILD_LINE = re.compile(r"^//go:build\s+(.+?)\s*$", re.MULTILINE)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")

# ⛔ Tracked .go files that sit under NO go.mod, and are therefore read by no
# linter at all. This is a DISCLOSED HOLE, not coverage: golangci-lint needs a
# module, so a file here cannot be linted without giving it one. Each entry
# must say why it is still outside.
#
# ⚠️ Adding an entry is the cheapest way to silence invariant 1b, and it buys
# nothing — it only records that nobody is looking. Prefer moving the file into
# a module.
_EXEMPT_GO_FILES = {
    "scripts/tools/ops/bench_filter.go":
        "Run as `go run <file>` by scripts/tools/ops/bench_wrapper.sh and copied "
        "standalone into the release bench harness by bench-gate-release.yaml, so "
        "it has never had a go.mod. Giving it one changes the module context those "
        "two callers run it in — tracked as its own change, not smuggled into a "
        "lint-coverage PR. Until then nothing lints or gofmts it (#1751).",
}

# Disposition for every build tag in the tree.
#
#   enrol         — the owning module's .golangci.yml must list it under
#                   `run.build-tags`. VERIFIED below, not taken on trust.
#   default-build — the tag is satisfied by the linux/amd64 build CI runs, so
#                   golangci-lint already loads those files. ⛔ This guard
#                   CANNOT verify that; it is a claim you are making.
#   unlinted      — outside the CI build and deliberately left there. A
#                   disclosed hole, like _EXEMPT_GO_FILES above.
#
# ⛔ `default-build` is only for tags the linux/amd64 build genuinely satisfies
# (GOOS/GOARCH names for that platform, `unix`, `cgo`, `go1.x`). A tag naming
# another platform — `windows`, `darwin` — is NOT one of them: writing
# `default-build` there records a false claim that nothing will catch. Use
# `enrol`, or `unlinted` if the gap is accepted.
_TAG_DISPOSITION = {
    "forge_e2e": "enrol",
    "unix": "default-build",
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


def _tracked_go_files() -> tuple[str, ...]:
    return tuple(p for p in _tracked_files() if p.endswith(".go"))


def _owning_module(rel: str, modules: frozenset[str]) -> str | None:
    """The nearest ancestor directory of `rel` that is a Go module, or None.

    None is a real answer, not an error: golangci-lint needs a module, so a
    tracked .go outside every go.mod is linted by nothing. Invariant 1b is
    where that is judged — returning None here keeps the judgement in one
    place instead of raising from whichever caller happens to reach it first.
    """
    candidates = [m for m in modules if rel.startswith(m + "/")]
    return max(candidates, key=len) if candidates else None


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


def test_every_go_file_is_under_a_linted_module() -> None:
    """The quantifier that matches the claim: FILES, not modules.

    ⛔ The test above quantifies over `go.mod` directories, and a `.go` written
    beside a module rather than inside it satisfies it vacuously — there is no
    module to be missing. That is not hypothetical: it is how
    `scripts/tools/ops/bench_filter.go` stayed unlinted (and un-gofmt'd) while
    an earlier revision of this module asserted the opposite in its docstring.
    """
    go_files = _tracked_go_files()
    assert _ANCHOR_GO_FILE in go_files, (
        f"the tracked .go derivation lost {_ANCHOR_GO_FILE!r} — it returned "
        f"{len(go_files)} path(s). An empty or truncated listing passes the "
        "assertions below trivially.")

    modules = _go_modules()
    linted = set(_lint_steps().values())
    orphans = sorted(
        rel for rel in go_files
        if _owning_module(rel, modules) not in linted
    )

    undisclosed = [rel for rel in orphans if rel not in _EXEMPT_GO_FILES]
    assert not undisclosed, (
        f"tracked .go file(s) that no lint step reaches: {undisclosed}.\n"
        "⛔ golangci-lint operates on modules, so a file outside every go.mod "
        "is read by no linter and by no gofmt — silently, because every "
        "enrolled module still prints `0 issues`.\n"
        "Move it under a module (preferred), or add it to _EXEMPT_GO_FILES "
        "with the reason it cannot be — that entry records a hole, it does not "
        "close one.")

    stale = sorted(set(_EXEMPT_GO_FILES) - set(orphans))
    assert not stale, (
        f"_EXEMPT_GO_FILES still excuses {stale}, which a lint step now "
        "reaches (or which no longer exists). Drop the entry: an exemption "
        "over a file nobody needs excused reads like a known hole and is not "
        "one.")


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
        "⇒ The answer is `enrol`: add the tag to the owning module's "
        "`run.build-tags`, then re-run that module's lint and fix what "
        "appears.\n"
        "⚠️ The other two values do NOT lint anything. `default-build` asserts "
        "the linux/amd64 build already covers the files — this guard cannot "
        "check that, so it is only honest for a tag that platform genuinely "
        "satisfies. `unlinted` records that the gap is accepted. Reaching for "
        "either because it is a one-line edit is how a false claim gets "
        "written into a pin nobody re-reads.")

    bad_value = sorted(t for t, d in _TAG_DISPOSITION.items()
                       if d not in {"enrol", "default-build", "unlinted"})
    assert not bad_value, (
        f"unknown disposition for build tag(s) {bad_value}. A value this "
        "module does not recognise is silently skipped by the enrolment loop "
        "below, so a typo would read as coverage.")

    gone = sorted(set(_TAG_DISPOSITION) - set(found))
    assert not gone, (
        f"pinned build tag(s) no longer present in any .go file: {gone}. Drop "
        "the entry — a pin over an empty set asserts nothing but reads like "
        "coverage.")

    modules = _go_modules()
    for tag, disposition in _TAG_DISPOSITION.items():
        if disposition != "enrol":
            continue
        # A None owner is a file under no module; that is invariant 1b's
        # judgement, not this one's, and enrolling a tag would not help it.
        owners = {_owning_module(f, modules) for f in found[tag]} - {None}
        for module in sorted(owners):
            assert tag in _configured_build_tags(module), (
                f"{module}/.golangci.yml does not list {tag!r} under "
                f"`run.build-tags`, so its {len(found[tag])} tagged file(s) are "
                "not analysed — `golangci-lint run ./...` loads none of them "
                "and prints `0 issues`.")


# ── invariant 4: the gate's linter and the one contributors run ────────────


def test_golangci_version_matches_the_dev_container() -> None:
    """`.golangci.yml` tells contributors a local run equals the gate.

    ⛔ That only holds if both sides run the same linter. The workflow pins a
    version; the dev container's `go` feature installs golangci-lint too, and
    with no `golangciLintVersion` it takes `latest` — so the two agreed by
    coincidence of when the image was last built, and the next rebuild could
    diverge with nothing saying so. Two independent sources, compared.
    """
    workflow = VALIDATE.read_text(encoding="utf-8")
    pinned = re.findall(r"^\s*GOLANGCI_VERSION=v?([0-9]+\.[0-9]+\.[0-9]+)\s*$",
                        workflow, re.MULTILINE)
    assert len(pinned) == 1, (
        f"expected exactly one GOLANGCI_VERSION pin in {VALIDATE.name}, found "
        f"{pinned}. With none, this test compares nothing; with several, it "
        "cannot say which one the job installs.")

    devcontainer = (ROOT / ".devcontainer" / "devcontainer.json").read_text(
        encoding="utf-8")
    declared = re.findall(
        r'"golangciLintVersion"\s*:\s*"v?([0-9]+\.[0-9]+\.[0-9]+)"', devcontainer)
    assert len(declared) == 1, (
        "`.devcontainer/devcontainer.json` does not pin `golangciLintVersion` "
        "exactly once (found "
        f"{declared}). Without a pin the `go` feature installs `latest`, so "
        "the dev container's linter is whatever existed when the image was "
        "built — and every `.golangci.yml` in this repo tells contributors "
        "their local run is equivalent to the gate.")

    assert declared[0] == pinned[0], (
        f"{VALIDATE.name} installs golangci-lint {pinned[0]} but the dev "
        f"container declares {declared[0]}. A local `golangci-lint run ./...` "
        "is then NOT the gate, which is exactly what the per-module configs "
        "promise it is. Bump both together.")
