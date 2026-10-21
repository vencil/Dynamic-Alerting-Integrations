"""No conf.d tool judges a conf.d entry in Python — an AST gate (#3206).

ADR-036 §7 step 9 makes "which paths a conf.d holds and what each one is"
Go's answer: the entries of `da-guard parse-tree` (role, class, problem). A
Python reader that also asks the disk or a name — `is_file()`, a `_` /
`.` prefix, `has_yaml_extension` — holds a second verdict that can disagree
with Go's, and each disagreement found so far was a silent one (a file whose
stat is refused read as "absent", a `_arch/` directory Go could not list
skipped by its name). This is ADR-036 §12's "AST lint + an allowlist with a
reason per entry", applied to the conf.d readers ahead of step 12.

The population is the enumeration contract's (`_confd_population`) over
`scripts/tools` and `scripts/ops`: every file that binds a conf.d reader
library, declares a conf.d flag, or names a conf.d. In it, these are
forbidden:

* a `startswith` call any of whose string constants — positional, in a
  tuple / list / set, or a keyword value — begins with `_` or `.`
  (`startswith("_defaults")`, `startswith((".", "~"))`);
* the path methods `is_file`, `is_dir`, `exists`, `iterdir`, `glob`,
  `rglob`, `stat`, `lstat`, `is_symlink`, `walk` called on anything;
* `os.scandir`, `os.listdir`, `os.stat`, `os.lstat`, `os.walk`, `os.access`;
  `os.path.isfile`, `isdir`, `exists`, `islink`, `lexists`, `getsize` —
  through any alias of `os` / `os.path` or a name imported from them;
* any call into `fnmatch` (`fnmatch.fnmatch`, `fnmatch.filter`, … or a name
  imported from it);
* the shared name predicates `has_yaml_extension`, `is_reserved_name`,
  `is_hidden_name`, `is_dir_symlink` (by name or as an attribute);
* `getattr(x, "<any method or function name above>")`;
* any of those handed on uncalled: `filter(Path.is_file, …)`,
  `map(os.path.isdir, …)`, `key=has_yaml_extension` — a reference through
  `Path` / `PurePath` / `pathlib.Path`, `os`, `os.path`, `fnmatch`, or to a
  predicate or imported function name, anywhere but as the callee.

Each remaining one is an `ALLOWED` entry, keyed `<file>:<function>:<call>`
(file relative to `scripts/`, function the dotted enclosing `def` / `class`
path, `<module>` at top level; a reference is `<name>(ref)`), valued
`(reason, count)`. The gate is exact both ways AND by count: a new call
outside the list is red, so is a second call of an allowed kind in the same
function, and so is an entry whose count no longer matches (fix the count,
or delete the entry). An entry covers its call in that function only, so a
`startswith("_")` added to a function allowed an `exists()` is red too.

⛔ What a reason may say: the path is not a conf.d entry (the operand
directory itself, an output, a repo file), the call words a message only, it
is the shared predicate's own definition, or — `PR_BETA2` and its siblings
in `NOT_MIGRATED` — the judgement is real and not migrated yet. That last
list is the remaining Python judgement of conf.d (ADR-036 §7 step 9's
progress line names exactly it); it is meant to shrink to nothing. An entry
that judges a conf.d entry under any other reason is the bug this gate
exists to catch.

⚠️ THE GATE READS CALLS, NOT INTENT. It is a textual heuristic and does not
catch, among others: indexing or slicing a name (`name[0] == "_"`,
`name[:1] == "_"`), `endswith` / `.suffix` / `.name` comparisons
(`p.suffix in (".yaml", ".yml")`), `re.match` / `re.search` on a name, a
prefix held in a variable (`name.startswith(PREFIX)`), `in` / `==` against
a literal, `os.stat` reached through a helper the gate does not name,
`stat.S_ISREG` on a mode obtained elsewhere, a bound method stored before
use (`check = p.is_file; check()`), `getattr` with a computed name, and
anything in a module outside the population. Those are caught, where they
are, by behaviour tests (a tenant file Go classes `tenant` whose name a
Python rule would call platform — `test_confd_tools_read_go_role`), not by
this gate.
"""
from __future__ import annotations

import ast
import collections
import pathlib
import re
import sys
import textwrap

import pytest

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from _confd_population import parse, population_keys  # noqa: E402

REPO = HERE.parents[1]
SCRIPTS = REPO / "scripts"
ROOTS = (SCRIPTS / "tools", SCRIPTS / "ops")

PATH_METHODS = frozenset({"is_file", "is_dir", "exists", "iterdir", "glob", "rglob",
                          "stat", "lstat", "is_symlink", "walk"})
OS_FUNCS = frozenset({"scandir", "listdir", "stat", "lstat", "walk", "access"})
OS_PATH_FUNCS = frozenset({"isfile", "isdir", "exists", "islink", "lexists", "getsize"})
NAME_PREDICATES = frozenset({"has_yaml_extension", "is_reserved_name", "is_hidden_name",
                             "is_dir_symlink"})
#: Every name `getattr(x, "<name>")` may not fetch.
FORBIDDEN_NAMES = PATH_METHODS | OS_FUNCS | OS_PATH_FUNCS | NAME_PREDICATES
PREFIXES = ("_", ".")
PATH_CLASSES = frozenset({"Path", "PurePath", "PosixPath", "PurePosixPath"})


class _Aliases:
    """What the module's imports bind: names for `os`, `os.path`, `fnmatch`,
    `pathlib`, and {local name: kind} for functions imported from them."""

    def __init__(self, tree: ast.AST) -> None:
        self.os, self.os_path, self.fnmatch, self.pathlib = set(), set(), set(), set()
        self.modules: "set[str]" = set()  # every name an `import x` binds
        self.funcs: "dict[str, str]" = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    local = a.asname or a.name.split(".")[0]
                    self.modules.add(local)
                    if a.name == "os":
                        self.os.add(local)
                    elif a.name == "os.path":
                        (self.os_path if a.asname else self.os).add(local)
                    elif a.name == "fnmatch":
                        self.fnmatch.add(local)
                    elif a.name == "pathlib":
                        self.pathlib.add(local)
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    local = a.asname or a.name
                    if node.module == "os" and a.name == "path":
                        self.os_path.add(local)
                    elif node.module == "os" and a.name in OS_FUNCS:
                        self.funcs[local] = f"os.{a.name}"
                    elif node.module == "os.path" and a.name in OS_PATH_FUNCS:
                        self.funcs[local] = f"os.path.{a.name}"
                    elif node.module == "fnmatch":
                        self.funcs[local] = f"fnmatch.{a.name}"

    def _is(self, node: ast.AST, names: "set[str]") -> bool:
        return isinstance(node, ast.Name) and node.id in names

    def is_os_path(self, node: ast.AST) -> bool:
        return self._is(node, self.os_path) or (
            isinstance(node, ast.Attribute) and node.attr == "path"
            and self._is(node.value, self.os))

    def is_path_class(self, node: ast.AST) -> bool:
        return (isinstance(node, ast.Name) and node.id in PATH_CLASSES) or (
            isinstance(node, ast.Attribute) and node.attr in PATH_CLASSES
            and self._is(node.value, self.pathlib))

    def qualified(self, node: ast.Attribute) -> "str | None":
        """The forbidden kind a `<module or Path>.<name>` names, else None."""
        v, a = node.value, node.attr
        if self.is_os_path(v) and a in OS_PATH_FUNCS:
            return f"os.path.{a}"
        if self._is(v, self.os) and a in OS_FUNCS:
            return f"os.{a}"
        if self._is(v, self.fnmatch):
            return f"fnmatch.{a}"
        if self.is_path_class(v) and a in PATH_METHODS:
            return a
        return None


