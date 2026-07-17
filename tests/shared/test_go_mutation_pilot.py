"""Smoke tests for the Go mutation-pilot catalog.

Validates the static contract of `_go_mutation_pilot.py` without
actually running `go test` (which requires the Go toolchain to be
present, e.g., Dev Container). The actual pilot run happens via:

    make dc-run CMD="python tests/shared/_go_mutation_pilot.py"

These tests catch catalog-level errors at commit time:

  - Each `Mutation.old` string is present in its `target_file`
  - Each `Mutation.old` is unique within the file (no ambiguous match)
  - Each `Mutation.new` is different from `Mutation.old`
  - Each target file actually exists
  - Test_target package selectors are well-formed
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_PILOT_PATH = REPO_ROOT / "tests" / "shared" / "_go_mutation_pilot.py"

_spec = importlib.util.spec_from_file_location("_go_mutation_pilot", _PILOT_PATH)
_pilot = importlib.util.module_from_spec(_spec)
sys.modules["_go_mutation_pilot"] = _pilot
_spec.loader.exec_module(_pilot)


# ============================================================
# Catalog validation
# ============================================================


class TestCatalog:

    def test_mutations_list_non_empty(self):
        # Property: catalog has at least 10 mutations (otherwise the
        # pilot is below the threshold for being a meaningful demo).
        assert len(_pilot.MUTATIONS) >= 10

    def test_each_mutation_has_required_fields(self):
        for m in _pilot.MUTATIONS:
            assert m.target_file, f"empty target_file: {m.label}"
            assert m.test_target, f"empty test_target: {m.label}"
            assert m.label, f"empty label for {m.fn_name}"
            assert m.old, f"empty old for {m.label}"
            assert m.new, f"empty new for {m.label}"
            assert m.fn_name, f"empty fn_name for {m.label}"

    def test_old_differs_from_new(self):
        for m in _pilot.MUTATIONS:
            assert m.old != m.new, (
                f"mutation {m.label} has identical old/new — would be a no-op"
            )

    def test_target_files_exist(self):
        for m in _pilot.MUTATIONS:
            path = m.module_dir() / m.target_file
            assert path.is_file(), (
                f"target file {m.target_file} not found "
                f"(referenced by mutation {m.label})"
            )

    def test_test_targets_use_package_selector_form(self):
        # Property: test_target should be a Go package selector (./pkg/...
        # or `./...` etc.). Catches typos like "/pkg" or "pkg/config".
        for m in _pilot.MUTATIONS:
            assert m.test_target.startswith("./"), (
                f"test_target {m.test_target!r} should start with './' "
                f"(mutation {m.label})"
            )


class TestOldStringsExist:
    """Every Mutation.old must appear exactly once in its target file —
    otherwise apply() would either no-op or raise. Catches catalog drift
    when the source file is refactored.
    """

    @pytest.fixture(scope="class")
    def file_contents(self):
        # Pre-load each unique target file once. The cache key is
        # (module, target_file) because the same relative path can exist in
        # more than one module (both go modules have their own trees).
        cache: dict[tuple[str, str], str] = {}
        for m in _pilot.MUTATIONS:
            key = (m.module, m.target_file)
            if key not in cache:
                path = m.module_dir() / m.target_file
                cache[key] = path.read_text(encoding="utf-8")
        return cache

    @pytest.mark.parametrize(
        "mutation",
        _pilot.MUTATIONS,
        ids=[f"{m.fn_name}: {m.label[:40]}" for m in _pilot.MUTATIONS],
    )
    def test_old_string_present_and_unique(self, mutation, file_contents):
        src = file_contents[(mutation.module, mutation.target_file)]
        count = src.count(mutation.old)
        assert count == 1, (
            f"old_string for {mutation.label!r} appears {count} times "
            f"in {mutation.target_file} (expected exactly 1). "
            "Either the source was refactored — update the mutation's "
            "old= to the new shape — or the string is too generic and "
            "needs more context."
        )


class TestApplyRevertRoundTrip:
    """apply() + revert() should leave the source file byte-identical
    to its starting state. Catches accidental \\r\\n / encoding drift.
    """

    def test_round_trip_preserves_bytes(self, tmp_path):
        # Pick the first mutation (any will do — they all use the same
        # apply/revert plumbing).
        m = _pilot.MUTATIONS[0]
        path = m.module_dir() / m.target_file
        original = path.read_bytes()

        with open(path, encoding="utf-8", newline="") as f:
            original_text = f.read()

        m.apply()
        try:
            after_apply = path.read_text(encoding="utf-8")
            assert after_apply != original_text, (
                "apply() didn't change the file — old/new must differ"
            )
        finally:
            m.revert(original_text)

        assert path.read_bytes() == original, (
            "revert() didn't restore byte-identical content "
            "(possibly due to newline normalisation)"
        )


class TestReachableFunctions:
    """Every mutation's fn_name should be findable in the target file
    (via `func <name>(`). Catches catalog→source drift when a function
    is renamed."""

    @pytest.mark.parametrize(
        "mutation",
        _pilot.MUTATIONS,
        ids=[f"{m.fn_name}: {m.label[:40]}" for m in _pilot.MUTATIONS],
    )
    def test_fn_name_findable_in_source(self, mutation):
        path = mutation.module_dir() / mutation.target_file
        src = path.read_text(encoding="utf-8")
        # Match either bare `func <name>(...)` or `func (recv) <name>(...)`.
        # Every pilot target is a standalone func (pkg/config primitives and
        # the tenant-api rbac/identity/token pure functions), so the bare form
        # is sufficient — methods would need the `func (recv) name(` form.
        marker = f"func {mutation.fn_name}("
        assert marker in src, (
            f"could not find {marker!r} in {mutation.target_file}. "
            "Was the function renamed or moved? Update the catalog's "
            "fn_name field accordingly."
        )
