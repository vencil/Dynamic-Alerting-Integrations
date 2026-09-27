"""A conf.d file read in a loop over the conf.d must be read strictly (#2123).

The exporter rejects a file that writes one key twice in one mapping;
`yaml.safe_load` / `_lib_io.load_yaml_file` keep the last value silently. A
tool that walks a conf.d and parses each file with those gives a verdict on
a file the exporter never serves. #2123 moved the judging tools onto
`_lib_io.strict_safe_load` / `strict_safe_load_all` / `load_yaml_file_strict`;
this gate keeps a new reader from quietly going back.

Derivation, not a list of tools (#2033 R′ rejected enumeration):

* **Population** — `_confd_population.population_keys`, the ONE answer the
  repo already has to "which scripts/tools files read a tenant conf.d"
  (shared with the enumeration and case-parity gates).
* **Predicate** — inside such a file, a lenient constructing reader
  (`_LENIENT`) called in the body of a loop whose iterable enumerates a
  directory — by the enumeration contract's own classifier
  (`test_confd_enumeration_contract._call_kind`), or by a function that
  itself contains such an enumeration (derived from the `_lib_*.py` modules
  and the file itself), or by a local name whose assignment reaches one
  through any chain of assignments in the same function (a fixpoint:
  `entries = sorted(d.iterdir())` → `paths = [p for p in entries …]` taints
  both) — or in a module-local function that loop body calls (one hop).
* **Residual** — `LENIENT_BY_DESIGN`: the files the predicate flags that
  are not judging a tenant conf.d, each with the reason. Two-way equality,
  like `FLAT_OUTSIDE_POPULATION`: a new flagged file is red until a human
  decides (migrate it, or pin it with the reason), and a pinned file that
  stops being flagged is red until its row is deleted.

⚠️ NOT GUARDED (stated so nobody reads the green as more than it is):
a parse reached through two or more calls from the loop; a file list built
in one function and parsed in another it is passed to (`describe_tenant`'s
`_iter_confd_yaml(entries, …)` — migrated, but by hand, not because this
gate would see it); a name tainted by augmented / tuple / walrus assignment
or by a `for` target (only plain `name = …` propagates); a single
`_defaults.yaml` read outside any loop (`gitops_check`, `policy_engine`,
`policy_opa_bridge` — migrated by hand too); readers outside the
population. And spellings `_is_lenient` does not recognise at all, so a
lenient read written this way is invisible even inside a conf.d loop:
`from yaml import safe_load` (bare name), `import yaml as y` (`y.safe_load`),
a local alias (`ld = yaml.safe_load; ld(f)`), and passing the reader as a
value instead of calling it (`map(yaml.safe_load, …)`,
`map(load_yaml_file, …)`). Those tools' behaviour is pinned by
`test_duplicate_key_rejected_across_tools.py` instead.
"""
from __future__ import annotations

import ast
import functools
import pathlib

import pytest

from _confd_population import parse, population_keys
from test_confd_enumeration_contract import _call_kind, _own_scope_calls

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLS = REPO / "scripts" / "tools"

# PyYAML's constructing entry points (a `yaml.` attribute), and the shared
# lenient file reader under any spelling. `compose` / `compose_all` are NOT
# here: a node tree keeps both copies of a repeated key, so nothing is
# silently lost — the tools that compose check the tree with the shared
# `find_duplicate_key` / `duplicate_in_mapping`.
_YAML_LENIENT = {"safe_load", "safe_load_all", "load", "load_all", "full_load",
                 "unsafe_load", "full_load_all", "unsafe_load_all"}
_LENIENT_ANY = {"load_yaml_file"}

