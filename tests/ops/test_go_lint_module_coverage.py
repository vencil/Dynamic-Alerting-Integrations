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

import contextlib
import re
import sys
import warnings
from pathlib import Path

import pytest
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
    shadowing = _shadowing_configs(ROOT / module)
    assert not shadowing, (
        f"{module} carries {shadowing} next to .golangci.yml; golangci may "
        "read one of those while this guard reads .golangci.yml. Keep one.")
    return yaml.safe_load(config.read_text(encoding="utf-8")) or {}


def _shadowing_configs(directory: Path) -> list[str]:
    """Sibling config files golangci could pick over `.golangci.yml`.

    Measured: a clean `.yml` beside a `.golangci.yaml` carrying
    `paths: ['.*']` runs to `0 issues`. Exact names of regular files only —
    a directory of that name, or a differently-cased one on Windows, is not
    a config to golangci and must not read as one here.
    """
    present = {p.name for p in directory.iterdir() if p.is_file()}
    return sorted(present & {".golangci.json", ".golangci.toml", ".golangci.yaml"})


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
            declared = _as_list((_module_config(module).get("run") or {}).get("build-tags"),
                                "run.build-tags")
            assert tag in [str(t) for t in declared], (
                f"{module}/.golangci.yml does not list {tag!r} under "
                "`run.build-tags`, so `run ./...` loads none of its tagged "
                "files and prints `0 issues`.")


# ── the step must be able to report ────────────────────────────────────────


def _as_list(value: object, where: str) -> list:
    """golangci list fields, read as lists only.

    `config verify` rejects a scalar in these fields; `run` — all CI executes
    — reinterprets it (a comma-separated string becomes several linters).
    Measured: `linters: godoclint` under a rule silences godoclint. Reading
    it any other way here would be a second, wrong parser.
    """
    if value is None:
        return []
    assert isinstance(value, list), (
        f"`{where}` is {value!r}, not a list; golangci `run` reads it in a "
        "way this guard does not model. Write it as `- item`.")
    return value


def _matching(pattern: object, files: list[str]) -> set[str]:
    """Files the exclusion regex hits.

    ⛔ Reject-to-score, never fail-open. golangci matches with Go RE2, this
    guard with Python `re`, and a pattern only one of them reads was a silent
    green in an earlier revision — measured: `\\pL` and `[[:alpha:]]` each
    take `run ./...` to `0 issues` while Python rejects the first and reads
    the second as a different set (caught through the FutureWarning Python
    emits for nested-set spellings). A YAML `!!binary` scalar is not a str
    and is refused for the same reason.
    """
    assert isinstance(pattern, str), (
        f"exclusion pattern {pattern!r} is not a string; golangci reads it as "
        "a regex and this guard cannot. Quote it.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", FutureWarning)
            # The warning is emitted while PARSING; a pattern already in
            # `re`'s compile cache is not parsed again and would pass here.
            re.purge()
            rx = re.compile(pattern)
    except (re.error, FutureWarning) as exc:
        raise AssertionError(
            f"exclusion pattern {pattern!r} is not readable by this guard "
            f"({exc}). golangci uses Go RE2, this guard uses Python `re`; a "
            "pattern only one of them reads is unmodelled, not harmless. Spell "
            "it in the subset both dialects share.") from exc
    return {f for f in files if rx.search(f)}


