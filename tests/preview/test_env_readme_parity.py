"""recipe-preview README env table ↔ app.py parity.

The README's ``## 設定（env）`` table is what an operator reads to configure the
service; ``app.py`` is what actually reads the environment. Before this test the
table had drifted (seven variables the code reads were missing, one default was
written as ``false`` where the code's default is the empty string), and nothing
noticed.

Both directions are checked:

* every variable ``app.py`` reads appears in the table (or in
  ``_NOT_OPERATOR_CONFIG`` with a reason), and every table row is still read;
* every table default equals the default the code falls back to.

Defaults are derived from the AST, never by importing ``app.py`` (importing
it runs the dev-bypass containment check and resolves the eval core).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from _pysource import parse_py

_REPO = Path(__file__).resolve().parents[2]
_APP = _REPO / "components" / "recipe-preview" / "app.py"
_README = _REPO / "components" / "recipe-preview" / "README.md"

# Read by app.py but not something an operator sets — kept out of the table.
_NOT_OPERATOR_CONFIG = {
    "KUBERNETES_SERVICE_HOST": "injected by Kubernetes into every pod; app.py only reads it to "
    "detect the cluster for the dev-bypass containment check (documented in that row)",
}

_EMPTY = "(空)"

# Sentinel for "the code passes a default we cannot evaluate statically".
_UNDERIVABLE = object()


def _const_eval(node: ast.AST):
    """Evaluate the small expression subset app.py uses for defaults."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Add)):
        left, right = _const_eval(node.left), _const_eval(node.right)
        if left is _UNDERIVABLE or right is _UNDERIVABLE:
            return _UNDERIVABLE
        return left * right if isinstance(node.op, ast.Mult) else left + right
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "str" and len(node.args) == 1 and not node.keywords):
        inner = _const_eval(node.args[0])
        return _UNDERIVABLE if inner is _UNDERIVABLE else str(inner)
    return _UNDERIVABLE


def _is_os_environ(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "environ"
            and isinstance(node.value, ast.Name) and node.value.id == "os")


def _code_env_reads() -> tuple[dict[str, object], list[str]]:
    """Map env name → default (None = no default) for every read in app.py.

    Any env access whose shape we do not recognise is returned as an offender
    rather than skipped, so a new read style cannot slip past the table.
    """
    reads: dict[str, object] = {}
    offenders: list[str] = []
    tree = parse_py(_APP)
    handled: set[int] = set()  # id() of os.environ / os.getenv nodes inside a recognised read
    found: list[tuple[int, ast.AST | None, ast.AST | None]] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and _is_os_environ(node.func.value)):
            handled.add(id(node.func.value))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "getenv" and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"):
            handled.add(id(node.func))
        elif isinstance(node, ast.Subscript) and _is_os_environ(node.value):
            handled.add(id(node.value))
            found.append((node.lineno, node.slice, None))
            continue
        else:
            continue
        found.append((node.lineno, node.args[0] if node.args else None,
                      node.args[1] if len(node.args) > 1 else None))
    for node in ast.walk(tree):
        bare = (_is_os_environ(node)
                or (isinstance(node, ast.Attribute) and node.attr == "getenv"
                    and isinstance(node.value, ast.Name) and node.value.id == "os")
                or (isinstance(node, ast.Name) and node.id in {"environ", "getenv"}))
        if bare and id(node) not in handled:
            offenders.append(f"line {node.lineno}: unrecognised env access {ast.unparse(node)}")
    for lineno, name_node, default_node in found:
        if not (isinstance(name_node, ast.Constant) and isinstance(name_node.value, str)):
            offenders.append(f"line {lineno}: env name is not a string literal")
            continue
        default = None if default_node is None else _const_eval(default_node)
        if default is _UNDERIVABLE:
            offenders.append(f"line {lineno}: cannot derive default of {name_node.value}")
            continue
        prev = reads.setdefault(name_node.value, default)
        if prev != default:
            offenders.append(f"{name_node.value}: read with two defaults {prev!r} / {default!r}")
    return reads, offenders


def _readme_rows() -> dict[str, str]:
    """Map env name → default cell of the README's ``## 設定（env）`` table."""
    text = _README.read_text(encoding="utf-8")
    section = re.search(r"^## 設定（env）\n(.*?)(?=^## )", text, re.MULTILINE | re.DOTALL)
    assert section, "README has no '## 設定（env）' section"
    rows: dict[str, str] = {}
    for line in section.group(1).splitlines():
        m = re.match(r"^\| `([A-Z][A-Z0-9_]*)` \| ([^|]*?) \|", line)
        if not m:
            continue
        name, cell = m.groups()
        assert name not in rows, f"README lists {name} twice"
        rows[name] = cell
    assert rows, "README env table has no rows"
    return rows


def _cell(default) -> str:
    return _EMPTY if default in (None, "") else f"`{default}`"


def test_env_reads_are_all_recognised():
    _, offenders = _code_env_reads()
    assert not offenders, "\n".join(offenders)


def test_readme_lists_exactly_the_vars_app_reads():
    reads, _ = _code_env_reads()
    code_names = set(reads) - set(_NOT_OPERATOR_CONFIG)
    readme_names = set(_readme_rows())
    assert not (code_names - readme_names), f"read by app.py, missing from README: {sorted(code_names - readme_names)}"
    assert not (readme_names - code_names), f"in README, not read by app.py: {sorted(readme_names - code_names)}"


def test_not_operator_config_entries_are_still_read():
    reads, _ = _code_env_reads()
    stale = set(_NOT_OPERATOR_CONFIG) - set(reads)
    assert not stale, f"_NOT_OPERATOR_CONFIG names no longer read by app.py: {sorted(stale)}"


def test_readme_defaults_match_code():
    reads, _ = _code_env_reads()
    rows = _readme_rows()
    wrong = {
        name: (rows[name], _cell(reads[name]))
        for name in rows.keys() & reads.keys()
        if rows[name] != _cell(reads[name])
    }
    assert not wrong, "README default ≠ code default (readme, code): " + repr(wrong)
