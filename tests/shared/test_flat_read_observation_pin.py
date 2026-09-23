"""#1652: pin who may touch the conf.d nesting primitives that record nothing.

`_lib_confd.observe_flat_reads` (used by `validate_config` to stop a flat
row's PASS covering files it never opened) sees a flat read only when it goes
through `warn_nested` or `resolve_defaults_file`. The conf.d enumeration
contract (`test_confd_enumeration_contract.py`) accepts three guard NAMES —
`warn_nested`, `nested_yaml_warning`, `nested_yaml_files` — and matches names
only, so a reader that calls either of the last two passes the contract while
staying invisible to the observer. `_print_nested_once` is the same escape in
private form.

This pins the production call/import sites of those names to exactly the
current set — none outside `_lib_confd` — derived from the AST of every
`scripts/**.py` file, so a new one has to be a decision rather than an
accident.
"""

from __future__ import annotations

import ast
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts"
_UNOBSERVED = {"nested_yaml_warning", "nested_yaml_files", "_print_nested_once",
               "_record_flat_read"}
_OWNER = "scripts/tools/_lib_confd.py"


def _sites(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = (fn.attr if isinstance(fn, ast.Attribute)
                    else getattr(fn, "id", ""))
            if name in _UNOBSERVED:
                found.add(f"call {name}")
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in _UNOBSERVED:
                    found.add(f"import {alias.name}")
        elif isinstance(node, ast.Attribute) and node.attr in _UNOBSERVED:
            # `getattr`-free reference such as `f = _lib_confd.nested_yaml_files`
            found.add(f"reference {node.attr}")
    return found


def _collect() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for path in sorted(_SCRIPTS.rglob("*.py")):
        rel = path.relative_to(_ROOT).as_posix()
        if "__pycache__" in rel:
            continue
        sites = _sites(ast.parse(path.read_text(encoding="utf-8"), rel))
        if sites:
            out[rel] = sites
    return out


def test_scan_sees_the_owner():
    """Must-fire control: the owner module really uses these names, so an
    empty result elsewhere means "none there", not "the scan saw nothing"."""
    found = _collect()
    assert _OWNER in found, sorted(found)
    assert {"call nested_yaml_files", "call nested_yaml_warning",
            "call _print_nested_once"} <= found[_OWNER], found[_OWNER]


def test_no_production_code_outside_lib_confd_uses_the_unobserved_primitives():
    outside = {f: s for f, s in _collect().items() if f != _OWNER}
    assert not outside, (
        "these production files use a conf.d nesting primitive that "
        "validate_config's flat-read observation (`observe_flat_reads`) "
        "CANNOT SEE: "
        + "; ".join(f"{f}: {sorted(s)}" for f, s in sorted(outside.items()))
        + ". A flat reader must announce itself with `warn_nested(...)` "
        "instead — that both prints the skipped files and is recorded, so a "
        "validate_config row built on this reader cannot report PASS about "
        "files it never opened. If you only need the list for something "
        "that is not a read, add the need to `_lib_confd` rather than "
        "calling these directly.")
