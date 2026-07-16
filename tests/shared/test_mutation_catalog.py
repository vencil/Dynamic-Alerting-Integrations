"""Anti-rot gate for BOTH mutation-pilot catalogs (Python + Go).

Why this exists (ROI refactor round 3): the nightly mutation pilot
applies each catalog entry by exact-string replacement. When a target
function is refactored, the entry's ``old`` string no longer matches,
``apply()`` raises, and the pilot records SETUP-FAIL — which the
nightly workflow historically swallowed (fail-open). Six Python
entries rotted this way without any PR going red.

This test is the fail-CLOSED half: it runs in the regular Python
Tests job (<1s, purely static — no pytest re-runs, no Go toolchain)
and asserts every ``Mutation.old`` appears EXACTLY ONCE in its target
file:

  - 0 matches  → catalog rot: the source was refactored; re-anchor
    the entry's ``old``/``new`` to the current code shape.
  - >1 matches → ambiguous injection point: add more context lines to
    the ``old`` string so the mutation lands deterministically.

So the PR that refactors a mutated function goes red HERE, at commit
time, instead of surfacing days later as a soft nightly SETUP-FAIL.

Note: ``tests/shared/test_go_mutation_pilot.py`` already carries a
same-shaped check for the Go catalog (plus Go-specific contract
tests); this file is the uniform two-pilot gate, so the Python
catalog — the one that actually rotted — gets identical coverage.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_pilot(module_name: str):
    """Import a pilot module by file path.

    The pilots are underscore-prefixed on purpose (pytest must not
    collect them), so they can't be imported via the normal test
    package mechanism — mirror test_go_mutation_pilot.py's loader.
    """
    path = REPO_ROOT / "tests" / "shared" / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_py_pilot = _load_pilot("_mutation_pilot")
_go_pilot = _load_pilot("_go_mutation_pilot")

# (pilot module, base dir for target_file resolution, mutation, id)
_CASES = [
    pytest.param(
        _py_pilot.REPO_ROOT, m,
        id=f"py::{m.fn_name}::{m.label[:48]}",
    )
    for m in _py_pilot.MUTATIONS
] + [
    pytest.param(
        # Go mutations now span two modules (exporter, tenant-api); the base
        # dir for target_file resolution is the mutation's own module_dir, not
        # a single hard-coded GO_APP_DIR.
        m.module_dir(), m,
        id=f"go::{m.fn_name}::{m.label[:48]}",
    )
    for m in _go_pilot.MUTATIONS
]


@pytest.fixture(scope="module")
def file_cache():
    """Read each unique target file once per session, not per case."""
    cache: dict[Path, str] = {}

    def read(path: Path) -> str:
        if path not in cache:
            cache[path] = path.read_text(encoding="utf-8")
        return cache[path]

    return read


class TestMutationCatalogAnchored:
    """Every catalog entry must anchor to the CURRENT source exactly once."""

    @pytest.mark.parametrize("base_dir,mutation", _CASES)
    def test_old_string_present_exactly_once(self, base_dir, mutation, file_cache):
        path = base_dir / mutation.target_file
        assert path.is_file(), (
            f"target file {mutation.target_file} not found "
            f"(mutation: {mutation.label})"
        )
        count = file_cache(path).count(mutation.old)
        assert count == 1, (
            f"old_string for {mutation.label!r} appears {count} times in "
            f"{mutation.target_file} (expected exactly 1).\n"
            "  0  → catalog rot: the source was refactored — re-anchor the\n"
            "       mutation's old=/new= to the current code shape\n"
            "       (tests/shared/_mutation_pilot.py or _go_mutation_pilot.py).\n"
            "  >1 → ambiguous injection point: widen the old= string with\n"
            "       surrounding context lines until it is unique."
        )

    @pytest.mark.parametrize("base_dir,mutation", _CASES)
    def test_old_differs_from_new(self, base_dir, mutation):
        # A no-op mutation would always "survive" and pollute the signal.
        assert mutation.old != mutation.new, (
            f"mutation {mutation.label!r} has identical old/new — no-op"
        )


class TestKillTargetsExist:
    """The OTHER half of catalog rot: a stale kill-test reference.

    A deleted/renamed test file makes pytest exit with a usage error
    (rc=4) while running ZERO tests — which the pre-2026-07 pilots
    counted as CAUGHT (rc != 0), i.e. a fake kill. Three parse_commit
    entries pointed at a test file deleted in PR #350 for months this
    way. The pilots now bin rc∉{0,1} as SETUP-FAIL at run time; this
    gate catches the stale reference statically at PR time.

    Validation depth per pilot:
      - Python: test_file is a space-separated list of pytest paths —
        assert every token is an existing FILE.
      - Go: test_target is a `go test` package selector (`./...`,
        `./pkg/config/...`, `./internal/rbac/...`), not a file — assert the
        selector's base DIRECTORY exists under the mutation's module root
        (module_dir: exporter vs tenant-api), filesystem level only; which
        packages the pattern expands to is go-toolchain territory that a
        static Python test can't see.
    """

    @pytest.mark.parametrize(
        "mutation",
        _py_pilot.MUTATIONS,
        ids=[f"py::{m.fn_name}::{m.label[:48]}" for m in _py_pilot.MUTATIONS],
    )
    def test_python_test_files_exist(self, mutation):
        for token in mutation.test_file.split():
            assert (_py_pilot.REPO_ROOT / token).is_file(), (
                f"kill-test reference {token!r} (mutation {mutation.label!r}) "
                "does not exist — pytest would rc=4 with zero tests run, "
                "which the old runner mis-counted as CAUGHT. Re-point "
                "test_file to the surviving test scope."
            )

    @pytest.mark.parametrize(
        "mutation",
        _go_pilot.MUTATIONS,
        ids=[f"go::{m.fn_name}::{m.label[:48]}" for m in _go_pilot.MUTATIONS],
    )
    def test_go_test_target_dirs_exist(self, mutation):
        selector = mutation.test_target
        assert selector.startswith("./"), (
            f"test_target {selector!r} is not a ./-rooted package selector"
        )
        base = selector[2:]
        if base.endswith("..."):
            base = base[:-3]
        base = base.rstrip("/")
        # Selector is resolved from the mutation's module root (module_dir),
        # not a single hard-coded GO_APP_DIR — tenant-api entries live in a
        # different Go module than the exporter entries.
        module_dir = mutation.module_dir()
        target_dir = module_dir / base if base else module_dir
        assert target_dir.is_dir(), (
            f"test_target {selector!r} (mutation {mutation.label!r}) points "
            f"at a non-existent directory {target_dir} — `go test` would "
            "fail with a matched-no-packages error instead of running the "
            "kill suite."
        )


def _go_selector_dir(mutation) -> tuple:
    """Resolve a Go mutation's test_target selector to (dir, recursive)."""
    base = mutation.test_target[2:]  # strip "./" (shape asserted elsewhere)
    recursive = base.endswith("...")
    if recursive:
        base = base[:-3]
    base = base.rstrip("/")
    module_dir = mutation.module_dir()
    return (module_dir / base if base else module_dir), recursive


