"""Parse a Python source file the way the interpreter would read it.

Why this exists (#1632): ``ast.parse(path.read_text("utf-8"))`` differs from
the interpreter on two counts. A file saved with a UTF-8 BOM imports fine —
the tokenizer drops the BOM — but ``read_text("utf-8")`` keeps the ``\\ufeff``
and ``ast.parse`` then raises ``invalid non-printable character U+FEFF``.
And without ``filename=`` every diagnostic says ``<unknown>``, so a test that
reports the failure cannot say which file it could not read.

⚠️ Only the BOM is forgiven. A file that is genuinely not Python still raises
``SyntaxError`` (now naming its path); callers that scan many files must turn
that into a reported offender, never a silent skip.
"""
from __future__ import annotations

import ast
from pathlib import Path


def parse_py(path: Path | str) -> ast.Module:
    """``ast.parse`` of ``path``, BOM-tolerant, with the path in diagnostics."""
    path = Path(path)
    source = path.read_bytes().decode("utf-8").removeprefix("\ufeff")
    return ast.parse(source, filename=str(path))
