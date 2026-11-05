"""#3556: the #1231 spelling rule lives in Go alone.

Which spellings of a tenant-config key name the same threshold — a retired
alias applied to the key's head, exactly or with `_critical`, a `{...}` part
kept as written — is decided by Go (`config.SpellingsOf`) and asked through
`da-guard key-spellings`. Python kept up to three copies of that rule (and a
copy of the alias table), and they disagreed with Go on
`<alias>_critical{...}`. This test keeps a copy from coming back:

* the authored alias table (`_registry_lib.DEPRECATED_KEY_ALIASES`, which the
  registry and Go's mirror are generated from or pinned to) is the only place
  in `scripts/` that spells an alias pair as a mapping, and
* no module but `_registry_lib` reads that table.

A reader that needs a key's other spellings calls
`_lib_tenant_values.key_spellings`.
"""
from __future__ import annotations

import ast
from pathlib import Path

from _registry_lib import DEPRECATED_KEY_ALIASES
from _tree import REPO_ROOT, repo_files

_TABLE_HOME = Path("scripts/tools/ops/_registry_lib.py")


def _scripts() -> list[tuple[Path, ast.AST]]:
    out = []
    for path in repo_files(".py"):
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[0] != "scripts" or rel == _TABLE_HOME:
            continue
        out.append((rel, ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))))
    return out


def _alias_pairs_in(tree: ast.AST) -> list[int]:
    """Lines of dict literals holding a (retired spelling, canonical) pair."""
    pairs = set(DEPRECATED_KEY_ALIASES.items())
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and isinstance(v, ast.Constant) \
                        and (k.value, v.value) in pairs:
                    lines.append(node.lineno)
    return lines


def _table_reads_in(tree: ast.AST) -> list[int]:
    """Lines naming DEPRECATED_KEY_ALIASES (a name, an attribute, an import)."""
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "DEPRECATED_KEY_ALIASES":
            lines.append(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr == "DEPRECATED_KEY_ALIASES":
            lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and any(
                a.name == "DEPRECATED_KEY_ALIASES" for a in node.names):
            lines.append(node.lineno)
    return lines


def test_the_alias_table_is_in_force() -> None:
    # With the window closed the two checks below are vacuous; say so.
    assert DEPRECATED_KEY_ALIASES, "no #1231 alias left: this guard checks nothing"


def test_no_script_but_the_registry_lib_spells_the_alias_table() -> None:
    found = {str(rel): lines for rel, tree in _scripts() if (lines := _alias_pairs_in(tree))}
    assert not found, (
        f"a copy of the #1231 alias table outside {_TABLE_HOME}: {found}. The spelling "
        "rule is Go's (#3556): ask `_lib_tenant_values.key_spellings` instead.")


def test_no_script_but_the_registry_lib_reads_the_alias_table() -> None:
    found = {str(rel): lines for rel, tree in _scripts() if (lines := _table_reads_in(tree))}
    assert not found, (
        f"DEPRECATED_KEY_ALIASES read outside {_TABLE_HOME}: {found}. Applying it to a key "
        "is Go's rule (#3556): ask `_lib_tenant_values.key_spellings` instead.")
