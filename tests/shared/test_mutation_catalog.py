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
        _go_pilot.GO_APP_DIR, m,
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
