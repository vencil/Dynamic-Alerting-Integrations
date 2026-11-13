"""The rule that cuts a documented ```yaml block into the files it documents.

scripts/tools/_lib_doc_yaml_inventory.py (`split_documented_files`) is the one
implementation: the drift lint's three checks, the unit-sanity lint and the Go
loader test all read its units (#3416). The Go test kept a second,
hand-written copy of the rule (and a Go half of this test) until #3814; it
now reads the inventory's JSON, so there is no second copy to hold to it.

The cases live in tests/shared/docs_yaml_split_matrix.json, a case matrix
that states the RULE rather than a snapshot of the live docs.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MATRIX = REPO / "tests" / "shared" / "docs_yaml_split_matrix.json"
SCRIPT = REPO / "scripts" / "tools" / "_lib_doc_yaml_inventory.py"


def _load_checker_module():
    """Import the inventory module by path (it is a script, not a package member)."""
    if str(SCRIPT.parent) not in sys.path:
        sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("_lib_doc_yaml_inventory", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CASES = json.loads(MATRIX.read_text(encoding="utf-8"))["cases"]


def test_matrix_is_not_empty() -> None:
    """A parity pin that measures nothing must fail loudly, not pass quietly."""
    assert len(CASES) >= 8, f"matrix shrank to {len(CASES)} cases"


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_python_splitter_matches_matrix(case: dict) -> None:
    mod = _load_checker_module()
    segments = mod.split_documented_files(case["block"])
    got = [offset for offset, _text in segments]
    assert got == case["offsets"], (
        f"{case['name']}\n  expected offsets {case['offsets']}, got {got}\n"
        f"  why this case exists: {case['why']}"
    )


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_segments_reconstruct_the_block(case: dict) -> None:
    """Splitting must PARTITION the block — no line duplicated, none dropped.

    A splitter that silently ate a line would still satisfy the offsets above
    while quietly removing config from the gate's view.
    """
    mod = _load_checker_module()
    segments = mod.split_documented_files(case["block"])
    rebuilt: list[str] = []
    for _offset, text in segments:
        rebuilt.extend(text.split("\n"))
    assert rebuilt == case["block"], f"{case['name']}: segments do not re-form the block"