def _silenced_files(cfg: dict, checker: str, files: list[str], *,
                    formatter: bool) -> set[str]:
    """Module-relative files on which `checker` can never report.

    ONE union over every carrier golangci applies, each measured on
    golangci-lint 2.12.2 (cells in this file's #1821 commit):

      linters.exclusions.paths         every checker, formatters included
      linters.exclusions.paths-except  every checker; a file survives when
                                       ANY pattern matches it
      linters.exclusions.rules         the linters a rule names, or every
                                       checker when `linters:` is absent;
                                       `path-except` inverts `path`
      formatters.exclusions.paths      formatters only

    ⛔ `files` must be MODULE-relative: golangci matches against the path as
    seen from the module root, so `^foo\\.go$` silences a one-file module
    while matching no repo-relative path at all.

    ⛔ `text:` and `source:` are NOT evaluated: `text: .*` is a total filter,
    `text: zzz` an empty one, and nothing static tells them apart — so a rule
    is taken to be as wide as its path and linters allow. Fail-closed on
    purpose: the cheapest way to green a catch-all rule must not be to add a
    `text:` to it. The price: a text/source rule that names no linter (the
    shape `golangci-lint migrate` emits for v1 `issues.exclude`) or names a
    required one reads as retiring every checker it reaches.

    Not read, so not guarded: `linters.settings.<name>` (a linter can be
    emptied from inside), `exclusions.presets` / `generated` (text- and
    header-scoped, never total), `issues.new*` (drops every finding already
    on the branch), `issues.fix` (rewrites the file instead of reporting).
    """
    silenced: set[str] = set()
    exclusions = (cfg.get("linters") or {}).get("exclusions") or {}

    for pattern in _as_list(exclusions.get("paths"), "linters.exclusions.paths"):
        silenced |= _matching(pattern, files)

    excepts = _as_list(exclusions.get("paths-except"), "linters.exclusions.paths-except")
    if excepts:
        kept: set[str] = set()
        for pattern in excepts:
            kept |= _matching(pattern, files)
        silenced |= set(files) - kept

    for rule in _as_list(exclusions.get("rules"), "linters.exclusions.rules"):
        named = [str(n) for n in _as_list(rule.get("linters"), "rule.linters")]
        if named and checker not in named:
            continue
        scope = set(files)
        if rule.get("path") is not None:
            scope &= _matching(rule["path"], files)
        if rule.get("path-except") is not None:
            scope -= _matching(rule["path-except"], files)
        silenced |= scope

    if formatter:
        formatters = (cfg.get("formatters") or {}).get("exclusions") or {}
        for pattern in _as_list(formatters.get("paths"), "formatters.exclusions.paths"):
            silenced |= _matching(pattern, files)

    return silenced


_MIXED = ["a.go", "b.go", "a_test.go"]
_ALL_TESTS = ["x_test.go", "y_test.go"]

# One row per axis of `_silenced_files`. An `E<n>` id is the shape of that
# cell in the #1821 commit's golangci matrix; other ids are guard-side only.
_SILENCING_CASES = [
    ("E5 two half rules naming the checker silence the whole module",
     _MIXED, {"linters": {"exclusions": {"rules": [
         {"path": "^a", "linters": ["godoclint", "gofmt"]},
         {"path": "^b", "linters": ["godoclint", "gofmt"]}]}}},
     "godoclint", False, {"a.go", "b.go", "a_test.go"}),
    ("E9 a rule naming another linter silences nothing for this one",
     _ALL_TESTS, {"linters": {"exclusions": {"rules": [
         {"path": r"_test\.go", "linters": ["errcheck"]}]}}},
     "gofmt", True, set()),
    ("E8 formatters.exclusions.paths reaches the formatter",
     _MIXED, {"formatters": {"exclusions": {"paths": [".*"]}}},
     "gofmt", True, {"a.go", "b.go", "a_test.go"}),
    ("E4 path-except inverts path",
     _MIXED, {"linters": {"exclusions": {"rules": [
         {"path-except": r"_test\.go", "linters": ["godoclint"]}]}}},
     "godoclint", False, {"a.go", "b.go"}),
    ("E28 paths-except keeps a file ANY of its patterns matches",
     _MIXED, {"linters": {"exclusions": {"paths-except": [r"^a\.go$", r"^b\.go$"]}}},
     "godoclint", False, {"a_test.go"}),
    ("E8-linter-side formatters.exclusions.paths does not reach a linter",
     _MIXED, {"formatters": {"exclusions": {"paths": [".*"]}}},
     "godoclint", False, set()),
    ("E7 linters.exclusions.paths reaches the formatter",
     _MIXED, {"linters": {"exclusions": {"paths": [".*"]}}},
     "gofmt", True, {"a.go", "b.go", "a_test.go"}),
    # POLICY, not a measured cell: golangci silences nothing for `text: zzz`
    # (measured with `linters: [gofmt]` added, E23); this guard reads the rule
    # as wide as its path, on purpose.
    ("P1 a rule with no linters reaches the formatter; text is not evaluated",
     _MIXED, {"linters": {"exclusions": {"rules": [{"path": ".*", "text": "zzz"}]}}},
     "gofmt", True, {"a.go", "b.go", "a_test.go"}),
]


