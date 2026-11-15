"""`tests/_pysource.py::parse_py` reads a file the way the interpreter does.

Both halves of its contract (#1632), on constructed files because the tree
has neither a BOM'd module nor a broken one — its green says nothing about
either branch:

  * a UTF-8 BOM is forgiven, exactly as `import` forgives it;
  * a file that is not Python RAISES, naming its path. `parse_py` never
    returns "nothing" for a file it could not read, so a caller scanning many
    files has to decide what to do with the failure — it cannot skip the
    file by accident.

These were held through `_scan_test_modules` in
tests/ops/test_ci_path_filter_coverage.py until #3756 retired that scanner
with the path filters it graded; `parse_py` has other callers, so its
contract is pinned here, on the function itself.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from _pysource import parse_py

_BOM = b"\xef\xbb\xbf"
_SOURCE = b"import os\nP = 'docs/index.md'\n"


def test_a_bom_is_forgiven_the_way_the_interpreter_forgives_it(
        tmp_path: Path) -> None:
    """Counterfactual: dropping the BOM strip makes `ast.parse` raise
    `invalid non-printable character U+FEFF` on a file `import` accepts."""
    bom = tmp_path / "test_bom.py"
    bom.write_bytes(_BOM + _SOURCE)
    plain = tmp_path / "test_plain.py"
    plain.write_bytes(_SOURCE)
    # The premise, stated so the test cannot pass on a file with no BOM.
    assert bom.read_text(encoding="utf-8").startswith("﻿")

    tree = parse_py(bom)

    assert isinstance(tree, ast.Module)
    assert ast.dump(tree) == ast.dump(parse_py(plain)), (
        "a BOM changed what was parsed, not only whether it parsed")
    # `str` paths are accepted too (callers pass `__file__`).
    assert ast.dump(parse_py(str(bom))) == ast.dump(tree)
    # And it is what the interpreter itself does with the same bytes.
    compile(bom.read_bytes(), str(bom), "exec")


@pytest.mark.parametrize("payload", [
    b"def f(:\n",
    # ⛔ Only a LEADING BOM is forgiven. One anywhere else is not something
    # the interpreter accepts either; stripping every U+FEFF would hide it.
    b"import os\n\xef\xbb\xbfP = 1\n",
])
def test_an_unparseable_file_raises_and_names_itself(
        tmp_path: Path, payload: bytes) -> None:
    """Counterfactuals: returning an empty module (or None) for a broken file
    lets a scanning caller count it as read; dropping `filename=` makes the
    diagnostic say `<unknown>`, so the caller cannot report WHICH file."""
    broken = tmp_path / "sub" / "test_broken.py"
    broken.parent.mkdir()
    broken.write_bytes(payload)

    with pytest.raises(SyntaxError) as caught:
        parse_py(broken)

    assert caught.value.filename == str(broken), caught.value.filename
    diag = f"{type(caught.value).__name__}: {caught.value}"
    assert diag.startswith("SyntaxError") and "test_broken.py" in diag, (
        f"the diagnostic does not name the file it failed on: {diag!r}")


def test_bytes_that_are_not_utf8_are_not_swallowed(tmp_path: Path) -> None:
    """Undecodable bytes are a failure too, never an empty parse."""
    latin = tmp_path / "test_latin.py"
    latin.write_bytes(b"P = '\xe9'\n")
    with pytest.raises(UnicodeDecodeError):
        parse_py(latin)
