"""The PyYAML rewrite layer is gone, and nothing reads conf.d through the
yaml.v3-imitating loader — an AST gate (ADR-036 §7 step 11, #3206).

ADR-036 makes Go the only parser and the only writer of conf.d: a tool reads
it from `da-guard parse-tree` (`_lib_parse_tree`) and writes it through
`da-guard write` (`_lib_confedit`). Step 10 moved the last conf.d writer to
`da-guard write`; step 11 deleted what the writers had read and dumped with:
`_lib_yaml_keys`' `RawPlain` / `KeepPlainScalarText` / `RewriteLoader` /
`RewriteDumper` / `load_for_rewrite` / `dump_for_rewrite`, `_lib_io`'s
`load_yaml_file_for_rewrite` / `StrictRewriteLoader` /
`strict_load_for_rewrite`, and `_lib_write_check`'s `NullText` /
`keep_null_spelling` (`REWRITE_LAYER`). Its last reader that was not a writer,
`da_assembler --render-cr` (the CR as written, to compare its ids with
da-crdecode's reading), now takes that comparison from da-crdecode
(`differs_from_text`); the last conf.d reader of the loader,
`check_md_yaml_drift` (the conf.d examples in docs/**/*.md), now reads them
through parse-tree.

Two checks, by name:

1. no Python file in the repo defines a `REWRITE_LAYER` name (a function, a
   class, or a module-level assignment), and no file under `scripts/` or
   `tests/` uses one — read from the AST, so an aliased import
   (`from _lib_yaml_keys import RawPlain as R`), a module alias
   (`import _lib_yaml_keys as k` … `k.load_for_rewrite`) or a constant
   `getattr(k, "dump_for_rewrite")` is caught under its canonical name;
2. the files under `scripts/` that use the yaml.v3-imitating loader
   (`LOADER`) are exactly `LOADER_READERS`, each reading YAML that is NOT
   conf.d — ADR-036 §12 moves each to its consumer's reading (step 12; the
   migration audit is deleted in step 13). A new reader turns this red.

⚠️ WHAT THE GATE CANNOT CATCH (it reads names, not intent): the layer
re-written under new names; a name built at run time
(`getattr(module, "load_for_" + "rewrite")`, `importlib`); a reader handed
the loader through a variable from a module outside `scripts/`. Whether a tool's reading of conf.d is yaml.v3's is a behaviour
question, answered by `tests/shared/test_yaml_v3_reading_across_tools.py`.
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

#: The deleted rewrite layer (step 11), with the private helpers that made it.
REWRITE_LAYER = frozenset({
    "RawPlain", "KeepPlainScalarText", "RewriteLoader", "RewriteDumper",
    "load_for_rewrite", "dump_for_rewrite", "load_yaml_file_for_rewrite",
    "StrictRewriteLoader", "strict_load_for_rewrite",
    "NullText", "keep_null_spelling", "_register_null_text", "_represent_null_text",
    "_represent_raw_plain", "_is_plain_as_written", "_RAW_PLAIN_TAGS",
})

#: ⚠️ TEMPORARY: the leftovers another change deletes (`_lib_write_check`'s
#: dead null-spelling helper, which registers itself on `RewriteDumper` at
#: import, and the one test of it). Matched EXACTLY, both ways: once that
#: change is merged these are gone, the tests below turn red, and this table
#: is to be emptied — it never grows.
PENDING_DEFINITIONS = {
    "scripts/tools/_lib_yaml_keys.py:RewriteDumper",
    "scripts/tools/_lib_write_check.py:NullText",
    "scripts/tools/_lib_write_check.py:_represent_null_text",
    "scripts/tools/_lib_write_check.py:_register_null_text",
    "scripts/tools/_lib_write_check.py:keep_null_spelling",
}
PENDING_USES = {
    "scripts/tools/_lib_write_check.py:RewriteDumper",
    "scripts/tools/_lib_write_check.py:NullText",
    "scripts/tools/_lib_write_check.py:_represent_null_text",
    "scripts/tools/_lib_write_check.py:_register_null_text",
    "tests/ops/test_lib_write_check.py:strict_load_for_rewrite",
    "tests/ops/test_lib_write_check.py:dump_for_rewrite",
    "tests/ops/test_lib_write_check.py:keep_null_spelling",
}
TESTS = REPO / "tests"
#: This gate names the layer in strings and synthetic sources only; it is
#: read like any other file (it must report nothing).

#: The yaml.v3-imitating loader that stays until step 12 (`_lib_yaml_keys`,
#: and `_lib_io`'s strict and file siblings).
LOADER = frozenset({
    "ExporterKeyLoader", "load_exporter_keys", "load_first_document_exporter_keys",
    "load_all_exporter_keys", "StrictExporterKeyLoader", "strict_load_exporter_keys",
    "load_yaml_file_exporter_keys",
})

#: Every file under `scripts/` that uses `LOADER`, and the YAML it reads —
#: none of it conf.d.
LOADER_READERS = {
    "tools/_lib_io.py": "the loader's strict and file siblings are defined here",
    "tools/ops/onboard_platform.py": "the Alertmanager config (its consumer: Alertmanager)",
    "tools/lint/check_retire_drift.py": "Namespace manifests (their consumer: kubectl)",
    "tools/_lib_tenant_state.py": "tenant-api's state store (its consumer: tenant-api)",
    "tools/ops/migration_audit.py": "reads a tree with both readers on purpose to list where "
                                    "they differ (ADR-036 §8); deleted in step 13",
}
#: Where the loader is defined (not a reader of it).
LOADER_HOME = "tools/_lib_yaml_keys.py"

_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".build", "site-packages",
                        ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache"})


def _repo_python() -> "list[pathlib.Path]":
    return sorted((p for p in REPO.rglob("*.py")
                   if not _SKIP_DIRS.intersection(p.relative_to(REPO).parts[:-1])),
                  key=lambda p: p.as_posix())


def _scripts_python() -> "list[pathlib.Path]":
    return sorted(SCRIPTS.rglob("*.py"), key=lambda p: p.as_posix())


def defines(tree: ast.AST, names: "frozenset[str]") -> "set[str]":
    """Every name of *names* this module defines: a function or class at any
    depth, or a module-level assignment."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.name in names:
            out.add(node.name)
    for node in getattr(tree, "body", []):
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        out |= {t.id for t in targets if isinstance(t, ast.Name) and t.id in names}
    return out


