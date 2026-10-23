"""Nothing outside `_lib_confd` uses its name predicates — an AST gate (#3206).

ADR-036 §7 step 9 makes "which paths a conf.d holds and what each one is"
Go's answer: the entries of `da-guard parse-tree` (role, class, problem),
read through `_lib_parse_tree` (`Entry.role`, `.reserved`, `.dir_link`,
`tenant_scope`, …). `_lib_confd`'s Python name rules — the second copy of
Go's that step 9b-3 deletes together with the parity matrix — must therefore
have no caller left by then. This gate counts down to that: outside
`_lib_confd.py`, no file under `scripts/` may import, call or hand on
(`key=has_yaml_extension`) one of `NAME_RULES`, except at an `ALLOWED`
site, keyed `<file>:<function>:<name>` (file relative to `scripts/`,
function the dotted enclosing `def` / `class` path, `<module>` at top
level) and tagged with why it is not migrated yet. An import is allowed
only in a file that has an allowed use of that name. The gate is exact both
ways: a new use is red, and so is an entry that matches nothing (delete it
— that site was migrated).

⛔ What the gate is NOT: a check that a tool does not judge conf.d paths in
Python some other way (a `stat`, a `startswith("_")`, an `is_file()`). A
textual check of those is a heuristic that a bypass always beats; whether a
tool's verdict agrees with Go's is a behaviour question, answered by the
behaviour matrix (#3206, the PR after this one) — run as nobody against a
tree built for every (role, problem kind) Go can write. Until then the
known places are named in ADR-036 §7 step 9's progress line.

⚠️ WHAT THE GATE CANNOT CATCH (it reads import statements and names, not
intent): `from _lib_confd import *`; the module bound another way —
`from tools import _lib_confd`, `from . import _lib_confd`, or
`import tools._lib_confd` without `as` (used as `tools._lib_confd.x`); the
function stored in a variable first (`f = _lib_confd.is_reserved_name` in
an allowed place, `f(n)` elsewhere is caught only as the first use); a name
fetched with `getattr(_lib_confd, "…")`; the private helpers
(`_is_hidden`, `_is_config`, …), which are not in `NAME_RULES`; a name
re-exported through another module, including an allowed one; and a second
call of the same name inside a function already allowed one (entries are
per function and name, not per call). Those are review's, and the behaviour
matrix's, to catch.

`configmap_key_problem` is not in `NAME_RULES` on purpose: it is
Kubernetes' rule for a ConfigMap key (no `..` prefix, a key's character
set), not the walk's name rule, and 9b-3 does not delete it.
"""
from __future__ import annotations

import ast
import pathlib
import re
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

#: `_lib_confd`'s name rules: the Python copy of Go's file-name rules
#: (`pkg/confdname`), which step 9b-3 deletes.
NAME_RULES = frozenset({
    "is_reserved_name", "is_hidden_name", "has_yaml_extension", "is_defaults_name",
    "is_defaults_document_name", "select_defaults_carrier", "config_stem",
    "is_dir_symlink", "CONFIG_SUFFIXES", "LEGACY_STATE_FILES", "legacy_state_files",
})


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


# ── why a site is not migrated yet ───────────────────────────────────

PR_BETA2 = "a conf.d reader not migrated in PR-β1: reads Go's entries in PR-β2 (#3206 step 9)"
ROUTE_GENERATOR = ("route generator: `_` / carrier test of a path the reader listed; Go's "
                   "class answers the same — threading Entry through its helpers is PR-β2")
CONFIGMAP = ("ConfigMaps (their keys, or dump files of them), not a conf.d walk; names mode "
             "(classify_names) answers a name — PR-β2")
AUDIT = ("migration_audit's \"today\" side IS the old Python selection, by design; it "
         "retires with the tool (ADR-036 §7)")
REASONS = frozenset({PR_BETA2, ROUTE_GENERATOR, CONFIGMAP, AUDIT})