def _starts_with_prefix(call: ast.Call) -> bool:
    values = [*call.args, *(k.value for k in call.keywords)]
    items = []
    for v in values:
        items.extend(v.elts if isinstance(v, (ast.Tuple, ast.List, ast.Set)) else [v])
    return any(isinstance(i, ast.Constant) and isinstance(i.value, str)
               and i.value.startswith(PREFIXES) for i in items)


def _call_kind(call: ast.Call, al: _Aliases) -> "str | None":
    f = call.func
    if isinstance(f, ast.Name):
        if f.id in NAME_PREDICATES:
            return f.id
        if f.id == "getattr" and len(call.args) >= 2:
            n = call.args[1]
            if isinstance(n, ast.Constant) and n.value in FORBIDDEN_NAMES:
                return f"getattr.{n.value}"
            return None
        return al.funcs.get(f.id)
    if not isinstance(f, ast.Attribute):
        return None
    q = al.qualified(f)
    if q is not None:
        return q
    if al.is_os_path(f.value) or al._is(f.value, al.os):
        return None  # another os / os.path function (join, basename, …)
    if f.attr in NAME_PREDICATES:
        return f.attr
    if f.attr == "startswith":
        return "startswith" if _starts_with_prefix(call) else None
    if f.attr == "walk" and al._is(f.value, al.modules):
        return None  # `ast.walk` and the like: a module's function, not Path.walk
    if f.attr in PATH_METHODS:
        return f.attr
    return None


def _ref_kind(node: ast.AST, al: _Aliases) -> "str | None":
    """The forbidden kind a reference that is NOT a callee hands on."""
    if isinstance(node, ast.Attribute):
        q = al.qualified(node)
        if q is None and node.attr in NAME_PREDICATES:
            q = node.attr
        return None if q is None else f"{q}(ref)"
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        if node.id in NAME_PREDICATES:
            return f"{node.id}(ref)"
        if node.id in al.funcs:
            return f"{al.funcs[node.id]}(ref)"
    return None


def judgements(tree: ast.AST) -> "dict[str, int]":
    """`<function>:<call>` → how many forbidden calls (or references) of
    that kind the function holds."""
    al = _Aliases(tree)
    out: "collections.Counter[str]" = collections.Counter()

    def visit(node: ast.AST, scope: "list[str]") -> None:
        callee = node.func if isinstance(node, ast.Call) else None
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, [*scope, child.name])
                continue
            where = ".".join(scope) or "<module>"
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                continue  # binding a name is no use of it
            if isinstance(child, ast.Call):
                kind = _call_kind(child, al)
                if kind is not None:
                    out[f"{where}:{kind}"] += 1
            elif child is not callee:
                kind = _ref_kind(child, al)
                if kind is not None:
                    out[f"{where}:{kind}"] += 1
                    continue
            visit(child, scope)
    visit(tree, [])
    return dict(out)


def _population() -> "list[pathlib.Path]":
    return sorted((p for root in ROOTS for p in root.rglob("*.py") if population_keys(p)),
                  key=lambda p: p.as_posix())


def _actual() -> "dict[str, int]":
    found: "dict[str, int]" = {}
    for p in _population():
        tree = parse(p)
        assert tree is not None, f"{p}: does not parse — the gate cannot read it"
        rel = p.relative_to(SCRIPTS).as_posix()
        for key, n in judgements(tree).items():
            found[f"{rel}:{key}"] = n
    return found


# ── the allowlist ────────────────────────────────────────────────────

ROOT = "the conf.d operand itself (missing / not a directory), not an entry in it"
ROOT_DEFAULT = "which directory is conf.d when none is given (repo layout), not an entry in it"
ROOT_OUTPUT = "the operand or output directory itself, not an entry of a conf.d it lists"
OUTPUT = "a path this tool writes or stages (its output, a scratch copy), not a conf.d it reads"
OPERAND = "a file the operator names (--policy, --base-config, a draft, …), not a listed entry"
NOT_CONFD = "not a conf.d path: repo, docs, rule packs, schemas, binaries, history, sockets"
LOADER = ("a generic loader of a path its caller names (dictionary, policy, hints); "
          "conf.d files go through load_confd_file")
KEY = "a YAML key (the `_` reserved keys), not a file name"
PREDICATE = "the shared predicate's own definition, kept until step 9b-3 (with the parity matrix)"
WORDING = "words a WARN about hidden names, which no listing holds; selects nothing"
MEASURE = "measures the bytes of files the listing already selected; decides nothing about them"
K8S_KEY = "Kubernetes' own ConfigMap-key rule (a `..` prefix is illegal), not the walk's"
VALUE_PATTERN = "matches a glob pattern against a value (a domain, a label), not a path"
CONFIGMAP = ("a ConfigMap's keys, not a path on disk; names mode (classify_names) can "
             "answer them — PR-β2")
GIT = ("a git tree being materialised (no listing exists yet): the walk's hidden rule "
       "for irregular entries — PR-β2")
PR_BETA2 = "judges conf.d entries in Python; not migrated in PR-β1 — PR-β2 (#3206 step 9)"
ROUTE_GENERATOR = ("route generator: `_` test of a path the reader listed; Go's class "
                   "answers the same — threading Entry through its helpers is PR-β2")