def uses(tree: ast.AST, names: "frozenset[str]") -> "set[str]":
    """Every name of *names* this module reaches, under whatever local name:
    imported (`from _lib_io import strict_load_for_rewrite as s`, then `s(…)`),
    referred to by its own name (a module using what it defines), reached as
    an attribute of any binding (`import _lib_yaml_keys as k`, then
    `k.load_for_rewrite`; `wc.keep_null_spelling`), or fetched by a constant
    string (`getattr(k, "dump_for_rewrite")`). Reported by the CANONICAL name,
    never the alias."""
    aliases: "dict[str, str]" = {}   # local binding -> canonical name
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name in names:
                    aliases[a.asname or a.name] = a.name
    out = set(aliases.values())
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in names:
                out.add(node.id)
            elif node.id in aliases:
                out.add(aliases[node.id])
        elif isinstance(node, ast.Attribute) and node.attr in names:
            out.add(node.attr)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id in ("getattr", "hasattr") and len(node.args) >= 2
              and isinstance(node.args[1], ast.Constant)
              and node.args[1].value in names):
            out.add(node.args[1].value)
    return out


def _read(p: pathlib.Path) -> ast.AST:
    tree = parse(p)
    assert tree is not None, f"{p}: does not parse — the gate cannot read it"
    return tree


# ── the gate ─────────────────────────────────────────────────────────

def test_the_files_are_read() -> None:
    """Tripwire: the gate is only as wide as the files it walks."""
    scripts = {p.relative_to(SCRIPTS).as_posix() for p in _scripts_python()}
    for must in ("tools/_lib_yaml_keys.py", "tools/_lib_io.py", "tools/_lib_write_check.py",
                 "tools/ops/da_assembler.py", "tools/lint/check_md_yaml_drift.py",
                 *LOADER_READERS):
        assert must in scripts, must
    repo = {p.relative_to(REPO).as_posix() for p in _repo_python()}
    assert "tests/ops/test_lib_write_check.py" in repo
    assert len(repo) > len(scripts)


def test_no_file_defines_the_rewrite_layer() -> None:
    found = {f"{p.relative_to(REPO).as_posix()}:{n}"
             for p in _repo_python() for n in defines(_read(p), REWRITE_LAYER)}
    assert found == PENDING_DEFINITIONS, (
        "the deleted PyYAML rewrite layer is defined (ADR-036 step 11):\n"
        + "".join(f"  NEW  {k}\n" for k in sorted(found - PENDING_DEFINITIONS))
        + "".join(f"  GONE {k} (drop it from PENDING_DEFINITIONS)\n"
                  for k in sorted(PENDING_DEFINITIONS - found)))


