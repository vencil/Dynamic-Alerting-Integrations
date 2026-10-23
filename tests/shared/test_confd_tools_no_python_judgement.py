"""`_lib_confd` has no name rules left, and nothing uses one — an AST gate (#3206).

ADR-036 §7 step 9 makes "which paths a conf.d holds and what each one is"
Go's answer: the entries of `da-guard parse-tree` (role, class, problem,
`tenant_named`, `defaults_carriers`), read through `_lib_parse_tree`
(`Entry.role`, `.reserved`, `.cls`, `tenant_scope`, …), and
`_lib_parse_tree.classify_names` for a name that is not on a disk. Step 9b-3
deleted `_lib_confd`'s Python copy of Go's name rules (`DELETED`) together
with its parity tests. This gate checks two things, by name: `_lib_confd`
defines (or lists in `__all__`) none of them at top level, and no file under
`scripts/tools/` or `scripts/ops/` imports one from `_lib_confd` or uses one
through it (a call, or a reference handed on: `key=has_yaml_extension`) —
there is no allowlist any more. It is a narrow leftover check; the defence
against a Python copy of the rules is the behaviour matrix.

⛔ What the gate is NOT: a check that a tool does not judge conf.d paths in
Python some other way (a `stat`, a `startswith("_")`, an `is_file()`). A
textual check of those is a heuristic that a bypass always beats; whether a
tool's verdict agrees with Go's is a behaviour question, answered by the
behaviour matrix (`tests/shared/test_confd_behaviour_matrix.py`) — run as
nobody against a tree built for every (role, problem kind) Go can write.

⚠️ WHAT THE GATE CANNOT CATCH (it reads import statements and names, not
intent): `from _lib_confd import *`; the module bound another way —
`from tools import _lib_confd`, `from . import _lib_confd`, or
`import tools._lib_confd` without `as`; a name fetched with
`getattr(_lib_confd, "…")`; a name rule defined in some other module; and a
rule re-written under a new name. The first three can only reach a name
`_lib_confd` defines, which `test_lib_confd_defines_no_name_rule` keeps out
(so they fail when run, not here); the rest are review's, and the behaviour
matrix's, to catch.

`configmap_key_problem` is not in `DELETED` on purpose: it is Kubernetes'
rule for a ConfigMap key (no `..` prefix, a key's character set), not the
walk's name rule. `LEGACY_STATE_FILES` is not either: it names the files
tenant-api used to write (domain data), and `legacy_state_files` answers
from Go's `role`.
"""
from __future__ import annotations

import ast
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from _confd_population import parse  # noqa: E402

REPO = HERE.parents[1]
SCRIPTS = REPO / "scripts"
ROOTS = (SCRIPTS / "tools", SCRIPTS / "ops")
LIB = SCRIPTS / "tools" / "_lib_confd.py"

#: `_lib_confd`'s name rules — the Python copy of Go's file-name rules
#: (`pkg/confdname`) that step 9b-3 deleted.
DELETED = frozenset({
    "is_reserved_name", "is_hidden_name", "has_yaml_extension", "is_defaults_name",
    "is_defaults_document_name", "select_defaults_carrier", "readable_carriers",
    "config_stem", "is_dir_symlink", "CONFIG_SUFFIXES", "declared_tenant_ids",
    "_is_hidden", "_is_config",
})
NAME_RULES = DELETED


def uses(tree: ast.AST) -> "tuple[set[str], set[str]]":
    """({`<function>:<name>` of every function holding a use of a name rule
    — a call, a reference, `_lib_confd.<name>`}, {names imported from
    `_lib_confd`})."""
    imported: "dict[str, str]" = {}   # local name -> rule
    modules: "set[str]" = set()       # local names of the `_lib_confd` module
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] == "_lib_confd":
            for a in node.names:
                if a.name in NAME_RULES:
                    imported[a.asname or a.name] = a.name
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[-1] == "_lib_confd":
                    modules.add(a.asname or a.name)
    out: "set[str]" = set()

    def visit(node: ast.AST, scope: "list[str]") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, [*scope, child.name])
                continue
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                continue
            rule = None
            if isinstance(child, ast.Name) and child.id in imported:
                rule = imported[child.id]
            elif (isinstance(child, ast.Attribute) and child.attr in NAME_RULES
                  and isinstance(child.value, ast.Name) and child.value.id in modules):
                rule = child.attr
            if rule is not None:
                out.add(f"{'.'.join(scope) or '<module>'}:{rule}")
                continue
            visit(child, scope)
    visit(tree, [])
    return out, set(imported.values())


def _files() -> "list[pathlib.Path]":
    return sorted((p for root in ROOTS for p in root.rglob("*.py") if p != LIB),
                  key=lambda p: p.as_posix())


def _actual() -> "tuple[set[str], set[str]]":
    """(every use key, every `<file>:<import>:<name>` import)."""
    found, imports = set(), set()
    for p in _files():
        tree = parse(p)
        assert tree is not None, f"{p}: does not parse — the gate cannot read it"
        rel = p.relative_to(SCRIPTS).as_posix()
        u, imp = uses(tree)
        found |= {f"{rel}:{k}" for k in u}
        imports |= {f"{rel}:<import>:{n}" for n in imp}
    return found, imports


# ── the gate ─────────────────────────────────────────────────────────

def test_the_files_are_read() -> None:
    """Tripwire: the gate is only as wide as the files it walks."""
    names = {p.relative_to(SCRIPTS).as_posix() for p in _files()}
    for must in ("tools/ops/validate_config.py", "tools/ops/deprecate_rule.py",
                 "tools/ops/init_project.py", "ops/configmap_assemble.py",
                 "tools/ops/_gsvc_child.py"):
        assert must in names, must
    assert "tools/_lib_confd.py" not in names