ALLOWED = {
    "tools/dx/generate_tenant_metadata.py:_load_custom_groups:legacy_state_files": PR_BETA2,
    "tools/ops/_grar_parse.py:_apply_tenant_entries:is_reserved_name": ROUTE_GENERATOR,
    "tools/ops/_grar_parse.py:_parse_config_files:is_defaults_name": ROUTE_GENERATOR,
    "tools/ops/_grar_parse.py:_parse_config_files:is_reserved_name": ROUTE_GENERATOR,
    "tools/ops/_grar_parse.py:_parse_nested_config:is_reserved_name": ROUTE_GENERATOR,
    "tools/ops/_grar_parse.py:_parse_platform_config:is_defaults_name": ROUTE_GENERATOR,
    "tools/ops/assemble_config_dir.py:<module>:CONFIG_SUFFIXES": PR_BETA2,
    "tools/ops/assemble_config_dir.py:detect_conflicts:is_defaults_name": PR_BETA2,
    "tools/ops/assemble_config_dir.py:detect_conflicts:select_defaults_carrier": PR_BETA2,
    "tools/ops/assemble_config_dir.py:list_visible_config_entries:has_yaml_extension": PR_BETA2,
    "tools/ops/assemble_config_dir.py:list_visible_config_entries:is_hidden_name": PR_BETA2,
    "tools/ops/assemble_config_dir.py:main:is_defaults_name": PR_BETA2,
    "tools/ops/assemble_config_dir.py:main:select_defaults_carrier": PR_BETA2,
    "tools/ops/batch_diagnose.py:discover_tenants:config_stem": CONFIGMAP,
    "tools/ops/batch_diagnose.py:discover_tenants:has_yaml_extension": CONFIGMAP,
    "tools/ops/batch_diagnose.py:discover_tenants:is_reserved_name": CONFIGMAP,
    "tools/ops/deprecate_rule.py:_deprecate:is_defaults_name": PR_BETA2,
    "tools/ops/deprecate_rule.py:_nested_ignored:is_defaults_name": PR_BETA2,
    "tools/ops/deprecate_rule.py:section_owner:is_defaults_name": PR_BETA2,
    "tools/ops/init_project.py:_ConfdTree.root_defaults:is_defaults_name": PR_BETA2,
    "tools/ops/init_project.py:_kustomize_carrier_files:is_defaults_name": PR_BETA2,
    "tools/ops/migration_audit.py:read_tree_today:is_defaults_name": AUDIT,
    "tools/ops/migration_audit.py:read_tree_today:select_defaults_carrier": AUDIT,
    "tools/ops/offboard_tenant.py:_legacy_refs:LEGACY_STATE_FILES": PR_BETA2,
    "tools/ops/offboard_tenant.py:find_config_file:config_stem": PR_BETA2,
    "tools/ops/offboard_tenant.py:load_all_configs:LEGACY_STATE_FILES": PR_BETA2,
    "tools/ops/patch_config.py:exporter_reads:has_yaml_extension": CONFIGMAP,
    "tools/ops/patch_config.py:exporter_reads:is_hidden_name": CONFIGMAP,
    "tools/ops/patch_config.py:find_defaults_key:is_defaults_name": CONFIGMAP,
    "tools/ops/patch_config.py:is_carrier_candidate:has_yaml_extension": CONFIGMAP,
    "tools/ops/patch_config.py:is_carrier_candidate:is_hidden_name": CONFIGMAP,
    "tools/ops/patch_config.py:is_carrier_candidate:is_reserved_name": CONFIGMAP,
    "tools/ops/validate_config.py:check_tenant_file_name:config_stem": PR_BETA2,
}


# ── the gate ─────────────────────────────────────────────────────────

def test_the_files_are_read() -> None:
    """Tripwire: the gate is only as wide as the files it walks."""
    names = {p.relative_to(SCRIPTS).as_posix() for p in _files()}
    for must in ("tools/ops/validate_config.py", "tools/ops/deprecate_rule.py",
                 "tools/ops/init_project.py", "ops/configmap_assemble.py"):
        assert must in names, must
    assert "tools/_lib_confd.py" not in names


def test_no_name_rule_is_used_outside_the_allowlist() -> None:
    found, imports = _actual()
    new = sorted(found - set(ALLOWED))
    stale = sorted(set(ALLOWED) - found)
    allowed_names = {(k.split(":")[0], k.split(":")[2]) for k in ALLOWED}
    bare = sorted(i for i in imports if (i.split(":")[0], i.split(":")[2]) not in allowed_names)
    assert not new and not stale and not bare, (
        "`_lib_confd`'s name rules are used where the gate does not allow it.\n"
        + "".join(f"  NEW    {k}\n" for k in new)
        + "".join(f"  STALE  {k} — that site no longer uses it: delete the entry\n" for k in stale)
        + "".join(f"  IMPORT {k} — imported with no allowed use\n" for k in bare)
        + "Read Go's entry instead (`_lib_parse_tree.Entry`: `.role`, `.cls`, `.reserved`, "
          "`.dir_link`; `tenant_scope`; `classify_names` for names not on a disk).")


def test_every_entry_gives_one_of_the_reasons() -> None:
    assert all(v in REASONS for v in ALLOWED.values()), ALLOWED
    assert all(k.split(":")[2] in NAME_RULES for k in ALLOWED), ALLOWED
    assert all("\n" not in r and r.strip() for r in REASONS)


@pytest.mark.parametrize("adr", ["036-single-parser-effective-config.md",
                                 "036-single-parser-effective-config.en.md"])
def test_the_adr_names_every_module_still_on_the_name_rules(adr) -> None:
    """ADR-036 §7's progress line for step 9 names every module this
    allowlist holds: one it does not name is a caller nobody was told about.
    (One direction only: the line also names judgements this gate cannot
    see, which hold no entry here.)"""
    text = (REPO / "docs" / "adr" / adr).read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines()
                if "test_confd_tools_no_python_judgement.py" in ln
                and "test_confd_behaviour_matrix.py" in ln)
    want = {pathlib.PurePosixPath(k.split(":")[0]).stem for k in ALLOWED}
    named = set(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", line))
    missing = sorted(want - named)
    assert not missing, f"{adr}: modules on the name rules the ADR does not name: {missing}"


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