def _scripts_and_tests() -> "list[pathlib.Path]":
    return [p for p in _repo_python() if p.is_relative_to(SCRIPTS) or p.is_relative_to(TESTS)]


def test_nothing_in_scripts_or_tests_uses_the_rewrite_layer() -> None:
    files = _scripts_and_tests()
    assert any(p.is_relative_to(TESTS) for p in files) and any(
        p.is_relative_to(SCRIPTS) for p in files), "tripwire: both trees are read"
    found = {f"{p.relative_to(REPO).as_posix()}:{n}"
             for p in files for n in uses(_read(p), REWRITE_LAYER)}
    assert found == PENDING_USES, (
        "a script or test uses the deleted PyYAML rewrite layer:\n"
        + "".join(f"  NEW  {k}\n" for k in sorted(found - PENDING_USES))
        + "".join(f"  GONE {k} (drop it from PENDING_USES)\n"
                  for k in sorted(PENDING_USES - found))
        + "conf.d is read by `da-guard parse-tree` (`_lib_parse_tree`) and written by "
          "`da-guard write` (`_lib_confedit`); a CR's text is compared by da-crdecode.")


def test_the_loader_reads_no_conf_d() -> None:
    """Exactly `LOADER_READERS` use the yaml.v3-imitating loader, both ways."""
    readers = {p.relative_to(SCRIPTS).as_posix() for p in _scripts_python()
               if uses(_read(p), LOADER)} - {LOADER_HOME}
    assert readers == set(LOADER_READERS), (
        f"new: {sorted(readers - set(LOADER_READERS))}; "
        f"gone (drop its row): {sorted(set(LOADER_READERS) - readers)}. conf.d is read "
        "through `_lib_parse_tree`, never this loader (ADR-036 step 11).")


# ── the detector (synthetic, no repo file) ───────────────────────────

@pytest.mark.parametrize("src, want", [
    ("from _lib_io import strict_load_for_rewrite\n", {"strict_load_for_rewrite"}),
    ("import _lib_yaml_keys as k\nx = k.dump_for_rewrite({})\n", {"dump_for_rewrite"}),
    ("def f(d):\n    return keep_null_spelling(d, None)\n", {"keep_null_spelling"}),
    ("from _lib_yaml_keys import RawPlain as R\n", {"RawPlain"}),
    # aliased, then used only under the alias: reported by the canonical name
    ("from _lib_io import strict_load_for_rewrite as s\ndef f(t):\n    return s(t)\n",
     {"strict_load_for_rewrite"}),
    ("import _lib_yaml_keys as k\ndef f(t):\n    return k.load_for_rewrite(t)\n",
     {"load_for_rewrite"}),
    ("from tools import _lib_write_check as wc\nwc.keep_null_spelling({}, {})\n",
     {"keep_null_spelling"}),
    ("import _lib_yaml_keys as k\nf = getattr(k, 'dump_for_rewrite')\n", {"dump_for_rewrite"}),
])
def test_each_use_is_caught(src, want) -> None:
    assert uses(ast.parse(src), REWRITE_LAYER) == want


@pytest.mark.parametrize("src, want", [
    ("class RewriteDumper:\n    pass\n", {"RewriteDumper"}),
    ("def f():\n    def load_for_rewrite():\n        pass\n", {"load_for_rewrite"}),
    ("_RAW_PLAIN_TAGS = frozenset()\n", {"_RAW_PLAIN_TAGS"}),
    ("NullText: type = str\n", {"NullText"}),
])
def test_each_definition_is_caught(src, want) -> None:
    assert defines(ast.parse(src), REWRITE_LAYER) == want


@pytest.mark.parametrize("src", [
    '"""load_for_rewrite was deleted in step 11."""\n',      # prose only
    "x = 'RawPlain'\n",                                      # a string
    "from _lib_parse_tree import to_python\n",
    "from _lib_yaml_keys import load_exporter_keys as load_for_rewrite_old\n",  # alias of another
])
def test_prose_and_strings_are_not_flagged(src) -> None:
    tree = ast.parse(src)
    assert uses(tree, REWRITE_LAYER) == set() and defines(tree, REWRITE_LAYER) == set()
