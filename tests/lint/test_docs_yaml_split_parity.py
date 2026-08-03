"""Python half of the Go<->Python doc-block splitting parity pin.

Both gates that read docs/**/*.md must cut a fenced block into the same units:

  * Go     pkg/config/docs_defaults_sample_test.go  (loader oracle)
  * Python scripts/tools/lint/check_md_yaml_drift.py (schema oracle)

Neither asserts the other. Both assert tests/shared/docs_yaml_split_matrix.json,
which makes their agreement transitive rather than claimed — the same shape as
tests/shared/optional_overrides_membership_matrix.json.

The Go half lives in TestDocsYamlSplitMatrix in that same test file; if you
change the rule, both halves fail until the matrix and both implementations
agree again. That is the point.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MATRIX = REPO / "tests" / "shared" / "docs_yaml_split_matrix.json"
SCRIPT = REPO / "scripts" / "tools" / "lint" / "check_md_yaml_drift.py"


def _load_checker_module():
    """Import the lint module by path (it is a script, not a package member)."""
    # The script bootstraps its own sys.path for _lib_compat; mirror that here.
    for extra in (SCRIPT.parent, SCRIPT.parent.parent):
        if str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
    spec = importlib.util.spec_from_file_location("check_md_yaml_drift", SCRIPT)
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
    segments = mod.MdYamlDriftChecker._split_documented_files(case["block"])
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
    segments = mod.MdYamlDriftChecker._split_documented_files(case["block"])
    rebuilt: list[str] = []
    for _offset, text in segments:
        rebuilt.extend(text.split("\n"))
    assert rebuilt == case["block"], f"{case['name']}: segments do not re-form the block"
