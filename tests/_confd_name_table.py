"""The conf.d name table's facts a test needs, read from the table (#3206).

`tests/shared/confd_name_classification_matrix.json` is owned by Go
(`pkg/confdname`'s matrix test asserts every cell and the coverage floor).
The Python side keeps no copy of the name rule — the former extension
constant and the name predicates of `_lib_confd` were deleted — so a test that needs "every spelling of a
config file's extension" derives it from the table's rows here instead of
typing the set out.
"""
from __future__ import annotations

import json
from pathlib import Path

MATRIX = Path(__file__).resolve().parent / "shared" / "confd_name_classification_matrix.json"


def rows() -> "list[dict]":
    """The table's rows."""
    return json.loads(MATRIX.read_text(encoding="utf-8"))["rows"]


def config_suffixes() -> "tuple[str, ...]":
    """Every extension spelling the table's tenant-carrier rows carry,
    lowercased, sorted: what follows the stem of each row that is a tenant
    carrier (`yaml_extension`, not reserved, not hidden). Raises when the
    table yields fewer than two — a gutted table must not make a floor
    test vacuous."""
    out = sorted({r["name"][len(r["stem"]):].lower() for r in rows()
                  if r["yaml_extension"] and not r["reserved_prefix"] and not r["hidden"]})
    if len(out) < 2:
        raise AssertionError(f"the name table yields extension spellings {out!r}; "
                             "pkg/confdname's matrix test should have gone red first")
    return tuple(out)