def test_lib_confd_defines_no_name_rule() -> None:
    """None of `DELETED` is defined (or assigned) at `_lib_confd`'s top level,
    nor named in its `__all__`."""
    tree = parse(LIB)
    assert tree is not None
    defined = set()
    exported: "set[str]" = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    defined.add(t.id)
                    if t.id == "__all__" and isinstance(node.value, (ast.List, ast.Tuple)):
                        exported |= {e.value for e in node.value.elts
                                     if isinstance(e, ast.Constant)}
    assert exported, "`__all__` not found — the gate cannot read it"
    back = sorted((defined | exported) & DELETED)
    assert not back, f"`_lib_confd` defines a deleted name rule again: {back}"


def test_no_name_rule_is_used() -> None:
    found, imports = _actual()
    assert not found and not imports, (
        "a deleted `_lib_confd` name rule is used:\n"
        + "".join(f"  USE    {k}\n" for k in sorted(found))
        + "".join(f"  IMPORT {k}\n" for k in sorted(imports))
        + "Read Go's entry instead (`_lib_parse_tree.Entry`: `.role`, `.cls`, `.reserved`, "
          "`.tenant_named`, `.not_read`; `tenant_scope`; `classify_names` for names not on a disk).")


# ── the detector (synthetic, no repo file) ───────────────────────────

@pytest.mark.parametrize("src, want", [
    ("from _lib_confd import is_reserved_name\ndef f(n):\n    return is_reserved_name(n)\n",
     {"f:is_reserved_name"}),
    ("from _lib_confd import is_reserved_name as r\ndef f(n):\n    return r(n)\n",
     {"f:is_reserved_name"}),
    ("import _lib_confd\ndef f(n):\n    return _lib_confd.has_yaml_extension(n)\n",
     {"f:has_yaml_extension"}),
    ("import _lib_confd as lc\ndef f(ns):\n    return sorted(ns, key=lc.config_stem)\n",
     {"f:config_stem"}),
    ("from _lib_confd import CONFIG_SUFFIXES\nX = tuple(CONFIG_SUFFIXES)\n",
     {"<module>:CONFIG_SUFFIXES"}),
    ("from _lib_confd import is_defaults_name\nclass C:\n    def m(self, n):\n"
     "        return list(filter(is_defaults_name, n))\n", {"C.m:is_defaults_name"}),
])
def test_each_use_is_caught(src, want) -> None:
    assert set(uses(ast.parse(src))[0]) == want


@pytest.mark.parametrize("src", [
    "def f(e):\n    return e.reserved or e.cls == 'defaults'\n",
    "def is_reserved_name(n):\n    return n\n",          # a local of the same name
    "from _lib_parse_tree import classify_names\n",
    "from _lib_confd import printable_name\ndef f(n):\n    return printable_name(n)\n",
])
def test_reading_the_entry_is_not_flagged(src) -> None:
    assert uses(ast.parse(src))[0] == set()


# ── the tenant set is one answer (#3206 M-A) ─────────────────────────

#: Who may take the tenant files WITHOUT the entries that leave the set
#: unknown (`_lib_parse_tree.TenantScope`), and why. Every other reader of
#: "which tenants does this tree declare" takes `tenant_scope` /
#: `declared_tenants` (both halves).
HALF_SCOPE_CALLERS = {
    "tools/ops/patch_config.py": "a ConfigMap's keys: no directory, link or special file can exist",
    "tools/ops/gitops_check.py": "reads every selected entry itself and names each it cannot read",
    "tools/lint/check_path_metadata_consistency.py": "names every unreadable entry from the same "
                                                     "document itself (with a `_` rule of its "
                                                     "own — ADR-036 step 9, not this gate's)",
}


def test_only_the_named_readers_take_half_the_tenant_scope() -> None:
    callers: "dict[str, set[str]]" = {}
    for p in sorted(p for root in ROOTS for p in root.rglob("*.py")):
        tree = parse(p)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name in ("tenant_files", "tenant_file_paths"):
                    callers.setdefault(name, set()).add(p.relative_to(SCRIPTS).as_posix())
    assert callers.get("tenant_files", set()) == set(HALF_SCOPE_CALLERS), callers
    assert callers.get("tenant_file_paths", set()) == {"tools/_lib_parse_tree.py",
                                                        "tools/_lib_parse_cache.py"}, callers


# ── who may read a tree Go names paths on (#3206) ────────────────────

#: The runs whose verdict on the tree IS their output, so the reader's
#: fail-closed exit (`_lib_parse_cache.ConfdReader.tree`) stands aside for
#: them (`_lib_parse_cache.judging`). Each names every such path as its own
#: finding and fails — the behaviour matrix holds that.
JUDGES = {
    "tools/ops/validate_config.py": "the validator: a path Go names is its finding (exit 1)",
    "tools/ops/generate_alertmanager_routes.py": "the route generator refuses such a tree itself",
    "tools/lint/check_routing_profiles.py": "runs the route generator; its refusal is the finding",
    "tools/ops/config_diff.py": "names a PR side it cannot read (exit 2) and leaves a broken "
                                "base side uncompared (exit 1), so a repairing PR is not blocked",
    "tools/lint/check_confd_schema.py": "the schema lint: names every entry it cannot read after "
                                        "the violations of the rest of the tree, exit 2",
}


def test_only_the_judges_enter_judging() -> None:
    """A tool that enters `judging()` reads a tree Go names paths on without
    the shared refusal: exactly the files in `JUDGES` may, both ways."""
    callers = set()
    for p in sorted(p for root in ROOTS for p in root.rglob("*.py")):
        tree = parse(p)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name == "judging":
                    callers.add(p.relative_to(SCRIPTS).as_posix())
    assert callers == set(JUDGES), callers
