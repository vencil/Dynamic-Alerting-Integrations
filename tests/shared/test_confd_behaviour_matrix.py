"""Behaviour matrix: every conf.d tool against every tree Go can flag (#3206).

ADR-036 §7 step 9 makes "which paths a conf.d holds and whether the tree can
be served" Go's verdict. An AST gate can forbid the Python name predicates;
it cannot tell whether a tool judges paths some other way (a `stat`, an
`is_file()`, a prefix) and so disagrees with Go. That is a behaviour
question, and this file asks it.

THE ORACLE IS GO'S VALIDATION VERDICT
-------------------------------------
Each cell builds a tree, then — as the uid the tool runs as — runs
`da-guard --config-dir <tree> --format json --cardinality-limit 0` (the
cardinality check off, so a cap cannot change the exit) and reads:

  * its exit: 0 (the tree can be served), 3 (files the exporter cannot read
    or decode) or 4 (paths only the route generator judges);
  * the paths Go requires a reader to name: `unreadable[].file` ∪
    `parse_failed[]` ∪ the `undetermined[]` items whose reason is
    `unreadable_outside_run` (`.file`).

Nothing here composes a "flagged" label of its own: the set to name is
exactly what Go names.

THE INVARIANT, for every tool in `TOOLS` on every tree
------------------------------------------------------
  * Go exit 0 (`matrix: control` rows): the tool exits 0. Without this, a
    tool that fails on everything would pass every other cell.
  * Go exit 3 or 4 (`matrix: flagged`): every path Go names appears (its
    path or base name) in the tool's stdout + stderr, AND the tool exits
    non-zero (which code is dev-rules §13's business — 1 or 2, or a code
    §13 sanctions such as `da-tools guard`'s 3 and 4 — not the matrix's),
    AND the
    output holds no Python traceback, AND the tool was not stopped by the
    cell timeout (exit 124 / 137: a hang, e.g. a FIFO opened for reading).
  * `matrix: refused` (a file name that is not UTF-8): the tool exits 2.
    The oracle there is `da-guard parse-tree`, which must exit 2 — da-guard
    validation itself exits 0 on such a tree today (a Go defect fixed in a
    later PR), so it is not this row's oracle.
  * Sanity, never a pass: a `flagged` row on which Go validation exits
    neither 3 nor 4, or names nothing, is red — "the cell measures nothing".
    Likewise a `control` row Go does not pass, or a `refused` row
    parse-tree does not refuse.

THERE IS NO EXCEPTION TABLE
---------------------------
No tool has a role column and no `(tool, kind)` is excused. A cell that
breaks the invariant today is in `XFAIL`, keyed `(tool, row)`, applied as
`xfail(strict=True)` with a reason naming the follow-up that fixes it: the
day it is fixed the cell XPASSes and goes red until the entry is deleted.
Nothing skips (`unprivileged_sandbox` FAILS instead of skipping under
`VIBE_REQUIRE_UNPRIVILEGED_FS`, which CI sets).

`deprecate_rule._reserved` reads Go's class (`Entry.reserved`). Replaced by
a name test (`rel[:1] == "_"`), every one of its cells came out the same
(measured): Go's rule is that very test on the base name
(`confdname.IsReserved`), and the tool asks it only of paths in Go's
listing — same as Go, unobservable here; step 9b-3 deletes the copy.

WHERE THE TREES COME FROM
-------------------------
`confd_fixture_catalog.json`. Go owns its expectations
(`cmd/da-guard/fixture_catalog*_test.go`: every (role, kind) parse-tree can
write has a row, each recipe gives exactly the expected entries, and each
row's `matrix` class is the validation exit class). This file never reads
`expect`: each cell asks da-guard itself, on the tree it just built.

WHO IS IN THE TABLE
-------------------
`TOOLS` is held to the enumeration contract's population (`_readers()` of
`test_confd_enumeration_contract`, the files with a `__main__` entry point)
both ways by `test_every_population_tool_is_in_the_matrix_or_says_why`.
"""
from __future__ import annotations

import ast
import base64
import contextlib
import dataclasses
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
from collections.abc import Callable

