"""Drift guards for what golangci-lint actually READS (#1751).

Goal: every tracked Go source file is analysed by some linter. It did not hold,
and every gap was silent — enrolled modules print `0 issues`, which reads like
whole-repo coverage.

⛔ The goal is NOT fully asserted. `_EXEMPT_GO_FILES` names what is still
outside. Read it as the list of holes.

⛔ Nothing here runs golangci-lint; the executable control is the `Go Lint` job.
A probe inside pytest was measured and rejected: `ci.yml::python-tests-run` has
no `setup-go`, so it would skip in CI, and a vacuous control is worse than a
declared gap.

⛔ Matching and condition evaluation are IMPORTED from
`test_ci_path_filter_coverage`, never reimplemented. Step-level `if:` and
`continue-on-error:` are deliberately not re-checked here — that module already
raises on both for this job (`GATED_LEGS` contains `("validate.yaml",
"go-lint")`).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
VALIDATE = ROOT / ".github" / "workflows" / "validate.yaml"
DEVCONTAINER = ROOT / ".devcontainer" / "devcontainer.json"

# Named, because "the job whose steps mention golangci-lint" would make a
# renamed-away job look like zero missing steps.
LINT_JOB = "go-lint"

sys.path.insert(0, str(ROOT / "tests" / "ops"))
from test_ci_path_filter_coverage import (  # noqa: E402
    _condition_triggers,
    _load_workflow,
    _tracked_files,
    _workflow_filters,
)

# Anti-vacuity pins: an empty derivation would pass every negative assertion
# below. ⛔ When one of these fires, fix the derivation — do not move the anchor
# to whatever the derivation happened to return.
_ANCHOR_MODULE = "components/tenant-api"
_ANCHOR_GO_FILE = "components/tenant-api/cmd/server/main.go"
_ANCHOR_TAG = "forge_e2e"

# The only step shape modelled. Anything else naming `golangci-lint run` raises
# rather than being skipped: a skipped step is an unenrolled module that would
# then read as enrolled.
_LINT_RUN = re.compile(r"^golangci-lint\s+run\s+\./\.\.\.$")

_BUILD_LINE = re.compile(r"^//go:build\s+(.+?)\s*$", re.MULTILINE)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")

# ⛔ Tracked .go outside every go.mod. golangci-lint needs a module, so these
# are read by no linter and no gofmt. This records holes; it closes none.
_EXEMPT_GO_FILES = {
    "scripts/tools/ops/bench_filter.go":
        "`go run <file>` from bench_wrapper.sh, and copied standalone into the "
        "release bench harness — giving it a go.mod changes the module context "
        "both callers run it in, so that is its own change (#1751).",
}

# Build-tag disposition. Only `enrol` is machine-verified; the other two assert
# that a gap is acceptable and are claims a human made.
#   enrol         → must appear in the owning module's `run.build-tags`
#   default-build → the linux/amd64 build CI runs already loads those files
#   unlinted      → outside the CI build, deliberately
_TAG_DISPOSITION = {
    "forge_e2e": "enrol",
    "unix": "default-build",
}
_DISPOSITIONS = frozenset({"enrol", "default-build", "unlinted"})

# The linter floor this repo's own configs and workflow comments promise.
# Asserted rather than described — prose nobody checks is how the promise and
# the tree drift apart.
_REQUIRED_LINTERS = frozenset({"godoclint"})

# `run:` keys this repo has looked at. ⛔ This is a CHANGE DETECTOR, not a
# safety proof — the honest claim is "someone reviewed this key once", and even
# that is conditional: `build-tags` is here because tenant-api needs it, yet
# enrolling a tag that some file NEGATES (`//go:build !x`) drops that file from
# the corpus, which is the very shape this module exists to catch. No file in
# the tree carries a negated constraint today.
# An accept-set rather than a denylist because the harmful set is open; kept
# wide enough that the ordinary knobs stay usable, because a guard that reds on
# `timeout:` is a guard that gets deleted.
_VETTED_RUN_KEYS = frozenset({
    "build-tags", "timeout", "concurrency", "modules-download-mode",
    "allow-parallel-runners", "allow-serial-runners",
})


def _go_modules() -> frozenset[str]:
    """Module directories, from git (which carries the truncation floor)."""
    return frozenset(
        str(Path(p).parent).replace("\\", "/")
        for p in _tracked_files() if p.endswith("go.mod"))


def _lint_steps() -> dict[str, str]:
    """Step name -> the module directory it lints, off the real workflow."""
    steps: dict[str, str] = {}
    for step in _load_workflow(VALIDATE)["jobs"][LINT_JOB]["steps"]:
        run = str(step.get("run", "")).strip()
        if "golangci-lint run" not in run:
            continue  # the install step names the binary, not a run
        if not _LINT_RUN.fullmatch(run):
            raise AssertionError(
                f"{VALIDATE.name}::{LINT_JOB} runs {run!r}, a shape this guard "
                "cannot map to a module directory. Model it here first.")
        directory = step.get("working-directory")
        if not directory:
            raise AssertionError(
                f"{VALIDATE.name}::{LINT_JOB} step {step.get('name')!r} has no "
                "`working-directory:`, so it lints the repo root — no go.mod "
                "there, a no-op that still exits 0.")
        steps[str(step.get("name"))] = str(directory).rstrip("/")
    return steps


def _owning_module(rel: str, modules: frozenset[str]) -> str | None:
    """Nearest ancestor go.mod directory, or None — None is a real answer."""
    candidates = [m for m in modules if rel.startswith(m + "/")]
    return max(candidates, key=len) if candidates else None


def _module_config(module: str) -> dict:
    config = ROOT / module / ".golangci.yml"
    assert config.is_file(), (
        f"{module} has no .golangci.yml — golangci-lint then runs its own "
        "defaults and the step still prints `0 issues`.")
    return yaml.safe_load(config.read_text(encoding="utf-8")) or {}


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


# ── coverage ───────────────────────────────────────────────────────────────


def test_every_go_file_is_under_a_linted_module() -> None:
    """FILES, not modules — the quantifier that matches the goal.

    ⛔ Quantifying over `go.mod` directories is vacuous for a `.go` written
    BESIDE a module rather than inside it: there is no module to be missing.
    That is how `scripts/tools/ops/bench_filter.go` stayed unlinted while an
    earlier revision of this file asserted the opposite.
    """
    go_files = [p for p in _tracked_files() if p.endswith(".go")]
    assert _ANCHOR_GO_FILE in go_files, (
        f"the tracked .go derivation lost {_ANCHOR_GO_FILE!r} "
        f"({len(go_files)} path(s) returned); every assertion below would pass "
        "trivially.")

    modules = _go_modules()
    linted = set(_lint_steps().values())
    orphans = [r for r in go_files if _owning_module(r, modules) not in linted]

    undisclosed = sorted(set(orphans) - set(_EXEMPT_GO_FILES))
    assert not undisclosed, (
        f"tracked .go read by no lint step: {undisclosed}. Move it under a "
        "linted module. Adding it to _EXEMPT_GO_FILES silences this and lints "
        "nothing — that is a last resort, not the fix.")

    stale = sorted(set(_EXEMPT_GO_FILES) - set(orphans))
    assert not stale, (
        f"_EXEMPT_GO_FILES still excuses {stale}, which is now linted (or gone). "
        "Drop the entry.")

    unbacked = sorted(set(linted) - modules)
    assert not unbacked, (
        f"{VALIDATE.name}::{LINT_JOB} lints director(ies) holding no go.mod: "
        f"{unbacked}. golangci-lint exits 0 there — coverage in appearance only.")


def test_editing_a_module_wakes_the_lint_job() -> None:
    """A gate the changed file cannot wake never runs on the PR that breaks it.

    ⛔ Ordering: adding a lint step before the filter reaches the module
    installs a gate that reports `0 issues` and never runs (#1399's shape).
    """
    filters = _workflow_filters(VALIDATE)
    condition = str(_load_workflow(VALIDATE)["jobs"][LINT_JOB]["if"])

    assert not _condition_triggers(condition, "zzz-not-a-tree/probe.go",
                                   filters), (
        "an unrelated top-level path reports as triggering, so this test "
        "cannot tell covered from uncovered — look for a catch-all entry.")

    unreachable = sorted(
        d for d in set(_lint_steps().values())
        if not _condition_triggers(condition, f"{d}/probe.go", filters))
    assert not unreachable, (
        f"these modules cannot trigger {LINT_JOB}: {unreachable}. A path-skipped "
        "required check reports `skipped`, which SATISFIES branch protection — "
        f"the step is real and never runs. Widen the `validate` filter in "
        f"{VALIDATE.name} first.")


# ── build tags ─────────────────────────────────────────────────────────────


def test_build_tags_are_pinned_and_enrolled() -> None:
    found = _build_tags()
    assert _ANCHOR_TAG in found, (
        f"the //go:build scan lost {_ANCHOR_TAG!r} (found {sorted(found)}).")

    bad = sorted(t for t, d in _TAG_DISPOSITION.items() if d not in _DISPOSITIONS)
    assert not bad, (
        f"unknown disposition for {bad}; the enrolment loop skips values it "
        "does not recognise, so a typo would read as coverage.")

    unpinned = sorted(set(found) - set(_TAG_DISPOSITION))
    assert not unpinned, (
        f"build tag(s) with no entry in _TAG_DISPOSITION: {unpinned} "
        f"(in {[f for t in unpinned for f in found[t]][:3]}). Files behind an "
        "unenrolled tag are not loaded by `golangci-lint run ./...`, which "
        "still exits 0. `enrol` adds the tag to the owning module's "
        "`run.build-tags`; the other two values lint nothing and are not "
        "verified.")

    gone = sorted(set(_TAG_DISPOSITION) - set(found))
    assert not gone, f"pinned tag(s) no longer in any .go file: {gone}."

    modules = _go_modules()
    for tag, disposition in _TAG_DISPOSITION.items():
        if disposition != "enrol":
            continue
        owners = sorted({_owning_module(f, modules) for f in found[tag]} - {None})
        assert owners, (
            f"tag {tag!r} is marked `enrol` but none of its files sits under a "
            "go.mod, so nothing can enrol it — the disposition is unreachable.")
        for module in owners:
            declared = (_module_config(module).get("run") or {}).get("build-tags") or []
            assert tag in [str(t) for t in declared], (
                f"{module}/.golangci.yml does not list {tag!r} under "
                "`run.build-tags`, so `run ./...` loads none of its tagged "
                "files and prints `0 issues`.")


# ── the step must be able to report ────────────────────────────────────────


def _excludes_every_go_file(patterns: list[str | None], files: list[str]) -> bool:
    """Would these exclusion `path`s together swallow the module's corpus?

    ⛔ Derived, not enumerated. A blacklist of spellings (`.*`, `^`, …) was
    measured to miss every natural one — including `_test\\.go` in a module
    whose files are ALL tests, which is this repo's most-copied exclusion.
    A rule with no `path` applies to everything, hence None -> True.

    ⛔ `files` must be MODULE-relative. golangci-lint matches these patterns
    against the path as seen from the module root, so `^foo\\.go$` excludes
    everything in a one-file module while matching no repo-relative path at
    all — measured: it took `run ./...` from rc=1 to `0 issues.` while an
    earlier revision of this predicate called it harmless.

    ⛔ Patterns are evaluated as a UNION, the way golangci applies them.
    Asking one at a time answers a narrower question than the config does —
    two patterns covering a file each silence a two-file module while neither
    is a catch-all on its own.
    """
    compiled = []
    for pattern in patterns:
        if pattern is None:
            return True
        try:
            compiled.append(re.compile(pattern))
        except re.error:
            continue  # golangci would reject it; not this guard's judgement
    return bool(files) and bool(compiled) and all(
        any(rx.search(f) for rx in compiled) for f in files)


def test_every_linted_module_reports_something() -> None:
    """Enrolment is a step; this is whether the step can ever report.

    `default: none`, a linter set emptied by `disable`, or an exclusion that
    matches the whole module all make `0 issues` permanent while the module
    still counts as enrolled.

    ⛔ `formatters:` is a SEPARATE top-level section in v2, and a config
    carrying only `linters:` reports no formatting drift at all (#1699). It is
    checked here rather than in its own test because it is the same invariant —
    the step runs and cannot report — read off a second carrier.
    """
    modules = sorted(set(_lint_steps().values()))
    assert _ANCHOR_MODULE in modules, (
        f"the lint-step derivation lost {_ANCHOR_MODULE!r} (got {modules}); the "
        "loop below would assert nothing.")

    go_files = [p for p in _tracked_files() if p.endswith(".go")]
    for module in modules:
        cfg = _module_config(module)
        linters = cfg.get("linters") or {}
        own = [f for f in go_files if f.startswith(module + "/")]

        assert linters.get("default") != "none", (
            f"{module}/.golangci.yml sets `linters.default: none`; the step "
            "runs and reports nothing.")

        enabled = set(linters.get("enable") or [])
        disabled = set(linters.get("disable") or [])
        missing = sorted((_REQUIRED_LINTERS - enabled) | (_REQUIRED_LINTERS & disabled))
        assert not missing, (
            f"{module}/.golangci.yml does not run {missing} — the workflow's "
            "own description of this job says every module does. Enable it, or "
            "change that description.")

        issues = cfg.get("issues") or {}
        for key in ("max-issues-per-linter", "max-same-issues"):
            assert issues.get(key) == 0, (
                f"{module}/.golangci.yml leaves `issues.{key}` at its default, "
                "which truncates the report — #1751 mis-measured this repo by "
                "exactly that. Set it to 0.")

        # Module-relative, because that is the base golangci matches against.
        rel = [f[len(module) + 1:] for f in own]

        unvetted = sorted(set(cfg.get("run") or {}) - _VETTED_RUN_KEYS)
        assert not unvetted, (
            f"{module}/.golangci.yml sets `run.{unvetted}`, which nobody has "
            "looked at yet. Four v2 keys are deliberately absent, each measured "
            "to turn a finding into rc=0: `issues-exit-code: 0` prints every "
            "finding and exits 0 anyway; `tests: false` empties the corpus of a "
            "module whose .go files are all tests; `relative-path-mode` moves "
            "the base the exclusion patterns above match against; `go:` above "
            "the go.mod version retires the diagnostics gated on it. Check "
            "yours against those shapes, then add it to _VETTED_RUN_KEYS.")

        formatters = cfg.get("formatters") or {}
        assert formatters.get("enable"), (
            f"{module}/.golangci.yml enables no formatter. v2 moved gofmt out "
            "of `linters`, so a config holding only `linters:` runs over a "
            "mis-formatted tree and exits 0. Asserted non-empty rather than "
            "pinned to `gofmt`: swapping in a stricter formatter is not a "
            "regression, and pinning would report one.")

        # ⛔ ONE set, not one per section: `linters.exclusions.paths` filters
        # formatter findings too, so a config splitting the corpus between the
        # two sections silences the formatter while each section on its own
        # looks narrow. Measured: `run ./...` rc=1 -> `0 issues.`.
        silencing = [
            str(p)
            for section in ("formatters", "linters")
            for p in ((cfg.get(section) or {}).get("exclusions") or {}).get("paths") or []
        ]
        assert not _excludes_every_go_file(silencing, rel), (
            f"{module}/.golangci.yml has `exclusions.paths` (formatters and "
            f"linters combined: {silencing}) matching all {len(rel)} of its "
            ".go files, so NO file in this module is format-checked any more. "
            "Other linters may still report — this says the formatter cannot. "
            "Narrow the patterns or drop them; reformatting the files cannot "
            "clear it, because the exclusion is what silences them.")

        # ⛔ DECLARED GAP — judging a rule by its `path` alone is the wrong
        # dimension, in both directions, and this change does not fix it
        # (#1821 carries the measurements). A rule silences only the linters it
        # names — including a formatter, if named — so `path` on its own
        # neither proves nor disproves that the module can still report.
        # Nothing in the tree trips either direction today.
        for rule in (linters.get("exclusions") or {}).get("rules") or []:
            pattern = rule.get("path")
            assert not _excludes_every_go_file(
                [None if pattern is None else str(pattern)], rel), (
                f"{module}/.golangci.yml has an exclusion matching all "
                f"{len(rel)} of its .go files (`path: {pattern}`). In a module "
                "that is entirely tests, `_test\\.go` is exactly this. Narrow "
                "it, or drop the linter deliberately.")


# ── toolchain ──────────────────────────────────────────────────────────────


def test_golangci_version_matches_the_dev_container() -> None:
    """`components/tenant-api/.golangci.yml` tells contributors a local run
    equals the gate. That holds only while both sides run the same linter."""
    pinned = re.findall(r"^\s*GOLANGCI_VERSION=v([0-9]+\.[0-9]+\.[0-9]+)\s*$",
                        VALIDATE.read_text(encoding="utf-8"), re.MULTILINE)
    assert len(pinned) == 1, (
        f"expected exactly one GOLANGCI_VERSION pin in {VALIDATE.name}, got "
        f"{pinned}: nothing to compare, or no way to tell which one installs.")

    # ⛔ No optional `v` here. The devcontainers `go` feature prepends it, so
    # `"v2.12.2"` becomes tag `vv2.12.2` and the install fails — accepting that
    # spelling would make this test green on a container that has no linter.
    declared = re.findall(r'"golangciLintVersion"\s*:\s*"([0-9]+\.[0-9]+\.[0-9]+)"',
                          DEVCONTAINER.read_text(encoding="utf-8"))
    assert len(declared) == 1, (
        f"{DEVCONTAINER.name} must pin `golangciLintVersion` exactly once, "
        f"unprefixed (got {declared}). Unpinned, the feature installs `latest`; "
        "prefixed with `v`, the install fails.")

    assert declared[0] == pinned[0], (
        f"{VALIDATE.name} installs golangci-lint {pinned[0]}, the dev container "
        f"declares {declared[0]}. Bump both together.")
