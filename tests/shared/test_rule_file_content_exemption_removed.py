"""`rule_files_in`'s content exemption is gone and stays gone (#3426 round 4).

`rule_files_in(..., other_yaml=NO_RULE_FILES)` let a caller say "a YAML file
here that is neither a manifest nor a rule file holds no rule file" — a
judgement of the file's content. No content test told the files apart: on
`kind` alone a manifest without one went unread; with `apiVersion` too,
`helm/*/Chart.yaml` and Grafana provisioning files were refused; `grups:`
went unread either way. Which files are rule file sources is now declared by
path (`files_to_read` with `--exclude`, `rule_pack_files`), and every file
given is read or refused, named.

This is a deletion guard, not a detector of the pattern: it fails when one
of the removed names comes back anywhere under `scripts/tools/` — as an
identifier, an attribute, a keyword argument, a parameter, or a string (a
`getattr(cr, "NO_RULE_FILES")`). It does not see the exemption rebuilt under
other names.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "scripts" / "tools"

#: The names the removal took out (#3426 round 4): the parameter and its two
#: values. (`kinds`, removed with it, is too common a word to guard by name.)
REMOVED = {"other_yaml", "NO_RULE_FILES", "REFUSE"}


def _hits(tree: ast.AST) -> "list[tuple[int, str]]":
    out = []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.keyword):
            name = node.arg
        elif isinstance(node, ast.arg):
            name = node.arg
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            name = node.value
        elif isinstance(node, (ast.alias,)):
            name = node.asname or node.name
        if name in REMOVED:
            out.append((getattr(node, "lineno", 0), name))
    return out


def test_no_removed_name_under_scripts_tools() -> None:
    found = []
    files = sorted(TOOLS.rglob("*.py"))
    assert files, f"no Python file under {TOOLS}"
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found += [f"{path.relative_to(REPO)}:{line}: {name}" for line, name in _hits(tree)]
    assert not found, (
        "#3426: the content exemption of rule_files_in is removed — which files are "
        "rule file sources is declared by path (`_lib_consumer_read.files_to_read` with "
        "--exclude, `rule_pack_files`), never judged from content:\n" + "\n".join(found))


def test_the_detector_sees_each_spelling() -> None:
    """Each spelling it claims to see, seen (so a refactor of `_hits` that
    stops looking is red here, not silently green above)."""
    src = ("import _lib_consumer_read as cr\n"
           "cr.rule_files_in(p, other_yaml=cr.NO_RULE_FILES)\n"
           "def f(other_yaml=REFUSE): pass\n"
           "getattr(cr, 'NO_RULE_FILES')\n"
           "from _lib_consumer_read import REFUSE\n")
    names = sorted({n for _line, n in _hits(ast.parse(src))})
    assert names == ["NO_RULE_FILES", "REFUSE", "other_yaml"], names