ALLOWED = {
    "ops/configmap_assemble.py:main:exists": (OUTPUT, 1),
    "ops/configmap_assemble.py:main:is_dir": (ROOT, 1),
    "ops/configmap_assemble.py:measure_artifact:stat": (MEASURE, 1),
    "ops/go_test_reads.py:direct_reads:is_file": (NOT_CONFD, 1),
    "ops/go_test_reads.py:main:glob": (NOT_CONFD, 1),
    "ops/go_test_reads.py:main:is_dir": (NOT_CONFD, 1),
    "tools/_lib_confd.py:_is_hidden:startswith": (PREDICATE, 1),
    "tools/_lib_confd.py:_python_nested_files:is_dir": (PR_BETA2, 1),
    "tools/_lib_confd.py:_python_nested_files:is_file": (PR_BETA2, 1),
    "tools/_lib_confd.py:_python_nested_files:os.walk": (PR_BETA2, 1),
    "tools/_lib_confd.py:config_listing:is_dir": (ROOT, 1),
    "tools/_lib_confd.py:config_stem:is_hidden_name": (PREDICATE, 1),
    "tools/_lib_confd.py:config_stem:is_reserved_name": (PREDICATE, 1),
    "tools/_lib_confd.py:configmap_key_problem:startswith": (K8S_KEY, 1),
    "tools/_lib_confd.py:is_defaults_document_name:has_yaml_extension": (PREDICATE, 1),
    "tools/_lib_confd.py:is_defaults_document_name:startswith": (PREDICATE, 1),
    "tools/_lib_confd.py:is_dir_symlink:is_dir": (PREDICATE, 1),
    "tools/_lib_confd.py:is_dir_symlink:is_symlink": (PREDICATE, 1),
    "tools/_lib_confd.py:is_reserved_name:startswith": (PREDICATE, 1),
    "tools/_lib_confd.py:legacy_state_files:is_file": (PR_BETA2, 1),
    "tools/_lib_confd.py:overlay_platform_tenants:is_reserved_name": (ROUTE_GENERATOR, 1),
    "tools/_lib_io.py:find_metric_dictionary:os.path.isfile": (NOT_CONFD, 1),
    "tools/_lib_io.py:load_yaml_file:is_file": (LOADER, 1),
    "tools/_lib_io.py:load_yaml_file_exporter_keys:is_file": (LOADER, 1),
    "tools/_lib_io.py:load_yaml_file_strict:is_file": (LOADER, 1),
    "tools/_lib_io.py:read_onboard_hints:is_file": (LOADER, 1),
    "tools/_lib_io.py:write_json_secure:os.stat": (OUTPUT, 1),
    "tools/_lib_io.py:write_text_secure:os.stat": (OUTPUT, 1),
    "tools/_lib_key_verdict.py:_judge_key:startswith": (KEY, 6),
    "tools/_lib_parse_tree.py:<module>:is_dir": (NOT_CONFD, 1),
    "tools/_lib_tenant_values.py:<module>:is_dir": (NOT_CONFD, 1),
    "tools/_lib_tenant_values.py:_missing_binary_message:os.path.isfile": (NOT_CONFD, 1),
    "tools/_lib_tenant_values.py:canonical_key:startswith": (KEY, 1),
    "tools/_lib_tenant_values.py:value_not_served_as_written:startswith": (KEY, 1),
    "tools/_lib_tenant_values.py:written_config:startswith": (KEY, 1),
    "tools/_lib_write_check.py:_as_root:is_dir": (OUTPUT, 1),
    "tools/_lib_write_check.py:_is_empty_dir:is_dir": (OUTPUT, 1),
    "tools/_lib_write_check.py:_is_empty_dir:rglob": (OUTPUT, 1),
    "tools/_lib_write_check.py:_staging_parent:exists": (OUTPUT, 1),
    "tools/_lib_write_check.py:_staging_parent:is_dir": (OUTPUT, 1),
    "tools/_lib_write_check.py:confirm_tree:exists": (OUTPUT, 1),
    "tools/dx/compile_custom_alerts.py:_committed_rules:exists": (OUTPUT, 1),
    "tools/dx/compile_custom_alerts.py:main:exists": (ROOT, 1),
    "tools/dx/describe_tenant.py:main:exists": (ROOT, 1),
    "tools/dx/describe_tenant.py:main:glob": (ROOT_DEFAULT, 1),
    "tools/dx/describe_tenant.py:main:is_dir": (ROOT_DEFAULT, 2),
    "tools/dx/describe_tenant.py:main:is_file": (OPERAND, 2),
    "tools/dx/describe_tenant.py:what_if_result:is_file": (PR_BETA2, 1),
    "tools/dx/describe_tenant.py:what_if_result:os.path.lexists": (PR_BETA2, 1),
    "tools/dx/generate_platform_data.py:_chart_app_version:is_file": (NOT_CONFD, 1),
    "tools/dx/generate_platform_data.py:_load_tenant_metadata:exists": (NOT_CONFD, 1),
    "tools/dx/generate_platform_data.py:_load_tenant_metadata:is_dir": (ROOT, 1),
    "tools/dx/generate_platform_data.py:gather_rule_counts:glob": (NOT_CONFD, 2),
    "tools/dx/generate_platform_data.py:main:exists": (NOT_CONFD, 3),
    "tools/dx/generate_tenant_fixture.py:main:exists": (OUTPUT, 1),
    "tools/dx/generate_tenant_fixture.py:main:iterdir": (OUTPUT, 1),
    "tools/dx/generate_tenant_fixture.py:main:rglob": (OUTPUT, 2),
    "tools/dx/generate_tenant_fixture.py:main:stat": (OUTPUT, 1),
    "tools/dx/generate_tenant_metadata.py:count_metrics:startswith": (KEY, 1),
    "tools/dx/generate_tenant_metadata.py:infer_rule_packs:startswith": (KEY, 1),
    "tools/dx/generate_tenant_metadata.py:main:exists": (ROOT_OUTPUT, 3),
    "tools/dx/migrate_conf_d.py:main:exists": (ROOT, 1),
    "tools/dx/run_chaos_soak.py:main:exists": (ROOT, 1),
    "tools/dx/run_chaos_soak.py:trigger_reload:exists": (PR_BETA2, 1),
    "tools/dx/run_chaos_soak.py:trigger_reload:has_yaml_extension": (PR_BETA2, 1),
    "tools/dx/run_chaos_soak.py:trigger_reload:is_hidden_name": (PR_BETA2, 1),
    "tools/dx/run_chaos_soak.py:trigger_reload:is_reserved_name": (PR_BETA2, 1),
    "tools/dx/run_chaos_soak.py:trigger_reload:rglob": (PR_BETA2, 1),
    "tools/dx/sync_schema.py:is_routing_key:startswith": (KEY, 1),
    "tools/dx/sync_schema.py:load_registry:startswith": (KEY, 2),
    "tools/dx/sync_schema.py:run:exists": (NOT_CONFD, 1),
    "tools/dx/sync_schema.py:sync_underscore_members:startswith": (KEY, 2),
    "tools/dx/tenant_verify.py:main:is_dir": (ROOT, 1),
    "tools/lint/_rule_tree.py:_find_repo_root:exists": (NOT_CONFD, 1),
    "tools/lint/_rule_tree.py:_generated_pack_names:is_file": (NOT_CONFD, 1),
    "tools/lint/_rule_tree.py:_generated_pack_names:rglob": (NOT_CONFD, 1),
    "tools/lint/check_cli_contract.py:_unscanned_carriers:glob": (NOT_CONFD, 1),
    "tools/lint/check_cli_contract.py:doc_files:is_file": (NOT_CONFD, 2),
    "tools/lint/check_cli_contract.py:doc_files:is_symlink": (NOT_CONFD, 1),
    "tools/lint/check_cli_contract.py:doc_files:rglob": (NOT_CONFD, 1),
    "tools/lint/check_cli_contract.py:load_baseline:is_file": (NOT_CONFD, 1),
    "tools/lint/check_cli_contract.py:main:is_file": (NOT_CONFD, 1),
    "tools/lint/check_cli_contract.py:scan:is_dir": (NOT_CONFD, 1),
    "tools/lint/check_cli_default_drift.py:_resolve:is_file": (NOT_CONFD, 1),
    "tools/lint/check_cli_default_drift.py:_resolve:rglob": (NOT_CONFD, 1),
    "tools/lint/check_cli_default_drift.py:main:is_file": (NOT_CONFD, 1),
    "tools/lint/check_confd_schema.py:main:os.path.isdir": (ROOT, 1),
    "tools/lint/check_doc_datools_cmds.py:_doc_files:rglob": (NOT_CONFD, 1),
    "tools/lint/check_doc_datools_cmds.py:_is_bind_mount:startswith": (NOT_CONFD, 1),
    "tools/lint/check_doc_datools_cmds.py:pin_capability_doc_files:is_file": (NOT_CONFD, 1),
    "tools/lint/check_doc_datools_cmds.py:run:is_file": (NOT_CONFD, 1),
    "tools/lint/check_frontmatter_versions.py:fix_drift:exists": (NOT_CONFD, 1),
    "tools/lint/check_frontmatter_versions.py:main:is_dir": (NOT_CONFD, 1),
    "tools/lint/check_frontmatter_versions.py:main:os.access": (NOT_CONFD, 1),
    "tools/lint/check_frontmatter_versions.py:main:rglob": (NOT_CONFD, 1),
    "tools/lint/check_frontmatter_versions.py:scan_docs:exists": (NOT_CONFD, 1),
    "tools/lint/check_frontmatter_versions.py:scan_docs:is_hidden_name": (NOT_CONFD, 1),
    "tools/lint/check_frontmatter_versions.py:scan_docs:rglob": (NOT_CONFD, 1),
    "tools/lint/check_k8s_manifests.py:find_manifest_files:is_dir": (NOT_CONFD, 1),
    "tools/lint/check_k8s_manifests.py:find_manifest_files:rglob": (NOT_CONFD, 1),
    "tools/lint/check_k8s_manifests.py:kube_linter_lint_dir:exists": (NOT_CONFD, 2),
    "tools/lint/check_k8s_manifests.py:manifest_root_exists:is_dir": (NOT_CONFD, 1),
    "tools/lint/check_md_yaml_drift.py:MdYamlDriftChecker._load_schema:exists": (NOT_CONFD, 1),
    "tools/lint/check_md_yaml_drift.py:MdYamlDriftChecker._receiver_checks:startswith": (KEY, 1),
    "tools/lint/check_md_yaml_drift.py:MdYamlDriftChecker.iter_config_units:rglob": (NOT_CONFD, 1),
    "tools/lint/check_md_yaml_drift.py:MdYamlDriftChecker.run_crd:exists": (NOT_CONFD, 1),
    "tools/lint/check_md_yaml_drift.py:MdYamlDriftChecker.run_crd:rglob": (NOT_CONFD, 1),
    "tools/lint/check_md_yaml_drift.py:MdYamlDriftChecker.run_fences:rglob": (NOT_CONFD, 1),
    "tools/lint/check_md_yaml_drift.py:main:exists": (NOT_CONFD, 1),
    "tools/lint/check_path_metadata_consistency.py:_main:exists": (ROOT, 1),
    "tools/lint/check_path_metadata_consistency.py:_main:is_dir": (PR_BETA2, 1),
    "tools/lint/check_path_metadata_consistency.py:_main:is_symlink": (PR_BETA2, 1),
    "tools/lint/check_path_metadata_consistency.py:_main:startswith": (PR_BETA2, 1),
    "tools/lint/check_path_metadata_consistency.py:scan_file:is_file": (PR_BETA2, 1),
    "tools/lint/check_retire_drift.py:helm_declared_targets:glob": (NOT_CONFD, 1),
    "tools/lint/check_retire_drift.py:helm_declared_targets:is_dir": (NOT_CONFD, 1),
    "tools/lint/check_retire_drift.py:main:is_dir": (ROOT, 1),
    "tools/lint/check_retire_drift.py:namespace_declared_targets:is_file": (NOT_CONFD, 1),
    "tools/lint/check_routing_profiles.py:main:os.path.isdir": (ROOT, 1),
    "tools/lint/check_scrape_reachability.py:load_repo_services:rglob": (NOT_CONFD, 1),
    "tools/lint/check_threshold_reachability.py:_defaults_writer_census:rglob": (NOT_CONFD, 1),
    "tools/lint/check_threshold_reachability.py:_is_defaults_artifact:startswith": (NOT_CONFD, 1),
    "tools/lint/check_threshold_reachability.py:_tracked_defaults_artifacts:is_file":
        (NOT_CONFD, 1),
    "tools/lint/check_threshold_registry.py:run_check:glob": (NOT_CONFD, 1),
    "tools/lint/check_threshold_registry.py:run_check:os.path.isfile": (NOT_CONFD, 2),
    "tools/lint/check_threshold_unit_sanity.py:_collect:os.path.isfile": (NOT_CONFD, 3),
    "tools/lint/check_threshold_unit_sanity.py:_declared_units:os.path.isfile": (NOT_CONFD, 2),
    "tools/lint/check_threshold_unit_sanity.py:_iter_confd_docs:os.path.isdir": (ROOT, 1),
    "tools/lint/check_threshold_unit_sanity.py:_iter_doc_files:os.walk": (NOT_CONFD, 1),
    "tools/lint/check_threshold_unit_sanity.py:_iter_yaml_files:os.path.isfile": (NOT_CONFD, 1),
    "tools/ops/_grar_parse.py:_apply_tenant_entries:is_reserved_name": (ROUTE_GENERATOR, 1),
    "tools/ops/_grar_parse.py:_is_routing_key:startswith": (KEY, 1),
    "tools/ops/_grar_parse.py:_is_routing_layer_key:startswith": (KEY, 1),
    "tools/ops/_grar_parse.py:_parse_config_files:is_reserved_name": (ROUTE_GENERATOR, 3),
    "tools/ops/_grar_parse.py:_parse_config_files:os.path.isdir": (ROOT, 1),
    "tools/ops/_grar_parse.py:_parse_nested_config:is_reserved_name": (ROUTE_GENERATOR, 2),
    "tools/ops/_grar_parse.py:_parse_platform_config:startswith": (ROUTE_GENERATOR, 1),
    "tools/ops/_grar_render.py:load_base_config:is_file": (OPERAND, 1),
    "tools/ops/_grar_validate.py:_find_platform_rules_configmap:is_file": (NOT_CONFD, 2),
    "tools/ops/_grar_validate.py:load_policy_document:is_file": (NOT_CONFD, 1),
    "tools/ops/_grar_validate.py:unknown_routing_keys:startswith": (KEY, 1),
    "tools/ops/_grar_validate.py:validate_receiver_domains:fnmatch.fnmatch": (VALUE_PATTERN, 1),
    "tools/ops/_grar_validate.py:validate_tenant_keys:startswith": (KEY, 1),
    "tools/ops/_grar_validate.py:writes_nothing:startswith": (KEY, 1),
    "tools/ops/_gsvc_child.py:_parent_dir:os.lstat": (OUTPUT, 1),
    "tools/ops/_gsvc_child.py:_write_file:os.lstat": (OUTPUT, 1),
    "tools/ops/_gsvc_child.py:apply_writes:os.path.isdir": (OUTPUT, 1),
    "tools/ops/_gsvc_child.py:apply_writes:os.path.islink": (OUTPUT, 1),
    "tools/ops/_gsvc_child.py:apply_writes:os.path.lexists": (OUTPUT, 1),
    "tools/ops/_gsvc_child.py:materialise:startswith": (GIT, 1),
    "tools/ops/_observed_map_lib.py:_find_rule_packs_dir:glob": (NOT_CONFD, 1),
    "tools/ops/_observed_map_lib.py:_find_rule_packs_dir:is_dir": (NOT_CONFD, 1),
    "tools/ops/_observed_map_lib.py:default_pack_paths:glob": (NOT_CONFD, 1),
    "tools/ops/_observed_map_lib.py:load_observed_map:os.path.isfile": (NOT_CONFD, 1),
    "tools/ops/_render_diff.py:_secret_key:startswith": (KEY, 1),
    "tools/ops/_render_diff.py:diff_served:startswith": (KEY, 1),
    "tools/ops/_render_diff.py:render_routes:exists": (OUTPUT, 1),
    "tools/ops/analyze_rule_pack_gaps.py:extract_custom_metrics:startswith": (KEY, 1),
    "tools/ops/analyze_rule_pack_gaps.py:main:os.path.isdir(ref)": (ROOT, 1),
    "tools/ops/analyze_rule_pack_gaps.py:main:os.path.isfile(ref)": (OPERAND, 2),
    "tools/ops/assemble_config_dir.py:carriers_already_in:is_dir": (ROOT_OUTPUT, 1),
    "tools/ops/assemble_config_dir.py:discover_yamls:is_dir": (ROOT, 1),
    "tools/ops/assemble_config_dir.py:list_visible_config_entries:has_yaml_extension": (WORDING, 1),
    "tools/ops/assemble_config_dir.py:list_visible_config_entries:is_hidden_name": (WORDING, 1),
    "tools/ops/assemble_config_dir.py:list_visible_config_entries:iterdir": (WORDING, 1),
    "tools/ops/backtest_threshold.py:_unwritable_output:exists": (OUTPUT, 2),
    "tools/ops/backtest_threshold.py:_unwritable_output:is_dir": (OUTPUT, 2),
    "tools/ops/backtest_threshold.py:_unwritable_output:os.access": (OUTPUT, 2),
    "tools/ops/backtest_threshold.py:_unwritable_output:stat": (OUTPUT, 1),
    "tools/ops/batch_diagnose.py:discover_tenants:has_yaml_extension": (CONFIGMAP, 1),
    "tools/ops/batch_diagnose.py:discover_tenants:is_reserved_name": (CONFIGMAP, 1),
    "tools/ops/blast_radius.py:classify_field:startswith": (KEY, 1),
    "tools/ops/blind_spot_discovery.py:load_monitored_db_types:is_dir": (ROOT, 1),
    "tools/ops/config_diff.py:_read_tree:os.path.isdir": (ROOT, 1),
    "tools/ops/config_diff.py:_run:exists": (ROOT, 1),
    "tools/ops/config_diff.py:_run:is_dir": (ROOT, 1),
    "tools/ops/config_diff.py:served_metric_configs:startswith": (KEY, 1),
    "tools/ops/config_diff.py:served_metric_diff:startswith": (KEY, 1),
    "tools/ops/config_diff.py:served_setting_configs:startswith": (KEY, 2),
    "tools/ops/config_diff.py:served_setting_diff:startswith": (KEY, 1),
    "tools/ops/config_history.py:_load_history:exists": (NOT_CONFD, 1),
    "tools/ops/config_history.py:_scan_config_dir:has_yaml_extension": (PR_BETA2, 1),
    "tools/ops/config_history.py:_scan_config_dir:is_dir": (PR_BETA2, 1),
    "tools/ops/config_history.py:_scan_config_dir:is_hidden_name": (PR_BETA2, 1),
    "tools/ops/config_history.py:_scan_config_dir:iterdir": (PR_BETA2, 1),
    "tools/ops/config_history.py:cmd_diff:exists": (OPERAND, 2),
    "tools/ops/da_assembler.py:_rendered_cr_name:is_file": (OUTPUT, 1),
    "tools/ops/da_assembler.py:reconcile_one:exists": (OUTPUT, 1),
    "tools/ops/da_assembler.py:reconcile_one:is_file": (OUTPUT, 1),
    "tools/ops/da_assembler.py:remove_rendered:exists": (OUTPUT, 1),
    "tools/ops/da_assembler.py:write_rendered:exists": (OUTPUT, 1),
    "tools/ops/deprecate_rule.py:_main:is_dir": (ROOT, 1),
    "tools/ops/diagnose.py:resolve_inheritance_chain:is_dir": (ROOT, 1),
    "tools/ops/diagnose.py:resolve_inheritance_chain:startswith": (KEY, 1),
    "tools/ops/drift_detect.py:compute_dir_manifest:has_yaml_extension": (PR_BETA2, 1),
    "tools/ops/drift_detect.py:compute_dir_manifest:is_dir": (PR_BETA2, 1),
    "tools/ops/drift_detect.py:compute_dir_manifest:is_hidden_name": (PR_BETA2, 1),
    "tools/ops/drift_detect.py:compute_dir_manifest:iterdir": (PR_BETA2, 1),
    "tools/ops/drift_detect.py:main:is_dir": (ROOT, 2),
    "tools/ops/explain_keys.py:_explain:is_dir": (ROOT, 1),
    "tools/ops/explain_route.py:main:is_dir": (ROOT, 1),
    "tools/ops/generate_rule_pack_split.py:process_rule_packs:glob": (NOT_CONFD, 1),
    "tools/ops/generate_tenant_mapping_rules.py:main:is_dir": (ROOT, 1),
    "tools/ops/generate_tenant_mapping_rules.py:main:is_file": (NOT_CONFD, 2),
    "tools/ops/generator_service.py:_prepare_socket_path:os.lstat": (NOT_CONFD, 1),
    "tools/ops/generator_service.py:main:os.path.isdir": (ROOT, 1),
    "tools/ops/gitops_check.py:check_local:is_dir": (ROOT, 1),
    "tools/ops/gitops_check.py:check_local:startswith": (KEY, 1),
    "tools/ops/init_project.py:_ConfdTree.__init__:is_dir": (ROOT, 1),
    "tools/ops/init_project.py:_case_variants_along:is_dir": (OUTPUT, 1),
    "tools/ops/init_project.py:_case_variants_along:os.walk": (OUTPUT, 1),
    "tools/ops/init_project.py:_check_existing_init:exists": (OUTPUT, 1),
    "tools/ops/init_project.py:_check_existing_init:is_file": (OUTPUT, 2),
    "tools/ops/init_project.py:_check_existing_init:is_symlink": (OUTPUT, 1),
    "tools/ops/init_project.py:_classify_root_file:exists": (OUTPUT, 1),
    "tools/ops/init_project.py:_classify_root_file:is_symlink": (OUTPUT, 1),
    "tools/ops/init_project.py:_enclosing_repo_root:exists": (NOT_CONFD, 1),
    "tools/ops/init_project.py:_existing_files:is_dir": (OUTPUT, 1),
    "tools/ops/init_project.py:_existing_files:is_file": (OUTPUT, 1),
    "tools/ops/init_project.py:_existing_files:rglob": (OUTPUT, 1),
    "tools/ops/maintenance_scheduler.py:load_recurring_schedules:is_dir": (ROOT, 1),
    "tools/ops/maintenance_scheduler.py:main:is_dir": (ROOT, 1),
    "tools/ops/migrate_rule.py:load_metric_dictionary:os.path.exists": (NOT_CONFD, 1),
    "tools/ops/migrate_to_operator.py:_tree_tenants:is_dir": (ROOT, 1),
    "tools/ops/migrate_to_operator.py:parse_configmap_rules:has_yaml_extension": (PR_BETA2, 1),
    "tools/ops/migrate_to_operator.py:parse_configmap_rules:is_dir": (ROOT, 1),
    "tools/ops/migrate_to_operator.py:parse_configmap_rules:is_file": (PR_BETA2, 1),
    "tools/ops/migrate_to_operator.py:parse_configmap_rules:iterdir": (PR_BETA2, 1),
    "tools/ops/migration_audit.py:_audit:is_dir": (ROOT, 1),
    "tools/ops/notification_tester.py:main:is_dir": (ROOT, 1),
    "tools/ops/offboard_tenant.py:find_config_file:is_dir": (ROOT, 1),
    "tools/ops/onboard_platform.py:scan_rule_files:glob": (NOT_CONFD, 1),
    "tools/ops/onboard_platform.py:scan_rule_files:is_file": (NOT_CONFD, 1),
    "tools/ops/operator_generate.py:discover_rule_packs:glob": (NOT_CONFD, 1),
    "tools/ops/operator_generate.py:discover_rule_packs:is_dir": (NOT_CONFD, 1),
    "tools/ops/operator_generate.py:discover_tenant_configs:is_dir": (ROOT, 1),
    "tools/ops/patch_config.py:exporter_reads:has_yaml_extension": (CONFIGMAP, 1),
    "tools/ops/patch_config.py:exporter_reads:is_hidden_name": (CONFIGMAP, 1),
    "tools/ops/patch_config.py:is_carrier_candidate:has_yaml_extension": (CONFIGMAP, 1),
    "tools/ops/patch_config.py:is_carrier_candidate:is_hidden_name": (CONFIGMAP, 1),
    "tools/ops/patch_config.py:is_carrier_candidate:is_reserved_name": (CONFIGMAP, 1),
    "tools/ops/patch_config.py:verify_pod:startswith": (KEY, 1),
    "tools/ops/policy_engine.py:_main:is_dir": (ROOT, 1),
    "tools/ops/policy_engine.py:_main:is_file": (OPERAND, 1),
    "tools/ops/policy_engine.py:_resolve_wildcard_values:fnmatch.fnmatch": (VALUE_PATTERN, 1),
    "tools/ops/policy_engine.py:load_policy_inputs:startswith": (KEY, 2),
    "tools/ops/policy_engine.py:read_policy_file:is_file": (OPERAND, 1),
    "tools/ops/policy_engine.py:rules_read_routing:fnmatch.fnmatch": (VALUE_PATTERN, 1),
    "tools/ops/policy_opa_bridge.py:build_opa_input:startswith": (KEY, 1),
    "tools/ops/threshold_govern.py:main:os.path.isdir": (ROOT, 1),
    "tools/ops/threshold_recommend.py:analyze_tenant:startswith": (KEY, 1),
    "tools/ops/threshold_recommend.py:main:is_dir": (ROOT, 1),
    "tools/ops/validate_config.py:_custom_alert_compiler:os.path.isdir": (NOT_CONFD, 1),
    "tools/ops/validate_config.py:_find_schema:is_file": (NOT_CONFD, 2),
    "tools/ops/validate_config.py:_keys_acting_when_merged:startswith": (KEY, 2),
    "tools/ops/validate_config.py:_main:os.path.isdir": (ROOT, 1),
    "tools/ops/validate_config.py:_policy_dsl_row:os.path.isfile": (OPERAND, 1),
    "tools/ops/validate_config.py:_root_decoded_keys:startswith": (KEY, 1),
    "tools/ops/validate_config.py:_schedule_null_lines:startswith": (KEY, 1),
    "tools/ops/validate_config.py:check_custom_rules:os.path.isdir": (NOT_CONFD, 1),
    "tools/ops/validate_config.py:check_json_schema:os.path.isdir": (NOT_CONFD, 1),
    "tools/ops/validate_config.py:check_profiles:startswith": (KEY, 1),
    "tools/ops/validate_config.py:check_root_defaults:startswith": (KEY, 4),
    "tools/ops/validate_config.py:check_versions:is_file": (NOT_CONFD, 1),
    "tools/validate_all.py:_compare_baseline:exists": (NOT_CONFD, 1),
    "tools/validate_all.py:_drop_isolated_tree:exists": (NOT_CONFD, 1),
    "tools/validate_all.py:_drop_isolated_tree:is_dir": (NOT_CONFD, 1),
    "tools/validate_all.py:_isolated_tree:is_file": (NOT_CONFD, 1),
    "tools/validate_all.py:_isolated_tree:is_symlink": (NOT_CONFD, 1),
    "tools/validate_all.py:_snapshot_mtimes:exists": (NOT_CONFD, 2),
    "tools/validate_all.py:_snapshot_mtimes:is_file": (NOT_CONFD, 1),
    "tools/validate_all.py:_snapshot_mtimes:rglob": (NOT_CONFD, 1),
    "tools/validate_all.py:_snapshot_mtimes:stat": (NOT_CONFD, 2),
    "tools/validate_all.py:main:exists": (NOT_CONFD, 2),
}