LENIENT_BY_DESIGN: dict[str, str] = {
    "_lib_confd.py":
        "declared_tenant_ids: documented to declare a file's tenants even when the "
        "exporter's decode rejects that file (its docstring); an existence lookup, not a verdict",
    "dx/generate_tenant_metadata.py":
        "generator of portal metadata, gives no pass/fail on the config; "
        "⚠️ a repeated key reads last-wins here (not migrated in #2123)",
    "dx/migrate_conf_d.py":
        "migration generator (flat → hierarchical layout), not a verdict",
    "lint/_rule_tree.py":
        "reads rule-pack trees, not a tenant conf.d",
    "lint/check_k8s_manifests.py":
        "reads Kubernetes manifests, not a tenant conf.d",
    "lint/check_md_yaml_drift.py":
        "reads YAML fences / CRDs quoted in docs, not a tenant conf.d",
    "lint/check_path_metadata_consistency.py":
        "warning-only (always exits 0) path-vs-_metadata advisory; the verdict on "
        "whether the file is valid YAML is check_confd_schema's (strict)",
    "lint/check_retire_drift.py":
        "reads the repo's own conf.d for db_type; skips what it cannot parse — the "
        "YAML verdict on those trees is check_confd_schema's (strict, same pre-commit run)",
    "lint/check_threshold_unit_sanity.py":
        "\"unparseable YAML is another gate's job\" (its own comment) — that gate is "
        "check_confd_schema (strict)",
    "ops/backtest_threshold.py":
        "replays threshold changes against Prometheus; the verdict is about alert "
        "behaviour, not the file. ⚠️ a repeated key reads last-wins here (not migrated in #2123)",
    "ops/deprecate_rule.py":
        "its verdict on a repeated key is exporter_verdicts' (compose + the shared "
        "duplicate_in_mapping): such a tenant file is named and never rewritten — "
        "tests/ops/test_deprecate_rule_carriers.py",
    "ops/diagnose.py":
        "runtime diagnostic lookup of one tenant's profile, no verdict about the file",
    "ops/generate_tenant_mapping_rules.py":
        "generator of mapping rules from tenant ids, not a verdict",
    "ops/migrate_to_operator.py":
        "parse_configmap_rules reads rule files for CRD generation, not a tenant conf.d",
}


def _name(call: ast.Call) -> str:
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")


def _is_lenient(call: ast.Call) -> bool:
    name = _name(call)
    if name in _LENIENT_ANY:
        return True
    f = call.func
    return (name in _YAML_LENIENT and isinstance(f, ast.Attribute)
            and isinstance(f.value, ast.Name) and f.value.id == "yaml")


def _enumerating_functions(tree: ast.AST) -> set[str]:
    return {fn.name for fn in ast.walk(tree)
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(_call_kind(c) in ("flat", "recursive") for c in _own_scope_calls(fn))}


@functools.lru_cache(maxsize=None)
def _lib_enumerators() -> frozenset[str]:
    out: set[str] = set()
    for lib in TOOLS.glob("_lib_*.py"):
        tree = parse(lib)
        if tree is not None:
            out |= {n for n in _enumerating_functions(tree) if not n.startswith("_")}
    return frozenset(out)


def _enumerates(expr: ast.AST, enums: set[str], tainted: set[str]) -> bool:
    for n in ast.walk(expr):
        if isinstance(n, ast.Call) and (
                _call_kind(n) in ("flat", "recursive") or _name(n) in enums):
            return True
        if isinstance(n, ast.Name) and n.id in tainted:
            return True
    return False