@pytest.mark.parametrize("label, files, cfg, checker, formatter, expected",
                         _SILENCING_CASES,
                         ids=[c[0].split()[0] for c in _SILENCING_CASES])
def test_silencing_reads_every_carrier(label, files, cfg, checker, formatter,
                                       expected) -> None:
    assert _silenced_files(cfg, checker, files, formatter=formatter) == expected, label


@pytest.mark.parametrize("pattern", [r"\pL", "[[:alpha:]]", b".*"],
                         ids=["E34-re2-only", "E35-posix-class", "E33-binary"])
def test_a_pattern_this_guard_cannot_read_is_red(pattern) -> None:
    """Each of these silenced a checker in golangci; none may read as harmless."""
    if isinstance(pattern, str):
        # Only a pattern Python compiles can sit in `re`'s cache; put it
        # there first, so the verdict does not depend on who compiled it.
        with warnings.catch_warnings(), contextlib.suppress(re.error):
            warnings.simplefilter("ignore")
            re.compile(pattern)
    with pytest.raises(AssertionError, match="exclusion pattern"):
        _silenced_files({"linters": {"exclusions": {"paths": [pattern]}}},
                        "godoclint", _MIXED, formatter=False)


@pytest.mark.parametrize("cfg", [
    {"linters": {"exclusions": {"rules": [{"path": ".*", "linters": "godoclint"}]}}},
    {"linters": {"exclusions": {"paths": ".*"}}},
], ids=["E32-rule-linters", "scalar-paths"])
def test_a_scalar_where_golangci_wants_a_list_is_red(cfg) -> None:
    """`run` reads these; `config verify` (which CI never runs) rejects them."""
    with pytest.raises(AssertionError, match="not a list"):
        _silenced_files(cfg, "godoclint", _MIXED, formatter=False)


def test_a_sibling_config_file_is_red(tmp_path, monkeypatch) -> None:
    """Through `_module_config`, not the helper: the assert lives at the call."""
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / ".golangci.yml").write_text("version: '2'\n", encoding="utf-8")
    (tmp_path / "m" / ".golangci.yaml").mkdir()    # a directory is not a config
    assert _module_config("m") == {"version": "2"}
    (tmp_path / "m" / ".golangci.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="carries"):
        _module_config("m")


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

        enabled = set(_as_list(linters.get("enable"), "linters.enable"))
        disabled = set(_as_list(linters.get("disable"), "linters.disable"))
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

        # Per checker, not per rule: a rule silences only the linters it
        # names, so its `path` alone proves nothing either way (#1821).
        # `_test\.go` + `[errcheck]` in an all-tests module retires errcheck
        # and nothing else — legal; the same path naming godoclint retires
        # the linter this job promises for every module.
        checkers = [(name, False) for name in sorted(_REQUIRED_LINTERS)]
        checkers += [(str(name), True)
                     for name in _as_list(formatters["enable"], "formatters.enable")]
        for checker, is_formatter in checkers:
            silenced = _silenced_files(cfg, checker, rel, formatter=is_formatter)
            assert set(rel) - silenced, (
                f"{module}/.golangci.yml: as this guard reads it, {checker} is "
                f"excluded on all {len(rel)} of the module's .go files — by "
                "`linters.exclusions.paths` / `paths-except`, by a rule that "
                "lists it or lists no linters, or (formatters) by "
                "`formatters.exclusions.paths`. `text:`/`source:` are not "
                "evaluated, so a rule narrowed only by text counts as wide as "
                "its path. Enrolled and silent reads exactly like clean. To "
                "green: make the exclusion reach fewer files (a narrower "
                "`path`, a wider `paths-except`), or point the rule at the "
                f"linters the text actually comes from instead of {checker}. A "
                f"text-only rule naming just {checker} cannot be narrowed here "
                f"— drop it and use `//nolint:{checker}` at the site. "
                "Reformatting or fixing the code cannot clear it.")


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