import pytest

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
TESTS = HERE.parent
for _p in (HERE, TESTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from _platform_fs import symlink_or_skip, unprivileged_sandbox  # noqa: E402

CATALOG_PATH = HERE / "confd_fixture_catalog.json"
CATALOG = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
ROWS = {r["id"]: r for r in CATALOG["rows"]}
MATRIX_ROWS = [r["id"] for r in CATALOG["rows"] if r["matrix"] != "go_only"]

# ── the recipe language (the Go half reads the same ops) ────────────────


def _path(base: pathlib.Path, op: dict) -> bytes:
    raw = bytes.fromhex(op["path_hex"]) if "path_hex" in op else op["path"].encode("utf-8")
    return os.path.join(os.fsencode(base), raw)


def build(base: pathlib.Path, row: dict) -> pathlib.Path:
    """Apply the catalogue's `base` (when the row asks) and the row's ops
    under *base*; return the tree (`base/conf.d`)."""
    ops = [*(CATALOG["base"] if row.get("base") else ()), *row["ops"]]
    for op in ops:
        kind, p = op["op"], _path(base, op)
        if kind == "mkdir":
            os.makedirs(p, exist_ok=True)
        elif kind == "write":
            data = (base64.b64decode(op["b64"]) if "b64" in op
                    else op["text"].encode("utf-8"))
            with open(p, "wb") as fh:
                fh.write(data)
        elif kind == "symlink":
            symlink_or_skip(op["target"], p)
        elif kind == "symlink_chain":
            via = os.path.join(os.fsencode(base), op["via"].encode("utf-8"))
            prev = op["target"]
            for i in range(op["length"]):
                symlink_or_skip(prev, os.path.join(via, b"l%d" % i))
                prev = f"l{i}"
            rel_via = os.path.relpath(os.fsdecode(via), os.path.dirname(os.fsdecode(p)))
            symlink_or_skip(os.path.join(rel_via, prev), p)
        elif kind == "mkfifo":
            os.mkfifo(p)
        elif kind == "chmod":
            os.chmod(p, int(op["mode"], 8))
        else:
            raise ValueError(f"unknown recipe op {kind!r}")
    return base / CATALOG["root"]


# ── the tools ───────────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class Tool:
    """One row of the table. *argv* is formatted with {py} (the
    interpreter), {scripts} (the sandbox's scripts/), {box}, {cell} (this
    cell's directory, the tree's parent), {conf} (the tree), {out} (a
    directory the tool may write) and {prom} (a Prometheus URL that refuses
    at once). *prep* readies operands before the oracle runs. *script* is
    the file under `scripts/` the row runs, when argv does not name it."""
    argv: tuple[str, ...]
    prep: Callable[[pathlib.Path], None] | None = None
    script: str | None = None

    def script_path(self) -> str:
        if self.script:
            return self.script
        for a in self.argv:
            if a.startswith("{scripts}/") and a.endswith(".py"):
                return a[len("{scripts}/"):]
        raise AssertionError(f"{self.argv}: no script named")


def _empty_source(cell: pathlib.Path) -> None:
    (cell / "src").mkdir()


def _helm_values(cell: pathlib.Path) -> None:
    """A K8s target for every tenant the catalogue's files declare a db_type
    for, so a tree with nothing flagged is no retire-ordering violation."""
    (cell / "helm").mkdir()
    for t in ("a", "b", "eye"):
        (cell / "helm" / f"values-{t}.yaml").write_text("{}\n", encoding="utf-8")
    (cell / "ns.yaml").write_text("", encoding="utf-8")


def _instance_mapping(cell: pathlib.Path) -> None:
    """Without `_instance_mapping.yaml` the generator returns before it
    reads any tenant; with it, `--validate` checks the ids against the tree."""
    (cell / "conf.d" / "_instance_mapping.yaml").write_text(
        "instance_tenant_mapping:\n  inst-1:\n    - tenant: a\n      filter: 'schema=\"a\"'\n",
        encoding="utf-8")


def _what_if(cell: pathlib.Path) -> None:
    (cell / "what-if.yaml").write_text("tenants:\n  a:\n    mysql_connections: \"90\"\n",
                                       encoding="utf-8")


_PY = "{py}"
_T = "{scripts}/tools/"
TOOLS: dict[str, Tool] = {
    "configmap_assemble": Tool((_PY, "{scripts}/ops/configmap_assemble.py", "--config-dir", "{conf}",
                                "--output", "{out}/cm.yaml")),
    "validate_config": Tool((_PY, _T + "ops/validate_config.py", "--config-dir", "{conf}")),
    "check_confd_schema": Tool((_PY, _T + "lint/check_confd_schema.py", "--config-dir", "{conf}",
                                "--ci")),
    "migrate_conf_d": Tool((_PY, _T + "dx/migrate_conf_d.py", "--conf-d", "{conf}", "--dry-run")),
    "assemble_config_dir": Tool((_PY, _T + "ops/assemble_config_dir.py", "--sources", "{conf}",
                                 "--output", "{out}/assembled")),
    "deprecate_rule": Tool((_PY, _T + "ops/deprecate_rule.py", "mysql_slave_lag",
                            "--config-dir", "{conf}")),
    "offboard_tenant": Tool((_PY, _T + "ops/offboard_tenant.py", "a", "--config-dir", "{conf}")),
    "explain_keys": Tool((_PY, _T + "ops/explain_keys.py", "--config-dir", "{conf}")),
    "init_project": Tool((_PY, _T + "ops/init_project.py", "--non-interactive", "--tenants", "newt",
                          "--deploy", "helm", "--force", "-o", "{cell}",
                          "--da-tools-image", "ghcr.io/vencil/da-tools:v0.0.0")),
    "operator_generate": Tool((_PY, _T + "ops/operator_generate.py", "--config-dir", "{conf}",
                               "--rule-packs-dir", "{box}/rule-packs", "--dry-run")),
    "migrate_to_operator": Tool((_PY, _T + "ops/migrate_to_operator.py", "--source-dir", "{cell}/src",
                                 "--config-dir", "{conf}", "--dry-run"), prep=_empty_source),
    "check_retire_drift": Tool((_PY, _T + "lint/check_retire_drift.py", "--config-dir", "{conf}",
                                "--helm-dir", "{cell}/helm", "--namespaces-file", "{cell}/ns.yaml",
                                "--ci"), prep=_helm_values),
    "generate_tenant_mapping_rules": Tool((_PY, _T + "ops/generate_tenant_mapping_rules.py",
                                           "--config-dir", "{conf}", "--validate", "--dry-run"),
                                          prep=_instance_mapping),
    "check_routing_profiles": Tool((_PY, _T + "lint/check_routing_profiles.py", "--config-dir", "{conf}")),
    # One tree on both sides: nothing changed, so a clean tree is exit 0.
    "config_diff": Tool((_PY, _T + "ops/config_diff.py", "--old-dir", "{conf}", "--new-dir", "{conf}")),
    "compile_custom_alerts": Tool((_PY, _T + "dx/compile_custom_alerts.py", "--config-dir", "{conf}",
                                   "--out", "{out}/pack.yaml", "--allow-empty")),
    "generate_alertmanager_routes": Tool((_PY, _T + "ops/generate_alertmanager_routes.py",
                                          "--config-dir", "{conf}", "--validate")),
    "config_history": Tool((_PY, _T + "ops/config_history.py", "--config-dir", "{conf}", "snapshot")),
    "drift_detect": Tool((_PY, _T + "ops/drift_detect.py", "--dirs", "{conf},{conf}")),
    # The soak's only conf.d selection runs inside a loop of whole minutes
    # against a live exporter; the cell calls that selection itself.
    "run_chaos_soak": Tool((_PY, "-c",
                            ("import sys; sys.path[:0] = [sys.argv[1], sys.argv[1] + '/dx'];"
                             " from pathlib import Path; import run_chaos_soak as m;"
                             " sys.exit(0 if m.trigger_reload(Path(sys.argv[2])) else 1)"),
                            "{scripts}/tools", "{conf}"), script="tools/dx/run_chaos_soak.py"),
    "describe_tenant": Tool((_PY, _T + "dx/describe_tenant.py", "--conf-d", "{conf}", "--all")),
    "describe_tenant --replaces": Tool((_PY, _T + "dx/describe_tenant.py", "a", "--conf-d", "{conf}",
                                        "--what-if", "{cell}/what-if.yaml", "--replaces", "{conf}/a.yaml"),
                                       prep=_what_if),
    "tenant_verify": Tool((_PY, _T + "dx/tenant_verify.py", "--all", "--conf-d", "{conf}")),
    "generate_tenant_metadata": Tool((_PY, _T + "dx/generate_tenant_metadata.py", "--config-dir", "{conf}",
                                      "--dry-run")),
    "check_path_metadata_consistency": Tool((_PY, _T + "lint/check_path_metadata_consistency.py",
                                             "--config-dir", "{conf}", "--ci")),
    "analyze_rule_pack_gaps": Tool((_PY, _T + "ops/analyze_rule_pack_gaps.py", "--config-dir", "{conf}")),
    "explain_route": Tool((_PY, _T + "ops/explain_route.py", "--config-dir", "{conf}")),
    "maintenance_scheduler": Tool((_PY, _T + "ops/maintenance_scheduler.py", "--config-dir", "{conf}",
                                   "--dry-run")),
    "migration_audit": Tool((_PY, _T + "ops/migration_audit.py", "--config-dir", "{conf}")),
    "notification_tester": Tool((_PY, _T + "ops/notification_tester.py", "--config-dir", "{conf}",
                                 "--dry-run")),
    "policy_engine": Tool((_PY, _T + "ops/policy_engine.py", "--config-dir", "{conf}", "--ci")),
    "policy_opa_bridge": Tool((_PY, _T + "ops/policy_opa_bridge.py", "--config-dir", "{conf}",
                               "--dry-run")),
    "gitops_check": Tool((_PY, _T + "ops/gitops_check.py", "local", "--dir", "{conf}", "--ci")),
    "guard_dispatch": Tool((_PY, _T + "ops/guard_dispatch.py", "defaults-impact", "--config-dir", "{conf}")),
    # The four below query a Prometheus as well; {prom} refuses at once, and
    # each reads the tree whatever the query answered.
    "blind_spot_discovery": Tool((_PY, _T + "ops/blind_spot_discovery.py", "--prometheus", "{prom}",
                                  "--config-dir", "{conf}")),
    "threshold_recommend": Tool((_PY, _T + "ops/threshold_recommend.py", "--config-dir", "{conf}",
                                 "--prometheus", "{prom}", "--dry-run")),
    "threshold_govern": Tool((_PY, _T + "ops/threshold_govern.py", "--config-dir", "{conf}",
                              "--prometheus", "{prom}")),
    "backtest_threshold": Tool((_PY, _T + "ops/backtest_threshold.py", "--config-dir", "{conf}",
                                "--baseline", "{conf}", "--prometheus", "{prom}",
                                "--skip-if-unavailable")),
}

#: Population files (paths under `scripts/`) with an entry point that are
#: not in `TOOLS`, and why. ⛔ "It would fail a cell" is not a reason.
NOT_IN_MATRIX: dict[str, str] = {
    "tools/ops/batch_diagnose.py": "takes ConfigMap keys (a flat mapping fetched from a cluster), "
                                   "not a conf.d tree",
    "tools/ops/patch_config.py": "reads a ConfigMap through kubectl (flat keys), not a conf.d tree",
    "tools/ops/diagnose.py": ("reads conf.d, but needs a live Prometheus: without one it exits 2 "
                              "whatever the tree (a precondition the sandbox does not provide)"),
    "tools/ops/cutover_tenant.py": "drives a cluster (kubectl); takes no conf.d operand",
    "tools/ops/byo_check.py": "queries a live Prometheus / Alertmanager; takes no conf.d operand",
    "tools/ops/baseline_discovery.py": "queries a live Prometheus and writes suggestions; reads no conf.d",
    "tools/ops/da_assembler.py": "its --config-dir is an OUTPUT (a CRD controller writing conf.d files)",
    "tools/ops/generator_service.py": "tenant-api's long-running judge over a stdin protocol, "
                                      "not a CLI over a tree",
    "tools/ops/migrate_rule.py": "writes conf.d content and reads it back at the path it writes; "
                                 "takes no input tree",
    "tools/ops/onboard_platform.py": "writes conf.d content and reads it back at the path it writes; "
                                     "takes no input tree",
    "tools/ops/scaffold_tenant.py": "writes conf.d content and reads it back at the path it writes; "
                                    "takes no input tree",
    "tools/dx/generate_tenant_fixture.py": "writes a synthetic conf.d; reads none",
    "tools/ops/generate_rule_pack_split.py": "splits rule packs; reads no conf.d",
    "tools/dx/generate_platform_data.py": "reads the repo's own conf.d at a fixed path; takes no operand",
    "tools/dx/sync_schema.py": "generates the schema from Go source; reads no conf.d",
    "tools/lint/check_account_registry_monotonic.py": "reads one registry file across commits, not a tree",
    "tools/lint/check_cli_contract.py": "checks documented commands against argparse; reads no conf.d",
    "tools/lint/check_cli_default_drift.py": "checks documented defaults against argparse; reads no conf.d",
    "tools/lint/check_doc_datools_cmds.py": "checks documented commands; reads no conf.d",
    "tools/lint/check_frontmatter_versions.py": "scans docs front matter; reads no conf.d",
    "tools/lint/check_md_yaml_drift.py": "checks YAML examples in docs against the schema; "
                                         "reads no conf.d tree",
    "tools/lint/check_structure.py": "checks the repo layout; reads no conf.d",
    "tools/lint/check_scrape_reachability.py": "repo self-check over fixed in-repo paths; no conf.d operand",
    "tools/lint/check_threshold_observed_map.py": "repo self-check over fixed in-repo paths; "
                                                  "no conf.d operand",
    "tools/lint/check_threshold_reachability.py": "repo self-check over fixed in-repo paths; "
                                                  "no conf.d operand",
    "tools/lint/check_threshold_registry.py": "repo self-check over fixed in-repo paths; no conf.d operand",
    "tools/lint/check_threshold_unit_sanity.py": "repo self-check over fixed in-repo paths; "
                                                 "no conf.d operand",
    "tools/validate_all.py": "runs other tools on the repo's fixed paths; takes no conf.d operand",
}

#: Table rows outside the population's root (`scripts/tools`).
OUTSIDE_POPULATION = {"ops/configmap_assemble.py"}

_X_EXIT = "X: shared fail-closed exit — a path Go names does not fail the tool (next PR)"
_X_NAME = "X: shared fail-closed exit — a path Go names is not named (next PR)"
_X_CRASH = "X: traceback — the tool crashes on a path Go names (next PR)"
_X_UTF8 = "X: a file name that is not UTF-8 is not refused with exit 2 (next PR)"
_B2 = "β2: migrates to da-guard"


def _cells(tool: str, reason: str, *rows: str) -> dict[tuple[str, str], str]:
    return {(tool, r): reason for r in rows}


#: Cells that break the invariant today, measured against this contract.
#: Each is strict: a fixed cell XPASSes and goes red until it is deleted.
XFAIL: dict[tuple[str, str], str] = {
    **_cells("configmap_assemble", _X_EXIT, "link_to_000_dir", "dir_444", "dangling_link", "looping_link",
             "chain_41_links", "fifo", "config_named_dir", "unlistable_config_named_dir",
             "unlistable_subdir", "unlistable_reserved_dir", "config_named_dir_link", "team_link_outside"),
    **_cells("migrate_conf_d", _X_EXIT, "dir_444", "config_named_dir"),
    **_cells("assemble_config_dir", _X_EXIT, "link_to_000_dir", "dir_444", "dangling_link", "looping_link",
             "chain_41_links", "fifo", "config_named_dir", "unlistable_config_named_dir",
             "unlistable_subdir", "unlistable_reserved_dir", "config_named_dir_link", "team_link_outside"),
    **_cells("deprecate_rule", _X_EXIT, "link_to_000_dir", "dangling_link", "looping_link",
             "chain_41_links", "fifo", "config_named_dir", "config_named_dir_link", "team_link_outside"),
    **_cells("offboard_tenant", _X_EXIT, "link_to_000_dir", "dir_444", "dangling_link", "looping_link",
             "chain_41_links", "fifo", "config_named_dir", "config_named_dir_link", "team_link_outside"),
    **_cells("explain_keys", _X_NAME, "link_to_000_dir", "config_named_dir_link", "team_link_outside"),
    **_cells("explain_keys", _X_EXIT, "config_named_dir"),
    **_cells("init_project", _X_EXIT, "mode_000_file", "link_to_000_dir", "dangling_link", "looping_link",
             "chain_41_links", "fifo", "config_named_dir", "unlistable_config_named_dir",
             "unlistable_subdir", "unlistable_reserved_dir", "config_named_dir_link", "team_link_outside"),
    **_cells("init_project", _X_CRASH, "link_into_000_dir", "dir_444"),
    **_cells("compile_custom_alerts", _X_EXIT, "link_to_000_dir", "config_named_dir_link",
             "team_link_outside"),
    **_cells("compile_custom_alerts", _X_NAME, "config_named_dir"),
    **_cells("config_history", _B2, "non_utf8_name", "mode_000_file", "link_into_000_dir", "link_to_000_dir", "dir_444",
             "dangling_link", "looping_link", "chain_41_links", "fifo", "config_named_dir",
             "unlistable_config_named_dir", "unlistable_subdir", "unlistable_reserved_dir",
             "config_named_dir_link", "team_link_outside"),
    **_cells("drift_detect", _B2, "non_utf8_name", "mode_000_file", "link_into_000_dir", "link_to_000_dir", "dir_444",
             "dangling_link", "looping_link", "chain_41_links", "fifo", "config_named_dir",
             "unlistable_config_named_dir", "unlistable_subdir", "unlistable_reserved_dir",
             "config_named_dir_link", "team_link_outside"),
    **_cells("run_chaos_soak", _B2, "non_utf8_name", "mode_000_file", "link_into_000_dir", "link_to_000_dir", "dir_444",
             "dangling_link", "looping_link", "chain_41_links", "fifo", "config_named_dir",
             "unlistable_config_named_dir", "unlistable_subdir", "unlistable_reserved_dir",
             "config_named_dir_link", "team_link_outside"),
    **_cells("describe_tenant", _X_EXIT, "mode_000_file", "link_into_000_dir", "link_to_000_dir", "dir_444",
             "dangling_link", "looping_link", "chain_41_links", "fifo", "config_named_dir",
             "unlistable_config_named_dir", "unlistable_subdir", "unlistable_reserved_dir",
             "config_named_dir_link", "team_link_outside"),
    **_cells("describe_tenant --replaces", _X_EXIT, "mode_000_file", "link_into_000_dir", "link_to_000_dir",
             "dir_444", "dangling_link", "looping_link", "chain_41_links", "fifo", "config_named_dir",
             "unlistable_config_named_dir", "unlistable_subdir", "unlistable_reserved_dir",
             "config_named_dir_link", "team_link_outside"),
    **_cells("tenant_verify", _X_NAME, "link_to_000_dir", "config_named_dir_link"),
    **_cells("tenant_verify", _X_EXIT, "config_named_dir", "team_link_outside"),
    **_cells("generate_tenant_metadata", _X_EXIT, "link_to_000_dir", "config_named_dir_link",
             "team_link_outside"),
    **_cells("generate_tenant_metadata", _X_NAME, "config_named_dir"),
    **_cells("check_path_metadata_consistency", _X_EXIT, "mode_000_file", "link_into_000_dir",
             "link_to_000_dir", "dir_444", "dangling_link", "looping_link", "chain_41_links", "fifo",
             "config_named_dir", "unlistable_config_named_dir", "unlistable_subdir",
             "unlistable_reserved_dir", "config_named_dir_link", "team_link_outside"),
    **_cells("analyze_rule_pack_gaps", _X_EXIT, "link_to_000_dir", "config_named_dir_link",
             "team_link_outside"),
    **_cells("analyze_rule_pack_gaps", _X_NAME, "config_named_dir"),
    **_cells("explain_route", _X_EXIT, "mode_000_file", "link_into_000_dir", "link_to_000_dir", "dir_444",
             "dangling_link", "looping_link", "chain_41_links", "fifo", "config_named_dir",
             "unlistable_config_named_dir", "unlistable_subdir", "unlistable_reserved_dir",
             "config_named_dir_link", "team_link_outside"),
    **_cells("maintenance_scheduler", _X_NAME, "link_to_000_dir", "config_named_dir",
             "config_named_dir_link", "team_link_outside"),
    **_cells("migration_audit", _X_EXIT, "mode_000_file", "link_into_000_dir", "link_to_000_dir", "dir_444",
             "dangling_link", "looping_link", "chain_41_links", "fifo", "config_named_dir",
             "unlistable_config_named_dir", "unlistable_subdir", "unlistable_reserved_dir",
             "config_named_dir_link", "team_link_outside"),
    **_cells("policy_engine", _X_NAME, "mode_000_file", "link_into_000_dir", "link_to_000_dir", "dir_444",
             "dangling_link", "looping_link", "chain_41_links", "fifo", "config_named_dir",
             "unlistable_config_named_dir", "unlistable_subdir", "config_named_dir_link",
             "team_link_outside"),
    **_cells("policy_engine", _X_EXIT, "unlistable_reserved_dir"),
    **_cells("policy_opa_bridge", _X_EXIT, "link_to_000_dir", "config_named_dir_link", "team_link_outside"),
    **_cells("policy_opa_bridge", _X_NAME, "config_named_dir"),
    **_cells("gitops_check", _X_NAME, "mode_000_file", "link_into_000_dir", "link_to_000_dir", "dir_444",
             "dangling_link", "looping_link", "chain_41_links", "fifo", "config_named_dir",
             "unlistable_config_named_dir", "unlistable_subdir", "unlistable_reserved_dir",
             "config_named_dir_link", "team_link_outside"),
    **_cells("blind_spot_discovery", _X_EXIT, "link_to_000_dir", "config_named_dir_link",
             "team_link_outside"),
    **_cells("blind_spot_discovery", _X_NAME, "config_named_dir"),
    **_cells("threshold_recommend", _X_EXIT, "link_to_000_dir", "config_named_dir_link",
             "team_link_outside"),
    **_cells("threshold_recommend", _X_NAME, "config_named_dir"),
    **_cells("threshold_govern", _X_EXIT, "link_to_000_dir", "config_named_dir_link", "team_link_outside"),
    **_cells("threshold_govern", _X_NAME, "config_named_dir"),
    **_cells("backtest_threshold", _X_EXIT, "link_to_000_dir", "config_named_dir_link",
             "team_link_outside"),
    **_cells("backtest_threshold", _X_NAME, "config_named_dir"),
    # da-guard's own subcommands refuse such a tree since #3206 (utf8_names.go),
    # and every tool that asks one of them now does; this one asks none.
    ("tenant_verify", "non_utf8_name"): _X_UTF8,
    # A file the exporter cannot decode (validation's parse_failed).
    **{(t, r): reason
       for r in ("yaml_syntax_error", "multi_document_file")
       for t, reason in (("init_project", _X_EXIT), ("describe_tenant", _X_EXIT),
                         ("describe_tenant --replaces", _X_EXIT), ("migration_audit", _X_EXIT),
                         ("check_path_metadata_consistency", _X_EXIT), ("explain_route", _X_EXIT),
                         ("policy_engine", _X_NAME), ("gitops_check", _X_NAME),
                         ("run_chaos_soak", _B2), ("drift_detect", _B2), ("config_history", _B2))},
}

# ── running one cell ────────────────────────────────────────────────────

# A `kubectl` that builds the manifest its `--from-file` arguments describe
# (configmap_assemble reads the manifest back and reconciles it): the nobody
# uid has no kubectl, and "kubectl not found" would be a non-zero exit that
# says nothing about the tree.
_KUBECTL = """#!{python}
import json, sys
data = {{}}
for arg in sys.argv[1:]:
    if arg.startswith("--from-file="):
        key, path = arg[len("--from-file="):].split("=", 1)
        data[key] = open(path, "rb").read().decode("utf-8")
doc = {{"apiVersion": "v1", "kind": "ConfigMap",
       "metadata": {{"name": sys.argv[3], "namespace": "monitoring"}}, "data": data}}
sys.stdout.write(json.dumps(doc))
"""

#: Repo directories a tool reads besides `scripts/` and `docs/schemas/`
#: (which the sandbox already holds).
_EXTRA_TREES = ("rule-packs",)

#: Refuses a connection at once (port 9, discard, is not listened on).
_PROM = "http://127.0.0.1:9"


@contextlib.contextmanager
def matrix_box():
    """`unprivileged_sandbox` plus what the table's tools need beside it:
    the rule packs, and a `kubectl` on the PATH the cells run with."""
    with unprivileged_sandbox(str(REPO)) as (box, run):
        box = pathlib.Path(box)
        for d in _EXTRA_TREES:
            shutil.copytree(REPO / d, box / d, ignore=shutil.ignore_patterns("__pycache__"))
        (box / "bin").mkdir()
        (box / "bin" / "kubectl").write_text(_KUBECTL.format(python=sys.executable),
                                             encoding="utf-8")
        (box / "cells").mkdir()
        for p in (box / "bin", box / "cells", *(box / d for d in _EXTRA_TREES)):
            for q in [p, *p.rglob("*")]:
                q.chmod(0o755 if q.is_dir() or q.parent.name == "bin" else 0o644)
        yield box, run


@pytest.fixture(scope="module")
def box():
    with matrix_box() as b:
        yield b


def _open_up(top: pathlib.Path) -> None:
    for dirpath, dirnames, _files in os.walk(top):
        for n in dirnames:
            p = os.path.join(dirpath, n)
            if not os.path.islink(p):
                with contextlib.suppress(OSError):
                    os.chmod(p, 0o755)


#: A tool that blocks (a FIFO opened for reading waits for a writer) is
#: stopped here, well before the sandbox's own 300 s: exit 124 (137 when
#: the TERM was ignored) — a violation like any other, not a hung run.
CELL_TIMEOUT_S = 30
_TIMEOUT = ("timeout", "-k", "5", str(CELL_TIMEOUT_S))
_HANG = {124, 137}
_TRACEBACK = "Traceback (most recent call last)"


def _own(top: pathlib.Path) -> None:
    """Under uid 0 the tools run as 65534: hand it the cell, as a tree is its
    user's on a CI runner (owner bits then decide, and a mode-000 entry still
    denies its owner)."""
    if os.geteuid() != 0:
        return
    os.lchown(top, 65534, 65534)
    for dirpath, dirnames, filenames in os.walk(top):
        for n in (*dirnames, *filenames):
            os.lchown(os.path.join(dirpath, n), 65534, 65534)


@dataclasses.dataclass
class Cell:
    tool: str
    row: str
    oracle_rc: int        # da-guard validation (parse-tree on a refused row)
    oracle_out: str
    named: list[str]      # the paths Go names on this tree, relative to it
    rc: int
    out: str


def go_named(doc: dict) -> list[str]:
    """The paths a validation report requires a reader to name."""
    out = {u["file"] for u in doc.get("unreadable") or ()}
    out.update(doc.get("parse_failed") or ())
    out.update(u["file"] for u in doc.get("undetermined") or ()
               if u.get("reason") == "unreadable_outside_run")
    return sorted(out)


def run_cell(box: pathlib.Path, run, tool: str, row: str) -> Cell:
    # A neutral name: a cell directory named after its row would put the
    # row's words (`team`, `_d`, ...) into every path the tool echoes.
    cell = pathlib.Path(tempfile.mkdtemp(prefix="c-",
                                         dir=box / "cells"))
    try:
        conf = build(cell, ROWS[row])
        spec = TOOLS[tool]
        if spec.prep is not None:
            spec.prep(cell)
        out = cell / "out"
        out.mkdir()
        _own(cell)
        guard = str(box / "da-guard")
        named: list[str] = []
        if ROWS[row]["matrix"] == "refused":
            oracle = run([guard, "parse-tree", "--config-dir", str(conf)])
        else:
            oracle = run([guard, "--config-dir", str(conf), "--format", "json",
                          "--cardinality-limit", "0"])
            if oracle.returncode in (0, 1, 3, 4):
                named = go_named(json.loads(oracle.stdout))
        fmt = {"py": sys.executable, "scripts": str(box / "scripts"), "box": str(box),
               "cell": str(cell), "conf": str(conf), "out": str(out), "prom": _PROM}
        argv = [a.format(**fmt) for a in spec.argv]
        env_path = f"PATH={box / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"
        r = run([*_TIMEOUT, shutil.which("env") or "/usr/bin/env", env_path, *argv])
        return Cell(tool, row, oracle.returncode, oracle.stdout[-800:] + oracle.stderr[-800:],
                    named, r.returncode, _masked(r.stdout + r.stderr, box, cell, conf))
    finally:
        _open_up(cell)
        shutil.rmtree(cell, ignore_errors=True)


def _masked(out: str, box: pathlib.Path, cell: pathlib.Path, conf: pathlib.Path) -> str:
    """*out* with the cell's own location replaced by placeholders, so that
    only what the tool says about the tree's entries can name one."""
    for where, mark in ((str(conf), "<CONF>"), (str(cell), "<CELL>"),
                        (os.path.relpath(cell, box), "<CELL>"), (str(box), "<BOX>")):
        out = out.replace(where, mark)
    return out


def _named(out: str, path: str) -> bool:
    """Whether *out* names the tree entry *path* (root-relative) — as the
    path itself or its base name, as a whole token: not inside a longer
    name (`_d` is not named by `reserved_dir`)."""
    for word in {path, path.rsplit("/", 1)[-1]}:
        # A sentence's full stop after the name still ends it.
        if re.search(r"(?<![\w.-])" + re.escape(word) + r"(?![\w-]|\.[\w-])", out):
            return True
    return False


def oracle_problems(c: Cell) -> list[str]:
    """Why the oracle cannot judge this cell ([] = it can): never a pass."""
    kind = ROWS[c.row]["matrix"]
    if kind == "refused":
        return [] if c.oracle_rc == 2 else [
            f"da-guard parse-tree exited {c.oracle_rc} on a tree it must refuse\n{c.oracle_out}"]
    if kind == "control":
        return [] if c.oracle_rc == 0 else [
            f"da-guard exited {c.oracle_rc} on a control tree\n{c.oracle_out}"]
    if c.oracle_rc not in (3, 4):
        return [(f"da-guard exited {c.oracle_rc} on flagged row {c.row!r}: the cell measures "
                 f"nothing\n{c.oracle_out}")]
    if not c.named:
        return [(f"da-guard names nothing on flagged row {c.row!r}: the cell measures "
                 f"nothing\n{c.oracle_out}")]
    return []


def violations(c: Cell) -> list[str]:
    """What this cell's run broke of the invariant ([] = it holds)."""
    kind = ROWS[c.row]["matrix"]
    tail = c.out[-1500:]
    if kind == "refused":
        return [] if c.rc == 2 else [f"exit {c.rc}, want 2 (parse-tree refuses this tree)\n{tail}"]
    if kind == "control":
        return [] if c.rc == 0 else [f"exit {c.rc} on a tree Go serves (exit 0)\n{tail}"]
    bad = []
    if c.rc in _HANG:
        bad.append(f"stopped after {CELL_TIMEOUT_S}s (exit {c.rc}): the tool hangs")
    elif c.rc == 0:
        bad.append(f"exit 0, want non-zero (Go exits {c.oracle_rc} naming {c.named})")
    for path in c.named:
        if not _named(c.out, path):
            bad.append(f"{path} (named by Go) is not named")
    if _TRACEBACK in c.out:
        bad.append("a Python traceback in the output")
    return bad + ([tail] if bad else [])


def _cases():
    for tool in TOOLS:
        for row in MATRIX_ROWS:
            reason = XFAIL.get((tool, row))
            marks = ([pytest.mark.xfail(strict=True, raises=AssertionError, reason=reason)]
                     if reason else [])
            yield pytest.param(tool, row, id=f"{tool}-{row}", marks=marks)


@pytest.mark.parametrize("tool,row", list(_cases()))
def test_tool_follows_go_on_the_tree(box, tool, row):
    b, run = box
    c = run_cell(b, run, tool, row)
    # Not an assertion: a cell whose oracle cannot judge fails even where
    # the cell is an expected failure (xfail catches AssertionError only).
    bad = oracle_problems(c)
    if bad:
        pytest.fail("\n".join(bad))
    bad = violations(c)
    assert not bad, f"{tool} on {row}:\n" + "\n".join(bad)


# ── the table's own consistency ─────────────────────────────────────────


def test_xfail_keys_are_cells_of_the_matrix():
    stale = sorted(k for k in XFAIL if k[0] not in TOOLS or k[1] not in MATRIX_ROWS)
    assert not stale, f"XFAIL entries for no cell: {stale}"
    for key, reason in XFAIL.items():
        assert reason.startswith(("X: ", "β2: ")), f"{key}: the reason names no follow-up: {reason!r}"
    for tool in ("operator_generate", "migrate_to_operator"):
        assert not [k for k in XFAIL if k[0] == tool], f"{tool} must pass every cell"


def test_catalogue_rows_have_a_matrix_class():
    for r in CATALOG["rows"]:
        assert r["matrix"] in ("control", "flagged", "refused", "go_only"), r["id"]
    assert {"control", "flagged", "refused"} <= {ROWS[r]["matrix"] for r in MATRIX_ROWS}


def _has_entry_point(path: pathlib.Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(isinstance(n, ast.If) and "__main__" in ast.unparse(n.test) for n in tree.body)


def _population() -> set[str]:
    import test_confd_enumeration_contract as enum
    scripts = REPO / "scripts"
    return {p.relative_to(scripts).as_posix() for p in enum._readers() if _has_entry_point(p)}


def test_every_population_tool_is_in_the_matrix_or_says_why():
    """Both ways: a population tool neither in `TOOLS` nor in `NOT_IN_MATRIX`
    is red, and so is an entry of either that is no longer in the population."""
    population = _population()
    assert len(population) > 20, f"the population collapsed: {sorted(population)}"
    in_table = {t.script_path() for t in TOOLS.values()}
    missing = sorted(population - in_table - set(NOT_IN_MATRIX))
    assert not missing, (
        f"conf.d tools with an entry point in neither TOOLS nor NOT_IN_MATRIX: {missing} — "
        "add a row (preferred), or say why it reads no conf.d tree")
    both = sorted(in_table & set(NOT_IN_MATRIX))
    assert not both, f"in TOOLS and in NOT_IN_MATRIX: {both}"
    stale = sorted(set(NOT_IN_MATRIX) - population)
    assert not stale, f"NOT_IN_MATRIX entries no longer in the population: {stale}"
    outside = sorted(in_table - population - OUTSIDE_POPULATION)
    assert not outside, f"TOOLS rows outside the population (and not in OUTSIDE_POPULATION): {outside}"
    for rel in in_table | set(NOT_IN_MATRIX):
        assert (REPO / "scripts" / rel).is_file(), f"{rel}: no such script"