REASONS = frozenset({ROOT, ROOT_DEFAULT, ROOT_OUTPUT, OUTPUT, OPERAND, NOT_CONFD, LOADER, KEY,
                     PREDICATE, WORDING, MEASURE, K8S_KEY, VALUE_PATTERN, CONFIGMAP, GIT,
                     PR_BETA2, ROUTE_GENERATOR})


# ── the gate ─────────────────────────────────────────────────────────

def test_the_population_is_read() -> None:
    """Tripwire: the gate is only as wide as the population it walks."""
    pop = _population()
    assert len(pop) >= 40, [p.name for p in pop]
    names = {p.relative_to(SCRIPTS).as_posix() for p in pop}
    for must in ("tools/_lib_confd.py", "tools/ops/validate_config.py",
                 "tools/ops/deprecate_rule.py", "tools/ops/init_project.py",
                 "tools/lint/check_retire_drift.py", "ops/configmap_assemble.py"):
        assert must in names, must


def test_no_python_judgement_outside_the_allowlist() -> None:
    """Exact both ways, and by count (M-2): a second `is_file()` in a function
    allowed one is a new judgement, not the old one again."""
    actual = _actual()
    allowed = {k: n for k, (_why, n) in ALLOWED.items()}
    new = {k: n for k, n in sorted(actual.items()) if k not in allowed}
    stale = sorted(k for k in allowed if k not in actual)
    moved = {k: (allowed[k], n) for k, n in sorted(actual.items())
             if k in allowed and allowed[k] != n}
    assert not new and not stale and not moved, (
        "Python judges a conf.d path where the gate does not allow it.\n"
        + "".join(f"  NEW   {k} (x{n})\n" for k, n in new.items())
        + "".join(f"  COUNT {k}: allowed {a}, found {n}\n" for k, (a, n) in moved.items())
        + "".join(f"  STALE {k} — matches nothing now: delete it\n" for k in stale)
        + "Read the entry from da-guard's listing instead (`Entry.role` / `.reserved` / "
          "`.dir_link`, `_lib_parse_tree.tenant_scope`), or — if the path is not a conf.d "
          "entry — add it (with its count) under one of the module's reasons.")