_KILL_TEST_CASES = [
    pytest.param(
        "py", m, id=f"py::{m.fn_name}::{m.kill_test}",
    )
    for m in _py_pilot.MUTATIONS if m.kill_test
] + [
    pytest.param(
        "go", m, id=f"go::{m.fn_name}::{m.kill_test}",
    )
    for m in _go_pilot.MUTATIONS if m.kill_test
]


class TestKillTestNamesAnchored:
    """Third catalog-rot half: the NAMED kill-test attribution.

    A mutation's ``kill_test`` names the test observed red when the entry
    was injection-verified — the rot-triage handle ("this survivor/rot maps
    to THAT test"). Unlike ``old`` (whose drift breaks apply()) and
    ``test_target`` (whose drift breaks the runner), a renamed kill test
    keeps the nightly GREEN while the attribution silently rots into a
    dangling name. This lane pins it statically: a non-None kill_test must
    exist as a test definition within the entry's kill scope —

      - Python: ``def <name>(`` in one of the mutation's test_file files;
      - Go: ``func <name>(`` in a *_test.go under the test_target selector's
        directory (recursive for ``...`` selectors).

    kill_test=None entries (historical batches verified at file/package
    scope without per-test attribution) are exempt by construction — the
    field's doc comment in each pilot explains when to backfill.
    """

    @pytest.mark.parametrize("kind,mutation", _KILL_TEST_CASES)
    def test_kill_test_definition_exists(self, kind, mutation):
        if kind == "py":
            files = [_py_pilot.REPO_ROOT / t for t in mutation.test_file.split()]
            marker = f"def {mutation.kill_test}("
        else:
            target_dir, recursive = _go_selector_dir(mutation)
            pattern = "*_test.go"
            files = sorted(
                target_dir.rglob(pattern) if recursive else target_dir.glob(pattern)
            )
            # Go tests are package-level funcs; methods never appear as tests.
            marker = f"func {mutation.kill_test}("
        assert files, (
            f"kill scope for {mutation.label!r} contains no test files — "
            "the kill_test attribution cannot be anchored"
        )
        assert any(
            marker in f.read_text(encoding="utf-8") for f in files
        ), (
            f"kill_test {mutation.kill_test!r} (mutation {mutation.label!r}) "
            f"not found as {marker!r} in the entry's kill scope. The test was "
            "renamed/moved — re-point kill_test to the surviving name (re-run "
            "the injection if unsure which test kills it now), so the rot-"
            "triage attribution doesn't silently dangle."
        )