def lenient_reads_in_confd_loops(tree: ast.AST, lib_enums: frozenset[str]) -> list[str]:
    """`function[->callee]:line` for every lenient read the predicate sees."""
    funcs = {fn.name: fn for fn in ast.walk(tree)
             if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))}
    enums = set(lib_enums) | _enumerating_functions(tree)
    hits: set[str] = set()
    for fn in funcs.values():
        # Fixpoint: `entries = sorted(d.iterdir())` then
        # `paths = [p for p in entries if …]` taints both names — one pass
        # saw only the first, and offboard_tenant's loop over the second
        # slipped by (#2123 round-2 blind review).
        assigns = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)]
        tainted: set[str] = set()
        while True:
            grown = tainted | {t.id for n in assigns
                               if _enumerates(n.value, enums, tainted)
                               for t in n.targets if isinstance(t, ast.Name)}
            if grown == tainted:
                break
            tainted = grown
        for loop in ast.walk(fn):
            if isinstance(loop, (ast.For, ast.AsyncFor)):
                if not _enumerates(loop.iter, enums, tainted):
                    continue
                bodies = loop.body
            elif isinstance(loop, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                if not any(_enumerates(g.iter, enums, tainted) for g in loop.generators):
                    continue
                bodies = [loop]
            else:
                continue
            for stmt in bodies:
                for c in ast.walk(stmt):
                    if not isinstance(c, ast.Call):
                        continue
                    if _is_lenient(c):
                        hits.add(f"{fn.name}:{c.lineno}")
                    callee = funcs.get(_name(c))
                    if callee is not None and callee is not fn:
                        hits |= {f"{fn.name}->{callee.name}:{x.lineno}"
                                 for x in ast.walk(callee)
                                 if isinstance(x, ast.Call) and _is_lenient(x)}
    return sorted(hits)


def _flagged() -> dict[str, list[str]]:
    lib_enums = _lib_enumerators()
    out = {}
    for p in sorted(TOOLS.rglob("*.py")):
        if not population_keys(p):
            continue
        tree = parse(p)
        if tree is None:
            continue
        hits = lenient_reads_in_confd_loops(tree, lib_enums)
        if hits:
            out[p.relative_to(TOOLS).as_posix()] = hits
    return out


def test_the_enumerator_derivation_is_alive():
    """Anti-vacuity: if the derivation stops finding the shared enumerators,
    every loop looks like a non-conf.d loop and the gate goes quiet."""
    assert {"iter_config_files", "iter_yaml_files", "list_config_tree"} <= _lib_enumerators()


def test_every_lenient_confd_reader_is_migrated_or_pinned():
    flagged = _flagged()
    actual, pinned = set(flagged), set(LENIENT_BY_DESIGN)
    assert actual == pinned, (
        "lenient YAML reads inside conf.d loops changed (#2123).\n"
        f"  new (read strictly, or pin with the reason): "
        f"{ {k: flagged[k] for k in sorted(actual - pinned)} }\n"
        f"  stale pin (no longer flagged — delete the row): {sorted(pinned - actual)}\n"
        "A tool that gives a pass/fail verdict on a tenant conf.d reads it with "
        "`_lib_io.strict_safe_load` / `strict_safe_load_all` / `load_yaml_file_strict`; "
        "a key written twice is then a YAMLError on the tool's existing syntax-error path.")


# ── Synthetic controls: the predicate catches the shapes, and only them ──

_CAUGHT = {
    "safe_load_in_iter_config_files_loop": (
        "import yaml\nfrom _lib_confd import iter_config_files\n"
        "def scan(d):\n    for p in iter_config_files(d):\n"
        "        with open(p) as f:\n            yaml.safe_load(f)\n"),
    "load_yaml_file_in_listdir_loop": (
        "import os\nfrom _lib_io import load_yaml_file\n"
        "def scan(d):\n    for n in os.listdir(d):\n        load_yaml_file(n)\n"),
    "helper_called_from_the_loop": (
        "import yaml\n"
        "def _load(p):\n    return yaml.safe_load(open(p))\n"
        "def scan(d):\n    for p in d.rglob('*.yaml'):\n        _load(p)\n"),
    "loop_over_a_name_bound_to_an_enumeration": (
        "import yaml\nfrom _lib_confd import list_config_tree\n"
        "def scan(d):\n    listed = list_config_tree(d).files\n"
        "    for p in listed:\n        yaml.safe_load_all(open(p))\n"),
    "loop_over_a_name_two_assignments_away": (
        "import yaml\n"
        "def scan(d):\n    entries = sorted(d.iterdir())\n"
        "    yaml_paths = [p for p in entries if p.is_file()]\n"
        "    for p in yaml_paths:\n        yaml.safe_load(open(p))\n"),
    "comprehension": (
        "import yaml, os\n"
        "def scan(d):\n    return [yaml.safe_load(open(n)) for n in os.listdir(d)]\n"),
}
_NOT_CAUGHT = {
    "strict_reader_in_the_loop": (
        "from _lib_io import strict_safe_load\nfrom _lib_confd import iter_config_files\n"
        "def scan(d):\n    for p in iter_config_files(d):\n        strict_safe_load(open(p))\n"),
    "lenient_read_outside_any_loop": (
        "import yaml\ndef read(p):\n    return yaml.safe_load(open(p))\n"),
    "json_load_in_the_loop": (
        "import json, os\n"
        "def scan(d):\n    for n in os.listdir(d):\n        json.load(open(n))\n"),
    "compose_in_the_loop": (
        "import yaml, os\n"
        "def scan(d):\n    for n in os.listdir(d):\n        yaml.compose(open(n))\n"),
}


@pytest.mark.parametrize("name", sorted(_CAUGHT))
def test_known_shapes_are_caught(name):
    assert lenient_reads_in_confd_loops(ast.parse(_CAUGHT[name]), _lib_enumerators())


@pytest.mark.parametrize("name", sorted(_NOT_CAUGHT))
def test_legitimate_shapes_are_not_flagged(name):
    assert lenient_reads_in_confd_loops(ast.parse(_NOT_CAUGHT[name]), _lib_enumerators()) == []