def test_every_entry_gives_one_of_the_reasons() -> None:
    bad = {k: v for k, v in ALLOWED.items()
           if not (isinstance(v, tuple) and len(v) == 2 and v[0] in REASONS
                   and isinstance(v[1], int) and v[1] > 0)}
    assert not bad, bad
    assert all("\n" not in r and r.strip() for r in REASONS)


#: The reasons that mean "a real judgement of conf.d, not migrated yet".
NOT_MIGRATED = frozenset({PR_BETA2, CONFIGMAP, GIT, ROUTE_GENERATOR})
_LIB = "tools/_lib_confd.py"


def _named_in_docs(key: str) -> str:
    """How ADR-036's step 9 progress line names a not-yet-migrated entry: a
    function of the shared library by its own name, any other tool by its
    module name."""
    rel, func, _call = key.split(":")
    return func if rel == _LIB else pathlib.PurePosixPath(rel).stem


@pytest.mark.parametrize("adr", ["036-single-parser-effective-config.md",
                                 "036-single-parser-effective-config.en.md"])
def test_the_adr_names_exactly_what_is_not_migrated(adr) -> None:
    """m5: ADR-036 §7's progress line for step 9 lists the Python judgements
    still left — and that list IS this allowlist's not-migrated entries, in
    both directions: an entry the ADR does not name is a judgement nobody was
    told about; a module the ADR names that has none left is a stale claim.
    (The line also names what this gate cannot see — `declared_tenant_ids` —
    which is no allowlist entry and so neither direction's business.)"""
    text = (REPO / "docs" / "adr" / adr).read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines()
                if "test_confd_tools_no_python_judgement.py" in ln and "`config_history`" in ln)
    want = {_named_in_docs(k) for k, (why, _n) in ALLOWED.items() if why in NOT_MIGRATED}
    named = set(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", line))
    missing = sorted(want - named)
    assert not missing, f"{adr}: not-migrated entries the ADR does not name: {missing}"
    candidates = {pathlib.PurePosixPath(k.split(":")[0]).stem for k in ALLOWED} | {
        k.split(":")[1] for k in ALLOWED if k.startswith(_LIB + ":")}
    stale = sorted((named & candidates) - want - {"_lib_confd"})
    assert not stale, f"{adr}: named as not migrated, but nothing of theirs is left: {stale}"


# ── the detector catches each shape (synthetic, no repo file) ────────

_SHAPES = {
    "startswith": "def f(name):\n    return name.startswith('_')\n",
    "startswith-longer": "def f(name):\n    return name.startswith('_defaults')\n",
    "startswith-dot": "def f(name):\n    return name.startswith('./x')\n",
    "startswith-tuple": "def f(p):\n    return p.name.startswith(('~', '.'))\n",
    "startswith-keyword": "def f(p):\n    return p.name.startswith(prefix='_x')\n",
    "is_file": "def f(p):\n    return p.is_file()\n",
    "is_dir": "def f(p):\n    return p.is_dir()\n",
    "exists": "def f(p):\n    return p.exists()\n",
    "iterdir": "def f(p):\n    return list(p.iterdir())\n",
    "rglob": "def f(p):\n    return list(p.rglob('*'))\n",
    "glob": "def f(p):\n    return list(p.glob('*.yaml'))\n",
    "stat": "def f(p):\n    return p.stat().st_mode\n",
    "lstat": "def f(p):\n    return p.lstat()\n",
    "is_symlink": "def f(p):\n    return p.is_symlink()\n",
    "Path.walk": "def f(p):\n    return list(p.walk())\n",
    "os.path.isfile": "import os\ndef f(p):\n    return os.path.isfile(p)\n",
    "os.path.isdir-alias": "import os.path as osp\ndef f(p):\n    return osp.isdir(p)\n",
    "os.path.exists-from": "from os.path import exists\ndef f(p):\n    return exists(p)\n",
    "os.path.islink": "import os\ndef f(p):\n    return os.path.islink(p)\n",
    "os.path.lexists": "import os\ndef f(p):\n    return os.path.lexists(p)\n",
    "os.path.getsize": "from os import path\ndef f(p):\n    return path.getsize(p)\n",
    "os.walk": "import os\ndef f(p):\n    return list(os.walk(p))\n",
    "os.walk-from": "from os import walk as w\ndef f(p):\n    return list(w(p))\n",
    "os.scandir": "import os\ndef f(p):\n    return list(os.scandir(p))\n",
    "os.listdir": "import os\ndef f(p):\n    return os.listdir(p)\n",
    "os.stat": "import os\ndef f(p):\n    return os.stat(p)\n",
    "os.lstat-from": "from os import lstat\ndef f(p):\n    return lstat(p)\n",
    "os.access": "import os\ndef f(p):\n    return os.access(p, os.R_OK)\n",
    "fnmatch": "import fnmatch\ndef f(n):\n    return fnmatch.fnmatch(n, '_*')\n",
    "fnmatch-from": "from fnmatch import filter as ff\ndef f(ns):\n    return ff(ns, '*.yaml')\n",
    "getattr": "def f(p):\n    return getattr(p, 'is_file')()\n",
    "getattr-predicate": "def f(m, n):\n    return getattr(m, 'is_reserved_name')(n)\n",
    "ref-Path.is_file": ("from pathlib import Path\n"
                         "def f(ps):\n    return list(filter(Path.is_file, ps))\n"),
    "ref-pathlib.Path.is_dir": ("import pathlib\n"
                                "def f(ps):\n    return [*filter(pathlib.Path.is_dir, ps)]\n"),
    "ref-os.path.isdir": "import os\ndef f(ps):\n    return list(map(os.path.isdir, ps))\n",
    "ref-predicate": "def f(ns):\n    return sorted(ns, key=has_yaml_extension)\n",
    "ref-imported": "from os.path import isfile\ndef f(ps):\n    return list(filter(isfile, ps))\n",
    "has_yaml_extension": "def f(n):\n    return has_yaml_extension(n)\n",
    "is_reserved_name": "import _lib_confd\ndef f(n):\n    return _lib_confd.is_reserved_name(n)\n",
    "is_hidden_name": "def f(n):\n    return is_hidden_name(n)\n",
    "is_dir_symlink": "def f(p):\n    return is_dir_symlink(p)\n",
}


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_each_forbidden_shape_is_caught(shape) -> None:
    found = judgements(ast.parse(_SHAPES[shape]))
    assert list(found) and all(k.startswith("f:") for k in found), (shape, found)


def test_the_key_names_the_enclosing_function_the_call_and_the_count() -> None:
    src = textwrap.dedent("""
        import pathlib
        class C:
            def m(self, e):
                def inner(n):
                    return n.startswith("_") or n.startswith(".")
                return e.reserved
        x = pathlib.Path(".").exists()
        y = list(filter(pathlib.Path.is_file, []))
    """)
    assert judgements(ast.parse(src)) == {"C.m.inner:startswith": 2, "<module>:exists": 1,
                                          "<module>:is_file(ref)": 1}


@pytest.mark.parametrize("src", [
    "def f(e):\n    return e.reserved or e.dir_link or e.config_file or e.is_dir\n",
    "def f(name):\n    return name.startswith('team/')\n",
    "def f(k):\n    return k.endswith('_')\n",
    "import ast\ndef f(t):\n    return list(ast.walk(t))\n",
    "import os\ndef f(a, b):\n    return os.path.join(a, b)\n",
    "from os.path import isfile\n",
    "def f(p):\n    return getattr(p, 'name')\n",
])
def test_reading_the_entry_is_not_flagged(src) -> None:
    assert judgements(ast.parse(src)) == {}


@pytest.mark.parametrize("src", [
    "def f(n):\n    return n[0] == '_'\n",
    "def f(n):\n    return n[:1] == '_'\n",
    "def f(p):\n    return p.suffix in ('.yaml', '.yml')\n",
    "import re\ndef f(n):\n    return re.match(r'_', n)\n",
    "P = '_'\ndef f(n):\n    return n.startswith(P)\n",
])
def test_what_the_gate_cannot_see_is_what_its_docstring_says(src) -> None:
    """The documented blind spots stay blind: if one of these starts being
    caught, the docstring's list is wrong and must be updated with it."""
    assert judgements(ast.parse(src)) == {}


# ── the tenant set is one answer (#3206 M-A) ─────────────────────────

#: Who may take the tenant files WITHOUT the entries that leave the set
#: unknown (`_lib_parse_tree.TenantScope`), and why. Every other reader of
#: "which tenants does this tree declare" takes `tenant_scope` /
#: `declared_tenants` (both halves).
HALF_SCOPE_CALLERS = {
    "tools/ops/patch_config.py": "a ConfigMap's keys: no directory, link or special file can exist",
    "tools/ops/gitops_check.py": "reads every selected entry itself and names each it cannot read",
    "tools/lint/check_path_metadata_consistency.py": "names every unreadable entry from the same "
                                                     "document (its own `_` rule: PR_BETA2 above)",
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
